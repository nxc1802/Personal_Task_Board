"""Unit tests for GraphitiMemoryClient (Temporal Knowledge Graph & Episodic Memory)."""

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
import pytest

from ptb_contracts.l3_storage import DecisionNodeRecord, LessonNodeRecord
from ptb_graph_memory.client import GraphitiMemoryClient


class MockRecord:
    def __init__(self, data: Dict[str, Any]):
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def data(self) -> Dict[str, Any]:
        return self._data


class MockAsyncResult:
    def __init__(self, records: Optional[List[MockRecord]] = None, single_record: Optional[MockRecord] = None):
        self.records = records or []
        self._single_record = single_record if single_record is not None else (self.records[0] if self.records else None)

    async def single(self) -> Optional[MockRecord]:
        return self._single_record

    def __aiter__(self):
        self._iter = iter(self.records)
        return self

    async def __anext__(self) -> MockRecord:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class MockSession:
    def __init__(self, run_handler: Optional[Callable] = None):
        self.queries: List[tuple[str, Dict[str, Any]]] = []
        self.run_handler = run_handler

    async def run(self, query: str, parameters: Optional[Dict[str, Any]] = None) -> MockAsyncResult:
        clean_query = query.strip()
        params = parameters or {}
        self.queries.append((clean_query, params))
        if self.run_handler:
            return await self.run_handler(clean_query, params)
        return MockAsyncResult()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class MockDriver:
    def __init__(self, run_handler: Optional[Callable] = None):
        self.sessions: List[MockSession] = []
        self.run_handler = run_handler
        self.closed = False

    def session(self, database: Optional[str] = None) -> MockSession:
        sess = MockSession(self.run_handler)
        self.sessions.append(sess)
        return sess

    async def verify_connectivity(self) -> bool:
        return True

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def mock_driver():
    return MockDriver()


@pytest.fixture
def memory_client(mock_driver):
    return GraphitiMemoryClient(driver=mock_driver)


@pytest.mark.asyncio
async def test_add_decision_episode(mock_driver, memory_client):
    dec_time = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)
    decision = DecisionNodeRecord(
        decision_id="dec-101",
        summary="Use Neo4j single store instead of dual store",
        rationale="Eliminates sync race condition and outbox complexity",
        topic="Architecture",
        decided_by="tech_lead",
        decided_at=dec_time,
    )

    res_id = await memory_client.add_decision_episode(
        decision=decision,
        affects_task_id="task-001",
        affects_project_key="PTB",
    )

    assert res_id == "dec-101"
    session = mock_driver.sessions[0]
    # Check that 3 queries were run (MERGE Decision, link task, link project)
    assert len(session.queries) == 3

    # Verify MERGE Decision query
    q0, p0 = session.queries[0]
    assert "MERGE (d:Decision {decision_id: $decision_id})" in q0
    assert "SET d:EpisodicNode" in q0
    assert p0["decision_id"] == "dec-101"
    assert p0["summary"] == "Use Neo4j single store instead of dual store"
    assert p0["valid_at"] == dec_time.isoformat()
    assert p0["invalid_at"] is None

    # Verify edge (Decision)-[:AFFECTS]->(UnifiedTask)
    q1, p1 = session.queries[1]
    assert "MERGE (d)-[:AFFECTS]->(t)" in q1
    assert p1["task_id"] == "task-001"

    # Verify edge (Decision)-[:AFFECTS]->(Project)
    q2, p2 = session.queries[2]
    assert "MERGE (d)-[:AFFECTS]->(p)" in q2
    assert p2["project_key"] == "PTB"


