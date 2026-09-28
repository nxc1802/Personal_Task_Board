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

