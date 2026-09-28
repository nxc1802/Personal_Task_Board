"""Unit tests for Neo4j Repositories: RawEventRepository, CheckpointRepository, TaskDomainRepository."""

from datetime import datetime, timezone
import json
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import AsyncMock
import pytest

from ptb_contracts.l1_acquisition import (
    IngestionCheckpointRecord,
    ProcessingAttemptRecord,
    ProcessingStatus,
    RawEventRecord,
    SourceType,
)
from ptb_contracts.l2_processing import (
    EvidenceRecord,
    EvidenceType,
    MergeAuditRecord,
    StatusTransitionAuditRecord,
    TaskStatus,
    UnifiedTaskCandidate,
)
from ptb_database.neo4j_client import Neo4jClient
from ptb_database.repositories import (
    CheckpointRepository,
    RawEventRepository,
    TaskDomainRepository,
)


class MockRecord:
    def __init__(self, data: Dict[str, Any]):
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def data(self) -> Dict[str, Any]:
        return self._data

    def __contains__(self, key: str) -> bool:
        return key in self._data


class MockAsyncResult:
    def __init__(self, records: Optional[List[MockRecord]] = None, single_record: Optional[MockRecord] = None):
        self.records = records or []
        self._single_record = single_record if single_record is not None else (self.records[0] if self.records else None)

    async def single(self) -> Optional[MockRecord]:
        return self._single_record

    def __aiter__(self):
        self._iter = iter(self.records)
        return self

    async def __anext__(self) -> MockRecord:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class MockTransaction:
    def __init__(self):
        self.queries: List[tuple[str, Dict[str, Any]]] = []
        self.committed = False
        self.rolled_back = False

    async def run(self, query: str, parameters: Optional[Dict[str, Any]] = None) -> MockAsyncResult:
        self.queries.append((query.strip(), parameters or {}))
        return MockAsyncResult()

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class MockSession:
    def __init__(self, run_handler: Optional[Callable] = None):
        self.queries: List[tuple[str, Dict[str, Any]]] = []
        self.run_handler = run_handler
        self.active_tx: Optional[MockTransaction] = None

    async def run(self, query: str, parameters: Optional[Dict[str, Any]] = None) -> MockAsyncResult:
        clean_query = query.strip()
        params = parameters or {}
        self.queries.append((clean_query, params))
        if self.run_handler:
            return await self.run_handler(clean_query, params)
        return MockAsyncResult()

    def begin_transaction(self) -> MockTransaction:
        self.active_tx = MockTransaction()
        return self.active_tx

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class MockDriver:
    def __init__(self, session: MockSession):
        self._session = session

    def session(self, database: Optional[str] = None) -> MockSession:
        return self._session

    async def close(self) -> None:
        pass


def make_client_with_session(session: MockSession) -> Neo4jClient:
    client = Neo4jClient()
    client._driver = MockDriver(session)
    return client


# ==============================================================================
# 1. RawEventRepository Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_persist_raw_event_success():
    async def handler(query, params):
        assert "MERGE (re:RawEvent {idempotency_key: $idempotency_key})" in query
        assert params["idempotency_key"] == "idemp-001"
        assert params["source_type"] == "ms_teams"
        assert params["tenant_id"] == "tenant-default"
        assert params["processing_status"] == "pending"
        assert params["payload_json"] == '{"text": "Hello world"}'
        return MockAsyncResult(single_record=MockRecord({"id": "event-123"}))

    session = MockSession(run_handler=handler)
    client = make_client_with_session(session)
    repo = RawEventRepository(client)

    record = RawEventRecord(
        id="event-123",
        tenant_id="tenant-default",
        source_type=SourceType.MS_TEAMS,
        external_id="ext-msg-1",
        idempotency_key="idemp-001",
        event_timestamp=datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc),
        author_external_id="author-1",
        conversation_or_project_id="chat-1",
        raw_payload={"text": "Hello world"},
        payload_json=None,
        normalized_text="Hello world",
        content_hash="hash-123",
        processing_status=ProcessingStatus.PENDING,
    )

    returned_id = await repo.persist_raw_event(record)
    assert returned_id == "event-123"
    assert len(session.queries) == 1


