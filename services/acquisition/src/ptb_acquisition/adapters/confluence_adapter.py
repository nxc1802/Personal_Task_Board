"""Confluence Cloud / Server Acquisition Adapter.

Truy xuất và nạp tài liệu, meeting notes, action items và comments từ Confluence REST API v1/v2
kèm hỗ trợ Confluence Query Language (CQL), chuyển đổi thành RawEventRecord v1 chuẩn hóa với source_type = SourceType.CONFLUENCE.

Tính năng:
- Pages & Blogposts tracking kèm version history (`version.number`, `version.when`)
- Trích xuất tự động Action Items / Task Lists từ Storage format (`<ac:task>`, `<ac:task-body>`)
- Comments tracking (`/wiki/rest/api/content/{page_id}/child/comment`)
- Hỗ trợ câu truy vấn CQL linh hoạt (`cql:...` hoặc theo Space Key)
- User resolution & Assignee extraction (`ri:user`, `createdBy`, `lastModifiedBy`)
- Hỗ trợ xác thực Confluence Cloud (Basic `email:api_token`) và Confluence Server (Bearer PAT)
- Phân trang chuẩn hóa (`start`, `limit`, `_links.next`)
- Xử lý Rate Limiting (HTTP 429, `Retry-After`), Timeout & HTTP 5xx retry
- Redaction PII & Secrets (Atlassian tokens, Basic auth, Passwords)
- Multi-tenant isolation & Deterministic Idempotency Key
"""

import asyncio
import base64
from datetime import datetime, timezone
import html
import json
import logging
import os
import re
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

logger = logging.getLogger("ptb.acquisition.adapters.confluence")

# =============================================================================
# SECRET & PII REDACTION REGEXES
# =============================================================================
_ATLASSIAN_TOKEN_RE = re.compile(r"(?:ATATT[A-Za-z0-9_\-=]{40,}|Bearer\s+[A-Za-z0-9_\-\.~+/]+=*)", re.IGNORECASE)
_BASIC_AUTH_RE = re.compile(r"Basic\s+[A-Za-z0-9+/=]{10,}", re.IGNORECASE)
_URL_CREDS_RE = re.compile(r"(https?://)([^:]+):([^@]+)@", re.IGNORECASE)

# HTML / Storage Format Regexes
_TASK_TAG_RE = re.compile(
    r"<ac:task>(?:.*?<ac:task-status>(?P<status>complete|incomplete)</ac:task-status>)?.*?<ac:task-body>(?P<body>.*?)</ac:task-body>.*?</ac:task>",
    re.DOTALL | re.IGNORECASE,
)
_USER_MENTION_RE = re.compile(
    r'<ri:user\s+(?:ri:account-id="(?P<aid>[^"]+)"|ri:userkey="(?P<ukey>[^"]+)"|ri:username="(?P<uname>[^"]+)")[^>]*/>',
    re.IGNORECASE,
)
_TAG_CLEANER_RE = re.compile(r"<[^>]+>")


def redact_secrets(text: Optional[str]) -> str:
    """Loại bỏ token, Basic auth và credentials khỏi text."""
    if not text:
        return ""
    clean = _ATLASSIAN_TOKEN_RE.sub("[REDACTED_ATLASSIAN_TOKEN]", str(text))
    clean = _BASIC_AUTH_RE.sub("Basic [REDACTED_BASIC_AUTH]", clean)
    clean = _URL_CREDS_RE.sub(r"\1[REDACTED_USER]:[REDACTED_PASS]@", clean)
    return clean


