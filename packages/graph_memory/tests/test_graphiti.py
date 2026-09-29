"""Unit tests for GraphitiMemoryClient (Temporal Knowledge Graph & Episodic Memory) & Rebuild mechanism."""

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from ptb_contracts.logging import BugCode
from ptb_contracts.l3_storage import DecisionNodeRecord, EvidenceNodeRecord, LessonNodeRecord
from ptb_graph_memory.adapter import GraphitiAdapter
from ptb_graph_memory.client import GraphitiMemoryClient
from ptb_graph_memory.rebuild import rebuild_graph_memory
from ptb_graph_memory.sync_worker import GraphMemorySyncWorker, GraphSyncStatus



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


# ==============================================================================
# Graph Memory Synchronization Worker Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_sync_worker_sync_episode_success(mock_driver):
    """Test successful synchronization of episodes marks status as SYNCED with timestamp."""
    mock_client = MagicMock()
    mock_client.add_evidence_episode = AsyncMock(return_value="ev-101")
    mock_client.add_decision_episode = AsyncMock(return_value="dec-101")
    mock_client.add_lesson_episode = AsyncMock(return_value="les-101")
    mock_client.get_driver = MagicMock(return_value=mock_driver)

    worker = GraphMemorySyncWorker(memory_client=mock_client, driver=mock_driver)

    # 1. Sync Evidence
    ev_payload = {
        "id": "ev-101",
        "snippet": "Task completed by worker",
        "source_type": "git",
        "task_id": "task-001",
    }
    status_ev = await worker.sync_episode("evidence", "ev-101", ev_payload)
    assert status_ev == GraphSyncStatus.SYNCED
    assert status_ev == "SYNCED"
    assert worker.get_sync_status("ev-101") == GraphSyncStatus.SYNCED
    rec_ev = worker.get_sync_record("ev-101")
    assert rec_ev is not None
    assert rec_ev["attempts"] == 1
    assert rec_ev["graph_synced_at"] is not None
    assert rec_ev["last_error"] is None
    mock_client.add_evidence_episode.assert_awaited_once()

    # 2. Sync Decision
    dec_payload = {
        "decision_id": "dec-101",
        "summary": "Adopt Graphiti Worker",
        "rationale": "Asynchronous sync resilience",
        "affects_task_id": "task-001",
    }
    status_dec = await worker.sync_episode("decision", "dec-101", dec_payload)
    assert status_dec == GraphSyncStatus.SYNCED
    assert worker.get_sync_status("dec-101") == GraphSyncStatus.SYNCED
    rec_dec = worker.get_sync_record("dec-101")
    assert rec_dec["graph_synced_at"] is not None
    mock_client.add_decision_episode.assert_awaited_once()

    # 3. Sync Lesson
    les_payload = {
        "lesson_id": "les-101",
        "topic": "Resilience",
        "description": "Never fail authoritative transaction",
        "solution": "Use async sync worker with bug logging",
    }
    status_les = await worker.sync_episode("lesson", "les-101", les_payload)
    assert status_les == GraphSyncStatus.SYNCED
    assert worker.get_sync_status("les-101") == GraphSyncStatus.SYNCED
    mock_client.add_lesson_episode.assert_awaited_once()

    # Verify Neo4j update queries were executed
    assert len(mock_driver.sessions) > 0
    all_queries = [q for sess in mock_driver.sessions for q, _ in sess.queries]
    assert any("graph_sync_status = $status" in q for q in all_queries)


