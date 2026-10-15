"""Data models and schemas for Independent Benchmark Framework."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from ptb_contracts.l1_acquisition import RawEventRecord, SourceType
from ptb_contracts.l2_processing import EvidenceRecord, TaskStatus, UnifiedTaskCandidate


class ClarityLabel(str, Enum):
    """Clarity classification of raw input event."""
    CLEAR = "CLEAR"
    AMBIGUOUS = "AMBIGUOUS"


class ControlType(str, Enum):
    """Semantic control type classification."""
    NONE = "NONE"
    COMMITMENT = "COMMITMENT"
    DECISION = "DECISION"
    LESSON = "LESSON"


class EvalVerdict(str, Enum):
    """Evaluation verdict for sample, dimension, or overall run."""
    PASS = "PASS"
    FAIL = "FAIL"
    PENDING = "PENDING"
    SKIPPED = "SKIPPED"


class FailureCategory(str, Enum):
    """Granular failure taxonomy for diagnostic attribution."""
    EXTRACTION_MISS = "extraction_miss"
    NOISE_LEAK = "noise_leak"
    ATTRIBUTION_OWNER = "attribution_owner"
    ATTRIBUTION_REQUESTER = "attribution_requester"
    DEADLINE_MISMATCH = "deadline_mismatch"
    STATUS_MISMATCH = "status_mismatch"
    CORRELATION_FALSE_MERGE = "correlation_false_merge"
    CORRELATION_MISSED_MERGE = "correlation_missed_merge"
    LINEAGE_ORPHAN = "lineage_orphan"
    IDEMPOTENCY_DUPLICATE = "idempotency_duplicate"
    LATENCY_EXCEEDED = "latency_exceeded"
    EXECUTION_ERROR = "execution_error"
    INFRASTRUCTURE_UNAVAILABLE = "infrastructure_unavailable"


class PersonaSpec(BaseModel):
    """Persona definition for deterministic identity matching."""
    canonical_id: str
    canonical_name: str
    email: str
    account_ids: List[str] = Field(default_factory=list)
    aliases: List[str] = Field(default_factory=list)
    tenant_id: str = "tenant-fpt-internal"
    is_current_user: bool = False


class ExpectedEvidenceSpec(BaseModel):
    """Ground truth expectations for task evidence provenance."""
    source_event_id: str
    expected_snippet_substrings: List[str] = Field(default_factory=list)
    expected_timestamp: Optional[datetime] = None
    expected_evidence_type: Optional[str] = None


class ExpectedCorrelationSpec(BaseModel):
    """Ground truth expectations for correlation and task merging."""
    expected_cluster_id: Optional[str] = None
    expected_merge: bool = False
    expected_anchors: List[str] = Field(default_factory=list)
    expected_cluster_events: List[str] = Field(default_factory=list)


class GroundTruthSpec(BaseModel):
    """Authoritative ground truth specifications for a benchmark sample."""
    expected_is_task: bool
    clarity: ClarityLabel = ClarityLabel.CLEAR
    control_type: ControlType = ControlType.NONE
    expected_title_keywords: List[str] = Field(default_factory=list)
    expected_owner_id: Optional[str] = None
    expected_owner_name: Optional[str] = None
    expected_requester_id: Optional[str] = None
    expected_requester_name: Optional[str] = None
    expected_due_date: Optional[datetime] = None
    expected_explicit_deadline: bool = False
    expected_status: Optional[TaskStatus] = None
    expected_inferred_status: Optional[str] = None
    expected_review_status: Optional[str] = None
    correlation: ExpectedCorrelationSpec = Field(default_factory=ExpectedCorrelationSpec)
    evidence: Optional[ExpectedEvidenceSpec] = None
    verification_criteria: List[str] = Field(default_factory=list)


class BenchmarkSample(BaseModel):
    """Single benchmark evaluation test case."""
    sample_id: str
    case_category: str = "direct_task"
    origin_reference: str = ""
    raw_event: RawEventRecord
    ground_truth: GroundTruthSpec


class ProbeSpec(BaseModel):
    """Fault injection or recovery probe specification."""
    probe_id: str
    probe_group: str
    name: str
    target_module: str
    fault_injection: str
    expected_behavior: str
    expected_state_or_status: str
    origin_reference: str = ""


class FileChecksum(BaseModel):
    """Cryptographic file integrity descriptor."""
    sha256: str
    size_bytes: Optional[int] = None
    item_count: Optional[int] = None


class DatasetManifest(BaseModel):
    """Dataset manifest containing metadata, personas, and cryptographic file checksums."""
    dataset_name: str
    version: str
    commit_baseline: str = ""
    created_at: Optional[datetime] = None
    evaluation_now: Optional[datetime] = None
    samples_sha256: Optional[str] = None
    probes_sha256: Optional[str] = None
    dataset_files: Dict[str, FileChecksum] = Field(default_factory=dict)
    allowed_email_domains: List[str] = Field(default_factory=list)
    supported_controls: List[str] = Field(default_factory=list)
    personas: List[PersonaSpec] = Field(default_factory=list)
    probe_groups: Dict[str, List[str]] = Field(default_factory=dict)


class BenchmarkDataset(BaseModel):
    """Complete loaded benchmark dataset."""
    manifest: DatasetManifest
    samples: List[BenchmarkSample]
    probes: List[ProbeSpec] = Field(default_factory=list)


class FieldMismatch(BaseModel):
    """Pinpoints exact field mismatch between actual output and ground truth."""
    sample_id: str
    module: str
    field: str
    expected: Any
    actual: Any
    message: str


class CaseEvaluationResult(BaseModel):
    """Evaluation result for an individual benchmark test case."""
    case_id: str
    case_category: str
    source_type: str
    verdict: EvalVerdict
    actual: Dict[str, Any] = Field(default_factory=dict)
    expected: Dict[str, Any] = Field(default_factory=dict)
    failure_category: Optional[FailureCategory] = None
    failure_message: Optional[str] = None
    mismatches: List[FieldMismatch] = Field(default_factory=list)
    latency_ms: float = 0.0
    semantic_score: float = 1.0
    trace: Dict[str, Any] = Field(default_factory=dict)


class ConfidenceInterval(BaseModel):
    """Wilson 95% confidence interval [lower, upper]."""
    lower: float
    upper: float
    confidence: float = 0.95


class DimensionScore(BaseModel):
    """Score summary for one of the 10 scorecard dimensions."""
    dimension_number: int
    dimension_name: str
    primary_metric: str
    target_bar: str
    hard_gate: str
    measured_value: float
    display_value: str
    ci: Optional[ConfidenceInterval] = None
    passed: bool
    details: Dict[str, Any] = Field(default_factory=dict)


class BenchmarkScorecard(BaseModel):
    """Complete 10-dimension evaluation scorecard."""
    dataset_name: str
    dataset_version: str
    execution_mode: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    total_cases: int = 0
    passed_cases: int = 0
    failed_cases: int = 0
    pending_cases: int = 0
    skipped_cases: int = 0
    pass_rate: float = 0.0
    overall_verdict: EvalVerdict = EvalVerdict.PENDING
    dimensions: List[DimensionScore] = Field(default_factory=list)
    case_results: List[CaseEvaluationResult] = Field(default_factory=list)