def parse_confluence_storage(body_html: str) -> Tuple[str, List[str]]:
    """Phân tích Confluence Storage XHTML để trích xuất plain text và danh sách Action Items."""
    if not body_html:
        return "", []

    action_items: List[str] = []

    # 1. Trích xuất <ac:task> action items
    for match in _TASK_TAG_RE.finditer(body_html):
        status = match.group("status") or "incomplete"
        task_body_raw = match.group("body") or ""
        # Clean user mentions inside task
        clean_task_body = _USER_MENTION_RE.sub(
            lambda m: f"@{m.group('uname') or m.group('aid') or m.group('ukey')}",
            task_body_raw,
        )
        clean_task_body = _TAG_CLEANER_RE.sub("", clean_task_body).strip()
        clean_task_body = html.unescape(clean_task_body)

        check_mark = "[x]" if status.lower() == "complete" else "[ ]"
        if clean_task_body:
            action_items.append(f"{check_mark} {clean_task_body}")

    # 2. Xóa các ac:task khỏi text chính để tránh duplicate
    without_tasks = _TASK_TAG_RE.sub("", body_html)

    # 3. Chuẩn hóa user mentions
    clean_mentions = _USER_MENTION_RE.sub(
        lambda m: f"@{m.group('uname') or m.group('aid') or m.group('ukey')}",
        without_tasks,
    )

    # 4. Strip còn lại các thẻ HTML
    plain_text = _TAG_CLEANER_RE.sub(" ", clean_mentions)
    plain_text = html.unescape(plain_text)
    plain_text = re.sub(r"\s+", " ", plain_text).strip()

    return plain_text, action_items


