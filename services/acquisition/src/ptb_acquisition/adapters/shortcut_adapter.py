"""Shortcut (formerly Clubhouse) REST API Acquisition Adapter.

Truy xuất và nạp stories từ Shortcut REST API v3 theo Search Query, Project hoặc Epic,
chuyển đổi thành RawEventRecord v1 chuẩn hóa với source_type = SourceType.SHORTCUT.
"""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Union
import urllib.error
import urllib.request
import uuid

from ptb_contracts import (
    IngestionCheckpointRecord,
    ProcessingStatus,
    RawEventRecord,
    SourceType,
)
from ptb_acquisition.adapters.base import AcquisitionAdapter

logger = logging.getLogger("ptb.acquisition.adapters.shortcut")


def _parse_shortcut_timestamp(ts_str: Optional[str]) -> datetime:
    """Parse chuỗi timestamp ISO-8601 từ Shortcut sang datetime UTC."""
    if not ts_str:
        return datetime.now(timezone.utc)
    try:
        cleaned = ts_str.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned).astimezone(timezone.utc)
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
    ):
        super().__init__(source_type=SourceType.SHORTCUT, tenant_id=tenant_id)
        self.api_token = api_token or os.getenv("SHORTCUT_API_TOKEN", "")
        self.base_url = (base_url or os.getenv("SHORTCUT_BASE_URL", self.DEFAULT_BASE_URL)).rstrip("/")
        self.project_ids = project_ids or []
        self.default_query = query or os.getenv("SHORTCUT_QUERY", "")
        self._http_fetcher = http_fetcher

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.api_token:
            headers["Shortcut-Token"] = self.api_token
        return headers

    async def _execute_request(
        self, endpoint: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None
    ) -> Any:
        """Thực thi HTTP request tới Shortcut API."""
        if self._http_fetcher:
            url = f"{self.base_url}{endpoint}" if not endpoint.startswith("http") else endpoint
            headers = self._get_headers()
            body_bytes = json.dumps(payload).encode("utf-8") if payload else None
            res = self._http_fetcher(url, headers, body_bytes)
            if asyncio.iscoroutine(res):
                return await res
            return res

        if not self.api_token:
            raise ValueError("Shortcut api_token chưa được thiết lập (SHORTCUT_API_TOKEN)")

        url = f"{self.base_url}{endpoint}" if not endpoint.startswith("http") else endpoint
        headers = self._get_headers()
        body_bytes = json.dumps(payload).encode("utf-8") if payload else None

        def _sync_call():
            req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read().decode("utf-8")
                return json.loads(data)

        return await asyncio.to_thread(_sync_call)

    def story_to_raw_event(self, story: Dict[str, Any]) -> RawEventRecord:
        """Chuyển đổi Shortcut Story JSON thành RawEventRecord v1 chuẩn hóa."""
        story_id = str(story.get("id", "UNKNOWN"))
        name = story.get("name", "")
        desc = story.get("description", "") or ""
        story_type = story.get("story_type", "feature")

        project_id = str(story.get("project_id", "") or story.get("group_id", "") or "shortcut")
        epic_id = str(story.get("epic_id")) if story.get("epic_id") else None

        # Build normalized text
        state_info = f"State: {story.get('workflow_state_id', '')}"
        normalized_text = f"[{story_id}] ({story_type}) {name}\n{state_info}\n\n{desc}".strip()
        content_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()

        # Idempotency key chuẩn v1: SHA256(tenant_id + source_type + external_id + content_hash)
        idempotency_raw = f"{self.tenant_id}:{SourceType.SHORTCUT.value}:{story_id}:{content_hash}"
        idempotency_key = hashlib.sha256(idempotency_raw.encode("utf-8")).hexdigest()

        event_timestamp = _parse_shortcut_timestamp(
            story.get("updated_at") or story.get("created_at")
        )
        now_utc = datetime.now(timezone.utc)

        creator_id = str(story.get("requested_by_id") or "shortcut-user")
        deep_link = story.get("app_url")
        payload_json = json.dumps(story, default=str)

        return RawEventRecord(
            id=f"raw-shortcut-{uuid.uuid4().hex[:12]}",
            tenant_id=self.tenant_id,
            source_type=SourceType.SHORTCUT,
            external_id=story_id,
            parent_external_id=epic_id,
            idempotency_key=idempotency_key,
            event_timestamp=event_timestamp,
            captured_at=now_utc,
            author_external_id=creator_id,
            author_display_name=None,
            conversation_or_project_id=project_id,
            deep_link=deep_link,
            raw_payload=story,
            payload_json=payload_json,
            normalized_text=normalized_text,
            content_hash=content_hash,
            processing_status=ProcessingStatus.PENDING,
            created_at=now_utc,
        )

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
                logger.warning(f"Không thể tự động discover Shortcut projects: {e}")

        return streams

    def _build_search_query(self, stream_id: str, since: Optional[datetime] = None) -> str:
        """Tạo search query cho Shortcut Search API."""
        query_parts: List[str] = []

        if stream_id not in ("all", ""):
            query_parts.append(f"project:{stream_id}")
        elif self.default_query:
            query_parts.append(self.default_query)
        elif self.project_ids:
            joined = " OR ".join(f"project:{pid}" for pid in self.project_ids)
            query_parts.append(f"({joined})")

        if since is not None:
            since_utc = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
            # Shortcut query format for dates: updated:>=2026-09-01
            query_parts.append(f"updated:>={since_utc.strftime('%Y-%m-%d')}")

        return " ".join(query_parts) if query_parts else "!is:archived"

    async def _fetch_stories(
        self, stream_id: str, since: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """Truy vấn stories từ Shortcut API."""
        all_stories: List[Dict[str, Any]] = []

        # Nếu stream_id là project cụ thể và không có search query phức tạp
        if stream_id not in ("all", "") and stream_id.isdigit() and not since:
            try:
                res = await self._execute_request(f"/projects/{stream_id}/stories")
                if isinstance(res, list):
                    return res
            except Exception as e:
                logger.debug(f"Không thể lấy theo /projects/{stream_id}/stories, fallback search: {e}")

        query = self._build_search_query(stream_id, since=since)
        next_cursor: Optional[str] = None

        while True:
            payload: Dict[str, Any] = {
                "query": query,
                "page_size": 25,
            }
            if next_cursor:
                payload["next"] = next_cursor

            try:
                result = await self._execute_request("/stories/search", method="POST", payload=payload)
            except Exception as e:
                logger.error(f"Lỗi truy vấn Shortcut stories qua query '{query}': {e}")
                break

            stories = result.get("data", []) if isinstance(result, dict) else (result if isinstance(result, list) else [])
            if not stories:
                break

            all_stories.extend(stories)

            if isinstance(result, dict):
                next_cursor = result.get("next")
                if not next_cursor:
                    break
            else:
                break

        return all_stories

    async def backfill(
        self, stream_id: str, since: Optional[datetime] = None
    ) -> AsyncIterator[RawEventRecord]:
        """Thu thập toàn bộ stories từ stream_id (project hoặc search query) từ mốc thời gian since."""
        logger.info(f"ShortcutAdapter backfill stream '{stream_id}' since={since}")
        stories = await self._fetch_stories(stream_id, since=since)
        for story in stories:
            yield self.story_to_raw_event(story)

    async def poll_incremental(
        self, stream_id: str, checkpoint: Optional[IngestionCheckpointRecord] = None
    ) -> AsyncIterator[RawEventRecord]:
        """Thu thập các stories mới hoặc vừa cập nhật kể từ checkpoint."""
        since_dt = checkpoint.last_event_timestamp if checkpoint else None
        logger.info(f"ShortcutAdapter incremental poll stream '{stream_id}' since={since_dt}")

        stories = await self._fetch_stories(stream_id, since=since_dt)
        for story in stories:
            event = self.story_to_raw_event(story)
            if checkpoint and checkpoint.last_event_timestamp:
                if event.event_timestamp <= checkpoint.last_event_timestamp:
                    if checkpoint.last_external_id and event.external_id == checkpoint.last_external_id:
                        continue
            yield event

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
            "status": "unconfigured",
        }

        if not is_configured and not self._http_fetcher:
            health_data["message"] = "Thiếu SHORTCUT_API_TOKEN"
            return health_data

        try:
            res = await self._execute_request("/member")
            health_data["status"] = "healthy"
            health_data["user"] = res.get("profile", {}).get("name") or res.get("id")
        except Exception as e:
            health_data["status"] = "degraded"
            health_data["error"] = str(e)

        return health_data
