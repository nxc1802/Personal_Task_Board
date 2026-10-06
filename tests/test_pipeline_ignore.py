"""Test for Issue #4: review_status='ignore' candidates must not persist into Neo4j."""

from datetime import datetime, timezone
import pytest
from unittest.mock import AsyncMock, MagicMock

from ptb_contracts.l1_acquisition import ProcessingStatus, RawEventRecord, SourceType
from ptb_contracts.l2_processing import ParsedMessageContent, UnifiedTaskCandidate
from ptb_processing.pipeline import ProcessingPipeline
from tests.support.test_doubles import InMemoryTaskDomainRepository


@pytest.mark.asyncio
async def test_ignore_candidate_not_persisted_to_task_repo():
    task_repo = InMemoryTaskDomainRepository()
    task_repo.upsert_task_atomic = AsyncMock(wraps=task_repo.upsert_task_atomic)

    # Mock extractor that returns an ignored candidate (confidence < 0.40)
    mock_extractor = MagicMock()
    ignored_candidate = UnifiedTaskCandidate(
        id="candidate-ignore-1",
        title="Just some chatter",
        review_status="ignore",
        extraction_confidence=0.30,
    )
    mock_extractor.extract_async = AsyncMock(return_value=ignored_candidate)

    pipeline = ProcessingPipeline(
        task_repo=task_repo,
        llm_extractor=mock_extractor,
    )

    raw_event = RawEventRecord(
        id="raw-ignore-test",
        tenant_id="local-user",
        source_type=SourceType.CODING_AGENT,
        external_id="ext-ignore-1",
        idempotency_key="idemp-ignore-1",
        event_timestamp=datetime.now(timezone.utc),
        author_external_id="user1",
        conversation_or_project_id="conv-1",
        raw_payload={"text": "Chào buổi sáng mọi người"},
        normalized_text="Chào buổi sáng mọi người",
        processing_status=ProcessingStatus.PENDING,
    )

    result = await pipeline.process(raw_event)

    assert result.status == ProcessingStatus.PROCESSED
    assert result.candidate is None
    assert result.task_id is None

    # Verify task_repo.upsert_task_atomic was NEVER called
    task_repo.upsert_task_atomic.assert_not_called()
    assert len(task_repo.tasks) == 0
