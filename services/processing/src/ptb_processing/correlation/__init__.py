"""Layer 2 Correlation Engine: Task matching, confidence scoring, and auto-merge."""

from ptb_processing.correlation.candidate_matcher import (
    CandidateMatch,
    DeterministicAnchor,
    TaskCandidateMatcher,
)
from ptb_processing.correlation.merger import TaskMerger
from ptb_processing.correlation.scoring import (
    ANCHOR_MERGE_THRESHOLD,
    SEMANTIC_MERGE_THRESHOLD,
    CorrelationScoreResult,
    CorrelationScorer,
)

__all__ = [
    "TaskCandidateMatcher",
    "DeterministicAnchor",
    "CandidateMatch",
    "CorrelationScorer",
    "CorrelationScoreResult",
    "ANCHOR_MERGE_THRESHOLD",
    "SEMANTIC_MERGE_THRESHOLD",
    "TaskMerger",
]
