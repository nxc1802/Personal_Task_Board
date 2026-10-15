"""Unit tests for Acquisition Adapters & Pipeline Runtime.

Kiểm tra:
1. Kế thừa và hợp đồng chuẩn của AcquisitionAdapter(ABC).
2. AgentWatchersAdapter bọc các watcher (Cursor, Claude, Antigravity) và emit RawEventRecord chuẩn v1.
3. AcquisitionPipeline: nạp event, khử trùng lặp, lưu trữ bền bỉ và tự động resume checkpoint.
4. Đảm bảo Playwright interceptors (Teams & Outlook) emit RawEventRecord với đầy đủ payload_json, content_hash.
"""

from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
import tempfile
from typing import AsyncIterator, Dict, List, Optional
import pytest

from ptb_contracts import (
    AgentType,
    IngestionCheckpointRecord,
    ProcessingStatus,
    RawAgentSessionRecord,
    RawEventRecord,
    SourceType,
)
from ptb_acquisition.adapters.agent_adapters import AgentWatchersAdapter, CodingAgentAdapter
from ptb_acquisition.adapters.base import (
    AcquisitionAdapter,
    AdapterAuthenticationError,
    AdapterRateLimitError,
    AdapterTransientError,
)
from ptb_acquisition.pipeline import AcquisitionPipeline
from tests.support.test_doubles import (
    InMemoryCheckpointRepository,
    InMemoryRawEventRepository,
)
from ptb_acquisition.playwright.outlook_interceptor import OutlookNetworkInterceptor
from ptb_acquisition.playwright.teams_interceptor import TeamsNetworkInterceptor
from ptb_acquisition.queue import LocalIngestionQueue
from ptb_acquisition.watchers.antigravity_watcher import AntigravityWatcher
from ptb_acquisition.watchers.claude_code_watcher import ClaudeCodeWatcher
from ptb_acquisition.watchers.cursor_watcher import CursorWatcher


# --- 1. Kế thừa và hợp đồng của AcquisitionAdapter ---

class IncompleteAdapter(AcquisitionAdapter):
    """Adapter thiếu triển khai các abstract methods."""
    pass


class DummyAdapter(AcquisitionAdapter):
    """Adapter hoàn chỉnh phục vụ kiểm tra hợp đồng."""

    def __init__(self, tenant_id: str = "test-tenant"):
        super().__init__(source_type=SourceType.CODING_AGENT, tenant_id=tenant_id)
        self.events: List[RawEventRecord] = []

    async def discover(self) -> List[Dict[str, str]]:
        return [{"stream_id": "dummy-stream", "name": "Dummy Stream"}]

    async def backfill(
        self, stream_id: str, since: Optional[datetime] = None
    ) -> AsyncIterator[RawEventRecord]:
        for e in self.events:
            if since is None or e.event_timestamp >= since:
                yield e

    async def poll_incremental(
        self, stream_id: str, checkpoint: Optional[IngestionCheckpointRecord] = None
    ) -> AsyncIterator[RawEventRecord]:
        for e in self.events:
            if checkpoint and checkpoint.last_event_timestamp:
                if e.event_timestamp <= checkpoint.last_event_timestamp:
                    continue
            yield e

    async def health(self) -> Dict[str, str]:
        return {"status": "healthy", "source_type": self.source_type.value}


def test_acquisition_adapter_contract():
    # Không thể khởi tạo adapter chưa triển khai đủ abstract methods
    with pytest.raises(TypeError):
        IncompleteAdapter(source_type=SourceType.CODING_AGENT)

    # Khởi tạo thành công adapter đã triển khai đầy đủ
    adapter = DummyAdapter(tenant_id="tenant-123")
    assert adapter.source_type == SourceType.CODING_AGENT
    assert adapter.tenant_id == "tenant-123"
    assert "DummyAdapter" in repr(adapter)


# --- 2. AgentWatchersAdapter: Trích xuất và Emit RawEventRecord chuẩn v1 ---

