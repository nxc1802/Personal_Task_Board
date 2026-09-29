"""External integration tests requiring live Docker/Neo4j/OpenWebUI infrastructure.

These tests are automatically skipped during local no-Docker test runs unless
Neo4j Bolt is listening on 127.0.0.1:7687 or `--run-external` is explicitly passed.
"""

from __future__ import annotations

import os
import urllib.request
import pytest

from ptb_database.neo4j_client import Neo4jClient


@pytest.mark.external_integration
@pytest.mark.asyncio
async def test_live_neo4j_bolt_connectivity() -> None:
    """Verify live Bolt connectivity to a running Neo4j instance."""
    client = Neo4jClient()
    try:
        is_connected = await client.verify_connectivity()
        assert is_connected is True
    finally:
        await client.close()


@pytest.mark.external_integration
def test_live_openwebui_reachability() -> None:
    """Verify HTTP reachability of a running OpenWebUI container on 127.0.0.1:3000."""
    url = os.getenv("OPENWEBUI_URL", "http://127.0.0.1:3000")
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=2.0) as response:
        assert 200 <= response.status < 400