@pytest.mark.asyncio
async def test_persist_raw_event_idempotency_existing():
    # Giả lập node đã tồn tại, MERGE trả về event ID đã có trong DB
    async def handler(query, params):
        return MockAsyncResult(single_record=MockRecord({"id": "existing-event-999"}))

    session = MockSession(run_handler=handler)
    client = make_client_with_session(session)
    repo = RawEventRepository(client)

    record = RawEventRecord(
        id="attempted-new-id",
        tenant_id="tenant-default",
        source_type=SourceType.JIRA,
        external_id="PROJ-101",
        idempotency_key="idemp-dup",
        event_timestamp=datetime.now(timezone.utc),
        author_external_id="dev-1",
        conversation_or_project_id="PROJ",
        raw_payload={"summary": "Fix bug"},
    )

    returned_id = await repo.persist_raw_event(record)
    assert returned_id == "existing-event-999"


@pytest.mark.asyncio
async def test_get_pending_raw_events():
    async def handler(query, params):
        assert "re.processing_status IN ['PENDING', 'pending', 'RETRY', 'retry']" in query
        assert "(re.next_retry_at IS NULL OR re.next_retry_at <= datetime())" in query
        assert "ORDER BY re.event_timestamp ASC" in query
        assert params["limit"] == 10
        nodes = [
            MockRecord({
                "re": {
                    "id": "raw-1",
                    "tenant_id": "tenant-default",
                    "source_type": "ms_teams",
                    "external_id": "ext-1",
                    "idempotency_key": "idemp-1",
                    "event_timestamp": "2026-09-28T10:00:00+00:00",
                    "author_external_id": "user-1",
                    "conversation_or_project_id": "conv-1",
                    "payload_json": '{"body": "deploy today"}',
                    "processing_status": "pending",
                    "retry_count": 0,
                }
            }),
            MockRecord({
                "re": {
                    "id": "raw-2",
                    "tenant_id": "tenant-default",
                    "source_type": "jira",
                    "external_id": "JIRA-2",
                    "idempotency_key": "idemp-2",
                    "event_timestamp": "2026-09-28T11:00:00+00:00",
                    "author_external_id": "user-2",
                    "conversation_or_project_id": "JIRA",
                    "payload_json": '{"summary": "test issue"}',
                    "processing_status": "pending",
                    "retry_count": 0,
                }
            })
        ]
        return MockAsyncResult(records=nodes)

    session = MockSession(run_handler=handler)
    client = make_client_with_session(session)
    repo = RawEventRepository(client)

    events = await repo.get_pending_raw_events(limit=10)
    assert len(events) == 2
    assert events[0].id == "raw-1"
    assert events[0].source_type == SourceType.MS_TEAMS
    assert events[0].raw_payload == {"body": "deploy today"}
    assert events[1].id == "raw-2"
    assert events[1].source_type == SourceType.JIRA


@pytest.mark.asyncio
async def test_mark_event_status_success():
    session = MockSession()
    client = make_client_with_session(session)
    repo = RawEventRepository(client)

    await repo.mark_event_status("raw-123", ProcessingStatus.PROCESSED)
    assert len(session.queries) == 1
    query, params = session.queries[0]
    assert "MATCH (re:RawEvent {id: $event_id})" in query
    assert params["event_id"] == "raw-123"
    assert params["status"] == "processed"
    assert params["error"] is None


@pytest.mark.asyncio
async def test_mark_event_status_failed_increments_retry():
    session = MockSession()
    client = make_client_with_session(session)
    repo = RawEventRepository(client)

    await repo.mark_event_status("raw-123", ProcessingStatus.FAILED, error="Connection timeout")
    assert len(session.queries) == 1
    query, params = session.queries[0]
    assert "re.retry_count = CASE WHEN $status = 'failed' THEN coalesce(re.retry_count, 0) + 1" in query
    assert params["event_id"] == "raw-123"
    assert params["status"] == "failed"
    assert params["error"] == "Connection timeout"


