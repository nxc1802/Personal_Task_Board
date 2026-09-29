"""Unit tests for Phase R6: Correlation Audit Trail and Split-Task Engine with 100% Provenance Preservation."""

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest

from ptb_application.service import ApplicationService
from ptb_contracts.l2_processing import (
    EvidenceRecord,
    EvidenceType,
    MergeAuditRecord,
    TaskStatus,
    UnifiedTaskCandidate,
)
from ptb_database.neo4j_client import Neo4jClient
from ptb_database.repositories.task_repo import TaskDomainRepository
from ptb_processing.correlation.candidate_matcher import TaskCandidateMatcher
from ptb_processing.correlation.merger import TaskMerger
from ptb_processing.correlation.scoring import (
    CorrelationScoreResult,
    CorrelationScorer,
)


# ==============================================================================
# Neo4j Mock Helpers for Repository Testing
# ==============================================================================

class MockRecord:
    def __init__(self, data: Dict[str, Any]):
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)


class MockAsyncResult:
    def __init__(
        self,
        records: Optional[List[MockRecord]] = None,
        single_record: Optional[MockRecord] = None,
    ):
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
# Test Fixtures
# ==============================================================================

@pytest.fixture
def sample_target_task() -> UnifiedTaskCandidate:
    return UnifiedTaskCandidate(
        id="task-target-001",
        title="Investigate deployment failure on staging",
        description="Kiểm tra nguyên nhân deployment bị lỗi trên Teams",
        status=TaskStatus.TODO,
        owner_canonical_id="person-cuong",
        owner_name="Dam Quang Cuong",
        project_key="OPS",
        due_date=datetime(2026, 9, 18, 17, 0, 0, tzinfo=timezone.utc),
        explicit_deadline=False,
        priority_score=50.0,
        extraction_confidence=0.85,
        created_at=datetime(2026, 9, 17, 10, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 9, 17, 10, 0, 0, tzinfo=timezone.utc),
        evidences=[
            EvidenceRecord(
                id="ev-001",
                task_id="task-target-001",
                raw_event_id="raw-908-teams-fpt",
                evidence_type=EvidenceType.CHAT_COMMITMENT,
                source_type="ms_teams",
                external_url="https://teams.microsoft.com/l/message/19:channel-devops-alerts/msg-123",
                author_canonical_id="person-cuong",
                timestamp=datetime(2026, 9, 17, 14, 18, 0, tzinfo=timezone.utc),
                snippet="Để em check nhé.",
                confidence=0.95,
            )
        ],
    )


@pytest.fixture
def sample_candidate_with_anchor() -> UnifiedTaskCandidate:
    return UnifiedTaskCandidate(
        id="cand-jira-001",
        title="Fix staging deployment crash [OPS-88]",
        description="Jira ticket OPS-88 created for deployment crash",
        status=TaskStatus.TODO,
        owner_canonical_id="person-cuong",
        owner_name="Dam Quang Cuong",
        project_key="OPS",
        due_date=datetime(2026, 9, 20, 18, 0, 0, tzinfo=timezone.utc),
        explicit_deadline=True,
        priority_score=75.0,
        extraction_confidence=0.90,
        evidences=[
            EvidenceRecord(
                id="ev-002",
                task_id="cand-jira-001",
                raw_event_id="raw-88-jira-ops",
                evidence_type=EvidenceType.JIRA_TICKET,
                source_type="jira",
                external_url="https://fpt-corp.atlassian.net/browse/OPS-88",
                author_canonical_id="person-huy",
                author_canonical_name="Nguyen Van Huy",
                timestamp=datetime(2026, 9, 17, 14, 0, 0, tzinfo=timezone.utc),
                snippet="Deployment failed on staging environment OPS-88",
                confidence=1.0,
            )
        ],
    )