def test_agent_watchers_adapter_discover_and_health():
    adapter = AgentWatchersAdapter(tenant_id="user-local")
    assert adapter.source_type == SourceType.CODING_AGENT
    assert adapter.tenant_id == "user-local"

    # Alias CodingAgentAdapter
    assert CodingAgentAdapter is AgentWatchersAdapter

    # Health check
    import asyncio
    health_res = asyncio.run(adapter.health())
    assert health_res["source_type"] == "coding_agent"
    assert "watchers" in health_res
    assert "cursor" in health_res["watchers"]
    assert "claude_code" in health_res["watchers"]
    assert "antigravity" in health_res["watchers"]

    # Discover streams
    streams = asyncio.run(adapter.discover())
    stream_ids = [s["stream_id"] for s in streams]
    assert "all" in stream_ids
    assert "cursor" in stream_ids
    assert "claude_code" in stream_ids
    assert "antigravity" in stream_ids


def test_agent_watchers_adapter_turn_conversion():
    adapter = AgentWatchersAdapter(tenant_id="test-tenant")

    session_turn = RawAgentSessionRecord(
        session_id="session-test-001",
        agent_type=AgentType.CURSOR,
        workspace_path="/workspace/my-project",
        turn_index=3,
        message_role="assistant",
        content="Chúng ta sẽ áp dụng Neo4j cho persistence.",
        tool_invocations=[{"tool": "run_command", "args": {"cmd": "ls"}}],
        timestamp=datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc),
        idempotency_key="original-session-key",
    )

    raw_event = adapter.turn_to_raw_event(session_turn)

    # Kiểm tra các trường chuẩn hóa v1
    assert raw_event.source_type == SourceType.CODING_AGENT
    assert raw_event.tenant_id == "test-tenant"
    assert raw_event.parent_external_id == "session-test-001"
    assert raw_event.external_id == "session-test-001:3:assistant"
    assert raw_event.normalized_text == "Chúng ta sẽ áp dụng Neo4j cho persistence."
    assert raw_event.content_hash is not None
    assert len(raw_event.content_hash) == 64  # SHA256 hex string

    # Idempotency key phải có định dạng hash SHA256
    assert len(raw_event.idempotency_key) == 64
    assert raw_event.payload_json is not None
    payload_obj = json.loads(raw_event.payload_json)
    assert payload_obj["session_id"] == "session-test-001"
    assert payload_obj["turn_index"] == 3

    assert raw_event.deep_link == "file:///workspace/my-project"
    assert raw_event.processing_status == ProcessingStatus.PENDING


