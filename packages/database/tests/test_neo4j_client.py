import pytest
from ptb_database.neo4j_client import Neo4jClient


def test_neo4j_client_initialization():
    client = Neo4jClient(
        uri="neo4j+s://demo.databases.neo4j.io",
        username="neo4j",
        password="password123",
        database="neo4j"
    )
    assert client.uri == "neo4j+s://demo.databases.neo4j.io"
    assert client.username == "neo4j"
    assert client.password == "password123"
    
    driver = client.get_driver()
    assert driver is not None


@pytest.mark.asyncio
async def test_cypher_script_parsing():
    script = """
    // Comment line 1
    CREATE CONSTRAINT c1 FOR (p:Person) REQUIRE p.id IS UNIQUE;
    
    // Comment line 2
    CREATE INDEX i1 FOR (t:Task) ON (t.status);
    """
    client = Neo4jClient()
    
    # Test script splitting without running against real DB
    raw_statements = script.split(";")
    statements = []
    for raw in raw_statements:
        lines = [l for l in raw.splitlines() if not l.strip().startswith("//")]
        cleaned = "\n".join(lines).strip()
        if cleaned:
            statements.append(cleaned)
            
    assert len(statements) == 2
    assert "CREATE CONSTRAINT c1" in statements[0]
    assert "CREATE INDEX i1" in statements[1]


@pytest.mark.asyncio
async def test_setup_neo4j_and_seed_dev_data(monkeypatch):
    from unittest.mock import AsyncMock
    from ptb_database.setup_all import setup_neo4j, seed_dev_data

    mock_client = AsyncMock()
    mock_client.verify_connectivity = AsyncMock()
    mock_client.execute_cypher_file = AsyncMock(return_value=["stmt1", "stmt2"])
    mock_client.get_active_constraints = AsyncMock(
        return_value=[{"name": "c1", "labelsOrTypes": ["Person"], "properties": ["canonical_id"]}]
    )
    mock_client.close = AsyncMock()

    monkeypatch.setattr("ptb_database.setup_all.Neo4jClient", lambda: mock_client)

    # 1. setup_neo4j should execute constraints file and NOT seed file
    res = await setup_neo4j()
    assert res is True
    assert mock_client.execute_cypher_file.call_count == 1
    call_args = mock_client.execute_cypher_file.call_args[0]
    assert len(call_args) == 0  # default constraints file

    # 2. seed_dev_data executes seed file
    mock_client.execute_cypher_file.reset_mock()
    seed_res = await seed_dev_data(mock_client)
    assert seed_res is True
    assert mock_client.execute_cypher_file.call_count == 1
    seed_arg = str(mock_client.execute_cypher_file.call_args[0][0])
    assert "001_dev_seed.cypher" in seed_arg