@pytest.mark.asyncio
async def test_record_processing_attempt():
    session = MockSession()
    client = make_client_with_session(session)
    repo = RawEventRepository(client)

    attempt = ProcessingAttemptRecord(
        id="attempt-001",
        raw_event_id="raw-123",
        attempt_number=2,
        status=ProcessingStatus.FAILED,
        error_message="LLM quota exceeded",
        attempted_at=datetime(2026, 9, 28, 12, 30, 0, tzinfo=timezone.utc),
    )

    await repo.record_processing_attempt(attempt)
    assert len(session.queries) == 1
    query, params = session.queries[0]
    assert "MATCH (re:RawEvent {id: $raw_event_id})" in query
    assert "MERGE (pa:ProcessingAttempt {id: $id})" in query
    assert "MERGE (re)-[:PROCESSING_ATTEMPT]->(pa)" in query
    assert params["raw_event_id"] == "raw-123"
    assert params["id"] == "attempt-001"
    assert params["attempt_number"] == 2
    assert params["status"] == "failed"
    assert params["error_message"] == "LLM quota exceeded"


# ==============================================================================
# 2. CheckpointRepository Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_get_checkpoint_found():
    async def handler(query, params):
        assert "MATCH (cp:IngestionCheckpoint)" in query
        assert params["source_type"] == "ms_teams"
        assert params["stream_id"] == "channel-general"
        assert params["tenant_id"] == "tenant-default"
        node = {
            "id": "cp-1",
            "source_type": "ms_teams",
            "stream_id": "channel-general",
            "tenant_id": "tenant-default",
            "last_external_id": "msg-99",
            "last_event_timestamp": "2026-09-28T12:00:00+00:00",
            "cursor_token": "delta-token-abc",
            "updated_at": "2026-09-28T12:05:00+00:00",
        }
        return MockAsyncResult(single_record=MockRecord({"cp": node}))

    session = MockSession(run_handler=handler)
    client = make_client_with_session(session)
    repo = CheckpointRepository(client)

    cp = await repo.get_checkpoint(
        source_type="ms_teams",
        stream_id="channel-general",
        tenant_id="tenant-default",
    )
    assert cp is not None
    assert cp.id == "cp-1"
    assert cp.last_external_id == "msg-99"
    assert cp.cursor_token == "delta-token-abc"


@pytest.mark.asyncio
async def test_get_checkpoint_not_found():
    async def handler(query, params):
        return MockAsyncResult(single_record=None)

    session = MockSession(run_handler=handler)
    client = make_client_with_session(session)
    repo = CheckpointRepository(client)

    cp = await repo.get_checkpoint("jira", "proj-unknown")
    assert cp is None


@pytest.mark.asyncio
async def test_save_checkpoint_with_id():
    session = MockSession()
    client = make_client_with_session(session)
    repo = CheckpointRepository(client)

    checkpoint = IngestionCheckpointRecord(
        id="checkpoint-custom-id",
        source_type=SourceType.SHORTCUT,
        stream_id="workspace-ops",
        tenant_id="tenant-shortcut",
        last_external_id="story-456",
        cursor_token="cursor-xyz",
    )

    await repo.save_checkpoint(checkpoint)
    assert len(session.queries) == 1
    query, params = session.queries[0]
    assert "MERGE (cp:IngestionCheckpoint {id: $id})" in query
    assert params["id"] == "checkpoint-custom-id"
    assert params["source_type"] == "shortcut"
    assert params["stream_id"] == "workspace-ops"
    assert params["last_external_id"] == "story-456"
    assert params["cursor_token"] == "cursor-xyz"


@pytest.mark.asyncio
async def test_save_checkpoint_composite_key():
    session = MockSession()
    client = make_client_with_session(session)
    repo = CheckpointRepository(client)

    checkpoint = IngestionCheckpointRecord(
        id="",  # Rỗng -> tạo composite key
        source_type=SourceType.CODING_AGENT,
        stream_id="session-antigravity-1",
        tenant_id="local",
        last_external_id="turn-5",
    )

    await repo.save_checkpoint(checkpoint)
    assert len(session.queries) == 1
    query, params = session.queries[0]
    assert params["id"] == "local:coding_agent:session-antigravity-1"


