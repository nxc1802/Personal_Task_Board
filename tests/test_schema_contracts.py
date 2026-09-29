"""Static Schema & Repository Contract Tests (Wave 6 — Sub-Agent 6D).

Validates without a live Neo4j Docker container that:
1. All 3 Cypher schema files define the 7 required uniqueness constraints.
2. CheckpointRepository Cypher queries match IngestionCheckpointRecord and ontology.
3. RawEventRepository Cypher queries match RawEventRecord, ontology, and retry attempt rules.
4. TaskDomainRepository Cypher queries match L2/L3 Pydantic contracts, ontology, and TypeScript contracts.
"""

from __future__ import annotations

import inspect
from pathlib import Path
import re

import pytest

from ptb_contracts.l1_acquisition import (
    IngestionCheckpointRecord,
    ProcessingAttemptRecord,
    RawEventRecord,
)
from ptb_contracts.l2_processing import (
    CommitmentRecord,
    EvidenceRecord,
    MergeAuditRecord,
    StatusTransitionAuditRecord,
    UnifiedTaskCandidate,
)
from ptb_contracts.l3_storage import (
    CanonicalPersonRecord,
    CustomerRecord,
    ProjectRecord,
)
from ptb_database.ontology import (
    ALLOWED_EDGES,
    ALLOWED_NODES,
    CANONICAL_NODE_SCHEMAS,
    EVIDENCE_FIELDS,
    INGESTION_CHECKPOINT_FIELDS,
    MERGE_AUDIT_FIELDS,
    PERSON_FIELDS,
    STATUS_TRANSITION_AUDIT_FIELDS,
)
from ptb_database.repositories.checkpoint_repo import CheckpointRepository
from ptb_database.repositories.raw_event_repo import RawEventRepository
from ptb_database.repositories.task_repo import TaskDomainRepository

REPO_ROOT = Path(__file__).resolve().parents[1]

SCHEMA_FILES = [
    REPO_ROOT / "packages" / "database" / "neo4j" / "constraints" / "001_constraints.cypher",
    REPO_ROOT / "packages" / "database" / "neo4j" / "migrations" / "001_constraints.cypher",
    REPO_ROOT / "packages" / "database" / "neo4j" / "consolidated_schema.cypher",
]

CHECKPOINT_REPO_PATH = (
    REPO_ROOT / "packages" / "database" / "src" / "ptb_database" / "repositories" / "checkpoint_repo.py"
)
RAW_EVENT_REPO_PATH = (
    REPO_ROOT / "packages" / "database" / "src" / "ptb_database" / "repositories" / "raw_event_repo.py"
)
TASK_REPO_PATH = (
    REPO_ROOT / "packages" / "database" / "src" / "ptb_database" / "repositories" / "task_repo.py"
)
TYPES_TS_PATH = REPO_ROOT / "packages" / "contracts" / "src" / "types.ts"


