"""Regression tests for held-out attribution, status, and filter quality.

Validates:
- Structured source (Jira, GitHub) fallback attribution to author
- Teams subordinate commitment resolution to team lead
- Relative date and deadline parsing from text
- Strict status inference (IN_PROGRESS, DONE)
- Architectural decision record filtering vs noise rejection negative controls
All cases use *new, unseen phrasing* never present in the benchmark dataset.
"""

from __future__ import annotations

from datetime import datetime, timezone
import pytest

from ptb_contracts.l1_acquisition import RawEventRecord, SourceType
from ptb_contracts.l2_processing import TaskStatus
from ptb_processing.extractor.heuristic_filter import HeuristicCandidateFilter
from ptb_processing.extractor.structured_extractor import DeterministicStructuredExtractor
from ptb_processing.identity.resolver import IdentityResolver
from ptb_processing.pipeline import ProcessingPipeline


@pytest.fixture
def identity_resolver() -> IdentityResolver:
    resolver = IdentityResolver()
    resolver.register_person(
        person_id="lead.tran",
        canonical_name="Tran Van Lead",
        primary_email="lead.tran@company.com",
        tenant_id="tenant-fpt-internal",
        aliases=["Lead", "anh Lead", "Tran Van Lead"],
    )
    resolver.register_person(
        person_id="cuong.dam",
        canonical_name="Dam Quang Cuong",
        primary_email="cuong.dam@company.com",
        tenant_id="tenant-fpt-internal",
        aliases=["Cuong", "anh Cường"],
    )
    resolver.register_person(
        person_id="mai.le",
        canonical_name="Mai Le",
        primary_email="mai.le@company.com",
        tenant_id="tenant-fpt-internal",
        aliases=["Mai", "chị Mai"],
    )
    return resolver


@pytest.fixture
def pipeline(identity_resolver: IdentityResolver) -> ProcessingPipeline:
    from tests.support.test_doubles import FakeDeterministicLLMExtractor
    return ProcessingPipeline(
        identity_resolver=identity_resolver,
        heuristic_filter=HeuristicCandidateFilter(),
        structured_extractor=DeterministicStructuredExtractor(),
        llm_extractor=FakeDeterministicLLMExtractor(),
    )


@pytest.mark.unit
def test_jira_structured_extractor_owner_fallback() -> None:
    """Jira ticket with sparse/empty payload fields must attribute author as owner."""
    extractor = DeterministicStructuredExtractor()
    raw = RawEventRecord(
        id="raw-test-jira-sparse",
        tenant_id="tenant-fpt-internal",
        source_type=SourceType.JIRA,
        external_id="INFRA-999",
        author_display_name="Dam Quang Cuong",
        author_external_id="cuong.dam@company.com",
        raw_payload={"content": "[INFRA-999] Provision isolated staging subnet"},
        normalized_text="[INFRA-999] Provision isolated staging subnet",
        idempotency_key="idemp-test-jira-1",
    )
    candidate = extractor.extract(raw)
    assert candidate is not None
    assert candidate.owner_name == "Dam Quang Cuong"
    assert candidate.requester_name is None
    assert candidate.project_key == "INFRA"
    assert candidate.status == TaskStatus.TODO