@pytest.mark.asyncio
async def test_agent_watchers_adapter_backfill_and_incremental():
    with tempfile.TemporaryDirectory() as tmp_dir:
        # 1. Tạo dữ liệu giả lập cho Cursor
        cursor_ws = os.path.join(tmp_dir, "cursor_ws", "ws1")
        os.makedirs(cursor_ws)
        db_path = os.path.join(cursor_ws, "state.vscdb")
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        mock_chatdata = {
            "tabs": [
                {
                    "tabId": "tab-1",
                    "bubbles": [
                        {"type": "user", "rawText": "Task 1: cursor turn", "timestamp": 1727500000000},
                        {"type": "ai", "text": "Result 1: done", "timestamp": 1727500005000},
                    ],
                }
            ]
        }
        cur.execute("INSERT INTO ItemTable VALUES (?, ?)", ("workbench.panel.aichat.chatdata", json.dumps(mock_chatdata)))
        conn.commit()
        conn.close()

        # 2. Tạo dữ liệu giả lập cho Claude Code
        claude_dir = os.path.join(tmp_dir, "claude")
        os.makedirs(claude_dir)
        with open(os.path.join(claude_dir, "claude-session.jsonl"), "w") as f:
            f.write(json.dumps({
                "role": "user",
                "content": "Task 2: claude turn",
                "created_at": "2026-09-28T05:00:00Z"
            }) + "\n")

        # 3. Tạo dữ liệu giả lập cho Antigravity
        ag_dir = os.path.join(tmp_dir, "antigravity", "conv-001", ".system_generated", "logs")
        os.makedirs(ag_dir)
        with open(os.path.join(ag_dir, "transcript.jsonl"), "w") as f:
            f.write(json.dumps({
                "type": "USER_INPUT",
                "content": "Task 3: antigravity turn",
            }) + "\n")

        # Khởi tạo adapter với 3 watchers trỏ vào thư mục test
        cursor_watcher = CursorWatcher(base_paths=[os.path.join(tmp_dir, "cursor_ws")])
        claude_watcher = ClaudeCodeWatcher(base_paths=[claude_dir])
        ag_watcher = AntigravityWatcher(base_paths=[os.path.join(tmp_dir, "antigravity")])

        adapter = AgentWatchersAdapter(
            watchers=[cursor_watcher, claude_watcher, ag_watcher],
            tenant_id="unit-test",
        )

        # Kiểm tra backfill toàn bộ
        backfilled_records = []
        async for r in adapter.backfill(stream_id="all"):
            backfilled_records.append(r)

        assert len(backfilled_records) == 4
        for r in backfilled_records:
            assert r.source_type == SourceType.CODING_AGENT
            assert r.payload_json is not None
            assert r.content_hash is not None

        # Kiểm tra backfill theo stream_id cụ thể (cursor)
        cursor_only = []
        async for r in adapter.backfill(stream_id="cursor"):
            cursor_only.append(r)
        assert len(cursor_only) == 2

        # Kiểm tra poll_incremental với checkpoint
        mid_checkpoint = IngestionCheckpointRecord(
            id="ckpt-test",
            source_type=SourceType.CODING_AGENT,
            stream_id="all",
            last_event_timestamp=backfilled_records[1].event_timestamp,
            last_external_id=backfilled_records[1].external_id,
        )

        incremental_records = []
        async for r in adapter.poll_incremental(stream_id="all", checkpoint=mid_checkpoint):
            incremental_records.append(r)

        # Số event còn lại phải ít hơn tổng backfill
        assert len(incremental_records) <= len(backfilled_records)


# --- 3. AcquisitionPipeline: Ingestion, Deduplication, Repositories, Checkpoint Resume ---

@pytest.mark.asyncio
async def test_acquisition_pipeline_ingest_and_deduplication():
    queue = LocalIngestionQueue()
    raw_repo = InMemoryRawEventRepository()
    pipeline = AcquisitionPipeline(queue=queue, raw_event_repo=raw_repo)

    now = datetime.now(timezone.utc)
    rec1 = RawEventRecord(
        id="raw-1",
        tenant_id="tenant-1",
        source_type=SourceType.CODING_AGENT,
        external_id="ext-1",
        idempotency_key="sha256-key-001",
        event_timestamp=now,
        author_external_id="user-1",
        conversation_or_project_id="conv-1",
        raw_payload={"content": "hello world"},
        normalized_text="hello world",
    )

    rec2 = RawEventRecord(
        id="raw-2",
        tenant_id="tenant-1",
        source_type=SourceType.CODING_AGENT,
        external_id="ext-1",
        idempotency_key="sha256-key-001",  # Trùng idempotency_key
        event_timestamp=now,
        author_external_id="user-1",
        conversation_or_project_id="conv-1",
        raw_payload={"content": "hello world (duplicate)"},
        normalized_text="hello world (duplicate)",
    )

    pushed1 = await pipeline.ingest_event(rec1)
    pushed2 = await pipeline.ingest_event(rec2)

    assert pushed1 is True
    assert pushed2 is False
    assert pipeline.stats["ingested"] == 1
    assert pipeline.stats["deduplicated"] == 1
    assert pipeline.stats["persisted"] == 1

    saved = await raw_repo.get_by_id("raw-1")
    assert saved is not None
    assert saved.payload_json is not None
    assert saved.content_hash is not None


