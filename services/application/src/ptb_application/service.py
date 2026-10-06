"""ApplicationService: Central business use-cases for Personal Task Board (Layer 5)."""

from datetime import datetime, timezone
import inspect
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from uuid import uuid4

from ptb_contracts.l1_acquisition import IngestionCheckpointRecord, SourceSyncState
from ptb_contracts.l2_processing import (
    CommitmentRecord,
    EvidenceRecord,
    EvidenceType,
    ReviewQueueItem,
    StatusTransitionAuditRecord,
    TaskStatus,
    UnifiedTaskCandidate,
)
from ptb_contracts.l4_intelligence import (
    DecisionRecord,
    ForgottenCommitmentItem,
    LessonRecord,
    PriorityBreakdown,
    TaskWithContext,
    TodayBoardView,
    TodayTaskItem,
    WaitingOnItem,
)
from ptb_contracts.l5_experience import (
    CoverageStatusResponse,
    CoverageTenantStatus,
    KnowledgeSearchResponse,
    TaskActionResponse,
)
from ptb_contracts.logging import BugCode, log_bug
from ptb_database.neo4j_client import Neo4jClient
from ptb_database.repositories import (
    CheckpointRepository,
    RawEventRepository,
    TaskDomainRepository,
)
from ptb_graph_memory.client import GraphitiMemoryClient
from ptb_intelligence.detectors import ForgottenCommitmentDetector, WaitingOnDetector
from ptb_intelligence.lifecycle import TaskIntelligenceLifecycle
from ptb_intelligence.planner import TodayBoardPlanner
from ptb_intelligence.priority import DeterministicPriorityEngine
from ptb_intelligence.status_machine import StatusInferenceMachine

logger = logging.getLogger("ptb.application.service")


