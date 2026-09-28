"""GraphitiMemoryClient: Temporal Knowledge Graph & Episodic Memory integration for Neo4j.

Manages episodic memory nodes (Decisions, Lessons) with temporal validity
(valid_at, invalid_at) and provides contextual semantic graph search.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Union
from uuid import uuid4

from neo4j import AsyncDriver
from ptb_contracts.l3_storage import DecisionNodeRecord, LessonNodeRecord
from ptb_database.neo4j_client import Neo4jClient

logger = logging.getLogger("ptb.graph_memory.client")


class GraphitiMemoryClient:
    """Temporal Episodic Memory Client powered by Neo4j and Graphiti principles."""

    def __init__(
        self,
        neo4j_client: Optional[Neo4jClient] = None,
        driver: Optional[AsyncDriver] = None,
        database: Optional[str] = None,
    ) -> None:
        """Initialize GraphitiMemoryClient with a Neo4jClient or raw AsyncDriver.
        
        Args:
            neo4j_client: Optional existing Neo4jClient instance.
            driver: Optional raw AsyncDriver instance (useful for mocks/tests).
            database: Optional Neo4j database name (defaults to client database or 'neo4j').
        """
        if neo4j_client is not None:
            self._neo4j_client = neo4j_client
            self._driver = driver or (neo4j_client.get_driver() if hasattr(neo4j_client, "get_driver") else None)
            self._database = database or getattr(neo4j_client, "database", "neo4j")
        elif driver is not None:
            self._neo4j_client = None
            self._driver = driver
            self._database = database or "neo4j"
        else:
            self._neo4j_client = Neo4jClient()
            self._driver = None
            self._database = database or "neo4j"

    def get_driver(self) -> AsyncDriver:
        """Get or create the underlying Neo4j AsyncDriver."""
        if self._driver is not None:
            return self._driver
        if self._neo4j_client is not None:
            self._driver = self._neo4j_client.get_driver()
            return self._driver
        raise RuntimeError("Neo4j driver is not configured.")

    async def verify_connectivity(self) -> bool:
        """Verify connection to the Neo4j cluster."""
        if self._neo4j_client is not None and hasattr(self._neo4j_client, "verify_connectivity"):
            return await self._neo4j_client.verify_connectivity()
        driver = self.get_driver()
        if hasattr(driver, "verify_connectivity"):
            await driver.verify_connectivity()
        return True

    async def close(self) -> None:
        """Close driver connection."""
        if self._neo4j_client is not None and hasattr(self._neo4j_client, "close"):
            await self._neo4j_client.close()
        elif self._driver is not None and hasattr(self._driver, "close"):
            await self._driver.close()
        self._driver = None

    async def add_decision_episode(
        self,
        decision: Union[DecisionNodeRecord, Dict[str, Any]],
        affects_task_id: Optional[str] = None,
        affects_project_key: Optional[str] = None,
    ) -> str:
        """Store a Decision as an Episodic Node with temporal attributes (valid_at, invalid_at).

        Args:
            decision: DecisionNodeRecord instance or compatible dictionary.
            affects_task_id: Optional UnifiedTask ID influenced by this decision.
            affects_project_key: Optional Project Key influenced by this decision.

        Returns:
            The decision_id.
        """
        driver = self.get_driver()
        
        if isinstance(decision, dict):
            dec_id = decision.get("decision_id") or str(uuid4())
            summary = decision.get("summary", "")
            rationale = decision.get("rationale", "")
            topic = decision.get("topic")
            decided_by = decision.get("decided_by", "SYSTEM")
            decided_at = decision.get("decided_at") or datetime.now(timezone.utc)
            valid_at = decision.get("valid_at") or decided_at
            invalid_at = decision.get("invalid_at")
        else:
            dec_id = decision.decision_id or str(uuid4())
            summary = decision.summary
            rationale = decision.rationale
            topic = decision.topic
            decided_by = decision.decided_by
            decided_at = decision.decided_at
            valid_at = getattr(decision, "valid_at", decided_at)
            invalid_at = getattr(decision, "invalid_at", None)

        valid_at_iso = valid_at.isoformat() if hasattr(valid_at, "isoformat") else str(valid_at)
        invalid_at_iso = invalid_at.isoformat() if hasattr(invalid_at, "isoformat") else (str(invalid_at) if invalid_at else None)
        decided_at_iso = decided_at.isoformat() if hasattr(decided_at, "isoformat") else str(decided_at)
        now_iso = datetime.now(timezone.utc).isoformat()

        cypher = """
        MERGE (d:Decision {decision_id: $decision_id})
        SET d:EpisodicNode,
            d.summary = $summary,
            d.rationale = $rationale,
            d.topic = $topic,
            d.decided_by = $decided_by,
            d.decided_at = $decided_at,
            d.valid_at = $valid_at,
            d.invalid_at = $invalid_at,
            d.episode_type = 'DECISION',
            d.updated_at = $updated_at
        RETURN d.decision_id AS id
        """

        params = {
            "decision_id": dec_id,
            "summary": summary,
            "rationale": rationale,
            "topic": topic,
            "decided_by": decided_by,
            "decided_at": decided_at_iso,
            "valid_at": valid_at_iso,
            "invalid_at": invalid_at_iso,
            "updated_at": now_iso,
        }

        async with driver.session(database=self._database) as session:
            await session.run(cypher, params)

            # Optional edge creation: (Decision)-[:AFFECTS]->(Project/UnifiedTask)
            if affects_task_id:
                link_task_cypher = """
                MATCH (d:Decision {decision_id: $decision_id})
                MATCH (t:UnifiedTask {id: $task_id})
                MERGE (d)-[:AFFECTS]->(t)
                """
                await session.run(link_task_cypher, {"decision_id": dec_id, "task_id": affects_task_id})

            if affects_project_key:
                link_proj_cypher = """
                MATCH (d:Decision {decision_id: $decision_id})
                MATCH (p:Project {project_key: $project_key})
                MERGE (d)-[:AFFECTS]->(p)
                """
                await session.run(link_proj_cypher, {"decision_id": dec_id, "project_key": affects_project_key})

        logger.info("Created decision episode: %s (valid_at: %s)", dec_id, valid_at_iso)
        return dec_id

    async def add_lesson_episode(
        self,
        lesson: Union[LessonNodeRecord, Dict[str, Any]],
        derived_from_task_id: Optional[str] = None,
        related_incident_id: Optional[str] = None,
    ) -> str:
        """Store a Lesson as an Episodic Node with temporal attributes (valid_at, invalid_at).

        Args:
            lesson: LessonNodeRecord instance or compatible dictionary.
            derived_from_task_id: Optional UnifiedTask ID that produced this lesson.
            related_incident_id: Optional Incident ID related to this lesson.

        Returns:
            The lesson_id.
        """
        driver = self.get_driver()

        if isinstance(lesson, dict):
            les_id = lesson.get("lesson_id") or str(uuid4())
            topic = lesson.get("topic", "")
            description = lesson.get("description", "")
            solution = lesson.get("solution", "")
            recorded_at = lesson.get("recorded_at") or datetime.now(timezone.utc)
            valid_at = lesson.get("valid_at") or recorded_at
            invalid_at = lesson.get("invalid_at")
        else:
            les_id = lesson.lesson_id or str(uuid4())
            topic = lesson.topic
            description = lesson.description
            solution = lesson.solution
            recorded_at = lesson.recorded_at
            valid_at = getattr(lesson, "valid_at", recorded_at)
            invalid_at = getattr(lesson, "invalid_at", None)

        valid_at_iso = valid_at.isoformat() if hasattr(valid_at, "isoformat") else str(valid_at)
        invalid_at_iso = invalid_at.isoformat() if hasattr(invalid_at, "isoformat") else (str(invalid_at) if invalid_at else None)
        recorded_at_iso = recorded_at.isoformat() if hasattr(recorded_at, "isoformat") else str(recorded_at)
        now_iso = datetime.now(timezone.utc).isoformat()

        cypher = """
        MERGE (l:Lesson {lesson_id: $lesson_id})
        SET l:EpisodicNode,
            l.topic = $topic,
            l.description = $description,
            l.solution = $solution,
            l.recorded_at = $recorded_at,
            l.valid_at = $valid_at,
            l.invalid_at = $invalid_at,
            l.episode_type = 'LESSON',
            l.updated_at = $updated_at
        RETURN l.lesson_id AS id
        """

        params = {
            "lesson_id": les_id,
            "topic": topic,
            "description": description,
            "solution": solution,
            "recorded_at": recorded_at_iso,
            "valid_at": valid_at_iso,
            "invalid_at": invalid_at_iso,
            "updated_at": now_iso,
        }

        async with driver.session(database=self._database) as session:
            await session.run(cypher, params)

            # Optional edge creation: (Lesson)-[:DERIVED_FROM]->(Task/Incident)
            if derived_from_task_id:
                link_task_cypher = """
                MATCH (l:Lesson {lesson_id: $lesson_id})
                MATCH (t:UnifiedTask {id: $task_id})
                MERGE (l)-[:DERIVED_FROM]->(t)
                """
                await session.run(link_task_cypher, {"lesson_id": les_id, "task_id": derived_from_task_id})

            if related_incident_id:
                link_inc_cypher = """
                MATCH (l:Lesson {lesson_id: $lesson_id})
                MATCH (inc:Incident {incident_id: $incident_id})
                MERGE (l)-[:DERIVED_FROM]->(inc)
                """
                await session.run(link_inc_cypher, {"lesson_id": les_id, "incident_id": related_incident_id})

        logger.info("Created lesson episode: %s (valid_at: %s)", les_id, valid_at_iso)
        return les_id

    async def invalidate_episode(
        self,
        episode_id: str,
        invalidated_at: Optional[datetime] = None,
    ) -> bool:
        """Mark an episodic node as invalid from a specific timestamp (superseded knowledge)."""
        driver = self.get_driver()
        inv_ts = invalidated_at or datetime.now(timezone.utc)
        inv_iso = inv_ts.isoformat() if hasattr(inv_ts, "isoformat") else str(inv_ts)

        cypher = """
        MATCH (n:EpisodicNode)
        WHERE (n.decision_id = $episode_id OR n.lesson_id = $episode_id OR n.id = $episode_id)
        SET n.invalid_at = $invalid_at,
            n.updated_at = $invalid_at
        RETURN count(n) AS updated_count
        """
        async with driver.session(database=self._database) as session:
            result = await session.run(cypher, {"episode_id": episode_id, "invalid_at": inv_iso})
            row = await result.single()
            count = row["updated_count"] if row else 0
            return count > 0

    async def search_context(
        self,
        query: str,
        limit: int = 5,
        include_invalidated: bool = False,
    ) -> List[Dict[str, Any]]:
        """Search contextual knowledge graph episodes by topic, summary, description, and edges.

        Args:
            query: The search term or topic keywords.
            limit: Maximum number of context episodes to return.
            include_invalidated: Whether to include historically invalidated knowledge.

        Returns:
            A list of dictionary records containing node data, temporal attributes, and connected edges.
        """
        driver = self.get_driver()
        now_iso = datetime.now(timezone.utc).isoformat()
        clean_query = query.strip()

        # Build Cypher query matching episodic nodes
        cypher = """
        MATCH (n:EpisodicNode)
        WHERE (
            $query = ''
            OR toLower(coalesce(n.topic, '')) CONTAINS toLower($query)
            OR toLower(coalesce(n.summary, '')) CONTAINS toLower($query)
            OR toLower(coalesce(n.description, '')) CONTAINS toLower($query)
            OR toLower(coalesce(n.rationale, '')) CONTAINS toLower($query)
            OR toLower(coalesce(n.solution, '')) CONTAINS toLower($query)
        )
        AND ($include_invalid = true OR n.invalid_at IS NULL OR n.invalid_at > $now)
        OPTIONAL MATCH (n)-[r]-(neighbor)
        WITH n,
             labels(n) AS node_labels,
             collect(
                CASE WHEN r IS NOT NULL THEN {
                    relation: type(r),
                    target_label: head(labels(neighbor)),
                    target_id: coalesce(neighbor.id, neighbor.decision_id, neighbor.lesson_id, neighbor.project_key, '')
                } ELSE null END
             ) AS raw_edges
        RETURN n,
               node_labels,
               [e IN raw_edges WHERE e IS NOT NULL] AS edges
        ORDER BY coalesce(n.valid_at, n.recorded_at, n.decided_at, '') DESC
        LIMIT $limit
        """

        params = {
            "query": clean_query,
            "now": now_iso,
            "include_invalid": include_invalidated,
            "limit": limit,
        }

        results: List[Dict[str, Any]] = []
        async with driver.session(database=self._database) as session:
            result = await session.run(cypher, params)
            async for row in result:
                node = row["n"]
                labels = row.get("node_labels") or []
                edges = row.get("edges") or []

                node_dict = dict(node) if hasattr(node, "items") or isinstance(node, dict) else {}
                
                # Determine ID and Type
                episode_type = node_dict.get("episode_type")
                if not episode_type:
                    if "Decision" in labels:
                        episode_type = "DECISION"
                    elif "Lesson" in labels:
                        episode_type = "LESSON"
                    else:
                        episode_type = "EPISODIC"

                node_id = (
                    node_dict.get("decision_id")
                    or node_dict.get("lesson_id")
                    or node_dict.get("id")
                    or ""
                )

                # Determine summary and content
                summary = (
                    node_dict.get("summary")
                    or node_dict.get("topic")
                    or node_dict.get("description", "")
                )
                content = (
                    node_dict.get("rationale")
                    or node_dict.get("solution")
                    or node_dict.get("description", "")
                )

                results.append({
                    "id": str(node_id),
                    "type": str(episode_type),
                    "topic": node_dict.get("topic", ""),
                    "summary": str(summary),
                    "content": str(content),
                    "valid_at": node_dict.get("valid_at"),
                    "invalid_at": node_dict.get("invalid_at"),
                    "edges": edges,
                    "properties": node_dict,
                })

        return results