# ==============================================================================
# 3. TaskDomainRepository Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_upsert_task_atomic_full():
    session = MockSession()
    client = make_client_with_session(session)
    repo = TaskDomainRepository(client)

    task = UnifiedTaskCandidate(
        id="task-999",
        title="Deploy release v1.0",
        description="Release personal task board v1.0",
        status=TaskStatus.IN_PROGRESS,
        priority_score=85.5,
        due_date=datetime(2026, 9, 30, 17, 0, 0, tzinfo=timezone.utc),
        owner_canonical_id="person-cuong",
        owner_name="Nguyen Xuan Cuong",
        requester_canonical_id="person-lead",
        requester_name="Team Lead",
        project_key="PTB",
        customer_id="internal",
        evidences=[
            EvidenceRecord(
                id="ev-1",
                task_id="task-999",
                raw_event_id="raw-event-1",
                evidence_type=EvidenceType.CHAT_COMMITMENT,
                source_type="ms_teams",
                timestamp=datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc),
                snippet="I will deploy release v1.0 by Wednesday",
                confidence=0.95,
            )
        ]
    )

    returned_id = await repo.upsert_task_atomic(task)
    assert returned_id == "task-999"

    # Đảm bảo transaction được mở và commit
    assert session.active_tx is not None
    assert session.active_tx.committed is True

    # Kiểm tra các câu lệnh Cypher được thực thi trong transaction
    tx_queries = session.active_tx.queries
    assert len(tx_queries) == 4  # 1: task, 2: owner, 3: requester, 4: evidences

    # 1. Task query
    q1, p1 = tx_queries[0]
    assert "MERGE (t:UnifiedTask {id: $id})" in q1
    assert p1["id"] == "task-999"
    assert p1["title"] == "Deploy release v1.0"
    assert p1["status"] == "IN_PROGRESS"
    assert p1["priority_score"] == 85.5

    # 2. Owner query
    q2, p2 = tx_queries[1]
    assert "MERGE (p:Person {canonical_id: $owner_canonical_id})" in q2
    assert "MERGE (p)-[:ASSIGNED_TO]->(t)" in q2
    assert p2["owner_canonical_id"] == "person-cuong"

    # 3. Requester query
    q3, p3 = tx_queries[2]
    assert "MERGE (req:Person {canonical_id: $requester_canonical_id})" in q3
    assert "MERGE (req)-[:REQUESTED]->(t)" in q3
    assert p3["requester_canonical_id"] == "person-lead"

    # 4. Evidences query
    q4, p4 = tx_queries[3]
    assert "UNWIND $evidences AS ev" in q4
    assert "MERGE (e:Evidence {id: ev.id})" in q4
    assert "MERGE (t)-[:HAS_EVIDENCE]->(e)" in q4
    assert "MERGE (e)-[:DERIVED_FROM]->(re)" in q4
    assert len(p4["evidences"]) == 1
    assert p4["evidences"][0]["id"] == "ev-1"
    assert p4["evidences"][0]["raw_event_id"] == "raw-event-1"


@pytest.mark.asyncio
async def test_upsert_task_atomic_minimal():
    session = MockSession()
    client = make_client_with_session(session)
    repo = TaskDomainRepository(client)

    task = UnifiedTaskCandidate(
        id="task-minimal",
        title="Quick task without owner or evidences",
        status=TaskStatus.TODO,
    )

    returned_id = await repo.upsert_task_atomic(task)
    assert returned_id == "task-minimal"
    assert session.active_tx.committed is True
    # Chỉ có 1 query task vì không có owner/requester/evidences
    assert len(session.active_tx.queries) == 1


