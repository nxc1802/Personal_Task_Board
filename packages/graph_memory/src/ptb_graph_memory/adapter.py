"""Adapter and graceful fallback wrapper for graphiti-core.

Ensures that if graphiti-core is missing, C extensions fail to compile,
or the environment is offline / lacks LLM API credentials, the system
falls back gracefully without breaking domain flows.
"""

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("ptb.graph_memory.adapter")

# Graceful import check for graphiti-core
try:
    import graphiti_core
    from graphiti_core import Graphiti
    from graphiti_core.nodes import EpisodeType
    HAS_GRAPHITI_CORE = True

    try:
        from graphiti_core.driver.neo4j_driver import Neo4jDriver, get_range_indices, get_fulltext_indices

        async def _safe_build_indices_and_constraints(self, delete_existing: bool = False):
            """Safely execute indices setup without leaving unawaited coroutines if connection fails."""
            try:
                if delete_existing:
                    await self.delete_all_indexes()
                range_indices = get_range_indices(self.provider)
                fulltext_indices = get_fulltext_indices(self.provider)
                for q in range_indices + fulltext_indices:
                    try:
                        await self._execute_index_query(q)
                    except Exception as query_exc:
                        logger.debug("Safe index setup query interrupted: %s", query_exc)
                        break
            except Exception as build_exc:
                logger.debug("Safe indices and constraints setup finished: %s", build_exc)

        Neo4jDriver.build_indices_and_constraints = _safe_build_indices_and_constraints
    except Exception as patch_exc:
        logger.debug("Could not patch Neo4jDriver.build_indices_and_constraints: %s", patch_exc)

except (ImportError, Exception) as exc:
    HAS_GRAPHITI_CORE = False
    Graphiti = None
    EpisodeType = None
    logger.info("graphiti-core is not available (%s). Operating in graceful fallback mode.", exc)


try:
    from graphiti_core.embedder.client import EmbedderClient
except Exception:
    class EmbedderClient:  # type: ignore
        pass

try:
    from graphiti_core.cross_encoder.client import CrossEncoderClient
except Exception:
    class CrossEncoderClient:  # type: ignore
        pass


class GraphitiQwen3Embedder(EmbedderClient):
    """Adapter wrapping Qwen3EmbeddingService for Graphiti-Core."""

    def __init__(self, embedding_service: Optional[Any] = None) -> None:
        self.embedding_service = embedding_service

    def _get_service(self) -> Any:
        if self.embedding_service is None:
            try:
                from packages.ai_service import Qwen3EmbeddingService
            except ImportError:
                from ai_service import Qwen3EmbeddingService
            self.embedding_service = Qwen3EmbeddingService()
        return self.embedding_service

    async def create(self, input_data: Any) -> List[float]:
        service = self._get_service()
        if isinstance(input_data, str):
            return await asyncio.to_thread(service.embed_text, input_data)
        elif isinstance(input_data, list) and input_data and isinstance(input_data[0], str):
            res = await asyncio.to_thread(service.embed_batch, input_data)
            return res[0] if res else []
        return []

    async def create_batch(self, input_data_list: List[str]) -> List[List[float]]:
        service = self._get_service()
        return await asyncio.to_thread(service.embed_batch, input_data_list)


class GraphitiKevCrossEncoder(CrossEncoderClient):
    """Adapter wrapping KevDecisionReranker for Graphiti-Core."""

    def __init__(self, reranker_service: Optional[Any] = None) -> None:
        self.reranker_service = reranker_service

    def _get_service(self) -> Any:
        if self.reranker_service is None:
            try:
                from packages.ai_service import KevDecisionReranker
            except ImportError:
                from ai_service import KevDecisionReranker
            self.reranker_service = KevDecisionReranker()
        return self.reranker_service

    async def rank(self, query: str, passages: List[str]) -> List[Tuple[str, float]]:
        if not passages:
            return []
        service = self._get_service()
        try:
            results = await asyncio.to_thread(service.rerank, query, passages)
            ranked = []
            for item in results:
                ranked.append((item.get("document", ""), float(item.get("score", 0.0))))
            return sorted(ranked, key=lambda x: x[1], reverse=True)
        except Exception as exc:
            logger.error("Kev cross-encoder ranking failed: %s", exc)
            raise RuntimeError(f"Kev cross-encoder ranking failed: {exc}") from exc