class ConfluenceAdapter(AcquisitionAdapter):
    """Adapter tích hợp Confluence REST API (Cloud & Server) chuẩn v1."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
        space_keys: Optional[List[str]] = None,
        tenant_id: str = "local-user",
        http_fetcher: Optional[Callable[..., Any]] = None,
        include_comments: bool = True,
    ):
        super().__init__(source_type=SourceType.CONFLUENCE, tenant_id=tenant_id)
        self.base_url = (base_url or os.getenv("CONFLUENCE_BASE_URL", "")).rstrip("/")
        self.email = email or os.getenv("CONFLUENCE_EMAIL", "")
        self.api_token = api_token or os.getenv("CONFLUENCE_API_TOKEN", "")
        self.space_keys = [s.strip() for s in (space_keys or []) if s.strip()]
        self._http_fetcher = http_fetcher
        self.include_comments = include_comments

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.email and self.api_token:
            auth_str = f"{self.email}:{self.api_token}"
            encoded = base64.b64encode(auth_str.encode("utf-8")).decode("utf-8")
            headers["Authorization"] = f"Basic {encoded}"
        elif self.api_token:
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
    ) -> Dict[str, Any]:
        """Thực thi HTTP request tới Confluence REST API."""
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
                if isinstance(res, dict):
                    data = res
                elif isinstance(res, (bytes, str)):
                    data = json.loads(res)
                elif hasattr(res, "json"):
                    data = res.json()
                elif hasattr(res, "read"):
                    data = json.loads(res.read().decode("utf-8"))
                else:
                    data = json.loads(str(res))
                return data
            except Exception as exc:
                self._handle_http_exception(exc)
                raise

        # 2. urllib.request mặc định
        if not self.base_url:
            raise AdapterConfigurationError(
                "Confluence base_url chưa được thiết lập (CONFLUENCE_BASE_URL)",
                source_type=self.source_type,
            )

        def _sync_call() -> Dict[str, Any]:
            req = urllib.request.Request(
                url,
                data=body_bytes,
                headers=headers,
                method=method,
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp_data = resp.read().decode("utf-8")
                    return json.loads(resp_data) if resp_data else {}
            except urllib.error.HTTPError as h_err:
                self._handle_urllib_http_error(h_err)
                raise
            except urllib.error.URLError as u_err:
                raise AdapterTransientError(
                    f"Lỗi mạng khi kết nối Confluence: {u_err}",
                    source_type=self.source_type,
                ) from u_err

        return await asyncio.to_thread(_sync_call)

    def _handle_http_exception(self, exc: Exception) -> None:
        """Chuyển đổi các ngoại lệ từ http_fetcher sang Adapter Error."""
        status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        exc_headers = getattr(exc, "headers", {}) or {}
        msg = str(exc)

        if status_code == 429:
            retry_after = 5.0
            if "Retry-After" in exc_headers:
                try:
                    retry_after = float(exc_headers["Retry-After"])
                except (ValueError, TypeError):
                    pass
            raise AdapterRateLimitError(
                f"Confluence Rate Limit (HTTP 429): {msg}",
                retry_after_seconds=retry_after,
                source_type=self.source_type,
            )

        if status_code in (401, 403):
            raise AdapterAuthenticationError(
                f"Confluence Auth Error (HTTP {status_code}): {msg}",
                source_type=self.source_type,
            )

        if status_code == 404:
            raise AdapterStreamNotFoundError(
                f"Confluence Space/Page Not Found (HTTP 404): {msg}",
                source_type=self.source_type,
            )

        if status_code and status_code >= 500:
            raise AdapterTransientError(
                f"Confluence Server Error (HTTP {status_code}): {msg}",
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

        if err.code == 429:
            retry_after = 5.0
            hdr_val = headers.get("Retry-After") or headers.get("retry-after")
            if hdr_val:
                try:
                    retry_after = float(hdr_val)
                except (ValueError, TypeError):
                    pass
            raise AdapterRateLimitError(
                f"Confluence Rate Limit 429: {body or err.reason}",
                retry_after_seconds=retry_after,
                source_type=self.source_type,
            )

        if err.code in (401, 403):
            raise AdapterAuthenticationError(
                f"Confluence xác thực thất bại (HTTP {err.code}): {body or err.reason}",
                source_type=self.source_type,
            )

        if err.code == 404:
            raise AdapterStreamNotFoundError(
                f"Confluence tài nguyên không tồn tại (HTTP 404): {body or err.reason}",
                source_type=self.source_type,
            )

        if err.code >= 500:
            raise AdapterTransientError(
                f"Confluence máy chủ lỗi tạm thời (HTTP {err.code}): {body or err.reason}",
                source_type=self.source_type,
            )

    # -------------------------------------------------------------------------
    # Canonical RawEventRecord Mapping & Normalization
    # -------------------------------------------------------------------------

    def _page_to_raw_event(self, page_item: Dict[str, Any], space_key: str) -> RawEventRecord:
        """Ánh xạ Confluence Page/Document sang RawEventRecord v1."""
        page_id = str(page_item.get("id") or uuid.uuid4().hex)
        title = redact_secrets(str(page_item.get("title") or "Untitled Document").strip())

        # Extract body
        body_dict = page_item.get("body", {})
        raw_storage = ""
        if isinstance(body_dict, dict):
            raw_storage = str(
                body_dict.get("storage", {}).get("value")
                or body_dict.get("view", {}).get("value")
                or ""
            )
        else:
            raw_storage = str(page_item.get("content") or "")

        plain_text, action_items = parse_confluence_storage(raw_storage)
        clean_text = redact_secrets(plain_text)

        # Version tracking
        version_dict = page_item.get("version") or {}
        version_num = version_dict.get("number", 1) if isinstance(version_dict, dict) else 1
        version_when = (
            version_dict.get("when")
            or page_item.get("history", {}).get("createdDate")
            or page_item.get("history", {}).get("lastUpdated")
        )

        # Author / Assignee resolution
        history = page_item.get("history") or {}
        created_by = (
            history.get("createdBy")
            or version_dict.get("by")
            or {}
        ) if isinstance(history, dict) else {}
        author_name = (
            created_by.get("displayName")
            or created_by.get("publicName")
            or page_item.get("author")
            or "Confluence Author"
        )
        author_id = (
            created_by.get("accountId")
            or created_by.get("userKey")
            or author_name
        )

        event_ts = datetime.now(timezone.utc)
        if version_when:
            try:
                event_ts = datetime.fromisoformat(str(version_when).replace("Z", "+00:00"))
            except Exception as exc:
                logger.debug("Failed to parse version timestamp '%s': %s", version_when, exc)

        # Build normalized text with action items section
        norm_parts = [f"[Confluence: {title}] (Space: {space_key}, v{version_num})"]
        if clean_text:
            norm_parts.append(clean_text)
        if action_items:
            norm_parts.append("Action Items:\n" + "\n".join(f"- {ai}" for ai in action_items))

        normalized_text = "\n\n".join(norm_parts).strip()

        # Deep link
        deep_link = ""
        links = page_item.get("_links", {})
        if isinstance(links, dict) and links.get("webui"):
            webui = links["webui"]
            deep_link = f"{self.base_url}{webui}" if self.base_url else webui
        if not deep_link:
            deep_link = f"{self.base_url}/pages/{page_id}" if self.base_url else f"/pages/{page_id}"

        safe_payload = dict(page_item)

        return self.build_raw_event(
            external_id=page_id,
            content=normalized_text,
            author_id=author_id,
            stream_id=space_key,
            event_timestamp=event_ts,
            deep_link=deep_link,
            raw_payload=safe_payload,
            parent_external_id=None,
            author_name=author_name,
        )

    def _comment_to_raw_event(
        self, comment_item: Dict[str, Any], page_id: str, space_key: str
    ) -> RawEventRecord:
        """Ánh xạ Confluence Comment sang RawEventRecord v1."""
        comment_id = str(comment_item.get("id") or uuid.uuid4().hex)
        body_dict = comment_item.get("body", {})
        raw_storage = ""
        if isinstance(body_dict, dict):
            raw_storage = str(
                body_dict.get("storage", {}).get("value")
                or body_dict.get("view", {}).get("value")
                or ""
            )
        plain_text, _ = parse_confluence_storage(raw_storage)
        clean_text = redact_secrets(plain_text)

        version_dict = comment_item.get("version") or {}
        created_by = (
            comment_item.get("history", {}).get("createdBy")
            or version_dict.get("by")
            or {}
        )
        author_name = created_by.get("displayName") or "Confluence Commenter"
        author_id = created_by.get("accountId") or author_name

        version_when = (
            version_dict.get("when")
            or comment_item.get("history", {}).get("createdDate")
        )
        event_ts = datetime.now(timezone.utc)
        if version_when:
            try:
                event_ts = datetime.fromisoformat(str(version_when).replace("Z", "+00:00"))
            except Exception as exc:
                logger.debug("Failed to parse comment timestamp '%s': %s", version_when, exc)

        normalized_text = f"[Confluence Comment on Page #{page_id}] @{author_name}:\n\n{clean_text}".strip()

        deep_link = ""
        links = comment_item.get("_links", {})
        if isinstance(links, dict) and links.get("webui"):
            webui = links["webui"]
            deep_link = f"{self.base_url}{webui}" if self.base_url else webui

        safe_payload = dict(comment_item)

        return self.build_raw_event(
            external_id=f"comment-{comment_id}",
            content=normalized_text,
            author_id=author_id,
            stream_id=space_key,
            event_timestamp=event_ts,
            deep_link=deep_link,
            raw_payload=safe_payload,
            parent_external_id=page_id,
            author_name=author_name,
        )

    # -------------------------------------------------------------------------
    # Comments & CQL Query Support
    # -------------------------------------------------------------------------

    async def _fetch_page_comments(
        self, page_id: str, space_key: str
    ) -> List[Dict[str, Any]]:
        """Lấy danh sách comments của một Confluence page."""
        endpoint = f"wiki/rest/api/content/{page_id}/child/comment"
        params = {
            "expand": "body.storage,history,version",
            "limit": 50,
        }
        all_comments: List[Dict[str, Any]] = []
        start = 0

        while True:
            params["start"] = start
            try:
                data = await self.execute_with_retry(
                    self._execute_request,
                    endpoint=endpoint,
                    params=params,
                )
            except Exception as e:
                logger.debug(f"Không thể lấy comments cho page {page_id}: {e}")
                break

            results = data.get("results", []) if isinstance(data, dict) else []
            if not results:
                break
            all_comments.extend(results)
            size = data.get("size", len(results))
            start += size
            if start >= data.get("totalSize", start) or len(results) < 50:
                break

        return all_comments

    def _build_cql(self, stream_id: str, since: Optional[datetime] = None) -> str:
        """Xây dựng câu truy vấn CQL chuẩn xác theo stream_id (space key hoặc custom CQL)."""
        cql_clauses: List[str] = []

        if stream_id.startswith("cql:"):
            # Custom CQL expression
            cql_clauses.append(f"({stream_id[4:].strip()})")
        elif stream_id not in ("GLOBAL", "all", ""):
            cql_clauses.append(f'space = "{stream_id}"')
            cql_clauses.append("type in (page, blogpost)")
        else:
            cql_clauses.append("type in (page, blogpost)")

        if since is not None:
            since_utc = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
            formatted_date = since_utc.strftime("%Y-%m-%d %H:%M")
            cql_clauses.append(f'lastModified >= "{formatted_date}"')

        cql_query = " AND ".join(cql_clauses) if cql_clauses else "type = page"
        return f"{cql_query} ORDER BY lastModified ASC"

    async def _search_cql(self, cql: str) -> List[Dict[str, Any]]:
        """Tìm kiếm nội dung Confluence qua API `/wiki/rest/api/content/search` với CQL."""
        endpoint = "wiki/rest/api/content/search"
        params: Dict[str, Any] = {
            "cql": cql,
            "expand": "body.storage,history,version,ancestors",
            "limit": 50,
        }
        all_results: List[Dict[str, Any]] = []
        start = 0

        while True:
            params["start"] = start
            try:
                data = await self.execute_with_retry(
                    self._execute_request,
                    endpoint=endpoint,
                    params=params,
                )
            except Exception as e:
                logger.warning(f"Lỗi khi tìm kiếm CQL '{cql}': {e}")
                break

            results = data.get("results", []) if isinstance(data, dict) else []
            if not results:
                break

            all_results.extend(results)
            size = data.get("size", len(results))
            start += size
            total = data.get("totalSize", start)
            if start >= total or len(results) < 50:
                break

        return all_results

    # -------------------------------------------------------------------------
    # Lifecycle Methods: discover, backfill, poll_incremental, health
    # -------------------------------------------------------------------------

    async def discover(self) -> List[Dict[str, Any]]:
        """Liệt kê các Confluence Spaces có sẵn."""
        streams: List[Dict[str, Any]] = []

        # 1. Các space_keys đã cấu hình trước
        for k in self.space_keys:
            streams.append({
                "stream_id": k,
                "name": f"Space {k}",
                "source_type": SourceType.CONFLUENCE.value,
                "space_key": k,
            })

        # 2. Truy vấn API /wiki/rest/api/space nếu có cấu hình
        if self.base_url and (self.api_token or self._http_fetcher):
            try:
                data = await self.execute_with_retry(
                    self._execute_request,
                    endpoint="wiki/rest/api/space",
                    params={"limit": 50},
                )
                results = data.get("results", []) if isinstance(data, dict) else []
                for sp in results:
                    skey = sp.get("key")
                    sname = sp.get("name", skey)
                    if skey and not any(s["stream_id"] == skey for s in streams):
                        streams.append({
                            "stream_id": skey,
                            "name": sname,
                            "source_type": SourceType.CONFLUENCE.value,
                            "space_key": skey,
                            "type": sp.get("type"),
                        })
            except Exception as e:
                logger.warning(f"Không thể tự động discover Confluence spaces: {e}")

        if not streams:
            streams.append({
                "stream_id": "GLOBAL",
                "name": "Global Knowledge Base",
                "source_type": SourceType.CONFLUENCE.value,
            })

        return streams

    async def backfill(
        self, stream_id: str, since: Optional[datetime] = None
    ) -> AsyncIterator[RawEventRecord]:
        """Thu nạp tài liệu và comments trong stream_id (space key hoặc CQL) từ mốc since."""
        cql = self._build_cql(stream_id, since=since)
        logger.info("ConfluenceAdapter backfill stream '%s' với CQL: %s", stream_id, cql)

        pages = await self._search_cql(cql)
        space_key = stream_id if not stream_id.startswith("cql:") else "CQL"

        for page in pages:
            # Lấy space key thực tế từ page nếu có
            p_space = (
                page.get("space", {}).get("key")
                if isinstance(page.get("space"), dict)
                else space_key
            )
            page_event = self._page_to_raw_event(page, space_key=p_space)
            yield page_event

            if self.include_comments:
                comments = await self._fetch_page_comments(page_event.external_id, space_key=p_space)
                for comm in comments:
                    yield self._comment_to_raw_event(comm, page_id=page_event.external_id, space_key=p_space)

    async def poll_incremental(
        self, stream_id: str, checkpoint: Optional[IngestionCheckpointRecord] = None
    ) -> AsyncIterator[RawEventRecord]:
        """Thu nạp tài liệu và comments mới được cập nhật kể từ checkpoint."""
        since_dt = checkpoint.last_event_timestamp if checkpoint else None
        cql = self._build_cql(stream_id, since=since_dt)
        logger.info("ConfluenceAdapter incremental poll stream '%s' với CQL: %s", stream_id, cql)

        pages = await self._search_cql(cql)
        space_key = stream_id if not stream_id.startswith("cql:") else "CQL"

        for page in pages:
            p_space = (
                page.get("space", {}).get("key")
                if isinstance(page.get("space"), dict)
                else space_key
            )
            page_event = self._page_to_raw_event(page, space_key=p_space)

            should_emit = True
            if checkpoint and checkpoint.last_event_timestamp:
                if page_event.event_timestamp < checkpoint.last_event_timestamp:
                    should_emit = False
                elif page_event.event_timestamp == checkpoint.last_event_timestamp:
                    if checkpoint.last_external_id and page_event.external_id == checkpoint.last_external_id:
                        should_emit = False

            if should_emit:
                yield page_event

            if self.include_comments:
                comments = await self._fetch_page_comments(page_event.external_id, space_key=p_space)
                for comm in comments:
                    comm_event = self._comment_to_raw_event(comm, page_id=page_event.external_id, space_key=p_space)
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

    async def health(self) -> Dict[str, Any]:
        """Kiểm tra tình trạng cấu hình và xác thực của ConfluenceAdapter."""
        configured = bool(self.base_url and (self.api_token or self.email))
        report: Dict[str, Any] = {
            "source_type": self.source_type.value,
            "tenant_id": self.tenant_id,
            "configured": configured,
            "status": "unconfigured" if not configured else "healthy",
            "base_url": self.base_url,
            "spaces_monitored": len(self.space_keys),
            "auth_type": "basic" if self.email else ("bearer" if self.api_token else "none"),
        }

        if not configured:
            return report

        try:
            # Thử gọi ping /wiki/rest/api/user/current hoặc /wiki/rest/api/space?limit=1
            user_data = await self._execute_request("wiki/rest/api/user/current")
            report["status"] = "healthy"
            report["user"] = (
                user_data.get("displayName")
                or user_data.get("publicName")
                or user_data.get("username")
            )
        except AdapterAuthenticationError as a_err:
            report["status"] = "auth_required"
            report["error"] = str(a_err)
        except Exception as err:
            report["status"] = "degraded"
            report["error"] = str(err)

        return report
