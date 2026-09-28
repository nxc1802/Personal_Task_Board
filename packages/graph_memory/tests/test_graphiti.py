"""Unit tests for GraphitiMemoryClient (Temporal Knowledge Graph & Episodic Memory) & Rebuild mechanism."""

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock
import pytest

from ptb_contracts.l3_storage import DecisionNodeRecord, EvidenceNodeRecord, LessonNodeRecord
from ptb_graph_memory.adapter import GraphitiAdapter
from ptb_graph_memory.client import GraphitiMemoryClient
from ptb_graph_memory.rebuild import rebuild_graph_memory


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
async def test_add_evidence_episode(mock_driver, memory_client):
    ev_time = datetime(2026, 9, 28, 14, 30, 0, tzinfo=timezone.utc)
    evidence = EvidenceNodeRecord(
        id="ev-303",
        snippet="PR #42 merged: Rebuilt Graphiti Memory layer",
        confidence=0.98,
        source_type="git",
        external_url="https://github.com/org/repo/pull/42",
        timestamp=ev_time,
        raw_event_id="raw-888",
    )

    res_id = await memory_client.add_evidence_episode(
        evidence=evidence,
        task_id="task-003",
        raw_event_id="raw-888",
    )

    assert res_id == "ev-303"
    session = mock_driver.sessions[0]
    assert len(session.queries) == 3

    # Check primary evidence query
    q0, p0 = session.queries[0]
    assert "MERGE (e:Evidence {id: $id})" in q0
    assert "SET e:EpisodicNode" in q0
    assert "e.episode_type = 'EVIDENCE'" in q0
    assert p0["id"] == "ev-303"
    assert p0["snippet"] == "PR #42 merged: Rebuilt Graphiti Memory layer"
    assert p0["source_type"] == "git"
    assert p0["valid_at"] == ev_time.isoformat()

    # Check task link: (UnifiedTask)-[:HAS_EVIDENCE]->(Evidence)
    q1, p1 = session.queries[1]
    assert "MERGE (t)-[:HAS_EVIDENCE]->(e)" in q1
    assert p1["task_id"] == "task-003"
    assert p1["id"] == "ev-303"

    # Check raw event link: (Evidence)-[:DERIVED_FROM]->(RawEvent)
    q2, p2 = session.queries[2]
    assert "MERGE (e)-[:DERIVED_FROM]->(r)" in q2
    assert p2["raw_event_id"] == "raw-888"


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
        rec3 = MockRecord({
            "n": {
                "id": "ev-303",
                "snippet": "PR merged on architecture scaling",
                "source_type": "git",
                "valid_at": "2026-09-28T12:00:00+00:00",
                "invalid_at": None,
                "episode_type": "EVIDENCE",
            },
            "node_labels": ["Evidence", "EpisodicNode"],
            "edges": [],
        })
        return MockAsyncResult(records=[rec1, rec2, rec3])

    custom_driver = MockDriver(run_handler=handler)
    client = GraphitiMemoryClient(driver=custom_driver)

    results = await client.search_context("Architecture", limit=5)
    assert len(results) == 3

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

    r2 = results[2]
    assert r2["id"] == "ev-303"
    assert r2["type"] == "EVIDENCE"
    assert "git" in r2["summary"]
    assert "PR merged" in r2["content"]


@pytest.mark.asyncio
async def test_client_connectivity_and_close(mock_driver, memory_client):
    connected = await memory_client.verify_connectivity()
    assert connected is True
    await memory_client.close()
    assert mock_driver.closed is True


