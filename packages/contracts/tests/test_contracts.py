import json
from pathlib import Path
import pytest

from ptb_contracts import (
    RawEventRecord,
    UnifiedTaskCandidate,
    TaskWithContext,
    TodayBoardView,
    ProcessingStatus,
    SourceType,
    TaskStatus,
)
from ptb_contracts.mocks import create_mock_raw_event, create_mock_unified_task, MOCKS_DIR



def test_load_l1_raw_teams_message():
    with open(MOCKS_DIR / "l1_raw_teams_message.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    record = RawEventRecord.model_validate(data)
    assert record.id == "raw-908-teams-fpt"
    assert record.source_type == SourceType.MS_TEAMS
    assert record.processing_status == ProcessingStatus.PENDING
    assert "Huy" in record.raw_payload["body"]["content"]


def test_load_l1_raw_jira_issue():
    with open(MOCKS_DIR / "l1_raw_jira_issue.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    record = RawEventRecord.model_validate(data)
    assert record.external_id == "OPS-88"
    assert record.source_type == SourceType.JIRA


def test_load_l1_raw_shortcut_story():
    with open(MOCKS_DIR / "l1_raw_shortcut_story.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    record = RawEventRecord.model_validate(data)
    assert record.source_type == SourceType.SHORTCUT
    assert record.external_id == "story-1204"


def test_load_l2_extracted_candidates():
    with open(MOCKS_DIR / "l2_extracted_candidates.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    candidates = [UnifiedTaskCandidate.model_validate(item) for item in data]
    assert len(candidates) == 1
    task = candidates[0]
    assert task.status == TaskStatus.TODO
    assert len(task.evidences) == 2
    assert task.evidences[0].confidence == 0.96


def test_load_l3_tasks_with_context():
    with open(MOCKS_DIR / "l3_tasks_with_context.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    tasks_with_context = [TaskWithContext.model_validate(item) for item in data]
    assert len(tasks_with_context) == 1
    twc = tasks_with_context[0]
    assert len(twc.relations) == 3
    assert twc.relations[0].relation_type == "OWNS"


def test_load_l4_today_board_view():
    with open(MOCKS_DIR / "l4_today_board_view.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    board = TodayBoardView.model_validate(data)
    assert len(board.top_tasks) == 1
    assert board.top_tasks[0].priority.total_score == 92.5
    assert len(board.waiting_on_others) == 1
    assert len(board.forgotten_commitments) == 1


def test_mock_generator():
    event = create_mock_raw_event(content="Hello world")
    assert event.idempotency_key is not None
    assert event.processing_status == ProcessingStatus.PENDING

    task = create_mock_unified_task(title="Fix bug")
    assert task.title == "Fix bug"
    assert len(task.evidences) == 1


def test_raw_agent_session_record():
    from datetime import datetime, timezone
    from ptb_contracts import RawAgentSessionRecord, AgentType
    import hashlib

    session_id = "sess-123"
    turn_index = 1
    role = "user"
    key = hashlib.sha256(f"{session_id}:{turn_index}:{role}".encode("utf-8")).hexdigest()

    record = RawAgentSessionRecord(
        session_id=session_id,
        agent_type=AgentType.CURSOR,
        workspace_path="/workspace/project",
        turn_index=turn_index,
        message_role=role,
        content="Refactor authentication layer",
        tool_invocations=[{"tool": "run_command", "args": {"cmd": "ls"}}],
        timestamp=datetime.now(timezone.utc),
        idempotency_key=key,
    )
    assert record.agent_type == AgentType.CURSOR
    assert record.turn_index == 1
    assert len(record.tool_invocations) == 1


def test_canonical_single_store_models():
    from datetime import datetime, timezone
    from ptb_contracts import (
        IngestionCheckpointRecord,
        ProcessingAttemptRecord,
        StatusTransitionAuditRecord,
        CommitmentRecord,
        UnifiedTaskRecord,
        EvidenceNodeRecord,
        TaskStatus,
        SourceType,
        ProcessingStatus,
    )

    cp = IngestionCheckpointRecord(
        id="cp-01",
        source_type=SourceType.MS_TEAMS_WEB,
        stream_id="channel-devops",
        tenant_id="tenant-fpt-internal",
        last_external_id="msg-100",
        last_event_timestamp=datetime.now(timezone.utc),
    )
    assert cp.stream_id == "channel-devops"

    attempt = ProcessingAttemptRecord(
        id="att-01",
        raw_event_id="raw-01",
        attempt_number=1,
        status=ProcessingStatus.PROCESSED,
    )
    assert attempt.status == ProcessingStatus.PROCESSED

    audit = StatusTransitionAuditRecord(
        id="aud-01",
        task_id="task-01",
        old_status=TaskStatus.TODO,
        new_status=TaskStatus.IN_PROGRESS,
        reason="Evidence from Teams chat indicates active development",
        source_evidence_ids=["ev-01"],
        confidence=0.9,
    )
    assert audit.change_actor == "SYSTEM"

    task = UnifiedTaskRecord(
        id="task-01",
        title="Refactor single-store Neo4j",
        status=TaskStatus.TODO,
    )
    assert task.status == TaskStatus.TODO

    comm = CommitmentRecord(
        id="comm-01",
        title="I will deliver this today",
        owner_id="person-01",
    )
    assert comm.status == "ACTIVE"


def test_raw_event_record_lifecycle_fields():
    from datetime import datetime, timezone
    from ptb_contracts import RawEventRecord, SourceType, ProcessingStatus

    now = datetime.now(timezone.utc)
    ev = RawEventRecord(
        id="raw-lifecycle-test",
        tenant_id="tenant-test",
        source_type=SourceType.MS_TEAMS,
        external_id="msg-test-1",
        idempotency_key="idemp-test-1",
        event_timestamp=now,
        author_external_id="user-test",
        conversation_or_project_id="conv-test",
        raw_payload={"body": "test"},
    )
    assert ev.processing_status == ProcessingStatus.PENDING
    assert ev.processing_attempt_count == 0
    assert ev.last_processing_error is None
    assert ev.next_retry_at is None
    assert ev.processed_at is None
    assert ev.processor_version is None

    # Test with retry and error fields
    retry_ev = RawEventRecord(
        id="raw-retry-test",
        tenant_id="tenant-test",
        source_type=SourceType.JIRA,
        external_id="jira-123",
        idempotency_key="idemp-jira-123",
        event_timestamp=now,
        author_external_id="user-jira",
        conversation_or_project_id="PROJ",
        raw_payload={},
        processing_status=ProcessingStatus.RETRY,
        processing_attempt_count=2,
        last_processing_error="API rate limit exceeded",
        next_retry_at=now,
        processor_version="v1.1",
    )
    assert retry_ev.processing_status == ProcessingStatus.RETRY
    assert retry_ev.processing_attempt_count == 2
    assert retry_ev.last_processing_error == "API rate limit exceeded"
    assert retry_ev.processor_version == "v1.1"


def test_canonical_person_record_canonical_id():
    from ptb_contracts import CanonicalPersonRecord

    # 1. Initialize with canonical_id
    p1 = CanonicalPersonRecord(
        canonical_id="person-cuong-01",
        canonical_name="Cuong Nguyen",
        primary_email="cuong@example.com",
    )
    assert p1.canonical_id == "person-cuong-01"
    assert p1.id == "person-cuong-01"

    # 2. Initialize with legacy id
    p2 = CanonicalPersonRecord(
        id="person-legacy-02",
        canonical_name="Legacy Person",
        primary_email="legacy@example.com",
    )
    assert p2.canonical_id == "person-legacy-02"
    assert p2.id == "person-legacy-02"


# ==============================================================================
# Wave 1D: Vocabulary & Schema Consistency Tests (docs/v1_2.md)
# ==============================================================================


def test_task_status_five_uppercase_states():
    """Chuẩn hóa 5 trạng thái uppercase: TODO, IN_PROGRESS, BLOCKED, DONE, DISMISSED."""
    from ptb_contracts import TaskStatus

    expected = {"TODO", "IN_PROGRESS", "BLOCKED", "DONE", "DISMISSED"}
    actual_names = {s.name for s in TaskStatus if s.name.isupper()}
    assert actual_names == expected

    for status_str in expected:
        ts = TaskStatus(status_str)
        assert ts.value == status_str
        assert ts.value.isupper()

    # Backwards compatibility aliases
    assert TaskStatus("open") == TaskStatus.TODO
    assert TaskStatus("in_progress") == TaskStatus.IN_PROGRESS
    assert TaskStatus("blocked") == TaskStatus.BLOCKED
    assert TaskStatus("done") == TaskStatus.DONE
    assert TaskStatus("dismissed") == TaskStatus.DISMISSED


def test_merge_audit_record_canonical_vocabulary():
    """MergeAuditRecord / MergeAudit:
    candidate_task_ids, winning_task_id, correlation_score,
    deterministic_anchors, merge_reason, merged_at."""
    from datetime import datetime, timezone
    from ptb_contracts import MergeAuditRecord, MergeAudit

    now = datetime.now(timezone.utc)
    audit = MergeAuditRecord(
        candidate_task_ids=["task-10", "task-20"],
        winning_task_id="task-10",
        correlation_score=0.92,
        deterministic_anchors=["jira:PROJ-55"],
        merge_reason="Exact Jira issue anchor match",
        merged_at=now,
    )
    assert audit.candidate_task_ids == ["task-10", "task-20"]
    assert audit.winning_task_id == "task-10"
    assert audit.correlation_score == 0.92
    assert audit.deterministic_anchors == ["jira:PROJ-55"]
    assert audit.merge_reason == "Exact Jira issue anchor match"
    assert audit.merged_at == now
    assert audit.created_at == now  # Synced
    assert audit.id is not None  # Auto-generated UUID

    # Test alias MergeAudit
    assert MergeAudit is MergeAuditRecord

    # Test created_at -> merged_at sync
    audit2 = MergeAudit(
        candidate_task_ids=["task-30"],
        winning_task_id="task-10",
        created_at=now,
    )
    assert audit2.merged_at == now


def test_status_transition_audit_record_canonical_vocabulary():
    """StatusTransitionAuditRecord:
    id, task_id, old_status, new_status, change_actor, timestamp, reason."""
    from datetime import datetime, timezone
    from ptb_contracts import StatusTransitionAuditRecord, StatusTransitionAudit, TaskStatus

    now = datetime.now(timezone.utc)
    audit = StatusTransitionAuditRecord(
        id="aud-st-01",
        task_id="task-99",
        old_status=TaskStatus.TODO,
        new_status=TaskStatus.IN_PROGRESS,
        change_actor="USER",
        timestamp=now,
        reason="User moved card to in-progress",
    )
    assert audit.id == "aud-st-01"
    assert audit.task_id == "task-99"
    assert audit.old_status == TaskStatus.TODO
    assert audit.new_status == TaskStatus.IN_PROGRESS
    assert audit.change_actor == "USER"
    assert audit.timestamp == now
    assert audit.changed_at == now  # Synced
    assert audit.reason == "User moved card to in-progress"

    # Test alias StatusTransitionAudit
    assert StatusTransitionAudit is StatusTransitionAuditRecord

    # Test changed_at -> timestamp sync
    audit2 = StatusTransitionAudit(
        task_id="task-99",
        old_status=TaskStatus.IN_PROGRESS,
        new_status=TaskStatus.DONE,
        reason="Finished",
        changed_at=now,
    )
    assert audit2.timestamp == now
    assert audit2.change_actor == "SYSTEM"  # Default


def test_evidence_record_canonical_vocabulary():
    """EvidenceRecord / Evidence:
    id, task_id, evidence_type, snippet, source_type, source_event_id, timestamp, confidence_score."""
    from datetime import datetime, timezone
    from ptb_contracts import EvidenceRecord, Evidence, EvidenceType

    now = datetime.now(timezone.utc)
    ev = EvidenceRecord(
        id="ev-spec-01",
        task_id="task-01",
        evidence_type=EvidenceType.CHAT_COMMITMENT,
        snippet="Tôi sẽ xử lý task này trước chiều nay.",
        source_type="ms_teams",
        source_event_id="raw-msg-456",
        timestamp=now,
        confidence_score=0.95,
    )
    assert ev.id == "ev-spec-01"
    assert ev.task_id == "task-01"
    assert ev.evidence_type == EvidenceType.CHAT_COMMITMENT
    assert ev.snippet == "Tôi sẽ xử lý task này trước chiều nay."
    assert ev.source_type == "ms_teams"
    assert ev.source_event_id == "raw-msg-456"
    assert ev.raw_event_id == "raw-msg-456"  # Synced alias
    assert ev.timestamp == now
    assert ev.confidence_score == 0.95
    assert ev.confidence == 0.95  # Synced alias

    # Test alias Evidence
    assert Evidence is EvidenceRecord

    # Test raw_event_id & confidence -> source_event_id & confidence_score sync
    ev2 = Evidence(
        evidence_type=EvidenceType.JIRA_TICKET,
        snippet="Jira ticket created",
        source_type="jira",
        raw_event_id="raw-jira-789",
        timestamp=now,
        confidence=0.88,
    )
    assert ev2.source_event_id == "raw-jira-789"
    assert ev2.confidence_score == 0.88


def test_ingestion_checkpoint_record_canonical_vocabulary():
    """IngestionCheckpointRecord / IngestionCheckpoint:
    tenant_id, source_type, stream_id, last_external_id, last_event_timestamp, cursor_token, updated_at."""
    from datetime import datetime, timezone
    from ptb_contracts import IngestionCheckpointRecord, IngestionCheckpoint, SourceType

    now = datetime.now(timezone.utc)
    cp = IngestionCheckpointRecord(
        tenant_id="tenant-acme",
        source_type=SourceType.MS_TEAMS,
        stream_id="channel-dev",
        last_external_id="ext-999",
        last_event_timestamp=now,
        cursor_token="cursor-tok-123",
        updated_at=now,
    )
    assert cp.tenant_id == "tenant-acme"
    assert cp.source_type == SourceType.MS_TEAMS
    assert cp.stream_id == "channel-dev"
    assert cp.last_external_id == "ext-999"
    assert cp.last_event_timestamp == now
    assert cp.cursor_token == "cursor-tok-123"
    assert cp.updated_at == now
    assert cp.id == "tenant-acme:ms_teams:channel-dev"  # Deterministic composite ID

    # Test alias IngestionCheckpoint
    assert IngestionCheckpoint is IngestionCheckpointRecord


def test_typescript_types_compatibility_and_no_outbox():
    """Xác nhận packages/contracts/src/types.ts không còn bất kỳ kiểu dữ liệu lỗi thời (như outbox),
    và đồng bộ toàn bộ vocabulary."""
    ts_file = Path(__file__).parent.parent / "src" / "types.ts"
    assert ts_file.exists(), f"TypeScript definitions file not found: {ts_file}"

    content = ts_file.read_text(encoding="utf-8")

    # 1. Kiểm tra không còn outbox
    assert "outbox" not in content.lower(), "types.ts must not contain outbox"

    # 2. Kiểm tra Person.canonical_id
    assert "canonical_id: string;" in content
    assert "export type Person = CanonicalPersonRecord;" in content

    # 3. Kiểm tra TaskStatus 5 uppercase
    assert 'export type TaskStatus = "TODO" | "IN_PROGRESS" | "BLOCKED" | "DONE" | "DISMISSED";' in content

    # 4. Kiểm tra MergeAuditRecord
    assert "export interface MergeAuditRecord" in content
    assert "candidate_task_ids: string[];" in content
    assert "winning_task_id: string;" in content
    assert "correlation_score: number;" in content
    assert "deterministic_anchors: string[];" in content
    assert "merge_reason: string;" in content
    assert "merged_at: string;" in content
    assert "export type MergeAudit = MergeAuditRecord;" in content

    # 5. Kiểm tra StatusTransitionAuditRecord
    assert "export interface StatusTransitionAuditRecord" in content
    assert "task_id: string;" in content
    assert "old_status: TaskStatus;" in content
    assert "new_status: TaskStatus;" in content
    assert "change_actor: string;" in content
    assert "timestamp: string;" in content
    assert "reason: string;" in content
    assert "export type StatusTransitionAudit = StatusTransitionAuditRecord;" in content

    # 6. Kiểm tra EvidenceRecord
    assert "export interface EvidenceRecord" in content
    assert "source_event_id?: string | null;" in content
    assert "confidence_score: number;" in content
    assert "export type Evidence = EvidenceRecord;" in content

    # 7. Kiểm tra IngestionCheckpointRecord
    assert "export interface IngestionCheckpointRecord" in content
    assert "tenant_id: string;" in content
    assert "stream_id: string;" in content
    assert "last_external_id?: string | null;" in content
    assert "last_event_timestamp?: string | null;" in content
    assert "cursor_token?: string | null;" in content
    assert "updated_at?: string | null;" in content
    assert "export type IngestionCheckpoint = IngestionCheckpointRecord;" in content


