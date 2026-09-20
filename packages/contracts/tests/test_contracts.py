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
    assert task.status == TaskStatus.OPEN
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
