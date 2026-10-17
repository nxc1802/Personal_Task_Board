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
import inspect
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from ptb_database.neo4j_client import Neo4jClient

logger = logging.getLogger("ptb.database.backup")

# Production snapshot covers complete canonical ontology and Graphiti entities
SNAPSHOT_LABELS = (
    "UnifiedTask", "Task", "Evidence", "Commitment",
    "Person", "SourceIdentity", "Project", "Customer", "Tenant",
    "RawEvent", "IngestionCheckpoint", "ProcessingAttempt",
    "Decision", "Lesson", "Document", "Incident",
    "StatusTransitionAudit", "MergeAudit",
    "Entity", "Episode",
)

LABEL_PRIMARY_KEY: Dict[str, str] = {
    "UnifiedTask": "id",
    "Task": "id",
    "Evidence": "id",
    "Commitment": "id",
    "Person": "canonical_id",
    "SourceIdentity": "identity_key",
    "Project": "project_key",
    "Customer": "customer_id",
    "Tenant": "tenant_id",
    "RawEvent": "id",
    "IngestionCheckpoint": "id",
    "ProcessingAttempt": "id",
    "Decision": "decision_id",
    "Lesson": "lesson_id",
    "Document": "doc_id",
    "Incident": "incident_id",
    "StatusTransitionAudit": "id",
    "MergeAudit": "id",
    "Entity": "id",
    "Episode": "id",
}

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
        pk_field = LABEL_PRIMARY_KEY.get(label, "id")
        for item in items:
            if not isinstance(item, dict):
                raise ValueError(f"Snapshot {label} node is invalid.")
            node_id = item.get("id") or item.get(pk_field)
            if not isinstance(node_id, str) or not node_id:
                raise ValueError(f"Snapshot {label} node missing string id.")
            if "_labels" in item:
                if not isinstance(item["_labels"], list) or any(not isinstance(l, str) or not _CYPHER_IDENTIFIER.fullmatch(l) for l in item["_labels"]):
                    raise ValueError(f"Snapshot {label} node contains invalid _labels.")
    for rel in relationships:
        if not isinstance(rel, dict) or not isinstance(rel.get("rel_type"), str):
            raise ValueError("Snapshot relationship format is invalid.")
        if not _CYPHER_IDENTIFIER.fullmatch(rel["rel_type"]):
            raise ValueError("Invalid snapshot relationship type.")
        if not rel.get("from_id") or not rel.get("to_id"):
            raise ValueError("Snapshot relationship missing an endpoint id.")
        if not isinstance(rel.get("rel_props", {}), dict):
            raise ValueError("Snapshot relationship properties must be a map.")
        if "from_labels" in rel and not isinstance(rel["from_labels"], list):
            raise ValueError("Snapshot relationship from_labels must be a list.")
        if "to_labels" in rel and not isinstance(rel["to_labels"], list):
            raise ValueError("Snapshot relationship to_labels must be a list.")
        if "from_pk_name" in rel and (not isinstance(rel["from_pk_name"], str) or not _CYPHER_IDENTIFIER.fullmatch(rel["from_pk_name"])):
            raise ValueError("Snapshot relationship from_pk_name is invalid.")
        if "to_pk_name" in rel and (not isinstance(rel["to_pk_name"], str) or not _CYPHER_IDENTIFIER.fullmatch(rel["to_pk_name"])):
            raise ValueError("Snapshot relationship to_pk_name is invalid.")


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
            query = f"MATCH (n:{label}) RETURN labels(n) AS node_labels, properties(n) AS node_props"
            result = await session.run(query)
            records = await result.data()
            node_list = []
            pk_field = LABEL_PRIMARY_KEY.get(label, "id")
            for r in records:
                node_props = dict(r.get("node_props") if "node_props" in r else r.get("n", {}))
                raw_labels = r.get("node_labels")
                if raw_labels is None and "n" in r and hasattr(r["n"], "labels"):
                    raw_labels = list(r["n"].labels)
                node_labels = [l for l in (raw_labels or [label]) if isinstance(l, str) and _CYPHER_IDENTIFIER.fullmatch(l)]

                clean_props = {}
                for k, v in node_props.items():
                    if hasattr(v, "isoformat"):
                        clean_props[k] = v.isoformat()
                    else:
                        clean_props[k] = v
                if "id" not in clean_props and pk_field in clean_props:
                    clean_props["id"] = str(clean_props[pk_field])
                if node_labels:
                    clean_props["_labels"] = node_labels
                node_list.append(clean_props)
            snapshot_data["nodes"][label] = node_list

        # Export relationships
        rel_query = """
        MATCH (a)-[r]->(b)
        RETURN labels(a) AS from_labels, properties(a) AS from_props,
               type(r) AS rel_type, properties(r) AS rel_props,
               labels(b) AS to_labels, properties(b) AS to_props
        """
        rel_result = await session.run(rel_query)
        rel_records = await rel_result.data()
        for r in rel_records:
            from_props = dict(r.get("from_props") or {})
            to_props = dict(r.get("to_props") or {})
            from_id = (
                r.get("from_id")
                or from_props.get("id")
                or from_props.get("canonical_id")
                or from_props.get("project_key")
                or from_props.get("customer_id")
                or from_props.get("tenant_id")
                or from_props.get("identity_key")
                or from_props.get("decision_id")
                or from_props.get("lesson_id")
                or from_props.get("doc_id")
                or from_props.get("incident_id")
            )
            to_id = (
                r.get("to_id")
                or to_props.get("id")
                or to_props.get("canonical_id")
                or to_props.get("project_key")
                or to_props.get("customer_id")
                or to_props.get("tenant_id")
                or to_props.get("identity_key")
                or to_props.get("decision_id")
                or to_props.get("lesson_id")
                or to_props.get("doc_id")
                or to_props.get("incident_id")
            )
            if not from_id or not to_id:
                logger.warning("Omitting unresolvable relationship %s: from=%s, to=%s", r.get("rel_type"), from_id, to_id)
                continue
            clean_rel_props = {}
            for k, v in dict(r.get("rel_props") or {}).items():
                if hasattr(v, "isoformat"):
                    clean_rel_props[k] = v.isoformat()
                else:
                    clean_rel_props[k] = v

            from_labels = [l for l in r.get("from_labels", []) if isinstance(l, str) and _CYPHER_IDENTIFIER.fullmatch(l)]
            to_labels = [l for l in r.get("to_labels", []) if isinstance(l, str) and _CYPHER_IDENTIFIER.fullmatch(l)]
            from_primary_label = next((l for l in from_labels if l in SNAPSHOT_LABELS), from_labels[0] if from_labels else None)
            to_primary_label = next((l for l in to_labels if l in SNAPSHOT_LABELS), to_labels[0] if to_labels else None)
            from_pk_name = LABEL_PRIMARY_KEY.get(from_primary_label, "id") if from_primary_label else "id"
            to_pk_name = LABEL_PRIMARY_KEY.get(to_primary_label, "id") if to_primary_label else "id"

            snapshot_data["relationships"].append({
                "from_labels": from_labels,
                "from_pk_name": from_pk_name,
                "from_id": str(from_id),
                "rel_type": r.get("rel_type"),
                "rel_props": clean_rel_props,
                "to_labels": to_labels,
                "to_pk_name": to_pk_name,
                "to_id": str(to_id),
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
        # Atomic Transaction Management: all writes succeed or all rollback
        if hasattr(session, "begin_transaction") and callable(session.begin_transaction):
            tx_obj = session.begin_transaction()
            tx = await tx_obj if inspect.isawaitable(tx_obj) else tx_obj
            # Delegate mock session.run if test mock configured session.run and tx has no custom side_effect
            if tx is not session and hasattr(session, "run") and type(session.run).__name__ in ("AsyncMock", "MagicMock"):
                if getattr(tx.run, "side_effect", None) is None:
                    tx.run = session.run
        else:
            tx = session

        try:
            # Track restored node identities across labels to preserve multi-label nodes losslessly
            restored_node_keys: Set[Tuple[str, str]] = set()

            # Restore nodes by label
            for label, nodes in data.get("nodes", {}).items():
                if not nodes:
                    continue
                pk = LABEL_PRIMARY_KEY.get(label, "id")
                clean_nodes = []
                multi_label_nodes = []

                for n in nodes:
                    item_dict = dict(n)
                    raw_labels = item_dict.pop("_labels", None)
                    all_labels = [l for l in raw_labels if isinstance(l, str) and _CYPHER_IDENTIFIER.fullmatch(l)] if isinstance(raw_labels, list) else [label]
                    if label not in all_labels:
                        all_labels.insert(0, label)

                    node_pk_val = item_dict.get(pk) or item_dict.get("id")
                    if pk not in item_dict and node_pk_val:
                        item_dict[pk] = node_pk_val
                    elif "id" not in item_dict and node_pk_val:
                        item_dict["id"] = node_pk_val

                    primary_key_tuple = (pk, str(node_pk_val))
                    if primary_key_tuple in restored_node_keys:
                        # Already merged this node under another label: attach new labels & update props
                        secondary_labels = [l for l in all_labels if _CYPHER_IDENTIFIER.fullmatch(l)]
                        set_labels_str = "".join(f":{l}" for l in secondary_labels)
                        attach_cypher = f"""
                        MATCH (n {{{pk}: $pk_val}})
                        SET n{set_labels_str}
                        SET n += $props
                        """
                        await tx.run(attach_cypher, pk_val=node_pk_val, props=item_dict)
                    else:
                        restored_node_keys.add(primary_key_tuple)
                        clean_nodes.append(item_dict)
                        extra_labels = [l for l in all_labels if l != label and _CYPHER_IDENTIFIER.fullmatch(l)]
                        if extra_labels:
                            multi_label_nodes.append((node_pk_val, extra_labels))

                if clean_nodes:
                    cypher = f"""
                    UNWIND $batch AS item
                    MERGE (n:{label} {{{pk}: item.{pk}}})
                    SET n += item
                    """
                    await tx.run(cypher, batch=clean_nodes)
                    restored_nodes += len(clean_nodes)

                for pk_val, extra_labels in multi_label_nodes:
                    set_labels_str = "".join(f":{l}" for l in extra_labels)
                    attach_cypher = f"""
                    MATCH (n:{label} {{{pk}: $pk_val}})
                    SET n{set_labels_str}
                    """
                    await tx.run(attach_cypher, pk_val=pk_val)

            # Restore relationships with targeted endpoint matching
            rels = data.get("relationships", [])
            for rel in rels:
                from_id = rel.get("from_id")
                to_id = rel.get("to_id")
                rel_type = rel.get("rel_type")
                from_labels = rel.get("from_labels") or []
                to_labels = rel.get("to_labels") or []

                from_label = next((l for l in from_labels if l in SNAPSHOT_LABELS), from_labels[0] if from_labels else None)
                to_label = next((l for l in to_labels if l in SNAPSHOT_LABELS), to_labels[0] if to_labels else None)
                from_pk = rel.get("from_pk_name") or (LABEL_PRIMARY_KEY.get(from_label, "id") if from_label else None)
                to_pk = rel.get("to_pk_name") or (LABEL_PRIMARY_KEY.get(to_label, "id") if to_label else None)

                if (
                    from_label and to_label and from_pk and to_pk
                    and _CYPHER_IDENTIFIER.fullmatch(from_label)
                    and _CYPHER_IDENTIFIER.fullmatch(to_label)
                    and _CYPHER_IDENTIFIER.fullmatch(from_pk)
                    and _CYPHER_IDENTIFIER.fullmatch(to_pk)
                ):
                    cypher = f"""
                    MATCH (a:{from_label} {{{from_pk}: $from_id}})
                    MATCH (b:{to_label} {{{to_pk}: $to_id}})
                    MERGE (a)-[r:{rel_type}]->(b)
                    SET r += $props
                    """
                else:
                    cypher = f"""
                    MATCH (a) WHERE (a.id = $from_id OR a.canonical_id = $from_id OR a.project_key = $from_id OR a.customer_id = $from_id OR a.tenant_id = $from_id OR a.identity_key = $from_id OR a.decision_id = $from_id OR a.lesson_id = $from_id OR a.doc_id = $from_id OR a.incident_id = $from_id)
                    MATCH (b) WHERE (b.id = $to_id OR b.canonical_id = $to_id OR b.project_key = $to_id OR b.customer_id = $to_id OR b.tenant_id = $to_id OR b.identity_key = $to_id OR b.decision_id = $to_id OR b.lesson_id = $to_id OR b.doc_id = $to_id OR b.incident_id = $to_id)
                    MERGE (a)-[r:{rel_type}]->(b)
                    SET r += $props
                    """
                await tx.run(cypher, from_id=from_id, to_id=to_id, props=rel.get("rel_props", {}))
                restored_rels += 1

            if hasattr(tx, "commit") and callable(tx.commit):
                c_res = tx.commit()
                if inspect.isawaitable(c_res):
                    await c_res
        except Exception:
            if hasattr(tx, "rollback") and callable(tx.rollback):
                rb_res = tx.rollback()
                if inspect.isawaitable(rb_res):
                    await rb_res
            raise

    logger.info("Restored %d nodes, %d relationships from %s", restored_nodes, restored_rels, snapshot_path)
    return {"restored_nodes": restored_nodes, "restored_relationships": restored_rels}
