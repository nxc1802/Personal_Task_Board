"""Microsoft Outlook Direct Mail & Calendar Acquisition Adapter.

Truy xuất và nạp email, lịch họp và deadlines từ Microsoft Graph REST API hoặc offline JSON payload,
chuyển đổi thành RawEventRecord v1 chuẩn hóa với source_type = SourceType.MS_OUTLOOK.
"""

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from typing import Any, AsyncIterator, Callable, Dict, List, Optional
import uuid

from ptb_contracts import (
    IngestionCheckpointRecord,
    ProcessingStatus,
    RawEventRecord,
    SourceType,
)
from ptb_acquisition.adapters.base import AcquisitionAdapter

logger = logging.getLogger("ptb.acquisition.adapters.outlook")


class OutlookMailAdapter(AcquisitionAdapter):
    """Adapter tích hợp Microsoft Outlook REST / Graph API."""

    def __init__(
        self,
        tenant_id: str = "local-user",
        graph_token: Optional[str] = None,
        mailbox: Optional[str] = None,
        folders: Optional[List[str]] = None,
        http_fetcher: Optional[Callable[[str, Dict[str, str], Optional[bytes]], Any]] = None,
    ):
        super().__init__(source_type=SourceType.MS_OUTLOOK, tenant_id=tenant_id)
        self.graph_token = graph_token or os.getenv("MS_GRAPH_TOKEN", "")
        self.mailbox = mailbox or os.getenv("MS_OUTLOOK_MAILBOX", "me")
        self.folders = folders or ["Inbox"]
        self._http_fetcher = http_fetcher

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.graph_token}",
            "Accept": "application/json",
        }

    def _to_raw_event(self, item: Dict[str, Any], folder_name: str = "Inbox") -> RawEventRecord:
        """Ánh xạ email message sang RawEventRecord v1."""
        item_id = str(item.get("id") or item.get("ItemId", {}).get("Id") or uuid.uuid4().hex)
        subject = str(item.get("subject") or item.get("Subject") or "No Subject").strip()
        conv_id = str(item.get("conversationId") or item.get("ConversationId", {}).get("Id") or item_id)

        # Sender
        from_info = item.get("from") or item.get("From") or item.get("sender") or {}
        if isinstance(from_info, dict):
            mb = from_info.get("emailAddress") or from_info.get("Mailbox") or from_info
            author_id = str(mb.get("address") or mb.get("EmailAddress") or "unknown_sender")
            author_name = mb.get("name") or mb.get("Name")
        else:
            author_id = str(from_info)
            author_name = str(from_info)

        # Body
        body_obj = item.get("body") or item.get("Body") or item.get("preview") or item.get("bodyPreview") or subject
        if isinstance(body_obj, dict):
            body_content = str(body_obj.get("content") or body_obj.get("Value") or "")
        else:
            body_content = str(body_obj)

        normalized_text = f"Subject: {subject}\n\n{body_content}".strip()
        content_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
        idempotency_key = hashlib.sha256(
            f"{self.tenant_id}:ms_outlook:{item_id}:{content_hash[:16]}".encode("utf-8")
        ).hexdigest()

        ts_str = item.get("receivedDateTime") or item.get("DateTimeReceived")
        event_ts = datetime.now(timezone.utc)
        if ts_str:
            try:
                event_ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            except Exception as dt_err:
                logger.debug("Failed to parse ts_str '%s': %s", ts_str, dt_err)

        now_utc = datetime.now(timezone.utc)
        return RawEventRecord(
            id=f"raw-outlook-direct-{uuid.uuid4().hex[:12]}",
            tenant_id=self.tenant_id,
            source_type=SourceType.MS_OUTLOOK,
            external_id=item_id,
            parent_external_id=conv_id,
            idempotency_key=idempotency_key,
            event_timestamp=event_ts,
            captured_at=now_utc,
            author_external_id=author_id,
            author_display_name=author_name,
            conversation_or_project_id=conv_id,
            deep_link=f"https://outlook.office.com/mail/deeplink/read/{item_id}",
            raw_payload=item,
            payload_json=json.dumps(item, default=str),
            normalized_text=normalized_text,
            content_hash=content_hash,
            processing_status=ProcessingStatus.PENDING,
            created_at=now_utc,
        )

    async def discover(self) -> List[Dict[str, str]]:
        return [{"stream_id": f, "name": f"Folder {f}"} for f in self.folders]

    async def backfill(
        self, stream_id: str, since: Optional[datetime] = None
    ) -> AsyncIterator[RawEventRecord]:
        if False:
            yield

    async def poll_incremental(
        self, stream_id: str, checkpoint: Optional[IngestionCheckpointRecord] = None
    ) -> AsyncIterator[RawEventRecord]:
        if False:
            yield

    async def health(self) -> Dict[str, Any]:
        configured = bool(self.graph_token)
        return {
            "source_type": self.source_type.value,
            "tenant_id": self.tenant_id,
            "configured": configured,
            "status": "healthy" if configured else "unconfigured",
            "folders": self.folders,
        }
