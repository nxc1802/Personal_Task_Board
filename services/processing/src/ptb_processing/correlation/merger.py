"""TaskMerger: Merges candidate tasks into target tasks without losing evidence provenance."""

from datetime import datetime, timezone
import logging
from typing import Any, List, Optional, Set
from uuid import uuid4

from ptb_contracts.l2_processing import (
    EvidenceRecord,
    MergeAuditRecord,
    UnifiedTaskCandidate,
)
from ptb_processing.correlation.candidate_matcher import TaskCandidateMatcher
from ptb_processing.correlation.scoring import CorrelationScoreResult

logger = logging.getLogger("ptb.processing.correlation.merger")


class TaskMerger:
    """Merges candidate task into target UnifiedTask while preserving 100% evidence provenance.

    Responsibilities according to docs/v1.md and Phase R6:
    1. Append new Evidence to target task (:HAS_EVIDENCE).
    2. Update updated_at and extend/refine deadline if candidate has a more specific or later deadline.
    3. Retain full provenance for every Evidence (pointing to original RawEvent) to support future Split-Task.
    4. Record and return complete audit trail metadata:
       - candidate_task_ids
       - winning_task_id
       - correlation_score
       - deterministic_anchors
       - merge_reason
       - merge_audit
    5. Persist merged task and MergeAudit record via TaskDomainRepository if available.
    """

    def __init__(self, task_repo: Optional[Any] = None) -> None:
        self.task_repo = task_repo
        self.last_audit: Optional[MergeAuditRecord] = None

    async def merge(
        self,
        target_task: UnifiedTaskCandidate,
        candidate: UnifiedTaskCandidate,
        score_result: Optional[CorrelationScoreResult] = None,
        persist: bool = True,
        candidate_task_ids: Optional[List[str]] = None,
    ) -> UnifiedTaskCandidate:
        """Merge candidate into target_task and return the updated task with audit metadata."""
        # Deep copy target task to prevent mutating original in-place unexpectedly
        merged = target_task.model_copy(deep=True)

        # 1. Preserve Evidence provenance and append new evidences
        existing_evidence_ids: Set[str] = {e.id for e in merged.evidences if e.id}
        existing_signatures: Set[tuple] = {
            (e.raw_event_id, e.snippet.strip()) for e in merged.evidences if e.snippet
        }

        new_evidences: List[EvidenceRecord] = list(merged.evidences)

        for ev in candidate.evidences:
            # Check duplicate by ID or (raw_event_id, snippet)
            sig = (ev.raw_event_id, ev.snippet.strip() if ev.snippet else "")
            if ev.id in existing_evidence_ids or sig in existing_signatures:
                continue

            # Copy evidence, preserving raw_event_id, author, source_type, timestamp, etc.
            # but updating task_id to target_task.id
            merged_ev = ev.model_copy(deep=True)
            merged_ev.task_id = merged.id
            if not merged_ev.id:
                merged_ev.id = str(uuid4())

            new_evidences.append(merged_ev)
            existing_evidence_ids.add(merged_ev.id)
            existing_signatures.add(sig)

        merged.evidences = new_evidences

        # 2. Update deadline: "mở rộng deadline nếu evidence mới có hạn cụ thể hơn"
        if candidate.due_date is not None:
            if merged.due_date is None:
                merged.due_date = candidate.due_date
                merged.explicit_deadline = candidate.explicit_deadline
            elif candidate.explicit_deadline and not merged.explicit_deadline:
                # Candidate has explicit deadline, replacing vague target deadline
                merged.due_date = candidate.due_date
                merged.explicit_deadline = True
            elif candidate.due_date > merged.due_date:
                # Extend deadline if candidate due_date is later
                merged.due_date = candidate.due_date
                if candidate.explicit_deadline:
                    merged.explicit_deadline = True

        # 3. Enrich description if target has none or candidate provides new info
        if not merged.description and candidate.description:
            merged.description = candidate.description
        elif candidate.description and candidate.description not in (merged.description or ""):
            # Append candidate notes if distinct
            merged.description = f"{merged.description}\n\n[Merged from {candidate.id}]: {candidate.description}"

        # 4. Complement project_key, customer_id, owner, requester if missing in target
        if not merged.project_key and candidate.project_key:
            merged.project_key = candidate.project_key

        if not merged.customer_id and candidate.customer_id:
            merged.customer_id = candidate.customer_id

        if not merged.owner_canonical_id and candidate.owner_canonical_id:
            merged.owner_canonical_id = candidate.owner_canonical_id
            merged.owner_name = candidate.owner_name

        if not merged.requester_canonical_id and candidate.requester_canonical_id:
            merged.requester_canonical_id = candidate.requester_canonical_id
            merged.requester_name = candidate.requester_name

        # 5. Inferred status transition if candidate brings completion / blocker signal
        if candidate.inferred_status and not merged.inferred_status:
            merged.inferred_status = candidate.inferred_status

        # 6. Priority score: adopt higher score
        merged.priority_score = max(merged.priority_score, candidate.priority_score)

        # 7. Correlation confidence & updated_at
        if score_result is not None:
            merged.correlation_confidence = score_result.correlation_confidence

        now_utc = datetime.now(timezone.utc)
        merged.updated_at = now_utc

        # 8. Compute and attach Audit Trail Metadata (Phase R6)
        cand_ids = candidate_task_ids if candidate_task_ids is not None else ([candidate.id] if candidate.id else [])
        winning_id = target_task.id
        
        if score_result is not None:
            corr_score = score_result.correlation_confidence
            matching_anchors = list(score_result.matching_anchors)
            sem_score = score_result.semantic_similarity
            reason = score_result.reason
        else:
            matcher = TaskCandidateMatcher()
            cand_anchors = matcher.extract_anchors(candidate)
            target_anchors = matcher.extract_anchors(target_task)
            common_anchors = cand_anchors & target_anchors
            matching_anchors = sorted([f"{a.anchor_type}:{a.value}" for a in common_anchors])
            corr_score = candidate.correlation_confidence or target_task.correlation_confidence or (0.90 if matching_anchors else 0.75)
            sem_score = 0.0
            if matching_anchors:
                reason = f"Deterministic anchor matched: {', '.join(matching_anchors)} -> AUTO-MERGE"
            else:
                reason = f"Auto-merged candidate {candidate.id} into target task {target_task.id}"

        audit_record = MergeAuditRecord(
            id=str(uuid4()),
            winning_task_id=winning_id,
            candidate_task_ids=cand_ids,
            correlation_score=corr_score,
            deterministic_anchors=matching_anchors,
            semantic_score=sem_score,
            merge_reason=reason,
            processor_version="v1.1",
            created_at=now_utc,
        )

        merged.candidate_task_ids = cand_ids
        merged.winning_task_id = winning_id
        merged.correlation_score = corr_score
        merged.deterministic_anchors = matching_anchors
        merged.merge_reason = reason
        merged.merge_audit = audit_record
        self.last_audit = audit_record

        # 9. Atomically persist via TaskDomainRepository if available
        if persist and self.task_repo is not None:
            if hasattr(self.task_repo, "upsert_task_atomic"):
                await self.task_repo.upsert_task_atomic(merged)
            if hasattr(self.task_repo, "record_merge_audit"):
                await self.task_repo.record_merge_audit(audit_record)

        logger.info(
            f"Successfully merged candidate {candidate.id} into target task {target_task.id}. "
            f"Total evidences: {len(merged.evidences)}, correlation_score={corr_score}."
        )
        return merged
