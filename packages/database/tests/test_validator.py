import pytest
from ptb_database.validator import GraphOntologyValidator


def test_valid_node_validation():
    res = GraphOntologyValidator.validate_node("Person", {"canonical_id": "p-123", "name": "Cuong"})
    assert res.is_valid is True
    assert res.error_message is None

    res2 = GraphOntologyValidator.validate_node("UnifiedTask", {"id": "t-123", "title": "Deploy"})
    assert res2.is_valid is True

    res3 = GraphOntologyValidator.validate_node("Evidence", {"id": "ev-123", "snippet": "I will do it"})
    assert res3.is_valid is True

    res4 = GraphOntologyValidator.validate_node("RawEvent", {"id": "raw-123", "source_type": "ms_teams"})
    assert res4.is_valid is True


def test_invalid_node_label():
    res = GraphOntologyValidator.validate_node("Subtask", {"subtask_id": "st-1"})
    assert res.is_valid is False
    assert "Subtask" in res.error_message


def test_node_missing_required_id():
    res = GraphOntologyValidator.validate_node("Person", {"name": "Cuong"})
    assert res.is_valid is False
    assert "canonical_id" in res.error_message


def test_valid_deterministic_edge():
    res = GraphOntologyValidator.validate_edge(
        edge_type="ASSIGNED_TO",
        source_label="Person",
        target_label="UnifiedTask"
    )
    assert res.is_valid is True

    res2 = GraphOntologyValidator.validate_edge(
        edge_type="HAS_EVIDENCE",
        source_label="UnifiedTask",
        target_label="Evidence"
    )
    assert res2.is_valid is True


def test_valid_ai_extracted_edge():
    res = GraphOntologyValidator.validate_edge(
        edge_type="COMMITTED_TO",
        source_label="Person",
        target_label="UnifiedTask",
        properties={"confidence": 0.95, "evidence_id": "ev-001"}
    )
    assert res.is_valid is True


def test_ai_edge_missing_evidence():
    res = GraphOntologyValidator.validate_edge(
        edge_type="COMMITTED_TO",
        source_label="Person",
        target_label="UnifiedTask",
        properties={"confidence": 0.95}
    )
    assert res.is_valid is False
    assert "evidence_id" in res.error_message


def test_ai_edge_invalid_confidence():
    res = GraphOntologyValidator.validate_edge(
        edge_type="COMMITTED_TO",
        source_label="Person",
        target_label="UnifiedTask",
        properties={"confidence": 1.5, "evidence_id": "ev-001"}
    )
    assert res.is_valid is False
    assert "confidence" in res.error_message


def test_edge_incompatible_endpoints():
    # ASSIGNED_TO phải là Person -> UnifiedTask, không được ngược lại
    res = GraphOntologyValidator.validate_edge(
        edge_type="ASSIGNED_TO",
        source_label="UnifiedTask",
        target_label="Person"
    )
    assert res.is_valid is False
    assert "Source label 'UnifiedTask' không hợp lệ" in res.error_message


def test_unknown_edge_type():
    res = GraphOntologyValidator.validate_edge(
        edge_type="DEPENDS_ON",
        source_label="UnifiedTask",
        target_label="UnifiedTask"
    )
    assert res.is_valid is False
    assert "DEPENDS_ON" in res.error_message


# ==============================================================================
# Wave 1D: Vocabulary & Schema Consistency Tests (docs/v1_2.md)
# ==============================================================================


def test_person_canonical_id_enforcement():
    """Person node bắt buộc phải có canonical_id."""
    res_valid = GraphOntologyValidator.validate_node("Person", {"canonical_id": "person-cuong-01", "canonical_name": "Cuong"})
    assert res_valid.is_valid is True

    res_invalid = GraphOntologyValidator.validate_node("Person", {"name": "Cuong without ID"})
    assert res_invalid.is_valid is False
    assert "canonical_id" in res_invalid.error_message


def test_task_status_vocabulary_enforcement():
    """UnifiedTask và StatusTransitionAudit chỉ chấp nhận 5 trạng thái uppercase:
    TODO, IN_PROGRESS, BLOCKED, DONE, DISMISSED."""
    canonical_statuses = ["TODO", "IN_PROGRESS", "BLOCKED", "DONE", "DISMISSED"]
    for status in canonical_statuses:
        res = GraphOntologyValidator.validate_node("UnifiedTask", {"id": "task-01", "status": status})
        assert res.is_valid is True, f"Status {status} should be valid"

    # Lowercase hoặc status ngoài danh mục bị từ chối
    invalid_statuses = ["todo", "open", "in_progress", "done", "DISMISSED_TEMPORARY", "REVIEW"]
    for status in invalid_statuses:
        res = GraphOntologyValidator.validate_node("UnifiedTask", {"id": "task-01", "status": status})
        assert res.is_valid is False, f"Status {status} should be invalid"
        assert "5 trạng thái uppercase" in res.error_message

    # Kiểm tra StatusTransitionAudit old_status/new_status
    res_audit_valid = GraphOntologyValidator.validate_node(
        "StatusTransitionAudit",
        {"id": "aud-01", "old_status": "TODO", "new_status": "IN_PROGRESS"}
    )
    assert res_audit_valid.is_valid is True

    res_audit_invalid = GraphOntologyValidator.validate_node(
        "StatusTransitionAudit",
        {"id": "aud-01", "old_status": "todo", "new_status": "IN_PROGRESS"}
    )
    assert res_audit_invalid.is_valid is False
    assert "old_status" in res_audit_invalid.error_message