class ApplicationService:
    """Điểm tập trung toàn bộ use-case nghiệp vụ của Personal Task Board.

    Kết hợp:
    - TaskDomainRepository (lưu trữ và truy vấn UnifiedTask, Evidences)
    - RawEventRepository (lưu trữ và tra cứu RawEvent, ProcessingAttempt)
    - CheckpointRepository (theo dõi con trỏ đồng bộ nguồn)
    - DeterministicPriorityEngine (tính điểm ưu tiên 0-100)
    - TodayBoardPlanner (lập kế hoạch Today Board)
    - GraphitiMemoryClient (bộ nhớ ngữ cảnh tri thức decisions & lessons)
    - StatusInferenceMachine (quản lý chuyển đổi trạng thái & audit)
    """

    def __init__(
        self,
        task_repo: Optional[TaskDomainRepository] = None,
        raw_event_repo: Optional[RawEventRepository] = None,
        checkpoint_repo: Optional[CheckpointRepository] = None,
        priority_engine: Optional[DeterministicPriorityEngine] = None,
        planner: Optional[TodayBoardPlanner] = None,
        graph_memory: Optional[GraphitiMemoryClient] = None,
        status_machine: Optional[StatusInferenceMachine] = None,
        lifecycle: Optional[TaskIntelligenceLifecycle] = None,
        neo4j_client: Optional[Neo4jClient] = None,
        processing_pipeline: Optional[Any] = None,
        processing_worker_status: Optional[str] = None,
        llm_status: Optional[str] = None,
        playwright_status: Optional[str] = None,
        sources_config: Optional[Dict[str, Any]] = None,
        runtime_statuses: Optional[Dict[str, str]] = None,
    ) -> None:
        self._explicit_neo4j_client = neo4j_client is not None
        repo_dict = getattr(task_repo, "__dict__", {}) if task_repo is not None else {}
        repo_client = repo_dict.get("neo4j_client") or repo_dict.get("_client")
        self._has_real_task_repo = (
            task_repo is None
            or isinstance(task_repo, TaskDomainRepository)
            or repo_client is not None
        )
        client = neo4j_client or repo_client or Neo4jClient()
        self._default_neo4j_client = None if self._explicit_neo4j_client else client
        self.neo4j_client = client
        emb_svc = None
        try:
            from packages.ai_service import Qwen3EmbeddingService
            emb_svc = Qwen3EmbeddingService()
        except Exception as e_emb:
            logger.debug("Qwen3 embedding service not loaded in ApplicationService: %s", e_emb)
        self.task_repo = task_repo or TaskDomainRepository(client, embedding_service=emb_svc)
        self.raw_event_repo = raw_event_repo or RawEventRepository(client)
        self.checkpoint_repo = checkpoint_repo or CheckpointRepository(client)
        self.priority_engine = priority_engine or DeterministicPriorityEngine()
        self.planner = planner or TodayBoardPlanner(priority_engine=self.priority_engine)
        self.graph_memory = graph_memory or GraphitiMemoryClient(neo4j_client=client)
        self.status_machine = status_machine or StatusInferenceMachine()
        self.lifecycle = lifecycle or TaskIntelligenceLifecycle(
            task_repo=self.task_repo,
            status_machine=self.status_machine,
            priority_engine=self.priority_engine,
        )
        self.processing_pipeline = processing_pipeline
        self.processing_worker_status = processing_worker_status
        self.llm_status = llm_status
        self.playwright_status = playwright_status
        self.sources_config = sources_config
        self.runtime_statuses = runtime_statuses or {}

    async def get_system_health(self) -> Dict[str, Any]:
        """Kiểm tra tình trạng sức khỏe sâu (deep health) của ApplicationService và các dependencies."""
        # 1. Kiểm tra kết nối neo4j
        repo_dict = getattr(self.task_repo, "__dict__", {}) if self.task_repo is not None else {}
        has_real_repo = (
            self._has_real_task_repo
            or isinstance(self.task_repo, TaskDomainRepository)
            or repo_dict.get("_client") is not None
            or repo_dict.get("neo4j_client") is not None
        )
        client_overridden = (
            self._explicit_neo4j_client
            or (self._default_neo4j_client is not None and self.neo4j_client is not self._default_neo4j_client)
            or ("verify_connectivity" in getattr(self.neo4j_client, "__dict__", {}))
        )

        neo4j_state = "healthy"
        if self.neo4j_client is not None and (has_real_repo or client_overridden):
            try:
                conn_res = self.neo4j_client.verify_connectivity()
                if inspect.isawaitable(conn_res):
                    conn_res = await conn_res
                if not conn_res:
                    log_bug(
                        BugCode.PTB_APP_001,
                        subsystem="application",
                        severity="ERROR",
                        message="Application dependency Neo4j is unhealthy",
                    )
                    neo4j_state = "not_ready"
            except Exception as exc:
                log_bug(
                    BugCode.PTB_APP_001,
                    subsystem="application",
                    severity="ERROR",
                    message="Application dependency Neo4j is unhealthy",
                    exc=exc,
                )
                neo4j_state = "not_ready"

        # 2. Kiểm tra graphiti
        graphiti_state = "healthy"
        if self.graph_memory is None:
            log_bug(
                BugCode.PTB_GRAPH_001,
                subsystem="graph_memory",
                severity="WARNING",
                message="Graphiti memory client is unavailable",
            )
            graphiti_state = "degraded"
        else:
            try:
                is_healthy_val = getattr(self.graph_memory, "is_healthy", None)
                if callable(is_healthy_val):
                    is_healthy_val = is_healthy_val()
                    if inspect.isawaitable(is_healthy_val):
                        is_healthy_val = await is_healthy_val

                check_health_fn = getattr(self.graph_memory, "check_health", None)
                check_health_val = True
                if callable(check_health_fn):
                    check_res = check_health_fn()
                    if inspect.isawaitable(check_res):
                        check_res = await check_res
                    if check_res is False:
                        check_health_val = False

                adapter = getattr(self.graph_memory, "adapter", None)
                if adapter is not None:
                    if hasattr(adapter, "is_available") and not adapter.is_available:
                        check_health_val = False
                    if getattr(adapter, "last_error", None) is not None:
                        check_health_val = False
                    adapter_embedder = getattr(adapter, "embedder", None)
                    if (
                        adapter_embedder is not None
                        and "Qwen3" in type(adapter_embedder).__name__
                    ):
                        try:
                            import socket
                            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                            s.settimeout(0.2)
                            s.connect(("127.0.0.1", 8082))
                            s.close()
                        except Exception:
                            check_health_val = False

                if is_healthy_val is False or check_health_val is False:
                    log_bug(
                        BugCode.PTB_GRAPH_001,
                        subsystem="graph_memory",
                        severity="WARNING",
                        message="Graphiti memory subsystem is unhealthy",
                    )
                    graphiti_state = "degraded"
            except Exception as exc:
                log_bug(
                    BugCode.PTB_GRAPH_001,
                    subsystem="graph_memory",
                    severity="WARNING",
                    message=f"Graphiti health check failed: {exc}",
                    exc=exc,
                )
                graphiti_state = "degraded"

        # 3. Kiểm tra processing_worker
        processing_worker_state = "not_ready"
        if getattr(self, "processing_worker_status", None) is not None:
            processing_worker_state = str(self.processing_worker_status).strip().lower()
        elif self.processing_pipeline is not None:
            pipe_health_status = getattr(self.processing_pipeline, "health_status", None)
            pipe_status = getattr(self.processing_pipeline, "status", None)
            pipe_healthy = getattr(self.processing_pipeline, "is_healthy", None)
            
            if isinstance(pipe_health_status, str) and pipe_health_status.strip():
                processing_worker_state = pipe_health_status.strip().lower()
            elif isinstance(pipe_status, str) and pipe_status.strip():
                processing_worker_state = pipe_status.strip().lower()
            elif pipe_healthy is False:
                processing_worker_state = "degraded"
            elif pipe_healthy is True:
                processing_worker_state = "healthy"

        # 4. Kiểm tra llm
        llm_state = "not_ready"
        if getattr(self, "llm_status", None) is not None:
            llm_state = str(self.llm_status).strip().lower()
        elif self.processing_pipeline is not None:
            extractor = getattr(self.processing_pipeline, "llm_extractor", None)
            if extractor is not None:
                ext_healthy = getattr(extractor, "is_healthy", None)
                has_key = bool(getattr(extractor, "api_key", None)) if hasattr(extractor, "api_key") else True
                is_fake = bool(getattr(extractor, "is_fake", False))
                
                if ext_healthy is False:
                    llm_state = "degraded"
                elif hasattr(extractor, "api_key") and not (has_key or is_fake) and (has_real_repo or client_overridden):
                    llm_state = "degraded"
                else:
                    llm_state = "healthy"

        # 5. Kiểm tra playwright
        if getattr(self, "playwright_status", None) is not None:
            playwright_state = str(self.playwright_status).strip().lower()
        else:
            try:
                from ptb_acquisition.playwright.session import SessionManager

                playwright_state = SessionManager().validate_session().value.lower()
            except Exception:
                playwright_state = "unconfigured"

        # 6. Kiểm tra Qwen3 Embedding (Port 8082 / local-ai)
        if getattr(self, "qwen3_status", None) is not None:
            qwen3_state = str(self.qwen3_status).strip().lower()
        else:
            try:
                import socket
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.3)
                s.connect(("127.0.0.1", 8082))
                s.close()
                qwen3_state = "healthy"
            except Exception:
                qwen3_state = "unconfigured"

        # 7. Kiểm tra Kev Decision Reranker (Port 8081 / local-ai)
        if getattr(self, "kev_status", None) is not None:
            kev_state = str(self.kev_status).strip().lower()
        else:
            try:
                import socket
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.3)
                s.connect(("127.0.0.1", 8081))
                s.close()
                kev_state = "healthy"
            except Exception:
                kev_state = "unconfigured"

        # 8. Tổng hợp status tổng thể
        require_local_ai = os.getenv("PTB_REQUIRE_LOCAL_AI", "false").lower() in ("true", "1")
        if neo4j_state == "not_ready" or processing_worker_state == "not_ready":
            overall_status = "not_ready"
        elif any(
            st == "degraded"
            for st in (graphiti_state, processing_worker_state, llm_state)
        ) or (require_local_ai and (qwen3_state != "healthy" or kev_state != "healthy")):
            overall_status = "degraded"
        else:
            overall_status = "healthy"

        return {
            "status": overall_status,
            "service": "ptb-application",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "neo4j": neo4j_state,
            "processing_worker": processing_worker_state,
            "graphiti": graphiti_state,
            "llm": llm_state,
            "playwright": playwright_state,
            "qwen3": qwen3_state,
            "kev": kev_state,
        }

    async def get_today_plan(self, user_id: str = "default") -> TodayBoardView:
        """Lấy today tasks, waiting on others, forgotten commitments, headline."""
        # 1. Lấy active tasks (TODO, IN_PROGRESS, BLOCKED)
        active_tasks = await self.list_tasks(filters={
            "status": [TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED]
        })

        # 2. Xây dựng TaskWithContext cho từng task
        tasks_with_context: list[TaskWithContext] = []
        now = datetime.now(timezone.utc)
        for t in active_tasks:
            last_change = t.updated_at or t.created_at or now
            if last_change.tzinfo is None:
                last_change = last_change.replace(tzinfo=timezone.utc)
            days_in_status = max(0, int((now - last_change).total_seconds() // 86400))
            has_comp = any(
                ev.evidence_type == EvidenceType.COMPLETION_SIGNAL
                or "done" in (ev.snippet or "").lower()
                for ev in t.evidences
            )
            ctx = TaskWithContext(
                task=t,
                last_status_change_at=last_change,
                days_in_current_status=days_in_status,
                has_completion_evidence=has_comp,
                blocking_tasks=[],
                dependent_people=[],
            )
            tasks_with_context.append(ctx)

        # 3. Lấy commitments
        commitments = await self.task_repo.get_active_commitments(user_id=user_id)

        # 4. Sử dụng TodayBoardPlanner để lập kế hoạch tổng hợp
        board_view = self.planner.plan_today(
            user_id=user_id,
            tasks=tasks_with_context,
            commitments=commitments,
        )
        return board_view

    async def list_tasks(self, filters: Optional[dict] = None) -> list[UnifiedTaskCandidate]:
        """Hỗ trợ lọc theo source, project, customer, status, due_range, priority, owner, stale, waiting, review_status."""
        return await self.task_repo.list_tasks(filters=filters)

    async def get_task_detail(self, task_id: str) -> Optional[TaskWithContext]:
        """Đầy đủ task, blockers, dependents, evidences, related decisions/lessons."""
        task_ctx = await self.task_repo.get_task_with_context(task_id)
        if not task_ctx:
            task = await self.task_repo.get_task_by_id(task_id)
            if not task:
                return None
            now = datetime.now(timezone.utc)
            last_change = task.updated_at or task.created_at or now
            if last_change.tzinfo is None:
                last_change = last_change.replace(tzinfo=timezone.utc)
            days_in_status = max(0, int((now - last_change).total_seconds() // 86400))
            task_ctx = TaskWithContext(
                task=task,
                last_status_change_at=last_change,
                days_in_current_status=days_in_status,
                has_completion_evidence=False,
                blocking_tasks=[],
                dependent_people=[],
            )

        # Trích xuất related decisions và lessons thông qua Graphiti Episodic Memory
        try:
            query = f"{task_ctx.task.title} {task_ctx.task.project_key or ''}".strip()
            if query:
                episodes = await self.graph_memory.search_context(query=query, limit=5)
                for ep in episodes:
                    ep_type = str(ep.get("type", "")).upper()
                    summary = ep.get("summary") or ep.get("topic") or ""
                    if "DECISION" in ep_type and summary:
                        if summary not in task_ctx.related_decisions:
                            task_ctx.related_decisions.append(summary)
                    elif "LESSON" in ep_type and summary:
                        if summary not in task_ctx.past_lessons_learned:
                            task_ctx.past_lessons_learned.append(summary)
        except Exception as ex:
            logger.warning("Error querying related knowledge for task %s: %s", task_id, ex)

        return task_ctx

    async def get_review_inbox(self, limit: int = 20) -> list[ReviewQueueItem]:
        """Danh sách task pending review (confidence 0.40 - 0.64 hoặc pending_review)."""
        items = await self.task_repo.get_review_queue(limit=limit)
        filtered: list[ReviewQueueItem] = []
        for item in items:
            cand = item.candidate_task
            if cand.status == TaskStatus.DISMISSED:
                continue
            if cand.review_status in ("rejected", "dismissed"):
                continue
            if cand.status_authoritative and cand.review_status == "auto_approved":
                continue
            filtered.append(item)
        return filtered[:limit]

    async def search_knowledge(
        self,
        query: str,
        project_key: Optional[str] = None,
        limit: int = 5,
    ) -> KnowledgeSearchResponse:
        """Tìm kiếm decisions và lessons qua GraphitiMemoryClient."""
        search_query = query
        if project_key:
            search_query = f"{query} {project_key}".strip()

        episodes = await self.graph_memory.search_context(query=search_query, limit=limit * 2)

        decisions: list[DecisionRecord] = []
        lessons: list[LessonRecord] = []
        now = datetime.now(timezone.utc)

        for ep in episodes:
            ep_type = str(ep.get("type", "")).upper()
            ep_id = ep.get("id", str(uuid4()))
            props = ep.get("properties", {})

            if "DECISION" in ep_type:
                dec_at = props.get("decided_at") or ep.get("valid_at") or now
                if isinstance(dec_at, str):
                    try:
                        dec_at = datetime.fromisoformat(dec_at)
                    except Exception:
                        dec_at = now
                decisions.append(DecisionRecord(
                    decision_id=ep_id,
                    project_key=project_key or props.get("project_key"),
                    summary=ep.get("summary") or props.get("summary", ""),
                    rationale=ep.get("content") or props.get("rationale", ""),
                    decided_by=props.get("decided_by", "SYSTEM"),
                    decided_at=dec_at,
                ))
            elif "LESSON" in ep_type:
                rec_at = props.get("recorded_at") or ep.get("valid_at") or now
                if isinstance(rec_at, str):
                    try:
                        rec_at = datetime.fromisoformat(rec_at)
                    except Exception:
                        rec_at = now
                lessons.append(LessonRecord(
                    lesson_id=ep_id,
                    topic=ep.get("topic") or ep.get("summary") or props.get("topic", ""),
                    description=ep.get("content") or props.get("description", ""),
                    solution=props.get("solution") or ep.get("content", ""),
                    related_incident_id=props.get("related_incident_id"),
                    recorded_at=rec_at,
                ))

        decisions = decisions[:limit]
        lessons = lessons[:limit]

        synthesis_parts = []
        if decisions:
            synthesis_parts.append(f"{len(decisions)} quyết định kiến trúc")
        if lessons:
            synthesis_parts.append(f"{len(lessons)} bài học kinh nghiệm")

        if synthesis_parts:
            summary = f"Tìm thấy {' và '.join(synthesis_parts)} phù hợp với '{query}'."
        else:
            summary = f"Không tìm thấy tri thức phù hợp với '{query}'."

        return KnowledgeSearchResponse(
            decisions=decisions,
            lessons=lessons,
            synthesis_summary=summary,
        )

    CANONICAL_SOURCES = [
        {
            "key": "ms_teams",
            "source_type": "ms_teams",
            "default_name": "Microsoft Teams",
            "default_tenant_id": "tenant-ms_teams",
        },
        {
            "key": "ms_outlook",
            "source_type": "ms_outlook",
            "default_name": "Microsoft Outlook",
            "default_tenant_id": "tenant-ms_outlook",
        },
        {
            "key": "coding_agent",
            "source_type": "coding_agent",
            "default_name": "Coding Agents",
            "default_tenant_id": "tenant-coding_agent",
        },
        {
            "key": "git",
            "source_type": "git",
            "default_name": "Local Git Repositories",
            "default_tenant_id": "tenant-git",
        },
        {
            "key": "jira",
            "source_type": "jira",
            "default_name": "Jira Cloud",
            "default_tenant_id": "tenant-jira",
        },
        {
            "key": "shortcut",
            "source_type": "shortcut",
            "default_name": "Shortcut Stories",
            "default_tenant_id": "tenant-shortcut",
        },
    ]

    @staticmethod
    def _normalize_source_key(source_type_val: Any) -> str:
        s = str(source_type_val.value if hasattr(source_type_val, "value") else source_type_val).lower().strip()
        if s in ("ms_teams", "ms_teams_web", "teams"):
            return "ms_teams"
        if s in ("ms_outlook", "ms_outlook_web", "outlook"):
            return "ms_outlook"
        if s in (
            "coding_agent", "coding_agents", "cursor", "claude_code",
            "antigravity", "codex", "github_copilot", "copilot",
            "windsurf", "continue", "aider", "cline", "roo_code"
        ):
            return "coding_agent"
        if s in ("git", "git_repo", "local_git"):
            return "git"
        if s in ("jira", "jira_cloud", "jira_server"):
            return "jira"
        if s in ("shortcut", "shortcut_stories", "clubhouse"):
            return "shortcut"
        return s

    @staticmethod
    def _get_source_config(sources_cfg: Dict[str, Any], key: str) -> Dict[str, Any]:
        if not isinstance(sources_cfg, dict):
            return {}
        sources_dict = sources_cfg.get("sources", sources_cfg) if isinstance(sources_cfg.get("sources"), dict) else sources_cfg
        if not isinstance(sources_dict, dict):
            return {}
        if key in sources_dict and isinstance(sources_dict[key], dict):
            return sources_dict[key]
        alias_map = {
            "coding_agent": ["coding_agents"],
            "ms_teams": ["teams", "ms_teams_web"],
            "ms_outlook": ["outlook", "ms_outlook_web"],
        }
        for alias in alias_map.get(key, []):
            if alias in sources_dict and isinstance(sources_dict[alias], dict):
                return sources_dict[alias]
        return {}

    @staticmethod
    def _load_default_sources_config() -> Dict[str, Any]:
        env_path = os.environ.get("PTB_SOURCES_CONFIG")
        candidates = []
        if env_path:
            candidates.append(Path(env_path))
        candidates.extend([
            Path("config/sources.yaml"),
            Path(__file__).resolve().parents[4] / "config" / "sources.yaml",
            Path(__file__).resolve().parents[3] / "config" / "sources.yaml",
        ])
        for p in candidates:
            if p and p.exists():
                try:
                    import yaml
                    with open(p, "r", encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}
                        if isinstance(data, dict):
                            return data
                except Exception as ex:
                    logger.debug("Could not read sources config from %s: %s", p, ex)
        return {}

    async def get_sources_health(
        self,
        runtime_statuses: Optional[Dict[str, str]] = None,
        sources_config: Optional[Dict[str, Any]] = None,
    ) -> CoverageStatusResponse:
        """Tình trạng các tenant và checkpoints cho toàn bộ source registry."""
        try:
            checkpoints = await self.checkpoint_repo.list_checkpoints()
        except Exception as ex:
            logger.warning("Could not query checkpoints: %s", ex)
            checkpoints = []
        now = datetime.now(timezone.utc)

        # Merge runtime statuses and sources config
        effective_runtime = dict(self.runtime_statuses)
        if runtime_statuses:
            effective_runtime.update(runtime_statuses)

        if sources_config is not None:
            effective_sources_cfg = sources_config
        elif self.sources_config is not None:
            effective_sources_cfg = self.sources_config
        else:
            effective_sources_cfg = self._load_default_sources_config()

        # Playwright status resolution
        playwright_state = "unconfigured"
        if getattr(self, "playwright_status", None) is not None:
            playwright_state = str(self.playwright_status).strip().lower()
        else:
            try:
                from ptb_acquisition.playwright.session import SessionManager
                playwright_state = SessionManager().validate_session().value.lower()
            except Exception as e:
                logger.debug("Could not validate playwright session: %s", e)

        # Index checkpoints by normalized source key
        checkpoints_by_key: Dict[str, List[IngestionCheckpointRecord]] = {}
        for cp in checkpoints:
            raw_st = cp.source_type.value if hasattr(cp.source_type, "value") else str(cp.source_type)
            norm_key = self._normalize_source_key(raw_st)
            checkpoints_by_key.setdefault(norm_key, []).append(cp)

        tenants_status: List[CoverageTenantStatus] = []

        # Canonical sources first, then any extra sources discovered in checkpoints
        canonical_keys = {c["key"] for c in self.CANONICAL_SOURCES}
        sources_to_evaluate = list(self.CANONICAL_SOURCES)
        for extra_key, extra_cps in checkpoints_by_key.items():
            if extra_key not in canonical_keys:
                sources_to_evaluate.append({
                    "key": extra_key,
                    "source_type": extra_key,
                    "default_name": f"Source {extra_key}",
                    "default_tenant_id": f"tenant-{extra_key}",
                })

        severity_rank = {
            SourceSyncState.ERROR.value.lower(): 10,
            SourceSyncState.AUTH_REQUIRED.value.lower(): 9,
            SourceSyncState.UNCONFIGURED.value.lower(): 8,
            SourceSyncState.DEGRADED.value.lower(): 7,
            SourceSyncState.NOT_INSTALLED.value.lower(): 6,
            SourceSyncState.NEVER_SYNCED.value.lower(): 5,
            SourceSyncState.HEALTHY.value.lower(): 1,
        }

        for src_meta in sources_to_evaluate:
            src_key = src_meta["key"]
            src_type = src_meta["source_type"]
            src_cfg = self._get_source_config(effective_sources_cfg, src_key)
            matching_cps = checkpoints_by_key.get(src_key, [])

            # Check runtime status override (by key or by tenant_id)
            r_st = (
                effective_runtime.get(src_key)
                or effective_runtime.get(src_type)
                or effective_runtime.get(src_meta["default_tenant_id"])
                or (effective_runtime.get(matching_cps[0].tenant_id) if matching_cps else None)
                or ""
            ).strip().lower()

            # Identify tenant_id and tenant_name
            if matching_cps:
                tenant_id = matching_cps[0].tenant_id
                st_val = matching_cps[0].source_type.value if hasattr(matching_cps[0].source_type, "value") else str(matching_cps[0].source_type)
                tenant_name = f"{tenant_id} ({st_val})"
            else:
                tenant_id = src_meta["default_tenant_id"]
                tenant_name = src_meta["default_name"]

            # Evaluate checkpoints if present
            valid_sync_times: List[datetime] = []
            stream_statuses: List[str] = []
            items_total = 0
            for cp in matching_cps:
                last_sync = cp.last_event_timestamp or cp.updated_at
                if last_sync:
                    last_sync_utc = last_sync if last_sync.tzinfo else last_sync.replace(tzinfo=timezone.utc)
                    valid_sync_times.append(last_sync_utc)
                    age_seconds = (now - last_sync_utc).total_seconds()
                    if age_seconds > (7 * 86400):
                        stream_statuses.append(SourceSyncState.DEGRADED.value.lower())
                    else:
                        stream_statuses.append(SourceSyncState.HEALTHY.value.lower())
                else:
                    stream_statuses.append(SourceSyncState.NEVER_SYNCED.value.lower())
                if cp.last_external_id:
                    items_total += 1

            last_successful_sync = max(valid_sync_times) if valid_sync_times else None

            # DETERMINISTIC PRECEDENCE:
            # 1. DISABLED: If enabled=false in config or disabled in runtime
            is_enabled = src_cfg.get("enabled", True)
            if is_enabled is False or r_st == "disabled":
                status = SourceSyncState.DISABLED.value.lower()
                error_msg = None

            # 2. RUNTIME ERROR: Runtime error wins over healthy checkpoints
            elif r_st in ("error", "critical", "failed"):
                status = SourceSyncState.ERROR.value.lower()
                error_msg = f"Runtime adapter error for {src_key}"

            # 3. AUTH_REQUIRED: Explicit runtime auth required or Microsoft source without valid session
            elif r_st == "auth_required" or (src_key in ("ms_teams", "ms_outlook") and playwright_state == "auth_required"):
                status = SourceSyncState.AUTH_REQUIRED.value.lower()
                error_msg = "Authentication required or session expired"

            # 4. UNCONFIGURED / NOT_INSTALLED
            elif r_st in ("unconfigured", "not_installed"):
                status = r_st
                error_msg = f"Source {src_key} is {r_st}"
            elif src_key in ("jira", "shortcut"):
                token = src_cfg.get("api_token") or os.environ.get(f"{src_key.upper()}_API_TOKEN")
                if not matching_cps and not token:
                    status = SourceSyncState.UNCONFIGURED.value.lower()
                    error_msg = f"Credentials missing for {src_key}"
                elif matching_cps:
                    worst_status = max(stream_statuses, key=lambda s: severity_rank.get(s, 0)) if stream_statuses else SourceSyncState.NEVER_SYNCED.value.lower()
                    status = worst_status
                    error_msg = "Checkpoint stale (> 7 days)" if status == SourceSyncState.DEGRADED.value.lower() else None
                else:
                    status = SourceSyncState.NEVER_SYNCED.value.lower()
                    error_msg = None
            elif src_key in ("ms_teams", "ms_outlook") and not matching_cps and playwright_state in ("unconfigured", "not_installed"):
                status = SourceSyncState.UNCONFIGURED.value.lower() if playwright_state == "unconfigured" else SourceSyncState.NOT_INSTALLED.value.lower()
                error_msg = f"Playwright session is {playwright_state}"

            # 5. CHECKPOINTS EXIST: Multi-stream aggregation (worst stream wins)
            elif matching_cps:
                worst_status = max(stream_statuses, key=lambda s: severity_rank.get(s, 0)) if stream_statuses else SourceSyncState.NEVER_SYNCED.value.lower()
                if r_st in ("degraded", "warning"):
                    status = SourceSyncState.DEGRADED.value.lower()
                    error_msg = f"Runtime adapter degraded for {src_key}"
                else:
                    status = worst_status
                    error_msg = "Checkpoint stale (> 7 days)" if status == SourceSyncState.DEGRADED.value.lower() else None

            # 6. NO CHECKPOINTS: Source has never synced
            else:
                if r_st in ("degraded", "warning"):
                    status = SourceSyncState.DEGRADED.value.lower()
                    error_msg = f"Runtime adapter degraded for {src_key}"
                else:
                    status = SourceSyncState.NEVER_SYNCED.value.lower()
                    error_msg = None

            tenants_status.append(CoverageTenantStatus(
                tenant_id=tenant_id,
                tenant_name=tenant_name,
                source_type=src_type,
                status=status,
                last_successful_sync=last_successful_sync,
                items_synced_total=items_total,
                error_message=error_msg,
            ))

        # Overall health calculation over entire source registry
        enabled_tenants = [t for t in tenants_status if t.status != SourceSyncState.DISABLED.value.lower()]
        if not enabled_tenants:
            overall = "not_ready"
        elif any(t.status == SourceSyncState.ERROR.value.lower() for t in enabled_tenants):
            overall = "critical"
        elif all(t.status == SourceSyncState.NEVER_SYNCED.value.lower() for t in enabled_tenants):
            overall = "not_ready"
        elif any(t.status in (
            SourceSyncState.DEGRADED.value.lower(),
            SourceSyncState.NEVER_SYNCED.value.lower(),
            SourceSyncState.AUTH_REQUIRED.value.lower(),
            SourceSyncState.UNCONFIGURED.value.lower(),
            SourceSyncState.NOT_INSTALLED.value.lower(),
        ) for t in enabled_tenants):
            overall = "degraded"
        else:
            overall = "healthy"

        return CoverageStatusResponse(
            tenants=tenants_status,
            overall_health=overall,
            last_checked_at=now,
        )

    async def execute_task_action(
        self,
        task_id: str,
        action: str,
        new_status: Optional[str] = None,
        actor: str = "USER",
    ) -> TaskActionResponse:
        """Cập nhật trạng thái task hoặc phê duyệt review task (chỉ gọi nội bộ từ application service hoặc OpenWebUI action)."""
        canonical_statuses = {
            TaskStatus.TODO.value,
            TaskStatus.IN_PROGRESS.value,
            TaskStatus.BLOCKED.value,
            TaskStatus.DONE.value,
            TaskStatus.DISMISSED.value,
        }

        task = await self.task_repo.get_task_by_id(task_id)
        if not task and task_id.startswith("rev-"):
            candidate_id = task_id[4:]
            task = await self.task_repo.get_task_by_id(candidate_id)
            if task:
                task_id = task.id

        if not task:
            return TaskActionResponse(
                success=False,
                task_id=task_id,
                message=f"Task {task_id} not found",
            )

        action_norm = (action or "").strip().upper()
        now = datetime.now(timezone.utc)
        old_status = task.status

        if action_norm == "UPDATE_STATUS":
            if not new_status:
                return TaskActionResponse(
                    success=False,
                    task_id=task_id,
                    message="Missing 'new_status' for UPDATE_STATUS action",
                )
            status_norm = str(new_status).strip().upper()
            if status_norm not in canonical_statuses:
                return TaskActionResponse(
                    success=False,
                    task_id=task_id,
                    message=f"Invalid status value: {new_status}",
                )
            target_status = TaskStatus(status_norm)

            audit = StatusTransitionAuditRecord(
                id=str(uuid4()),
                task_id=task_id,
                old_status=old_status,
                new_status=target_status,
                reason=f"Status updated to {target_status.value} by {actor}",
                confidence=1.0,
                changed_at=now,
                change_actor=actor,
            )
            task.status = target_status
            if actor != "SYSTEM":
                task.status_authoritative = True
            task.updated_at = now
            await self.task_repo.upsert_task_atomic(task)
            await self.task_repo.record_status_transition_audit(audit)

            # Trigger TaskIntelligenceLifecycle hook
            if hasattr(self, "lifecycle") and self.lifecycle:
                await self.lifecycle.on_task_changed(task, now=now)

            return TaskActionResponse(
                success=True,
                task_id=task_id,
                message=f"Successfully updated task status to {target_status.value}",
                updated_at=now,
            )

        elif action_norm == "APPROVE":
            if new_status is not None:
                status_norm = str(new_status).strip().upper()
                if status_norm not in canonical_statuses:
                    return TaskActionResponse(
                        success=False,
                        task_id=task_id,
                        message=f"Invalid status value: {new_status}",
                    )
                target_status = TaskStatus(status_norm)
            else:
                target_status = TaskStatus.TODO

            task.review_status = "auto_approved"
            task.status = target_status
            if actor != "SYSTEM":
                task.status_authoritative = True
            task.updated_at = now
            audit = StatusTransitionAuditRecord(
                id=str(uuid4()),
                task_id=task_id,
                old_status=old_status,
                new_status=task.status,
                reason=f"Task review approved by {actor}",
                confidence=1.0,
                changed_at=now,
                change_actor=actor,
            )
            await self.task_repo.upsert_task_atomic(task)
            await self.task_repo.record_status_transition_audit(audit)

            # Trigger TaskIntelligenceLifecycle hook
            if hasattr(self, "lifecycle") and self.lifecycle:
                await self.lifecycle.on_task_changed(task, now=now)

            return TaskActionResponse(
                success=True,
                task_id=task_id,
                message="Task candidate approved successfully",
                updated_at=now,
            )

        elif action_norm == "REJECT":
            task.review_status = "rejected"
            task.status = TaskStatus.DISMISSED
            if actor != "SYSTEM":
                task.status_authoritative = True
            task.updated_at = now
            audit = StatusTransitionAuditRecord(
                id=str(uuid4()),
                task_id=task_id,
                old_status=old_status,
                new_status=TaskStatus.DISMISSED,
                reason=f"Task rejected by {actor}",
                confidence=1.0,
                changed_at=now,
                change_actor=actor,
            )
            await self.task_repo.upsert_task_atomic(task)
            await self.task_repo.record_status_transition_audit(audit)

            # Trigger TaskIntelligenceLifecycle hook
            if hasattr(self, "lifecycle") and self.lifecycle:
                await self.lifecycle.on_task_changed(task, now=now)

            return TaskActionResponse(
                success=True,
                task_id=task_id,
                message="Task candidate rejected and dismissed",
                updated_at=now,
            )

        elif action_norm == "DISMISS":
            task.review_status = "rejected"
            task.status = TaskStatus.DISMISSED
            if actor != "SYSTEM":
                task.status_authoritative = True
            task.updated_at = now
            audit = StatusTransitionAuditRecord(
                id=str(uuid4()),
                task_id=task_id,
                old_status=old_status,
                new_status=TaskStatus.DISMISSED,
                reason=f"Task dismissed by {actor}",
                confidence=1.0,
                changed_at=now,
                change_actor=actor,
            )
            await self.task_repo.upsert_task_atomic(task)
            await self.task_repo.record_status_transition_audit(audit)

            # Trigger TaskIntelligenceLifecycle hook
            if hasattr(self, "lifecycle") and self.lifecycle:
                await self.lifecycle.on_task_changed(task, now=now)

            return TaskActionResponse(
                success=True,
                task_id=task_id,
                message="Task dismissed successfully",
                updated_at=now,
            )

        elif action_norm == "MARK_DONE":
            task.status = TaskStatus.DONE
            if actor != "SYSTEM":
                task.status_authoritative = True
            task.updated_at = now
            audit = StatusTransitionAuditRecord(
                id=str(uuid4()),
                task_id=task_id,
                old_status=old_status,
                new_status=TaskStatus.DONE,
                reason=f"Task marked as DONE by {actor}",
                confidence=1.0,
                changed_at=now,
                change_actor=actor,
            )
            await self.task_repo.upsert_task_atomic(task)
            await self.task_repo.record_status_transition_audit(audit)

            # Trigger TaskIntelligenceLifecycle hook
            if hasattr(self, "lifecycle") and self.lifecycle:
                await self.lifecycle.on_task_changed(task, now=now)

            return TaskActionResponse(
                success=True,
                task_id=task_id,
                message="Task marked as DONE successfully",
                updated_at=now,
            )

        else:
            return TaskActionResponse(
                success=False,
                task_id=task_id,
                message=f"Unsupported action: {action}",
            )

    async def split_task(
        self,
        task_id: str,
        evidence_ids: list[str],
        new_title: Optional[str] = None,
    ) -> UnifiedTaskCandidate:
        """Tách các evidence chỉ định khỏi task_id thành một UnifiedTask mới.

        Gọi TaskDomainRepository.split_task và cập nhật domain graph.
        Bảo toàn 100% Provenance của RawEvent và cập nhật updated_at cho cả 2 tasks.
        """
        cleaned_evidence_ids = [eid.strip() for eid in (evidence_ids or []) if eid and str(eid).strip()]
        if not cleaned_evidence_ids:
            raise ValueError("evidence_ids list cannot be empty")

        clean_title = new_title.strip() if isinstance(new_title, str) and new_title.strip() else None

        logger.info(
            f"Splitting task {task_id}: detaching {len(cleaned_evidence_ids)} evidences, new_title={clean_title}"
        )
        new_task = await self.task_repo.split_task(
            original_task_id=task_id,
            evidence_ids_to_detach=cleaned_evidence_ids,
            new_task_title=clean_title,
        )
        # Re-compute intelligence lifecycle on both original task and new task after split
        if hasattr(self, "lifecycle") and self.lifecycle:
            try:
                original_task = await self.task_repo.get_task_by_id(task_id)
                if isinstance(original_task, (UnifiedTaskCandidate, TaskWithContext)):
                    await self.lifecycle.on_task_changed(original_task)
            except Exception as orig_err:
                logger.debug("Could not recompute lifecycle for original task %s: %s", task_id, orig_err)

            if isinstance(new_task, (UnifiedTaskCandidate, TaskWithContext)):
                try:
                    lifecycle_res = await self.lifecycle.on_task_changed(new_task)
                    new_task = getattr(lifecycle_res, "task", new_task)
                except Exception as new_err:
                    logger.debug("Could not recompute lifecycle for new task: %s", new_err)
        return new_task

    async def update_task(
        self,
        task_id: str,
        **updates: Any,
    ) -> Optional[UnifiedTaskCandidate]:
        """Cập nhật các trường thông tin của UnifiedTask (title, description, notes, status, due_date, etc.)."""
        task = await self.task_repo.get_task_by_id(task_id)
        if not task and task_id.startswith("rev-"):
            candidate_id = task_id[4:]
            task = await self.task_repo.get_task_by_id(candidate_id)
            if task:
                task_id = task.id

        if not task:
            return None

        now = datetime.now(timezone.utc)
        old_status = task.status
        actor = updates.pop("actor", "USER")

        # Hỗ trợ trường 'notes' ánh xạ sang 'description' của UnifiedTaskCandidate
        if "notes" in updates:
            notes_val = updates.pop("notes")
            if notes_val is not None and ("description" not in updates or updates["description"] is None):
                updates["description"] = notes_val

        canonical_statuses = {
            TaskStatus.TODO.value,
            TaskStatus.IN_PROGRESS.value,
            TaskStatus.BLOCKED.value,
            TaskStatus.DONE.value,
            TaskStatus.DISMISSED.value,
        }

        for field, val in updates.items():
            if val is not None and hasattr(task, field):
                if field == "status":
                    if isinstance(val, str):
                        status_norm = val.strip().upper()
                        if status_norm not in canonical_statuses:
                            raise ValueError(f"Invalid status '{val}'. Must be one of {sorted(canonical_statuses)}")
                        val = TaskStatus(status_norm)
                elif field == "due_date":
                    if isinstance(val, str):
                        val_str = val.strip()
                        if not val_str:
                            val = None
                        else:
                            parsed_dt = datetime.fromisoformat(val_str.replace("Z", "+00:00"))
                            if parsed_dt.tzinfo is None:
                                parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
                            val = parsed_dt
                    elif isinstance(val, datetime) and val.tzinfo is None:
                        val = val.replace(tzinfo=timezone.utc)
                setattr(task, field, val)

        if "due_date" in updates and task.due_date is not None and "explicit_deadline" not in updates:
            task.explicit_deadline = True

        if "status" in updates and updates["status"] is not None and actor != "SYSTEM":
            task.status_authoritative = True
        if "priority_score" in updates and updates["priority_score"] is not None and actor != "SYSTEM":
            task.priority_override = task.priority_score

        task.updated_at = now
        await self.task_repo.upsert_task_atomic(task)

        if old_status != task.status:
            audit = StatusTransitionAuditRecord(
                id=str(uuid4()),
                task_id=task_id,
                old_status=old_status,
                new_status=task.status,
                reason=f"Status updated via update_task by {actor}",
                confidence=1.0,
                changed_at=now,
                change_actor=actor,
            )
            await self.task_repo.record_status_transition_audit(audit)

        # Trigger TaskIntelligenceLifecycle hook
        if hasattr(self, "lifecycle") and self.lifecycle:
            await self.lifecycle.on_task_changed(task, now=now)

        fetched = await self.task_repo.get_task_by_id(task_id)
        if fetched is not None:
            if "owner_name" in updates and updates["owner_name"] is not None:
                fetched.owner_name = task.owner_name
            if "requester_name" in updates and updates["requester_name"] is not None:
                fetched.requester_name = task.requester_name
            return fetched
        return task


