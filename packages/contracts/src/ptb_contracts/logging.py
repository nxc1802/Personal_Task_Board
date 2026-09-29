"""Bug Logging Contract and structured bug reporting for Personal Task Board.

Provides standardized bug codes (BugCode), Pydantic record model (BugLogRecord),
and the log_bug() helper to enforce uniform bug reporting across all subsystems.
"""

from datetime import datetime, timezone
from enum import Enum
import json
import logging
import sys
from typing import Any, Dict, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator


class BugCode(str, Enum):
    """Standardized bug codes across the Personal Task Board platform."""

    PTB_STORAGE_001 = "PTB-STORAGE-001"  # Neo4j authoritative store unavailable
    PTB_CKPT_001 = "PTB-CKPT-001"        # Checkpoint read/write inconsistency
    PTB_L1_001 = "PTB-L1-001"            # Playwright authentication expired
    PTB_L1_002 = "PTB-L1-002"            # Playwright capture/persist failure
    PTB_L2_001 = "PTB-L2-001"            # Processing pipeline failure
    PTB_LLM_001 = "PTB-LLM-001"          # LLM provider unavailable
    PTB_GRAPH_001 = "PTB-GRAPH-001"      # Graphiti unavailable
    PTB_APP_001 = "PTB-APP-001"          # Application dependency unhealthy
    PTB_MCP_001 = "PTB-MCP-001"          # MCP server unhealthy
    PTB_OWUI_001 = "PTB-OWUI-001"        # OpenWebUI cannot reach Application API


class BugLogRecord(BaseModel):
    """Normalized structured bug report model."""

    model_config = ConfigDict(populate_by_name=True)

    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when the bug occurred/was logged",
    )
    bug_code: Union[BugCode, str] = Field(
        description="Standardized bug code identifying the failure type",
    )
    severity: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="ERROR",
        description="Severity level of the bug",
    )
    subsystem: str = Field(
        description="Subsystem or component where the bug occurred (e.g. neo4j, playwright, processing)",
    )
    message: str = Field(
        default="",
        description="Human-readable description of the error",
    )
    exception_type: Optional[str] = Field(
        default=None,
        description="Class name or type of exception if available",
    )
    raw_event_id: Optional[str] = Field(
        default=None,
        description="Associated raw event ID if applicable",
    )
    task_id: Optional[str] = Field(
        default=None,
        description="Associated task ID if applicable",
    )
    source_type: Optional[str] = Field(
        default=None,
        description="Associated source type (e.g. ms_teams, jira, git) if applicable",
    )
    tenant_id: Optional[str] = Field(
        default=None,
        description="Associated tenant ID if applicable",
    )
    context: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Arbitrary contextual metadata or parameters",
    )

    @field_validator("bug_code", mode="before")
    @classmethod
    def _normalize_bug_code(cls, v: Any) -> Union[BugCode, str]:
        if isinstance(v, BugCode):
            return v
        if isinstance(v, str):
            try:
                return BugCode(v)
            except ValueError:
                return v
        return str(v)

    @field_validator("severity", mode="before")
    @classmethod
    def _normalize_severity(cls, v: Any) -> str:
        if isinstance(v, str):
            v_upper = v.upper()
            if v_upper in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
                return v_upper
        return v

    @field_validator("source_type", mode="before")
    @classmethod
    def _normalize_source_type(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, Enum):
            return str(v.value)
        return str(v)


def log_bug(
    code: Union[str, BugCode],
    subsystem: str,
    severity: str = "ERROR",
    message: str = "",
    context: Optional[dict[str, Any]] = None,
    exc: Optional[Exception] = None,
    raw_event_id: Optional[str] = None,
    task_id: Optional[str] = None,
    source_type: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> BugLogRecord:
    """Standardized bug logging entrypoint.

    Builds a normalized BugLogRecord, emits a structured log message with JSON payload
    using standard Python logging (logger named 'ptb.bugs.{subsystem}'), and returns the record.
    """
    # 1. Resolve exception type & active exception
    exception_type: Optional[str] = None
    if exc is not None:
        exception_type = type(exc).__name__
    else:
        active_exc = sys.exc_info()[1]
        if isinstance(active_exc, Exception):
            exc = active_exc
            exception_type = type(active_exc).__name__

    # 2. Resolve message fallback from exception if empty
    effective_message = message
    if not effective_message and exc is not None:
        effective_message = str(exc)

    # 3. Normalize severity
    sev = severity.upper() if isinstance(severity, str) else "ERROR"
    if sev not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        sev = "ERROR"

    # 4. Normalize source_type if Enum was passed
    src_type_val = source_type.value if isinstance(source_type, Enum) else source_type

    # 5. Build BugLogRecord
    record = BugLogRecord(
        timestamp=datetime.now(timezone.utc),
        bug_code=code,
        severity=sev,  # type: ignore[arg-type]
        subsystem=subsystem,
        message=effective_message,
        exception_type=exception_type,
        raw_event_id=raw_event_id,
        task_id=task_id,
        source_type=src_type_val,
        tenant_id=tenant_id,
        context=context,
    )

    # 6. Emit structured log via standard logging
    logger = logging.getLogger(f"ptb.bugs.{subsystem}")
    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    level = level_map.get(record.severity, logging.ERROR)

    payload = record.model_dump(mode="json")
    payload_json = json.dumps(payload, ensure_ascii=False)
    log_text = f"[{record.bug_code}] {record.message}" if record.message else f"[{record.bug_code}]"

    logger.log(
        level,
        "%s | %s",
        log_text,
        payload_json,
        extra={"bug_record": payload, "payload": payload},
        exc_info=exc,
    )

    return record
