"""Graph Rebuild mechanism for Graphiti Memory.

Reconstructs all semantic episodes and temporal relationships in the derived
Graphiti memory layer from authoritative Neo4j domain data (Decisions, Lessons,
and accepted Evidences).
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional

from ptb_graph_memory.client import GraphitiMemoryClient

logger = logging.getLogger("ptb.graph_memory.rebuild")


async def rebuild_graph_memory(
    neo4j_client: Any,
    graphiti_client: Optional[GraphitiMemoryClient] = None,
) -> Dict[str, Any]:
    """Rebuild Graphiti semantic episodes and temporal graph edges from authoritative Neo4j store.

    Args:
        neo4j_client: Neo4jClient instance or compatible AsyncDriver/session runner.
        graphiti_client: Optional GraphitiMemoryClient instance. If omitted, will be created.

    Returns:
        Dict summarizing the rebuild outcome:
            - status: "success", "partial_success", or "failed"
            - decisions_rebuilt: int
            - lessons_rebuilt: int
            - evidences_rebuilt: int
            - total_rebuilt: int
            - errors: List[str]
    """
    logger.info("Starting Graphiti memory rebuild from authoritative Neo4j store...")

    # Resolve driver and database
    if hasattr(neo4j_client, "get_driver"):
        driver = neo4j_client.get_driver()
        database = getattr(neo4j_client, "database", "neo4j")
    elif hasattr(neo4j_client, "session"):
        driver = neo4j_client
        database = "neo4j"
    else:
        raise ValueError("neo4j_client must be a Neo4jClient or AsyncDriver instance.")

    if graphiti_client is None:
        graphiti_client = GraphitiMemoryClient(neo4j_client=neo4j_client if hasattr(neo4j_client, "get_driver") else None, driver=driver if not hasattr(neo4j_client, "get_driver") else None, database=database)

    decisions_rebuilt = 0
    lessons_rebuilt = 0
    evidences_rebuilt = 0
    errors: List[str] = []

    # 1. Rebuild Decisions
    decision_cypher = """
    MATCH (d:Decision)
    OPTIONAL MATCH (d)-[:AFFECTS]->(t:UnifiedTask)
    OPTIONAL MATCH (d)-[:AFFECTS]->(p:Project)
    RETURN d,
           head(collect(DISTINCT t.id)) AS task_id,
           head(collect(DISTINCT p.project_key)) AS project_key
    """

    try:
        async with driver.session(database=database) as session:
            result = await session.run(decision_cypher)
            async for row in result:
                try:
                    node = row["d"]
                    props = dict(node) if hasattr(node, "items") or isinstance(node, dict) else {}
                    task_id = row.get("task_id")
                    project_key = row.get("project_key")

                    await graphiti_client.add_decision_episode(
                        decision=props,
                        affects_task_id=task_id,
                        affects_project_key=project_key,
                    )
                    decisions_rebuilt += 1
                except Exception as row_exc:
                    err_msg = f"Failed to rebuild decision node {row}: {row_exc}"
                    logger.error(err_msg)
                    errors.append(err_msg)
    except Exception as exc:
        err_msg = f"Query error during Decision rebuild: {exc}"
        logger.error(err_msg)
        errors.append(err_msg)

    # 2. Rebuild Lessons
    lesson_cypher = """
    MATCH (l:Lesson)
    OPTIONAL MATCH (l)-[:DERIVED_FROM]->(t:UnifiedTask)
    OPTIONAL MATCH (l)-[:DERIVED_FROM]->(inc:Incident)
    RETURN l,
           head(collect(DISTINCT t.id)) AS task_id,
           head(collect(DISTINCT inc.incident_id)) AS incident_id
    """

    try:
        async with driver.session(database=database) as session:
            result = await session.run(lesson_cypher)
            async for row in result:
                try:
                    node = row["l"]
                    props = dict(node) if hasattr(node, "items") or isinstance(node, dict) else {}
                    task_id = row.get("task_id")
                    incident_id = row.get("incident_id")

                    await graphiti_client.add_lesson_episode(
                        lesson=props,
                        derived_from_task_id=task_id,
                        related_incident_id=incident_id,
                    )
                    lessons_rebuilt += 1
                except Exception as row_exc:
                    err_msg = f"Failed to rebuild lesson node {row}: {row_exc}"
                    logger.error(err_msg)
                    errors.append(err_msg)
    except Exception as exc:
        err_msg = f"Query error during Lesson rebuild: {exc}"
        logger.error(err_msg)
        errors.append(err_msg)

    # 3. Rebuild Accepted Evidences
    # An Evidence is accepted if it is linked to a UnifiedTask (:HAS_EVIDENCE) or has is_accepted = true
    evidence_cypher = """
    MATCH (e:Evidence)
    WHERE (e)<-[:HAS_EVIDENCE]-(:UnifiedTask) OR e.is_accepted = true
    OPTIONAL MATCH (t:UnifiedTask)-[:HAS_EVIDENCE]->(e)
    OPTIONAL MATCH (e)-[:DERIVED_FROM]->(r:RawEvent)
    RETURN DISTINCT e,
           head(collect(DISTINCT t.id)) AS task_id,
           head(collect(DISTINCT coalesce(r.id, e.raw_event_id))) AS raw_event_id
    """

    try:
        async with driver.session(database=database) as session:
            result = await session.run(evidence_cypher)
            async for row in result:
                try:
                    node = row["e"]
                    props = dict(node) if hasattr(node, "items") or isinstance(node, dict) else {}
                    task_id = row.get("task_id")
                    raw_event_id = row.get("raw_event_id")

                    await graphiti_client.add_evidence_episode(
                        evidence=props,
                        task_id=task_id,
                        raw_event_id=raw_event_id,
                    )
                    evidences_rebuilt += 1
                except Exception as row_exc:
                    err_msg = f"Failed to rebuild evidence node {row}: {row_exc}"
                    logger.error(err_msg)
                    errors.append(err_msg)
    except Exception as exc:
        err_msg = f"Query error during Evidence rebuild: {exc}"
        logger.error(err_msg)
        errors.append(err_msg)

    total_rebuilt = decisions_rebuilt + lessons_rebuilt + evidences_rebuilt

    if not errors:
        status = "success"
    elif total_rebuilt > 0:
        status = "partial_success"
    else:
        status = "failed"

    summary = {
        "status": status,
        "decisions_rebuilt": decisions_rebuilt,
        "lessons_rebuilt": lessons_rebuilt,
        "evidences_rebuilt": evidences_rebuilt,
        "total_rebuilt": total_rebuilt,
        "errors": errors,
    }

    logger.info("Graph memory rebuild completed: %s", summary)
    return summary