@pytest.mark.asyncio
async def test_sync_worker_offline_logs_bug_and_retries_without_crashing():
    """Test that offline Graphiti emits log_bug(PTB-GRAPH-001), transitions to RETRY, and does not crash."""
    mock_client = MagicMock()
    mock_client.add_evidence_episode = AsyncMock(side_effect=RuntimeError("Graphiti cluster connection refused"))

    worker = GraphMemorySyncWorker(memory_client=mock_client, max_retries=3)

    ev_payload = {
        "id": "ev-err-1",
        "snippet": "Important evidence but Graphiti is down",
        "source_type": "teams",
    }

    with patch("ptb_graph_memory.sync_worker.log_bug") as mock_log_bug:
        # Must NOT raise exception / crash process
        status = await worker.sync_episode("evidence", "ev-err-1", ev_payload)

        assert status == GraphSyncStatus.RETRY
        assert status == "RETRY"
        assert worker.get_sync_status("ev-err-1") == GraphSyncStatus.RETRY

        # Verify log_bug was called with PTB_GRAPH_001
        assert mock_log_bug.call_count == 1
        call_kwargs = mock_log_bug.call_args[1]
        assert call_kwargs["code"] == BugCode.PTB_GRAPH_001 or call_kwargs["code"] == "PTB-GRAPH-001"
        assert call_kwargs["subsystem"] == "graph_memory"
        assert call_kwargs["severity"] == "WARNING"
        assert "Graphiti episode sync failed" in call_kwargs["message"]
        assert "ev-err-1" in call_kwargs["message"]
        assert isinstance(call_kwargs["exc"], RuntimeError)

        rec = worker.get_sync_record("ev-err-1")
        assert rec["attempts"] == 1
        assert rec["graph_synced_at"] is None
        assert "connection refused" in rec["last_error"]


@pytest.mark.asyncio
async def test_sync_worker_adapter_offline_detected_and_retried():
    """Test that when GraphitiAdapter has is_available=False, worker logs PTB-GRAPH-001 and sets RETRY."""
    mock_adapter = MagicMock()
    mock_adapter.is_available = False

    mock_client = MagicMock()
    mock_client.adapter = mock_adapter

    worker = GraphMemorySyncWorker(memory_client=mock_client, max_retries=3)

    with patch("ptb_graph_memory.sync_worker.log_bug") as mock_log_bug:
        status = await worker.sync_episode("decision", "dec-off", {"summary": "Offline Test"})

        assert status == GraphSyncStatus.RETRY
        assert mock_log_bug.call_count == 1
        call_kwargs = mock_log_bug.call_args[1]
        assert call_kwargs["code"] == BugCode.PTB_GRAPH_001
        assert "offline or unavailable" in str(call_kwargs["exc"])


