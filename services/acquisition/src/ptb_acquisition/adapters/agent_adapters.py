"""Coding Agent Watchers Adapter (Layer 1B Wrapper).

Đóng gói các watchers (Cursor, Claude Code, Antigravity) vào chuẩn AcquisitionAdapter,
chuyển đổi các bản ghi RawAgentSessionRecord thành RawEventRecord v1 chuẩn hóa.
"""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from typing import Any, AsyncIterator, Dict, List, Optional
import uuid

from ptb_contracts import (
    AgentType,
    IngestionCheckpointRecord,
    ProcessingStatus,
    RawAgentSessionRecord,
    RawEventRecord,
    SourceType,
)
from ptb_acquisition.adapters.base import AcquisitionAdapter
from ptb_acquisition.watchers.antigravity_watcher import AntigravityWatcher
from ptb_acquisition.watchers.base import BaseAgentWatcher
from ptb_acquisition.watchers.claude_code_watcher import ClaudeCodeWatcher
from ptb_acquisition.watchers.cursor_watcher import CursorWatcher

logger = logging.getLogger("ptb.acquisition.adapters.agent")


class AgentWatchersAdapter(AcquisitionAdapter):
    """Adapter tích hợp các Coding Agent Watchers theo chuẩn AcquisitionAdapter."""

    def __init__(
        self,
        watchers: Optional[List[BaseAgentWatcher]] = None,
        tenant_id: str = "local-user",
    ):
        super().__init__(source_type=SourceType.CODING_AGENT, tenant_id=tenant_id)
        if watchers is not None:
            self.watchers = watchers
        else:
            self.watchers = [
                CursorWatcher(),
                ClaudeCodeWatcher(),
                AntigravityWatcher(),
            ]

    def turn_to_raw_event(self, turn: RawAgentSessionRecord) -> RawEventRecord:
        """Chuyển đổi RawAgentSessionRecord thành RawEventRecord chuẩn v1."""
        clean_text = turn.content.strip()
        content_hash = hashlib.sha256(clean_text.encode("utf-8")).hexdigest()
        external_id = f"{turn.session_id}:{turn.turn_index}:{turn.message_role}"

        # Idempotency key chuẩn v1: SHA256(tenant_id + source_type + external_id + content_hash)
        idempotency_raw = f"{self.tenant_id}:{SourceType.CODING_AGENT.value}:{external_id}:{content_hash}"
        idempotency_key = hashlib.sha256(idempotency_raw.encode("utf-8")).hexdigest()

        payload_dict = turn.model_dump(mode="json")
        payload_json = json.dumps(payload_dict, default=str)

        agent_val = (
            turn.agent_type.value
            if hasattr(turn.agent_type, "value")
            else str(turn.agent_type)
        )
        display_name = f"{agent_val.capitalize()} ({turn.message_role})"

        deep_link = None
        if turn.workspace_path:
            deep_link = f"file://{turn.workspace_path}"

        now_utc = datetime.now(timezone.utc)

        return RawEventRecord(
            id=f"raw-agent-{uuid.uuid4().hex[:12]}",
            tenant_id=self.tenant_id,
            source_type=SourceType.CODING_AGENT,
            external_id=external_id,
            parent_external_id=turn.session_id,
            idempotency_key=idempotency_key,
            event_timestamp=turn.timestamp,
            captured_at=now_utc,
            author_external_id=f"{agent_val}:{turn.message_role}",
            author_display_name=display_name,
            conversation_or_project_id=turn.workspace_path or turn.session_id,
            deep_link=deep_link,
            raw_payload=payload_dict,
            payload_json=payload_json,
            normalized_text=clean_text,
            content_hash=content_hash,
            processing_status=ProcessingStatus.PENDING,
            created_at=now_utc,
        )

    async def discover(self) -> List[Dict[str, Any]]:
        """Liệt kê các streams có sẵn (theo từng agent_type hoặc session_id)."""
        streams: List[Dict[str, Any]] = [
            {
                "stream_id": "all",
                "name": "All Coding Agents",
                "source_type": SourceType.CODING_AGENT.value,
                "description": "Aggregate stream of all local coding agents",
            }
        ]

        for watcher in self.watchers:
            agent_name = (
                watcher.agent_type.value
                if hasattr(watcher.agent_type, "value")
                else str(watcher.agent_type)
            )
            paths_found = [p for p in watcher.base_paths if os.path.exists(p)]
            is_avail = len(paths_found) > 0
            streams.append(
                {
                    "stream_id": agent_name,
                    "name": f"{agent_name.capitalize()} Watcher",
                    "agent_type": agent_name,
                    "source_type": SourceType.CODING_AGENT.value,
                    "paths_checked": watcher.base_paths,
                    "available": is_avail,
                    "status": "AVAILABLE" if is_avail else "NOT_INSTALLED",
                }
            )

        return streams

    def _get_watchers_for_stream(self, stream_id: str) -> List[BaseAgentWatcher]:
        """Lọc danh sách watcher tương ứng với stream_id."""
        if stream_id in ("all", "coding_agent", ""):
            return self.watchers
        
        target = [
            w for w in self.watchers
            if (w.agent_type.value if hasattr(w.agent_type, "value") else str(w.agent_type)) == stream_id
        ]
        if target:
            return target
        
        # Nếu stream_id là session cụ thể (ví dụ "cursor-tab-123" hay "claude-session-001")
        # thì quét toàn bộ watchers và lọc sau
        return self.watchers

    async def _collect_turns(
        self, stream_id: str, since: Optional[datetime] = None
    ) -> List[RawAgentSessionRecord]:
        """Thu thập và sắp xếp các session turns từ watchers."""
        watchers = self._get_watchers_for_stream(stream_id)
        all_turns: List[RawAgentSessionRecord] = []

        for w in watchers:
            try:
                # Nếu agent không được cài đặt trên máy người dùng, bỏ qua một cách an toàn
                if hasattr(w, "is_installed") and not w.is_installed:
                    continue
                # Quét an toàn trong async thread pool
                turns = await asyncio.to_thread(w.scan_sessions)
                for t in turns:
                    # Lọc theo session nếu stream_id là session ID cụ thể
                    agent_val = (
                        w.agent_type.value
                        if hasattr(w.agent_type, "value")
                        else str(w.agent_type)
                    )
                    if (
                        stream_id not in ("all", "coding_agent", agent_val, "")
                        and t.session_id != stream_id
                    ):
                        continue

                    # Lọc theo mốc thời gian since
                    if since is not None:
                        t_ts = t.timestamp
                        if t_ts.tzinfo is None:
                            t_ts = t_ts.replace(tzinfo=timezone.utc)
                        since_ts = since
                        if since_ts.tzinfo is None:
                            since_ts = since_ts.replace(tzinfo=timezone.utc)
                        if t_ts < since_ts:
                            continue

                    all_turns.append(t)
            except Exception as e:
                logger.warning(f"Lỗi khi quét session từ {w.agent_type}: {e}")

        # Sắp xếp theo timestamp tăng dần và turn_index
        def sort_key(turn: RawAgentSessionRecord):
            ts = turn.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return (ts, turn.turn_index)

        all_turns.sort(key=sort_key)
        return all_turns

    async def backfill(
        self, stream_id: str, since: Optional[datetime] = None
    ) -> AsyncIterator[RawEventRecord]:
        """Thu thập toàn bộ dữ liệu lịch sử từ stream chỉ định."""
        turns = await self._collect_turns(stream_id=stream_id, since=since)
        for turn in turns:
            yield self.turn_to_raw_event(turn)

    async def poll_incremental(
        self, stream_id: str, checkpoint: Optional[IngestionCheckpointRecord] = None
    ) -> AsyncIterator[RawEventRecord]:
        """Thu thập dữ liệu mới kể từ checkpoint."""
        since = checkpoint.last_event_timestamp if checkpoint else None
        turns = await self._collect_turns(stream_id=stream_id, since=since)

        for turn in turns:
            raw_event = self.turn_to_raw_event(turn)
            if checkpoint:
                ckpt_ts = checkpoint.last_event_timestamp
                if ckpt_ts:
                    event_ts = raw_event.event_timestamp
                    if event_ts.tzinfo is None:
                        event_ts = event_ts.replace(tzinfo=timezone.utc)
                    if ckpt_ts.tzinfo is None:
                        ckpt_ts = ckpt_ts.replace(tzinfo=timezone.utc)

                    if event_ts < ckpt_ts:
                        continue
                    if event_ts == ckpt_ts and checkpoint.last_external_id:
                        if raw_event.external_id == checkpoint.last_external_id:
                            continue

            yield raw_event

    async def health(self) -> Dict[str, Any]:
        """Kiểm tra tình trạng sức khỏe của adapter và các watchers bên dưới."""
        watcher_statuses: Dict[str, Any] = {}
        any_found = False

        for w in self.watchers:
            agent_key = (
                w.agent_type.value
                if hasattr(w.agent_type, "value")
                else str(w.agent_type)
            )
            paths_found = [p for p in w.base_paths if os.path.exists(p)]
            if paths_found:
                any_found = True

            watcher_statuses[agent_key] = {
                "status": "healthy" if paths_found else "NOT_INSTALLED",
                "installed": len(paths_found) > 0,
                "paths_checked": w.base_paths,
                "paths_found": paths_found,
            }

        return {
            "status": "healthy" if any_found else "standby",
            "source_type": SourceType.CODING_AGENT.value,
            "tenant_id": self.tenant_id,
            "watchers": watcher_statuses,
        }


# Alias thuận tiện
CodingAgentAdapter = AgentWatchersAdapter