@pytest.mark.asyncio
async def test_acquisition_pipeline_automatic_checkpoint_resumption():
    raw_repo = InMemoryRawEventRepository()
    ckpt_repo = InMemoryCheckpointRepository()
    pipeline = AcquisitionPipeline(raw_event_repo=raw_repo, checkpoint_repo=ckpt_repo, tenant_id="tenant-test")

    # Tạo dummy adapter với 3 events theo thứ tự thời gian
    t0 = datetime(2026, 9, 28, 8, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=10)
    t2 = t0 + timedelta(minutes=20)

    adapter = DummyAdapter(tenant_id="tenant-test")
    adapter.events = [
        RawEventRecord(
            id="e0",
            tenant_id="tenant-test",
            source_type=SourceType.CODING_AGENT,
            external_id="ext-0",
            idempotency_key="key-0",
            event_timestamp=t0,
            author_external_id="author",
            conversation_or_project_id="proj",
            raw_payload={"msg": "first"},
        ),
        RawEventRecord(
            id="e1",
            tenant_id="tenant-test",
            source_type=SourceType.CODING_AGENT,
            external_id="ext-1",
            idempotency_key="key-1",
            event_timestamp=t1,
            author_external_id="author",
            conversation_or_project_id="proj",
            raw_payload={"msg": "second"},
        ),
    ]

    # --- Lần 1: Khởi động adapter khi chưa có checkpoint -> Tự động chạy backfill ---
    count1 = await pipeline.sync_adapter(adapter, stream_id="dummy-stream")
    assert count1 == 2
    assert raw_repo.count == 2

    # Kiểm tra checkpoint đã được lưu tự động sau đợt backfill
    ckpt1 = await ckpt_repo.get_checkpoint(SourceType.CODING_AGENT.value, "dummy-stream", tenant_id="tenant-test")
    assert ckpt1 is not None
    assert ckpt1.last_event_timestamp == t1
    assert ckpt1.last_external_id == "ext-1"

    # --- Lần 2: Chạy lại khi không có dữ liệu mới -> Không nạp thêm ---
    count2 = await pipeline.sync_adapter(adapter, stream_id="dummy-stream")
    assert count2 == 0
    assert raw_repo.count == 2

    # --- Lần 3: Adapter xuất hiện thêm dữ liệu mới e2 ---
    adapter.events.append(
        RawEventRecord(
            id="e2",
            tenant_id="tenant-test",
            source_type=SourceType.CODING_AGENT,
            external_id="ext-2",
            idempotency_key="key-2",
            event_timestamp=t2,
            author_external_id="author",
            conversation_or_project_id="proj",
            raw_payload={"msg": "third"},
        )
    )

    # Pipeline tự động resume từ checkpoint và nạp event mới qua poll_incremental
    count3 = await pipeline.sync_adapter(adapter, stream_id="dummy-stream")
    assert count3 == 1
    assert raw_repo.count == 3

    # Checkpoint được tự động cập nhật lên t2
    ckpt2 = await ckpt_repo.get_checkpoint(SourceType.CODING_AGENT.value, "dummy-stream", tenant_id="tenant-test")
    assert ckpt2.last_event_timestamp == t2
    assert ckpt2.last_external_id == "ext-2"


# --- 4. Kiểm tra Playwright Interceptors emit RawEventRecord chuẩn v1 ---

def test_playwright_interceptors_emit_standard_raw_event():
    # Teams Web
    teams_payload = {
        "messages": [
            {
                "id": "teams-msg-100",
                "conversationId": "chat-999",
                "body": {"content": "Xác nhận đã fix xong adapter."},
                "from": {"id": "dev-01", "displayName": "Dev One"}
            }
        ]
    }
    teams_interceptor = TeamsNetworkInterceptor(tenant_id="test-teams")
    teams_records = teams_interceptor.parse_payload(teams_payload)

    assert len(teams_records) == 1
    tr = teams_records[0]
    assert tr.source_type == SourceType.MS_TEAMS_WEB
    assert tr.payload_json is not None
    assert tr.content_hash is not None
    assert len(tr.content_hash) == 64
    assert tr.normalized_text == "Xác nhận đã fix xong adapter."
    assert tr.captured_at is not None

    # Outlook Web
    outlook_payload = {
        "value": [
            {
                "id": "outlook-msg-200",
                "subject": "Tiến độ tích hợp Layer 1",
                "body": {"content": "Mọi thứ đã sẵn sàng cho pipeline."},
                "from": {"emailAddress": {"name": "QA Lead", "address": "qa@test.com"}}
            }
        ]
    }
    outlook_interceptor = OutlookNetworkInterceptor(tenant_id="test-outlook")
    outlook_records = outlook_interceptor.parse_payload(outlook_payload)

    assert len(outlook_records) == 1
    or_rec = outlook_records[0]
    assert or_rec.source_type == SourceType.MS_OUTLOOK_WEB
    assert or_rec.payload_json is not None
    assert or_rec.content_hash is not None
    assert len(or_rec.content_hash) == 64
    assert or_rec.normalized_text == "Mọi thứ đã sẵn sàng cho pipeline."
    assert or_rec.captured_at is not None


