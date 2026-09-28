"""ptb_processing: Layer 2 Data Processing and Correlation Engine."""

from ptb_processing.correlation import (
    ANCHOR_MERGE_THRESHOLD,
    SEMANTIC_MERGE_THRESHOLD,
    CandidateMatch,
    CorrelationScoreResult,
    CorrelationScorer,
    DeterministicAnchor,
    TaskCandidateMatcher,
    TaskMerger,
)
from ptb_processing.extractor import (
    HeuristicCandidateFilter,
    LLMStructuredExtractor,
)
from ptb_processing.identity import (
    IdentityResolutionResult,
    IdentityResolver,
)
from ptb_processing.parsers import TeamsQuoteReplyParser
from ptb_processing.validator import (
    AttributionReport,
    AttributionValidator,
)

__all__ = [
    # Correlation & Merge
    "TaskCandidateMatcher",
    "DeterministicAnchor",
    "CandidateMatch",
    "CorrelationScorer",
    "CorrelationScoreResult",
    "ANCHOR_MERGE_THRESHOLD",
    "SEMANTIC_MERGE_THRESHOLD",
    "TaskMerger",
    # Parsers & Extractor & Identity & Validator
    "TeamsQuoteReplyParser",
    "HeuristicCandidateFilter",
    "LLMStructuredExtractor",
    "IdentityResolver",
    "IdentityResolutionResult",
    "AttributionValidator",
    "AttributionReport",
]