def test_merge_audit_canonical_schema():
    """MergeAudit bắt buộc có candidate_task_ids, winning_task_id, correlation_score,
    deterministic_anchors, merge_reason, merged_at."""
    valid_props = {
        "id": "ma-01",
        "candidate_task_ids": ["task-1", "task-2"],
        "winning_task_id": "task-1",
        "correlation_score": 0.95,
        "deterministic_anchors": ["jira:PROJ-101"],
        "merge_reason": "Deterministic Jira issue key match",
        "merged_at": "2026-09-29T11:00:00Z",
    }
    res = GraphOntologyValidator.validate_canonical_schema("MergeAudit", valid_props)
    assert res.is_valid is True

    # Test với alias created_at
    valid_with_alias = dict(valid_props)
    del valid_with_alias["merged_at"]
    valid_with_alias["created_at"] = "2026-09-29T11:00:00Z"
    res_alias = GraphOntologyValidator.validate_canonical_schema("MergeAudit", valid_with_alias)
    assert res_alias.is_valid is True

    # Thiếu winning_task_id
    invalid_props = dict(valid_props)
    del invalid_props["winning_task_id"]
    res_missing = GraphOntologyValidator.validate_canonical_schema("MergeAudit", invalid_props)
    assert res_missing.is_valid is False
    assert "winning_task_id" in res_missing.error_message


def test_status_transition_audit_canonical_schema():
    """StatusTransitionAudit bắt buộc có id, task_id, old_status, new_status,
    change_actor, timestamp, reason."""
    valid_props = {
        "id": "sta-01",
        "task_id": "task-01",
        "old_status": "TODO",
        "new_status": "IN_PROGRESS",
        "change_actor": "SYSTEM",
        "timestamp": "2026-09-29T11:00:00Z",
        "reason": "Developer started working",
    }
    res = GraphOntologyValidator.validate_canonical_schema("StatusTransitionAudit", valid_props)
    assert res.is_valid is True

    # Test với alias changed_at
    valid_with_alias = dict(valid_props)
    del valid_with_alias["timestamp"]
    valid_with_alias["changed_at"] = "2026-09-29T11:00:00Z"
    res_alias = GraphOntologyValidator.validate_canonical_schema("StatusTransitionAudit", valid_with_alias)
    assert res_alias.is_valid is True

    # Thiếu change_actor
    invalid_props = dict(valid_props)
    del invalid_props["change_actor"]
    res_missing = GraphOntologyValidator.validate_canonical_schema("StatusTransitionAudit", invalid_props)
    assert res_missing.is_valid is False
    assert "change_actor" in res_missing.error_message


def test_evidence_canonical_schema():
    """Evidence bắt buộc có id, task_id, evidence_type, snippet, source_type,
    source_event_id, timestamp, confidence_score."""
    valid_props = {
        "id": "ev-01",
        "task_id": "task-01",
        "evidence_type": "chat_commitment",
        "snippet": "I will deliver this module by 5pm",
        "source_type": "ms_teams",
        "source_event_id": "raw-event-01",
        "timestamp": "2026-09-29T11:00:00Z",
        "confidence_score": 0.98,
    }
    res = GraphOntologyValidator.validate_canonical_schema("Evidence", valid_props)
    assert res.is_valid is True

    # Test với alias raw_event_id & confidence
    valid_with_aliases = dict(valid_props)
    del valid_with_aliases["source_event_id"]
    del valid_with_aliases["confidence_score"]
    valid_with_aliases["raw_event_id"] = "raw-event-01"
    valid_with_aliases["confidence"] = 0.98
    res_alias = GraphOntologyValidator.validate_canonical_schema("Evidence", valid_with_aliases)
    assert res_alias.is_valid is True

    # Thiếu snippet
    invalid_props = dict(valid_props)
    del invalid_props["snippet"]
    res_missing = GraphOntologyValidator.validate_canonical_schema("Evidence", invalid_props)
    assert res_missing.is_valid is False
    assert "snippet" in res_missing.error_message


def test_ingestion_checkpoint_canonical_schema():
    """IngestionCheckpoint bắt buộc có tenant_id, source_type, stream_id,
    last_external_id, last_event_timestamp, cursor_token, updated_at."""
    valid_props = {
        "id": "cp-01",
        "tenant_id": "tenant-fpt",
        "source_type": "ms_teams",
        "stream_id": "channel-general",
        "last_external_id": "msg-123",
        "last_event_timestamp": "2026-09-29T11:00:00Z",
        "cursor_token": "token-xyz",
        "updated_at": "2026-09-29T11:00:00Z",
    }
    res = GraphOntologyValidator.validate_canonical_schema("IngestionCheckpoint", valid_props)
    assert res.is_valid is True

    # Thiếu tenant_id
    invalid_props = dict(valid_props)
    del invalid_props["tenant_id"]
    res_missing = GraphOntologyValidator.validate_canonical_schema("IngestionCheckpoint", invalid_props)
    assert res_missing.is_valid is False
    assert "tenant_id" in res_missing.error_message