# --- 5. Multi-Source Pipeline: Registration, Partial Failure Isolation, Checkpoint Scoping & Deduplication ---

class FailingAdapter(AcquisitionAdapter):
    """Adapter giả lập lỗi rate limit, auth, hoặc network để kiểm tra error isolation."""

    def __init__(
        self,
        source_type: SourceType,
        error_type: str = "rate_limit",
        tenant_id: str = "test-tenant",
    ):
        super().__init__(source_type=source_type, tenant_id=tenant_id)
        self.error_type = error_type

    async def discover(self) -> List[Dict[str, str]]:
        return [{"stream_id": "fail-stream", "name": f"Failing {self.source_type.value}"}]

    async def backfill(
        self, stream_id: str, since: Optional[datetime] = None
    ) -> AsyncIterator[RawEventRecord]:
        if self.error_type == "rate_limit":
            raise AdapterRateLimitError("Rate limit exceeded: 429 Too Many Requests", retry_after_seconds=30, source_type=self.source_type)
        elif self.error_type == "auth":
            raise AdapterAuthenticationError("Unauthorized: 401 Invalid Token", source_type=self.source_type)
        elif self.error_type == "network":
            raise AdapterTransientError("Connection reset by peer: ECONNRESET", source_type=self.source_type)
        else:
            raise RuntimeError(f"Simulated unknown failure: {self.error_type}")
        if False:
            yield None  # type: ignore

    async def poll_incremental(
        self, stream_id: str, checkpoint: Optional[IngestionCheckpointRecord] = None
    ) -> AsyncIterator[RawEventRecord]:
        if self.error_type == "rate_limit":
            raise AdapterRateLimitError("Rate limit in incremental poll", retry_after_seconds=60, source_type=self.source_type)
        raise RuntimeError("Incremental failure")
        if False:
            yield None  # type: ignore

    async def health(self) -> Dict[str, str]:
        return {"status": "unhealthy", "source_type": self.source_type.value}


@pytest.mark.asyncio
async def test_pipeline_multi_source_registration_and_lookup():
    pipeline = AcquisitionPipeline()

    adapter_coding = DummyAdapter(tenant_id="tenant-1")
    adapter_git = DummyAdapter(tenant_id="tenant-1")
    adapter_git.source_type = SourceType.GIT

    pipeline.register_adapter(adapter_coding, name="coding_agents")
    pipeline.register_adapter(adapter_git, name="git")

    assert pipeline.get_adapter("coding_agents") is adapter_coding
    assert pipeline.get_adapter(SourceType.CODING_AGENT) is adapter_coding
    assert pipeline.get_adapter("coding_agent") is adapter_coding
    assert pipeline.get_adapter("git") is adapter_git
    assert pipeline.get_adapter(SourceType.GIT) is adapter_git
    assert pipeline.get_adapter("unknown") is None

    registered = pipeline.list_registered_adapters()
    assert len(registered) == 2
    names = [r["name"] for r in registered]
    assert "coding_agents" in names
    assert "git" in names