# ==============================================================================
# Task 1: Correlation Wiring & Audit Trail Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_merge_task_generates_full_audit_trail(sample_target_task, sample_candidate_with_anchor):
    """Test gộp task sinh đầy đủ audit trail:
    - candidate_task_ids
    - winning_task_id
    - correlation_score
    - deterministic_anchors
    - merge_reason
    - merge_audit
    """
    matcher = TaskCandidateMatcher()
    scorer = CorrelationScorer()

    cand_anchors = matcher.extract_anchors(sample_candidate_with_anchor)
    target_anchors = matcher.extract_anchors(sample_target_task)
    matching_anchors = sorted([f"{a.anchor_type}:{a.value}" for a in (cand_anchors & target_anchors)])

    # Jira OPS-88 appears in candidate, let's also give OPS-88 to target description
    sample_target_task.description = "Fix staging deployment OPS-88"
    target_anchors = matcher.extract_anchors(sample_target_task)
    matching_anchors = sorted([f"{a.anchor_type}:{a.value}" for a in (cand_anchors & target_anchors)])

    score_result = scorer.score(
        candidate=sample_candidate_with_anchor,
        target_task=sample_target_task,
        matching_anchors=matching_anchors,
    )

    merger = TaskMerger()
    merged = await merger.merge(
        target_task=sample_target_task,
        candidate=sample_candidate_with_anchor,
        score_result=score_result,
        persist=False,
    )

    # 1. Winning task ID is target task ID
    assert merged.winning_task_id == sample_target_task.id
    assert merged.id == sample_target_task.id

    # 2. Candidate task IDs contains candidate id
    assert sample_candidate_with_anchor.id in merged.candidate_task_ids

    # 3. Correlation score is recorded
    assert merged.correlation_score is not None
    assert merged.correlation_score >= 0.55
    assert merged.correlation_confidence == merged.correlation_score

    # 4. Deterministic anchors recorded
    assert "jira:OPS-88" in merged.deterministic_anchors

    # 5. Merge reason detailed
    assert "OPS-88" in merged.merge_reason
    assert "AUTO-MERGE" in merged.merge_reason

    # 6. Full MergeAuditRecord attached
    assert merged.merge_audit is not None
    assert isinstance(merged.merge_audit, MergeAuditRecord)
    assert merged.merge_audit.winning_task_id == sample_target_task.id
    assert sample_candidate_with_anchor.id in merged.merge_audit.candidate_task_ids
    assert merged.merge_audit.correlation_score == merged.correlation_score
    assert merged.merge_audit.deterministic_anchors == merged.deterministic_anchors
    assert merged.merge_audit.processor_version == "v1.1"
    assert merged.merge_audit.created_at is not None

    # 7. Merger keeps last_audit
    assert merger.last_audit == merged.merge_audit


@pytest.mark.asyncio
async def test_merge_task_with_pr_and_shortcut_anchors():
    """Test merge audit trail captures PR URL and Shortcut story anchors."""
    target = UnifiedTaskCandidate(
        id="target-sc-01",
        title="Refactor auth service story-1204",
        status=TaskStatus.IN_PROGRESS,
        evidences=[],
    )
    candidate = UnifiedTaskCandidate(
        id="cand-sc-02",
        title="Auth refactor PR merged",
        description="Merged https://github.com/my-org/my-repo/pull/42 for story-1204",
        status=TaskStatus.DONE,
        evidences=[],
    )

    matcher = TaskCandidateMatcher()
    scorer = CorrelationScorer()

    c_anchors = matcher.extract_anchors(candidate)
    t_anchors = matcher.extract_anchors(target)
    common = sorted([f"{a.anchor_type}:{a.value}" for a in (c_anchors & t_anchors)])

    score_result = scorer.score(
        candidate=candidate,
        target_task=target,
        matching_anchors=common,
    )

    merger = TaskMerger()
    merged = await merger.merge(target_task=target, candidate=candidate, score_result=score_result, persist=False)

    assert merged.winning_task_id == target.id
    assert "shortcut:story-1204" in merged.deterministic_anchors
    assert merged.correlation_score >= 0.55
    assert merged.merge_audit.processor_version == "v1.1"


@pytest.mark.asyncio
async def test_merge_pure_semantic_audit_trail(sample_target_task):
    """Test merge without anchors captures pure semantic similarity audit."""
    candidate = UnifiedTaskCandidate(
        id="cand-sem-001",
        title="Investigate deployment failure on staging env",
        description="Looking into staging deploy crash",
        status=TaskStatus.TODO,
        evidences=[],
    )

    scorer = CorrelationScorer()
    score_result = scorer.score(candidate=candidate, target_task=sample_target_task, matching_anchors=[])

    merger = TaskMerger()
    merged = await merger.merge(
        target_task=sample_target_task,
        candidate=candidate,
        score_result=score_result,
        persist=False,
    )

    assert merged.deterministic_anchors == []
    assert merged.correlation_score == score_result.correlation_confidence
    assert merged.merge_audit.semantic_score == score_result.semantic_similarity
    assert "Pure semantic" in merged.merge_reason


