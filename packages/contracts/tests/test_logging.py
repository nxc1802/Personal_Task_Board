import json
import logging
from datetime import datetime, timezone
import pytest

from ptb_contracts import (
    BugCode,
    BugLogRecord,
    SourceType,
    log_bug,
)


def test_bug_code_enum_values():
    """Verify all required BugCode enum members and their string mappings."""
    expected_codes = {
        "PTB_STORAGE_001": "PTB-STORAGE-001",
        "PTB_CKPT_001": "PTB-CKPT-001",
        "PTB_L1_001": "PTB-L1-001",
        "PTB_L1_002": "PTB-L1-002",
        "PTB_L2_001": "PTB-L2-001",
        "PTB_LLM_001": "PTB-LLM-001",
        "PTB_GRAPH_001": "PTB-GRAPH-001",
        "PTB_APP_001": "PTB-APP-001",
        "PTB_MCP_001": "PTB-MCP-001",
        "PTB_OWUI_001": "PTB-OWUI-001",
    }

    assert len(BugCode) == len(expected_codes)
    for attr, val in expected_codes.items():
        enum_member = getattr(BugCode, attr)
        assert enum_member.value == val
        assert enum_member == val
        assert isinstance(enum_member, str)


def test_bug_log_record_defaults():
    """Verify BugLogRecord instantiation with defaults."""
    record = BugLogRecord(
        bug_code=BugCode.PTB_STORAGE_001,
        subsystem="neo4j",
        message="Authoritative database connection lost",
    )

    assert record.bug_code == BugCode.PTB_STORAGE_001
    assert record.subsystem == "neo4j"
    assert record.message == "Authoritative database connection lost"
    assert record.severity == "ERROR"
    assert isinstance(record.timestamp, datetime)
    assert record.timestamp.tzinfo is not None  # UTC timezone aware
    assert record.exception_type is None
    assert record.raw_event_id is None
    assert record.task_id is None
    assert record.source_type is None
    assert record.tenant_id is None
    assert record.context is None


def test_bug_log_record_full_fields_and_serialization():
    """Verify BugLogRecord with all fields populated and serialization roundtrip."""
    now = datetime.now(timezone.utc)
    context_data = {
        "host": "neo4j://localhost:7687",
        "retry_attempt": 3,
        "details": {"timeout_ms": 5000},
    }

    record = BugLogRecord(
        timestamp=now,
        bug_code=BugCode.PTB_L1_001,
        severity="CRITICAL",
        subsystem="playwright",
        message="Playwright authentication session expired",
        exception_type="AuthExpiredError",
        raw_event_id="raw-evt-12345",
        task_id="task-9999",
        source_type=SourceType.MS_TEAMS,
        tenant_id="tenant-fpt-internal",
        context=context_data,
    )

    # Field validations
    assert record.timestamp == now
    assert record.bug_code == BugCode.PTB_L1_001
    assert record.severity == "CRITICAL"
    assert record.subsystem == "playwright"
    assert record.message == "Playwright authentication session expired"
    assert record.exception_type == "AuthExpiredError"
    assert record.raw_event_id == "raw-evt-12345"
    assert record.task_id == "task-9999"
    assert record.source_type == "ms_teams"
    assert record.tenant_id == "tenant-fpt-internal"
    assert record.context == context_data

    # Model dump mode='python'
    dumped = record.model_dump()
    assert dumped["bug_code"] == "PTB-L1-001"
    assert dumped["severity"] == "CRITICAL"
    assert dumped["source_type"] == "ms_teams"
    assert dumped["context"]["retry_attempt"] == 3

    # Model dump mode='json'
    json_dumped = record.model_dump(mode="json")
    assert json_dumped["bug_code"] == "PTB-L1-001"
    assert isinstance(json_dumped["timestamp"], str)
    assert json_dumped["source_type"] == "ms_teams"

    # JSON serialization
    serialized_json = record.model_dump_json()
    parsed_json = json.loads(serialized_json)
    assert parsed_json["bug_code"] == "PTB-L1-001"
    assert parsed_json["severity"] == "CRITICAL"
    assert parsed_json["tenant_id"] == "tenant-fpt-internal"

    # Deserialization roundtrip
    restored = BugLogRecord.model_validate_json(serialized_json)
    assert restored.bug_code == record.bug_code
    assert restored.severity == record.severity
    assert restored.subsystem == record.subsystem
    assert restored.message == record.message
    assert restored.exception_type == record.exception_type
    assert restored.raw_event_id == record.raw_event_id
    assert restored.task_id == record.task_id
    assert restored.source_type == record.source_type
    assert restored.tenant_id == record.tenant_id
    assert restored.context == record.context


def test_bug_log_record_normalization():
    """Verify normalization of bug_code, severity, and source_type."""
    # String bug_code normalized to enum if valid
    r1 = BugLogRecord(
        bug_code="PTB-STORAGE-001",
        subsystem="storage",
        severity="error",  # lowercase normalized to uppercase
        source_type=SourceType.GIT,
    )
    assert r1.bug_code == BugCode.PTB_STORAGE_001
    assert isinstance(r1.bug_code, BugCode)
    assert r1.severity == "ERROR"
    assert r1.source_type == "git"

    # Custom string bug_code retained as string
    r2 = BugLogRecord(
        bug_code="PTB-CUSTOM-999",
        subsystem="custom",
        severity="warning",
    )
    assert r2.bug_code == "PTB-CUSTOM-999"
    assert r2.severity == "WARNING"


