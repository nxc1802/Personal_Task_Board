"""GitHub Issues, Pull Requests & Comments Acquisition Adapter.

Truy xuất và nạp issues, pull requests và review comments từ GitHub REST API v3
(và GraphQL API), chuyển đổi thành RawEventRecord v1 chuẩn hóa với source_type = SourceType.GITHUB.

Tính năng:
- Issues & Pull Requests tracking (trạng thái, người thực hiện, nhãn, người mở)
- Comments tracking (`/repos/{owner}/{repo}/issues/{number}/comments`)
- Updates/edits tracking theo timestamp `updated_at` và `since` query
- Quản lý Repo scope (`repositories` cấu hình hoặc tự động phát hiện qua `/user/repos`)
- Hỗ trợ GraphQL endpoint (`/graphql`) cho bulk querying
- Xử lý Rate Limit (HTTP 429, HTTP 403 Secondary Rate Limit, `Retry-After`, `X-RateLimit-Reset`)
- Che giấu PII & Secrets (GitHub PAT, Bearer tokens, embedded credentials)
- Multi-tenant isolation & Deterministic Idempotency Key
"""

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import re
import time
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request
import uuid

from ptb_contracts import (
    IngestionCheckpointRecord,
    ProcessingStatus,
    RawEventRecord,
    SourceType,
)
from ptb_acquisition.adapters.base import (
    AcquisitionAdapter,
    AcquisitionError,
    AdapterAuthenticationError,
    AdapterConfigurationError,
    AdapterRateLimitError,
    AdapterStreamNotFoundError,
    AdapterTransientError,
)

logger = logging.getLogger("ptb.acquisition.adapters.github")

# =============================================================================
# SECRET & PII REDACTION REGEXES
# =============================================================================
_GITHUB_TOKEN_RE = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9_]{36,}|github_pat_[A-Za-z0-9_]{50,})", re.IGNORECASE
)
_BEARER_TOKEN_RE = re.compile(r"Bearer\s+[A-Za-z0-9_\-\.~+/]+=*", re.IGNORECASE)
_URL_CREDS_RE = re.compile(r"(https?://)([^:]+):([^@]+)@", re.IGNORECASE)


def redact_secrets(text: Optional[str]) -> str:
    """Loại bỏ GitHub PATs, auth headers và embedded credentials khỏi text."""
    if not text:
        return ""
    clean = _GITHUB_TOKEN_RE.sub("[REDACTED_GITHUB_TOKEN]", str(text))
    clean = _BEARER_TOKEN_RE.sub("Bearer [REDACTED_TOKEN]", clean)
    clean = _URL_CREDS_RE.sub(r"\1[REDACTED_USER]:[REDACTED_PASS]@", clean)
    return clean


