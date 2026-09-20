import pytest
from datetime import datetime, timezone
from ptb_contracts.l3_storage import GraphActionType, GraphOutboxEventPayload
from ptb_database.outbox_worker import OutboxWorker


@pytest.mark.asyncio
async def test_outbox_worker_upsert_node():
    worker = OutboxWorker(neo4j_driver=None)
    event = GraphOutboxEventPayload(
        aggregate_type="Task",
        aggregate_id="task-001",
        action=GraphActionType.UPSERT_NODE,
        node_label="Task",
        node_properties={"title": "Deploy staging", "task_id": "task-001"}
    )
    result = await worker.process_outbox_event(event)
    assert result["success"] is True
    assert result["status"] == "completed"
    assert "MERGE (n:Task" in result["cypher"]


@pytest.mark.asyncio
async def test_outbox_worker_reject_invalid_node():
    worker = OutboxWorker(neo4j_driver=None)
    event = GraphOutboxEventPayload(
        aggregate_type="CustomEntity",
        aggregate_id="custom-001",
        action=GraphActionType.UPSERT_NODE,
        node_label="CustomEntity",
        node_properties={"name": "test"}
    )
    result = await worker.process_outbox_event(event)
    assert result["success"] is False
    assert result["status"] == "rejected_schema_violation"
    assert "CustomEntity" in result["error"]


@pytest.mark.asyncio
async def test_outbox_worker_upsert_edge_valid():
    worker = OutboxWorker(neo4j_driver=None)
    event = GraphOutboxEventPayload(
        aggregate_type="Task",
        aggregate_id="task-001",
        action=GraphActionType.UPSERT_EDGE,
        edge_type="COMMITTED_TO",
        source_canonical_id="cuong-person-id",
        target_canonical_id="task-001",
        confidence=0.95,
        evidence_id="ev-001",
        edge_properties={
            "source_label": "Person",
            "target_label": "Task"
        }
    )
    result = await worker.process_outbox_event(event)
    assert result["success"] is True
    assert result["status"] == "completed"
    assert "MERGE (src)-[r:COMMITTED_TO]->(dst)" in result["cypher"]


@pytest.mark.asyncio
async def test_outbox_worker_invalidate_edge():
    worker = OutboxWorker(neo4j_driver=None)
    event = GraphOutboxEventPayload(
        aggregate_type="Task",
        aggregate_id="task-001",
        action=GraphActionType.INVALIDATE_EDGE,
        edge_type="BLOCKED_BY",
        source_canonical_id="task-001",
        target_canonical_id="task-002",
        edge_properties={
            "source_label": "Task",
            "target_label": "Task"
        }
    )
    result = await worker.process_outbox_event(event)
    assert result["success"] is True
    assert result["status"] == "completed"
    assert "SET r.invalid_at = datetime(" in result["cypher"]
