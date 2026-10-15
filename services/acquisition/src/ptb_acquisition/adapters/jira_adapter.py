"""Jira Cloud / Server Acquisition Adapter.

Truy xuất và nạp issues, comments, transitions từ Jira REST API (Cloud v3 / Server v2)
theo JQL hoặc Project Key, chuyển đổi thành RawEventRecord v1 chuẩn hóa với source_type = SourceType.JIRA.
"""

import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import re
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Union
import urllib.error
import urllib.request
import uuid

from ptb_contracts.l1_acquisition import (
    AdapterHealthStatus,
    IngestionCheckpointRecord,
    ProcessingStatus,
    RateLimitConfig,
    RawEventRecord,
    RetryPolicyConfig,
    SourceSyncState,
    SourceType,
    compute_checkpoint_id,
    compute_content_hash,
    compute_idempotency_key,
    ensure_utc,
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

logger = logging.getLogger("ptb.acquisition.adapters.jira")


def _extract_jira_text(description_field: Any) -> str:
    """Trích xuất text từ Jira description hoặc comment body.

    Hỗ trợ cả plain string (Jira Server / API v2) và 
    Atlassian Document Format (ADF dict/JSON ở Jira Cloud API v3).
    """
    if not description_field:
        return ""
    if isinstance(description_field, str):
        return description_field.strip()
    if not isinstance(description_field, (dict, list)):
        return str(description_field).strip()

    def _render_node(node: Any) -> str:
        if not node:
            return ""
        if isinstance(node, str):
            return node
        if isinstance(node, list):
            return "".join(_render_node(child) for child in node)
        if not isinstance(node, dict):
            return str(node)

        node_type = node.get("type", "")
        content = node.get("content", [])
        attrs = node.get("attrs", {})

        if node_type == "text":
            text = node.get("text", "")
            marks = node.get("marks", [])
            for mark in marks:
                if not isinstance(mark, dict):
                    continue
                m_type = mark.get("type")
                if m_type == "link":
                    href = mark.get("attrs", {}).get("href")
                    if href and href != text:
                        text = f"[{text}]({href})"
                elif m_type == "code":
                    text = f"`{text}`"
            return text

        elif node_type == "paragraph":
            inner = "".join(_render_node(c) for c in content).strip()
            return f"{inner}\n\n" if inner else ""

        elif node_type == "heading":
            level = attrs.get("level", 1)
            inner = "".join(_render_node(c) for c in content).strip()
            prefix = "#" * max(1, min(int(level), 6))
            return f"{prefix} {inner}\n\n" if inner else ""

        elif node_type == "bulletList":
            items = []
            for item in content:
                item_text = "".join(_render_node(c) for c in item.get("content", [])).strip()
                if item_text:
                    items.append(f"- {item_text}")
            return "\n".join(items) + "\n\n" if items else ""

        elif node_type == "orderedList":
            items = []
            order = attrs.get("order", 1)
            for i, item in enumerate(content):
                item_text = "".join(_render_node(c) for c in item.get("content", [])).strip()
                if item_text:
                    items.append(f"{order + i}. {item_text}")
            return "\n".join(items) + "\n\n" if items else ""

        elif node_type == "codeBlock":
            lang = attrs.get("language", "")
            inner = "".join(_render_node(c) for c in content)
            return f"```{lang}\n{inner}\n```\n\n"

        elif node_type == "blockquote":
            inner = "".join(_render_node(c) for c in content).strip()
            return f"> {inner}\n\n" if inner else ""

        elif node_type == "hardBreak":
            return "\n"

        elif node_type == "rule":
            return "---\n\n"

        elif node_type == "mention":
            mention_text = attrs.get("text") or attrs.get("id") or "user"
            if not mention_text.startswith("@"):
                mention_text = f"@{mention_text}"
            return mention_text

        elif node_type == "emoji":
            return attrs.get("shortName") or attrs.get("text") or ""

        elif node_type in ("inlineCard", "blockCard"):
            url = attrs.get("url", "")
            return f" {url} " if url else ""

        elif node_type == "table":
            rows = []
            for row in content:
                cells = []
                for cell in row.get("content", []):
                    cell_text = "".join(_render_node(c) for c in cell.get("content", [])).strip().replace("\n", " ")
                    cells.append(cell_text)
                if cells:
                    rows.append("| " + " | ".join(cells) + " |")
            return "\n".join(rows) + "\n\n" if rows else ""

        elif node_type == "doc":
            return "".join(_render_node(c) for c in content)

        else:
            if content:
                return "".join(_render_node(c) for c in content)
            if "text" in node:
                return node["text"]
            return ""

    rendered = _render_node(description_field)
    cleaned = re.sub(r"\n{3,}", "\n\n", rendered).strip()
    return cleaned


def _parse_jira_timestamp(ts_str: Optional[str]) -> datetime:
    """Parse chuỗi timestamp từ Jira ISO-8601 sang datetime UTC."""
    if not ts_str:
        return datetime.now(timezone.utc)
    try:
        cleaned = str(ts_str).replace("Z", "+00:00")
        if len(cleaned) >= 28 and (cleaned[-5] in ("+", "-")) and ":" not in cleaned[-5:]:
            cleaned = cleaned[:-2] + ":" + cleaned[-2:]
        parsed = datetime.fromisoformat(cleaned)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


class JiraAdapter(AcquisitionAdapter):
    """Adapter tích hợp Jira REST API (Cloud & Server/Data Center)."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
        jql: Optional[str] = None,
        project_keys: Optional[List[str]] = None,
        tenant_id: str = "local-user",
        http_fetcher: Optional[Callable[[str, Dict[str, str], Optional[bytes]], Any]] = None,
        api_version: Optional[str] = None,
        include_comments: bool = True,
        include_changelog: bool = True,
        retry_policy: Optional[RetryPolicyConfig] = None,
        rate_limit_config: Optional[RateLimitConfig] = None,
        default_jql: Optional[str] = None,
    ):
        super().__init__(
            source_type=SourceType.JIRA,
            tenant_id=tenant_id,
            retry_policy=retry_policy,
            rate_limit_config=rate_limit_config,
        )
        self.base_url = (base_url or os.getenv("JIRA_BASE_URL", "")).rstrip("/")
        self.email = email or os.getenv("JIRA_EMAIL", "")
        self.api_token = api_token or os.getenv("JIRA_API_TOKEN", "") or os.getenv("JIRA_PAT", "")
        self.default_jql = default_jql or jql or os.getenv("JIRA_JQL", "")

        # Project keys list or comma-separated string from env
        if project_keys is not None:
            self.project_keys = list(project_keys)
        else:
            env_keys = os.getenv("JIRA_PROJECT_KEYS", "")
            self.project_keys = [k.strip() for k in env_keys.split(",") if k.strip()] if env_keys else []

        self._http_fetcher = http_fetcher
        self.api_version = api_version or os.getenv("JIRA_API_VERSION", "3")
        self.include_comments = include_comments
        self.include_changelog = include_changelog

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
            # Personal Access Token (PAT) cho Jira Server/Data Center
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    async def _handle_http_error(
        self, code: int, headers: Any, reason: str, url: str = ""
    ) -> None:
        """Xử lý phân loại mã lỗi HTTP chuẩn hóa."""
        if code in (401, 403):
            raise AdapterAuthenticationError(
                f"Jira authentication failure (HTTP {code}): {reason}",
                source_type=self.source_type,
                details={"status_code": code, "url": url},
            )
        elif code == 429:
            retry_after: float = 5.0
            if headers:
                h_val = headers.get("Retry-After") if hasattr(headers, "get") else None
                if h_val:
                    try:
                        retry_after = float(h_val)
                    except (ValueError, TypeError):
                        pass
            await self.handle_rate_limit(retry_after)
            raise AdapterRateLimitError(
                f"Jira rate limit reached (HTTP 429): {reason}",
                retry_after_seconds=retry_after,
                source_type=self.source_type,
                details={"status_code": 429, "url": url},
            )
        elif code == 404:
            raise AdapterStreamNotFoundError(
                f"Jira resource not found (HTTP 404): {reason}",
                source_type=self.source_type,
                details={"status_code": 404, "url": url},
            )
        elif code >= 500:
            raise AdapterTransientError(
                f"Jira server transient error (HTTP {code}): {reason}",
                source_type=self.source_type,
                details={"status_code": code, "url": url},
            )
        else:
            raise AcquisitionError(
                f"Jira API error (HTTP {code}): {reason}",
                source_type=self.source_type,
                details={"status_code": code, "url": url},
            )

    async def _execute_request(
        self, endpoint: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None
    ) -> Any:
        """Thực thi HTTP request tới Jira API với Exponential Backoff & Rate Limit handling."""
        if self._http_fetcher:
            url = f"{self.base_url}{endpoint}" if (self.base_url and not endpoint.startswith("http")) else endpoint
            headers = self._get_headers()
            body_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None

            async def _call_fetcher():
                try:
                    res = self._http_fetcher(url, headers, body_bytes)
                    if asyncio.iscoroutine(res):
                        res = await res
                except urllib.error.HTTPError as err:
                    await self._handle_http_error(err.code, err.headers, err.reason, url=url)
                    raise
                except (ConnectionError, TimeoutError, OSError) as net_err:
                    raise AdapterTransientError(
                        f"Network failure calling Jira: {net_err}",
                        source_type=self.source_type,
                    ) from net_err

                if hasattr(res, "status_code"):
                    code = res.status_code
                    res_headers = getattr(res, "headers", {})
                    reason = getattr(res, "reason", f"HTTP {code}")
                    if code >= 400:
                        await self._handle_http_error(code, res_headers, reason, url=url)
                    if hasattr(res, "json"):
                        return res.json()
                    if hasattr(res, "text"):
                        return json.loads(res.text) if res.text else {}
                return res

            return await self.execute_with_retry(
                _call_fetcher,
                retryable_exceptions=(AdapterTransientError, AdapterRateLimitError, ConnectionError, TimeoutError),
            )

        if not self.base_url:
            raise AdapterConfigurationError(
                "Jira base_url chưa được thiết lập (JIRA_BASE_URL)",
                source_type=self.source_type,
            )

        url = f"{self.base_url}{endpoint}" if not endpoint.startswith("http") else endpoint
        headers = self._get_headers()
        body_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None

        def _sync_call():
            req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = resp.read().decode("utf-8")
                    return json.loads(data) if data else {}
            except urllib.error.HTTPError as err:
                return ("http_error", err.code, err.headers, err.reason)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as net_err:
                return ("net_error", str(net_err))

        async def _call():
            result = await asyncio.to_thread(_sync_call)
            if isinstance(result, tuple):
                if result[0] == "http_error":
                    await self._handle_http_error(result[1], result[2], result[3], url=url)
                elif result[0] == "net_error":
                    raise AdapterTransientError(
                        f"Network connection error calling Jira: {result[1]}",
                        source_type=self.source_type,
                    )
            return result

        return await self.execute_with_retry(
            _call,
            retryable_exceptions=(AdapterTransientError, AdapterRateLimitError, ConnectionError, TimeoutError),
        )

    async def _execute_jira_api(
        self, path: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None
    ) -> Any:
        """Gọi Jira API theo phiên bản hiện tại, tự động fallback v3 -> v2 nếu gặp 404."""
        endpoint = f"/rest/api/{self.api_version}/{path.lstrip('/')}"
        try:
            return await self._execute_request(endpoint, method=method, payload=payload)
        except AdapterStreamNotFoundError:
            if self.api_version == "3":
                logger.info("Jira API v3 endpoint %s không khả dụng (404), fallback sang v2", endpoint)
                self.api_version = "2"
                endpoint_v2 = f"/rest/api/2/{path.lstrip('/')}"
                return await self._execute_request(endpoint_v2, method=method, payload=payload)
            raise

    def issue_to_raw_event(self, issue: Dict[str, Any]) -> RawEventRecord:
        """Chuyển đổi Jira issue JSON thành RawEventRecord v1 chuẩn hóa."""
        fields = issue.get("fields", {})
        key = issue.get("key") or str(issue.get("id", "UNKNOWN"))
        summary = str(fields.get("summary", "")).strip()
        raw_desc = fields.get("description")
        desc_text = _extract_jira_text(raw_desc)

        status_info = fields.get("status") or {}
        status_name = status_info.get("name", "Unknown") if isinstance(status_info, dict) else str(status_info)
        status_category_info = status_info.get("statusCategory") if isinstance(status_info, dict) else {}
        status_category = status_category_info.get("name") if isinstance(status_category_info, dict) else None

        assignee_info = fields.get("assignee") or {}
        assignee_name = (
            assignee_info.get("displayName") or assignee_info.get("name")
            if isinstance(assignee_info, dict)
            else None
        )

        creator_info = fields.get("creator") or fields.get("reporter") or {}
        if isinstance(creator_info, dict):
            creator_id = (
                creator_info.get("accountId")
                or creator_info.get("name")
                or creator_info.get("emailAddress")
                or "jira-user"
            )
            creator_name = creator_info.get("displayName") or creator_info.get("name")
        else:
            creator_id = str(creator_info) if creator_info else "jira-user"
            creator_name = str(creator_info) if creator_info else None

        project_info = fields.get("project") or {}
        project_key = project_info.get("key") if isinstance(project_info, dict) else str(project_info)
        if not project_key or project_key == "{}":
            project_key = key.split("-")[0] if "-" in key else "JIRA"

        parent_info = fields.get("parent") or {}
        parent_key = (
            parent_info.get("key")
            if isinstance(parent_info, dict)
            else (str(parent_info) if parent_info else None)
        )

        priority_info = fields.get("priority") or {}
        priority_name = (
            priority_info.get("name")
            if isinstance(priority_info, dict)
            else (str(priority_info) if priority_info else None)
        )

        resolution_info = fields.get("resolution") or {}
        resolution_name = (
            resolution_info.get("name")
            if isinstance(resolution_info, dict)
            else (str(resolution_info) if resolution_info else None)
        )

        issuetype_info = fields.get("issuetype") or {}
        issuetype_name = (
            issuetype_info.get("name")
            if isinstance(issuetype_info, dict)
            else (str(issuetype_info) if issuetype_info else None)
        )

        labels = fields.get("labels", [])

        # Extract transitions history from changelog if present
        transitions: List[str] = []
        changelog = issue.get("changelog") or {}
        histories = changelog.get("histories", []) if isinstance(changelog, dict) else []
        for h in histories:
            h_created = h.get("created")
            h_author = h.get("author", {}).get("displayName") or h.get("author", {}).get("name")
            for item in h.get("items", []):
                if item.get("field") == "status":
                    from_str = item.get("fromString", "")
                    to_str = item.get("toString", "")
                    transitions.append(f"[{h_created}] {from_str} -> {to_str} by {h_author}")

        # Build normalized text preserving backward-compatible prefix
        assignee_str = f"Assignee: {assignee_name}" if assignee_name else "Unassigned"
        meta_items = [f"Status: {status_name}"]
        if status_category:
            meta_items.append(f"Category: {status_category}")
        if priority_name:
            meta_items.append(f"Priority: {priority_name}")
        if issuetype_name:
            meta_items.append(f"Type: {issuetype_name}")
        meta_items.append(assignee_str)
        if resolution_name:
            meta_items.append(f"Resolution: {resolution_name}")
        if labels:
            meta_items.append(f"Labels: {', '.join(labels)}")

        meta_line = " | ".join(meta_items)
        blocks = [f"[{key}] {summary}", meta_line]
        if desc_text:
            blocks.append(f"\n{desc_text}")
        if transitions:
            blocks.append("\nRecent Transitions:\n" + "\n".join(transitions[-5:]))
        normalized_text = "\n".join(blocks).strip()

        event_timestamp = _parse_jira_timestamp(fields.get("updated") or fields.get("created"))
        deep_link = f"{self.base_url}/browse/{key}" if self.base_url else None

        return self.build_raw_event(
            external_id=key,
            content=normalized_text,
            author_id=creator_id,
            stream_id=project_key,
            event_timestamp=event_timestamp,
            deep_link=deep_link,
            raw_payload=issue,
            parent_external_id=parent_key,
            author_name=creator_name,
        )

    def comment_to_raw_event(
        self, issue_key: str, project_key: str, comment: Dict[str, Any]
    ) -> RawEventRecord:
        """Chuyển đổi Jira comment thành RawEventRecord v1 dạng child record."""
        comment_id = str(comment.get("id") or uuid.uuid4().hex[:8])
        author_info = comment.get("author") or comment.get("updateAuthor") or {}
        if isinstance(author_info, dict):
            author_id = (
                author_info.get("accountId")
                or author_info.get("name")
                or author_info.get("emailAddress")
                or "jira-user"
            )
            author_name = author_info.get("displayName") or author_info.get("name")
        else:
            author_id = str(author_info) if author_info else "jira-user"
            author_name = str(author_info) if author_info else None

        body_raw = comment.get("body")
        body_text = _extract_jira_text(body_raw)
        normalized_text = f"[{issue_key} Comment by {author_name or author_id}]\n\n{body_text}".strip()

        event_timestamp = _parse_jira_timestamp(
            comment.get("updated") or comment.get("created")
        )
        deep_link = (
            f"{self.base_url}/browse/{issue_key}?focusedCommentId={comment_id}"
            if self.base_url
            else None
        )
        external_id = f"{issue_key}-comment-{comment_id}"

        return self.build_raw_event(
            external_id=external_id,
            content=normalized_text,
            author_id=author_id,
            stream_id=project_key,
            event_timestamp=event_timestamp,
            deep_link=deep_link,
            raw_payload=comment,
            parent_external_id=issue_key,
            author_name=author_name,
        )

    async def discover(self) -> List[Dict[str, Any]]:
        """Khám phá các streams / projects có sẵn trong Jira."""
        streams: List[Dict[str, Any]] = [
            {
                "stream_id": "all",
                "name": "All Jira Issues",
                "source_type": SourceType.JIRA.value,
                "description": "All issues matching default JQL or project filter",
            }
        ]

        # 1. Thêm các project_keys đã cấu hình trước
        for pkey in self.project_keys:
            streams.append({
                "stream_id": pkey,
                "name": f"Project {pkey}",
                "source_type": SourceType.JIRA.value,
                "project_key": pkey,
            })

        # 2. Thử truy vấn API /project nếu có cấu hình
        if self.base_url and (self.api_token or self._http_fetcher):
            try:
                data = await self._execute_jira_api("project")
                if isinstance(data, list):
                    for proj in data:
                        pkey = proj.get("key")
                        if pkey and not any(s["stream_id"] == pkey for s in streams):
                            streams.append({
                                "stream_id": pkey,
                                "name": proj.get("name", pkey),
                                "source_type": SourceType.JIRA.value,
                                "project_key": pkey,
                                "id": proj.get("id"),
                            })
            except Exception as e:
                logger.warning("Không thể tự động discover Jira projects qua API: %s", e)

        return streams

    def _build_jql(self, stream_id: str, since: Optional[datetime] = None) -> str:
        """Xây dựng câu truy vấn JQL chuẩn xác theo stream_id và thời gian."""
        jql_parts: List[str] = []

        if stream_id not in ("all", ""):
            # Check if stream_id is already custom JQL syntax
            if stream_id.lower().startswith("jql:") or "=" in stream_id or " in " in stream_id:
                raw_jql = stream_id[4:].strip() if stream_id.lower().startswith("jql:") else stream_id
                jql_parts.append(f"({raw_jql})")
            else:
                jql_parts.append(f"project = '{stream_id}'")
        elif self.default_jql:
            jql_parts.append(f"({self.default_jql})")
        elif self.project_keys:
            joined = ", ".join(f"'{k}'" for k in self.project_keys)
            jql_parts.append(f"project in ({joined})")

        if since is not None:
            since_utc = ensure_utc(since) or datetime.now(timezone.utc)
            formatted_date = since_utc.strftime("%Y-%m-%d %H:%M")
            jql_parts.append(f"updated >= '{formatted_date}'")

        if not jql_parts:
            return "ORDER BY updated ASC"

        # Check if already ends with an ORDER BY clause
        base_query = " AND ".join(jql_parts)
        if "order by" not in base_query.lower():
            base_query += " ORDER BY updated ASC"
        return base_query

    async def _fetch_issues(self, jql: str) -> List[Dict[str, Any]]:
        """Truy vấn issues với JQL, hỗ trợ phân trang Jira REST API."""
        all_issues: List[Dict[str, Any]] = []
        start_at = 0
        max_results = 50

        fields = [
            "summary",
            "description",
            "status",
            "statusCategory",
            "assignee",
            "creator",
            "reporter",
            "project",
            "parent",
            "created",
            "updated",
            "priority",
            "resolution",
            "labels",
            "issuetype",
        ]
        if self.include_comments:
            fields.append("comment")

        while True:
            payload: Dict[str, Any] = {
                "jql": jql,
                "startAt": start_at,
                "maxResults": max_results,
                "fields": fields,
            }
            if self.include_changelog:
                payload["expand"] = ["changelog"]

            result = await self._execute_jira_api("search", method="POST", payload=payload)
            issues = result.get("issues", []) if isinstance(result, dict) else []
            if not issues:
                break

            all_issues.extend(issues)
            total = result.get("total", len(all_issues))
            start_at += len(issues)

            if start_at >= total or len(issues) < max_results:
                break

        return all_issues

    async def _stream_issues_and_comments(
        self,
        issues: List[Dict[str, Any]],
        since: Optional[datetime] = None,
        checkpoint: Optional[IngestionCheckpointRecord] = None,
    ) -> AsyncIterator[RawEventRecord]:
        """Tạo luồng emit issue records và comment records kèm điều kiện lọc checkpoint."""
        for issue in issues:
            issue_rec = self.issue_to_raw_event(issue)
            project_key = issue_rec.conversation_or_project_id

            # Filter issue itself if incremental checkpoint specified
            should_emit_issue = True
            if checkpoint and checkpoint.last_event_timestamp:
                if issue_rec.event_timestamp < checkpoint.last_event_timestamp:
                    should_emit_issue = False
                elif issue_rec.event_timestamp == checkpoint.last_event_timestamp:
                    if checkpoint.last_external_id and issue_rec.external_id == checkpoint.last_external_id:
                        should_emit_issue = False

            if should_emit_issue:
                yield issue_rec

            # Handle comments if present and enabled
            if self.include_comments:
                comment_data = issue.get("fields", {}).get("comment") or {}
                comments = comment_data.get("comments", []) if isinstance(comment_data, dict) else []
                for c in comments:
                    comm_rec = self.comment_to_raw_event(issue_rec.external_id, project_key, c)
                    should_emit_comment = True
                    if since is not None and comm_rec.event_timestamp < since:
                        should_emit_comment = False
                    elif checkpoint and checkpoint.last_event_timestamp:
                        if comm_rec.event_timestamp < checkpoint.last_event_timestamp:
                            should_emit_comment = False
                        elif comm_rec.event_timestamp == checkpoint.last_event_timestamp:
                            if checkpoint.last_external_id and comm_rec.external_id == checkpoint.last_external_id:
                                should_emit_comment = False
                    if should_emit_comment:
                        yield comm_rec

    async def backfill(
        self, stream_id: str, since: Optional[datetime] = None
    ) -> AsyncIterator[RawEventRecord]:
        """Thu thập toàn bộ issues từ stream_id (dự án hoặc JQL) từ mốc thời gian since."""
        jql = self._build_jql(stream_id, since=since)
        logger.info("JiraAdapter backfill stream '%s' với JQL: %s", stream_id, jql)

        issues = await self._fetch_issues(jql)
        async for record in self._stream_issues_and_comments(issues, since=since):
            yield record

    async def poll_incremental(
        self, stream_id: str, checkpoint: Optional[IngestionCheckpointRecord] = None
    ) -> AsyncIterator[RawEventRecord]:
        """Thu thập các issues mới cập nhật kể từ checkpoint."""
        since_dt = checkpoint.last_event_timestamp if checkpoint else None
        jql = self._build_jql(stream_id, since=since_dt)
        logger.info("JiraAdapter incremental poll stream '%s' với JQL: %s", stream_id, jql)

        issues = await self._fetch_issues(jql)
        async for record in self._stream_issues_and_comments(
            issues, since=since_dt, checkpoint=checkpoint
        ):
            yield record

    async def health(self) -> Dict[str, Any]:
        """Kiểm tra tình trạng kết nối và cấu hình của JiraAdapter."""
        has_auth = bool(self.api_token or (self.email and self.api_token))
        is_configured = bool(self.base_url and has_auth)

        health_data = {
            "source_type": SourceType.JIRA.value,
            "tenant_id": self.tenant_id,
            "configured": is_configured,
            "base_url": self.base_url or "(not set)",
            "auth_type": "basic" if self.email else ("bearer" if self.api_token else "none"),
            "project_keys": self.project_keys,
            "default_jql": self.default_jql or "(none)",
            "status": AdapterHealthStatus.NOT_CONFIGURED.value,
        }

        if not is_configured and not self._http_fetcher:
            health_data["message"] = "Thiếu JIRA_BASE_URL hoặc JIRA_API_TOKEN"
            return health_data

        try:
            res = await self._execute_jira_api("myself")
            health_data["status"] = AdapterHealthStatus.HEALTHY.value
            health_data["user"] = (
                res.get("displayName") or res.get("name") if isinstance(res, dict) else None
            )
        except AdapterAuthenticationError as auth_err:
            health_data["status"] = AdapterHealthStatus.AUTH_REQUIRED.value
            health_data["error"] = str(auth_err)
        except Exception as e:
            health_data["status"] = AdapterHealthStatus.DEGRADED.value
            health_data["error"] = str(e)

        return health_data