@pytest.mark.asyncio
async def test_merge_persists_merge_audit_to_repository(sample_target_task, sample_candidate_with_anchor):
    """Test merger calls repository.record_merge_audit when available."""
    mock_repo = AsyncMock()
    mock_repo.upsert_task_atomic = AsyncMock(return_value=sample_target_task.id)
    mock_repo.record_merge_audit = AsyncMock(return_value="merge-audit-id-123")

    merger = TaskMerger(task_repo=mock_repo)
    merged = await merger.merge(
        target_task=sample_target_task,
        candidate=sample_candidate_with_anchor,
        persist=True,
    )

    assert mock_repo.upsert_task_atomic.called
    assert mock_repo.record_merge_audit.called

    audit_arg = mock_repo.record_merge_audit.call_args[0][0]
    assert isinstance(audit_arg, MergeAuditRecord)
    assert audit_arg.winning_task_id == sample_target_task.id
    assert sample_candidate_with_anchor.id in audit_arg.candidate_task_ids


# ==============================================================================
# Task 2: Split-Task Engine & 100% Provenance Preservation Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_split_task_detaches_evidence_and_preserves_provenance():
    """Test split task:
    1. Tách đúng các Evidence chỉ định khỏi original_task_id.
    2. Tạo một UnifiedTask mới với title chỉ định.
    3. Gắn các Evidence đã detach vào Task mới.
    4. BẢO TOÀN 100% PROVENANCE: Mỗi Evidence giữ nguyên raw_event_id, source_type, snippet.
    5. Cập nhật updated_at cho cả 2 tasks.
    """
    ev1 = EvidenceRecord(
        id="ev-teams-001",
        task_id="orig-task-100",
        raw_event_id="raw-event-teams-111",
        evidence_type=EvidenceType.CHAT_COMMITMENT,
        source_type="ms_teams",
        timestamp=datetime(2026, 9, 20, 9, 0, 0, tzinfo=timezone.utc),
        snippet="Em sẽ sửa lỗi staging.",
        confidence=0.95,
    )
    ev2 = EvidenceRecord(
        id="ev-outlook-002",
        task_id="orig-task-100",
        raw_event_id="raw-event-outlook-222",
        evidence_type=EvidenceType.EMAIL_THREAD,
        source_type="ms_outlook",
        timestamp=datetime(2026, 9, 20, 10, 0, 0, tzinfo=timezone.utc),
        snippet="Customer requested follow-up on error log.",
        confidence=0.90,
    )
    ev3 = EvidenceRecord(
        id="ev-jira-003",
        task_id="orig-task-100",
        raw_event_id="raw-event-jira-333",
        evidence_type=EvidenceType.JIRA_TICKET,
        source_type="jira",
        timestamp=datetime(2026, 9, 20, 11, 0, 0, tzinfo=timezone.utc),
        snippet="Jira ticket OPS-99 for log analysis",
        confidence=1.0,
    )

    original_task = UnifiedTaskCandidate(
        id="orig-task-100",
        title="Comprehensive staging fixes",
        description="Handle all staging issues",
        status=TaskStatus.IN_PROGRESS,
        owner_canonical_id="person-cuong",
        owner_name="Dam Quang Cuong",
        project_key="OPS",
        due_date=datetime(2026, 9, 25, 17, 0, 0, tzinfo=timezone.utc),
        explicit_deadline=True,
        priority_score=60.0,
        extraction_confidence=0.9,
        created_at=datetime(2026, 9, 20, 8, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 9, 20, 8, 0, 0, tzinfo=timezone.utc),
        evidences=[ev1, ev2, ev3],
    )

    # Set up mock repository with original task
    task_storage = {"orig-task-100": original_task}

    class MockTaskRepo:
        def __init__(self):
            self.tasks = task_storage

        async def get_task_by_id(self, tid: str):
            return self.tasks.get(tid)

    session = MockSession()
    client = make_client_with_session(session)
    repo = TaskDomainRepository(client)
    # Monkey-patch get_task_by_id on repo to return our memory fixtures
    repo.get_task_by_id = AsyncMock(side_effect=lambda tid: task_storage.get(tid))

    # Split ev2 and ev3 into a new task
    evidence_ids_to_detach = ["ev-outlook-002", "ev-jira-003"]
    new_task = await repo.split_task(
        original_task_id="orig-task-100",
        evidence_ids_to_detach=evidence_ids_to_detach,
        new_task_title="Customer log analysis follow-up",
    )

    # 1. New task ID is distinct UUID
    assert new_task.id != original_task.id
    assert new_task.title == "Customer log analysis follow-up"

    # 2. Exactly the 2 detached evidences belong to new task
    assert len(new_task.evidences) == 2
    detached_ids = {e.id for e in new_task.evidences}
    assert detached_ids == {"ev-outlook-002", "ev-jira-003"}

    # 3. 100% PROVENANCE PRESERVATION:
    # Each detached Evidence strictly preserves its raw_event_id and source_type
    ev_out = next(e for e in new_task.evidences if e.id == "ev-outlook-002")
    assert ev_out.raw_event_id == "raw-event-outlook-222"
    assert ev_out.source_type == "ms_outlook"
    assert ev_out.snippet == "Customer requested follow-up on error log."
    assert ev_out.task_id == new_task.id

    ev_j = next(e for e in new_task.evidences if e.id == "ev-jira-003")
    assert ev_j.raw_event_id == "raw-event-jira-333"
    assert ev_j.source_type == "jira"
    assert ev_j.snippet == "Jira ticket OPS-99 for log analysis"
    assert ev_j.task_id == new_task.id

    # 4. updated_at is refreshed
    assert new_task.updated_at is not None
    assert new_task.created_at is not None

    # 5. Check executed Cypher queries in transaction:
    assert session.active_tx is not None
    assert session.active_tx.committed is True
    split_cypher, split_params = session.active_tx.queries[0]

    assert "MATCH (orig:UnifiedTask {id: $original_task_id})" in split_cypher
    assert "DELETE r" in split_cypher
    assert "MERGE (new_t)-[:HAS_EVIDENCE]->(e)" in split_cypher
    assert split_params["original_task_id"] == "orig-task-100"
    assert split_params["new_task_id"] == new_task.id
    assert split_params["evidence_ids_to_detach"] == evidence_ids_to_detach


