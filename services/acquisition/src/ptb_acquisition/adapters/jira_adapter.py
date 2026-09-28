"""Jira Cloud / Server Acquisition Adapter.

Truy xuất và nạp issues từ Jira REST API v2/v3 theo JQL hoặc Project Key,
chuyển đổi thành RawEventRecord v1 chuẩn hóa với source_type = SourceType.JIRA.
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

logger = logging.getLogger("ptb.acquisition.adapters.jira")


def _extract_jira_text(description_field: Any) -> str:
    """Trích xuất text từ Jira description.
    
    Hỗ trợ cả plain string (Jira Server / API v2) và 
    Atlassian Document Format (ADF dict/JSON ở Jira Cloud API v3).
    """
    if not description_field:
        return ""
    if isinstance(description_field, str):
        return description_field.strip()
    if isinstance(description_field, dict):
        text_parts = []
        def _walk(node: Any):
            if isinstance(node, dict):
                if node.get("type") == "text" and "text" in node:
                    text_parts.append(node["text"])
                for child in node.get("content", []):
                    _walk(child)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)
        _walk(description_field)
        return " ".join(text_parts).strip()
    return str(description_field).strip()


def _parse_jira_timestamp(ts_str: Optional[str]) -> datetime:
    """Parse chuỗi timestamp từ Jira ISO-8601 sang datetime UTC."""
    if not ts_str:
        return datetime.now(timezone.utc)
    try:
        # Handles format like "2026-09-28T14:30:00.000+0000" or "+0700" or "Z"
        cleaned = ts_str.replace("Z", "+00:00")
        if len(cleaned) >= 28 and (cleaned[-5] in ("+", "-")) and ":" not in cleaned[-5:]:
            # Convert +0700 to +07:00
            cleaned = cleaned[:-2] + ":" + cleaned[-2:]
        return datetime.fromisoformat(cleaned).astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


class JiraAdapter(AcquisitionAdapter):
    """Adapter tích hợp Jira REST API (Cloud & Server) theo chuẩn AcquisitionAdapter."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
        jql: Optional[str] = None,
        project_keys: Optional[List[str]] = None,
        tenant_id: str = "local-user",
        http_fetcher: Optional[Callable[[str, Dict[str, str], Optional[bytes]], Any]] = None,
    ):
        super().__init__(source_type=SourceType.JIRA, tenant_id=tenant_id)
        self.base_url = (base_url or os.getenv("JIRA_BASE_URL", "")).rstrip("/")
        self.email = email or os.getenv("JIRA_EMAIL", "")
        self.api_token = api_token or os.getenv("JIRA_API_TOKEN", "")
        self.default_jql = jql or os.getenv("JIRA_JQL", "")
        self.project_keys = project_keys or []
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
            # Bearer token (Personal Access Token for Jira Server/Data Center)
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    async def _execute_request(
        self, endpoint: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Thực thi HTTP request tới Jira API."""
        if self._http_fetcher:
            url = f"{self.base_url}{endpoint}" if self.base_url else endpoint
            headers = self._get_headers()
            body_bytes = json.dumps(payload).encode("utf-8") if payload else None
            res = self._http_fetcher(url, headers, body_bytes)
            if asyncio.iscoroutine(res):
                return await res
            return res

        if not self.base_url:
            raise ValueError("Jira base_url chưa được thiết lập (JIRA_BASE_URL)")

        url = f"{self.base_url}{endpoint}"
        headers = self._get_headers()
        body_bytes = json.dumps(payload).encode("utf-8") if payload else None

        def _sync_call():
            req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read().decode("utf-8")
                return json.loads(data)

        return await asyncio.to_thread(_sync_call)

    def issue_to_raw_event(self, issue: Dict[str, Any]) -> RawEventRecord:
        """Chuyển đổi Jira issue JSON thành RawEventRecord v1 chuẩn hóa."""
        fields = issue.get("fields", {})
        key = issue.get("key") or str(issue.get("id", "UNKNOWN"))
        summary = fields.get("summary", "")
        raw_desc = fields.get("description")
        desc_text = _extract_jira_text(raw_desc)

        status_info = fields.get("status") or {}
        status_name = status_info.get("name", "Unknown") if isinstance(status_info, dict) else str(status_info)

        assignee_info = fields.get("assignee") or {}
        assignee_name = assignee_info.get("displayName") or assignee_info.get("name") if isinstance(assignee_info, dict) else None

        creator_info = fields.get("creator") or fields.get("reporter") or {}
        creator_id = (
            creator_info.get("accountId")
            or creator_info.get("emailAddress")
            or creator_info.get("name")
            or "jira-user"
        ) if isinstance(creator_info, dict) else str(creator_info)
        creator_name = creator_info.get("displayName") if isinstance(creator_info, dict) else None

        project_info = fields.get("project") or {}
        project_key = project_info.get("key") if isinstance(project_info, dict) else str(project_info)
        if not project_key or project_key == "{}":
            project_key = key.split("-")[0] if "-" in key else "JIRA"

        parent_info = fields.get("parent") or {}
        parent_key = parent_info.get("key") if isinstance(parent_info, dict) else None

        # Build normalized text
        assignee_str = f"Assignee: {assignee_name}" if assignee_name else "Unassigned"
        normalized_text = f"[{key}] {summary}\nStatus: {status_name} | {assignee_str}\n\n{desc_text}".strip()
        content_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()

        # Idempotency key chuẩn v1: SHA256(tenant_id + source_type + external_id + content_hash)
        idempotency_raw = f"{self.tenant_id}:{SourceType.JIRA.value}:{key}:{content_hash}"
        idempotency_key = hashlib.sha256(idempotency_raw.encode("utf-8")).hexdigest()

        event_timestamp = _parse_jira_timestamp(fields.get("updated") or fields.get("created"))
        now_utc = datetime.now(timezone.utc)

        deep_link = f"{self.base_url}/browse/{key}" if self.base_url else None
        payload_json = json.dumps(issue, default=str)

        return RawEventRecord(
            id=f"raw-jira-{uuid.uuid4().hex[:12]}",
            tenant_id=self.tenant_id,
            source_type=SourceType.JIRA,
            external_id=key,
            parent_external_id=parent_key,
            idempotency_key=idempotency_key,
            event_timestamp=event_timestamp,
            captured_at=now_utc,
            author_external_id=creator_id,
            author_display_name=creator_name,
            conversation_or_project_id=project_key,
            deep_link=deep_link,
            raw_payload=issue,
            payload_json=payload_json,
            normalized_text=normalized_text,
            content_hash=content_hash,
            processing_status=ProcessingStatus.PENDING,
            created_at=now_utc,
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

        # 2. Thử truy vấn API /rest/api/3/project nếu có kết nối
        if self.base_url and (self.api_token or self._http_fetcher):
            try:
                data = await self._execute_request("/rest/api/3/project")
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
                logger.warning(f"Không thể tự động discover Jira projects qua API: {e}")

        return streams

    def _build_jql(self, stream_id: str, since: Optional[datetime] = None) -> str:
        """Xây dựng câu truy vấn JQL chuẩn xác theo stream_id và thời gian."""
        jql_parts: List[str] = []

        if stream_id not in ("all", ""):
            jql_parts.append(f"project = '{stream_id}'")
        elif self.default_jql:
            jql_parts.append(f"({self.default_jql})")
        elif self.project_keys:
            joined = ", ".join(f"'{k}'" for k in self.project_keys)
            jql_parts.append(f"project in ({joined})")

        if since is not None:
            since_utc = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
            formatted_date = since_utc.strftime("%Y-%m-%d %H:%M")
            jql_parts.append(f"updated >= '{formatted_date}'")

        if not jql_parts:
            return "ORDER BY updated ASC"

        return " AND ".join(jql_parts) + " ORDER BY updated ASC"

    async def _fetch_issues(self, jql: str) -> List[Dict[str, Any]]:
        """Truy vấn issues với JQL, hỗ trợ phân trang Jira REST API."""
        all_issues: List[Dict[str, Any]] = []
        start_at = 0
        max_results = 50

        while True:
            payload = {
                "jql": jql,
                "startAt": start_at,
                "maxResults": max_results,
                "fields": [
                    "summary",
                    "description",
                    "status",
                    "assignee",
                    "creator",
                    "reporter",
                    "project",
                    "parent",
                    "created",
                    "updated",
                ],
            }

            try:
                result = await self._execute_request("/rest/api/3/search", method="POST", payload=payload)
            except Exception as e:
                # Fallback to GET /rest/api/2/search or log error
                logger.error(f"Lỗi truy vấn Jira issues qua JQL '{jql}': {e}")
                break

            issues = result.get("issues", [])
            if not issues:
                break

            all_issues.extend(issues)
            total = result.get("total", 0)
            start_at += len(issues)

            if start_at >= total or len(issues) < max_results:
                break

        return all_issues

    async def backfill(
        self, stream_id: str, since: Optional[datetime] = None
    ) -> AsyncIterator[RawEventRecord]:
        """Thu thập toàn bộ issues từ stream_id (dự án hoặc JQL) từ mốc thời gian since."""
        jql = self._build_jql(stream_id, since=since)
        logger.info(f"JiraAdapter backfill stream '{stream_id}' với JQL: {jql}")

        issues = await self._fetch_issues(jql)
        for issue in issues:
            yield self.issue_to_raw_event(issue)

    async def poll_incremental(
        self, stream_id: str, checkpoint: Optional[IngestionCheckpointRecord] = None
    ) -> AsyncIterator[RawEventRecord]:
        """Thu thập các issues mới cập nhật kể từ checkpoint."""
        since_dt = checkpoint.last_event_timestamp if checkpoint else None
        jql = self._build_jql(stream_id, since=since_dt)
        logger.info(f"JiraAdapter incremental poll stream '{stream_id}' với JQL: {jql}")

        issues = await self._fetch_issues(jql)
        for issue in issues:
            event = self.issue_to_raw_event(issue)
            if checkpoint and checkpoint.last_event_timestamp:
                if event.event_timestamp <= checkpoint.last_event_timestamp:
                    if checkpoint.last_external_id and event.external_id == checkpoint.last_external_id:
                        continue
            yield event

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
            "status": "unconfigured",
        }

        if not is_configured and not self._http_fetcher:
            health_data["message"] = "Thiếu JIRA_BASE_URL hoặc JIRA_API_TOKEN"
            return health_data

        try:
            # Kiểm tra ping /rest/api/3/myself
            res = await self._execute_request("/rest/api/3/myself")
            health_data["status"] = "healthy"
            health_data["user"] = res.get("displayName") or res.get("name")
        except Exception as e:
            health_data["status"] = "degraded"
            health_data["error"] = str(e)

        return health_data