def test_log_bug_basic(caplog):
    """Verify log_bug writes structured log and returns BugLogRecord."""
    with caplog.at_level(logging.ERROR):
        record = log_bug(
            code=BugCode.PTB_STORAGE_001,
            subsystem="neo4j",
            severity="ERROR",
            message="Neo4j authoritative store unavailable",
            context={"bolt_url": "bolt://localhost:7687"},
        )

    assert isinstance(record, BugLogRecord)
    assert record.bug_code == BugCode.PTB_STORAGE_001
    assert record.subsystem == "neo4j"
    assert record.message == "Neo4j authoritative store unavailable"
    assert record.exception_type is None
    assert record.context == {"bolt_url": "bolt://localhost:7687"}

    # Verify log output in caplog
    matching_logs = [r for r in caplog.records if r.name == "ptb.bugs.neo4j"]
    assert len(matching_logs) == 1
    log_item = matching_logs[0]
    assert log_item.levelname == "ERROR"
    assert "PTB-STORAGE-001" in log_item.message
    assert "Neo4j authoritative store unavailable" in log_item.message
    assert getattr(log_item, "bug_record")["bug_code"] == "PTB-STORAGE-001"


def test_log_bug_with_explicit_exception(caplog):
    """Verify log_bug captures explicit exception and its type."""
    exc = ConnectionRefusedError("Connection to port 7687 refused")

    with caplog.at_level(logging.ERROR):
        record = log_bug(
            code=BugCode.PTB_STORAGE_001,
            subsystem="neo4j",
            message="Database unreachable",
            exc=exc,
            tenant_id="tenant-primary",
        )

    assert record.exception_type == "ConnectionRefusedError"
    assert record.message == "Database unreachable"
    assert record.tenant_id == "tenant-primary"

    # Verify log record has exc_info
    log_item = [r for r in caplog.records if r.name == "ptb.bugs.neo4j"][0]
    assert log_item.exc_info is not None


def test_log_bug_with_empty_message_fallback_to_exc():
    """Verify log_bug falls back to str(exc) if message is empty."""
    exc = TimeoutError("Request timed out after 30 seconds")
    record = log_bug(
        code=BugCode.PTB_LLM_001,
        subsystem="llm",
        message="",
        exc=exc,
    )
    assert record.message == "Request timed out after 30 seconds"
    assert record.exception_type == "TimeoutError"


def test_log_bug_captures_active_exception_context():
    """Verify log_bug captures active exception from sys.exc_info when inside except block."""
    try:
        raise ValueError("Invalid checkpoint offset")
    except ValueError:
        record = log_bug(
            code=BugCode.PTB_CKPT_001,
            subsystem="checkpoint",
            message="Failed to commit offset",
        )

    assert record.exception_type == "ValueError"
    assert record.bug_code == BugCode.PTB_CKPT_001
    assert record.subsystem == "checkpoint"


def test_log_bug_severities_and_levels(caplog):
    """Verify severity mapping to python logging levels."""
    levels_to_test = [
        ("DEBUG", logging.DEBUG),
        ("INFO", logging.INFO),
        ("WARNING", logging.WARNING),
        ("ERROR", logging.ERROR),
        ("CRITICAL", logging.CRITICAL),
        ("info", logging.INFO),  # lowercase normalization
        ("INVALID_SEV", logging.ERROR),  # fallback to ERROR
    ]

    for sev_input, expected_logging_level in levels_to_test:
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            record = log_bug(
                code=BugCode.PTB_APP_001,
                subsystem="application",
                severity=sev_input,
                message=f"Testing severity {sev_input}",
            )
            matching = [r for r in caplog.records if r.name == "ptb.bugs.application"]
            assert len(matching) == 1
            assert matching[0].levelno == expected_logging_level


def test_log_bug_all_optional_identifiers():
    """Verify log_bug correctly propagates all optional identifiers."""
    record = log_bug(
        code=BugCode.PTB_L2_001,
        subsystem="processing",
        raw_event_id="raw-555",
        task_id="task-777",
        source_type=SourceType.MS_OUTLOOK,
        tenant_id="tenant-corp",
        context={"step": "extraction"},
    )

    assert record.raw_event_id == "raw-555"
    assert record.task_id == "task-777"
    assert record.source_type == "ms_outlook"
    assert record.tenant_id == "tenant-corp"
    assert record.context == {"step": "extraction"}


def test_package_exports():
    """Verify symbols are exported from ptb_contracts root."""
    import ptb_contracts

    assert hasattr(ptb_contracts, "BugCode")
    assert hasattr(ptb_contracts, "BugLogRecord")
    assert hasattr(ptb_contracts, "log_bug")
    assert "BugCode" in ptb_contracts.__all__
    assert "BugLogRecord" in ptb_contracts.__all__
    assert "log_bug" in ptb_contracts.__all__
