"""Unit tests for Layer 2 Correlation Engine: candidate matcher, scoring, and auto-merge."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock
import pytest

from ptb_contracts.l2_processing import (
    EvidenceRecord,
    EvidenceType,
    TaskStatus,
    UnifiedTaskCandidate,
)
from ptb_processing.correlation.candidate_matcher import (
    DeterministicAnchor,
    TaskCandidateMatcher,
)
from ptb_processing.correlation.merger import TaskMerger
from ptb_processing.correlation.scoring import (
    ANCHOR_MERGE_THRESHOLD,
    SEMANTIC_MERGE_THRESHOLD,
    CorrelationScorer,
)


@pytest.fixture
def correlation_scorer() -> CorrelationScorer:
    return CorrelationScorer()


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


# ------------------------------------------------------------------------------
# 1. Deterministic Anchor Extraction Tests
# ------------------------------------------------------------------------------

def test_extract_jira_anchor_from_candidate():
    matcher = TaskCandidateMatcher()
    candidate = UnifiedTaskCandidate(
        id="cand-001",
        title="Resolve OPS-88 staging issue",
        description="Fix the staging bug linked to PROJ-123",
        evidences=[
            EvidenceRecord(
                id="ev-jira",
                raw_event_id="raw-88-jira-ops",
                evidence_type=EvidenceType.JIRA_TICKET,
                source_type="jira",
                external_url="https://fpt-corp.atlassian.net/browse/OPS-88",
                timestamp=datetime(2026, 9, 17, 14, 0, 0, tzinfo=timezone.utc),
                snippet="Deployment failed on staging environment",
            )
        ],
    )
    anchors = matcher.extract_anchors(candidate)
    anchor_values = {a.value for a in anchors if a.anchor_type == "jira"}
    assert "OPS-88" in anchor_values
    assert "PROJ-123" in anchor_values


def test_extract_shortcut_and_pr_anchors():
    matcher = TaskCandidateMatcher()
    candidate = UnifiedTaskCandidate(
        id="cand-002",
        title="Refactor auth token refresh handler (story-1204)",
        description="Merged PR https://github.com/org/repo/pull/42 with commit abcdef1234567890",
        evidences=[],
    )
    anchors = matcher.extract_anchors(candidate)
    sc_anchors = {a.value for a in anchors if a.anchor_type == "shortcut"}
    pr_anchors = {a.value for a in anchors if a.anchor_type == "pr"}
    commit_anchors = {a.value for a in anchors if a.anchor_type == "commit"}

    assert "story-1204" in sc_anchors
    assert "github:org/repo:42" in pr_anchors
    assert "abcdef12" in commit_anchors


def test_anchor_conflict_detection():
    anchors_cand = {DeterministicAnchor(anchor_type="jira", value="OPS-88")}
    anchors_target = {DeterministicAnchor(anchor_type="jira", value="OPS-89")}
    assert TaskCandidateMatcher.check_anchor_conflict(anchors_cand, anchors_target) is True

    # Same anchor -> No conflict
    anchors_same = {DeterministicAnchor(anchor_type="jira", value="OPS-88")}
    assert TaskCandidateMatcher.check_anchor_conflict(anchors_cand, anchors_same) is False


# ------------------------------------------------------------------------------
# 2. CorrelationScorer Tests (Jira Anchor >= 0.55 vs Semantic >= 0.70)
# ------------------------------------------------------------------------------

def test_matching_with_jira_anchor_ops_88_confidence(sample_target_task, correlation_scorer):
    """Test matching với Jira anchor (OPS-88) có correlation_confidence >= 0.55 -> AUTO-MERGE."""
    # Target task references OPS-88 in description/evidence
    target = sample_target_task.model_copy(deep=True)
    target.description = "Deployment failure linked to Jira OPS-88"

    # Candidate has different title ("Fix CI deployment") but shares Jira OPS-88 anchor
    candidate = UnifiedTaskCandidate(
        id="cand-ops-88",
        title="Fix CI deployment",
        description="See ticket https://fpt-corp.atlassian.net/browse/OPS-88",
        extraction_confidence=0.60,  # Demonstrates independence from extraction_confidence
        evidences=[
            EvidenceRecord(
                id="ev-jira-88",
                raw_event_id="raw-88-jira",
                evidence_type=EvidenceType.JIRA_TICKET,
                source_type="jira",
                external_url="https://fpt-corp.atlassian.net/browse/OPS-88",
                timestamp=datetime(2026, 9, 17, 14, 0, 0, tzinfo=timezone.utc),
                snippet="OPS-88: Staging deploy failed",
            )
        ],
    )

    matcher = TaskCandidateMatcher(scorer=correlation_scorer)
    cand_anchors = matcher.extract_anchors(candidate)
    target_anchors = matcher.extract_anchors(target)
    common_anchors = [f"{a.anchor_type}:{a.value}" for a in (cand_anchors & target_anchors)]

    assert "jira:OPS-88" in common_anchors

    result = correlation_scorer.score(
        candidate=candidate,
        target_task=target,
        matching_anchors=common_anchors,
        has_conflict=False,
    )

    assert result.has_deterministic_anchor is True
    assert result.correlation_confidence >= 0.55
    assert result.correlation_confidence >= ANCHOR_MERGE_THRESHOLD
    assert result.should_merge is True
    assert "AUTO-MERGE" in result.reason


def test_matching_pure_semantic_above_threshold(correlation_scorer):
    """Test matching thuần semantic không có anchor (cần >= 0.70) -> AUTO-MERGE."""
    target = UnifiedTaskCandidate(
        id="task-sem-1",
        title="Refactor auth token refresh handler",
        evidences=[],
    )
    # Similar title with slight variation, NO deterministic anchors
    candidate = UnifiedTaskCandidate(
        id="cand-sem-1",
        title="Refactor auth token refresh handler and service",
        evidences=[],
    )

    result = correlation_scorer.score(
        candidate=candidate,
        target_task=target,
        matching_anchors=[],
        has_conflict=False,
    )

    assert result.has_deterministic_anchor is False
    assert result.correlation_confidence >= 0.70
    assert result.correlation_confidence >= SEMANTIC_MERGE_THRESHOLD
    assert result.should_merge is True
    assert "AUTO-MERGE" in result.reason


def test_matching_pure_semantic_below_threshold(correlation_scorer):
    """Test matching thuần semantic dưới ngưỡng (< 0.70) -> Keep as independent candidate."""
    target = UnifiedTaskCandidate(
        id="task-sem-2",
        title="Refactor auth token refresh handler",
        evidences=[],
    )
    # Unrelated title
    candidate = UnifiedTaskCandidate(
        id="cand-sem-2",
        title="Upgrade Kubernetes worker nodes in production",
        evidences=[],
    )

    result = correlation_scorer.score(
        candidate=candidate,
        target_task=target,
        matching_anchors=[],
        has_conflict=False,
    )

    assert result.has_deterministic_anchor is False
    assert result.correlation_confidence < 0.70
    assert result.should_merge is False
    assert "independent" in result.reason.lower()


def test_conflicting_anchors_prevent_merge(correlation_scorer):
    """Test conflicting anchors disqualify merge even if titles are similar."""
    target = UnifiedTaskCandidate(
        id="task-c1",
        title="Fix database migration on staging",
        description="Ticket OPS-88",
        evidences=[],
    )
    candidate = UnifiedTaskCandidate(
        id="task-c2",
        title="Fix database migration on staging",
        description="Ticket OPS-99",
        evidences=[],
    )

    matcher = TaskCandidateMatcher(scorer=correlation_scorer)
    cand_anchors = matcher.extract_anchors(candidate)
    target_anchors = matcher.extract_anchors(target)
    has_conflict = matcher.check_anchor_conflict(cand_anchors, target_anchors)
    assert has_conflict is True

    result = correlation_scorer.score(
        candidate=candidate,
        target_task=target,
        matching_anchors=[],
        has_conflict=has_conflict,
    )

    assert result.correlation_confidence == 0.0
    assert result.should_merge is False
    assert result.has_anchor_conflict is True


# ------------------------------------------------------------------------------
# 3. Matcher End-to-End Search Tests
# ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_matcher_find_best_match(sample_target_task):
    target1 = sample_target_task.model_copy(deep=True)
    target1.description = "Associated with OPS-88"

    target2 = UnifiedTaskCandidate(
        id="task-unrelated",
        title="Setup weekly meeting schedule",
        evidences=[],
    )

    candidate = UnifiedTaskCandidate(
        id="cand-new",
        title="Investigate failure",
        description="Refer to OPS-88",
        evidences=[],
    )

    matcher = TaskCandidateMatcher()
    best_match = await matcher.find_best_match(candidate, existing_tasks=[target2, target1])

    assert best_match is not None
    assert best_match.target_task.id == "task-target-001"
    assert best_match.score_result.should_merge is True
    assert best_match.score_result.correlation_confidence >= 0.55


# ------------------------------------------------------------------------------
# 4. TaskMerger Auto-Merge & Provenance Preservation Tests
# ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_merge_preserves_evidence_provenance(sample_target_task):
    """Test auto-merge bảo toàn toàn bộ Evidence provenance."""
    merger = TaskMerger()

    candidate = UnifiedTaskCandidate(
        id="cand-002",
        title="Investigate deployment failure on staging",
        project_key="OPS",
        due_date=datetime(2026, 9, 20, 18, 0, 0, tzinfo=timezone.utc),  # Later explicit deadline
        explicit_deadline=True,
        evidences=[
            EvidenceRecord(
                id="ev-002",
                raw_event_id="raw-88-jira-ops",
                evidence_type=EvidenceType.JIRA_TICKET,
                source_type="jira",
                external_url="https://fpt-corp.atlassian.net/browse/OPS-88",
                author_canonical_id="person-huy",
                author_canonical_name="Nguyen Van Huy",
                timestamp=datetime(2026, 9, 17, 14, 0, 0, tzinfo=timezone.utc),
                snippet="Deployment failed on staging environment",
                confidence=1.0,
            )
        ],
    )

    merged = await merger.merge(target_task=sample_target_task, candidate=candidate, persist=False)

    # 1. Target ID is preserved
    assert merged.id == sample_target_task.id

    # 2. Both evidences exist
    assert len(merged.evidences) == 2

    # 3. Provenance of first evidence is strictly preserved
    ev1 = next(e for e in merged.evidences if e.id == "ev-001")
    assert ev1.raw_event_id == "raw-908-teams-fpt"
    assert ev1.source_type == "ms_teams"
    assert ev1.snippet == "Để em check nhé."
    assert ev1.task_id == "task-target-001"

    # 4. Provenance of second evidence is strictly preserved
    ev2 = next(e for e in merged.evidences if e.id == "ev-002")
    assert ev2.raw_event_id == "raw-88-jira-ops"
    assert ev2.source_type == "jira"
    assert ev2.external_url == "https://fpt-corp.atlassian.net/browse/OPS-88"
    assert ev2.author_canonical_id == "person-huy"
    assert ev2.author_canonical_name == "Nguyen Van Huy"
    assert ev2.snippet == "Deployment failed on staging environment"
    assert ev2.task_id == "task-target-001"  # Linked to merged task

    # 5. Deadline extended and marked explicit
    assert merged.due_date == datetime(2026, 9, 20, 18, 0, 0, tzinfo=timezone.utc)
    assert merged.explicit_deadline is True

    # 6. updated_at is updated
    assert merged.updated_at is not None


@pytest.mark.asyncio
async def test_auto_merge_with_mock_repository(sample_target_task):
    """Test auto-merge persists via TaskDomainRepository.upsert_task_atomic."""
    mock_repo = AsyncMock()
    mock_repo.upsert_task_atomic = AsyncMock(return_value="task-target-001")

    merger = TaskMerger(task_repo=mock_repo)

    candidate = UnifiedTaskCandidate(
        id="cand-003",
        title="Check staging failure",
        evidences=[
            EvidenceRecord(
                id="ev-003",
                raw_event_id="raw-email-456",
                evidence_type=EvidenceType.EMAIL_THREAD,
                source_type="outlook",
                timestamp=datetime(2026, 9, 17, 15, 0, 0, tzinfo=timezone.utc),
                snippet="Please verify if staging is working again.",
            )
        ],
    )

    merged = await merger.merge(target_task=sample_target_task, candidate=candidate, persist=True)

    assert mock_repo.upsert_task_atomic.called
    saved_task = mock_repo.upsert_task_atomic.call_args[0][0]
    assert saved_task.id == "task-target-001"
    assert len(saved_task.evidences) == 2
    assert any(e.raw_event_id == "raw-email-456" for e in saved_task.evidences)


@pytest.mark.asyncio
async def test_auto_merge_deduplicates_same_evidence(sample_target_task):
    """Test auto-merge avoids duplicating identical evidence."""
    merger = TaskMerger()

    # Candidate contains the exact same evidence as target task
    candidate = UnifiedTaskCandidate(
        id="cand-dup",
        title="Duplicate evidence candidate",
        evidences=[
            sample_target_task.evidences[0].model_copy()
        ],
    )

    merged = await merger.merge(target_task=sample_target_task, candidate=candidate, persist=False)
    assert len(merged.evidences) == 1