@pytest.mark.asyncio
async def test_pipeline_sync_all_registered_partial_failure_tolerance():
    """Kiểm tra partial failure: lỗi ở adapter Jira (429) và Confluence (401)

    không làm sập pipeline hay ảnh hưởng tới CodingAgent và Git đang hoạt động bình thường.
    """
    raw_repo = InMemoryRawEventRepository()
    ckpt_repo = InMemoryCheckpointRepository()
    pipeline = AcquisitionPipeline(raw_event_repo=raw_repo, checkpoint_repo=ckpt_repo, tenant_id="tenant-1")

    now = datetime.now(timezone.utc)

    # 1. Adapter thành công: Coding Agent với 2 events
    ok_adapter = DummyAdapter(tenant_id="tenant-1")
    ok_adapter.events = [
        RawEventRecord(
            id="ok-1",
            tenant_id="tenant-1",
            source_type=SourceType.CODING_AGENT,
            external_id="ext-ok-1",
            idempotency_key="ok-key-1",
            event_timestamp=now,
            author_external_id="user",
            conversation_or_project_id="proj",
            raw_payload={"msg": "first ok"},
        ),
        RawEventRecord(
            id="ok-2",
            tenant_id="tenant-1",
            source_type=SourceType.CODING_AGENT,
            external_id="ext-ok-2",
            idempotency_key="ok-key-2",
            event_timestamp=now + timedelta(seconds=1),
            author_external_id="user",
            conversation_or_project_id="proj",
            raw_payload={"msg": "second ok"},
        ),
    ]

    # 2. Adapter lỗi rate limit: Jira
    rate_limited_adapter = FailingAdapter(SourceType.JIRA, error_type="rate_limit", tenant_id="tenant-1")

    # 3. Adapter lỗi auth: Confluence
    auth_failed_adapter = FailingAdapter(SourceType.CONFLUENCE, error_type="auth", tenant_id="tenant-1")

    # 4. Adapter lỗi network: Slack
    net_failed_adapter = FailingAdapter(SourceType.SLACK, error_type="network", tenant_id="tenant-1")

    pipeline.register_adapter(ok_adapter, name="coding_agents")
    pipeline.register_adapter(rate_limited_adapter, name="jira")
    pipeline.register_adapter(auth_failed_adapter, name="confluence")
    pipeline.register_adapter(net_failed_adapter, name="slack")

    # sync_all_registered KHÔNG được ném exception
    results = await pipeline.sync_all_registered()

    # Kiểm tra kết quả chi tiết từng source
    assert results["coding_agents"]["status"] == "success"
    assert results["coding_agents"]["count"] == 2

    assert results["jira"]["status"] == "error"
    assert "429" in results["jira"]["error"]

    assert results["confluence"]["status"] == "error"
    assert "401" in results["confluence"]["error"]

    assert results["slack"]["status"] == "error"
    assert "ECONNRESET" in results["slack"]["error"]

    # Stats tổng thể ghi nhận đúng số event nạp và số lỗi
    assert pipeline.stats["ingested"] == 2
    assert pipeline.stats["persisted"] == 2
    assert pipeline.stats["errors"] == 3
    assert raw_repo.count == 2


@pytest.mark.asyncio
async def test_pipeline_sync_source_on_demand():
    """Kiểm tra sync_source chạy riêng lẻ theo tên hoặc SourceType."""
    raw_repo = InMemoryRawEventRepository()
    pipeline = AcquisitionPipeline(raw_event_repo=raw_repo, tenant_id="tenant-1")

    adapter = DummyAdapter(tenant_id="tenant-1")
    adapter.events = [
        RawEventRecord(
            id="src-1",
            tenant_id="tenant-1",
            source_type=SourceType.CODING_AGENT,
            external_id="ext-src-1",
            idempotency_key="key-src-1",
            event_timestamp=datetime.now(timezone.utc),
            author_external_id="user",
            conversation_or_project_id="proj",
            raw_payload={"action": "test sync_source"},
        )
    ]
    pipeline.register_adapter(adapter, name="coding_agents")

    # Chạy theo tên
    count = await pipeline.sync_source("coding_agents")
    assert count == 1
    assert raw_repo.count == 1

    # Chạy adapter lỗi với raise_on_error=False không được crash
    fail_adapter = FailingAdapter(SourceType.GITHUB, error_type="auth", tenant_id="tenant-1")
    pipeline.register_adapter(fail_adapter, name="github")
    fail_count = await pipeline.sync_source("github", raise_on_error=False)
    assert fail_count == 0


