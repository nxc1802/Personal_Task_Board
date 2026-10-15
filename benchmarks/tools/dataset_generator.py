"""Deterministic Dataset Generator for PTB-Bench-500.

Generates 500 benchmark cases:
- 475 raw event samples across 6 channels (Outlook, Teams, Slack, GitHub, Jira, Confluence)
- 25 fault injection / recovery probe specifications
- 11 challenge categories (noise, negation, multilingual, near_duplicate, cross_source,
  attachments, ambiguous, date_parsing, direct_task, quote_reply, issue_tracking)
- Stratified splits: Dev (235), Calibration (95), Locked Test (145)
- Full cryptographic SHA-256 manifest generation
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

# Ensure repository root is on sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.manifest import (
    compute_file_sha256,
    generate_manifest_checksums,
    validate_ground_truth_sample,
    validate_manifest,
)
from benchmarks.models import (
    BenchmarkDataset,
    BenchmarkSample,
    ClarityLabel,
    ControlType,
    DatasetManifest,
    ExpectedCorrelationSpec,
    ExpectedEvidenceSpec,
    FileChecksum,
    GroundTruthSpec,
    PersonaSpec,
    ProbeSpec,
)
from ptb_contracts.l1_acquisition import RawEventRecord, SourceType
from ptb_contracts.l2_processing import TaskStatus

# ---------------------------------------------------------------------------
# Personas Definition (Standardized Identity Graph)
# ---------------------------------------------------------------------------
PERSONAS: List[Dict[str, Any]] = [
    {
        "canonical_id": "cuong.dam",
        "canonical_name": "Dam Quang Cuong",
        "email": "cuong.dam@company.com",
        "account_ids": ["cuong.dam", "cuong.dam_jira", "cuong.dam_slack", "cuong-dam-gh", "cuongdam"],
        "aliases": ["Cuong", "anh Cường", "anh Cuong", "Cuong Dam", "Cường", "Dam Quang Cuong"],
        "tenant_id": "tenant-fpt-internal",
        "is_current_user": True,
    },
    {
        "canonical_id": "lead.tran",
        "canonical_name": "Tran Van Lead",
        "email": "lead.tran@company.com",
        "account_ids": ["lead.tran", "lead.tran_jira", "lead.tran_slack", "lead-tran-gh", "leadtran"],
        "aliases": ["Lead", "anh Lead", "Tran Van Lead", "Sếp Lead", "Lead Tran"],
        "tenant_id": "tenant-fpt-internal",
        "is_current_user": False,
    },
    {
        "canonical_id": "huy.nguyen",
        "canonical_name": "Nguyen Van Huy",
        "email": "huy.nguyen@company.com",
        "account_ids": ["huy.nguyen", "huy.nguyen_jira", "huy.nguyen_slack", "huy-nguyen-gh", "huynguyen"],
        "aliases": ["Huy", "Nguyen Van Huy", "anh Huy", "Huy Nguyen"],
        "tenant_id": "tenant-fpt-internal",
        "is_current_user": False,
    },
    {
        "canonical_id": "lan.pham",
        "canonical_name": "Pham Thi Lan",
        "email": "lan.pham@company.com",
        "account_ids": ["lan.pham", "lan.pham_jira", "lan.pham_slack", "lan-pham-gh", "lanpham"],
        "aliases": ["Lan", "chi Lan", "Lan Pham", "Pham Thi Lan", "chị Lan"],
        "tenant_id": "tenant-fpt-internal",
        "is_current_user": False,
    },
    {
        "canonical_id": "minh.vu",
        "canonical_name": "Vu Duc Minh",
        "email": "minh.vu@company.com",
        "account_ids": ["minh.vu", "minh.vu_jira", "minh.vu_slack", "minh-vu-gh", "minhvu"],
        "aliases": ["Minh", "anh Minh", "Minh Vu", "Vu Duc Minh"],
        "tenant_id": "tenant-fpt-internal",
        "is_current_user": False,
    },
    {
        "canonical_id": "anh.le",
        "canonical_name": "Le Hoang Anh",
        "email": "anh.le@company.com",
        "account_ids": ["anh.le", "anh.le_jira", "anh.le_slack", "anh-le-gh", "anhle"],
        "aliases": ["Hoang Anh", "anh Hoang Anh", "Le Hoang Anh", "Anh Le", "QC Anh"],
        "tenant_id": "tenant-fpt-internal",
        "is_current_user": False,
    },
    {
        "canonical_id": "sarah.chen",
        "canonical_name": "Sarah Chen",
        "email": "sarah.chen@company.com",
        "account_ids": ["sarah.chen", "sarah.chen_jira", "sarah.chen_slack", "sarahchen-gh", "sarahchen"],
        "aliases": ["Sarah", "Sarah Chen", "Ms. Sarah"],
        "tenant_id": "tenant-fpt-internal",
        "is_current_user": False,
    },
    {
        "canonical_id": "alex.miller",
        "canonical_name": "Alex Miller",
        "email": "alex.miller@partner.com",
        "account_ids": ["alex.miller", "alex.miller_jira", "alex.miller_slack", "alexmiller-gh", "alexmiller"],
        "aliases": ["Alex", "Alex Miller", "Mr. Miller"],
        "tenant_id": "tenant-fpt-internal",
        "is_current_user": False,
    },
]

ALLOWED_EMAIL_DOMAINS = ["company.com", "partner.com", "fpt.com"]

# ---------------------------------------------------------------------------
# Granular Technical Scenarios and Vocabulary
# ---------------------------------------------------------------------------
TECHNICAL_DOMAINS = [
    {
        "topic": "payment_gateway",
        "keywords": ["payment", "checkout", "vnpay", "transaction"],
        "action_vi": "tích hợp cổng thanh toán VNPay và kiểm tra xác thực chữ ký HMAC",
        "action_en": "integrate VNPay payment gateway and verify HMAC signature check",
        "component": "PaymentGatewayService",
        "ticket_prefix": "PAY",
    },
    {
        "topic": "auth_security",
        "keywords": ["auth", "token", "oauth2", "jwt"],
        "action_vi": "cập nhật middleware JWT refresh token và thu hồi session",
        "action_en": "update JWT refresh token middleware and revoke stale sessions",
        "component": "AuthSecurityModule",
        "ticket_prefix": "SEC",
    },
    {
        "topic": "database_pool",
        "keywords": ["database", "connection", "postgres", "pool"],
        "action_vi": "sửa lỗi leak connection pool trên PostgreSQL database",
        "action_en": "fix PostgreSQL database connection pool leak under high concurrency",
        "component": "DatabasePoolManager",
        "ticket_prefix": "DB",
    },
    {
        "topic": "k8s_ingress",
        "keywords": ["kubernetes", "ingress", "tls", "deploy"],
        "action_vi": "cấu hình gia hạn chứng chỉ TLS trên Kubernetes Ingress",
        "action_en": "renew TLS certificates and update Kubernetes Ingress controller rules",
        "component": "K8sIngressController",
        "ticket_prefix": "OPS",
    },
    {
        "topic": "search_indexing",
        "keywords": ["search", "indexing", "elasticsearch", "query"],
        "action_vi": "tối ưu query Elasticsearch và build lại search indexing queue",
        "action_en": "optimize Elasticsearch queries and rebuild background search index",
        "component": "ElasticsearchIndexer",
        "ticket_prefix": "SRCH",
    },
    {
        "topic": "frontend_hydration",
        "keywords": ["hydration", "react", "frontend", "ui"],
        "action_vi": "sửa lỗi hydration mismatch trên giao diện React Next.js",
        "action_en": "resolve React Next.js SSR hydration mismatch bug on task list",
        "component": "TaskBoardUI",
        "ticket_prefix": "UI",
    },
    {
        "topic": "ci_pipeline",
        "keywords": ["pipeline", "github", "actions", "ci"],
        "action_vi": "tối ưu cache Docker multi-stage build trong GitHub Actions workflow",
        "action_en": "optimize Docker multi-stage build cache in GitHub Actions CI pipeline",
        "component": "BuildPipelineWorkflow",
        "ticket_prefix": "CI",
    },
    {
        "topic": "notification_service",
        "keywords": ["notification", "websocket", "push", "apns"],
        "action_vi": "khắc phục sự cố drop kết nối WebSocket trên Notification Service",
        "action_en": "fix WebSocket reconnection drop bug on real-time notification service",
        "component": "NotificationGateway",
        "ticket_prefix": "NOTIF",
    },
    {
        "topic": "analytics_export",
        "keywords": ["export", "csv", "report", "analytics"],
        "action_vi": "sửa lỗi timeout khi xuất file CSV báo cáo thống kê",
        "action_en": "fix timeout error during large CSV report export generation",
        "component": "ReportGenerator",
        "ticket_prefix": "REP",
    },
    {
        "topic": "qa_regression",
        "keywords": ["regression", "playwright", "test", "e2e"],
        "action_vi": "bổ sung test case Playwright E2E cho quy trình đăng ký tài khoản",
        "action_en": "add automated Playwright E2E test cases for user registration flow",
        "component": "QATestSuite",
        "ticket_prefix": "QA",
    },
]

# ---------------------------------------------------------------------------
# 25 Fault Injection and Recovery Probes Definition
# ---------------------------------------------------------------------------
PROBES_SPECIFICATION: List[Dict[str, Any]] = [
    {
        "probe_id": "probe-01-missing-timestamp",
        "probe_group": "input_robustness",
        "name": "Missing timestamp graceful fallback",
        "target_module": "L1.Acquisition",
        "fault_injection": "Set raw_event.event_timestamp to None in ingestion stream",
        "expected_behavior": "Pipeline assigns captured_at as authoritative fallback without raising uncaught exception",
        "expected_state_or_status": "PROCESSED",
        "origin_reference": "ptb_500://probes/01",
    },
    {
        "probe_id": "probe-02-empty-payload",
        "probe_group": "input_robustness",
        "name": "Empty payload rejection",
        "target_module": "L2.HeuristicFilter",
        "fault_injection": "Set raw_payload to empty dict and normalized_text to empty string",
        "expected_behavior": "Filter rejects empty event gracefully with should_extract=False",
        "expected_state_or_status": "PROCESSED",
        "origin_reference": "ptb_500://probes/02",
    },
    {
        "probe_id": "probe-03-malformed-json-payload",
        "probe_group": "input_robustness",
        "name": "Malformed JSON payload parsing recovery",
        "target_module": "L2.UnifiedChannelParser",
        "fault_injection": "Pass corrupted non-JSON serialized payload string into channel parser",
        "expected_behavior": "Parser catches deserialization error gracefully, fallbacks to raw string",
        "expected_state_or_status": "PROCESSED",
        "origin_reference": "ptb_500://probes/03",
    },
    {
        "probe_id": "probe-04-oversized-text-payload",
        "probe_group": "input_robustness",
        "name": "Oversized text message truncation guard",
        "target_module": "L1.Sanitizer",
        "fault_injection": "Ingest single raw event containing 1.5MB text block",
        "expected_behavior": "Sanitizer safely truncates to max safe buffer length without OOM",
        "expected_state_or_status": "PROCESSED",
        "origin_reference": "ptb_500://probes/04",
    },
    {
        "probe_id": "probe-05-invalid-source-type",
        "probe_group": "input_robustness",
        "name": "Unknown source_type adapter routing",
        "target_module": "L1.AdapterRouter",
        "fault_injection": "Inject event with unrecognized source_type 'custom_legacy_crm'",
        "expected_behavior": "Acquisition logs unknown adapter and applies generic fallback parser",
        "expected_state_or_status": "PROCESSED",
        "origin_reference": "ptb_500://probes/05",
    },
    {
        "probe_id": "probe-06-missing-idempotency-key",
        "probe_group": "input_robustness",
        "name": "Missing idempotency key deterministic regeneration",
        "target_module": "L1.AcquisitionPipeline",
        "fault_injection": "Submit RawEventRecord with empty idempotency_key",
        "expected_behavior": "Acquisition pipeline recomputes deterministic SHA256 key from payload",
        "expected_state_or_status": "PROCESSED",
        "origin_reference": "ptb_500://probes/06",
    },
    {
        "probe_id": "probe-07-duplicate-event-replay",
        "probe_group": "concurrency_and_lifecycle",
        "name": "Duplicate event ingestion rejection",
        "target_module": "L1.JournalRepository",
        "fault_injection": "Ingest exact duplicate event with identical idempotency_key twice",
        "expected_behavior": "Journal rejects second entry as duplicate without creating duplicate task",
        "expected_state_or_status": "PROCESSED",
        "origin_reference": "ptb_500://probes/07",
    },
    {
        "probe_id": "probe-08-out-of-order-event-delivery",
        "probe_group": "concurrency_and_lifecycle",
        "name": "Out-of-order event delivery handling",
        "target_module": "L3.Lifecycle",
        "fault_injection": "Deliver earlier event timestamp after a later event on the same ticket has processed",
        "expected_behavior": "Lifecycle preserves newer state without rolling back authoritative state",
        "expected_state_or_status": "PROCESSED",
        "origin_reference": "ptb_500://probes/08",
    },
    {
        "probe_id": "probe-09-llm-timeout-recovery",
        "probe_group": "infrastructure_recovery",
        "name": "LLM extractor timeout resilience",
        "target_module": "L2.LLMStructuredExtractor",
        "fault_injection": "Simulate 5000ms network timeout on LLM completion endpoint",
        "expected_behavior": "Pipeline records retry attempt and marks case PENDING without crashing process",
        "expected_state_or_status": "PENDING",
        "origin_reference": "ptb_500://probes/09",
    },
    {
        "probe_id": "probe-10-llm-rate-limit-429",
        "probe_group": "infrastructure_recovery",
        "name": "LLM API rate limit 429 backoff",
        "target_module": "L2.LLMStructuredExtractor",
        "fault_injection": "Simulate HTTP 429 Too Many Requests response with retry-after header",
        "expected_behavior": "Extractor catches HTTP 429, logs rate limit, marks status RETRY",
        "expected_state_or_status": "RETRY",
        "origin_reference": "ptb_500://probes/10",
    },
    {
        "probe_id": "probe-11-neo4j-transient-disconnect",
        "probe_group": "infrastructure_recovery",
        "name": "Graph database transient connection recovery",
        "target_module": "L4.Neo4jDomainRepository",
        "fault_injection": "Simulate socket disconnect during Cypher transaction execution",
        "expected_behavior": "Repository triggers exponential backoff retry policy",
        "expected_state_or_status": "RETRY",
        "origin_reference": "ptb_500://probes/11",
    },
    {
        "probe_id": "probe-12-graphiti-sync-failure",
        "probe_group": "infrastructure_recovery",
        "name": "Secondary knowledge graph sync fault tolerance",
        "target_module": "L2.EvidenceRecordSync",
        "fault_injection": "Simulate network failure when syncing EvidenceRecord to Graphiti",
        "expected_behavior": "Primary transaction succeeds; evidence graph_sync_status set to RETRY",
        "expected_state_or_status": "PROCESSED",
        "origin_reference": "ptb_500://probes/12",
    },
    {
        "probe_id": "probe-13-corrupted-unicode-null-bytes",
        "probe_group": "input_robustness",
        "name": "Corrupted unicode and null bytes stripping",
        "target_module": "L1.Sanitizer",
        "fault_injection": "Inject string with null bytes (\\x00) and orphaned UTF-16 surrogates",
        "expected_behavior": "Sanitizer cleans illegal codepoints; database write completes cleanly",
        "expected_state_or_status": "PROCESSED",
        "origin_reference": "ptb_500://probes/13",
    },
    {
        "probe_id": "probe-14-future-timestamp-drift",
        "probe_group": "input_robustness",
        "name": "Far future timestamp drift validation",
        "target_module": "L1.Sanitizer",
        "fault_injection": "Submit event with event_timestamp set to year 2099",
        "expected_behavior": "Sanitizer logs clock skew warning and clamps event timestamp to ingestion time",
        "expected_state_or_status": "PROCESSED",
        "origin_reference": "ptb_500://probes/14",
    },
    {
        "probe_id": "probe-15-deep-link-malformed-scheme",
        "probe_group": "input_robustness",
        "name": "Malformed deep_link URL scheme handling",
        "target_module": "L1.Sanitizer",
        "fault_injection": "Submit event with deep_link 'javascript:alert(1)' or 'htp:////invalid'",
        "expected_behavior": "URL sanitizer rejects unsafe schemes, sanitizes deep_link to None",
        "expected_state_or_status": "PROCESSED",
        "origin_reference": "ptb_500://probes/15",
    },
    {
        "probe_id": "probe-16-quote-reply-attribution-confusion",
        "probe_group": "attribution_and_identity",
        "name": "Quote reply owner versus requester disambiguation",
        "target_module": "L2.AttributionValidator",
        "fault_injection": "Nested blockquote from Lead Tran, replied by Cuong Dam committing to task",
        "expected_behavior": "Attribution assigns Cuong Dam as owner, Lead Tran as requester",
        "expected_state_or_status": "PROCESSED",
        "origin_reference": "ptb_500://probes/16",
    },
    {
        "probe_id": "probe-17-cross-tenant-impersonation",
        "probe_group": "attribution_and_identity",
        "name": "Cross-tenant impersonation attempt isolation",
        "target_module": "L2.IdentityResolver",
        "fault_injection": "Submit event claiming author email of another tenant with matching name",
        "expected_behavior": "IdentityResolver blocks automatic merge across tenants; emits candidate signal",
        "expected_state_or_status": "PROCESSED",
        "origin_reference": "ptb_500://probes/17",
    },
    {
        "probe_id": "probe-18-unknown-author-display-name",
        "probe_group": "attribution_and_identity",
        "name": "Unregistered author display name handling",
        "target_module": "L2.IdentityResolver",
        "fault_injection": "Submit event from external email 'external_vendor@partner.com' without Persona",
        "expected_behavior": "Resolver returns unresolved status without raising KeyError or unhandled exception",
        "expected_state_or_status": "PROCESSED",
        "origin_reference": "ptb_500://probes/18",
    },
    {
        "probe_id": "probe-19-conflicting-status-transition",
        "probe_group": "concurrency_and_lifecycle",
        "name": "Conflicting status transition state machine enforcement",
        "target_module": "L3.StatusInferenceMachine",
        "fault_injection": "Attempt direct illegal status jump from DONE to DISMISSED without reason",
        "expected_behavior": "State machine enforces transition invariants, preserves verified status",
        "expected_state_or_status": "PROCESSED",
        "origin_reference": "ptb_500://probes/19",
    },
    {
        "probe_id": "probe-20-missing-required-jira-summary",
        "probe_group": "input_robustness",
        "name": "Missing summary field in Jira issue webhook",
        "target_module": "L2.JiraIssueParser",
        "fault_injection": "Jira webhook JSON payload with missing 'fields.summary' key",
        "expected_behavior": "Parser falls back to issue key or description snippet without raising KeyError",
        "expected_state_or_status": "PROCESSED",
        "origin_reference": "ptb_500://probes/20",
    },
    {
        "probe_id": "probe-21-circular-parent-id-reference",
        "probe_group": "concurrency_and_lifecycle",
        "name": "Circular parent_external_id reference cycle breaker",
        "target_module": "L2.HierarchyDetector",
        "fault_injection": "Event A has parent_external_id pointing to Event B, and Event B points to Event A",
        "expected_behavior": "Detector recognizes cyclic graph, breaks cycle safely without stack overflow",
        "expected_state_or_status": "PROCESSED",
        "origin_reference": "ptb_500://probes/21",
    },
    {
        "probe_id": "probe-22-null-author-external-id",
        "probe_group": "input_robustness",
        "name": "Null author external id fallback",
        "target_module": "L1.AcquisitionPipeline",
        "fault_injection": "RawEventRecord with empty or null author_external_id",
        "expected_behavior": "Acquisition sets author to 'system-anonymous' without crashing",
        "expected_state_or_status": "PROCESSED",
        "origin_reference": "ptb_500://probes/22",
    },
    {
        "probe_id": "probe-23-concurrent-task-update-conflict",
        "probe_group": "concurrency_and_lifecycle",
        "name": "Concurrent task update version conflict resolution",
        "target_module": "L4.TaskDomainRepository",
        "fault_injection": "Simultaneous write of two differing updates against identical task version",
        "expected_behavior": "Optimistic concurrency check retries stale transaction cleanly",
        "expected_state_or_status": "PROCESSED",
        "origin_reference": "ptb_500://probes/23",
    },
    {
        "probe_id": "probe-24-extremely-long-title-truncation",
        "probe_group": "input_robustness",
        "name": "Extremely long task title truncation",
        "target_module": "L2.Extractor",
        "fault_injection": "Event containing single paragraph of 2000 continuous characters without spaces",
        "expected_behavior": "Extractor normalizes title length to 255 characters cleanly",
        "expected_state_or_status": "PROCESSED",
        "origin_reference": "ptb_500://probes/24",
    },
    {
        "probe_id": "probe-25-unauthenticated-reset-guard",
        "probe_group": "security_integrity",
        "name": "Unauthenticated database reset endpoint rejection",
        "target_module": "Security.Guard",
        "fault_injection": "Issue HTTP POST /api/database/reset without bearer token or authorization",
        "expected_behavior": "Security guard rejects request with 401/403 and writes security audit log",
        "expected_state_or_status": "BLOCKED",
        "origin_reference": "ptb_500://probes/25",
    },
]


class PTB500Generator:
    """Deterministic generator producing 475 samples, 25 probes, and cryptographic manifest."""

    def __init__(self, seed: int = 42) -> None:
        self.seed = seed
        self.rng = random.Random(seed)
        self.current_user = next(p for p in PERSONAS if p["is_current_user"])
        self.other_personas = [p for p in PERSONAS if not p["is_current_user"]]

    def _generate_teams_sample(
        self,
        index: int,
        category: str,
        topic: Dict[str, Any],
    ) -> BenchmarkSample:
        sample_id = f"ptb500-{index:03d}-teams-{category}"
        raw_id = f"raw-teams-{index:03d}"
        ext_id = f"ext-teams-{index:03d}"
        conv_id = f"conv-teams-{topic['topic']}"
        ts = f"2026-10-15T{8 + (index % 10):02d}:{index % 60:02d}:00Z"
        cap_ts = f"2026-10-15T{8 + (index % 10):02d}:{index % 60:02d}:05Z"
        author = self.rng.choice(PERSONAS)
        requester = self.rng.choice(self.other_personas)
        due_day = 18 + (index % 12)
        due_dt = f"2026-10-{due_day:02d}T17:00:00Z"
        t_key = f"{topic['ticket_prefix']}-{100 + index}"

        if category == "noise":
            noise_texts = [
                "Trưa nay cả team ăn cơm gà hay bún chả mọi người ơi?",
                "Chào buổi sáng cả nhà, chúc mọi người tuần làm việc vui vẻ và hiệu quả!",
                "Kính gửi cả team, bánh sinh nhật đã có ở quầy pantry tầng 3 nhé.",
                "Chúc mừng sinh nhật anh Lead Tran, chúc anh luôn mạnh khỏe và nhiều thành công!",
                "Cuối tuần này có ai đi đá bóng giao lưu cùng team Infra không ạ?",
                "Hôm nay trời mưa to quá, mọi người đi làm cẩn thận nhé.",
                "Cảm ơn mọi người đã tham gia buổi demo sáng nay, tài liệu slide đã upload lên Confluence.",
            ]
            text = self.rng.choice(noise_texts)
            payload = {"body": {"content": text}}
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://teams/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.MS_TEAMS,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=text,
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=False,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.NONE,
                    expected_title_keywords=[],
                    correlation=ExpectedCorrelationSpec(expected_merge=False),
                ),
            )

        elif category == "negation":
            neg_texts = [
                f"Không cần làm task {t_key} {topic['keywords'][0]} nữa nhé Cuong, anh đã hoàn thành xong rồi.",
                f"Đừng deploy bản cập nhật {t_key} hôm nay, bên khách hàng yêu cầu tạm hoãn đợt release.",
                f"Hủy kế hoạch test {t_key} trên staging nhé mọi người, bên đối tác dời lịch bảo trì.",
                f"No need to fix ticket {t_key} on {topic['keywords'][0]}, the client closed it as not needed.",
                f"Don't deploy {t_key} to production tonight, the deployment window has been postponed.",
            ]
            text = self.rng.choice(neg_texts)
            payload = {"body": {"content": text}}
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://teams/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.MS_TEAMS,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=requester["email"],
                    author_display_name=requester["canonical_name"],
                    raw_payload=payload,
                    normalized_text=text,
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=False,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.NONE,
                    expected_title_keywords=[],
                    correlation=ExpectedCorrelationSpec(expected_merge=False),
                ),
            )

        elif category == "ambiguous":
            amb_texts = [
                f"Có thể sau này team mình sẽ cân nhắc đổi kiến trúc {topic['component']} xem sao.",
                f"Chắc đợt tới rảnh thì nghiên cứu thêm về {topic['keywords'][0]} cho hệ thống.",
                f"Maybe we could consider refactoring the {topic['component']} in Q4 if we have spare capacity.",
                f"Có lẽ sau khi go-live mình nên xem lại phần {topic['keywords'][1]} xem có tối ưu được không.",
            ]
            text = self.rng.choice(amb_texts)
            payload = {"body": {"content": text}}
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://teams/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.MS_TEAMS,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=text,
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=False,
                    clarity=ClarityLabel.AMBIGUOUS,
                    control_type=ControlType.NONE,
                    expected_title_keywords=[],
                    correlation=ExpectedCorrelationSpec(expected_merge=False),
                ),
            )

        elif category == "quote_reply":
            quote_text = f"Nhờ anh Cuong phụ trách task {t_key}: {topic['action_vi']} trước 17:00 ngày 2026-10-{due_day:02d} nhé."
            reply_text = f"Vâng anh Lead, để em xử lý task {t_key} và hoàn thành trước 17:00 ngày 2026-10-{due_day:02d}."
            html_content = (
                f'<blockquote><strong itemprop="author">{requester["canonical_name"]}</strong>: '
                f'{quote_text}</blockquote><p>{reply_text}</p>'
            )
            payload = {"body": {"contentType": "html", "content": html_content}}
            norm_text = reply_text
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://teams/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.MS_TEAMS,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=self.current_user["email"],
                    author_display_name=self.current_user["canonical_name"],
                    raw_payload=payload,
                    normalized_text=norm_text,
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, topic["keywords"][0]],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=requester["canonical_id"],
                    expected_requester_name=requester["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=False,
                        expected_anchors=[t_key],
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[f"xử lý task {t_key}"],
                    ),
                ),
            )

        elif category == "multilingual":
            text = f"Anh Cuong deploy giup em {topic['component']} cho task {t_key} len staging truoc 17:00 ngay 2026-10-{due_day:02d} nhe."
            payload = {"body": {"content": text}}
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://teams/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.MS_TEAMS,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=requester["email"],
                    author_display_name=requester["canonical_name"],
                    raw_payload=payload,
                    normalized_text=text,
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, topic["component"]],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=requester["canonical_id"],
                    expected_requester_name=requester["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=False,
                        expected_anchors=[t_key],
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[f"task {t_key}"],
                    ),
                ),
            )

        elif category == "near_duplicate":
            text = f"Nhắc lại: Cuong hỗ trợ hoàn thành task {t_key}: {topic['keywords'][0]} trước 17:00 ngày 2026-10-{due_day:02d}."
            payload = {"body": {"content": text}}
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://teams/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.MS_TEAMS,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=requester["email"],
                    author_display_name=requester["canonical_name"],
                    raw_payload=payload,
                    normalized_text=text,
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, topic["keywords"][0]],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=requester["canonical_id"],
                    expected_requester_name=requester["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=True,
                        expected_anchors=[t_key],
                        expected_cluster_id=f"cluster-{t_key}",
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[f"task {t_key}"],
                    ),
                ),
            )

        elif category == "date_parsing":
            has_deadline = (index % 2 == 0)
            if has_deadline:
                date_str = f"trước 17:00 ngày 2026-10-{due_day:02d}"
                exp_due = due_dt
                exp_explicit = True
            else:
                date_str = "khi nào rảnh trong tuần"
                exp_due = None
                exp_explicit = False
            text = f"Nhờ anh Cuong hỗ trợ fix gấp bug {t_key} trên {topic['component']} {date_str}."
            payload = {"body": {"content": text}}
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://teams/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.MS_TEAMS,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=requester["email"],
                    author_display_name=requester["canonical_name"],
                    raw_payload=payload,
                    normalized_text=text,
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, "bug"],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=requester["canonical_id"],
                    expected_requester_name=requester["canonical_name"],
                    expected_due_date=exp_due,
                    expected_explicit_deadline=exp_explicit,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=False,
                        expected_anchors=[t_key],
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[f"bug {t_key}"],
                    ),
                ),
            )

        else:
            # direct_task (default)
            text = f"Nhờ Cuong thực hiện task {t_key}: {topic['action_vi']} trước 17:00 ngày 2026-10-{due_day:02d}."
            payload = {"body": {"content": text}}
            return BenchmarkSample(
                sample_id=sample_id,
                case_category="direct_task",
                origin_reference=f"ptb_500://teams/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.MS_TEAMS,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=requester["email"],
                    author_display_name=requester["canonical_name"],
                    raw_payload=payload,
                    normalized_text=text,
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, topic["keywords"][0]],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=requester["canonical_id"],
                    expected_requester_name=requester["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=False,
                        expected_anchors=[t_key],
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[f"task {t_key}"],
                    ),
                ),
            )

    def _generate_outlook_sample(
        self,
        index: int,
        category: str,
        topic: Dict[str, Any],
    ) -> BenchmarkSample:
        sample_id = f"ptb500-{index:03d}-outlook-{category}"
        raw_id = f"raw-outlook-{index:03d}"
        ext_id = f"ext-outlook-{index:03d}"
        conv_id = f"conv-mail-thread-{index:03d}"
        ts = f"2026-10-15T{8 + (index % 10):02d}:{index % 60:02d}:00Z"
        cap_ts = f"2026-10-15T{8 + (index % 10):02d}:{index % 60:02d}:05Z"
        author = self.rng.choice(self.other_personas)
        t_key = f"{topic['ticket_prefix']}-{200 + index}"
        due_day = 19 + (index % 11)
        due_dt = f"2026-10-{due_day:02d}T17:00:00Z"

        if category == "noise":
            subjects = [
                "Weekly Engineering All-Hands Summary",
                "Company Holiday Schedule Announcement",
                "IT Infrastructure Planned Maintenance Notice",
                "Security Awareness Monthly Newsletter",
                "Pantry Snack Restock & Coffee Machine Cleaning",
            ]
            subj = self.rng.choice(subjects)
            body = (
                f"Kính gửi toàn thể thành viên,\n\n"
                f"Đây là thông báo định kỳ về {subj.lower()}.\n"
                f"Chúc mọi người một tuần làm việc nhiều năng lượng và hiệu quả.\n\n"
                f"Trân trọng,\nBan Truyền Thông Nội Bộ"
            )
            payload = {"subject": subj, "body": {"content": body}}
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://outlook/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.MS_OUTLOOK,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"{subj}: {body}",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=False,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.NONE,
                    expected_title_keywords=[],
                    correlation=ExpectedCorrelationSpec(expected_merge=False),
                ),
            )

        elif category == "negation":
            subj = f"HỦY: Triển khai tác vụ {t_key}"
            body = (
                f"Chào team,\n\n"
                f"Do yêu cầu từ phía đối tác, không cần làm task {t_key} {topic['keywords'][0]} nữa nhé.\n"
                f"Chúng tôi sẽ cập nhật lại kế hoạch chi tiết trong phiên họp tuần tới.\n\n"
                f"Best regards,\n{author['canonical_name']}"
            )
            payload = {"subject": subj, "body": {"content": body}}
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://outlook/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.MS_OUTLOOK,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"{subj}: {body}",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=False,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.NONE,
                    expected_title_keywords=[],
                    correlation=ExpectedCorrelationSpec(expected_merge=False),
                ),
            )

        elif category == "ambiguous":
            subj = f"Thảo luận định hướng {topic['component']}"
            body = (
                f"Chào cả nhà,\n\n"
                f"Có thể sau này team mình sẽ cân nhắc đổi kiến trúc {topic['component']} xem sao.\n"
                f"Chúng ta sẽ đánh giá lại trong buổi retro cuối tháng.\n\n"
                f"Best regards,\n{author['canonical_name']}"
            )
            payload = {"subject": subj, "body": {"content": body}}
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://outlook/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.MS_OUTLOOK,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"{subj}: {body}",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=False,
                    clarity=ClarityLabel.AMBIGUOUS,
                    control_type=ControlType.NONE,
                    expected_title_keywords=[],
                    correlation=ExpectedCorrelationSpec(expected_merge=False),
                ),
            )

        elif category == "multilingual":
            subj = f"Request: deploy {topic['component']} to staging"
            body = (
                f"Hi Cuong,\n\n"
                f"Anh Cuong deploy giup em service {t_key} len staging environment truoc 17:00 ngay 2026-10-{due_day:02d} nhe.\n"
                f"Thanks & best regards,\n{author['canonical_name']}"
            )
            payload = {"subject": subj, "body": {"content": body}}
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://outlook/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.MS_OUTLOOK,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"{subj}: {body}",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, topic["component"]],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=author["canonical_id"],
                    expected_requester_name=author["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=False,
                        expected_anchors=[t_key],
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[f"service {t_key}"],
                    ),
                ),
            )

        elif category == "attachments":
            subj = f"Tài liệu đặc tả kiến trúc {t_key}: {topic['keywords'][0]}"
            file_name = f"Spec_Architecture_{topic['topic']}_v2.pdf"
            body = (
                f"Chào anh Cuong,\n\n"
                f"Gửi anh tài liệu đặc tả đính kèm {file_name}.\n"
                f"Nhờ anh Cuong review tài liệu đặc tả {t_key} trước 17:00 ngày 2026-10-{due_day:02d}.\n\n"
                f"Trân trọng,\n{author['canonical_name']}"
            )
            payload = {
                "subject": subj,
                "body": {"content": body},
                "hasAttachments": True,
                "attachments": [{"name": file_name, "size": 1048576, "contentType": "application/pdf"}],
            }
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://outlook/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.MS_OUTLOOK,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"{subj}: {body}",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, "review"],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=author["canonical_id"],
                    expected_requester_name=author["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=False,
                        expected_anchors=[t_key],
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[f"review tài liệu đặc tả {t_key}"],
                    ),
                ),
            )

        elif category == "date_parsing":
            has_deadline = (index % 2 == 0)
            if has_deadline:
                date_str = f"trước 17:00 ngày 2026-10-{due_day:02d}"
                exp_due = due_dt
                exp_explicit = True
            else:
                date_str = "khi nào có thời gian"
                exp_due = None
                exp_explicit = False
            subj = f"Nhiệm vụ {t_key}: cấu hình {topic['component']}"
            body = (
                f"Chào anh Cuong,\n\n"
                f"Nhờ anh Cuong cấu hình {t_key} {date_str} để phục vụ đợt nghiệm thu.\n\n"
                f"Trân trọng,\n{author['canonical_name']}"
            )
            payload = {"subject": subj, "body": {"content": body}}
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://outlook/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.MS_OUTLOOK,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"{subj}: {body}",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, topic["component"]],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=author["canonical_id"],
                    expected_requester_name=author["canonical_name"],
                    expected_due_date=exp_due,
                    expected_explicit_deadline=exp_explicit,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=False,
                        expected_anchors=[t_key],
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[f"cấu hình {t_key}"],
                    ),
                ),
            )

        elif category == "quote_reply":
            subj = f"Re: Yêu cầu xử lý task {t_key}"
            body = (
                f"Vâng anh Lead, để em nhận và xử lý task {t_key} trước 17:00 ngày 2026-10-{due_day:02d}.\n\n"
                f"-----Original Message-----\n"
                f"From: {author['canonical_name']} <{author['email']}>\n"
                f"Subject: Yêu cầu xử lý task {t_key}\n"
                f"Nhờ Cuong thực hiện task {t_key} nhé."
            )
            payload = {"subject": subj, "body": {"content": body}}
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://outlook/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.MS_OUTLOOK,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=self.current_user["email"],
                    author_display_name=self.current_user["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"{subj}: {body}",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, "xử lý"],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=author["canonical_id"],
                    expected_requester_name=author["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=False,
                        expected_anchors=[t_key],
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[f"xử lý task {t_key}"],
                    ),
                ),
            )

        elif category == "near_duplicate":
            subj = f"Reminder: Nhiệm vụ {t_key} cần xử lý"
            body = (
                f"Chào anh Cuong,\n\n"
                f"Email nhắc nhở: nhờ anh Cuong tiếp tục hoàn thành task {t_key} trước 17:00 ngày 2026-10-{due_day:02d}.\n\n"
                f"Regards,\n{author['canonical_name']}"
            )
            payload = {"subject": subj, "body": {"content": body}}
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://outlook/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.MS_OUTLOOK,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"{subj}: {body}",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, "hoàn thành"],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=author["canonical_id"],
                    expected_requester_name=author["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=True,
                        expected_anchors=[t_key],
                        expected_cluster_id=f"cluster-{t_key}",
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[f"task {t_key}"],
                    ),
                ),
            )

        else:
            # direct_task (default)
            subj = f"Yêu cầu xử lý task {t_key}: {topic['keywords'][0]}"
            body = (
                f"Chào anh Cuong,\n\n"
                f"Nhờ anh Cuong phụ trách thực hiện {topic['action_vi']} cho task {t_key} trước 17:00 ngày 2026-10-{due_day:02d}.\n"
                f"Mọi thắc mắc anh có thể trao đổi trực tiếp với team.\n\n"
                f"Best regards,\n{author['canonical_name']}"
            )
            payload = {"subject": subj, "body": {"content": body}}
            return BenchmarkSample(
                sample_id=sample_id,
                case_category="direct_task",
                origin_reference=f"ptb_500://outlook/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.MS_OUTLOOK,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"{subj}: {body}",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, topic["keywords"][0]],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=author["canonical_id"],
                    expected_requester_name=author["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=False,
                        expected_anchors=[t_key],
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[f"task {t_key}"],
                    ),
                ),
            )

    def _generate_slack_sample(
        self,
        index: int,
        category: str,
        topic: Dict[str, Any],
    ) -> BenchmarkSample:
        sample_id = f"ptb500-{index:03d}-slack-{category}"
        raw_id = f"raw-slack-{index:03d}"
        ext_id = f"ext-slack-{index:03d}"
        conv_id = f"C0123-{topic['topic']}"
        ts = f"2026-10-15T{8 + (index % 10):02d}:{index % 60:02d}:00Z"
        cap_ts = f"2026-10-15T{8 + (index % 10):02d}:{index % 60:02d}:05Z"
        author = self.rng.choice(self.other_personas)
        t_key = f"{topic['ticket_prefix']}-{300 + index}"
        due_day = 20 + (index % 10)
        due_dt = f"2026-10-{due_day:02d}T17:00:00Z"

        if category == "noise":
            noise_texts = [
                "Coffee break anyone? Heading downstairs in 5 mins :coffee:",
                "Hey team, does anyone have the Zoom link for the all-hands meeting?",
                "+1, totally agree with this proposal!",
                ":tada: Congrats to the team for the successful v1.0 release yesterday!",
                "Just pushed the latest branch, feel free to pull when ready.",
            ]
            text = self.rng.choice(noise_texts)
            payload = {"text": text, "user": author["canonical_id"]}
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://slack/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.SLACK,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=text,
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=False,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.NONE,
                    expected_title_keywords=[],
                    correlation=ExpectedCorrelationSpec(expected_merge=False),
                ),
            )

        elif category == "negation":
            text = f"<@{self.current_user['canonical_id']}> no need to deploy {t_key} on {topic['keywords'][0]}, client confirmed fixed on their side."
            payload = {"text": text, "user": author["canonical_id"]}
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://slack/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.SLACK,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=text,
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=False,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.NONE,
                    expected_title_keywords=[],
                    correlation=ExpectedCorrelationSpec(expected_merge=False),
                ),
            )

        elif category == "ambiguous":
            text = f"<@{self.current_user['canonical_id']}> maybe we should consider refactoring {topic['component']} sometime in Q4."
            payload = {"text": text, "user": author["canonical_id"]}
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://slack/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.SLACK,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=text,
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=False,
                    clarity=ClarityLabel.AMBIGUOUS,
                    control_type=ControlType.NONE,
                    expected_title_keywords=[],
                    correlation=ExpectedCorrelationSpec(expected_merge=False),
                ),
            )

        elif category == "multilingual":
            text = f"<@{self.current_user['canonical_id']}> check giup em pull request {t_key} ve {topic['keywords'][0]} truoc 17:00 ngay 2026-10-{due_day:02d} nhe."
            payload = {"text": text, "user": author["canonical_id"]}
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://slack/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.SLACK,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=text,
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, topic["keywords"][0]],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=author["canonical_id"],
                    expected_requester_name=author["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=False,
                        expected_anchors=[t_key],
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[f"pull request {t_key}"],
                    ),
                ),
            )

        elif category == "near_duplicate":
            text = f"<@{self.current_user['canonical_id']}> second reminder on {t_key}: please finish {topic['keywords'][0]} before 17:00 on 2026-10-{due_day:02d}."
            payload = {"text": text, "user": author["canonical_id"]}
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://slack/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.SLACK,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=text,
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, topic["keywords"][0]],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=author["canonical_id"],
                    expected_requester_name=author["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=True,
                        expected_anchors=[t_key],
                        expected_cluster_id=f"cluster-{t_key}",
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[f"reminder on {t_key}"],
                    ),
                ),
            )

        elif category == "cross_source":
            text = f"Following up on Jira {t_key}: <@{self.current_user['canonical_id']}> please investigate {topic['keywords'][0]} issue before 17:00 on 2026-10-{due_day:02d}."
            payload = {"text": text, "user": author["canonical_id"]}
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://slack/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.SLACK,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=text,
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, topic["keywords"][0]],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=author["canonical_id"],
                    expected_requester_name=author["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=True,
                        expected_anchors=[t_key],
                        expected_cluster_id=f"cluster-{t_key}",
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[f"Jira {t_key}"],
                    ),
                ),
            )

        elif category == "attachments":
            text = f"Attached debug logs log_dump.txt: <@{self.current_user['canonical_id']}> please analyze error in {t_key} before 17:00 on 2026-10-{due_day:02d}."
            payload = {"text": text, "user": author["canonical_id"], "files": [{"name": "log_dump.txt"}]}
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://slack/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.SLACK,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=text,
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, "analyze"],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=author["canonical_id"],
                    expected_requester_name=author["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=False,
                        expected_anchors=[t_key],
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[f"error in {t_key}"],
                    ),
                ),
            )

        elif category == "date_parsing":
            has_deadline = (index % 2 == 0)
            if has_deadline:
                date_str = f"before 17:00 on 2026-10-{due_day:02d}"
                exp_due = due_dt
                exp_explicit = True
            else:
                date_str = "whenever you have spare time"
                exp_due = None
                exp_explicit = False
            text = f"<@{self.current_user['canonical_id']}> please fix {t_key} on {topic['component']} {date_str}."
            payload = {"text": text, "user": author["canonical_id"]}
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://slack/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.SLACK,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=text,
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, topic["component"]],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=author["canonical_id"],
                    expected_requester_name=author["canonical_name"],
                    expected_due_date=exp_due,
                    expected_explicit_deadline=exp_explicit,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=False,
                        expected_anchors=[t_key],
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[f"fix {t_key}"],
                    ),
                ),
            )

        elif category == "quote_reply":
            text = f"I am on it, will resolve {t_key} before 17:00 on 2026-10-{due_day:02d}."
            payload = {
                "text": text,
                "user": self.current_user["canonical_id"],
                "thread_ts": ts,
                "ts": cap_ts,
                "parent_message": {"user": author["canonical_id"], "text": f"Please resolve {t_key}"},
            }
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://slack/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.SLACK,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=self.current_user["email"],
                    author_display_name=self.current_user["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"{author['canonical_name']}: Please resolve {t_key}\n{text}",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, "resolve"],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=author["canonical_id"],
                    expected_requester_name=author["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=False,
                        expected_anchors=[t_key],
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[f"resolve {t_key}"],
                    ),
                ),
            )

        else:
            # direct_task (default)
            text = f"<@{self.current_user['canonical_id']}> please {topic['action_en']} for task {t_key} before 17:00 on 2026-10-{due_day:02d}."
            payload = {"text": text, "user": author["canonical_id"]}
            return BenchmarkSample(
                sample_id=sample_id,
                case_category="direct_task",
                origin_reference=f"ptb_500://slack/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.SLACK,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=text,
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, topic["keywords"][0]],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=author["canonical_id"],
                    expected_requester_name=author["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=False,
                        expected_anchors=[t_key],
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[f"task {t_key}"],
                    ),
                ),
            )

    def _generate_jira_sample(
        self,
        index: int,
        category: str,
        topic: Dict[str, Any],
    ) -> BenchmarkSample:
        sample_id = f"ptb500-{index:03d}-jira-{category}"
        raw_id = f"raw-jira-{index:03d}"
        t_key = f"{topic['ticket_prefix']}-{400 + index}"
        ext_id = t_key
        conv_id = topic["ticket_prefix"]
        ts = f"2026-10-15T{8 + (index % 10):02d}:{index % 60:02d}:00Z"
        cap_ts = f"2026-10-15T{8 + (index % 10):02d}:{index % 60:02d}:05Z"
        reporter = self.rng.choice(self.other_personas)
        due_day = 21 + (index % 10)
        due_dt = f"2026-10-{due_day:02d}T18:00:00Z"

        if category == "noise":
            summary = f"{t_key}: Closed duplicate report for {topic['keywords'][0]}"
            payload = {
                "key": t_key,
                "fields": {
                    "summary": summary,
                    "status": {"name": "Closed"},
                    "resolution": {"name": "Won't Fix"},
                    "description": "This issue is closed as duplicate of existing work. Won't fix.",
                    "assignee": None,
                    "reporter": {"accountId": reporter["account_ids"][0], "displayName": reporter["canonical_name"]},
                },
            }
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://jira/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.JIRA,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=reporter["email"],
                    author_display_name=reporter["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"{summary}. Status: Closed, Won't Fix.",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=False,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.NONE,
                    expected_title_keywords=[],
                    correlation=ExpectedCorrelationSpec(expected_merge=False),
                ),
            )

        elif category == "negation":
            summary = f"{t_key}: Cancelled task on {topic['keywords'][0]}"
            payload = {
                "key": t_key,
                "fields": {
                    "summary": summary,
                    "status": {"name": "Cancelled"},
                    "description": f"Hủy task {t_key} do kế hoạch thay đổi.",
                    "assignee": None,
                    "reporter": {"accountId": reporter["account_ids"][0], "displayName": reporter["canonical_name"]},
                },
            }
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://jira/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.JIRA,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=reporter["email"],
                    author_display_name=reporter["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"Hủy task {t_key} do kế hoạch thay đổi.",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=False,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.NONE,
                    expected_title_keywords=[],
                    correlation=ExpectedCorrelationSpec(expected_merge=False),
                ),
            )

        elif category == "ambiguous":
            summary = f"{t_key}: Research spike for {topic['component']}"
            desc = "Open exploratory spike to study architectural feasibility without committed deadline."
            payload = {
                "key": t_key,
                "fields": {
                    "summary": summary,
                    "description": desc,
                    "status": {"name": "Open"},
                    "assignee": None,
                    "reporter": {"accountId": reporter["account_ids"][0], "displayName": reporter["canonical_name"]},
                },
            }
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://jira/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.JIRA,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=reporter["email"],
                    author_display_name=reporter["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"{summary}. {desc}",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=False,
                    clarity=ClarityLabel.AMBIGUOUS,
                    control_type=ControlType.NONE,
                    expected_title_keywords=[],
                    correlation=ExpectedCorrelationSpec(expected_merge=False),
                ),
            )

        elif category == "near_duplicate":
            summary = f"{t_key}: Duplicate issue on {topic['keywords'][0]} in {topic['component']}"
            desc = f"Identical to primary ticket {topic['ticket_prefix']}-401. Merging into primary anchor."
            payload = {
                "key": t_key,
                "fields": {
                    "summary": summary,
                    "description": desc,
                    "assignee": {"accountId": self.current_user["account_ids"][1], "displayName": self.current_user["canonical_name"]},
                    "reporter": {"accountId": reporter["account_ids"][0], "displayName": reporter["canonical_name"]},
                    "duedate": due_dt,
                    "status": {"name": "To Do"},
                },
            }
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://jira/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.JIRA,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=reporter["email"],
                    author_display_name=reporter["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"{summary}. {desc}",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, topic["keywords"][0]],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=reporter["canonical_id"],
                    expected_requester_name=reporter["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=True,
                        expected_anchors=[t_key],
                        expected_cluster_id=f"cluster-{t_key}",
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[summary],
                    ),
                ),
            )

        elif category == "cross_source":
            summary = f"{t_key}: Cross-source tracker for {topic['keywords'][0]}"
            desc = f"Main issue coordinating PR and chat discussion for {topic['action_vi']}."
            payload = {
                "key": t_key,
                "fields": {
                    "summary": summary,
                    "description": desc,
                    "assignee": {"accountId": self.current_user["account_ids"][1], "displayName": self.current_user["canonical_name"]},
                    "reporter": {"accountId": reporter["account_ids"][0], "displayName": reporter["canonical_name"]},
                    "duedate": due_dt,
                    "status": {"name": "In Progress"},
                },
            }
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://jira/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.JIRA,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=reporter["email"],
                    author_display_name=reporter["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"{summary}. {desc}",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, topic["keywords"][0]],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=reporter["canonical_id"],
                    expected_requester_name=reporter["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.IN_PROGRESS,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=True,
                        expected_anchors=[t_key],
                        expected_cluster_id=f"cluster-{t_key}",
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[summary],
                    ),
                ),
            )

        elif category == "attachments":
            summary = f"{t_key}: Triage crash report with attached logs"
            desc = f"Attached file dump_{t_key}.log. Cần xử lý {topic['keywords'][0]} trước 18:00 ngày 2026-10-{due_day:02d}."
            payload = {
                "key": t_key,
                "fields": {
                    "summary": summary,
                    "description": desc,
                    "assignee": {"accountId": self.current_user["account_ids"][1], "displayName": self.current_user["canonical_name"]},
                    "reporter": {"accountId": reporter["account_ids"][0], "displayName": reporter["canonical_name"]},
                    "duedate": due_dt,
                    "status": {"name": "To Do"},
                    "attachment": [{"filename": f"dump_{t_key}.log", "size": 20480}],
                },
            }
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://jira/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.JIRA,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=reporter["email"],
                    author_display_name=reporter["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"{summary}. {desc}",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, "crash"],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=reporter["canonical_id"],
                    expected_requester_name=reporter["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=False,
                        expected_anchors=[t_key],
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[summary],
                    ),
                ),
            )

        elif category == "date_parsing":
            has_deadline = (index % 2 == 0)
            if has_deadline:
                date_val = due_dt
                exp_explicit = True
            else:
                date_val = None
                exp_explicit = False
            summary = f"{t_key}: Deadline evaluation for {topic['keywords'][0]}"
            desc = f"Execute tasks for {topic['component']} with duedate set in Jira fields."
            payload = {
                "key": t_key,
                "fields": {
                    "summary": summary,
                    "description": desc,
                    "assignee": {"accountId": self.current_user["account_ids"][1], "displayName": self.current_user["canonical_name"]},
                    "reporter": {"accountId": reporter["account_ids"][0], "displayName": reporter["canonical_name"]},
                    "duedate": date_val,
                    "status": {"name": "To Do"},
                },
            }
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://jira/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.JIRA,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=reporter["email"],
                    author_display_name=reporter["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"{summary}. {desc}",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, topic["keywords"][0]],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=reporter["canonical_id"],
                    expected_requester_name=reporter["canonical_name"],
                    expected_due_date=date_val,
                    expected_explicit_deadline=exp_explicit,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=False,
                        expected_anchors=[t_key],
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[summary],
                    ),
                ),
            )

        else:
            # issue_tracking / direct_task (default)
            summary = f"{t_key}: Implement {topic['keywords'][0]} in {topic['component']}"
            desc = f"Nhiệm vụ {t_key}: {topic['action_vi']} để bảo đảm hệ thống hoạt động ổn định."
            payload = {
                "key": t_key,
                "fields": {
                    "summary": summary,
                    "description": desc,
                    "assignee": {
                        "accountId": self.current_user["account_ids"][1],
                        "displayName": self.current_user["canonical_name"],
                    },
                    "reporter": {
                        "accountId": reporter["account_ids"][0],
                        "displayName": reporter["canonical_name"],
                    },
                    "duedate": due_dt,
                    "status": {"name": "To Do"},
                    "priority": {"name": "High"},
                },
            }
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://jira/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.JIRA,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=reporter["email"],
                    author_display_name=reporter["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"{summary}. {desc}",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, topic["keywords"][0]],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=reporter["canonical_id"],
                    expected_requester_name=reporter["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=False,
                        expected_anchors=[t_key],
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[summary],
                    ),
                ),
            )

    def _generate_github_sample(
        self,
        index: int,
        category: str,
        topic: Dict[str, Any],
    ) -> BenchmarkSample:
        sample_id = f"ptb500-{index:03d}-github-{category}"
        raw_id = f"raw-github-{index:03d}"
        pr_number = 500 + index
        ext_id = f"PR-{pr_number}"
        conv_id = "repo-ptb-core"
        ts = f"2026-10-15T{8 + (index % 10):02d}:{index % 60:02d}:00Z"
        cap_ts = f"2026-10-15T{8 + (index % 10):02d}:{index % 60:02d}:05Z"
        author = self.rng.choice(self.other_personas)
        t_key = f"{topic['ticket_prefix']}-{500 + index}"
        due_day = 22 + (index % 9)
        due_dt = f"2026-10-{due_day:02d}T17:00:00Z"

        if category == "noise":
            title = f"chore(deps): bump library versions for {topic['keywords'][0]}"
            payload = {
                "type": "pull_request",
                "number": pr_number,
                "title": title,
                "state": "closed",
                "body": "Automated Dependabot bump closed as duplicate.",
                "user": {"login": "dependabot[bot]"},
            }
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://github/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.GITHUB,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id="dependabot[bot]@github.internal",
                    author_display_name="Dependabot",
                    raw_payload=payload,
                    normalized_text=f"[GitHub Pull Request #{pr_number}] {title}\nAuthor: dependabot | State: CLOSED",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=False,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.NONE,
                    expected_title_keywords=[],
                    correlation=ExpectedCorrelationSpec(expected_merge=False),
                ),
            )

        elif category == "ambiguous":
            title = f"rfc: explore potential architecture changes in {topic['component']}"
            body = "Open discussion on future architecture options. No immediate commitment or deadline."
            payload = {
                "type": "issue",
                "number": pr_number,
                "title": title,
                "state": "open",
                "body": body,
                "user": {"login": author["account_ids"][3]},
            }
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://github/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.GITHUB,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"[GitHub Issue #{pr_number}] {title}\nDescription:\n{body}",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=False,
                    clarity=ClarityLabel.AMBIGUOUS,
                    control_type=ControlType.NONE,
                    expected_title_keywords=[],
                    correlation=ExpectedCorrelationSpec(expected_merge=False),
                ),
            )

        elif category == "cross_source":
            title = f"feat({topic['topic']}): resolves {t_key} {topic['keywords'][0]}"
            body = f"Resolves issue {t_key}. Assignee: {self.current_user['canonical_name']}. Target deadline: 2026-10-{due_day:02d} 17:00."
            payload = {
                "type": "pull_request",
                "number": pr_number,
                "title": title,
                "state": "open",
                "body": body,
                "user": {"login": author["account_ids"][3]},
                "assignees": [{"login": self.current_user["account_ids"][3]}],
            }
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://github/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.GITHUB,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"[GitHub Pull Request #{pr_number}] {title}\nDescription:\n{body}",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, topic["keywords"][0]],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=author["canonical_id"],
                    expected_requester_name=author["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.IN_PROGRESS,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=True,
                        expected_anchors=[t_key],
                        expected_cluster_id=f"cluster-{t_key}",
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[f"Resolves issue {t_key}"],
                    ),
                ),
            )

        elif category == "near_duplicate":
            title = f"fix({topic['topic']}): duplicate branch patch for {t_key}"
            body = f"Secondary pull request for issue {t_key}. Needs review by {self.current_user['canonical_name']} before 2026-10-{due_day:02d} 17:00."
            payload = {
                "type": "pull_request",
                "number": pr_number,
                "title": title,
                "state": "open",
                "body": body,
                "user": {"login": author["account_ids"][3]},
                "assignees": [{"login": self.current_user["account_ids"][3]}],
            }
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://github/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.GITHUB,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"[GitHub Pull Request #{pr_number}] {title}\nDescription:\n{body}",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, topic["keywords"][0]],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=author["canonical_id"],
                    expected_requester_name=author["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=True,
                        expected_anchors=[t_key],
                        expected_cluster_id=f"cluster-{t_key}",
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[f"issue {t_key}"],
                    ),
                ),
            )

        elif category == "multilingual":
            title = f"fix({topic['topic']}): sua loi {topic['keywords'][0]} tren {topic['component']}"
            body = f"Nho anh Cuong review va merge truoc 17:00 ngay 2026-10-{due_day:02d}."
            payload = {
                "type": "pull_request",
                "number": pr_number,
                "title": title,
                "state": "open",
                "body": body,
                "user": {"login": author["account_ids"][3]},
                "assignees": [{"login": self.current_user["account_ids"][3]}],
            }
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://github/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.GITHUB,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"[GitHub Pull Request #{pr_number}] {title}\nDescription:\n{body}",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[topic["keywords"][0], "review"],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=author["canonical_id"],
                    expected_requester_name=author["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=False,
                        expected_anchors=[f"PR-{pr_number}"],
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=["review va merge"],
                    ),
                ),
            )

        elif category == "attachments":
            title = f"bug: memory leak profile report for {topic['component']}"
            body = (
                f"Attached profile screenshot memory_leak_{t_key}.png.\n"
                f"Cuong Dam needs to analyze and fix before 17:00 on 2026-10-{due_day:02d}."
            )
            payload = {
                "type": "issue",
                "number": pr_number,
                "title": title,
                "state": "open",
                "body": body,
                "user": {"login": author["account_ids"][3]},
                "assignees": [{"login": self.current_user["account_ids"][3]}],
            }
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://github/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.GITHUB,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"[GitHub Issue #{pr_number}] {title}\nDescription:\n{body}",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[topic["component"], "memory"],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=author["canonical_id"],
                    expected_requester_name=author["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=False,
                        expected_anchors=[f"PR-{pr_number}"],
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=["analyze and fix"],
                    ),
                ),
            )

        elif category == "date_parsing":
            has_deadline = (index % 2 == 0)
            if has_deadline:
                date_str = f"before 17:00 on 2026-10-{due_day:02d}"
                exp_due = due_dt
                exp_explicit = True
            else:
                date_str = "without strict target release milestone"
                exp_due = None
                exp_explicit = False
            title = f"task({topic['topic']}): refactor {topic['component']} handlers"
            body = f"Assigned to {self.current_user['canonical_name']} {date_str}."
            payload = {
                "type": "issue",
                "number": pr_number,
                "title": title,
                "state": "open",
                "body": body,
                "user": {"login": author["account_ids"][3]},
                "assignees": [{"login": self.current_user["account_ids"][3]}],
            }
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://github/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.GITHUB,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"[GitHub Issue #{pr_number}] {title}\nDescription:\n{body}",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[topic["component"], "refactor"],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=author["canonical_id"],
                    expected_requester_name=author["canonical_name"],
                    expected_due_date=exp_due,
                    expected_explicit_deadline=exp_explicit,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=False,
                        expected_anchors=[f"PR-{pr_number}"],
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=["Assigned to"],
                    ),
                ),
            )

        else:
            # issue_tracking / direct_task (default)
            title = f"fix({topic['topic']}): resolve {topic['keywords'][0]} bug in {topic['component']}"
            body = (
                f"Assigned to {self.current_user['canonical_name']} for {topic['action_en']}.\n"
                f"Please complete review before 17:00 on 2026-10-{due_day:02d}."
            )
            payload = {
                "type": "pull_request",
                "number": pr_number,
                "title": title,
                "state": "open",
                "body": body,
                "user": {"login": author["account_ids"][3]},
                "assignees": [{"login": self.current_user["account_ids"][3]}],
            }
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://github/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.GITHUB,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"[GitHub Pull Request #{pr_number}] {title}\nDescription:\n{body}",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[topic["keywords"][0], "bug"],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=author["canonical_id"],
                    expected_requester_name=author["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=False,
                        expected_anchors=[f"PR-{pr_number}"],
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[f"resolve {topic['keywords'][0]} bug"],
                    ),
                ),
            )

    def _generate_confluence_sample(
        self,
        index: int,
        category: str,
        topic: Dict[str, Any],
    ) -> BenchmarkSample:
        sample_id = f"ptb500-{index:03d}-confluence-{category}"
        raw_id = f"raw-confluence-{index:03d}"
        ext_id = f"conf-{600 + index}"
        conv_id = "SPACE-ENG"
        ts = f"2026-10-15T{8 + (index % 10):02d}:{index % 60:02d}:00Z"
        cap_ts = f"2026-10-15T{8 + (index % 10):02d}:{index % 60:02d}:05Z"
        author = self.rng.choice(self.other_personas)
        t_key = f"{topic['ticket_prefix']}-{600 + index}"
        due_day = 23 + (index % 8)
        due_dt = f"2026-10-{due_day:02d}T17:00:00Z"

        if category == "noise":
            title = f"Architecture Guidelines: Best practices for {topic['keywords'][0]}"
            body = (
                f"<p>Tài liệu hướng dẫn quy chuẩn kiến trúc khi làm việc với {topic['component']}.</p>"
                f"<p>Tài liệu này dùng cho mục đích tham khảo kỹ thuật chung, không có action item tồn đọng.</p>"
            )
            payload = {
                "title": title,
                "space": "ENG",
                "author": author["canonical_name"],
                "body": {"storage": {"value": body}},
            }
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://confluence/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.CONFLUENCE,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"[Confluence: {title}] (Space: ENG)\nDocument Content:\n{title}. Tài liệu dùng cho mục đích tham khảo kỹ thuật.",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=False,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.NONE,
                    expected_title_keywords=[],
                    correlation=ExpectedCorrelationSpec(expected_merge=False),
                ),
            )

        elif category == "negation":
            title = f"Meeting Minutes: Hủy bỏ đợt triển khai {t_key}"
            body = (
                f"<p>Quyết định trong cuộc họp: hủy bỏ triển khai task {t_key} {topic['keywords'][0]}.</p>"
                f"<p>Team sẽ chuyển hướng sang giải pháp thay thế trong quý tới.</p>"
            )
            payload = {
                "title": title,
                "space": "ENG",
                "author": author["canonical_name"],
                "body": {"storage": {"value": body}},
            }
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://confluence/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.CONFLUENCE,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"[Confluence: {title}]\nQuyết định: hủy bỏ triển khai task {t_key}.",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=False,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.NONE,
                    expected_title_keywords=[],
                    correlation=ExpectedCorrelationSpec(expected_merge=False),
                ),
            )

        elif category == "ambiguous":
            title = f"RFC Brainstorming: Future tech radar for {topic['component']}"
            body = "<p>Tài liệu ghi chép các ý tưởng tiềm năng, chưa có kế hoạch hoặc cam kết triển khai cụ thể.</p>"
            payload = {
                "title": title,
                "space": "ENG",
                "author": author["canonical_name"],
                "body": {"storage": {"value": body}},
            }
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://confluence/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.CONFLUENCE,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=f"[Confluence: {title}] (Space: ENG)\nDocument Content:\nÝ tưởng tiềm năng, chưa có cam kết.",
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=False,
                    clarity=ClarityLabel.AMBIGUOUS,
                    control_type=ControlType.NONE,
                    expected_title_keywords=[],
                    correlation=ExpectedCorrelationSpec(expected_merge=False),
                ),
            )

        elif category == "attachments":
            title = f"Architecture Spec: {topic['topic']} Technical Blueprint"
            action_snippet = f"Cuong Dam kiểm tra sơ đồ đính kèm và triển khai {t_key} trước 17:00 ngày 2026-10-{due_day:02d}."
            body = (
                f"<p>Tài liệu kèm file đính kèm architecture_blueprint_{t_key}.drawio:</p>"
                f"<ac:task-list>"
                f"<ac:task><ac:task-status>incomplete</ac:task-status><ac:task-body>{action_snippet}</ac:task-body></ac:task>"
                f"</ac:task-list>"
            )
            payload = {
                "title": title,
                "space": "ENG",
                "author": author["canonical_name"],
                "body": {"storage": {"value": body}},
                "attachments": [{"title": f"architecture_blueprint_{t_key}.drawio"}],
            }
            norm_text = (
                f"[Confluence: {title}] (Space: ENG)\n"
                f"Action Items Extracted:\n• [Action Item (incomplete)] {action_snippet}"
            )
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://confluence/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.CONFLUENCE,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=norm_text,
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, "kiểm tra"],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=author["canonical_id"],
                    expected_requester_name=author["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=False,
                        expected_anchors=[t_key],
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[action_snippet],
                    ),
                ),
            )

        elif category == "cross_source":
            title = f"Cross-Team Alignment: Status of {t_key}"
            action_snippet = f"Cuong Dam cập nhật tiến độ liên kênh cho {t_key} trước 17:00 ngày 2026-10-{due_day:02d}."
            body = (
                f"<p>Biên bản thống nhất đồng bộ liên kênh cho task {t_key}:</p>"
                f"<ac:task-list>"
                f"<ac:task><ac:task-status>incomplete</ac:task-status><ac:task-body>{action_snippet}</ac:task-body></ac:task>"
                f"</ac:task-list>"
            )
            payload = {
                "title": title,
                "space": "ENG",
                "author": author["canonical_name"],
                "body": {"storage": {"value": body}},
            }
            norm_text = (
                f"[Confluence: {title}] (Space: ENG)\n"
                f"Action Items Extracted:\n• [Action Item (incomplete)] {action_snippet}"
            )
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://confluence/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.CONFLUENCE,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=norm_text,
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, topic["keywords"][0]],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=author["canonical_id"],
                    expected_requester_name=author["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=True,
                        expected_anchors=[t_key],
                        expected_cluster_id=f"cluster-{t_key}",
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[action_snippet],
                    ),
                ),
            )

        elif category == "near_duplicate":
            title = f"Meeting Follow-up: Reiterate {t_key} action items"
            action_snippet = f"Nhắc lại: Cuong Dam hoàn thiện task {t_key} trước 17:00 ngày 2026-10-{due_day:02d}."
            body = (
                f"<p>Biên bản nhắc lại hành động cần làm:</p>"
                f"<ac:task-list>"
                f"<ac:task><ac:task-status>incomplete</ac:task-status><ac:task-body>{action_snippet}</ac:task-body></ac:task>"
                f"</ac:task-list>"
            )
            payload = {
                "title": title,
                "space": "ENG",
                "author": author["canonical_name"],
                "body": {"storage": {"value": body}},
            }
            norm_text = (
                f"[Confluence: {title}] (Space: ENG)\n"
                f"Action Items Extracted:\n• [Action Item (incomplete)] {action_snippet}"
            )
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://confluence/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.CONFLUENCE,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=norm_text,
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, topic["keywords"][0]],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=author["canonical_id"],
                    expected_requester_name=author["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=True,
                        expected_anchors=[t_key],
                        expected_cluster_id=f"cluster-{t_key}",
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[action_snippet],
                    ),
                ),
            )

        elif category == "multilingual":
            title = f"Sprint Notes: Multilingual actions for {topic['topic']}"
            action_snippet = f"Cuong Dam deploy {t_key} len staging environment truoc 17:00 ngay 2026-10-{due_day:02d}."
            body = (
                f"<p>Multilingual sprint actions:</p>"
                f"<ac:task-list>"
                f"<ac:task><ac:task-status>incomplete</ac:task-status><ac:task-body>{action_snippet}</ac:task-body></ac:task>"
                f"</ac:task-list>"
            )
            payload = {
                "title": title,
                "space": "ENG",
                "author": author["canonical_name"],
                "body": {"storage": {"value": body}},
            }
            norm_text = (
                f"[Confluence: {title}] (Space: ENG)\n"
                f"Action Items Extracted:\n• [Action Item (incomplete)] {action_snippet}"
            )
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://confluence/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.CONFLUENCE,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=norm_text,
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, "deploy"],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=author["canonical_id"],
                    expected_requester_name=author["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=False,
                        expected_anchors=[t_key],
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[action_snippet],
                    ),
                ),
            )

        else:
            # issue_tracking / direct_task (action items)
            title = f"Meeting Notes: Sprint Planning for {topic['topic']}"
            action_snippet = f"Cuong Dam hoàn thành task {t_key}: {topic['action_vi']} trước 17:00 ngày 2026-10-{due_day:02d}."
            body = (
                f"<p>Biên bản cuộc họp Sprint Planning:</p>"
                f"<ac:task-list>"
                f"<ac:task><ac:task-status>incomplete</ac:task-status><ac:task-body>{action_snippet}</ac:task-body></ac:task>"
                f"</ac:task-list>"
            )
            payload = {
                "title": title,
                "space": "ENG",
                "author": author["canonical_name"],
                "body": {"storage": {"value": body}},
            }
            norm_text = (
                f"[Confluence: {title}] (Space: ENG)\n"
                f"Action Items Extracted:\n• [Action Item (incomplete)] {action_snippet}"
            )
            return BenchmarkSample(
                sample_id=sample_id,
                case_category=category,
                origin_reference=f"ptb_500://confluence/{index:03d}",
                raw_event=RawEventRecord(
                    id=raw_id,
                    tenant_id="tenant-fpt-internal",
                    source_type=SourceType.CONFLUENCE,
                    external_id=ext_id,
                    conversation_or_project_id=conv_id,
                    event_timestamp=ts,
                    captured_at=cap_ts,
                    author_external_id=author["email"],
                    author_display_name=author["canonical_name"],
                    raw_payload=payload,
                    normalized_text=norm_text,
                    idempotency_key=f"idemp-{raw_id}",
                ),
                ground_truth=GroundTruthSpec(
                    expected_is_task=True,
                    clarity=ClarityLabel.CLEAR,
                    control_type=ControlType.COMMITMENT,
                    expected_title_keywords=[t_key, topic["keywords"][0]],
                    expected_owner_id=self.current_user["canonical_id"],
                    expected_owner_name=self.current_user["canonical_name"],
                    expected_requester_id=author["canonical_id"],
                    expected_requester_name=author["canonical_name"],
                    expected_due_date=due_dt,
                    expected_explicit_deadline=True,
                    expected_status=TaskStatus.TODO,
                    correlation=ExpectedCorrelationSpec(
                        expected_merge=False,
                        expected_anchors=[t_key],
                    ),
                    evidence=ExpectedEvidenceSpec(
                        source_event_id=raw_id,
                        expected_snippet_substrings=[action_snippet],
                    ),
                ),
            )

    def generate_all_samples(self) -> List[BenchmarkSample]:
        """Generate exactly 475 samples matching the specified distribution matrix."""
        # Target counts per source and category
        # Total: 475 samples
        matrix: List[Tuple[str, str, int]] = [
            # ms_teams (85)
            ("ms_teams", "direct_task", 20),
            ("ms_teams", "noise", 12),
            ("ms_teams", "negation", 8),
            ("ms_teams", "ambiguous", 8),
            ("ms_teams", "multilingual", 10),
            ("ms_teams", "near_duplicate", 5),
            ("ms_teams", "date_parsing", 7),
            ("ms_teams", "quote_reply", 15),
            # ms_outlook (85)
            ("ms_outlook", "direct_task", 18),
            ("ms_outlook", "noise", 13),
            ("ms_outlook", "negation", 9),
            ("ms_outlook", "ambiguous", 8),
            ("ms_outlook", "multilingual", 10),
            ("ms_outlook", "attachments", 8),
            ("ms_outlook", "date_parsing", 8),
            ("ms_outlook", "quote_reply", 5),
            ("ms_outlook", "near_duplicate", 6),
            # slack (80)
            ("slack", "direct_task", 12),
            ("slack", "noise", 10),
            ("slack", "negation", 8),
            ("slack", "ambiguous", 9),
            ("slack", "multilingual", 10),
            ("slack", "near_duplicate", 4),
            ("slack", "cross_source", 5),
            ("slack", "attachments", 2),
            ("slack", "date_parsing", 10),
            ("slack", "quote_reply", 10),
            # jira (80)
            ("jira", "issue_tracking", 25),
            ("jira", "direct_task", 15),
            ("jira", "noise", 10),
            ("jira", "negation", 5),
            ("jira", "ambiguous", 5),
            ("jira", "near_duplicate", 5),
            ("jira", "cross_source", 5),
            ("jira", "attachments", 5),
            ("jira", "date_parsing", 5),
            # github (75)
            ("github", "issue_tracking", 20),
            ("github", "direct_task", 10),
            ("github", "cross_source", 10),
            ("github", "noise", 10),
            ("github", "near_duplicate", 5),
            ("github", "multilingual", 5),
            ("github", "attachments", 5),
            ("github", "date_parsing", 5),
            ("github", "ambiguous", 5),
            # confluence (70)
            ("confluence", "issue_tracking", 5),
            ("confluence", "direct_task", 10),
            ("confluence", "noise", 15),
            ("confluence", "attachments", 10),
            ("confluence", "cross_source", 10),
            ("confluence", "near_duplicate", 5),
            ("confluence", "multilingual", 5),
            ("confluence", "ambiguous", 5),
            ("confluence", "negation", 5),
        ]

        total_requested = sum(count for _, _, count in matrix)
        assert total_requested == 475, f"Expected 475 samples, but matrix sums to {total_requested}"

        samples: List[BenchmarkSample] = []
        global_index = 1

        for source_name, category, count in matrix:
            for _ in range(count):
                topic = TECHNICAL_DOMAINS[(global_index - 1) % len(TECHNICAL_DOMAINS)]
                if source_name == "ms_teams":
                    s = self._generate_teams_sample(global_index, category, topic)
                elif source_name == "ms_outlook":
                    s = self._generate_outlook_sample(global_index, category, topic)
                elif source_name == "slack":
                    s = self._generate_slack_sample(global_index, category, topic)
                elif source_name == "jira":
                    s = self._generate_jira_sample(global_index, category, topic)
                elif source_name == "github":
                    s = self._generate_github_sample(global_index, category, topic)
                elif source_name == "confluence":
                    s = self._generate_confluence_sample(global_index, category, topic)
                else:
                    raise ValueError(f"Unknown source: {source_name}")

                samples.append(s)
                global_index += 1

        assert len(samples) == 475, f"Expected 475 samples generated, got {len(samples)}"
        return samples

    def assign_splits(
        self,
        samples: List[BenchmarkSample],
    ) -> Tuple[List[BenchmarkSample], List[BenchmarkSample], List[BenchmarkSample]]:
        """Stratified assignment of 475 samples into Dev (235), Calibration (95), Test (145)."""
        # Bucket by (source_type, case_category) to preserve balance
        buckets: Dict[Tuple[str, str], List[BenchmarkSample]] = {}
        for s in samples:
            src = (
                s.raw_event.source_type.value
                if hasattr(s.raw_event.source_type, "value")
                else str(s.raw_event.source_type)
            )
            key = (src, s.case_category)
            buckets.setdefault(key, []).append(s)

        dev_samples: List[BenchmarkSample] = []
        calib_samples: List[BenchmarkSample] = []
        test_samples: List[BenchmarkSample] = []

        # Target distribution:
        # Dev: ~49.5% (235)
        # Calib: ~20.0% (95)
        # Test: ~30.5% (145)
        for key in sorted(buckets.keys()):
            bucket = buckets[key]
            for i, s in enumerate(bucket):
                pattern_idx = i % 10
                if pattern_idx in (0, 1, 3, 5, 6):
                    target = "dev"
                elif pattern_idx in (2, 7):
                    target = "calibration"
                else:
                    target = "test"

                s.origin_reference = f"ptb_500://{target}/{key[0]}/{s.sample_id}"

                if target == "dev":
                    dev_samples.append(s)
                elif target == "calibration":
                    calib_samples.append(s)
                else:
                    test_samples.append(s)

        # Fine-tune boundaries to reach exact target counts (235, 95, 145)
        while len(dev_samples) > 235:
            item = dev_samples.pop()
            if len(calib_samples) < 95:
                item.origin_reference = item.origin_reference.replace("/dev/", "/calibration/")
                calib_samples.append(item)
            else:
                item.origin_reference = item.origin_reference.replace("/dev/", "/test/")
                test_samples.append(item)

        while len(dev_samples) < 235:
            if len(test_samples) > 145:
                item = test_samples.pop()
                item.origin_reference = item.origin_reference.replace("/test/", "/dev/")
                dev_samples.append(item)
            elif len(calib_samples) > 95:
                item = calib_samples.pop()
                item.origin_reference = item.origin_reference.replace("/calibration/", "/dev/")
                dev_samples.append(item)

        while len(calib_samples) > 95:
            item = calib_samples.pop()
            item.origin_reference = item.origin_reference.replace("/calibration/", "/test/")
            test_samples.append(item)

        while len(calib_samples) < 95 and len(test_samples) > 145:
            item = test_samples.pop()
            item.origin_reference = item.origin_reference.replace("/test/", "/calibration/")
            calib_samples.append(item)

        assert len(dev_samples) == 235, f"Dev split expected 235, got {len(dev_samples)}"
        assert len(calib_samples) == 95, f"Calibration split expected 95, got {len(calib_samples)}"
        assert len(test_samples) == 145, f"Test split expected 145, got {len(test_samples)}"

        return dev_samples, calib_samples, test_samples

    def generate_probes(self) -> List[ProbeSpec]:
        """Deserialize and return 25 fault injection probe specifications."""
        return [ProbeSpec.model_validate(p) for p in PROBES_SPECIFICATION]

    def build_and_save_dataset(
        self,
        output_dir: Path,
    ) -> DatasetManifest:
        """Execute full generation, write files, compute checksums, and validate manifest."""
        output_dir.mkdir(parents=True, exist_ok=True)

        samples = self.generate_all_samples()
        dev_samples, calib_samples, test_samples = self.assign_splits(samples)
        probes = self.generate_probes()

        # 1. Write dataset json files
        samples_file = output_dir / "samples.json"
        dev_file = output_dir / "dev_samples.json"
        calib_file = output_dir / "calibration_samples.json"
        test_file = output_dir / "test_samples.json"
        probes_file = output_dir / "probes.json"
        splits_file = output_dir / "splits.json"

        # Serialize with newline formatting
        samples_file.write_text(
            json.dumps([s.model_dump(mode="json") for s in samples], indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        dev_file.write_text(
            json.dumps([s.model_dump(mode="json") for s in dev_samples], indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        calib_file.write_text(
            json.dumps([s.model_dump(mode="json") for s in calib_samples], indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        test_file.write_text(
            json.dumps([s.model_dump(mode="json") for s in test_samples], indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        probes_file.write_text(
            json.dumps([p.model_dump(mode="json") for p in probes], indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        # Build splits summary metadata
        splits_metadata = {
            "dataset_name": "PTB-BENCH-500",
            "version": "1.0.0",
            "total_samples": len(samples),
            "splits": {
                "dev": {
                    "count": len(dev_samples),
                    "file": "dev_samples.json",
                    "description": "Development and prompt tuning split (safe for iterative training/tuning)",
                },
                "calibration": {
                    "count": len(calib_samples),
                    "file": "calibration_samples.json",
                    "description": "Confidence threshold and temperature calibration split",
                },
                "test": {
                    "count": len(test_samples),
                    "file": "test_samples.json",
                    "description": "LOCKED final evaluation split (STRICTLY FORBIDDEN from tuning/training)",
                },
            },
        }
        splits_file.write_text(json.dumps(splits_metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        # 2. Compute cryptographic checksums
        file_checksums = generate_manifest_checksums(output_dir)

        # 3. Assemble probe groups
        probe_groups: Dict[str, List[str]] = {}
        for p in probes:
            probe_groups.setdefault(p.probe_group, []).append(p.probe_id)

        # 4. Assemble manifest
        manifest = DatasetManifest(
            dataset_name="PTB-BENCH-500",
            version="1.0.0",
            commit_baseline="79a41665d841067331608c9b3a93983f0d147bd8",
            created_at=datetime(2026, 10, 15, 10, 0, 0, tzinfo=timezone.utc),
            evaluation_now=datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc),
            samples_sha256=file_checksums["samples.json"].sha256,
            probes_sha256=file_checksums["probes.json"].sha256,
            dataset_files=file_checksums,
            allowed_email_domains=ALLOWED_EMAIL_DOMAINS,
            supported_controls=["COMMITMENT", "NONE", "DECISION", "LESSON"],
            personas=[PersonaSpec.model_validate(p) for p in PERSONAS],
            probe_groups=probe_groups,
        )

        manifest_file = output_dir / "manifest.json"
        manifest_file.write_text(
            json.dumps(manifest.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        return manifest


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dataset_generator",
        description="Generate deterministic PTB-Bench-500 dataset with cryptographic manifest.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="benchmarks/dataset/ptb_500",
        help="Target output directory (default: benchmarks/dataset/ptb_500)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Deterministic PRNG seed (default: 42)",
    )
    args = parser.parse_args(argv)

    out_path = Path(args.output_dir)
    print(f"[*] Generating PTB-Bench-500 dataset (seed={args.seed})...")
    generator = PTB500Generator(seed=args.seed)
    manifest = generator.build_and_save_dataset(out_path)
    print(f"[+] Dataset generated successfully in {out_path}")
    print(f"    - Manifest: {manifest.dataset_name} (v{manifest.version})")
    print(f"    - Files written:")
    for fname, check in manifest.dataset_files.items():
        print(f"      * {fname:<25s}: {check.item_count or 0:3d} items ({check.size_bytes or 0:7d} bytes)")

    # Run internal validation
    from benchmarks.tools.validate_manifest import run_full_validation
    print("\n[*] Validating generated manifest and ground truth invariants...")
    val_res = run_full_validation(out_path)
    return val_res


if __name__ == "__main__":
    sys.exit(main())
