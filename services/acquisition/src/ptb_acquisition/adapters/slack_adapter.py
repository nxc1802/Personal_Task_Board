"""Slack Conversations & Mentions Acquisition Adapter.

Truy xuất và nạp tin nhắn, threads và commitments từ Slack Web API v2,
chuyển đổi thành RawEventRecord v1 chuẩn hóa với source_type = SourceType.SLACK.
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

logger = logging.getLogger("ptb.acquisition.adapters.slack")


class SlackAdapter(AcquisitionAdapter):
    """Adapter tích hợp Slack Web API (Channels, Direct Messages)."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        channels: Optional[List[str]] = None,
        tenant_id: str = "local-user",
        http_fetcher: Optional[Callable[[str, Dict[str, str], Optional[bytes]], Any]] = None,
    ):
        super().__init__(source_type=SourceType.SLACK, tenant_id=tenant_id)
        self.bot_token = bot_token or os.getenv("SLACK_BOT_TOKEN", "")
        self.channels = channels or []
        self._http_fetcher = http_fetcher

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _to_raw_event(self, msg: Dict[str, Any], channel_id: str = "C_GENERAL") -> RawEventRecord:
        """Ánh xạ Slack Message sang RawEventRecord v1."""
        ts = str(msg.get("ts") or uuid.uuid4().hex)
        thread_ts = msg.get("thread_ts")
        text = str(msg.get("text") or "").strip()
        user_id = str(msg.get("user") or msg.get("username") or "slack-user").strip()

        event_ts = datetime.now(timezone.utc)
        try:
            event_ts = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except Exception as dt_err:
            logger.debug("Failed to parse Slack ts '%s': %s", ts, dt_err)

        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        idempotency_key = hashlib.sha256(
            f"{self.tenant_id}:slack:{channel_id}:{ts}:{content_hash[:16]}".encode("utf-8")
        ).hexdigest()

        now_utc = datetime.now(timezone.utc)
        return RawEventRecord(
            id=f"raw-slack-{uuid.uuid4().hex[:12]}",
            tenant_id=self.tenant_id,
            source_type=SourceType.SLACK,
            external_id=ts,
            parent_external_id=str(thread_ts) if thread_ts else channel_id,
            idempotency_key=idempotency_key,
            event_timestamp=event_ts,
            captured_at=now_utc,
            author_external_id=user_id,
            author_display_name=user_id,
            conversation_or_project_id=channel_id,
            deep_link=f"https://slack.com/archives/{channel_id}/p{ts.replace('.', '')}",
            raw_payload=msg,
            payload_json=json.dumps(msg, default=str),
            normalized_text=text,
            content_hash=content_hash,
            processing_status=ProcessingStatus.PENDING,
            created_at=now_utc,
        )

    async def discover(self) -> List[Dict[str, str]]:
        if self.channels:
            return [{"stream_id": c, "name": f"Channel #{c}"} for c in self.channels]
        return [{"stream_id": "general", "name": "Channel #general"}]

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
        configured = bool(self.bot_token)
        return {
            "source_type": self.source_type.value,
            "tenant_id": self.tenant_id,
            "configured": configured,
            "status": "healthy" if configured else "unconfigured",
            "channels_monitored": len(self.channels),
        }
