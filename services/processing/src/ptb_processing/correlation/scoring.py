"""CorrelationScorer: Evaluates correlation confidence and merge decisions between task candidates."""

from dataclasses import dataclass, field
import difflib
import re
import unicodedata
from typing import List, Optional, Set

from ptb_contracts.l2_processing import UnifiedTaskCandidate

# Thresholds frozen in docs/v1.md
ANCHOR_MERGE_THRESHOLD = 0.55
SEMANTIC_MERGE_THRESHOLD = 0.70


@dataclass
class CorrelationScoreResult:
    """Detailed score result of comparing an incoming candidate with an existing task."""

    correlation_confidence: float
    has_deterministic_anchor: bool
    matching_anchors: List[str] = field(default_factory=list)
    semantic_similarity: float = 0.0
    should_merge: bool = False
    target_task_id: Optional[str] = None
    has_anchor_conflict: bool = False
    reason: str = ""


class CorrelationScorer:
    """Scorer calculating correlation confidence independent of extraction confidence.

    Rules from docs/v1.md:
    - With Deterministic Anchor: merge threshold >= 0.55 -> AUTO-MERGE.
    - Without Deterministic Anchor (semantic text similarity only): merge threshold >= 0.70 -> AUTO-MERGE.
    - Below threshold -> keep as independent candidate.
    """

    def __init__(
        self,
        anchor_threshold: float = ANCHOR_MERGE_THRESHOLD,
        semantic_threshold: float = SEMANTIC_MERGE_THRESHOLD,
    ) -> None:
        self.anchor_threshold = anchor_threshold
        self.semantic_threshold = semantic_threshold

    @staticmethod
    def normalize_text(text: str) -> str:
        """Normalize string: unicode NFKD, lowercase, remove special characters."""
        if not text:
            return ""
        # Normalize unicode
        text = unicodedata.normalize("NFKD", text)
        text = text.lower()
        # Replace non-alphanumeric (except standard spaces) with space
        text = re.sub(r"[^\w\s]", " ", text)
        # Collapse multiple whitespaces
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @classmethod
    def tokenize(cls, text: str) -> List[str]:
        """Tokenize normalized text into words."""
        normalized = cls.normalize_text(text)
        if not normalized:
            return []
        return [t for t in normalized.split(" ") if t]

    def calculate_semantic_similarity(self, text1: str, text2: str) -> float:
        """Calculate semantic/text similarity between two texts.

        Uses combination of token Jaccard similarity and sequence matcher ratio.
        """
        if not text1 or not text2:
            return 0.0

        norm1 = self.normalize_text(text1)
        norm2 = self.normalize_text(text2)

        if norm1 == norm2 and norm1:
            return 1.0

        tokens1 = set(self.tokenize(text1))
        tokens2 = set(self.tokenize(text2))

        if not tokens1 or not tokens2:
            return 0.0

        # Jaccard similarity
        intersection = len(tokens1 & tokens2)
        union = len(tokens1 | tokens2)
        jaccard = intersection / union if union > 0 else 0.0

        # Sequence matcher ratio on normalized text
        seq_ratio = difflib.SequenceMatcher(None, norm1, norm2).ratio()

        # Combine: take max to favor strong token overlap or high substring similarity
        combined = max(jaccard, seq_ratio)
        return round(float(combined), 4)

    def score(
        self,
        candidate: UnifiedTaskCandidate,
        target_task: UnifiedTaskCandidate,
        matching_anchors: Optional[List[str]] = None,
        has_conflict: bool = False,
    ) -> CorrelationScoreResult:
        """Score candidate against target_task.

        Note: correlation_confidence is independent of extraction_confidence.
        """
        target_id = target_task.id
        matching_anchors = matching_anchors or []

        # 1. Conflict detection (e.g. different Jira keys)
        if has_conflict:
            return CorrelationScoreResult(
                correlation_confidence=0.0,
                has_deterministic_anchor=False,
                matching_anchors=[],
                semantic_similarity=0.0,
                should_merge=False,
                target_task_id=target_id,
                has_anchor_conflict=True,
                reason="Conflicting deterministic anchors (e.g. different ticket IDs). Disqualifying merge.",
            )

        # 2. Compute semantic similarity between titles (and snippets if title is minimal)
        title_sim = self.calculate_semantic_similarity(candidate.title, target_task.title)
        semantic_sim = title_sim

        # If candidate has snippets in evidences, check if snippet reinforces similarity
        if candidate.evidences and target_task.evidences:
            snippets_cand = " ".join(e.snippet for e in candidate.evidences if e.snippet)
            snippets_target = " ".join(e.snippet for e in target_task.evidences if e.snippet)
            if snippets_cand and snippets_target:
                snippet_sim = self.calculate_semantic_similarity(snippets_cand, snippets_target)
                # Boost if snippet shows strong overlap
                if snippet_sim > semantic_sim:
                    semantic_sim = round(0.7 * title_sim + 0.3 * snippet_sim, 4)

        # 3. Deterministic anchor branch
        if matching_anchors:
            has_anchor = True
            # Strong external key matches (Jira, Shortcut, PR, Commit, URL) provide high confidence
            is_strong_anchor = any(
                a.startswith(("jira:", "shortcut:", "pr:", "commit:", "url:", "OPS-", "PROJ-", "story-"))
                for a in matching_anchors
            )
            if is_strong_anchor:
                # Jira / Shortcut / PR match: 0.85 - 1.0 based on semantic similarity
                confidence = round(0.85 + 0.15 * semantic_sim, 4)
            else:
                # Thread ID or weaker anchor: 0.60 - 0.95
                confidence = round(0.60 + 0.35 * semantic_sim, 4)

            # Ensure minimum >= anchor_threshold if matching anchor is present
            confidence = max(self.anchor_threshold, confidence)
            should_merge = confidence >= self.anchor_threshold
            reason = (
                f"Deterministic anchor matched: {', '.join(matching_anchors)} "
                f"(correlation_confidence={confidence:.2f} >= threshold {self.anchor_threshold}) -> AUTO-MERGE"
            )
        else:
            # 4. Pure semantic similarity branch
            has_anchor = False
            confidence = semantic_sim
            should_merge = confidence >= self.semantic_threshold
            if should_merge:
                reason = (
                    f"Pure semantic similarity match: correlation_confidence={confidence:.2f} "
                    f">= threshold {self.semantic_threshold} -> AUTO-MERGE"
                )
            else:
                reason = (
                    f"Pure semantic similarity below threshold: correlation_confidence={confidence:.2f} "
                    f"< threshold {self.semantic_threshold} -> Keep as independent candidate"
                )

        return CorrelationScoreResult(
            correlation_confidence=confidence,
            has_deterministic_anchor=has_anchor,
            matching_anchors=matching_anchors,
            semantic_similarity=semantic_sim,
            should_merge=should_merge,
            target_task_id=target_id,
            has_anchor_conflict=False,
            reason=reason,
        )

    def should_merge(self, score_result: CorrelationScoreResult) -> bool:
        """Check whether score result qualifies for auto-merge."""
        return score_result.should_merge