@pytest.mark.asyncio
async def test_get_task_by_id_found():
    async def handler(query, params):
        assert "MATCH (t:UnifiedTask {id: $task_id})" in query
        assert params["task_id"] == "t-100"
        task_node = {
            "id": "t-100",
            "title": "Build Graphiti integration",
            "description": "Integrate temporal memory",
            "status": "TODO",
            "priority_score": 70.0,
            "due_date": "2026-10-01T10:00:00+00:00",
            "project_key": "MEMORY",
        }
        ev1 = {
            "id": "ev-100",
            "task_id": "t-100",
            "raw_event_id": "raw-200",
            "evidence_type": "agent_decision",
            "source_type": "antigravity",
            "timestamp": "2026-09-28T09:00:00+00:00",
            "snippet": "We should use Graphiti for temporal knowledge graph",
            "confidence": 0.9,
            "extraction_version": "v1.0",
        }
        return MockAsyncResult(single_record=MockRecord({
            "t": task_node,
            "owner_canonical_id": "p-1",
            "owner_name": "Cuong",
            "requester_canonical_id": None,
            "requester_name": None,
            "evidences": [ev1],
        }))

    session = MockSession(run_handler=handler)
    client = make_client_with_session(session)
    repo = TaskDomainRepository(client)

    task = await repo.get_task_by_id("t-100")
    assert task is not None
    assert task.id == "t-100"
    assert task.title == "Build Graphiti integration"
    assert task.owner_canonical_id == "p-1"
    assert task.owner_name == "Cuong"
    assert len(task.evidences) == 1
    assert task.evidences[0].id == "ev-100"
    assert task.evidences[0].evidence_type == EvidenceType.AGENT_DECISION


@pytest.mark.asyncio
async def test_get_task_by_id_not_found():
    async def handler(query, params):
        return MockAsyncResult(single_record=None)

    session = MockSession(run_handler=handler)
    client = make_client_with_session(session)
    repo = TaskDomainRepository(client)

    task = await repo.get_task_by_id("non-existent-task")
    assert task is None


@pytest.mark.asyncio
async def test_record_status_transition_audit():
    async def handler(query, params):
        assert "MATCH (t:UnifiedTask {id: $task_id})" in query
        assert "CREATE (a:StatusTransitionAudit" in query
        assert "CREATE (t)-[:STATUS_AUDIT]->(a)" in query
        assert params["task_id"] == "t-500"
        assert params["old_status"] == "TODO"
        assert params["new_status"] == "IN_PROGRESS"
        assert params["reason"] == "Detected PR opened"
        assert params["change_actor"] == "SYSTEM"
        return MockAsyncResult(single_record=MockRecord({"id": "audit-uuid-1"}))

    session = MockSession(run_handler=handler)
    client = make_client_with_session(session)
    repo = TaskDomainRepository(client)

    audit = StatusTransitionAuditRecord(
        id="audit-uuid-1",
        task_id="t-500",
        old_status=TaskStatus.TODO,
        new_status=TaskStatus.IN_PROGRESS,
        reason="Detected PR opened",
        source_evidence_ids=["ev-pr-1"],
        confidence=0.88,
        changed_at=datetime(2026, 9, 28, 14, 0, 0, tzinfo=timezone.utc),
        change_actor="SYSTEM",
    )

    audit_id = await repo.record_status_transition_audit(audit)
    assert audit_id == "audit-uuid-1"


# ==============================================================================
# 4. Checkpoint Deduplication & Advanced Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_cleanup_duplicate_checkpoints_with_deletions():
    async def handler(query, params):
        if "size(nodes) > 1" in query:
            assert "UNWIND tail(nodes) AS dup" in query
            assert "DETACH DELETE dup" in query
            return MockAsyncResult(single_record=MockRecord({"deleted_count": 3}))
        return MockAsyncResult()

    session = MockSession(run_handler=handler)
    client = make_client_with_session(session)
    repo = CheckpointRepository(client)

    deleted = await repo.cleanup_duplicate_checkpoints()
    assert deleted == 3


@pytest.mark.asyncio
async def test_cleanup_duplicate_checkpoints_no_duplicates():
    async def handler(query, params):
        return MockAsyncResult(single_record=None)

    session = MockSession(run_handler=handler)
    client = make_client_with_session(session)
    repo = CheckpointRepository(client)

    deleted = await repo.cleanup_duplicate_checkpoints()
    assert deleted == 0


