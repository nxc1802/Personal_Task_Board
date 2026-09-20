"""Synthetic data generator for contracts testing and layer mocking."""

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Dict, Any

from ptb_contracts.l1_acquisition import RawEventRecord, SourceType, ProcessingStatus
from ptb_contracts.l2_processing import UnifiedTaskCandidate, TaskStatus, EvidenceRecord, EvidenceType


def create_mock_raw_event(
    source_type: SourceType = SourceType.MS_TEAMS,
    external_id: str = "msg-12345",
    content: str = "Để em check vụ này nhé.",
    author_id: str = "cuong.dam@fpt.com",
    author_name: str = "Dam Quang Cuong"
) -> RawEventRecord:
    tenant_id = "tenant-fpt-internal"
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    idempotency_key = hashlib.sha256(
        f"{tenant_id}:{source_type.value}:{external_id}:{content_hash}".encode("utf-8")
    ).hexdigest()

    return RawEventRecord(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        source_type=source_type,
        external_id=external_id,
        idempotency_key=idempotency_key,
        event_timestamp=datetime.now(timezone.utc),
        author_external_id=author_id,
        author_display_name=author_name,
        conversation_or_project_id="channel-devops",
        raw_payload={"content": content, "author": author_name},
        processing_status=ProcessingStatus.PENDING,
        created_at=datetime.now(timezone.utc),
    )


def create_mock_unified_task(
    title: str = "Kiểm tra hệ thống staging",
    owner_id: str = "00000000-0000-0000-0000-000000000002",
    requester_id: str = "00000000-0000-0000-0000-000000000003"
) -> UnifiedTaskCandidate:
    task_id = str(uuid.uuid4())
    ev = EvidenceRecord(
        id=str(uuid.uuid4()),
        task_id=task_id,
        raw_event_id=str(uuid.uuid4()),
        evidence_type=EvidenceType.CHAT_COMMITMENT,
        source_type="ms_teams",
        author_canonical_id=owner_id,
        timestamp=datetime.now(timezone.utc),
        snippet="Để em check nhé.",
        confidence=0.95,
        extraction_version="v1.0"
    )

    return UnifiedTaskCandidate(
        id=task_id,
        title=title,
        status=TaskStatus.OPEN,
        owner_canonical_id=owner_id,
        requester_canonical_id=requester_id,
        extraction_confidence=0.95,
        evidences=[ev]
    )
