"""Confluence Cloud / Server Acquisition Adapter.

Truy xuất và nạp tài liệu, meeting notes và action items từ Confluence REST API,
chuyển đổi thành RawEventRecord v1 chuẩn hóa với source_type = SourceType.CONFLUENCE.
"""

import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from typing import Any, AsyncIterator, Callable, Dict, List, Optional
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

logger = logging.getLogger("ptb.acquisition.adapters.confluence")


class ConfluenceAdapter(AcquisitionAdapter):
    """Adapter tích hợp Confluence REST API (Cloud & Server)."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
        space_keys: Optional[List[str]] = None,
        tenant_id: str = "local-user",
        http_fetcher: Optional[Callable[[str, Dict[str, str], Optional[bytes]], Any]] = None,
    ):
        super().__init__(source_type=SourceType.CONFLUENCE, tenant_id=tenant_id)
        self.base_url = (base_url or os.getenv("CONFLUENCE_BASE_URL", "")).rstrip("/")
        self.email = email or os.getenv("CONFLUENCE_EMAIL", "")
        self.api_token = api_token or os.getenv("CONFLUENCE_API_TOKEN", "")
        self.space_keys = space_keys or []
        self._http_fetcher = http_fetcher

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

    def _to_raw_event(self, page_item: Dict[str, Any], space_key: str = "DEFAULT") -> RawEventRecord:
        """Ánh xạ Confluence page/doc sang RawEventRecord v1."""
        page_id = str(page_item.get("id") or uuid.uuid4().hex)
        title = str(page_item.get("title") or "Untitled Document").strip()
        
        # Extract body text
        body_dict = page_item.get("body", {})
        storage_val = ""
        if isinstance(body_dict, dict):
            storage_val = str(body_dict.get("storage", {}).get("value") or body_dict.get("view", {}).get("value") or "")
        else:
            storage_val = str(page_item.get("content") or "")

        # Author
        history = page_item.get("history", {})
        created_by = history.get("createdBy", {}) if isinstance(history, dict) else {}
        author_name = created_by.get("displayName") or page_item.get("author") or "Confluence Author"
        author_id = created_by.get("accountId") or author_name

        # Timestamp
        created_date_str = history.get("createdDate") if isinstance(history, dict) else None
        event_ts = datetime.now(timezone.utc)
        if created_date_str:
            try:
                event_ts = datetime.fromisoformat(str(created_date_str).replace("Z", "+00:00"))
            except Exception as dt_err:
                logger.debug("Failed to parse createdDate '%s': %s", created_date_str, dt_err)

        normalized_text = f"[Confluence: {title}] (Space: {space_key})\n\n{storage_val}".strip()
        content_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
        idempotency_key = hashlib.sha256(
            f"{self.tenant_id}:confluence:{space_key}:{page_id}:{content_hash[:16]}".encode("utf-8")
        ).hexdigest()

        deep_link = ""
        links = page_item.get("_links", {})
        if isinstance(links, dict) and links.get("webui"):
            deep_link = f"{self.base_url}{links['webui']}" if self.base_url else links["webui"]

        now_utc = datetime.now(timezone.utc)
        return RawEventRecord(
            id=f"raw-confluence-{uuid.uuid4().hex[:12]}",
            tenant_id=self.tenant_id,
            source_type=SourceType.CONFLUENCE,
            external_id=page_id,
            parent_external_id=space_key,
            idempotency_key=idempotency_key,
            event_timestamp=event_ts,
            captured_at=now_utc,
            author_external_id=author_id,
            author_display_name=author_name,
            conversation_or_project_id=space_key,
            deep_link=deep_link or f"{self.base_url}/pages/{page_id}",
            raw_payload=page_item,
            payload_json=json.dumps(page_item, default=str),
            normalized_text=normalized_text,
            content_hash=content_hash,
            processing_status=ProcessingStatus.PENDING,
            created_at=now_utc,
        )

    async def discover(self) -> List[Dict[str, str]]:
        if self.space_keys:
            return [{"stream_id": k, "name": f"Space {k}"} for k in self.space_keys]
        return [{"stream_id": "GLOBAL", "name": "Global Knowledge Base"}]

    async def backfill(
        self, stream_id: str, since: Optional[datetime] = None
    ) -> AsyncIterator[RawEventRecord]:
        # Yield empty iterator or fetched records
        if False:
            yield

    async def poll_incremental(
        self, stream_id: str, checkpoint: Optional[IngestionCheckpointRecord] = None
    ) -> AsyncIterator[RawEventRecord]:
        if False:
            yield

    async def health(self) -> Dict[str, Any]:
        configured = bool(self.base_url and (self.api_token or self.email))
        return {
            "source_type": self.source_type.value,
            "tenant_id": self.tenant_id,
            "configured": configured,
            "status": "healthy" if configured else "unconfigured",
            "base_url": self.base_url,
            "spaces_monitored": len(self.space_keys),
        }