@pytest.mark.asyncio
async def test_get_checkpoint_tenant_id_default_and_custom():
    queries = []
    async def handler(query, params):
        queries.append((query, params))
        return MockAsyncResult(single_record=None)

    session = MockSession(run_handler=handler)
    client = make_client_with_session(session)
    repo = CheckpointRepository(client)

    # 1. Custom tenant_id
    await repo.get_checkpoint("jira", "stream-1", tenant_id="my-tenant")
    assert len(queries) == 1
    assert queries[0][1]["tenant_id"] == "my-tenant"
    assert "ORDER BY cp.updated_at DESC" in queries[0][0]

    # 2. None tenant_id falls back to default
    await repo.get_checkpoint("jira", "stream-2", tenant_id=None)
    assert len(queries) == 2
    assert queries[1][1]["tenant_id"] == "default"


# ==============================================================================
# 5. RawEvent Lifecycle & Advanced Mark Status Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_persist_raw_event_lifecycle_fields():
    session = MockSession()
    client = make_client_with_session(session)
    repo = RawEventRepository(client)

    now = datetime.now(timezone.utc)
    record = RawEventRecord(
        id="raw-lifecycle-1",
        tenant_id="tenant-alpha",
        source_type=SourceType.MS_TEAMS,
        external_id="msg-alpha-1",
        idempotency_key="idemp-alpha-1",
        event_timestamp=now,
        author_external_id="user-alpha",
        conversation_or_project_id="conv-alpha",
        raw_payload={"msg": "hello"},
        processing_status=ProcessingStatus.PENDING,
        processing_attempt_count=0,
        last_processing_error=None,
        next_retry_at=now,
        processor_version="v2.0-beta",
    )

    await repo.persist_raw_event(record)
    assert len(session.queries) == 1
    query, params = session.queries[0]
    assert "re.processing_attempt_count = $processing_attempt_count" in query
    assert "re.last_processing_error = $last_processing_error" in query
    assert "re.next_retry_at = $next_retry_at" in query
    assert "re.processor_version = $processor_version" in query
    assert params["processor_version"] == "v2.0-beta"
    assert params["processing_attempt_count"] == 0


@pytest.mark.asyncio
async def test_mark_event_status_retry_and_processing():
    session = MockSession()
    client = make_client_with_session(session)
    repo = RawEventRepository(client)

    retry_time = datetime(2026, 9, 29, 3, 0, 0, tzinfo=timezone.utc)

    # 1. Mark RETRY
    await repo.mark_event_status(
        "raw-100",
        ProcessingStatus.RETRY,
        error="LLM rate limited",
        next_retry_at=retry_time,
        processor_version="v1.1",
    )
    assert len(session.queries) == 1
    q1, p1 = session.queries[0]
    assert p1["status"] == "retry"
    assert p1["error"] == "LLM rate limited"
    assert p1["next_retry_at"] == retry_time.isoformat()
    assert p1["processor_version"] == "v1.1"

    # 2. Mark PROCESSING
    await repo.mark_event_status(
        "raw-100",
        ProcessingStatus.PROCESSING,
        processor_version="v1.1",
    )
    assert len(session.queries) == 2
    q2, p2 = session.queries[1]
    assert p2["status"] == "processing"


# ==============================================================================
# 6. Task Timestamps & Evidence Preserved Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_task_created_at_on_create_only():
    session = MockSession()
    client = make_client_with_session(session)
    repo = TaskDomainRepository(client)

    fixed_created = datetime(2026, 9, 25, 8, 0, 0, tzinfo=timezone.utc)
    original_ev_time = datetime(2026, 9, 25, 7, 30, 0, tzinfo=timezone.utc)

    task = UnifiedTaskCandidate(
        id="task-timestamps-test",
        title="Check timestamps",
        status=TaskStatus.TODO,
        created_at=fixed_created,
        evidences=[
            EvidenceRecord(
                id="ev-ts-1",
                task_id="task-timestamps-test",
                raw_event_id="raw-1",
                evidence_type=EvidenceType.CHAT_COMMITMENT,
                source_type="ms_teams",
                timestamp=original_ev_time,
                snippet="Original timestamp test",
            )
        ]
    )

    await repo.upsert_task_atomic(task)
    tx_queries = session.active_tx.queries
    task_q, task_p = tx_queries[0]
    ev_q, ev_p = tx_queries[1]

    # Task Cypher: ON CREATE SET t.created_at = $created_at
    assert "ON CREATE SET t.created_at = $created_at" in task_q
    assert "t.updated_at = $updated_at" in task_q
    assert task_p["created_at"] == fixed_created.isoformat()

    # Evidence Cypher: e.timestamp = ev.timestamp
    assert "e.timestamp = ev.timestamp" in ev_q
    assert ev_p["evidences"][0]["timestamp"] == original_ev_time.isoformat()