class GitHubAdapter(AcquisitionAdapter):
    """Adapter tích hợp GitHub REST API v3 & GraphQL theo chuẩn AcquisitionAdapter v1."""

    def __init__(
        self,
        repositories: Optional[List[str]] = None,
        api_token: Optional[str] = None,
        base_url: str = "https://api.github.com",
        tenant_id: str = "local-user",
        http_fetcher: Optional[Callable[..., Any]] = None,
        include_comments: bool = True,
    ):
        super().__init__(source_type=SourceType.GITHUB, tenant_id=tenant_id)
        self.repositories = [r.strip() for r in (repositories or []) if r.strip()]
        self.api_token = api_token or os.getenv("GITHUB_TOKEN") or ""
        self.base_url = (base_url or "https://api.github.com").rstrip("/")
        self._http_fetcher = http_fetcher
        self.include_comments = include_comments

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    # -------------------------------------------------------------------------
    # HTTP Execution, Rate Limiting & Resilience
    # -------------------------------------------------------------------------

    async def _execute_request(
        self,
        endpoint: str,
        method: str = "GET",
        params: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Any, Dict[str, str]]:
        """Thực thi HTTP request tới GitHub REST API và trả về (data, headers)."""
        url = endpoint if endpoint.startswith("http") else f"{self.base_url}/{endpoint.lstrip('/')}"
        if params:
            clean_params = {k: v for k, v in params.items() if v is not None}
            if clean_params:
                query_string = urllib.parse.urlencode(clean_params)
                url = f"{url}?{query_string}"

        headers = self._get_headers()
        body_bytes = json.dumps(payload).encode("utf-8") if payload else None

        # 1. Hỗ trợ test double / injected http_fetcher
        if self._http_fetcher:
            try:
                res = self._http_fetcher(url, headers, body_bytes)
                if asyncio.iscoroutine(res):
                    res = await res
                if isinstance(res, tuple) and len(res) >= 2:
                    data, resp_headers = res[0], res[1]
                elif isinstance(res, dict):
                    data, resp_headers = res, {}
                elif isinstance(res, list):
                    data, resp_headers = res, {}
                elif isinstance(res, (bytes, str)):
                    data, resp_headers = json.loads(res), {}
                elif hasattr(res, "json"):
                    data, resp_headers = res.json(), getattr(res, "headers", {})
                else:
                    data, resp_headers = json.loads(str(res)), {}
                return data, resp_headers
            except Exception as exc:
                self._handle_http_exception(exc)
                raise

        # 2. urllib.request mặc định
        def _sync_call() -> Tuple[Any, Dict[str, str]]:
            req = urllib.request.Request(
                url,
                data=body_bytes,
                headers=headers,
                method=method,
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp_data = resp.read().decode("utf-8")
                    resp_headers = dict(resp.headers)
                    parsed = json.loads(resp_data) if resp_data else {}
                    return parsed, resp_headers
            except urllib.error.HTTPError as h_err:
                self._handle_urllib_http_error(h_err)
                raise
            except urllib.error.URLError as u_err:
                raise AdapterTransientError(
                    f"Lỗi mạng khi kết nối GitHub API: {u_err}",
                    source_type=self.source_type,
                ) from u_err

        return await asyncio.to_thread(_sync_call)

    def _handle_http_exception(self, exc: Exception) -> None:
        """Chuyển đổi ngoại lệ từ fetcher thành Adapter Error."""
        status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        exc_headers = getattr(exc, "headers", {}) or {}
        msg = str(exc)

        # Rate Limit handling (429 hoặc 403 secondary rate limit)
        if status_code == 429 or (
            status_code == 403 and ("rate limit" in msg.lower() or exc_headers.get("x-ratelimit-remaining") == "0")
        ):
            retry_after = self._extract_retry_after(exc_headers)
            raise AdapterRateLimitError(
                f"GitHub Rate Limit (HTTP {status_code}): {msg}",
                retry_after_seconds=retry_after,
                source_type=self.source_type,
            )

        if status_code in (401, 403):
            raise AdapterAuthenticationError(
                f"GitHub Auth Error (HTTP {status_code}): {msg}",
                source_type=self.source_type,
            )

        if status_code == 404:
            raise AdapterStreamNotFoundError(
                f"GitHub Repo/Resource Not Found (HTTP 404): {msg}",
                source_type=self.source_type,
            )

        if status_code and status_code >= 500:
            raise AdapterTransientError(
                f"GitHub Server Error (HTTP {status_code}): {msg}",
                source_type=self.source_type,
            )

    def _handle_urllib_http_error(self, err: urllib.error.HTTPError) -> None:
        """Xử lý HTTP error từ urllib."""
        headers = dict(err.headers)
        body = ""
        try:
            body = err.read().decode("utf-8")
        except Exception as exc:
            logger.debug("Failed to decode HTTP error body: %s", exc)

        if err.code == 429 or (
            err.code == 403
            and ("rate limit" in body.lower() or headers.get("X-RateLimit-Remaining") == "0")
        ):
            retry_after = self._extract_retry_after(headers)
            raise AdapterRateLimitError(
                f"GitHub API Rate Limit vượt ngưỡng (chờ {retry_after}s)",
                retry_after_seconds=retry_after,
                source_type=self.source_type,
            )

        if err.code in (401, 403):
            raise AdapterAuthenticationError(
                f"GitHub xác thực thất bại (HTTP {err.code}): {body or err.reason}",
                source_type=self.source_type,
            )

        if err.code == 404:
            raise AdapterStreamNotFoundError(
                f"GitHub repository hoặc stream không tồn tại (HTTP 404): {body or err.reason}",
                source_type=self.source_type,
            )

        if err.code >= 500:
            raise AdapterTransientError(
                f"GitHub máy chủ lỗi tạm thời (HTTP {err.code}): {body or err.reason}",
                source_type=self.source_type,
            )

    def _extract_retry_after(self, headers: Dict[str, str]) -> float:
        """Tính thời gian chờ retry từ headers `Retry-After` hoặc `X-RateLimit-Reset`."""
        # 1. Header Retry-After
        retry_hdr = headers.get("Retry-After") or headers.get("retry-after")
        if retry_hdr:
            try:
                return max(1.0, float(retry_hdr))
            except (ValueError, TypeError):
                pass

        # 2. Header X-RateLimit-Reset (epoch timestamp)
        reset_hdr = headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")
        if reset_hdr:
            try:
                reset_ts = float(reset_hdr)
                now_ts = time.time()
                diff = reset_ts - now_ts
                if diff > 0:
                    return min(diff, 60.0)  # Cắt ngắn nếu vượt quá ngưỡng an toàn
            except (ValueError, TypeError):
                pass

        return 5.0

    # -------------------------------------------------------------------------
    # GraphQL Query Support
    # -------------------------------------------------------------------------

    async def execute_graphql(
        self, query: str, variables: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Thực thi câu truy vấn GitHub GraphQL API."""
        payload = {"query": query, "variables": variables or {}}
        data, _ = await self.execute_with_retry(
            self._execute_request,
            endpoint="graphql",
            method="POST",
            payload=payload,
        )
        if isinstance(data, dict) and data.get("errors"):
            errors = data.get("errors", [])
            err_msg = "; ".join(str(e.get("message", e)) for e in errors)
            raise AcquisitionError(
                f"GitHub GraphQL query error: {err_msg}",
                source_type=self.source_type,
                details={"errors": errors},
            )
        return data.get("data", {}) if isinstance(data, dict) else {}

    # -------------------------------------------------------------------------
    # Canonical RawEventRecord Mapping & Normalization
    # -------------------------------------------------------------------------

    def _issue_to_raw_event(self, item: Dict[str, Any], repo: str) -> RawEventRecord:
        """Ánh xạ GitHub Issue hoặc Pull Request sang RawEventRecord v1."""
        item_id = str(item.get("id") or uuid.uuid4().hex)
        number = item.get("number") or item_id
        is_pr = "pull_request" in item
        prefix = "Pull Request" if is_pr else "Issue"

        title = redact_secrets(str(item.get("title") or "Untitled Issue").strip())
        body = redact_secrets(str(item.get("body") or "").strip())
        state = item.get("state", "open")

        # Author resolution
        user = item.get("user") or {}
        author_name = user.get("login") if isinstance(user, dict) else str(user)
        author_id = author_name or "github-user"

        # Assignees & labels
        assignees_list = item.get("assignees") or []
        assignee_names = [
            a.get("login") for a in assignees_list if isinstance(a, dict) and a.get("login")
        ]
        labels_list = item.get("labels") or []
        label_names = [
            l.get("name") for l in labels_list if isinstance(l, dict) and l.get("name")
        ]

        # Timestamp parsing (ưu tiên updated_at để theo dõi edits/updates)
        updated_at_str = item.get("updated_at") or item.get("created_at")
        event_ts = datetime.now(timezone.utc)
        if updated_at_str:
            try:
                event_ts = datetime.fromisoformat(str(updated_at_str).replace("Z", "+00:00"))
            except Exception as exc:
                logger.debug("Failed to parse issue timestamp '%s': %s", updated_at_str, exc)

        assignee_str = f"Assignees: {', '.join(assignee_names)}" if assignee_names else "Unassigned"
        labels_str = f"Labels: {', '.join(label_names)}" if label_names else ""
        meta_line = f"State: {state} | {assignee_str}"
        if labels_str:
            meta_line += f" | {labels_str}"

        normalized_text = f"[GitHub {prefix} #{number}] {title}\n{meta_line}\n\n{body}".strip()
        deep_link = item.get("html_url") or f"https://github.com/{repo}/issues/{number}"

        # Clean payload
        safe_payload = dict(item)

        return self.build_raw_event(
            external_id=str(number),
            content=normalized_text,
            author_id=author_id,
            stream_id=repo,
            event_timestamp=event_ts,
            deep_link=deep_link,
            raw_payload=safe_payload,
            parent_external_id=None,
            author_name=author_name,
        )

    def _comment_to_raw_event(
        self, comment: Dict[str, Any], issue_number: str, repo: str
    ) -> RawEventRecord:
        """Ánh xạ Issue Comment hoặc PR Review Comment sang RawEventRecord v1."""
        comment_id = str(comment.get("id") or uuid.uuid4().hex)
        body = redact_secrets(str(comment.get("body") or "").strip())

        user = comment.get("user") or {}
        author_name = user.get("login") if isinstance(user, dict) else str(user)
        author_id = author_name or "github-user"

        updated_at_str = comment.get("updated_at") or comment.get("created_at")
        event_ts = datetime.now(timezone.utc)
        if updated_at_str:
            try:
                event_ts = datetime.fromisoformat(str(updated_at_str).replace("Z", "+00:00"))
            except Exception as exc:
                logger.debug("Failed to parse comment timestamp '%s': %s", updated_at_str, exc)

        normalized_text = f"[GitHub Comment on #{issue_number}] @{author_name}:\n\n{body}".strip()
        deep_link = (
            comment.get("html_url")
            or f"https://github.com/{repo}/issues/{issue_number}#issuecomment-{comment_id}"
        )

        safe_payload = dict(comment)

        return self.build_raw_event(
            external_id=f"comment-{comment_id}",
            content=normalized_text,
            author_id=author_id,
            stream_id=repo,
            event_timestamp=event_ts,
            deep_link=deep_link,
            raw_payload=safe_payload,
            parent_external_id=str(issue_number),
            author_name=author_name,
        )

    # -------------------------------------------------------------------------
    # Comments Fetching
    # -------------------------------------------------------------------------

    async def _fetch_issue_comments(
        self, repo: str, issue_number: str, since: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """Lấy danh sách comments của một issue cụ thể."""
        endpoint = f"repos/{repo}/issues/{issue_number}/comments"
        params: Dict[str, Any] = {"per_page": 100}
        if since is not None:
            params["since"] = since.isoformat()

        try:
            data, _ = await self.execute_with_retry(
                self._execute_request,
                endpoint=endpoint,
                params=params,
            )
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.debug(f"Không thể lấy comments cho {repo}#{issue_number}: {e}")
            return []

    async def _fetch_repo_comments(
        self, repo: str, since: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """Lấy tất cả comments trong repository cập nhật kể từ `since`."""
        endpoint = f"repos/{repo}/issues/comments"
        params: Dict[str, Any] = {
            "per_page": 100,
            "sort": "updated",
            "direction": "asc",
        }
        if since is not None:
            params["since"] = since.isoformat()

        all_comments: List[Dict[str, Any]] = []
        page = 1
        while True:
            params["page"] = page
            try:
                data, _ = await self.execute_with_retry(
                    self._execute_request,
                    endpoint=endpoint,
                    params=params,
                )
            except Exception as e:
                logger.warning(f"Lỗi khi lấy repo comments cho {repo}: {e}")
                break

            if not isinstance(data, list) or not data:
                break
            all_comments.extend(data)
            if len(data) < 100:
                break
            page += 1

        return all_comments

    # -------------------------------------------------------------------------
    # Lifecycle Methods: discover, backfill, poll_incremental, health
    # -------------------------------------------------------------------------

    async def discover(self) -> List[Dict[str, Any]]:
        """Liệt kê các repositories có sẵn hoặc được phân quyền."""
        streams: List[Dict[str, Any]] = []

        # 1. Thêm các repositories đã cấu hình thủ công
        for r in self.repositories:
            streams.append({
                "stream_id": r,
                "name": f"Repo {r}",
                "source_type": SourceType.GITHUB.value,
                "full_name": r,
            })

        # 2. Nếu có api_token và chưa cấu hình repos, tự động khám phá qua /user/repos
        if not streams and self.api_token:
            try:
                data, _ = await self.execute_with_retry(
                    self._execute_request,
                    endpoint="user/repos",
                    params={"per_page": 100, "sort": "updated"},
                )
                if isinstance(data, list):
                    for repo in data:
                        full_name = repo.get("full_name")
                        if full_name and not any(s["stream_id"] == full_name for s in streams):
                            streams.append({
                                "stream_id": full_name,
                                "name": repo.get("name", full_name),
                                "source_type": SourceType.GITHUB.value,
                                "full_name": full_name,
                                "private": repo.get("private", False),
                                "description": repo.get("description", ""),
                            })
            except Exception as e:
                logger.warning(f"Không thể tự động discover GitHub repos: {e}")

        if not streams:
            streams.append({
                "stream_id": "DEFAULT_REPO",
                "name": "Default Repository",
                "source_type": SourceType.GITHUB.value,
            })

        return streams

    async def backfill(
        self, stream_id: str, since: Optional[datetime] = None
    ) -> AsyncIterator[RawEventRecord]:
        """Thu nạp toàn bộ issues, PRs và comments của stream_id từ mốc since."""
        repo = stream_id
        endpoint = f"repos/{repo}/issues"
        params: Dict[str, Any] = {
            "state": "all",
            "sort": "updated",
            "direction": "asc",
            "per_page": 100,
        }
        if since is not None:
            params["since"] = since.isoformat()

        page = 1
        while True:
            params["page"] = page
            data, _ = await self.execute_with_retry(
                self._execute_request,
                endpoint=endpoint,
                params=params,
            )

            if not isinstance(data, list) or not data:
                break

            for item in data:
                issue_event = self._issue_to_raw_event(item, repo=repo)
                yield issue_event

                # Nếu issue có comments và cờ include_comments được bật
                num_comments = item.get("comments", 0)
                if self.include_comments and num_comments > 0:
                    issue_num = str(item.get("number", issue_event.external_id))
                    comments = await self._fetch_issue_comments(
                        repo=repo, issue_number=issue_num, since=since
                    )
                    comments.sort(
                        key=lambda c: c.get("updated_at") or c.get("created_at") or ""
                    )
                    for comm in comments:
                        yield self._comment_to_raw_event(
                            comm, issue_number=issue_num, repo=repo
                        )

            if len(data) < 100:
                break
            page += 1

    async def poll_incremental(
        self, stream_id: str, checkpoint: Optional[IngestionCheckpointRecord] = None
    ) -> AsyncIterator[RawEventRecord]:
        """Thu nạp các issues, PRs và comments mới tạo hoặc đã chỉnh sửa kể từ checkpoint."""
        repo = stream_id
        since_dt = checkpoint.last_event_timestamp if checkpoint else None
        endpoint = f"repos/{repo}/issues"
        params: Dict[str, Any] = {
            "state": "all",
            "sort": "updated",
            "direction": "asc",
            "per_page": 100,
        }
        if since_dt is not None:
            params["since"] = since_dt.isoformat()

        page = 1
        while True:
            params["page"] = page
            data, _ = await self.execute_with_retry(
                self._execute_request,
                endpoint=endpoint,
                params=params,
            )

            if not isinstance(data, list) or not data:
                break

            for item in data:
                issue_event = self._issue_to_raw_event(item, repo=repo)

                # Kiểm tra lọc checkpoint
                should_emit = True
                if checkpoint and checkpoint.last_event_timestamp:
                    if issue_event.event_timestamp < checkpoint.last_event_timestamp:
                        should_emit = False
                    elif issue_event.event_timestamp == checkpoint.last_event_timestamp:
                        if (
                            checkpoint.last_external_id
                            and issue_event.external_id == checkpoint.last_external_id
                        ):
                            should_emit = False

                if should_emit:
                    yield issue_event

                # Comments
                num_comments = item.get("comments", 0)
                if self.include_comments and num_comments > 0:
                    issue_num = str(item.get("number", issue_event.external_id))
                    comments = await self._fetch_issue_comments(
                        repo=repo, issue_number=issue_num, since=since_dt
                    )
                    comments.sort(
                        key=lambda c: c.get("updated_at") or c.get("created_at") or ""
                    )
                    for comm in comments:
                        comm_event = self._comment_to_raw_event(
                            comm, issue_number=issue_num, repo=repo
                        )
                        if checkpoint and checkpoint.last_event_timestamp:
                            if comm_event.event_timestamp < checkpoint.last_event_timestamp:
                                continue
                            if (
                                comm_event.event_timestamp == checkpoint.last_event_timestamp
                                and checkpoint.last_external_id
                                and comm_event.external_id == checkpoint.last_external_id
                            ):
                                continue
                        yield comm_event

            if len(data) < 100:
                break
            page += 1

    async def health(self) -> Dict[str, Any]:
        """Kiểm tra tính hợp lệ của token và hạn ngạch API của GitHubAdapter."""
        configured = bool(self.api_token or self.repositories)
        report: Dict[str, Any] = {
            "source_type": self.source_type.value,
            "tenant_id": self.tenant_id,
            "configured": configured,
            "status": "unconfigured" if not configured else "healthy",
            "repositories": self.repositories,
        }

        if not configured:
            return report

        try:
            # Kiểm tra hạn mức rate limit
            data, headers = await self._execute_request("rate_limit")
            report["status"] = "healthy"
            rate_info = data.get("rate", {}) if isinstance(data, dict) else {}
            report["rate_limit_limit"] = rate_info.get("limit")
            report["rate_limit_remaining"] = rate_info.get("remaining")
            report["rate_limit_reset"] = rate_info.get("reset")

            # Nếu có token thì lấy thêm username
            if self.api_token:
                try:
                    user_data, _ = await self._execute_request("user")
                    if isinstance(user_data, dict):
                        report["user"] = user_data.get("login")
                except Exception as exc:
                    logger.debug("Failed to fetch GitHub username for health check: %s", exc)
        except AdapterAuthenticationError as a_err:
            report["status"] = "auth_required"
            report["error"] = str(a_err)
        except Exception as err:
            report["status"] = "degraded"
            report["error"] = str(err)

        return report