@pytest.mark.asyncio
async def test_add_lesson_episode(mock_driver, memory_client):
    lesson_time = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
    lesson = LessonNodeRecord(
        lesson_id="les-202",
        topic="Neo4j Connection Pool",
        description="AuraDB closes idle connections after 30 minutes",
        solution="Set max_connection_lifetime=1800 and keep_alive=True",
        recorded_at=lesson_time,
    )

    res_id = await memory_client.add_lesson_episode(
        lesson=lesson,
        derived_from_task_id="task-002",
        related_incident_id="inc-99",
    )

    assert res_id == "les-202"
    session = mock_driver.sessions[0]
    assert len(session.queries) == 3

    # Check primary lesson query
    q0, p0 = session.queries[0]
    assert "MERGE (l:Lesson {lesson_id: $lesson_id})" in q0
    assert "SET l:EpisodicNode" in q0
    assert p0["lesson_id"] == "les-202"
    assert p0["topic"] == "Neo4j Connection Pool"
    assert p0["valid_at"] == lesson_time.isoformat()
    assert p0["invalid_at"] is None

    # Check task link
    q1, p1 = session.queries[1]
    assert "MERGE (l)-[:DERIVED_FROM]->(t)" in q1
    assert p1["task_id"] == "task-002"

    # Check incident link
    q2, p2 = session.queries[2]
    assert "MERGE (l)-[:DERIVED_FROM]->(inc)" in q2
    assert p2["incident_id"] == "inc-99"


@pytest.mark.asyncio
async def test_invalidate_episode(memory_client):
    async def handler(query: str, params: Dict[str, Any]):
        return MockAsyncResult(single_record=MockRecord({"updated_count": 1}))

    custom_driver = MockDriver(run_handler=handler)
    client = GraphitiMemoryClient(driver=custom_driver)

    inv_time = datetime(2026, 9, 29, 0, 0, 0, tzinfo=timezone.utc)
    success = await client.invalidate_episode("dec-101", invalidated_at=inv_time)
    assert success is True

    sess = custom_driver.sessions[0]
    assert len(sess.queries) == 1
    q, p = sess.queries[0]
    assert "SET n.invalid_at = $invalid_at" in q
    assert p["episode_id"] == "dec-101"
    assert p["invalid_at"] == inv_time.isoformat()


@pytest.mark.asyncio
async def test_search_context(memory_client):
    async def handler(query: str, params: Dict[str, Any]):
        # Mock search result rows
        rec1 = MockRecord({
            "n": {
                "decision_id": "dec-101",
                "summary": "Use Neo4j single store",
                "rationale": "Better architecture",
                "topic": "Architecture",
                "valid_at": "2026-09-28T10:00:00+00:00",
                "invalid_at": None,
                "episode_type": "DECISION",
            },
            "node_labels": ["Decision", "EpisodicNode"],
            "edges": [
                {"relation": "AFFECTS", "target_label": "Project", "target_id": "PTB"}
            ],
        })
        rec2 = MockRecord({
            "n": {
                "lesson_id": "les-202",
                "topic": "Architecture Scalability",
                "description": "Partition graph by tenant",
                "solution": "Use composite database",
                "valid_at": "2026-09-28T11:00:00+00:00",
                "invalid_at": None,
                "episode_type": "LESSON",
            },
            "node_labels": ["Lesson", "EpisodicNode"],
            "edges": [],
        })
        return MockAsyncResult(records=[rec1, rec2])

    custom_driver = MockDriver(run_handler=handler)
    client = GraphitiMemoryClient(driver=custom_driver)

    results = await client.search_context("Architecture", limit=5)
    assert len(results) == 2

    r0 = results[0]
    assert r0["id"] == "dec-101"
    assert r0["type"] == "DECISION"
    assert r0["summary"] == "Use Neo4j single store"
    assert len(r0["edges"]) == 1
    assert r0["edges"][0]["relation"] == "AFFECTS"

    r1 = results[1]
    assert r1["id"] == "les-202"
    assert r1["type"] == "LESSON"
    assert r1["topic"] == "Architecture Scalability"
    assert r1["summary"] == "Architecture Scalability"


@pytest.mark.asyncio
async def test_client_connectivity_and_close(mock_driver, memory_client):
    connected = await memory_client.verify_connectivity()
    assert connected is True
    await memory_client.close()
    assert mock_driver.closed is True
