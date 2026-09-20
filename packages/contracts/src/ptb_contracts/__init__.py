"""Shared contracts and data transfer objects for Personal Task Board."""

from ptb_contracts.l1_acquisition import (
    ProcessingStatus,
    RawEventRecord,
    SourceConnectionConfig,
    SourceType,
    SyncCheckpointState,
)
from ptb_contracts.l2_processing import (
    EvidenceRecord,
    EvidenceType,
    ExtractedCommitment,
    ParsedMessageContent,
    ReviewQueueItem,
    TaskStatus,
    UnifiedTaskCandidate,
)
from ptb_contracts.l3_storage import (
    CanonicalPersonRecord,
    GraphActionType,
    GraphNeighborhoodQuery,
    GraphOutboxEventPayload,
    SourceIdentityRecord,
)
from ptb_contracts.l4_intelligence import (
    DecisionRecord,
    ForgottenCommitmentItem,
    GraphRelationInfo,
    LessonRecord,
    PriorityBreakdown,
    TaskWithContext,
    TodayBoardView,
    TodayTaskItem,
    WaitingOnItem,
)
from ptb_contracts.l5_experience import (
    CoverageStatusResponse,
    CoverageTenantStatus,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    MCPToolCallRequest,
    MCPToolCallResult,
    ReviewActionResponse,
    TaskActionResponse,
)

__all__ = [
    # L1
    "SourceType",
    "ProcessingStatus",
    "RawEventRecord",
    "SourceConnectionConfig",
    "SyncCheckpointState",
    # L2
    "TaskStatus",
    "EvidenceType",
    "ParsedMessageContent",
    "EvidenceRecord",
    "ExtractedCommitment",
    "UnifiedTaskCandidate",
    "ReviewQueueItem",
    # L3
    "GraphActionType",
    "CanonicalPersonRecord",
    "SourceIdentityRecord",
    "GraphOutboxEventPayload",
    "GraphNeighborhoodQuery",
    # L4
    "GraphRelationInfo",
    "TaskWithContext",
    "PriorityBreakdown",
    "TodayTaskItem",
    "ForgottenCommitmentItem",
    "WaitingOnItem",
    "TodayBoardView",
    "DecisionRecord",
    "LessonRecord",
    # L5
    "TaskActionResponse",
    "ReviewActionResponse",
    "CoverageTenantStatus",
    "CoverageStatusResponse",
    "KnowledgeSearchRequest",
    "KnowledgeSearchResponse",
    "MCPToolCallRequest",
    "MCPToolCallResult",
]