@pytest.mark.asyncio
async def test_record_merge_audit():
    async def handler(query, params):
        assert "MATCH (t:UnifiedTask {id: $winning_task_id})" in query
        assert "CREATE (a:MergeAudit" in query
        assert "CREATE (t)-[:MERGE_AUDIT]->(a)" in query
        assert params["winning_task_id"] == "task-win-001"
        assert params["candidate_task_ids"] == ["task-cand-002"]
        assert params["correlation_score"] == 0.92
        assert params["deterministic_anchors"] == ["jira:OPS-88"]
        return MockAsyncResult(single_record=MockRecord({"id": "audit-merge-1"}))

    session = MockSession(run_handler=handler)
    client = make_client_with_session(session)
    repo = TaskDomainRepository(client)

    audit = MergeAuditRecord(
        id="audit-merge-1",
        winning_task_id="task-win-001",
        candidate_task_ids=["task-cand-002"],
        correlation_score=0.92,
        deterministic_anchors=["jira:OPS-88"],
        semantic_score=0.80,
        merge_reason="Deterministic anchor matched: jira:OPS-88 -> AUTO-MERGE",
        processor_version="v1.1",
        created_at=datetime(2026, 9, 28, 14, 0, 0, tzinfo=timezone.utc),
    )

    audit_id = await repo.record_merge_audit(audit)
    assert audit_id == "audit-merge-1"


@pytest.mark.asyncio
async def test_split_task_in_repo():
    ev1 = EvidenceRecord(
        id="ev-1",
        task_id="task-orig",
        raw_event_id="raw-1",
        evidence_type=EvidenceType.CHAT_COMMITMENT,
        source_type="ms_teams",
        timestamp=datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc),
        snippet="Snippet 1",
    )
    ev2 = EvidenceRecord(
        id="ev-2",
        task_id="task-orig",
        raw_event_id="raw-2",
        evidence_type=EvidenceType.EMAIL_THREAD,
        source_type="ms_outlook",
        timestamp=datetime(2026, 9, 28, 11, 0, 0, tzinfo=timezone.utc),
        snippet="Snippet 2",
    )
    orig_task = UnifiedTaskCandidate(
        id="task-orig",
        title="Original Big Task",
        status=TaskStatus.IN_PROGRESS,
        evidences=[ev1, ev2],
    )

    session = MockSession()
    client = make_client_with_session(session)
    repo = TaskDomainRepository(client)
    repo.get_task_by_id = AsyncMock(
        side_effect=lambda tid: orig_task if tid == "task-orig" else None
    )

    new_task = await repo.split_task(
        original_task_id="task-orig",
        evidence_ids_to_detach=["ev-2"],
        new_task_title="Split Task 2",
    )

    assert new_task.title == "Split Task 2"
    assert len(new_task.evidences) == 1
    assert new_task.evidences[0].id == "ev-2"
    assert new_task.evidences[0].raw_event_id == "raw-2"
    assert new_task.evidences[0].source_type == "ms_outlook"

    # Verify Cypher execution in transaction
    assert session.active_tx is not None
    assert session.active_tx.committed is True
    q, p = session.active_tx.queries[0]
    assert "MATCH (orig:UnifiedTask {id: $original_task_id})" in q
    assert "DELETE r" in q
    assert "MERGE (new_t)-[:HAS_EVIDENCE]->(e)" in q
    assert p["original_task_id"] == "task-orig"
    assert p["evidence_ids_to_detach"] == ["ev-2"]


