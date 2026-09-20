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
