"""Asynchronous Graph Memory Synchronization Worker.

Manages asynchronous, resilient synchronization between the authoritative Neo4j store
and the derived Graphiti semantic memory layer. Ensures that Graphiti failures or
unavailability never interrupt or roll back authoritative domain operations while
reliably tracking sync status, retries, and bug telemetry.
"""

from datetime import datetime, timezone
from enum import Enum
import logging
from typing import Any, Dict, List, Optional, Union

from ptb_contracts.logging import BugCode, log_bug

logger = logging.getLogger("ptb.graph_memory.sync_worker")


class GraphSyncStatus(str, Enum):
    """Synchronization lifecycle states for episodic graph memory."""

    PENDING = "PENDING"
    SYNCING = "SYNCING"
    SYNCED = "SYNCED"
    RETRY = "RETRY"
    FAILED = "FAILED"


class GraphMemorySyncWorker:
    """Worker responsible for asynchronous episodic memory sync to Graphiti."""

    def __init__(
        self,
        memory_client: Optional[Any] = None,
        neo4j_client: Optional[Any] = None,
        driver: Optional[Any] = None,
        database: Optional[str] = None,
        max_retries: int = 3,
    ) -> None:
        """Initialize GraphMemorySyncWorker.

        Args:
            memory_client: Optional GraphitiMemoryClient instance.
            neo4j_client: Optional Neo4jClient instance.
            driver: Optional Neo4j AsyncDriver instance.
            database: Optional Neo4j database name (default: "neo4j").
            max_retries: Maximum retry attempts before marking an episode FAILED.
        """
        self.max_retries = max_retries
        self._database = database or "neo4j"
        self._neo4j_client = neo4j_client
        self._driver = driver
        self.memory_client = memory_client

        if self.memory_client is None:
            from ptb_graph_memory.client import GraphitiMemoryClient

            self.memory_client = GraphitiMemoryClient(
                neo4j_client=neo4j_client,
                driver=driver,
                database=self._database,
            )

        # In-memory tracking cache for sync states: item_id -> record
        self._sync_states: Dict[str, Dict[str, Any]] = {}
        # In-memory queue of pending/retry items for standalone sweeping
        self._pending_items: Dict[str, Dict[str, Any]] = {}

    def _get_driver(self) -> Optional[Any]:
        """Resolve driver from explicit driver, neo4j_client, or memory_client."""
        if self._driver is not None:
            return self._driver
        if self._neo4j_client is not None and hasattr(self._neo4j_client, "get_driver"):
            try:
                return self._neo4j_client.get_driver()
            except Exception:
                pass
        if self.memory_client is not None and hasattr(self.memory_client, "get_driver"):
            try:
                return self.memory_client.get_driver()
            except Exception:
                pass
        return None

    def _make_key(self, item_type: str, item_id: str) -> str:
        return f"{item_type.lower()}:{item_id}"

    def _get_or_create_record(self, item_type: str, item_id: str) -> Dict[str, Any]:
        key = self._make_key(item_type, item_id)
        if key not in self._sync_states:
            record = {
                "item_type": item_type.lower(),
                "item_id": item_id,
                "status": GraphSyncStatus.PENDING,
                "attempts": 0,
                "graph_synced_at": None,
                "last_error": None,
            }
            self._sync_states[key] = record
            # Also index by plain item_id for fast lookup
            self._sync_states[item_id] = record
        return self._sync_states[key]

    def enqueue_sync(
        self,
        item_type: str,
        item_id: str,
        payload: Dict[str, Any],
    ) -> None:
        """Enqueue an episode to be picked up by the next sync sweep."""
        rec = self._get_or_create_record(item_type, item_id)
        rec["status"] = GraphSyncStatus.PENDING
        key = self._make_key(item_type, item_id)
        self._pending_items[key] = {
            "item_type": item_type.lower(),
            "item_id": item_id,
            "payload": payload,
        }

    def get_sync_record(
        self,
        item_id: str,
        item_type: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve local in-memory sync record for an item."""
        if item_type:
            key = self._make_key(item_type, item_id)
            if key in self._sync_states:
                return self._sync_states[key]
        return self._sync_states.get(item_id)

    def get_sync_status(
        self,
        item_id: str,
        item_type: Optional[str] = None,
    ) -> Optional[GraphSyncStatus]:
        """Retrieve the sync status for an item."""
        rec = self.get_sync_record(item_id, item_type)
        if rec:
            return rec.get("status")
        return None

    async def _update_neo4j_status(
        self,
        item_type: str,
        item_id: str,
        status: GraphSyncStatus,
        attempts: int,
        synced_at: Optional[str] = None,
        last_error: Optional[str] = None,
    ) -> None:
        """Best-effort persistence of graph sync status properties to Neo4j node."""
        driver = self._get_driver()
        if driver is None or not hasattr(driver, "session"):
            return

        norm_type = item_type.lower()
        if norm_type == "decision":
            cypher = """
            MATCH (n:Decision {decision_id: $item_id})
            SET n.graph_sync_status = $status,
                n.graph_sync_attempts = $attempts,
                n.graph_synced_at = $synced_at,
                n.graph_last_error = $last_error,
                n.updated_at = $updated_at
            RETURN count(n) AS updated
            """
        elif norm_type == "lesson":
            cypher = """
            MATCH (n:Lesson {lesson_id: $item_id})
            SET n.graph_sync_status = $status,
                n.graph_sync_attempts = $attempts,
                n.graph_synced_at = $synced_at,
                n.graph_last_error = $last_error,
                n.updated_at = $updated_at
            RETURN count(n) AS updated
            """
        elif norm_type == "evidence":
            cypher = """
            MATCH (n:Evidence {id: $item_id})
            SET n.graph_sync_status = $status,
                n.graph_sync_attempts = $attempts,
                n.graph_synced_at = $synced_at,
                n.graph_last_error = $last_error,
                n.updated_at = $updated_at
            RETURN count(n) AS updated
            """
        else:
            cypher = """
            MATCH (n:EpisodicNode)
            WHERE (n.id = $item_id OR n.decision_id = $item_id OR n.lesson_id = $item_id)
            SET n.graph_sync_status = $status,
                n.graph_sync_attempts = $attempts,
                n.graph_synced_at = $synced_at,
                n.graph_last_error = $last_error,
                n.updated_at = $updated_at
            RETURN count(n) AS updated
            """

        status_val = status.value if hasattr(status, "value") else str(status)
        now_iso = datetime.now(timezone.utc).isoformat()
        params = {
            "item_id": item_id,
            "status": status_val,
            "attempts": attempts,
            "synced_at": synced_at,
            "last_error": last_error,
            "updated_at": now_iso,
        }

        try:
            async with driver.session(database=self._database) as session:
                await session.run(cypher, params)
        except Exception as exc:
            logger.debug(
                "Could not persist graph sync metadata to Neo4j for %s:%s: %s",
                item_type,
                item_id,
                exc,
            )

    async def sync_episode(
        self,
        item_type: str,
        item_id: str,
        payload: Union[Dict[str, Any], Any],
    ) -> str:
        """Synchronize an episode to Graphiti derived semantic memory.

        Calls GraphitiMemoryClient.add_evidence_episode, add_decision_episode,
        or add_lesson_episode.
        - If success: marks SYNCED, records graph_synced_at.
        - If error: catches error (DO NOT rollback or break Neo4j domain task),
          emits log_bug(BugCode.PTB_GRAPH_001), and marks RETRY (or FAILED if max_retries exceeded).

        Returns:
            The resulting status as string ("SYNCED", "RETRY", or "FAILED").
        """
        rec = self._get_or_create_record(item_type, item_id)
        rec["attempts"] += 1
        rec["status"] = GraphSyncStatus.SYNCING
        attempts = rec["attempts"]

        norm_type = item_type.lower().strip()
        call_payload = dict(payload) if isinstance(payload, dict) else (
            payload.model_dump() if hasattr(payload, "model_dump") else dict(payload)
        )

        await self._update_neo4j_status(
            item_type=norm_type,
            item_id=item_id,
            status=GraphSyncStatus.SYNCING,
            attempts=attempts,
        )

        try:
            # Check underlying Graphiti adapter availability if present
            adapter = getattr(self.memory_client, "adapter", None)
            if adapter is not None:
                if not getattr(adapter, "is_available", True):
                    raise RuntimeError(
                        f"Graphiti adapter is offline or unavailable for {item_type}:{item_id}"
                    )
                adapter.last_error = None

            if norm_type == "evidence":
                if "id" not in call_payload and item_id:
                    call_payload["id"] = item_id
                await self.memory_client.add_evidence_episode(
                    evidence=call_payload,
                    task_id=call_payload.get("task_id"),
                    raw_event_id=call_payload.get("raw_event_id"),
                )
            elif norm_type == "decision":
                if "decision_id" not in call_payload and item_id:
                    call_payload["decision_id"] = item_id
                await self.memory_client.add_decision_episode(
                    decision=call_payload,
                    affects_task_id=call_payload.get("affects_task_id") or call_payload.get("task_id"),
                    affects_project_key=call_payload.get("affects_project_key") or call_payload.get("project_key"),
                )
            elif norm_type == "lesson":
                if "lesson_id" not in call_payload and item_id:
                    call_payload["lesson_id"] = item_id
                await self.memory_client.add_lesson_episode(
                    lesson=call_payload,
                    derived_from_task_id=call_payload.get("derived_from_task_id") or call_payload.get("task_id"),
                    related_incident_id=call_payload.get("related_incident_id") or call_payload.get("incident_id"),
                )
            else:
                raise ValueError(f"Unknown item_type for graph memory sync: {item_type}")

            # Verify if adapter recorded a failure during ingestion
            if adapter is not None and getattr(adapter, "last_error", None) is not None:
                raise adapter.last_error

            now_iso = datetime.now(timezone.utc).isoformat()
            rec["status"] = GraphSyncStatus.SYNCED
            rec["graph_synced_at"] = now_iso
            rec["last_error"] = None

            await self._update_neo4j_status(
                item_type=norm_type,
                item_id=item_id,
                status=GraphSyncStatus.SYNCED,
                attempts=attempts,
                synced_at=now_iso,
                last_error=None,
            )

            # Clean from pending queue if present
            key = self._make_key(item_type, item_id)
            self._pending_items.pop(key, None)

            logger.info("Graphiti episode synced successfully: %s:%s", norm_type, item_id)
            return GraphSyncStatus.SYNCED.value

        except Exception as e:
            # Derived semantic layer invariant: Never raise or disrupt domain operations
            log_bug(
                code=BugCode.PTB_GRAPH_001,
                subsystem="graph_memory",
                severity="WARNING",
                message=f"Graphiti episode sync failed for {item_type}:{item_id}",
                exc=e,
                context={
                    "item_type": norm_type,
                    "item_id": item_id,
                    "attempts": attempts,
                    "max_retries": self.max_retries,
                },
            )

            if attempts >= self.max_retries:
                final_status = GraphSyncStatus.FAILED
            else:
                final_status = GraphSyncStatus.RETRY

            rec["status"] = final_status
            rec["last_error"] = str(e)

            await self._update_neo4j_status(
                item_type=norm_type,
                item_id=item_id,
                status=final_status,
                attempts=attempts,
                synced_at=None,
                last_error=str(e),
            )

            return final_status.value

    async def run_sync_sweep(self, limit: int = 100) -> Dict[str, Any]:
        """Sweep and synchronize all episodes currently in PENDING or RETRY state.

        Scans both authoritative store (Neo4j) if available, and any locally enqueued episodes.
        """
        logger.info("Starting Graph memory sync sweep (limit=%d)...", limit)
        scanned = 0
        synced = 0
        retried = 0
        failed = 0
        errors: List[str] = []

        driver = self._get_driver()
        if driver is not None and hasattr(driver, "session"):
            cypher = """
            MATCH (n:EpisodicNode)
            WHERE coalesce(n.graph_sync_status, 'PENDING') IN ['PENDING', 'RETRY']
              AND coalesce(n.graph_sync_attempts, 0) < $max_retries
            OPTIONAL MATCH (n)-[:AFFECTS]->(t:UnifiedTask)
            OPTIONAL MATCH (n)-[:AFFECTS]->(p:Project)
            OPTIONAL MATCH (n)-[:DERIVED_FROM]->(r:RawEvent)
            OPTIONAL MATCH (n)-[:DERIVED_FROM]->(inc:Incident)
            OPTIONAL MATCH (task:UnifiedTask)-[:HAS_EVIDENCE]->(n)
            RETURN labels(n) AS node_labels,
                   properties(n) AS props,
                   head(collect(DISTINCT coalesce(t.id, task.id))) AS task_id,
                   head(collect(DISTINCT p.project_key)) AS project_key,
                   head(collect(DISTINCT r.id)) AS raw_event_id,
                   head(collect(DISTINCT inc.incident_id)) AS incident_id
            LIMIT $limit
            """
            try:
                async with driver.session(database=self._database) as session:
                    result = await session.run(
                        cypher,
                        {"max_retries": self.max_retries, "limit": limit},
                    )
                    async for row in result:
                        scanned += 1
                        labels = row.get("node_labels") or []
                        props = dict(row["props"]) if hasattr(row.get("props"), "items") or isinstance(row.get("props"), dict) else {}
                        task_id = row.get("task_id")
                        project_key = row.get("project_key")
                        raw_event_id = row.get("raw_event_id")
                        incident_id = row.get("incident_id")

                        if "Evidence" in labels or props.get("episode_type") == "EVIDENCE":
                            itype = "evidence"
                            iid = props.get("id") or props.get("evidence_id")
                            if task_id:
                                props["task_id"] = task_id
                            if raw_event_id:
                                props["raw_event_id"] = raw_event_id
                        elif "Decision" in labels or props.get("episode_type") == "DECISION":
                            itype = "decision"
                            iid = props.get("decision_id") or props.get("id")
                            if task_id:
                                props["affects_task_id"] = task_id
                            if project_key:
                                props["affects_project_key"] = project_key
                        elif "Lesson" in labels or props.get("episode_type") == "LESSON":
                            itype = "lesson"
                            iid = props.get("lesson_id") or props.get("id")
                            if task_id:
                                props["derived_from_task_id"] = task_id
                            if incident_id:
                                props["related_incident_id"] = incident_id
                        else:
                            itype = "evidence"
                            iid = props.get("id") or props.get("decision_id") or props.get("lesson_id")

                        if not iid:
                            continue

                        status = await self.sync_episode(
                            item_type=itype,
                            item_id=str(iid),
                            payload=props,
                        )
                        if status == GraphSyncStatus.SYNCED.value:
                            synced += 1
                        elif status == GraphSyncStatus.FAILED.value:
                            failed += 1
                        else:
                            retried += 1
            except Exception as exc:
                err_msg = f"Error during Neo4j sync sweep query: {exc}"
                logger.warning(err_msg)
                errors.append(err_msg)

        # Sweep local in-memory enqueued items
        for key, item in list(self._pending_items.items()):
            rec = self.get_sync_record(item["item_id"], item["item_type"])
            curr_status = rec["status"] if rec else GraphSyncStatus.PENDING
            curr_attempts = rec["attempts"] if rec else 0

            if curr_status in [GraphSyncStatus.PENDING, GraphSyncStatus.RETRY] and curr_attempts < self.max_retries:
                scanned += 1
                status = await self.sync_episode(
                    item_type=item["item_type"],
                    item_id=item["item_id"],
                    payload=item["payload"],
                )
                if status == GraphSyncStatus.SYNCED.value:
                    synced += 1
                elif status == GraphSyncStatus.FAILED.value:
                    failed += 1
                else:
                    retried += 1

        summary = {
            "total_scanned": scanned,
            "synced": synced,
            "retried": retried,
            "failed": failed,
            "errors": errors,
        }
        logger.info("Graph memory sync sweep finished: %s", summary)
        return summary

    async def close(self) -> None:
        """Close client and driver resources."""
        if self.memory_client is not None and hasattr(self.memory_client, "close"):
            try:
                await self.memory_client.close()
            except Exception as exc:
                logger.debug("Error closing memory_client in sync worker: %s", exc)