class GraphitiAdapter:
    """Adapter wrapping graphiti_core.Graphiti with graceful degradation & fallback.
    
    Graphiti represents the derived semantic memory layer in the Single-Store Neo4j
    architecture. Any failure in this layer must NEVER rollback or interrupt authoritative
    domain operations.
    """

    def __init__(
        self,
        graphiti_instance: Optional[Any] = None,
        uri: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None,
        llm_client: Optional[Any] = None,
        embedder: Optional[Any] = None,
        cross_encoder: Optional[Any] = None,
        enabled: bool = True,
    ) -> None:
        self.enabled = enabled
        self._graphiti = graphiti_instance
        self._is_initialized = False
        self.last_error: Optional[Exception] = None

        if not self.enabled:
            return

        if self._graphiti is not None:
            self._is_initialized = True
            return

        self.embedder = embedder
        self.cross_encoder = cross_encoder

        if HAS_GRAPHITI_CORE and Graphiti is not None and uri:
            try:
                # Wire Qwen3 embedder if configured / enabled and not explicitly provided
                if embedder is None:
                    is_qwen_configured = bool(
                        os.getenv("EMBEDDING_BASE_URL")
                        or os.getenv("QWEN_EMBEDDING_URL")
                        or os.getenv("PTB_ENABLE_LOCAL_AI", "false").lower() in ("true", "1", "yes")
                    )
                    if is_qwen_configured:
                        try:
                            embedder = GraphitiQwen3Embedder()
                        except Exception as e_err:
                            logger.debug("Failed initializing GraphitiQwen3Embedder: %s", e_err)

                # Wire Kev cross-encoder if configured / enabled and not explicitly provided
                if cross_encoder is None:
                    is_kev_configured = bool(
                        os.getenv("DECISION_BASE_URL")
                        or os.getenv("KEV_DECISION_URL")
                        or os.getenv("PTB_ENABLE_LOCAL_AI", "false").lower() in ("true", "1", "yes")
                    )
                    if is_kev_configured:
                        try:
                            cross_encoder = GraphitiKevCrossEncoder()
                        except Exception as k_err:
                            logger.debug("Failed initializing GraphitiKevCrossEncoder: %s", k_err)

                self.embedder = embedder
                self.cross_encoder = cross_encoder

                # Attempt to initialize Graphiti with provided connection details
                self._graphiti = Graphiti(
                    uri=uri,
                    user=user,
                    password=password,
                    llm_client=llm_client,
                    embedder=embedder,
                    cross_encoder=cross_encoder,
                )
                self._is_initialized = True
                logger.info("Graphiti-core initialized successfully for URI: %s", uri)
            except Exception as e:
                logger.warning(
                    "Graphiti-core initialization fallback: %s. Continuing in fallback mode.",
                    e,
                )
                self._graphiti = None
                self._is_initialized = False

    @property
    def is_available(self) -> bool:
        """Return True if Graphiti instance is actively ready."""
        return self.enabled and self._graphiti is not None and self._is_initialized

    async def add_episode(
        self,
        name: str,
        episode_body: str,
        source_description: str,
        reference_time: Union[datetime, str],
        source_type: Optional[str] = "text",
        group_id: Optional[str] = None,
        uuid: Optional[str] = None,
    ) -> Optional[Any]:
        """Safely ingest an episode into Graphiti.
        
        Guaranteed to never raise an exception that could interrupt domain operations.
        """
        if not self.is_available:
            logger.debug("GraphitiAdapter not available; skipping episode '%s'", name)
            self.last_error = RuntimeError(f"GraphitiAdapter is offline or unavailable for episode '{name}'")
            return None

        # Normalize reference_time to datetime
        ref_dt = reference_time
        if isinstance(ref_dt, str):
            try:
                ref_dt = datetime.fromisoformat(ref_dt)
            except Exception:
                ref_dt = datetime.now(timezone.utc)
        elif not isinstance(ref_dt, datetime):
            ref_dt = datetime.now(timezone.utc)

        try:
            ep_source = EpisodeType.text if (EpisodeType and hasattr(EpisodeType, "text")) else "text"
            if source_type == "message" and EpisodeType and hasattr(EpisodeType, "message"):
                ep_source = EpisodeType.message
            elif source_type == "json" and EpisodeType and hasattr(EpisodeType, "json"):
                ep_source = EpisodeType.json

            res = await self._graphiti.add_episode(
                name=name,
                episode_body=episode_body,
                source_description=source_description,
                reference_time=ref_dt,
                source=ep_source,
                group_id=group_id,
                uuid=uuid,
            )
            self.last_error = None
            logger.debug("Successfully added episode to Graphiti: %s (uuid: %s)", name, uuid)
            return res
        except Exception as exc:
            self.last_error = exc
            # Derived semantic layer invariant: Never throw or disrupt domain operations
            logger.warning(
                "Graphiti episode ingestion failed for '%s' (uuid: %s): %s. Domain flow intact.",
                name,
                uuid,
                exc,
            )
            return None

    async def search(
        self,
        query: str,
        limit: int = 5,
        group_ids: Optional[List[str]] = None,
    ) -> List[Any]:
        """Safely perform semantic search in Graphiti."""
        if not self.is_available:
            return []

        try:
            return await self._graphiti.search(
                query=query,
                num_results=limit,
                group_ids=group_ids,
            )
        except Exception as exc:
            logger.warning("Graphiti search failed for query '%s': %s", query, exc)
            return []

    async def close(self) -> None:
        """Safely close underlying Graphiti resources."""
        if self._graphiti is not None and hasattr(self._graphiti, "close"):
            try:
                await self._graphiti.close()
            except Exception as e:
                logger.debug("Error while closing Graphiti: %s", e)
        self._graphiti = None
        self._is_initialized = False


# Re-export GraphMemorySyncWorker & GraphSyncStatus
from ptb_graph_memory.sync_worker import GraphMemorySyncWorker, GraphSyncStatus

__all__ = [
    "GraphitiAdapter",
    "GraphMemorySyncWorker",
    "GraphSyncStatus",
]
