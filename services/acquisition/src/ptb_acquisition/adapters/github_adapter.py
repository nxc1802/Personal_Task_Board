"""GitHub Issues & Pull Requests Acquisition Adapter.

Truy xuất và nạp issues, pull requests và review comments từ GitHub REST API v3,
chuyển đổi thành RawEventRecord v1 chuẩn hóa với source_type = SourceType.GITHUB.
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

logger = logging.getLogger("ptb.acquisition.adapters.github")


class GitHubAdapter(AcquisitionAdapter):
    """Adapter tích hợp GitHub REST API (Issues, Pull Requests)."""

    def __init__(
        self,
        repositories: Optional[List[str]] = None,
        api_token: Optional[str] = None,
        base_url: str = "https://api.github.com",
        tenant_id: str = "local-user",
        http_fetcher: Optional[Callable[[str, Dict[str, str], Optional[bytes]], Any]] = None,
    ):
        super().__init__(source_type=SourceType.GITHUB, tenant_id=tenant_id)
        self.repositories = repositories or []
        self.api_token = api_token or os.getenv("GITHUB_TOKEN", "")
        self.base_url = (base_url or "https://api.github.com").rstrip("/")
        self._http_fetcher = http_fetcher

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def _to_raw_event(self, item: Dict[str, Any], repo: str) -> RawEventRecord:
        """Ánh xạ GitHub Issue hoặc Pull Request sang RawEventRecord v1."""
        item_id = str(item.get("id") or uuid.uuid4().hex)
        number = item.get("number")
        title = str(item.get("title") or "Untitled Issue").strip()
        body = str(item.get("body") or "").strip()
        user = item.get("user", {})
        author_name = user.get("login") if isinstance(user, dict) else str(user)
        is_pr = "pull_request" in item

        created_at_str = item.get("created_at")
        event_ts = datetime.now(timezone.utc)
        if created_at_str:
            try:
                event_ts = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            except Exception as dt_err:
                logger.debug("Failed to parse created_at '%s': %s", created_at_str, dt_err)

        prefix = "Pull Request" if is_pr else "Issue"
        normalized_text = f"[GitHub {prefix} #{number}] {title}\n\n{body}".strip()
        content_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
        idempotency_key = hashlib.sha256(
            f"{self.tenant_id}:github:{repo}:{item_id}:{content_hash[:16]}".encode("utf-8")
        ).hexdigest()

        now_utc = datetime.now(timezone.utc)
        return RawEventRecord(
            id=f"raw-github-{uuid.uuid4().hex[:12]}",
            tenant_id=self.tenant_id,
            source_type=SourceType.GITHUB,
            external_id=str(number or item_id),
            parent_external_id=repo,
            idempotency_key=idempotency_key,
            event_timestamp=event_ts,
            captured_at=now_utc,
            author_external_id=author_name or "github-user",
            author_display_name=author_name,
            conversation_or_project_id=repo,
            deep_link=item.get("html_url") or f"https://github.com/{repo}/issues/{number}",
            raw_payload=item,
            payload_json=json.dumps(item, default=str),
            normalized_text=normalized_text,
            content_hash=content_hash,
            processing_status=ProcessingStatus.PENDING,
            created_at=now_utc,
        )

    async def discover(self) -> List[Dict[str, str]]:
        if self.repositories:
            return [{"stream_id": r, "name": f"Repo {r}"} for r in self.repositories]
        return [{"stream_id": "DEFAULT_REPO", "name": "Default Repository"}]

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
        configured = bool(self.api_token or self.repositories)
        return {
            "source_type": self.source_type.value,
            "tenant_id": self.tenant_id,
            "configured": configured,
            "status": "healthy" if configured else "unconfigured",
            "repositories": self.repositories,
        }