@pytest.mark.asyncio
async def test_pipeline_composite_checkpoint_scoping():
    """Kiểm tra checkpoint scoping độc lập theo (tenant_id, source_type, stream_id)."""
    ckpt_repo = InMemoryCheckpointRepository()
    raw_repo = InMemoryRawEventRepository()

    pipeline_user1 = AcquisitionPipeline(raw_event_repo=raw_repo, checkpoint_repo=ckpt_repo, tenant_id="user-1")
    pipeline_user2 = AcquisitionPipeline(raw_event_repo=raw_repo, checkpoint_repo=ckpt_repo, tenant_id="user-2")

    t1 = datetime(2026, 10, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 1, 11, 0, 0, tzinfo=timezone.utc)

    # User 1 lưu checkpoint
    ckpt_user1 = IngestionCheckpointRecord(
        id="ckpt-u1",
        source_type=SourceType.GIT,
        stream_id="repo-main",
        last_event_timestamp=t1,
        last_external_id="commit-aaa",
        tenant_id="user-1",
    )
    await ckpt_repo.save_checkpoint(ckpt_user1)

    # User 2 lưu checkpoint trên cùng source và stream nhưng khác tenant
    ckpt_user2 = IngestionCheckpointRecord(
        id="ckpt-u2",
        source_type=SourceType.GIT,
        stream_id="repo-main",
        last_event_timestamp=t2,
        last_external_id="commit-bbb",
        tenant_id="user-2",
    )
    await ckpt_repo.save_checkpoint(ckpt_user2)

    # Kiểm tra truy xuất checkpoint đúng tenant
    loaded_u1 = await pipeline_user1._load_checkpoint(SourceType.GIT, "repo-main")
    loaded_u2 = await pipeline_user2._load_checkpoint(SourceType.GIT, "repo-main")

    assert loaded_u1 is not None
    assert loaded_u1.last_external_id == "commit-aaa"
    assert loaded_u1.last_event_timestamp == t1

    assert loaded_u2 is not None
    assert loaded_u2.last_external_id == "commit-bbb"
    assert loaded_u2.last_event_timestamp == t2


@pytest.mark.asyncio
async def test_pipeline_replay_deduplication_across_restarts():
    """Kiểm tra replay safety: chạy lại ingestion sau khi khởi động lại pipeline

    tuyệt đối không tạo ra duplicate task hoặc duplicate raw events trong repository.
    """
    shared_repo = InMemoryRawEventRepository()
    now = datetime.now(timezone.utc)

    event_payload = {
        "id": "event-rep-1",
        "tenant_id": "tenant-rep",
        "source_type": SourceType.GIT,
        "external_id": "commit-12345",
        "idempotency_key": "deterministic-hash-rep-1",
        "event_timestamp": now,
        "author_external_id": "developer",
        "conversation_or_project_id": "repo-alpha",
        "raw_payload": {"message": "feat: initial commit"},
        "normalized_text": "feat: initial commit",
    }

    rec = RawEventRecord(
        id=event_payload["id"],
        tenant_id=event_payload["tenant_id"],
        source_type=event_payload["source_type"],
        external_id=event_payload["external_id"],
        idempotency_key=event_payload["idempotency_key"],
        event_timestamp=event_payload["event_timestamp"],
        author_external_id=event_payload["author_external_id"],
        conversation_or_project_id=event_payload["conversation_or_project_id"],
        raw_payload=event_payload["raw_payload"],
        normalized_text=event_payload["normalized_text"],
    )

    # Run 1: Pipeline instance 1
    p1 = AcquisitionPipeline(raw_event_repo=shared_repo, tenant_id="tenant-rep")
    pushed1 = await p1.ingest_event(rec)
    assert pushed1 is True
    assert p1.stats["ingested"] == 1
    assert p1.stats["deduplicated"] == 0
    assert shared_repo.count == 1

    # Run 2: Giả lập restart process -> Tạo Pipeline instance mới hoàn toàn với cùng storage repo
    p2 = AcquisitionPipeline(raw_event_repo=shared_repo, tenant_id="tenant-rep")
    pushed2 = await p2.ingest_event(rec)

    # Phải bị phát hiện là duplicate từ repo và không nạp vào queue
    assert pushed2 is False
    assert p2.stats["ingested"] == 0
    assert p2.stats["deduplicated"] == 1
    # Số lượng trong kho lưu trữ không tăng
    assert shared_repo.count == 1

