"""Fail-closed PTB domain JSON snapshot security regressions."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from unittest.mock import AsyncMock, MagicMock

import pytest

from ptb_database.backup import (
    _validate_snapshot_payload,
    export_database_snapshot,
    restore_database_snapshot,
)


def _valid_snapshot() -> dict:
    data = {
        "format_version": "1.0",
        "nodes": {"Task": [{"id": "t-1", "title": "Test"}]},
        "relationships": [],
    }
    data["sha256"] = hashlib.sha256(
        json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return data


@pytest.mark.unit
def test_snapshot_requires_checksum() -> None:
    data = _valid_snapshot()
    del data["sha256"]
    with pytest.raises(ValueError, match="checksum"):
        _validate_snapshot_payload(data)


@pytest.mark.unit
def test_snapshot_rejects_tampered_contents() -> None:
    data = _valid_snapshot()
    data["nodes"]["Task"][0]["title"] = "Tampered"
    with pytest.raises(ValueError, match="checksum mismatch"):
        _validate_snapshot_payload(data)


@pytest.mark.unit
def test_snapshot_rejects_unknown_labels_before_cypher() -> None:
    data = _valid_snapshot()
    data["nodes"] = {"Task`) DETACH DELETE n //": [{"id": "evil"}]}
    unsigned = dict(data)
    unsigned.pop("sha256")
    data["sha256"] = hashlib.sha256(json.dumps(unsigned, indent=2, sort_keys=True).encode("utf-8")).hexdigest()
    with pytest.raises(ValueError, match="label"):
        _validate_snapshot_payload(data)


@pytest.mark.unit
def test_snapshot_rejects_cypher_relationship_injection() -> None:
    data = _valid_snapshot()
    data["relationships"] = [{
        "from_id": "t-1",
        "to_id": "t-2",
        "rel_type": "FOLLOWS_UP`) DELETE r //",
        "rel_props": {},
    }]
    unsigned = dict(data)
    unsigned.pop("sha256")
    data["sha256"] = hashlib.sha256(json.dumps(unsigned, indent=2, sort_keys=True).encode("utf-8")).hexdigest()
    with pytest.raises(ValueError, match="relationship type"):
        _validate_snapshot_payload(data)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_restore_without_checksum_never_opens_driver(tmp_path: Path) -> None:
    source = tmp_path / "unsigned.json"
    payload = _valid_snapshot()
    del payload["sha256"]
    source.write_text(json.dumps(payload), encoding="utf-8")
    client = MagicMock()
    with pytest.raises(ValueError, match="checksum"):
        await restore_database_snapshot(source, client=client)
    client.get_driver.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_export_uses_atomic_private_file(tmp_path: Path) -> None:
    cli = MagicMock()
    cli.database = "neo4j"
    session = MagicMock()

    async def fake_run(query, **kwargs):
        result = MagicMock()
        result.data = AsyncMock(return_value=[])
        return result

    session.run = AsyncMock(side_effect=fake_run)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    driver = MagicMock()
    driver.session.return_value = session
    cli.get_driver.return_value = driver

    target = tmp_path / "backups" / "snapshot.json"
    result = await export_database_snapshot(cli, target)
    assert result == target
    assert target.is_file()
    assert not list(target.parent.glob(".ptb-snapshot-*.tmp"))
    assert _valid_snapshot()["sha256"] != json.loads(target.read_text())["sha256"]
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.unit
def test_snapshot_supports_canonical_ontology_keys() -> None:
    data = {
        "format_version": "1.0",
        "nodes": {
            "UnifiedTask": [{"id": "ut-101", "title": "Refactor Backup"}],
            "Person": [{"canonical_id": "lead.tran", "canonical_name": "Tran Van Lead"}],
            "Project": [{"project_key": "PTB", "name": "Personal Task Board"}],
            "Decision": [{"decision_id": "dec-01", "topic": "Neo4j 5.26"}],
        },
        "relationships": [
            {
                "from_labels": ["UnifiedTask"],
                "from_id": "ut-101",
                "rel_type": "ASSIGNED_TO",
                "rel_props": {},
                "to_labels": ["Person"],
                "to_id": "lead.tran",
            }
        ],
    }
    data["sha256"] = hashlib.sha256(
        json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    ).hexdigest()

    # Must validate with 0 exceptions
    _validate_snapshot_payload(data)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_restore_maps_non_id_primary_keys(tmp_path: Path) -> None:
    data = {
        "format_version": "1.0",
        "nodes": {
            "Person": [{"canonical_id": "cuong.dam", "canonical_name": "Dam Quang Cuong"}],
            "Project": [{"project_key": "SEC", "name": "Security Module"}],
        },
        "relationships": [],
    }
    data["sha256"] = hashlib.sha256(
        json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    ).hexdigest()
    snapshot_file = tmp_path / "valid_ontology.json"
    snapshot_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    session = MagicMock()
    executed_queries = []

    async def fake_run(query, **kwargs):
        executed_queries.append((query, kwargs))
        result = MagicMock()
        return result

    session.run = AsyncMock(side_effect=fake_run)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    driver = MagicMock()
    driver.session.return_value = session
    cli = MagicMock()
    cli.database = "neo4j"
    cli.get_driver.return_value = driver

    res = await restore_database_snapshot(snapshot_file, client=cli)
    assert res["restored_nodes"] == 2
    # Verify Person used canonical_id in MERGE and Project used project_key
    cypher_text = "\n".join(q[0] for q in executed_queries)
    assert "MERGE (n:Person {canonical_id: item.canonical_id})" in cypher_text
    assert "MERGE (n:Project {project_key: item.project_key})" in cypher_text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_restore_preserves_multi_label_nodes(tmp_path: Path) -> None:
    """Multi-label node (UnifiedTask:Task) must be restored as one node with both labels attached."""
    data = {
        "format_version": "1.0",
        "nodes": {
            "UnifiedTask": [{
                "id": "ut-99",
                "title": "Multi Label Task",
                "_labels": ["UnifiedTask", "Task"],
            }],
        },
        "relationships": [],
    }
    data["sha256"] = hashlib.sha256(
        json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    ).hexdigest()
    snapshot_file = tmp_path / "multi_label.json"
    snapshot_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    session = MagicMock()
    executed_queries = []

    async def fake_run(query, **kwargs):
        executed_queries.append((query, kwargs))
        return MagicMock()

    session.run = AsyncMock(side_effect=fake_run)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    driver = MagicMock()
    driver.session.return_value = session
    cli = MagicMock()
    cli.database = "neo4j"
    cli.get_driver.return_value = driver

    res = await restore_database_snapshot(snapshot_file, client=cli)
    assert res["restored_nodes"] == 1
    cypher_text = "\n".join(q[0] for q in executed_queries)
    assert "MERGE (n:UnifiedTask {id: item.id})" in cypher_text
    assert "SET n:Task" in cypher_text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_restore_uses_targeted_endpoint_matching(tmp_path: Path) -> None:
    """Relationship restore with label metadata must target exact endpoints to prevent cross-type collisions."""
    data = {
        "format_version": "1.0",
        "nodes": {
            "UnifiedTask": [{"id": "ut-100", "title": "Targeted Endpoints"}],
            "Person": [{"canonical_id": "lead.tran", "canonical_name": "Tran Van Lead"}],
        },
        "relationships": [
            {
                "from_labels": ["UnifiedTask"],
                "from_pk_name": "id",
                "from_id": "ut-100",
                "rel_type": "ASSIGNED_TO",
                "rel_props": {},
                "to_labels": ["Person"],
                "to_pk_name": "canonical_id",
                "to_id": "lead.tran",
            }
        ],
    }
    data["sha256"] = hashlib.sha256(
        json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    ).hexdigest()
    snapshot_file = tmp_path / "targeted_rel.json"
    snapshot_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    session = MagicMock()
    executed_queries = []

    async def fake_run(query, **kwargs):
        executed_queries.append((query, kwargs))
        return MagicMock()

    session.run = AsyncMock(side_effect=fake_run)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    driver = MagicMock()
    driver.session.return_value = session
    cli = MagicMock()
    cli.database = "neo4j"
    cli.get_driver.return_value = driver

    res = await restore_database_snapshot(snapshot_file, client=cli)
    assert res["restored_relationships"] == 1
    rel_query = executed_queries[-1][0]
    assert "MATCH (a:UnifiedTask {id: $from_id})" in rel_query
    assert "MATCH (b:Person {canonical_id: $to_id})" in rel_query


@pytest.mark.unit
@pytest.mark.asyncio
async def test_restore_atomic_rollback_on_failure(tmp_path: Path) -> None:
    """Failure mid-restore must roll back the transaction."""
    data = {
        "format_version": "1.0",
        "nodes": {
            "UnifiedTask": [{"id": "ut-fail", "title": "Fail Task"}],
        },
        "relationships": [],
    }
    data["sha256"] = hashlib.sha256(
        json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    ).hexdigest()
    snapshot_file = tmp_path / "fail.json"
    snapshot_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    session = MagicMock()
    tx = MagicMock()
    tx.run = AsyncMock(side_effect=RuntimeError("Database constraint failure"))
    tx.rollback = AsyncMock()
    session.begin_transaction = MagicMock(return_value=tx)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    driver = MagicMock()
    driver.session.return_value = session
    cli = MagicMock()
    cli.database = "neo4j"
    cli.get_driver.return_value = driver

    with pytest.raises(RuntimeError, match="Database constraint failure"):
        await restore_database_snapshot(snapshot_file, client=cli)
    tx.rollback.assert_awaited_once()

