"""Neo4j PTB domain snapshot export/restore (NOT a full database backup).

Snapshots cover an explicit allowlist of domain labels; Graphiti and other
Neo4j nodes may be excluded. Use a native Neo4j backup for full disaster
recovery and verify restore on an isolated database before production use.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from ptb_database.neo4j_client import Neo4jClient

logger = logging.getLogger("ptb.database.backup")

# This is a DOMAIN snapshot, not a lossless Neo4j/Graphiti backup.
# The explicit scope must be audited against the actual production labels.
SNAPSHOT_LABELS = (
    "Task", "Evidence", "RawEvent", "IngestionCheckpoint",
    "Person", "Project", "Customer", "Decision", "Lesson",
)
_CYPHER_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")


def _validate_snapshot_payload(data: Any) -> None:
    """Validate *everything* before opening a database write session."""
    if not isinstance(data, dict) or data.get("format_version") != "1.0":
        raise ValueError("Unsupported or malformed snapshot format_version.")
    digest = data.get("sha256")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ValueError("Snapshot sha256 checksum is required.")
    unsigned = dict(data)
    unsigned.pop("sha256")
    actual = hashlib.sha256(
        json.dumps(unsigned, indent=2, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if actual.lower() != digest.lower():
        raise ValueError("Snapshot checksum mismatch: file contents have changed.")
    nodes = data.get("nodes")
    relationships = data.get("relationships")
    if not isinstance(nodes, dict) or not isinstance(relationships, list):
        raise ValueError("Snapshot nodes/relationships must be a map and list.")
    if any(label not in SNAPSHOT_LABELS for label in nodes):
        raise ValueError("Snapshot contains a label outside the supported domain allowlist.")
    for label, items in nodes.items():
        if not isinstance(items, list):
            raise ValueError(f"Snapshot node list for {label} is invalid.")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                raise ValueError(f"Snapshot {label} node missing string id.")
    for rel in relationships:
        if not isinstance(rel, dict) or not isinstance(rel.get("rel_type"), str):
            raise ValueError("Snapshot relationship format is invalid.")
        if not _CYPHER_IDENTIFIER.fullmatch(rel["rel_type"]):
            raise ValueError("Invalid snapshot relationship type.")
        if not rel.get("from_id") or not rel.get("to_id"):
            raise ValueError("Snapshot relationship missing an endpoint id.")
        if not isinstance(rel.get("rel_props", {}), dict):
            raise ValueError("Snapshot relationship properties must be a map.")


async def export_database_snapshot(
    client: Optional[Neo4jClient] = None,
    output_path: Optional[Path] = None,
) -> Path:
    """Export complete authoritative domain state from Neo4j to a verified JSON snapshot."""
    cli = client or Neo4jClient()
    driver = cli.get_driver()

    snapshot_data: Dict[str, Any] = {
        "format_version": "1.0",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "database": cli.database,
        "nodes": {},
        "relationships": [],
    }

    labels_to_export = SNAPSHOT_LABELS

    async with driver.session(database=cli.database) as session:
        for label in labels_to_export:
            query = f"MATCH (n:{label}) RETURN n"
            result = await session.run(query)
            records = await result.data()
            node_list = []
            for r in records:
                node_props = dict(r["n"])
                # Serialize any non-json types (e.g. DateTime)
                clean_props = {}
                for k, v in node_props.items():
                    if hasattr(v, "isoformat"):
                        clean_props[k] = v.isoformat()
                    else:
                        clean_props[k] = v
                node_list.append(clean_props)
            snapshot_data["nodes"][label] = node_list

        # Export relationships
        rel_query = """
        MATCH (a)-[r]->(b)
        RETURN labels(a) AS from_labels, a.id AS from_id, type(r) AS rel_type, properties(r) AS rel_props, labels(b) AS to_labels, b.id AS to_id
        """
        rel_result = await session.run(rel_query)
        rel_records = await rel_result.data()
        for r in rel_records:
            clean_rel_props = {}
            for k, v in dict(r.get("rel_props") or {}).items():
                if hasattr(v, "isoformat"):
                    clean_rel_props[k] = v.isoformat()
                else:
                    clean_rel_props[k] = v
            snapshot_data["relationships"].append({
                "from_labels": r.get("from_labels", []),
                "from_id": r.get("from_id"),
                "rel_type": r.get("rel_type"),
                "rel_props": clean_rel_props,
                "to_labels": r.get("to_labels", []),
                "to_id": r.get("to_id"),
            })

    # Compute content checksum
    serialized_bytes = json.dumps(snapshot_data, indent=2, sort_keys=True).encode("utf-8")
    content_sha256 = hashlib.sha256(serialized_bytes).hexdigest()
    snapshot_data["sha256"] = content_sha256

    final_bytes = json.dumps(snapshot_data, indent=2, sort_keys=True).encode("utf-8")

    if output_path is None:
        backups_dir = Path("backups")
        backups_dir.mkdir(parents=True, exist_ok=True)
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        target_file = backups_dir / f"ptb_snapshot_{ts_str}.json"
    else:
        target_file = output_path
        target_file.parent.mkdir(parents=True, exist_ok=True)

    # A snapshot can contain sensitive task content or credentials.
    # Atomic replacement prevents truncated JSON after process interruption.
    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=target_file.parent,
            prefix=".ptb-snapshot-", suffix=".tmp", delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(final_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_path, 0o600)
        except OSError as exc:
            logger.warning("Unable to set snapshot file permissions: %s", exc)
        os.replace(temp_path, target_file)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    total_nodes = sum(len(v) for v in snapshot_data["nodes"].values())
    total_rels = len(snapshot_data["relationships"])
    logger.info(
        "Exported snapshot to %s: %d nodes, %d relationships, sha256=%s",
        target_file,
        total_nodes,
        total_rels,
        content_sha256[:10],
    )
    return target_file


async def restore_database_snapshot(
    snapshot_path: Path,
    client: Optional[Neo4jClient] = None,
) -> Dict[str, int]:
    """Restore database from a verified JSON snapshot."""
    if not snapshot_path.is_file():
        raise FileNotFoundError(f"Snapshot file not found: {snapshot_path}")

    raw_bytes = snapshot_path.read_bytes()
    data = json.loads(raw_bytes.decode("utf-8"))

    # Fail closed: unsigned, tampered or malformed data must never reach Cypher.
    _validate_snapshot_payload(data)

    cli = client or Neo4jClient()
    driver = cli.get_driver()

    restored_nodes = 0
    restored_rels = 0

    async with driver.session(database=cli.database) as session:
        # Restore nodes by label
        for label, nodes in data.get("nodes", {}).items():
            if not nodes:
                continue
            cypher = f"""
            UNWIND $batch AS item
            MERGE (n:{label} {{id: item.id}})
            SET n += item
            """
            await session.run(cypher, batch=nodes)
            restored_nodes += len(nodes)

        # Restore relationships
        rels = data.get("relationships", [])
        for rel in rels:
            from_id = rel.get("from_id")
            to_id = rel.get("to_id")
            rel_type = rel.get("rel_type")
            # Every relationship was validated before any write.
            # Missing endpoints remain a real-restore acceptance requirement.
            cypher = f"""
            MATCH (a {{id: $from_id}}), (b {{id: $to_id}})
            MERGE (a)-[r:{rel_type}]->(b)
            SET r += $props
            """
            await session.run(cypher, from_id=from_id, to_id=to_id, props=rel.get("rel_props", {}))
            restored_rels += 1

    logger.info("Restored %d nodes, %d relationships from %s", restored_nodes, restored_rels, snapshot_path)
    return {"restored_nodes": restored_nodes, "restored_relationships": restored_rels}