def _extract_ts_interface_fields(ts_source: str, interface_name: str) -> set[str]:
    """Extract property names from a TypeScript interface block."""
    pattern = rf"export\s+interface\s+{re.escape(interface_name)}\s*\{{([^}}]+)\}}"
    match = re.search(pattern, ts_source, re.DOTALL)
    assert match is not None, f"TypeScript interface '{interface_name}' not found in types.ts"
    body = match.group(1)
    return set(re.findall(r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\??\s*:", body, re.MULTILINE))


def _extract_object_map_keys(cypher_source: str, label: str) -> set[str]:
    """Extract property keys from Cypher inline map `(alias:Label { key: $val, ... })`."""
    pattern = rf"\(\w+:{re.escape(label)}\s*\{{([^}}]+)\}}\)"
    keys: set[str] = set()
    for match in re.finditer(pattern, cypher_source, re.DOTALL):
        block = match.group(1)
        keys.update(re.findall(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*:", block))
    return keys


@pytest.mark.contract
def test_all_required_neo4j_constraints_exist_in_schema_files() -> None:
    """Verify all 3 Cypher schema files define the 7 mandatory uniqueness constraints."""
    required_constraint_patterns = {
        "UnifiedTask.id": re.compile(
            r"CREATE\s+CONSTRAINT\s+\w+\s+IF\s+NOT\s+EXISTS\s+"
            r"FOR\s+\((\w+):UnifiedTask\)\s+REQUIRE\s+\1\.id\s+IS\s+UNIQUE",
            re.IGNORECASE,
        ),
        "RawEvent.id": re.compile(
            r"CREATE\s+CONSTRAINT\s+\w+\s+IF\s+NOT\s+EXISTS\s+"
            r"FOR\s+\((\w+):RawEvent\)\s+REQUIRE\s+\1\.id\s+IS\s+UNIQUE",
            re.IGNORECASE,
        ),
        "RawEvent.idempotency_key": re.compile(
            r"CREATE\s+CONSTRAINT\s+\w+\s+IF\s+NOT\s+EXISTS\s+"
            r"FOR\s+\((\w+):RawEvent\)\s+REQUIRE\s+\1\.idempotency_key\s+IS\s+UNIQUE",
            re.IGNORECASE,
        ),
        "IngestionCheckpoint(tenant_id, source_type, stream_id)": re.compile(
            r"CREATE\s+CONSTRAINT\s+\w+\s+IF\s+NOT\s+EXISTS\s+"
            r"FOR\s+\((\w+):IngestionCheckpoint\)\s+REQUIRE\s+"
            r"\(\1\.tenant_id,\s*\1\.source_type,\s*\1\.stream_id\)\s+IS\s+UNIQUE",
            re.IGNORECASE,
        ),
        "Evidence.id": re.compile(
            r"CREATE\s+CONSTRAINT\s+\w+\s+IF\s+NOT\s+EXISTS\s+"
            r"FOR\s+\((\w+):Evidence\)\s+REQUIRE\s+\1\.id\s+IS\s+UNIQUE",
            re.IGNORECASE,
        ),
        "MergeAudit.id": re.compile(
            r"CREATE\s+CONSTRAINT\s+\w+\s+IF\s+NOT\s+EXISTS\s+"
            r"FOR\s+\((\w+):MergeAudit\)\s+REQUIRE\s+\1\.id\s+IS\s+UNIQUE",
            re.IGNORECASE,
        ),
        "StatusTransitionAudit.id": re.compile(
            r"CREATE\s+CONSTRAINT\s+\w+\s+IF\s+NOT\s+EXISTS\s+"
            r"FOR\s+\((\w+):StatusTransitionAudit\)\s+REQUIRE\s+\1\.id\s+IS\s+UNIQUE",
            re.IGNORECASE,
        ),
    }

    assert len(SCHEMA_FILES) == 3
    for schema_path in SCHEMA_FILES:
        assert schema_path.exists(), f"Schema file missing: {schema_path}"
        content = schema_path.read_text(encoding="utf-8")
        for constraint_name, pattern in required_constraint_patterns.items():
            assert pattern.search(content) is not None, (
                f"Missing required constraint '{constraint_name}' in {schema_path}"
            )


@pytest.mark.contract
def test_checkpoint_repository_cypher_matches_contract_fields() -> None:
    """Verify CheckpointRepository MERGE composite key and cp.<prop> accesses match contracts & ontology."""
    source = CHECKPOINT_REPO_PATH.read_text(encoding="utf-8")
    save_source = inspect.getsource(CheckpointRepository.save_checkpoint)

    # 1. Ontology node label check
    assert "IngestionCheckpoint" in ALLOWED_NODES
    assert "IngestionCheckpoint" in CANONICAL_NODE_SCHEMAS
    assert ":IngestionCheckpoint" in source

    # 2. Composite MERGE key in save_checkpoint
    merge_pattern = re.compile(
        r"MERGE\s*\(\s*cp:IngestionCheckpoint\s*\{\s*"
        r"tenant_id:\s*\$tenant_id\s*,\s*"
        r"source_type:\s*\$source_type\s*,\s*"
        r"stream_id:\s*\$stream_id\s*\}\s*\)"
    )
    assert merge_pattern.search(save_source) is not None, (
        "save_checkpoint must MERGE on composite key {tenant_id: $tenant_id, source_type: $source_type, stream_id: $stream_id}"
    )

    # 3. Every cp.<prop> read/written in CheckpointRepository must match IngestionCheckpointRecord and ontology
    cp_props_in_repo = set(re.findall(r"\bcp\.([a-zA-Z_][a-zA-Z0-9_]*)", source))
    cp_map_keys = _extract_object_map_keys(source, "IngestionCheckpoint")
    all_cp_props = cp_props_in_repo | cp_map_keys

    contract_fields = set(IngestionCheckpointRecord.model_fields.keys())
    ontology_fields = INGESTION_CHECKPOINT_FIELDS | {"id"}

    assert all_cp_props, "Expected cp.<prop> references in checkpoint_repo.py"
    assert all_cp_props <= contract_fields, (
        f"checkpoint_repo.py references properties not in IngestionCheckpointRecord: {all_cp_props - contract_fields}"
    )
    assert all_cp_props <= ontology_fields, (
        f"checkpoint_repo.py references properties not in ontology IngestionCheckpoint schema: {all_cp_props - ontology_fields}"
    )
    assert INGESTION_CHECKPOINT_FIELDS <= all_cp_props, (
        f"checkpoint_repo.py is missing canonical IngestionCheckpoint fields: {INGESTION_CHECKPOINT_FIELDS - all_cp_props}"
    )


@pytest.mark.contract
def test_raw_event_repository_cypher_matches_contract_fields() -> None:
    """Verify RawEventRepository MERGE key, re.<prop> fields, and processing_attempt_count increment rule."""
    source = RAW_EVENT_REPO_PATH.read_text(encoding="utf-8")
    persist_source = inspect.getsource(RawEventRepository.persist_raw_event)
    mark_status_source = inspect.getsource(RawEventRepository.mark_event_status)

    # 1. Ontology node & edge checks
    assert "RawEvent" in ALLOWED_NODES
    assert "ProcessingAttempt" in ALLOWED_NODES
    assert ALLOWED_EDGES.get("PROCESSING_ATTEMPT") == ("RawEvent", "ProcessingAttempt")

    # 2. MERGE (re:RawEvent {idempotency_key: $idempotency_key}) in persist_raw_event
    merge_pattern = re.compile(
        r"MERGE\s*\(\s*re:RawEvent\s*\{\s*idempotency_key:\s*\$idempotency_key\s*\}\s*\)"
    )
    assert merge_pattern.search(persist_source) is not None, (
        "persist_raw_event must use MERGE (re:RawEvent {idempotency_key: $idempotency_key})"
    )

    # 3. Every re.<prop> in persist_raw_event and across raw_event_repo matches RawEventRecord
    re_props_persist = set(re.findall(r"\bre\.([a-zA-Z_][a-zA-Z0-9_]*)", persist_source))
    re_map_keys = _extract_object_map_keys(source, "RawEvent")
    re_props_all = set(re.findall(r"\bre\.([a-zA-Z_][a-zA-Z0-9_]*)", source)) | re_map_keys

    raw_event_contract_fields = set(RawEventRecord.model_fields.keys())
    # raw_payload is a dict in Python serialized to payload_json in Neo4j; updated_at is audit timestamp in mark_event_status
    expected_persisted_fields = raw_event_contract_fields - {"raw_payload"}
    assert re_props_persist == expected_persisted_fields, (
        f"Mismatch between persist_raw_event Cypher properties and RawEventRecord fields: "
        f"diff={re_props_persist ^ expected_persisted_fields}"
    )
    assert re_props_all <= (raw_event_contract_fields | {"updated_at"}), (
        f"Unexpected re.<prop> in raw_event_repo.py: {re_props_all - (raw_event_contract_fields | {'updated_at'})}"
    )

    # ProcessingAttempt properties check
    pa_props = set(re.findall(r"\bpa\.([a-zA-Z_][a-zA-Z0-9_]*)", source)) | _extract_object_map_keys(
        source, "ProcessingAttempt"
    )
    pa_contract_fields = set(ProcessingAttemptRecord.model_fields.keys())
    assert pa_props <= pa_contract_fields, (
        f"Unexpected pa.<prop> in raw_event_repo.py: {pa_props - pa_contract_fields}"
    )

    # 4. mark_event_status only increments processing_attempt_count when $status IN ['processing', 'PROCESSING']
    attempt_case_pattern = re.compile(
        r"re\.processing_attempt_count\s*=\s*CASE\s+"
        r"WHEN\s+\$status\s+IN\s+\[\s*'processing'\s*,\s*'PROCESSING'\s*\]\s+"
        r"THEN\s+coalesce\(re\.processing_attempt_count,\s*re\.retry_count,\s*0\)\s*\+\s*1\s+"
        r"ELSE\s+coalesce\(re\.processing_attempt_count,\s*re\.retry_count,\s*0\)\s+"
        r"END",
        re.DOTALL,
    )
    assert attempt_case_pattern.search(mark_status_source) is not None, (
        "mark_event_status must increment re.processing_attempt_count ONLY when $status IN ['processing', 'PROCESSING']"
    )


@pytest.mark.contract
def test_task_domain_repository_cypher_matches_contract_fields() -> None:
    """Verify TaskDomainRepository node labels and properties match L2/L3 Pydantic contracts, ontology, and types.ts."""
    source = TASK_REPO_PATH.read_text(encoding="utf-8")
    ts_source = TYPES_TS_PATH.read_text(encoding="utf-8")
    upsert_source = inspect.getsource(TaskDomainRepository._execute_upsert_task)
    merge_audit_source = inspect.getsource(TaskDomainRepository.record_merge_audit)
    status_audit_source = inspect.getsource(TaskDomainRepository.record_status_transition_audit)

    # 1. Required domain node labels exist in ontology
    required_labels = {
        "UnifiedTask",
        "Evidence",
        "MergeAudit",
        "StatusTransitionAudit",
        "Person",
        "Project",
        "Customer",
        "Commitment",
    }
    assert required_labels <= ALLOWED_NODES, (
        f"Missing required labels in ALLOWED_NODES: {required_labels - ALLOWED_NODES}"
    )

    # Node labels referenced directly in task_repo.py Cypher patterns
    cypher_labels_in_task_repo = {
        "UnifiedTask",
        "Evidence",
        "MergeAudit",
        "StatusTransitionAudit",
        "Person",
        "Project",
        "Commitment",
    }
    for label in cypher_labels_in_task_repo:
        assert f":{label}" in source, f"Expected node label :{label} in task_repo.py Cypher queries"

    # Project & Customer contracts alignment with UnifiedTask
    assert "project_key" in ProjectRecord.model_fields
    assert "customer_id" in CustomerRecord.model_fields
    assert "t.project_key" in upsert_source
    assert "t.customer_id" in upsert_source

    # Commitment alignment
    assert "c.status" in source and "c.due_date" in source
    assert {"id", "title", "status", "due_date"} <= set(CommitmentRecord.model_fields.keys())

    # 2. Person canonical_id alignment across task_repo.py, ontology, Pydantic, and types.ts
    assert "canonical_id" in PERSON_FIELDS
    assert "canonical_id" in CanonicalPersonRecord.model_fields
    ts_person_fields = _extract_ts_interface_fields(ts_source, "CanonicalPersonRecord")
    assert {"canonical_id", "canonical_name", "primary_email"} <= ts_person_fields
    assert re.search(r"MERGE\s*\(\s*p:Person\s*\{\s*canonical_id:\s*\$owner_canonical_id\s*\}\s*\)", upsert_source)
    assert re.search(
        r"MERGE\s*\(\s*req:Person\s*\{\s*canonical_id:\s*\$requester_canonical_id\s*\}\s*\)", upsert_source
    )
    assert "owner.canonical_id" in source
    assert "req.canonical_id" in source

    # 3. MergeAudit fields alignment across task_repo.py, ontology, MergeAuditRecord, and types.ts
    merge_audit_cypher_props = _extract_object_map_keys(merge_audit_source, "MergeAudit")
    merge_audit_contract_fields = set(MergeAuditRecord.model_fields.keys())
    ts_merge_audit_fields = _extract_ts_interface_fields(ts_source, "MergeAuditRecord")

    required_merge_audit_keys = {"candidate_task_ids", "winning_task_id", "correlation_score"}
    assert required_merge_audit_keys <= merge_audit_cypher_props
    assert MERGE_AUDIT_FIELDS <= merge_audit_cypher_props, (
        f"MergeAudit Cypher missing ontology fields: {MERGE_AUDIT_FIELDS - merge_audit_cypher_props}"
    )
    assert merge_audit_cypher_props <= merge_audit_contract_fields, (
        f"MergeAudit Cypher uses fields not in MergeAuditRecord: {merge_audit_cypher_props - merge_audit_contract_fields}"
    )
    assert merge_audit_cypher_props <= ts_merge_audit_fields, (
        f"MergeAudit Cypher uses fields not in types.ts MergeAuditRecord: {merge_audit_cypher_props - ts_merge_audit_fields}"
    )

    # 4. StatusTransitionAudit fields alignment across task_repo.py, ontology, StatusTransitionAuditRecord, and types.ts
    status_audit_cypher_props = _extract_object_map_keys(status_audit_source, "StatusTransitionAudit")
    status_audit_contract_fields = set(StatusTransitionAuditRecord.model_fields.keys())
    ts_status_audit_fields = _extract_ts_interface_fields(ts_source, "StatusTransitionAuditRecord")

    required_status_audit_keys = {"old_status", "new_status", "change_actor"}
    assert required_status_audit_keys <= status_audit_cypher_props
    assert STATUS_TRANSITION_AUDIT_FIELDS <= status_audit_cypher_props, (
        f"StatusTransitionAudit Cypher missing ontology fields: {STATUS_TRANSITION_AUDIT_FIELDS - status_audit_cypher_props}"
    )
    assert status_audit_cypher_props <= status_audit_contract_fields, (
        f"StatusTransitionAudit Cypher uses fields not in StatusTransitionAuditRecord: "
        f"{status_audit_cypher_props - status_audit_contract_fields}"
    )
    assert status_audit_cypher_props <= ts_status_audit_fields, (
        f"StatusTransitionAudit Cypher uses fields not in types.ts StatusTransitionAuditRecord: "
        f"{status_audit_cypher_props - ts_status_audit_fields}"
    )

    # 5. UnifiedTask and Evidence Cypher properties match UnifiedTaskCandidate, EvidenceRecord, ontology, and types.ts
    task_props_in_upsert = set(re.findall(r"\bt\.([a-zA-Z_][a-zA-Z0-9_]*)", upsert_source)) | _extract_object_map_keys(
        upsert_source, "UnifiedTask"
    )
    task_contract_fields = set(UnifiedTaskCandidate.model_fields.keys())
    assert task_props_in_upsert <= task_contract_fields, (
        f"UnifiedTask Cypher uses fields not in UnifiedTaskCandidate: {task_props_in_upsert - task_contract_fields}"
    )

    evidence_props_in_upsert = set(
        re.findall(r"\be\.([a-zA-Z_][a-zA-Z0-9_]*)", upsert_source)
    ) | _extract_object_map_keys(upsert_source, "Evidence")
    evidence_contract_fields = set(EvidenceRecord.model_fields.keys())
    ts_evidence_fields = _extract_ts_interface_fields(ts_source, "EvidenceRecord")
    assert EVIDENCE_FIELDS <= evidence_props_in_upsert, (
        f"Evidence Cypher missing ontology fields: {EVIDENCE_FIELDS - evidence_props_in_upsert}"
    )
    assert evidence_props_in_upsert <= evidence_contract_fields, (
        f"Evidence Cypher uses fields not in EvidenceRecord: {evidence_props_in_upsert - evidence_contract_fields}"
    )
    assert evidence_props_in_upsert <= ts_evidence_fields, (
        f"Evidence Cypher uses fields not in types.ts EvidenceRecord: {evidence_props_in_upsert - ts_evidence_fields}"
    )