# ==============================================================================
# Graphiti Derived Semantic Layer & Invariant Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_graphiti_episode_ingestion_with_active_adapter(mock_driver):
    """Test that Graphiti adapter is called when active and records episodes."""
    mock_graphiti = AsyncMock()
    mock_graphiti.add_episode = AsyncMock(return_value={"status": "ok"})
    
    adapter = GraphitiAdapter(graphiti_instance=mock_graphiti, enabled=True)
    assert adapter.is_available is True

    client = GraphitiMemoryClient(driver=mock_driver, graphiti_client=adapter)

    # 1. Ingest Decision
    dec = DecisionNodeRecord(
        decision_id="dec-777",
        summary="Adopt Temporal Graphs",
        rationale="Clear timeline modeling",
        decided_by="alice",
        decided_at=datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc),
    )
    res_dec = await client.add_decision_episode(dec, affects_task_id="task-77")
    assert res_dec == "dec-777"
    assert mock_graphiti.add_episode.call_count == 1
    call_kwargs = mock_graphiti.add_episode.call_args[1]
    assert call_kwargs["name"] == "decision_dec-777"
    assert call_kwargs["uuid"] == "dec-777"
    assert "Temporal Graphs" in call_kwargs["episode_body"]

    # 2. Ingest Lesson
    lesson = LessonNodeRecord(
        lesson_id="les-888",
        topic="Index Tuning",
        description="Text indexes speed up queries",
        solution="Use fulltext search",
        recorded_at=datetime(2026, 9, 28, 11, 0, 0, tzinfo=timezone.utc),
    )
    res_les = await client.add_lesson_episode(lesson)
    assert res_les == "les-888"
    assert mock_graphiti.add_episode.call_count == 2
    call_kwargs_l = mock_graphiti.add_episode.call_args[1]
    assert call_kwargs_l["name"] == "lesson_les-888"
    assert "Index Tuning" in call_kwargs_l["episode_body"]

    # 3. Ingest Evidence
    evidence = EvidenceNodeRecord(
        id="ev-999",
        snippet="User confirmed acceptance",
        confidence=1.0,
        source_type="teams",
        timestamp=datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc),
        raw_event_id="raw-1",
    )
    res_ev = await client.add_evidence_episode(evidence, task_id="task-77")
    assert res_ev == "ev-999"
    assert mock_graphiti.add_episode.call_count == 3
    call_kwargs_e = mock_graphiti.add_episode.call_args[1]
    assert call_kwargs_e["name"] == "evidence_ev-999"
    assert "User confirmed acceptance" in call_kwargs_e["episode_body"]


@pytest.mark.asyncio
async def test_graphiti_error_never_interrupts_domain_flow(mock_driver):
    """Invariant test: Any error in Graphiti must never raise or rollback Neo4j domain write."""
    failing_graphiti = AsyncMock()
    failing_graphiti.add_episode = AsyncMock(side_effect=RuntimeError("Graphiti cluster unreachable / timeout"))

    adapter = GraphitiAdapter(graphiti_instance=failing_graphiti, enabled=True)
    client = GraphitiMemoryClient(driver=mock_driver, graphiti_client=adapter)

    # Ingest Decision despite Graphiti error
    dec = DecisionNodeRecord(
        decision_id="dec-err",
        summary="Invariant Test Decision",
        rationale="Must succeed even if Graphiti throws",
        decided_by="system",
        decided_at=datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc),
    )
    res_dec = await client.add_decision_episode(dec)
    assert res_dec == "dec-err"

    # Ingest Lesson despite Graphiti error
    les = LessonNodeRecord(
        lesson_id="les-err",
        topic="Error Resilience",
        description="Graphiti is derived semantic layer",
        solution="Catch and log warning",
        recorded_at=datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc),
    )
    res_les = await client.add_lesson_episode(les)
    assert res_les == "les-err"

    # Ingest Evidence despite Graphiti error
    ev = EvidenceNodeRecord(
        id="ev-err",
        snippet="Evidence write must remain intact",
        confidence=0.9,
        source_type="jira",
        timestamp=datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc),
        raw_event_id="raw-err",
    )
    res_ev = await client.add_evidence_episode(ev)
    assert res_ev == "ev-err"

    # Verify that authoritative Neo4j operations succeeded without interruption (1 dec + 1 les + 2 ev)
    assert len(mock_driver.sessions) == 3
    total_queries = sum(len(s.queries) for s in mock_driver.sessions)
    assert total_queries == 4


@pytest.mark.asyncio
async def test_graphiti_adapter_offline_graceful_fallback():
    """Test graceful fallback when Graphiti is disabled or offline."""
    adapter = GraphitiAdapter(uri=None, enabled=False)
    assert adapter.is_available is False

    res = await adapter.add_episode(
        name="test",
        episode_body="body",
        source_description="src",
        reference_time=datetime.now(timezone.utc),
    )
    assert res is None

    search_res = await adapter.search("query")
    assert search_res == []

    await adapter.close()


