"""CheckpointRepository: Repository for managing IngestionCheckpoint nodes in Neo4j."""

from datetime import datetime, timezone
import logging
from typing import Optional
from uuid import uuid4

from neo4j import AsyncDriver
from ptb_contracts.l1_acquisition import (
    IngestionCheckpointRecord,
    SourceType,
)
from ptb_database.neo4j_client import Neo4jClient

logger = logging.getLogger("ptb.database.repositories.checkpoint_repo")


class CheckpointRepository:
    """Repository quản lý IngestionCheckpoint trong Neo4j."""

    def __init__(self, neo4j_client: Neo4jClient) -> None:
        self.neo4j_client = neo4j_client

    def _get_driver(self) -> AsyncDriver:
        return self.neo4j_client.get_driver()

    async def get_checkpoint(
        self,
        source_type: str,
        stream_id: str,
        tenant_id: str = "default",
    ) -> Optional[IngestionCheckpointRecord]:
        """Tìm node (:IngestionCheckpoint {source_type: ..., stream_id: ..., tenant_id: ...})."""
        driver = self._get_driver()
        st_val = source_type.value if hasattr(source_type, "value") else str(source_type)
        tid_val = tenant_id if (tenant_id is not None and str(tenant_id).strip() != "") else "default"
        cypher = """
        MATCH (cp:IngestionCheckpoint)
        WHERE cp.source_type = $source_type
          AND cp.stream_id = $stream_id
          AND cp.tenant_id = $tenant_id
        RETURN cp
        ORDER BY cp.updated_at DESC
        LIMIT 1
        """
        params = {
            "source_type": st_val,
            "stream_id": stream_id,
            "tenant_id": tid_val,
        }
        async with driver.session(database=self.neo4j_client.database) as session:
            result = await session.run(cypher, params)
            row = await result.single()
            if not row or not row["cp"]:
                return None
            data = dict(row["cp"])
            for dt_field in ["last_event_timestamp", "updated_at"]:
                if dt_field in data and isinstance(data[dt_field], str):
                    try:
                        data[dt_field] = datetime.fromisoformat(data[dt_field])
                    except Exception:
                        pass
            return IngestionCheckpointRecord.model_validate(data)

    async def save_checkpoint(self, checkpoint: IngestionCheckpointRecord) -> None:
        """MERGE trên checkpoint id hoặc composite key, SET last_external_id, last_event_timestamp, cursor_token, updated_at."""
        driver = self._get_driver()
        st_val = checkpoint.source_type.value if hasattr(checkpoint.source_type, "value") else str(checkpoint.source_type)
        cp_id = checkpoint.id or f"{checkpoint.tenant_id}:{st_val}:{checkpoint.stream_id}"

        last_event_timestamp = (
            checkpoint.last_event_timestamp.isoformat()
            if checkpoint.last_event_timestamp and hasattr(checkpoint.last_event_timestamp, "isoformat")
            else (str(checkpoint.last_event_timestamp) if checkpoint.last_event_timestamp else None)
        )
        updated_at = (
            checkpoint.updated_at.isoformat()
            if checkpoint.updated_at and hasattr(checkpoint.updated_at, "isoformat")
            else datetime.now(timezone.utc).isoformat()
        )

        cypher = """
        MERGE (cp:IngestionCheckpoint {id: $id})
        SET cp.source_type = $source_type,
            cp.stream_id = $stream_id,
            cp.tenant_id = $tenant_id,
            cp.last_external_id = $last_external_id,
            cp.last_event_timestamp = $last_event_timestamp,
            cp.cursor_token = $cursor_token,
            cp.updated_at = $updated_at
        """
        params = {
            "id": cp_id,
            "source_type": st_val,
            "stream_id": checkpoint.stream_id,
            "tenant_id": checkpoint.tenant_id,
            "last_external_id": checkpoint.last_external_id,
            "last_event_timestamp": last_event_timestamp,
            "cursor_token": checkpoint.cursor_token,
            "updated_at": updated_at,
        }
        async with driver.session(database=self.neo4j_client.database) as session:
            await session.run(cypher, params)

    async def list_checkpoints(self) -> list[IngestionCheckpointRecord]:
        """Lấy toàn bộ danh sách IngestionCheckpoint hiện có."""
        driver = self._get_driver()
        cypher = """
        MATCH (cp:IngestionCheckpoint)
        RETURN cp
        ORDER BY cp.updated_at DESC
        """
        checkpoints: list[IngestionCheckpointRecord] = []
        async with driver.session(database=self.neo4j_client.database) as session:
            result = await session.run(cypher)
            async for row in result:
                node = row.get("cp")
                if node:
                    data = dict(node)
                    for dt_field in ["last_event_timestamp", "updated_at"]:
                        if dt_field in data and isinstance(data[dt_field], str):
                            try:
                                data[dt_field] = datetime.fromisoformat(data[dt_field])
                            except Exception:
                                pass
                    checkpoints.append(IngestionCheckpointRecord.model_validate(data))
        return checkpoints

    async def cleanup_duplicate_checkpoints(self) -> int:
        """Tìm và xóa các IngestionCheckpoint bị trùng lặp theo (tenant_id, source_type, stream_id),
        giữ lại node có updated_at mới nhất. Trả về số lượng nodes đã xóa."""
        driver = self._get_driver()
        # Đảm bảo các node checkpoint không có tenant_id rỗng hoặc null
        cypher_fix_null = """
        MATCH (cp:IngestionCheckpoint)
        WHERE cp.tenant_id IS NULL OR cp.tenant_id = ''
        SET cp.tenant_id = 'default'
        """
        cypher_dedup = """
        MATCH (cp:IngestionCheckpoint)
        WITH cp
        ORDER BY cp.updated_at DESC
        WITH cp.tenant_id AS tenant_id, cp.source_type AS source_type, cp.stream_id AS stream_id, collect(cp) AS nodes
        WHERE size(nodes) > 1
        UNWIND tail(nodes) AS dup
        DETACH DELETE dup
        RETURN count(dup) AS deleted_count
        """
        async with driver.session(database=self.neo4j_client.database) as session:
            await session.run(cypher_fix_null)
            result = await session.run(cypher_dedup)
            row = await result.single()
            if row and row.get("deleted_count") is not None:
                return int(row["deleted_count"])
            return 0
