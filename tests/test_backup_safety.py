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