# ==============================================================================
# Graph Rebuild Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_rebuild_graph_memory_success():
    """Test full rebuild of Decisions, Lessons, and accepted Evidences from Neo4j."""
    async def read_handler(query: str, params: Dict[str, Any]):
        if "MATCH (d:Decision)" in query:
            return MockAsyncResult(records=[
                MockRecord({
                    "d": {
                        "decision_id": "dec-r1",
                        "summary": "Rebuilt Decision 1",
                        "rationale": "Rationale 1",
                        "topic": "Architecture",
                        "decided_by": "lead",
                        "decided_at": "2026-09-28T10:00:00+00:00",
                        "valid_at": "2026-09-28T10:00:00+00:00",
                    },
                    "task_id": "task-r1",
                    "project_key": "PTB",
                }),
                MockRecord({
                    "d": {
                        "decision_id": "dec-r2",
                        "summary": "Rebuilt Decision 2",
                        "rationale": "Rationale 2",
                        "topic": "Process",
                        "decided_by": "pm",
                        "decided_at": "2026-09-28T11:00:00+00:00",
                        "valid_at": "2026-09-28T11:00:00+00:00",
                    },
                    "task_id": None,
                    "project_key": "PTB",
                }),
            ])
        elif "MATCH (l:Lesson)" in query:
            return MockAsyncResult(records=[
                MockRecord({
                    "l": {
                        "lesson_id": "les-r1",
                        "topic": "Rebuilt Lesson 1",
                        "description": "Desc 1",
                        "solution": "Sol 1",
                        "recorded_at": "2026-09-28T12:00:00+00:00",
                        "valid_at": "2026-09-28T12:00:00+00:00",
                    },
                    "task_id": "task-r1",
                    "incident_id": "inc-r1",
                }),
            ])
        elif "MATCH (e:Evidence)" in query:
            return MockAsyncResult(records=[
                MockRecord({
                    "e": {
                        "id": "ev-r1",
                        "snippet": "Accepted PR comment",
                        "source_type": "git",
                        "confidence": 0.99,
                        "timestamp": "2026-09-28T13:00:00+00:00",
                        "valid_at": "2026-09-28T13:00:00+00:00",
                    },
                    "task_id": "task-r1",
                    "raw_event_id": "raw-r1",
                }),
                MockRecord({
                    "e": {
                        "id": "ev-r2",
                        "snippet": "Accepted Jira transition evidence",
                        "source_type": "jira",
                        "confidence": 1.0,
                        "timestamp": "2026-09-28T14:00:00+00:00",
                        "valid_at": "2026-09-28T14:00:00+00:00",
                    },
                    "task_id": "task-r2",
                    "raw_event_id": "raw-r2",
                }),
            ])
        return MockAsyncResult()

    read_driver = MockDriver(run_handler=read_handler)
    write_driver = MockDriver()
    mock_graphiti = AsyncMock()
    mock_graphiti.add_episode = AsyncMock(return_value={"status": "ok"})
    adapter = GraphitiAdapter(graphiti_instance=mock_graphiti, enabled=True)
    graphiti_client = GraphitiMemoryClient(driver=write_driver, graphiti_client=adapter)

    summary = await rebuild_graph_memory(neo4j_client=read_driver, graphiti_client=graphiti_client)

    assert summary["status"] == "success"
    assert summary["decisions_rebuilt"] == 2
    assert summary["lessons_rebuilt"] == 1
    assert summary["evidences_rebuilt"] == 2
    assert summary["total_rebuilt"] == 5
    assert len(summary["errors"]) == 0

    # Ensure Graphiti adapter received all 5 episodes during rebuild
    assert mock_graphiti.add_episode.call_count == 5


@pytest.mark.asyncio
async def test_rebuild_graph_memory_resilient_to_partial_errors():
    """Test that rebuild handles single record failures gracefully without aborting overall process."""
    async def read_handler(query: str, params: Dict[str, Any]):
        if "MATCH (d:Decision)" in query:
            return MockAsyncResult(records=[
                MockRecord({
                    "d": {
                        "decision_id": "dec-good",
                        "summary": "Good Decision",
                        "rationale": "Valid",
                        "decided_by": "lead",
                        "decided_at": "2026-09-28T10:00:00+00:00",
                    },
                    "task_id": "task-1",
                    "project_key": "PTB",
                }),
            ])
        elif "MATCH (l:Lesson)" in query:
            return MockAsyncResult(records=[
                MockRecord({
                    "l": {
                        "lesson_id": "les-bad",
                    },
                    "task_id": None,
                    "incident_id": None,
                })
            ])
        elif "MATCH (e:Evidence)" in query:
            return MockAsyncResult(records=[])
        return MockAsyncResult()

    read_driver = MockDriver(run_handler=read_handler)
    
    # Graphiti client mock that raises an error when adding lesson
    mock_client = MagicMock()
    mock_client.add_decision_episode = AsyncMock(return_value="dec-good")
    mock_client.add_lesson_episode = AsyncMock(side_effect=ValueError("Corrupt lesson data in Neo4j"))
    mock_client.add_evidence_episode = AsyncMock(return_value="ev-ok")

    summary = await rebuild_graph_memory(neo4j_client=read_driver, graphiti_client=mock_client)

    assert summary["status"] == "partial_success"
    assert summary["decisions_rebuilt"] == 1
    assert summary["lessons_rebuilt"] == 0
    assert summary["total_rebuilt"] == 1
    assert len(summary["errors"]) == 1
    assert "Corrupt lesson data" in summary["errors"][0]
