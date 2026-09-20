import pytest
from ptb_database.validator import GraphOntologyValidator


def test_valid_node_validation():
    res = GraphOntologyValidator.validate_node("Person", {"canonical_id": "p-123", "name": "Cuong"})
    assert res.is_valid is True
    assert res.error_message is None

    res2 = GraphOntologyValidator.validate_node("Task", {"task_id": "t-123", "title": "Deploy"})
    assert res2.is_valid is True


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
        edge_type="OWNS",
        source_label="Person",
        target_label="Task"
    )
    assert res.is_valid is True


def test_valid_ai_extracted_edge():
    res = GraphOntologyValidator.validate_edge(
        edge_type="COMMITTED_TO",
        source_label="Person",
        target_label="Task",
        properties={"confidence": 0.95, "evidence_id": "ev-001"}
    )
    assert res.is_valid is True


def test_ai_edge_missing_evidence():
    res = GraphOntologyValidator.validate_edge(
        edge_type="COMMITTED_TO",
        source_label="Person",
        target_label="Task",
        properties={"confidence": 0.95}
    )
    assert res.is_valid is False
    assert "evidence_id" in res.error_message


def test_ai_edge_invalid_confidence():
    res = GraphOntologyValidator.validate_edge(
        edge_type="COMMITTED_TO",
        source_label="Person",
        target_label="Task",
        properties={"confidence": 1.5, "evidence_id": "ev-001"}
    )
    assert res.is_valid is False
    assert "confidence" in res.error_message


def test_edge_incompatible_endpoints():
    # OWNS phải là Person -> Task, không được ngược lại
    res = GraphOntologyValidator.validate_edge(
        edge_type="OWNS",
        source_label="Task",
        target_label="Person"
    )
    assert res.is_valid is False
    assert "Source label 'Task' không hợp lệ" in res.error_message


def test_unknown_edge_type():
    res = GraphOntologyValidator.validate_edge(
        edge_type="DEPENDS_ON",
        source_label="Task",
        target_label="Task"
    )
    assert res.is_valid is False
    assert "DEPENDS_ON" in res.error_message
