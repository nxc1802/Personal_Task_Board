"""Shortcut (formerly Clubhouse) REST API Acquisition Adapter.

Truy xuất và nạp stories, comments, owners, workflow states từ Shortcut REST API v3
theo Search Query, Project hoặc Epic, chuyển đổi thành RawEventRecord v1 chuẩn hóa
với source_type = SourceType.SHORTCUT.
"""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import re
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Set, Union
import urllib.error
import urllib.parse
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

logger = logging.getLogger("ptb.acquisition.adapters.shortcut")


def _parse_shortcut_timestamp(ts_str: Optional[str]) -> datetime:
    """Parse chuỗi timestamp ISO-8601 từ Shortcut sang datetime UTC."""
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


class ShortcutAdapter(AcquisitionAdapter):
    """Adapter tích hợp Shortcut REST API v3 theo chuẩn AcquisitionAdapter."""

    DEFAULT_BASE_URL = "https://api.app.shortcut.com/api/v3"

    def __init__(
        self,
        api_token: Optional[str] = None,
        base_url: Optional[str] = None,
        project_ids: Optional[List[Union[int, str]]] = None,
        query: Optional[str] = None,
        tenant_id: str = "local-user",
        http_fetcher: Optional[Callable[[str, Dict[str, str], Optional[bytes]], Any]] = None,
        include_comments: bool = True,
        include_archived: bool = False,
        retry_policy: Optional[RetryPolicyConfig] = None,
        rate_limit_config: Optional[RateLimitConfig] = None,
        default_query: Optional[str] = None,
    ):
        super().__init__(
            source_type=SourceType.SHORTCUT,
            tenant_id=tenant_id,
            retry_policy=retry_policy,
            rate_limit_config=rate_limit_config,
        )
        self.api_token = api_token or os.getenv("SHORTCUT_API_TOKEN", "")
        self.base_url = (base_url or os.getenv("SHORTCUT_BASE_URL", self.DEFAULT_BASE_URL)).rstrip("/")

        if project_ids is not None:
            self.project_ids = list(project_ids)
        else:
            env_pids = os.getenv("SHORTCUT_PROJECT_IDS", "")
            self.project_ids = [p.strip() for p in env_pids.split(",") if p.strip()] if env_pids else []

        self.default_query = default_query or query or os.getenv("SHORTCUT_QUERY", "")
        self._http_fetcher = http_fetcher
        self.include_comments = include_comments
        self.include_archived = (
            include_archived
            or os.getenv("SHORTCUT_INCLUDE_ARCHIVED", "").strip().lower() in ("true", "1")
        )
        self._workflow_states: Dict[str, str] = {}

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.api_token:
            headers["Shortcut-Token"] = self.api_token
        return headers

    async def _handle_http_error(
        self, code: int, headers: Any, reason: str, url: str = ""
    ) -> None:
        """Xử lý phân loại mã lỗi HTTP chuẩn hóa."""
        if code in (401, 403):
            raise AdapterAuthenticationError(
                f"Shortcut authentication failure (HTTP {code}): {reason}",
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
                f"Shortcut rate limit reached (HTTP 429): {reason}",
                retry_after_seconds=retry_after,
                source_type=self.source_type,
                details={"status_code": 429, "url": url},
            )
        elif code == 404:
            raise AdapterStreamNotFoundError(
                f"Shortcut resource not found (HTTP 404): {reason}",
                source_type=self.source_type,
                details={"status_code": 404, "url": url},
            )
        elif code >= 500:
            raise AdapterTransientError(
                f"Shortcut server transient error (HTTP {code}): {reason}",
                source_type=self.source_type,
                details={"status_code": code, "url": url},
            )
        else:
            raise AcquisitionError(
                f"Shortcut API error (HTTP {code}): {reason}",
                source_type=self.source_type,
                details={"status_code": code, "url": url},
            )

    async def _execute_request(
        self, endpoint: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None
    ) -> Any:
        """Thực thi HTTP request tới Shortcut API với Exponential Backoff & Rate Limit handling."""
        if self._http_fetcher:
            url = f"{self.base_url}{endpoint}" if not endpoint.startswith("http") else endpoint
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
                        f"Network failure calling Shortcut: {net_err}",
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

        if not self.api_token:
            raise AdapterConfigurationError(
                "Shortcut api_token chưa được thiết lập (SHORTCUT_API_TOKEN)",
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
                        f"Network connection error calling Shortcut: {result[1]}",
                        source_type=self.source_type,
                    )
            return result

        return await self.execute_with_retry(
            _call,
            retryable_exceptions=(AdapterTransientError, AdapterRateLimitError, ConnectionError, TimeoutError),
        )

    def story_to_raw_event(self, story: Dict[str, Any]) -> RawEventRecord:
        """Chuyển đổi Shortcut Story JSON thành RawEventRecord v1 chuẩn hóa."""
        story_id = str(story.get("id", "UNKNOWN"))
        name = str(story.get("name", "")).strip()
        desc = str(story.get("description", "") or "").strip()
        story_type = story.get("story_type", "feature")

        project_id = str(story.get("project_id", "") or story.get("group_id", "") or "shortcut")
        epic_id = str(story.get("epic_id")) if story.get("epic_id") else None

        workflow_state_id = story.get("workflow_state_id", "")
        workflow_name = self._workflow_states.get(str(workflow_state_id))
        state_label = workflow_name or str(workflow_state_id)

        owner_ids = story.get("owner_ids", [])
        is_archived = bool(story.get("archived", False))

        # Build normalized text
        state_info = f"State: {state_label}"
        archived_tag = " [ARCHIVED]" if is_archived else ""
        owners_info = f" | Owners: {', '.join(str(o) for o in owner_ids)}" if owner_ids else ""
        header = f"[{story_id}] ({story_type}) {name}{archived_tag}"
        meta = f"{state_info}{owners_info}"

        if desc:
            normalized_text = f"{header}\n{meta}\n\n{desc}".strip()
        else:
            normalized_text = f"{header}\n{meta}".strip()

        event_timestamp = _parse_shortcut_timestamp(
            story.get("updated_at") or story.get("created_at")
        )
        creator_id = str(story.get("requested_by_id") or "shortcut-user")
        deep_link = story.get("app_url")

        return self.build_raw_event(
            external_id=story_id,
            content=normalized_text,
            author_id=creator_id,
            stream_id=project_id,
            event_timestamp=event_timestamp,
            deep_link=deep_link,
            raw_payload=story,
            parent_external_id=epic_id,
            author_name=None,
        )

    def comment_to_raw_event(
        self, story_id: str, project_id: str, app_url: Optional[str], comment: Dict[str, Any]
    ) -> RawEventRecord:
        """Chuyển đổi Shortcut Story comment thành RawEventRecord v1 dạng child record."""
        comment_id = str(comment.get("id") or uuid.uuid4().hex[:8])
        author_id = str(comment.get("author_id") or "shortcut-user")
        text = str(comment.get("text") or "").strip()
        normalized_text = f"[{story_id} Comment by {author_id}]\n\n{text}".strip()

        event_timestamp = _parse_shortcut_timestamp(
            comment.get("updated_at") or comment.get("created_at")
        )
        deep_link = f"{app_url}#comment-{comment_id}" if app_url else None
        external_id = f"{story_id}-comment-{comment_id}"

        return self.build_raw_event(
            external_id=external_id,
            content=normalized_text,
            author_id=author_id,
            stream_id=project_id,
            event_timestamp=event_timestamp,
            deep_link=deep_link,
            raw_payload=comment,
            parent_external_id=story_id,
            author_name=author_id,
        )

    async def _load_workflow_states(self) -> None:
        """Nạp danh mục workflow states từ Shortcut API để hiển thị tên trạng thái rõ ràng."""
        if self._workflow_states:
            return
        if not (self.api_token or self._http_fetcher):
            return
        try:
            workflows = await self._execute_request("/workflows")
            if isinstance(workflows, list):
                for wf in workflows:
                    for state in wf.get("states", []):
                        s_id = str(state.get("id"))
                        s_name = state.get("name")
                        if s_id and s_name:
                            self._workflow_states[s_id] = s_name
        except Exception as e:
            logger.debug("Không thể tải workflow states từ Shortcut API: %s", e)

    async def discover(self) -> List[Dict[str, Any]]:
        """Khám phá các streams / projects có sẵn trong Shortcut."""
        streams: List[Dict[str, Any]] = [
            {
                "stream_id": "all",
                "name": "All Shortcut Stories",
                "source_type": SourceType.SHORTCUT.value,
                "description": "All stories from search or configured project filters",
            }
        ]

        # 1. Thêm project_ids đã cấu hình thủ công
        for pid in self.project_ids:
            streams.append({
                "stream_id": str(pid),
                "name": f"Project {pid}",
                "source_type": SourceType.SHORTCUT.value,
                "project_id": str(pid),
            })

        # 2. Truy vấn API /projects nếu có API token
        if self.api_token or self._http_fetcher:
            try:
                projects = await self._execute_request("/projects")
                if isinstance(projects, list):
                    for p in projects:
                        pid = str(p.get("id"))
                        if not any(s["stream_id"] == pid for s in streams):
                            streams.append({
                                "stream_id": pid,
                                "name": p.get("name", f"Project {pid}"),
                                "source_type": SourceType.SHORTCUT.value,
                                "project_id": pid,
                                "app_url": p.get("app_url"),
                            })
            except Exception as e:
                logger.warning("Không thể tự động discover Shortcut projects: %s", e)

        return streams

    def _build_search_query(self, stream_id: str, since: Optional[datetime] = None) -> str:
        """Tạo search query cho Shortcut Search API."""
        query_parts: List[str] = []

        if stream_id not in ("all", ""):
            if ":" in stream_id or " " in stream_id:
                query_parts.append(stream_id)
            else:
                query_parts.append(f"project:{stream_id}")
        elif self.default_query:
            query_parts.append(self.default_query)
        elif self.project_ids:
            joined = " OR ".join(f"project:{pid}" for pid in self.project_ids)
            query_parts.append(f"({joined})")

        if since is not None:
            since_utc = ensure_utc(since) or datetime.now(timezone.utc)
            query_parts.append(f"updated:>={since_utc.strftime('%Y-%m-%d')}")

        # Archived handling: default excludes archived unless include_archived is True or query specifies
        all_query_str = " ".join(query_parts)
        if not self.include_archived and "archived" not in all_query_str:
            query_parts.append("!is:archived")

        return " ".join(query_parts) if query_parts else "!is:archived"

    async def _fetch_stories(
        self, stream_id: str, since: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """Truy vấn stories từ Shortcut API với cursor-based pagination."""
        # Nếu stream_id là project cụ thể, số nguyên thuần túy và không có mốc since
        if stream_id not in ("all", "") and stream_id.isdigit() and not since:
            try:
                res = await self._execute_request(f"/projects/{stream_id}/stories")
                if isinstance(res, list):
                    return res
            except Exception as e:
                logger.debug("Không thể lấy theo /projects/%s/stories, fallback search: %s", stream_id, e)

        query = self._build_search_query(stream_id, since=since)
        all_stories: List[Dict[str, Any]] = []
        next_cursor: Optional[str] = None
        seen_cursors: Set[str] = set()

        while True:
            payload: Dict[str, Any] = {
                "query": query,
                "page_size": 25,
            }
            if next_cursor:
                # Handle next_cursor as query parameter string, full URL or raw token
                if "next=" in next_cursor:
                    parsed = urllib.parse.urlparse(next_cursor)
                    qs = urllib.parse.parse_qs(parsed.query)
                    cursor_token = qs.get("next", [next_cursor])[0]
                    payload["next"] = cursor_token
                else:
                    payload["next"] = next_cursor

            result = await self._execute_request("/stories/search", method="POST", payload=payload)
            stories = (
                result.get("data", [])
                if isinstance(result, dict)
                else (result if isinstance(result, list) else [])
            )
            if not stories:
                break

            all_stories.extend(stories)

            if isinstance(result, dict):
                next_cursor = result.get("next")
                if not next_cursor or next_cursor in seen_cursors:
                    break
                seen_cursors.add(next_cursor)
            else:
                break

        return all_stories

    async def _stream_stories_and_comments(
        self,
        stories: List[Dict[str, Any]],
        since: Optional[datetime] = None,
        checkpoint: Optional[IngestionCheckpointRecord] = None,
    ) -> AsyncIterator[RawEventRecord]:
        """Tạo luồng emit story records và comment records kèm điều kiện lọc checkpoint."""
        for story in stories:
            story_rec = self.story_to_raw_event(story)
            project_id = story_rec.conversation_or_project_id
            app_url = story.get("app_url")

            should_emit_story = True
            if checkpoint and checkpoint.last_event_timestamp:
                if story_rec.event_timestamp < checkpoint.last_event_timestamp:
                    should_emit_story = False
                elif story_rec.event_timestamp == checkpoint.last_event_timestamp:
                    if checkpoint.last_external_id and story_rec.external_id == checkpoint.last_external_id:
                        should_emit_story = False

            if should_emit_story:
                yield story_rec

            # Handle comments if present and enabled
            if self.include_comments:
                comments = story.get("comments", []) if isinstance(story.get("comments"), list) else []
                for c in comments:
                    if c.get("deleted"):
                        continue
                    comm_rec = self.comment_to_raw_event(story_rec.external_id, project_id, app_url, c)
                    should_emit_comm = True
                    if since is not None and comm_rec.event_timestamp < since:
                        should_emit_comm = False
                    elif checkpoint and checkpoint.last_event_timestamp:
                        if comm_rec.event_timestamp < checkpoint.last_event_timestamp:
                            should_emit_comm = False
                        elif comm_rec.event_timestamp == checkpoint.last_event_timestamp:
                            if checkpoint.last_external_id and comm_rec.external_id == checkpoint.last_external_id:
                                should_emit_comm = False
                    if should_emit_comm:
                        yield comm_rec

    async def backfill(
        self, stream_id: str, since: Optional[datetime] = None
    ) -> AsyncIterator[RawEventRecord]:
        """Thu thập toàn bộ stories từ stream_id (project hoặc search query) từ mốc thời gian since."""
        logger.info("ShortcutAdapter backfill stream '%s' since=%s", stream_id, since)
        stories = await self._fetch_stories(stream_id, since=since)
        async for record in self._stream_stories_and_comments(stories, since=since):
            yield record

    async def poll_incremental(
        self, stream_id: str, checkpoint: Optional[IngestionCheckpointRecord] = None
    ) -> AsyncIterator[RawEventRecord]:
        """Thu thập các stories mới hoặc vừa cập nhật kể từ checkpoint."""
        since_dt = checkpoint.last_event_timestamp if checkpoint else None
        logger.info("ShortcutAdapter incremental poll stream '%s' since=%s", stream_id, since_dt)

        stories = await self._fetch_stories(stream_id, since=since_dt)
        async for record in self._stream_stories_and_comments(
            stories, since=since_dt, checkpoint=checkpoint
        ):
            yield record

    async def health(self) -> Dict[str, Any]:
        """Kiểm tra tình trạng kết nối và cấu hình của ShortcutAdapter."""
        is_configured = bool(self.api_token)

        health_data = {
            "source_type": SourceType.SHORTCUT.value,
            "tenant_id": self.tenant_id,
            "configured": is_configured,
            "base_url": self.base_url,
            "project_ids": self.project_ids,
            "default_query": self.default_query or "(none)",
            "status": AdapterHealthStatus.NOT_CONFIGURED.value,
        }

        if not is_configured and not self._http_fetcher:
            health_data["message"] = "Thiếu SHORTCUT_API_TOKEN"
            return health_data

        try:
            res = await self._execute_request("/member")
            health_data["status"] = AdapterHealthStatus.HEALTHY.value
            health_data["user"] = (
                res.get("profile", {}).get("name") if isinstance(res, dict) else None
            ) or (res.get("id") if isinstance(res, dict) else None)
        except AdapterAuthenticationError as auth_err:
            health_data["status"] = AdapterHealthStatus.AUTH_REQUIRED.value
            health_data["error"] = str(auth_err)
        except Exception as e:
            health_data["status"] = AdapterHealthStatus.DEGRADED.value
            health_data["error"] = str(e)

        return health_data