@pytest.mark.asyncio
async def test_sync_worker_max_retries_transitions_to_failed():
    """Test that reaching max_retries marks episode as FAILED."""
    mock_client = MagicMock()
    mock_client.add_evidence_episode = AsyncMock(side_effect=RuntimeError("Persistent Graphiti failure"))

    worker = GraphMemorySyncWorker(memory_client=mock_client, max_retries=2)

    ev_payload = {"id": "ev-max-fail", "snippet": "Fails twice"}

    # Attempt 1 -> RETRY
    status_1 = await worker.sync_episode("evidence", "ev-max-fail", ev_payload)
    assert status_1 == GraphSyncStatus.RETRY
    assert worker.get_sync_status("ev-max-fail") == GraphSyncStatus.RETRY

    # Attempt 2 -> FAILED
    status_2 = await worker.sync_episode("evidence", "ev-max-fail", ev_payload)
    assert status_2 == GraphSyncStatus.FAILED
    assert status_2 == "FAILED"
    assert worker.get_sync_status("ev-max-fail") == GraphSyncStatus.FAILED

    rec = worker.get_sync_record("ev-max-fail")
    assert rec["attempts"] == 2
    assert rec["status"] == GraphSyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_worker_run_sync_sweep_processes_pending_and_retry():
    """Test that run_sync_sweep scans and processes both locally queued and Neo4j PENDING/RETRY episodes."""
    # 1. Test local enqueue and sweep
    mock_client = MagicMock()
    mock_client.add_evidence_episode = AsyncMock(return_value="ev-q1")
    mock_client.add_decision_episode = AsyncMock(return_value="dec-q1")

    worker = GraphMemorySyncWorker(memory_client=mock_client, max_retries=3)
    worker.enqueue_sync("evidence", "ev-q1", {"id": "ev-q1", "snippet": "Queued evidence"})
    worker.enqueue_sync("decision", "dec-q1", {"decision_id": "dec-q1", "summary": "Queued decision"})

    sweep_res = await worker.run_sync_sweep()
    assert sweep_res["total_scanned"] == 2
    assert sweep_res["synced"] == 2
    assert sweep_res["retried"] == 0
    assert sweep_res["failed"] == 0

    assert worker.get_sync_status("ev-q1") == GraphSyncStatus.SYNCED
    assert worker.get_sync_status("dec-q1") == GraphSyncStatus.SYNCED

    # 2. Test sweep from Neo4j driver
    async def sweep_handler(query: str, params: Dict[str, Any]):
        if "MATCH (n:EpisodicNode)" in query:
            return MockAsyncResult(records=[
                MockRecord({
                    "node_labels": ["Evidence", "EpisodicNode"],
                    "props": {"id": "ev-db1", "snippet": "Evidence from DB", "graph_sync_status": "PENDING"},
                    "task_id": "task-db1",
                    "project_key": None,
                    "raw_event_id": "raw-db1",
                    "incident_id": None,
                }),
                MockRecord({
                    "node_labels": ["Decision", "EpisodicNode"],
                    "props": {"decision_id": "dec-db1", "summary": "Decision from DB", "graph_sync_status": "RETRY"},
                    "task_id": "task-db1",
                    "project_key": "PTB",
                    "raw_event_id": None,
                    "incident_id": None,
                }),
            ])
        return MockAsyncResult()

    driver = MockDriver(run_handler=sweep_handler)
    worker_db = GraphMemorySyncWorker(memory_client=mock_client, driver=driver, max_retries=3)

    sweep_db_res = await worker_db.run_sync_sweep()
    assert sweep_db_res["total_scanned"] == 2
    assert sweep_db_res["synced"] == 2
    assert worker_db.get_sync_status("ev-db1") == GraphSyncStatus.SYNCED
    assert worker_db.get_sync_status("dec-db1") == GraphSyncStatus.SYNCED