@pytest.mark.unit
def test_github_structured_extractor_date_and_completion() -> None:
    """GitHub commit/issue with completion signals must resolve status=DONE and extract deadline."""
    extractor = DeterministicStructuredExtractor()
    raw = RawEventRecord(
        id="raw-test-gh-done",
        tenant_id="tenant-fpt-internal",
        source_type=SourceType.GITHUB,
        external_id="ext-gh-888",
        author_display_name="Nguyen Van Huy",
        author_external_id="huy.nguyen@company.com",
        raw_payload={"content": "Đã hoàn thành deploy v1.3.2 lên cluster production ngày 2026-11-20."},
        normalized_text="Đã hoàn thành deploy v1.3.2 lên cluster production ngày 2026-11-20.",
        idempotency_key="idemp-test-gh-1",
    )
    candidate = extractor.extract(raw)
    assert candidate is not None
    assert candidate.status == TaskStatus.DONE
    assert candidate.owner_name == "Nguyen Van Huy"
    assert candidate.due_date == datetime.fromisoformat("2026-11-20T18:00:00+00:00")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_teams_subordinate_commitment_assigns_lead_requester(pipeline: ProcessingPipeline) -> None:
    """Subordinate commitment phrasing ('em sẽ') in team channel assigns registered lead as requester."""
    raw = RawEventRecord(
        id="raw-test-teams-subordinate",
        tenant_id="tenant-fpt-internal",
        source_type=SourceType.MS_TEAMS,
        external_id="ext-teams-commit-1",
        conversation_or_project_id="conv-teams-main",
        event_timestamp=datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc),
        author_display_name="Mai Le",
        author_external_id="mai.le@company.com",
        raw_payload={"content": "Vâng anh, để em chạy benchmark suite trước 14:00 ngày 2026-11-25 nhé."},
        normalized_text="Để em chạy benchmark suite trước 14:00 ngày 2026-11-25.",
        idempotency_key="idemp-test-subordinate-1",
    )
    result = await pipeline.process_raw_event(raw)
    assert result.candidate is not None
    assert result.candidate.owner_canonical_id == "mai.le"
    assert result.candidate.requester_canonical_id == "lead.tran"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_peer_self_commitment_negative_control(pipeline: ProcessingPipeline) -> None:
    """Self-commitment by lead or peer ('Tôi sẽ', 'Anh sẽ') must NOT assign lead as requester."""
    raw = RawEventRecord(
        id="raw-test-self-commitment",
        tenant_id="tenant-fpt-internal",
        source_type=SourceType.MS_OUTLOOK,
        external_id="ext-mail-self-1",
        conversation_or_project_id="conv-mail-main",
        event_timestamp=datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc),
        author_display_name="Dam Quang Cuong",
        author_external_id="cuong.dam@company.com",
        raw_payload={"content": "Mình sẽ cập nhật tài liệu kiến trúc trước 17:00 ngày 2026-11-22."},
        normalized_text="Mình sẽ cập nhật tài liệu kiến trúc trước 17:00 ngày 2026-11-22.",
        idempotency_key="idemp-test-self-1",
    )
    result = await pipeline.process_raw_event(raw)
    assert result.candidate is not None
    assert result.candidate.owner_canonical_id == "cuong.dam"
    assert result.candidate.requester_canonical_id is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_slack_in_progress_status_inference(pipeline: ProcessingPipeline) -> None:
    """Active progress phrasing ('đang bắt tay vào') must infer status=IN_PROGRESS."""
    raw = RawEventRecord(
        id="raw-test-slack-inprogress",
        tenant_id="tenant-fpt-internal",
        source_type=SourceType.SLACK,
        external_id="ext-slack-wip-1",
        conversation_or_project_id="conv-slack-main",
        event_timestamp=datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc),
        author_display_name="Mai Le",
        author_external_id="mai.le@company.com",
        raw_payload={"content": "Team mình đang bắt tay vào refactor adapter Outlook Web trước 17:00."},
        normalized_text="Team mình đang bắt tay vào refactor adapter Outlook Web trước 17:00.",
        idempotency_key="idemp-test-wip-1",
    )
    result = await pipeline.process_raw_event(raw)
    assert result.candidate is not None
    assert result.candidate.status == TaskStatus.IN_PROGRESS


@pytest.mark.unit
def test_heuristic_filter_decision_commitment_and_negative_control() -> None:
    """Heuristic filter should extract technical decision records while rejecting social noise."""
    filter_ = HeuristicCandidateFilter()

    # Positive: Decision record
    assert filter_.should_extract(
        "Thống nhất chọn PostgreSQL 16 làm cơ sở dữ liệu quan hệ cho module auth.",
        source_type="ms_outlook",
    ) is True
    assert filter_.should_extract(
        "Quyết định triển khai OpenWebUI cho sprint 4.",
        source_type="slack",
    ) is True

    # Negative controls: Social chatter
    assert filter_.should_extract("Trưa nay ai đi ăn bún chả không?", source_type="ms_teams") is False
    assert filter_.should_extract("Chào buổi sáng cả nhà, chúc tuần mới vui vẻ!", source_type="slack") is False
    assert filter_.should_extract("Cuối tuần này có ai đi đá bóng không?", source_type="ms_teams") is False