@pytest.mark.asyncio
async def test_split_task_default_title_generation(sample_target_task):
    """Test split task generates 'Split: <original_title>' when new_title is omitted."""
    session = MockSession()
    client = make_client_with_session(session)
    repo = TaskDomainRepository(client)
    repo.get_task_by_id = AsyncMock(
        side_effect=lambda tid: sample_target_task if tid == sample_target_task.id else None
    )

    new_task = await repo.split_task(
        original_task_id=sample_target_task.id,
        evidence_ids_to_detach=["ev-001"],
        new_task_title=None,
    )

    assert new_task.title == f"Split: {sample_target_task.title}"
    assert len(new_task.evidences) == 1
    assert new_task.evidences[0].id == "ev-001"
    assert new_task.evidences[0].raw_event_id == "raw-908-teams-fpt"


@pytest.mark.asyncio
async def test_split_task_validation_errors(sample_target_task):
    """Test split task raises appropriate ValueError on invalid inputs."""
    session = MockSession()
    client = make_client_with_session(session)
    repo = TaskDomainRepository(client)

    # 1. Non-existent task
    repo.get_task_by_id = AsyncMock(return_value=None)
    with pytest.raises(ValueError, match="not found"):
        await repo.split_task("non-existent-task", ["ev-001"])

    # 2. None of the evidence IDs belong to original task
    repo.get_task_by_id = AsyncMock(return_value=sample_target_task)
    with pytest.raises(ValueError, match="None of the specified evidence IDs"):
        await repo.split_task(sample_target_task.id, ["non-existent-evidence-id"])


@pytest.mark.asyncio
async def test_application_service_split_task_delegation(sample_target_task):
    """Test ApplicationService.split_task delegates to TaskDomainRepository.split_task."""
    mock_task_repo = AsyncMock()
    split_result = UnifiedTaskCandidate(
        id="new-split-task-uuid",
        title="Split: Detached work",
        status=TaskStatus.TODO,
        evidences=[sample_target_task.evidences[0]],
    )
    mock_task_repo.split_task = AsyncMock(return_value=split_result)

    app_service = ApplicationService(task_repo=mock_task_repo)

    result = await app_service.split_task(
        task_id=sample_target_task.id,
        evidence_ids=["ev-001"],
        new_title="Split: Detached work",
    )

    assert result.id == "new-split-task-uuid"
    assert result.title == "Split: Detached work"
    mock_task_repo.split_task.assert_called_once_with(
        original_task_id=sample_target_task.id,
        evidence_ids_to_detach=["ev-001"],
        new_task_title="Split: Detached work",
    )