@pytest.mark.asyncio
async def test_graph_worker_sweep_syncs_pending_evidence():
    """Verify that GraphMemorySyncWorker sweep scans Evidence nodes in Neo4j with
    graph_sync_status IN ['PENDING', 'RETRY'], synchronizes them to Graphiti,
    and updates status to SYNCED with graph_synced_at timestamp in Neo4j."""
    executed_queries: List[tuple[str, Dict[str, Any]]] = []

    async def sweep_handler(query: str, params: Dict[str, Any]):
        executed_queries.append((query, params))
        if "MATCH (e:Evidence)" in query:
            return MockAsyncResult(records=[
                MockRecord({
                    "props": {
                        "id": "ev-sweep-001",
                        "snippet": "User requested database indexing",
                        "source_type": "ms_teams",
                        "timestamp": "2026-09-29T10:00:00+00:00",
                        "graph_sync_status": "PENDING",
                        "graph_sync_attempts": 0,
                    },
                    "task_id": "task-sweep-001",
                    "raw_event_id": "raw-sweep-001",
                }),
                MockRecord({
                    "props": {
                        "id": "ev-sweep-002",
                        "snippet": "Retry evidence after transient network hiccup",
                        "source_type": "git",
                        "timestamp": "2026-09-29T10:05:00+00:00",
                        "graph_sync_status": "RETRY",
                        "graph_sync_attempts": 1,
                        "graph_last_error": "Connection timeout",
                    },
                    "task_id": "task-sweep-002",
                    "raw_event_id": "raw-sweep-002",
                }),
            ])
        elif "MATCH (n:Evidence)" in query:
            return MockAsyncResult(single_record=MockRecord({"updated": 1}))
        return MockAsyncResult()

    driver = MockDriver(run_handler=sweep_handler)

    mock_client = MagicMock()
    mock_client.add_evidence_episode = AsyncMock(return_value="synced-ok")
    mock_client.get_driver = MagicMock(return_value=driver)

    worker = GraphMemorySyncWorker(
        memory_client=mock_client,
        driver=driver,
        max_retries=3,
    )

    # Execute sweep
    sweep_summary = await worker.sweep_pending_evidence()

    # 1. Verify sweep summary results
    assert sweep_summary["total_scanned"] == 2
    assert sweep_summary["synced"] == 2
    assert sweep_summary["retried"] == 0
    assert sweep_summary["failed"] == 0
    assert len(sweep_summary["errors"]) == 0

    # 2. Verify Graphiti client was invoked for both evidence items
    assert mock_client.add_evidence_episode.call_count == 2
    call_args_1 = mock_client.add_evidence_episode.call_args_list[0][1]
    assert call_args_1["evidence"]["id"] == "ev-sweep-001"
    assert call_args_1["task_id"] == "task-sweep-001"
    call_args_2 = mock_client.add_evidence_episode.call_args_list[1][1]
    assert call_args_2["evidence"]["id"] == "ev-sweep-002"
    assert call_args_2["task_id"] == "task-sweep-002"

    # 3. Verify in-memory status transitioned to SYNCED
    assert worker.get_sync_status("ev-sweep-001") == GraphSyncStatus.SYNCED
    rec1 = worker.get_sync_record("ev-sweep-001")
    assert rec1 is not None
    assert rec1["status"] == GraphSyncStatus.SYNCED
    assert rec1["graph_synced_at"] is not None
    assert rec1["last_error"] is None
    assert rec1["attempts"] == 1

    assert worker.get_sync_status("ev-sweep-002") == GraphSyncStatus.SYNCED
    rec2 = worker.get_sync_record("ev-sweep-002")
    assert rec2 is not None
    assert rec2["status"] == GraphSyncStatus.SYNCED
    assert rec2["graph_synced_at"] is not None
    assert rec2["last_error"] is None
    # Previous attempts were 1, so after this successful attempt it is 2
    assert rec2["attempts"] == 2

    # 4. Verify Neo4j update queries were executed to persist SYNCED state
    status_update_queries = [
        (q, p) for q, p in executed_queries
        if "SET n.graph_sync_status = $status" in q
    ]
    assert len(status_update_queries) >= 2
    synced_updates = [
        (q, p) for q, p in status_update_queries
        if p.get("status") == "SYNCED"
    ]
    assert len(synced_updates) == 2
    for _, p in synced_updates:
        assert p["status"] == "SYNCED"
        assert p["synced_at"] is not None
        assert p["last_error"] is None


