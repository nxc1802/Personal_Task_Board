"""TaskDomainRepository: Repository for managing UnifiedTask, Evidence, and Person relationships in Neo4j."""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional
from uuid import uuid4

from neo4j import AsyncDriver
from ptb_contracts.l2_processing import (
    CommitmentRecord,
    EvidenceRecord,
    EvidenceType,
    MergeAuditRecord,
    ReviewQueueItem,
    StatusTransitionAuditRecord,
    TaskStatus,
    UnifiedTaskCandidate,
)
from ptb_contracts.l4_intelligence import TaskWithContext
from ptb_database.neo4j_client import Neo4jClient

logger = logging.getLogger("ptb.database.repositories.task_repo")


class TaskDomainRepository:
    """Repository quản lý UnifiedTask domain, Evidence provenance và Task State Machine audit trong Neo4j."""

    def __init__(self, neo4j_client: Neo4jClient) -> None:
        self.neo4j_client = neo4j_client

    def _get_driver(self) -> AsyncDriver:
        return self.neo4j_client.get_driver()

    async def upsert_task_atomic(self, task: UnifiedTaskCandidate) -> str:
        """MERGE (:UnifiedTask {id: $task.id}), SET title, description, status, priority_score, due_date, updated_at.
        
        Nếu có owner_canonical_id, MERGE (:Person)-[:ASSIGNED_TO]->(:UnifiedTask).
        Unwind task.evidences: MERGE (:Evidence {id: ev.id}), MERGE (:UnifiedTask)-[:HAS_EVIDENCE]->(:Evidence),
        và nếu có raw_event_id MERGE (:Evidence)-[:DERIVED_FROM]->(:RawEvent).
        """
        driver = self._get_driver()
        task_id = task.id or str(uuid4())
        status_val = task.status.value if hasattr(task.status, "value") else str(task.status)
        due_date_val = (
            task.due_date.isoformat()
            if task.due_date and hasattr(task.due_date, "isoformat")
            else (str(task.due_date) if task.due_date else None)
        )
        now_iso = datetime.now(timezone.utc).isoformat()

        # Chuẩn bị dữ liệu evidences
        evidences_data: List[Dict[str, Any]] = []
        for ev in task.evidences:
            ev_id = ev.id or str(uuid4())
            ev_type = ev.evidence_type.value if hasattr(ev.evidence_type, "value") else str(ev.evidence_type)
            ev_ts = ev.timestamp.isoformat() if hasattr(ev.timestamp, "isoformat") else str(ev.timestamp)
            evidences_data.append({
                "id": ev_id,
                "task_id": task_id,
                "raw_event_id": ev.raw_event_id,
                "evidence_type": ev_type,
                "source_type": ev.source_type,
                "external_url": ev.external_url,
                "author_canonical_id": ev.author_canonical_id,
                "author_canonical_name": ev.author_canonical_name,
                "timestamp": ev_ts,
                "snippet": ev.snippet,
                "confidence": ev.confidence,
                "extraction_version": ev.extraction_version,
            })

        async with driver.session(database=self.neo4j_client.database) as session:
            if hasattr(session, "begin_transaction"):
                async with session.begin_transaction() as tx:
                    await self._execute_upsert_task(
                        tx, task_id, task, status_val, due_date_val, now_iso, evidences_data
                    )
                    await tx.commit()
            else:
                await self._execute_upsert_task(
                    session, task_id, task, status_val, due_date_val, now_iso, evidences_data
                )
        return task_id

    async def _execute_upsert_task(
        self,
        runner: Any,
        task_id: str,
        task: UnifiedTaskCandidate,
        status_val: str,
        due_date_val: Optional[str],
        now_iso: str,
        evidences_data: List[Dict[str, Any]],
    ) -> None:
        created_at_val = (
            task.created_at.isoformat()
            if getattr(task, "created_at", None) and hasattr(task.created_at, "isoformat")
            else (str(task.created_at) if getattr(task, "created_at", None) else now_iso)
        )

        # 1. Upsert UnifiedTask: Task mới được set created_at, update chỉ đổi updated_at
        upsert_task_cypher = """
        MERGE (t:UnifiedTask {id: $id})
        ON CREATE SET t.created_at = $created_at
        SET t.title = $title,
            t.description = $description,
            t.status = $status,
            t.inferred_status = $inferred_status,
            t.status_authoritative = $status_authoritative,
            t.priority_score = $priority_score,
            t.priority_override = $priority_override,
            t.inferred_priority_score = $inferred_priority_score,
            t.due_date = $due_date,
            t.explicit_deadline = $explicit_deadline,
            t.project_key = $project_key,
            t.customer_id = $customer_id,
            t.extraction_confidence = $extraction_confidence,
            t.correlation_confidence = $correlation_confidence,
            t.review_status = $review_status,
            t.candidate_task_ids = $candidate_task_ids,
            t.winning_task_id = $winning_task_id,
            t.correlation_score = $correlation_score,
            t.deterministic_anchors = $deterministic_anchors,
            t.merge_reason = $merge_reason,
            t.updated_at = $updated_at
        """
        task_params = {
            "id": task_id,
            "title": task.title,
            "description": task.description,
            "status": status_val,
            "inferred_status": task.inferred_status,
            "status_authoritative": getattr(task, "status_authoritative", False),
            "priority_score": task.priority_score,
            "priority_override": getattr(task, "priority_override", None),
            "inferred_priority_score": getattr(task, "inferred_priority_score", None),
            "due_date": due_date_val,
            "explicit_deadline": task.explicit_deadline,
            "project_key": task.project_key,
            "customer_id": task.customer_id,
            "extraction_confidence": task.extraction_confidence,
            "correlation_confidence": task.correlation_confidence,
            "review_status": task.review_status,
            "candidate_task_ids": getattr(task, "candidate_task_ids", []) or [],
            "winning_task_id": getattr(task, "winning_task_id", None),
            "correlation_score": getattr(task, "correlation_score", None),
            "deterministic_anchors": getattr(task, "deterministic_anchors", []) or [],
            "merge_reason": getattr(task, "merge_reason", None),
            "created_at": created_at_val,
            "updated_at": now_iso,
        }
        await runner.run(upsert_task_cypher, task_params)

        # 2. Liên kết Owner (:Person)-[:ASSIGNED_TO]->(:UnifiedTask) nếu có (thống nhất Person.canonical_id)
        if task.owner_canonical_id:
            owner_cypher = """
            MATCH (t:UnifiedTask {id: $task_id})
            MERGE (p:Person {canonical_id: $owner_canonical_id})
            ON CREATE SET p.canonical_name = $owner_name, p.id = $owner_canonical_id
            SET p.id = coalesce(p.id, $owner_canonical_id)
            MERGE (p)-[:ASSIGNED_TO]->(t)
            """
            await runner.run(owner_cypher, {
                "task_id": task_id,
                "owner_canonical_id": task.owner_canonical_id,
                "owner_name": task.owner_name,
            })

        # 3. Liên kết Requester (:Person)-[:REQUESTED]->(:UnifiedTask) nếu có (thống nhất Person.canonical_id)
        if task.requester_canonical_id:
            req_cypher = """
            MATCH (t:UnifiedTask {id: $task_id})
            MERGE (req:Person {canonical_id: $requester_canonical_id})
            ON CREATE SET req.canonical_name = $requester_name, req.id = $requester_canonical_id
            SET req.id = coalesce(req.id, $requester_canonical_id)
            MERGE (req)-[:REQUESTED]->(t)
            """
            await runner.run(req_cypher, {
                "task_id": task_id,
                "requester_canonical_id": task.requester_canonical_id,
                "requester_name": task.requester_name,
            })

        # 4. Unwind evidences & liên kết (:UnifiedTask)-[:HAS_EVIDENCE]->(:Evidence) và (:Evidence)-[:DERIVED_FROM]->(:RawEvent)
        if evidences_data:
            evidences_cypher = """
            MATCH (t:UnifiedTask {id: $task_id})
            UNWIND $evidences AS ev
            MERGE (e:Evidence {id: ev.id})
            SET e.task_id = $task_id,
                e.snippet = ev.snippet,
                e.confidence = ev.confidence,
                e.source_type = ev.source_type,
                e.external_url = ev.external_url,
                e.timestamp = ev.timestamp,
                e.raw_event_id = ev.raw_event_id,
                e.author_canonical_id = ev.author_canonical_id,
                e.author_canonical_name = ev.author_canonical_name,
                e.evidence_type = ev.evidence_type,
                e.extraction_version = ev.extraction_version
            MERGE (t)-[:HAS_EVIDENCE]->(e)
            WITH e, ev
            CALL {
                WITH e, ev
                WITH e, ev WHERE ev.raw_event_id IS NOT NULL
                MATCH (re:RawEvent {id: ev.raw_event_id})
                MERGE (e)-[:DERIVED_FROM]->(re)
                RETURN count(re) AS _re_cnt
            }
            """
            await runner.run(evidences_cypher, {
                "task_id": task_id,
                "evidences": evidences_data,
            })

    def _parse_task_candidate(self, row: Any) -> Optional[UnifiedTaskCandidate]:
        if not row or not row.get("t"):
            return None

        task_dict = dict(row["t"])
        task_dict["owner_canonical_id"] = row.get("owner_canonical_id") or task_dict.get("owner_canonical_id")
        task_dict["owner_name"] = row.get("owner_name") or task_dict.get("owner_name")
        task_dict["requester_canonical_id"] = row.get("requester_canonical_id") or task_dict.get("requester_canonical_id")
        task_dict["requester_name"] = row.get("requester_name") or task_dict.get("requester_name")

        if "status" in task_dict and isinstance(task_dict["status"], str):
            try:
                task_dict["status"] = TaskStatus(task_dict["status"])
            except Exception:
                task_dict["status"] = TaskStatus.TODO
        if "priority_score" not in task_dict or task_dict["priority_score"] is None:
            task_dict["priority_score"] = 0.0
        if "extraction_confidence" not in task_dict or task_dict["extraction_confidence"] is None:
            task_dict["extraction_confidence"] = 1.0
        if "review_status" not in task_dict or task_dict["review_status"] is None:
            task_dict["review_status"] = "auto_approved"
        if "explicit_deadline" not in task_dict or task_dict["explicit_deadline"] is None:
            task_dict["explicit_deadline"] = False
        if "priority_override" in task_dict and task_dict["priority_override"] is not None:
            try:
                task_dict["priority_override"] = float(task_dict["priority_override"])
            except Exception:
                task_dict["priority_override"] = None
        else:
            task_dict["priority_override"] = None

        if "status_authoritative" in task_dict and task_dict["status_authoritative"] is not None:
            task_dict["status_authoritative"] = bool(task_dict["status_authoritative"])
        else:
            task_dict["status_authoritative"] = False

        if "inferred_priority_score" in task_dict and task_dict["inferred_priority_score"] is not None:
            try:
                task_dict["inferred_priority_score"] = float(task_dict["inferred_priority_score"])
            except Exception:
                task_dict["inferred_priority_score"] = None
        else:
            task_dict["inferred_priority_score"] = None

        task_dict["candidate_task_ids"] = row.get("candidate_task_ids") or task_dict.get("candidate_task_ids") or []
        task_dict["winning_task_id"] = row.get("winning_task_id") or task_dict.get("winning_task_id")
        task_dict["correlation_score"] = row.get("correlation_score") or task_dict.get("correlation_score")
        task_dict["deterministic_anchors"] = row.get("deterministic_anchors") or task_dict.get("deterministic_anchors") or []
        task_dict["merge_reason"] = row.get("merge_reason") or task_dict.get("merge_reason")

        for dt_field in ["due_date", "created_at", "updated_at"]:
            if dt_field in task_dict and isinstance(task_dict[dt_field], str):
                try:
                    task_dict[dt_field] = datetime.fromisoformat(task_dict[dt_field])
                except Exception:
                    pass

        evidences: List[EvidenceRecord] = []
        for ev_node in (row.get("evidences") or []):
            if ev_node is not None:
                ev_data = dict(ev_node)
                if "timestamp" in ev_data and isinstance(ev_data["timestamp"], str):
                    try:
                        ev_data["timestamp"] = datetime.fromisoformat(ev_data["timestamp"])
                    except Exception:
                        pass
                if "confidence" not in ev_data or ev_data["confidence"] is None:
                    ev_data["confidence"] = 1.0
                if "extraction_version" not in ev_data or ev_data["extraction_version"] is None:
                    ev_data["extraction_version"] = "v1.0"
                evidences.append(EvidenceRecord.model_validate(ev_data))
        task_dict["evidences"] = evidences

        return UnifiedTaskCandidate.model_validate(task_dict)

    async def get_task_by_id(self, task_id: str) -> Optional[UnifiedTaskCandidate]:
        """Truy vấn đầy đủ task và evidences liên quan."""
        driver = self._get_driver()
        cypher = """
        MATCH (t:UnifiedTask {id: $task_id})
        OPTIONAL MATCH (owner:Person)-[:ASSIGNED_TO]->(t)
        OPTIONAL MATCH (req:Person)-[:REQUESTED]->(t)
        OPTIONAL MATCH (t)-[:HAS_EVIDENCE]->(e:Evidence)
        RETURN t,
               owner.canonical_id AS owner_canonical_id,
               owner.canonical_name AS owner_name,
               req.canonical_id AS requester_canonical_id,
               req.canonical_name AS requester_name,
               collect(e) AS evidences
        """
        async with driver.session(database=self.neo4j_client.database) as session:
            result = await session.run(cypher, {"task_id": task_id})
            row = await result.single()
            return self._parse_task_candidate(row)

    async def list_tasks(self, filters: Optional[dict] = None) -> list[UnifiedTaskCandidate]:
        """Lấy danh sách tasks và lọc theo tiêu chí (status, project, customer, owner, due_range, etc.)."""
        driver = self._get_driver()
        cypher = """
        MATCH (t:UnifiedTask)
        OPTIONAL MATCH (owner:Person)-[:ASSIGNED_TO]->(t)
        OPTIONAL MATCH (req:Person)-[:REQUESTED]->(t)
        OPTIONAL MATCH (t)-[:HAS_EVIDENCE]->(e:Evidence)
        RETURN t,
               owner.canonical_id AS owner_canonical_id,
               owner.canonical_name AS owner_name,
               req.canonical_id AS requester_canonical_id,
               req.canonical_name AS requester_name,
               collect(e) AS evidences
        ORDER BY coalesce(t.priority_score, 0.0) DESC, coalesce(t.updated_at, '') DESC
        """
        tasks: list[UnifiedTaskCandidate] = []
        async with driver.session(database=self.neo4j_client.database) as session:
            result = await session.run(cypher)
            async for row in result:
                candidate = self._parse_task_candidate(row)
                if candidate:
                    tasks.append(candidate)

        if not filters:
            return tasks

        filtered = []
        now = datetime.now(timezone.utc)
        for t in tasks:
            if "status" in filters and filters["status"]:
                req_status = filters["status"]
                if isinstance(req_status, (list, set, tuple)):
                    status_vals = [s.value if hasattr(s, "value") else str(s) for s in req_status]
                    if t.status.value not in status_vals:
                        continue
                else:
                    status_val = req_status.value if hasattr(req_status, "value") else str(req_status)
                    if t.status.value != status_val:
                        continue

            if "project" in filters and filters["project"]:
                if (t.project_key or "").lower() != str(filters["project"]).lower():
                    continue
            if "project_key" in filters and filters["project_key"]:
                if (t.project_key or "").lower() != str(filters["project_key"]).lower():
                    continue

            if "customer" in filters and filters["customer"]:
                if (t.customer_id or "").lower() != str(filters["customer"]).lower():
                    continue
            if "customer_id" in filters and filters["customer_id"]:
                if (t.customer_id or "").lower() != str(filters["customer_id"]).lower():
                    continue

            if "source" in filters and filters["source"]:
                req_source = str(filters["source"]).lower()
                if not any((ev.source_type or "").lower() == req_source for ev in t.evidences):
                    continue
            if "source_type" in filters and filters["source_type"]:
                req_source = str(filters["source_type"]).lower()
                if not any((ev.source_type or "").lower() == req_source for ev in t.evidences):
                    continue

            if "owner" in filters and filters["owner"]:
                req_owner = str(filters["owner"]).lower()
                owner_match = (
                    (t.owner_canonical_id or "").lower() == req_owner
                    or (t.owner_name or "").lower() == req_owner
                )
                if not owner_match:
                    continue

            if "priority" in filters and filters["priority"] is not None:
                p_filter = filters["priority"]
                if isinstance(p_filter, dict):
                    min_p = p_filter.get("min", 0.0)
                    max_p = p_filter.get("max", 100.0)
                    if not (min_p <= t.priority_score <= max_p):
                        continue
                elif isinstance(p_filter, (int, float)):
                    if t.priority_score < float(p_filter):
                        continue

            if "due_range" in filters and filters["due_range"]:
                due_range = filters["due_range"]
                if not t.due_date:
                    continue
                t_due = t.due_date if t.due_date.tzinfo else t.due_date.replace(tzinfo=timezone.utc)
                if isinstance(due_range, dict):
                    start_dt = due_range.get("start")
                    end_dt = due_range.get("end")
                    if start_dt:
                        start_utc = start_dt if start_dt.tzinfo else start_dt.replace(tzinfo=timezone.utc)
                        if t_due < start_utc:
                            continue
                    if end_dt:
                        end_utc = end_dt if end_dt.tzinfo else end_dt.replace(tzinfo=timezone.utc)
                        if t_due > end_utc:
                            continue
                elif isinstance(due_range, (list, tuple)) and len(due_range) == 2:
                    start_dt, end_dt = due_range
                    if start_dt:
                        start_utc = start_dt if start_dt.tzinfo else start_dt.replace(tzinfo=timezone.utc)
                        if t_due < start_utc:
                            continue
                    if end_dt:
                        end_utc = end_dt if end_dt.tzinfo else end_dt.replace(tzinfo=timezone.utc)
                        if t_due > end_utc:
                            continue

            if "has_deadline" in filters and filters["has_deadline"] is not None:
                has_deadline_flag = bool(t.due_date is not None or t.explicit_deadline)
                if has_deadline_flag != bool(filters["has_deadline"]):
                    continue

            if "stale" in filters and filters["stale"] is not None:
                updated = t.updated_at or t.created_at
                is_stale = False
                if updated:
                    upd_utc = updated if updated.tzinfo else updated.replace(tzinfo=timezone.utc)
                    is_stale = (now - upd_utc).total_seconds() >= (3 * 86400) and t.status not in [TaskStatus.DONE, TaskStatus.DISMISSED]
                if is_stale != bool(filters["stale"]):
                    continue

            if "waiting" in filters and filters["waiting"] is not None:
                is_waiting = (t.status == TaskStatus.BLOCKED)
                if is_waiting != bool(filters["waiting"]):
                    continue

            if "review_status" in filters and filters["review_status"]:
                if (t.review_status or "").lower() != str(filters["review_status"]).lower():
                    continue

            filtered.append(t)

        if "limit" in filters and isinstance(filters["limit"], int):
            return filtered[:filters["limit"]]

        return filtered

    async def get_task_with_context(self, task_id: str) -> Optional[TaskWithContext]:
        """Truy vấn toàn bộ mạng lưới ngữ cảnh của một UnifiedTask."""
        driver = self._get_driver()
        cypher = """
        MATCH (t:UnifiedTask {id: $task_id})
        OPTIONAL MATCH (owner:Person)-[:ASSIGNED_TO]->(t)
        OPTIONAL MATCH (req:Person)-[:REQUESTED]->(t)
        OPTIONAL MATCH (t)-[:BELONGS_TO]->(p:Project)
        OPTIONAL MATCH (t)-[:BLOCKED_BY]->(blocker:UnifiedTask)
        OPTIONAL MATCH (t)-[:HAS_EVIDENCE]->(e:Evidence)
        OPTIONAL MATCH (dependent:Person)-[:WAITING_FOR]->(owner)
        RETURN t,
               owner.canonical_id AS owner_canonical_id,
               owner.canonical_name AS owner_name,
               req.canonical_id AS requester_canonical_id,
               req.canonical_name AS requester_name,
               collect(DISTINCT blocker.id) AS blocker_ids,
               collect(DISTINCT e) AS evidences,
               collect(DISTINCT coalesce(dependent.canonical_name, dependent.canonical_id)) AS dependent_names
        """
        async with driver.session(database=self.neo4j_client.database) as session:
            result = await session.run(cypher, {"task_id": task_id})
            row = await result.single()
            if not row or not row.get("t"):
                return None

            candidate = self._parse_task_candidate(row)
            if not candidate:
                return None

            now = datetime.now(timezone.utc)
            last_change = candidate.updated_at or candidate.created_at or now
            if last_change.tzinfo is None:
                last_change = last_change.replace(tzinfo=timezone.utc)
            days_in_current_status = max(0, int((now - last_change).total_seconds() // 86400))

            has_completion = any(
                ev.evidence_type == EvidenceType.COMPLETION_SIGNAL
                or "done" in (ev.snippet or "").lower()
                for ev in candidate.evidences
            )

            blocker_ids = [str(bid) for bid in (row.get("blocker_ids") or []) if bid]
            dependent_names = [str(d) for d in (row.get("dependent_names") or []) if d]

            return TaskWithContext(
                task=candidate,
                last_status_change_at=last_change,
                days_in_current_status=days_in_current_status,
                has_completion_evidence=has_completion,
                blocking_tasks=blocker_ids,
                dependent_people=dependent_names,
            )

    async def get_review_queue(self, limit: int = 20) -> list[ReviewQueueItem]:
        """Lấy danh sách các task cần phê duyệt (confidence 0.40 - 0.64 hoặc pending_review)."""
        all_tasks = await self.list_tasks()
        review_items: list[ReviewQueueItem] = []
        for t in all_tasks:
            is_pending = (
                t.review_status == "pending_review"
                or (0.40 <= (t.extraction_confidence or 0.0) < 0.65)
            )
            if is_pending:
                reason = "Confidence trung bình cần người dùng xác nhận"
                if (t.extraction_confidence or 0.0) < 0.65:
                    reason = f"Extraction confidence {t.extraction_confidence:.2f} nằm trong ngưỡng review (0.40 - 0.64)"
                raw_ev_id = t.evidences[0].raw_event_id if t.evidences else ""
                review_items.append(ReviewQueueItem(
                    id=f"rev-{t.id}",
                    raw_event_id=raw_ev_id,
                    candidate_task=t,
                    reason=reason,
                    created_at=t.created_at or datetime.now(timezone.utc),
                ))
                if len(review_items) >= limit:
                    break
        return review_items

    async def get_active_commitments(self, user_id: Optional[str] = None) -> list[CommitmentRecord]:
        """Lấy danh sách các commitments có status ACTIVE."""
        driver = self._get_driver()
        cypher = """
        MATCH (c:Commitment)
        WHERE c.status = 'ACTIVE'
        RETURN c
        ORDER BY coalesce(c.due_date, '') ASC
        """
        commitments: list[CommitmentRecord] = []
        async with driver.session(database=self.neo4j_client.database) as session:
            result = await session.run(cypher)
            async for row in result:
                node = row.get("c")
                if node:
                    data = dict(node)
                    for dt_field in ["due_date", "created_at"]:
                        if dt_field in data and isinstance(data[dt_field], str):
                            try:
                                data[dt_field] = datetime.fromisoformat(data[dt_field])
                            except Exception:
                                pass
                    commitments.append(CommitmentRecord.model_validate(data))
        return commitments

    async def record_status_transition_audit(self, audit: StatusTransitionAuditRecord) -> str:
        """Ghi lại lịch sử thay đổi trạng thái task."""
        driver = self._get_driver()
        audit_id = audit.id or str(uuid4())
        old_status = audit.old_status.value if hasattr(audit.old_status, "value") else str(audit.old_status)
        new_status = audit.new_status.value if hasattr(audit.new_status, "value") else str(audit.new_status)
        changed_at = (
            audit.changed_at.isoformat()
            if hasattr(audit.changed_at, "isoformat")
            else str(audit.changed_at)
        )

        cypher = """
        MATCH (t:UnifiedTask {id: $task_id})
        CREATE (a:StatusTransitionAudit {
            id: $id,
            task_id: $task_id,
            old_status: $old_status,
            new_status: $new_status,
            reason: $reason,
            source_evidence_ids: $source_evidence_ids,
            confidence: $confidence,
            changed_at: $changed_at,
            change_actor: $change_actor
        })
        CREATE (t)-[:STATUS_AUDIT]->(a)
        RETURN a.id AS id
        """
        params = {
            "id": audit_id,
            "task_id": audit.task_id,
            "old_status": old_status,
            "new_status": new_status,
            "reason": audit.reason,
            "source_evidence_ids": audit.source_evidence_ids,
            "confidence": audit.confidence,
            "changed_at": changed_at,
            "change_actor": audit.change_actor,
        }
        async with driver.session(database=self.neo4j_client.database) as session:
            result = await session.run(cypher, params)
            row = await result.single()
            if row and row["id"]:
                return str(row["id"])
            return audit_id

    async def record_merge_audit(self, audit: MergeAuditRecord) -> str:
        """Ghi lại quan hệ MergeAudit liên kết với (:UnifiedTask)."""
        driver = self._get_driver()
        audit_id = audit.id or str(uuid4())
        created_at_val = (
            audit.created_at.isoformat()
            if hasattr(audit.created_at, "isoformat")
            else str(audit.created_at)
        )
        cypher = """
        MATCH (t:UnifiedTask {id: $winning_task_id})
        CREATE (a:MergeAudit {
            id: $id,
            winning_task_id: $winning_task_id,
            candidate_task_ids: $candidate_task_ids,
            correlation_score: $correlation_score,
            deterministic_anchors: $deterministic_anchors,
            semantic_score: $semantic_score,
            merge_reason: $merge_reason,
            processor_version: $processor_version,
            created_at: $created_at
        })
        CREATE (t)-[:MERGE_AUDIT]->(a)
        RETURN a.id AS id
        """
        params = {
            "id": audit_id,
            "winning_task_id": audit.winning_task_id,
            "candidate_task_ids": audit.candidate_task_ids,
            "correlation_score": audit.correlation_score,
            "deterministic_anchors": audit.deterministic_anchors,
            "semantic_score": audit.semantic_score,
            "merge_reason": audit.merge_reason,
            "processor_version": audit.processor_version,
            "created_at": created_at_val,
        }
        async with driver.session(database=self.neo4j_client.database) as session:
            result = await session.run(cypher, params)
            row = await result.single()
            if row and row.get("id"):
                return str(row["id"])
            return audit_id

    async def split_task(
        self,
        original_task_id: str,
        evidence_ids_to_detach: list[str],
        new_task_title: Optional[str] = None,
    ) -> UnifiedTaskCandidate:
        """Tách các evidence chỉ định khỏi original_task_id và tạo một UnifiedTask mới.

        1. Detach các Evidence chỉ định khỏi original_task_id (DELETE [r:HAS_EVIDENCE]).
        2. Tạo một (:UnifiedTask) mới với id = uuid4(), title = new_task_title or f"Split: {original_task.title}".
        3. Gắn các Evidence đã detach vào Task mới (MERGE (:UnifiedTask)-[:HAS_EVIDENCE]->(:Evidence)).
        4. Bảo toàn 100% Provenance: Mỗi Evidence vẫn giữ nguyên liên kết [:DERIVED_FROM]->(:RawEvent).
        5. Cập nhật updated_at cho cả 2 tasks.
        """
        driver = self._get_driver()
        original_task = await self.get_task_by_id(original_task_id)
        if not original_task:
            raise ValueError(f"Original task {original_task_id} not found")

        detached_evidences = [e for e in original_task.evidences if e.id in evidence_ids_to_detach]
        if not detached_evidences:
            raise ValueError(
                f"None of the specified evidence IDs {evidence_ids_to_detach} belong to task {original_task_id}"
            )

        new_task_id = str(uuid4())
        title = new_task_title or f"Split: {original_task.title}"
        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.isoformat()

        status_val = (
            original_task.status.value
            if hasattr(original_task.status, "value")
            else str(original_task.status)
        )
        due_date_val = (
            original_task.due_date.isoformat()
            if original_task.due_date and hasattr(original_task.due_date, "isoformat")
            else (str(original_task.due_date) if original_task.due_date else None)
        )

        cypher = """
        MATCH (orig:UnifiedTask {id: $original_task_id})
        SET orig.updated_at = $now_iso

        CREATE (new_t:UnifiedTask {
            id: $new_task_id,
            title: $title,
            description: $description,
            status: $status,
            inferred_status: $inferred_status,
            priority_score: $priority_score,
            due_date: $due_date,
            explicit_deadline: $explicit_deadline,
            project_key: $project_key,
            customer_id: $customer_id,
            extraction_confidence: $extraction_confidence,
            correlation_confidence: $correlation_confidence,
            review_status: $review_status,
            created_at: $now_iso,
            updated_at: $now_iso
        })

        WITH orig, new_t
        MATCH (orig)-[r:HAS_EVIDENCE]->(e:Evidence)
        WHERE e.id IN $evidence_ids_to_detach
        DELETE r
        SET e.task_id = $new_task_id
        MERGE (new_t)-[:HAS_EVIDENCE]->(e)
        RETURN new_t.id AS id
        """

        params = {
            "original_task_id": original_task_id,
            "new_task_id": new_task_id,
            "title": title,
            "description": original_task.description,
            "status": status_val,
            "inferred_status": original_task.inferred_status,
            "priority_score": original_task.priority_score,
            "due_date": due_date_val,
            "explicit_deadline": original_task.explicit_deadline,
            "project_key": original_task.project_key,
            "customer_id": original_task.customer_id,
            "extraction_confidence": original_task.extraction_confidence,
            "correlation_confidence": original_task.correlation_confidence,
            "review_status": original_task.review_status,
            "now_iso": now_iso,
            "evidence_ids_to_detach": evidence_ids_to_detach,
        }

        async with driver.session(database=self.neo4j_client.database) as session:
            if hasattr(session, "begin_transaction"):
                async with session.begin_transaction() as tx:
                    await tx.run(cypher, params)
                    await tx.commit()
            else:
                await session.run(cypher, params)

        created_task = await self.get_task_by_id(new_task_id)
        if created_task:
            return created_task

        return UnifiedTaskCandidate(
            id=new_task_id,
            title=title,
            description=original_task.description,
            status=original_task.status,
            inferred_status=original_task.inferred_status,
            status_authoritative=original_task.status_authoritative,
            owner_canonical_id=original_task.owner_canonical_id,
            owner_name=original_task.owner_name,
            requester_canonical_id=original_task.requester_canonical_id,
            requester_name=original_task.requester_name,
            project_key=original_task.project_key,
            customer_id=original_task.customer_id,
            due_date=original_task.due_date,
            explicit_deadline=original_task.explicit_deadline,
            priority_score=original_task.priority_score,
            priority_override=original_task.priority_override,
            inferred_priority_score=original_task.inferred_priority_score,
            extraction_confidence=original_task.extraction_confidence,
            correlation_confidence=original_task.correlation_confidence,
            review_status=original_task.review_status,
            created_at=now_utc,
            updated_at=now_utc,
            evidences=[e.model_copy(update={"task_id": new_task_id}) for e in detached_evidences],
        )