@pytest.mark.asyncio
async def test_graph_worker_failure_marks_retry_without_task_rollback():
    """Verify that when Graphiti throws an error during sync:
    1. Exception does not propagate or crash the worker.
    2. PTB-GRAPH-001 bug telemetry is logged via log_bug.
    3. Status transitions to RETRY (if attempts < max_retries) or FAILED (if attempts >= max_retries).
    4. graph_sync_attempts is incremented and graph_last_error is persisted to Neo4j.
    5. The authoritative domain task and evidence in Neo4j are completely preserved without rollback.
    """
    executed_queries: List[tuple[str, Dict[str, Any]]] = []

    async def sweep_handler(query: str, params: Dict[str, Any]):
        executed_queries.append((query, params))
        if "MATCH (e:Evidence)" in query:
            return MockAsyncResult(records=[
                MockRecord({
                    "props": {
                        "id": "ev-fail-001",
                        "snippet": "Critical evidence for task-fail-001",
                        "source_type": "jira",
                        "timestamp": "2026-09-29T10:10:00+00:00",
                        "graph_sync_status": "PENDING",
                        "graph_sync_attempts": 0,
                    },
                    "task_id": "task-fail-001",
                    "raw_event_id": "raw-fail-001",
                })
            ])
        elif "MATCH (n:Evidence)" in query:
            return MockAsyncResult(single_record=MockRecord({"updated": 1}))
        return MockAsyncResult()

    driver = MockDriver(run_handler=sweep_handler)

    # Graphiti client that throws error
    mock_client = MagicMock()
    mock_client.add_evidence_episode = AsyncMock(
        side_effect=RuntimeError("Graphiti cluster unreachable / timeout")
    )
    mock_client.get_driver = MagicMock(return_value=driver)

    worker = GraphMemorySyncWorker(
        memory_client=mock_client,
        driver=driver,
        max_retries=3,
    )

    with patch("ptb_graph_memory.sync_worker.log_bug") as mock_log_bug:
        # First attempt: 0 -> 1 < 3 => RETRY
        summary = await worker.sweep_pending_evidence()

        assert summary["total_scanned"] == 1
        assert summary["synced"] == 0
        assert summary["retried"] == 1
        assert summary["failed"] == 0

        # Verify log_bug with PTB_GRAPH_001
        assert mock_log_bug.call_count == 1
        call_kwargs = mock_log_bug.call_args[1]
        assert call_kwargs["code"] == BugCode.PTB_GRAPH_001
        assert call_kwargs["subsystem"] == "graph_memory"
        assert call_kwargs["severity"] == "WARNING"
        assert "ev-fail-001" in call_kwargs["message"]
        assert "Graphiti cluster unreachable" in str(call_kwargs["exc"])

        # Verify status is RETRY
        assert worker.get_sync_status("ev-fail-001") == GraphSyncStatus.RETRY
        rec = worker.get_sync_record("ev-fail-001")
        assert rec["attempts"] == 1
        assert "Graphiti cluster unreachable" in rec["last_error"]

        # Verify Neo4j update query recorded RETRY status, attempts=1, and last_error
        retry_updates = [
            (q, p) for q, p in executed_queries
            if p.get("status") == "RETRY" and p.get("item_id") == "ev-fail-001"
        ]
        assert len(retry_updates) == 1
        _, p_retry = retry_updates[0]
        assert p_retry["attempts"] == 1
        assert "Graphiti cluster unreachable" in p_retry["last_error"]

        # Crucial invariant check: No rollback, no DELETE query was executed
        assert not any("DELETE" in q.upper() for q, _ in executed_queries)
        assert not any("ROLLBACK" in q.upper() for q, _ in executed_queries)

    # Now simulate exceeding max_retries (attempt 2 and 3)
    with patch("ptb_graph_memory.sync_worker.log_bug"):
        # Attempt 2: 1 -> 2 < 3 => RETRY
        status_2 = await worker.sync_episode(
            item_type="evidence",
            item_id="ev-fail-001",
            payload={"id": "ev-fail-001", "snippet": "Critical evidence", "graph_sync_attempts": 1},
        )
        assert status_2 == GraphSyncStatus.RETRY

        # Attempt 3: 2 -> 3 >= 3 => FAILED
        status_3 = await worker.sync_episode(
            item_type="evidence",
            item_id="ev-fail-001",
            payload={"id": "ev-fail-001", "snippet": "Critical evidence", "graph_sync_attempts": 2},
        )
        assert status_3 == GraphSyncStatus.FAILED
        assert worker.get_sync_status("ev-fail-001") == GraphSyncStatus.FAILED

        failed_updates = [
            (q, p) for q, p in executed_queries
            if p.get("status") == "FAILED" and p.get("item_id") == "ev-fail-001"
        ]
        assert len(failed_updates) == 1
        _, p_failed = failed_updates[0]
        assert p_failed["attempts"] == 3
        assert "Graphiti cluster unreachable" in p_failed["last_error"]

        # Invariant maintained: Authoritative Neo4j data never deleted/rolled back
        assert not any("DELETE" in q.upper() for q, _ in executed_queries)


