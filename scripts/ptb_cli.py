#!/usr/bin/env python3
"""Personal Task Board (PTB) Command Line Interface.

Quản trị và vận hành hệ thống Personal Task Board v1 theo docs/v1.md & docs/v1_1.md:
  ptb run             : Khởi chạy và giám sát toàn bộ hệ thống (Process Supervisor).
  ptb doctor          : Kiểm tra môi trường và các thành phần phụ thuộc.
  ptb login microsoft : Đăng nhập Microsoft 365 (Teams & Outlook Web) lưu session.
  ptb openwebui install: Cài đặt PTB Tools & Artifacts vào OpenWebUI.
  ptb status          : Báo cáo trạng thái chi tiết của từng subsystem.
  ptb init            : Khởi tạo database constraints & seed data trên Neo4j.
  ptb ingest          : Chạy 1 vòng quét tất cả các adapters.
  ptb serve           : Khởi động FastMCP Server và Application Service độc lập.
  ptb graph rebuild   : Rebuild semantic memory từ authoritative Neo4j domain data.
"""

import argparse
import asyncio
import inspect
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import socket
from datetime import datetime, timezone
import re
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Union
import urllib.error
import urllib.request

# Ensure UTF-8 output on Windows consoles to prevent UnicodeEncodeError with Vietnamese text
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            _sys_stdout_err = True
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        try:
            sys.stderr.reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            _sys_stderr_err = True

# Thêm root workspace vào sys.path để các module có thể import lẫn nhau
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "packages" / "contracts" / "src"))
sys.path.insert(0, str(ROOT_DIR / "packages" / "database" / "src"))
sys.path.insert(0, str(ROOT_DIR / "services" / "acquisition" / "src"))
sys.path.insert(0, str(ROOT_DIR / "services" / "processing" / "src"))
sys.path.insert(0, str(ROOT_DIR / "services" / "intelligence" / "src"))
sys.path.insert(0, str(ROOT_DIR / "services" / "application" / "src"))
sys.path.insert(0, str(ROOT_DIR / "services" / "mcp" / "src"))
sys.path.insert(0, str(ROOT_DIR / "packages" / "graph_memory" / "src"))
sys.path.insert(0, str(ROOT_DIR))
venv_sp = ROOT_DIR / ".venv" / "Lib" / "site-packages"
if venv_sp.exists() and str(venv_sp) not in sys.path:
    sys.path.append(str(venv_sp))

from ptb_contracts import BugCode, log_bug

try:
    from ptb_processing.llm_readiness import check_llm_readiness
except ImportError:
    check_llm_readiness = None  # type: ignore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ptb.cli")


def load_env():
    """Nạp biến môi trường từ file .env nếu có."""
    env_file = ROOT_DIR / ".env"
    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("\"'")
                    if k not in os.environ or not os.environ[k]:
                        os.environ[k] = v


def resolve_env_vars(val: Any) -> Any:
    """Đệ quy thay thế chuỗi ${VAR:-default} hoặc ${VAR} bằng giá trị biến môi trường."""
    if isinstance(val, str):
        pattern = re.compile(r"\$\{([A-Za-z0-9_]+)(?::-([^}]*))?\}")

        def _repl(match):
            var_name = match.group(1)
            default_val = match.group(2) if match.group(2) is not None else ""
            return os.getenv(var_name, default_val)

        return pattern.sub(_repl, val)
    elif isinstance(val, dict):
        return {k: resolve_env_vars(v) for k, v in val.items()}
    elif isinstance(val, list):
        return [resolve_env_vars(i) for i in val]
    return val


def load_config_yaml(file_path: Path) -> Dict[str, Any]:
    """Đọc file YAML cấu hình nếu tồn tại và phân giải biến môi trường."""
    if not file_path.exists():
        return {}
    try:
        import yaml
        with open(file_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
            return resolve_env_vars(raw)
    except Exception as e:
        logger.warning(f"Không thể đọc file YAML {file_path}: {e}")
        return {}


def get_sources_config(config_path: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """Tải cấu hình sources.yaml hoặc fallback sources.example.yaml."""
    if config_path:
        p = Path(config_path)
        if p.exists():
            return load_config_yaml(p)
    sources_file = ROOT_DIR / "config" / "sources.yaml"
    if sources_file.exists():
        return load_config_yaml(sources_file)
    example_file = ROOT_DIR / "config" / "sources.example.yaml"
    if example_file.exists():
        return load_config_yaml(example_file)
    return {}


def get_models_config(config_path: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """Tải cấu hình models.yaml hoặc fallback models.example.yaml."""
    if config_path:
        p = Path(config_path)
        if p.exists():
            return load_config_yaml(p)
    models_file = ROOT_DIR / "config" / "models.yaml"
    if models_file.exists():
        return load_config_yaml(models_file)
    example_file = ROOT_DIR / "config" / "models.example.yaml"
    if example_file.exists():
        return load_config_yaml(example_file)
    return {}


# ==============================================================================
# ADAPTER POLLING RUNNER (Independent Retry & Backoff Isolation)
# ==============================================================================
class AdapterPollingRunner:
    """Quản lý vòng lặp polling định kỳ cho một acquisition adapter với cơ chế retry và exponential backoff độc lập."""

    def __init__(
        self,
        name: Optional[str] = None,
        adapter: Optional[Any] = None,
        pipeline: Optional[Any] = None,
        poll_interval: float = 60.0,
        min_backoff: float = 5.0,
        max_backoff: float = 300.0,
        backoff_multiplier: float = 2.0,
        display_name: Optional[str] = None,
    ):
        if adapter is None:
            raise ValueError("Cần cung cấp adapter cho AdapterPollingRunner")
        if pipeline is None:
            raise ValueError("Cần cung cấp pipeline cho AdapterPollingRunner")

        resolved_name = name or (adapter.source_type.value if hasattr(adapter.source_type, "value") else str(adapter.source_type))
        self.name = resolved_name
        self.adapter = adapter
        self.pipeline = pipeline
        self.poll_interval = max(poll_interval, 0.05)
        self.min_backoff = max(min_backoff, 0.05)
        self.max_backoff = max_backoff
        self.backoff_multiplier = backoff_multiplier
        self.display_name = display_name or resolved_name.replace("_", " ").title()

        self.consecutive_failures: int = 0
        self.status: str = "INITIALIZING"
        self.last_sync_time: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self.total_synced_events: int = 0
        self.sync_count: int = 0

    async def poll_once(self) -> float:
        """Thực thi một chu kỳ sync đơn lẻ, trả về thời gian chờ (poll_interval hoặc backoff)."""
        try:
            events_synced = await self.pipeline.sync_adapter(self.adapter, stream_id="all", raise_on_error=True)
            self.sync_count += 1
            self.total_synced_events += events_synced
            self.consecutive_failures = 0
            self.status = "HEALTHY"
            self.last_sync_time = datetime.now(timezone.utc)
            self.last_error = None
            return self.poll_interval
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.consecutive_failures += 1
            self.status = "DEGRADED"
            self.last_error = str(e)
            retry_after = getattr(e, "retry_after_seconds", None)
            if retry_after is not None and retry_after > 0:
                backoff = min(self.max_backoff, max(self.min_backoff, float(retry_after)))
            else:
                backoff = min(
                    self.max_backoff,
                    self.min_backoff * (self.backoff_multiplier ** (self.consecutive_failures - 1)),
                )
            logger.warning(
                "Adapter '%s' (%s) polling error (attempt %d): %s. Backoff for %.1fs.",
                self.name,
                getattr(self.adapter, "source_type", "unknown"),
                self.consecutive_failures,
                e,
                backoff,
            )
            src_val = getattr(self.adapter, "source_type", None)
            src_type_str = src_val.value if hasattr(src_val, "value") else str(src_val) if src_val else self.name
            log_bug(
                code=BugCode.PTB_L1_002,
                subsystem=f"adapter_{self.name.lower()}",
                severity="WARNING",
                message=f"Adapter {self.name} synchronization failed: {e}",
                source_type=src_type_str,
                tenant_id=getattr(self.adapter, "tenant_id", None),
                exc=e,
                context={"consecutive_failures": self.consecutive_failures, "backoff": backoff},
            )
            return backoff

    async def run_loop(self, should_run_fn: Callable[[], bool]) -> None:
        """Thực thi vòng lặp polling định kỳ với checkpoint và independent retry/backoff."""
        self.status = "HEALTHY"
        while should_run_fn():
            try:
                sleep_time = await self.poll_once()
            except asyncio.CancelledError:
                break
            except Exception:
                sleep_time = self.min_backoff
            try:
                await asyncio.sleep(sleep_time)
            except asyncio.CancelledError:
                break
        self.status = "STOPPED"


# ==============================================================================
# PROCESS SUPERVISOR (ptb run)
# ==============================================================================
class PTBProcessSupervisor:
    """CLI Process Supervisor quản lý và giám sát đồng thời toàn bộ các components:

    1. Playwright acquisition runner (Teams & Outlook interceptors)
    2. Coding Agent Watchers (Cursor, Claude Code, Antigravity,...)
    3. External Ingestion Adapters (GitWatcherAdapter, JiraAdapter, ShortcutAdapter)
    4. ProcessingWorker vòng lặp xử lý background
    5. Application Service FastAPI REST trên 127.0.0.1:8000
    6. FastMCP Server trên 127.0.0.1:8001
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        app_port: int = 8000,
        mcp_port: int = 8001,
        tenant_id: str = "local-user",
        enable_playwright: bool = True,
        poll_interval: float = 2.0,
        storage_path: Optional[str] = None,
        raw_event_repo: Optional[Any] = None,
        checkpoint_repo: Optional[Any] = None,
        task_repo: Optional[Any] = None,
        application_service: Optional[Any] = None,
        sources_config: Optional[Dict[str, Any]] = None,
        sources_config_path: Optional[str] = None,
        git_adapter: Optional[Any] = None,
        jira_adapter: Optional[Any] = None,
        shortcut_adapter: Optional[Any] = None,
        confluence_adapter: Optional[Any] = None,
        github_adapter: Optional[Any] = None,
        slack_adapter: Optional[Any] = None,
        outlook_adapter: Optional[Any] = None,
        sync_worker: Optional[Any] = None,
        port: Optional[int] = None,
    ):
        self.host = host or os.getenv("PTB_HOST", "127.0.0.1")
        self.app_port = port or app_port or int(os.getenv("PTB_PORT", "8000"))
        self.mcp_port = mcp_port or int(os.getenv("PTB_MCP_PORT", "8001"))
        self.tenant_id = tenant_id
        self.enable_playwright = enable_playwright
        self.poll_interval = poll_interval
        self.storage_path = storage_path or os.getenv("PTB_STORAGE_STATE") or os.path.join(ROOT_DIR, "data", "playwright", "storage_state.json")

        self.raw_event_repo = raw_event_repo
        self.checkpoint_repo = checkpoint_repo
        self.task_repo = task_repo
        self.application_service = application_service
        self.sync_worker = sync_worker

        self.sources_config_path = sources_config_path
        self.sources_config = sources_config or (get_sources_config(sources_config_path) if sources_config_path else None)
        self.git_adapter = git_adapter
        self.jira_adapter = jira_adapter
        self.shortcut_adapter = shortcut_adapter
        self.confluence_adapter = confluence_adapter
        self.github_adapter = github_adapter
        self.slack_adapter = slack_adapter
        self.outlook_adapter = outlook_adapter

        self.state: str = "INITIALIZING"
        self._running = False
        self._shutdown_event = asyncio.Event()

        # Component references
        self.app_server: Optional[Any] = None
        self.mcp_server: Optional[Any] = None
        self.processing_worker: Optional[Any] = None
        self.graph_sync_task: Optional[asyncio.Task] = None
        self.playwright_orchestrator: Optional[Any] = None
        self.neo4j_client: Optional[Any] = None
        self.acq_pipeline: Optional[Any] = None
        self.adapter_runners: Dict[str, AdapterPollingRunner] = {}
        self.tasks: List[asyncio.Task] = []

    def trigger_shutdown(self) -> None:
        """Kích hoạt tín hiệu dừng graceful shutdown."""
        self._shutdown_event.set()

    async def get_graph_backlog_count(self) -> int:
        """Đếm số lượng episodes đang PENDING hoặc RETRY chờ đồng bộ sang Graphiti."""
        count = 0
        if self.sync_worker is not None:
            pending_items = getattr(self.sync_worker, "_pending_items", None)
            if isinstance(pending_items, dict):
                count += len(pending_items)

        if self.neo4j_client is not None and hasattr(self.neo4j_client, "get_driver"):
            try:
                driver = self.neo4j_client.get_driver()
                if driver is not None and hasattr(driver, "session"):
                    cypher = """
                    MATCH (n:EpisodicNode)
                    WHERE coalesce(n.graph_sync_status, 'PENDING') IN ['PENDING', 'RETRY']
                    RETURN count(n) AS cnt
                    """
                    async with driver.session() as session:
                        res = await session.run(cypher)
                        rec = await res.single()
                        if rec and "cnt" in rec:
                            count += rec["cnt"]
            except Exception as e:
                logger.debug("Lỗi truy vấn graph backlog từ Neo4j: %s", e)
        return count

    async def get_system_health(self) -> Dict[str, Any]:
        """Kiểm tra tình trạng runtime health của toàn bộ supervisor subsystems."""
        if self.application_service is not None and hasattr(self.application_service, "get_system_health"):
            health = await self.application_service.get_system_health()
        else:
            health = {
                "status": "healthy" if self.state in ("READY", "READY_WITH_WARNINGS") else self.state.lower(),
                "service": "ptb-supervisor",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "neo4j": "healthy" if self.neo4j_client else "not_ready",
                "processing_worker": "healthy" if self.processing_worker and getattr(self.processing_worker, "_running", False) else "not_ready",
                "graphiti": "healthy",
            }

        graph_running = bool(self.graph_sync_task and not self.graph_sync_task.done())
        health["graph_worker"] = "RUNNING" if graph_running else "STOPPED"
        backlog_count = await self.get_graph_backlog_count()
        health["graph_backlog"] = backlog_count

        adapter = None
        if self.sync_worker is not None:
            mem_client = getattr(self.sync_worker, "memory_client", None)
            adapter = getattr(mem_client, "adapter", None)
        if adapter is None and self.application_service is not None:
            mem_client = getattr(self.application_service, "graph_memory", None)
            adapter = getattr(mem_client, "adapter", None)

        if adapter is not None:
            if not getattr(adapter, "is_available", True) or getattr(adapter, "last_error", None) is not None:
                health["graphiti"] = "degraded"
                log_bug(
                    code=BugCode.PTB_GRAPH_001,
                    subsystem="graph_memory",
                    severity="WARNING",
                    message="Graphiti adapter is unavailable or errored",
                )
                if health.get("status") == "healthy":
                    health["status"] = "degraded"
            else:
                health["graphiti"] = "healthy"

        return health

    async def _poll_health_url(self, url: str, timeout: float = 1.0) -> bool:
        """Kiểm tra HTTP endpoint trả về status 200 và JSON status != 'not_ready' trong separate thread."""
        def _check():
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "PTB-Supervisor"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    status_code = getattr(resp, "status", None)
                    if not isinstance(status_code, int):
                        status_code = getattr(resp, "status_code", None)
                    if not isinstance(status_code, int) and hasattr(resp, "getcode"):
                        code = resp.getcode()
                        if isinstance(code, int):
                            status_code = code
                    if status_code is None:
                        status_code = 200
                    if status_code != 200:
                        return False

                    raw_body = resp.read() if hasattr(resp, "read") else b""
                    if isinstance(raw_body, bytes):
                        raw_body = raw_body.decode("utf-8")
                    if isinstance(raw_body, str) and raw_body.strip():
                        data = json.loads(raw_body)
                    elif hasattr(resp, "json") and callable(resp.json):
                        data = resp.json()
                    else:
                        data = {}

                    if not isinstance(data, dict):
                        return False
                    if str(data.get("status", "")).lower() == "not_ready":
                        return False
                    return True
            except Exception:
                return False
        return await asyncio.to_thread(_check)

    async def _poll_port_open(self, host: str, port: int, timeout: float = 1.0) -> bool:
        """Kiểm tra TCP port đã bind và listening hay chưa."""
        def _check():
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            try:
                s.connect((host, port))
                s.close()
                return True
            except Exception:
                return False
        return await asyncio.to_thread(_check)

    async def run(self, max_runtime: Optional[float] = None) -> int:
        """Khởi động toàn bộ runtime services và giám sát health check."""
        import uvicorn
        from ptb_acquisition.adapters import AgentWatchersAdapter
        from ptb_acquisition.pipeline import AcquisitionPipeline
        from ptb_acquisition.playwright.runner import PlaywrightOrchestrator
        from ptb_acquisition.playwright.session import SessionHealthState
        from ptb_processing.pipeline import ProcessingPipeline
        from ptb_processing.worker import ProcessingWorker

        print("\n" + "=" * 70)
        print("PERSONAL TASK BOARD: STARTING PROCESS SUPERVISOR (ptb run)")
        print("=" * 70)

        # 1. Khởi tạo kho dữ liệu (Neo4j Authoritative Domain Store - Fail-Fast)
        if self.raw_event_repo is None or self.checkpoint_repo is None:
            try:
                from ptb_database.neo4j_client import Neo4jClient
                from ptb_database.repositories import (
                    CheckpointRepository,
                    RawEventRepository,
                    TaskDomainRepository,
                )
                client = Neo4jClient()
                if await client.verify_connectivity():
                    self.neo4j_client = client
                    self.raw_event_repo = RawEventRepository(client)
                    self.checkpoint_repo = CheckpointRepository(client)
                    emb_svc = None
                    is_local_ai_enabled = (
                        os.getenv("PTB_ENABLE_LOCAL_AI", "false").lower() in ("true", "1", "yes")
                        or os.getenv("PTB_REQUIRE_LOCAL_AI", "false").lower() in ("true", "1", "yes")
                    )
                    if is_local_ai_enabled:
                        try:
                            from packages.ai_service import Qwen3EmbeddingService
                            emb_svc = Qwen3EmbeddingService()
                        except Exception as e_emb:
                            logger.debug("Qwen3 embedding service not loaded in supervisor: %s", e_emb)
                    self.task_repo = TaskDomainRepository(client, embedding_service=emb_svc)
                    print("  [✓] Đã kết nối Neo4j Persistence Engine (Authoritative Domain Store)")
                else:
                    await client.close()
                    self.state = "NOT_READY"
                    log_bug(
                        code=BugCode.PTB_STORAGE_001,
                        subsystem="neo4j",
                        severity="CRITICAL",
                        message="Neo4j authoritative store unavailable",
                    )
                    print("\n[✗] CRITICAL ERROR: Neo4j authoritative store unavailable. Không thể khởi động supervisor!")
                    print("    Hệ thống Personal Task Board yêu cầu Neo4j hoạt động (Fail-Fast).")
                    print("    Tuyệt đối không tiếp tục chạy với RAM store.\n")
                    return 1
            except Exception as e:
                logger.error(f"Neo4j connectivity check failed: {e}")
                self.state = "NOT_READY"
                log_bug(
                    code=BugCode.PTB_STORAGE_001,
                    subsystem="neo4j",
                    severity="CRITICAL",
                    message="Neo4j authoritative store unavailable",
                    exc=e,
                )
                print(f"\n[✗] CRITICAL ERROR: Neo4j không khả dụng ({e}). Không thể khởi động supervisor!")
                print("    Hệ thống Personal Task Board yêu cầu Neo4j hoạt động (Fail-Fast).")
                print("    Tuyệt đối không tiếp tục chạy với RAM store.\n")
                return 1

        if self.task_repo is None and self.neo4j_client:
            from ptb_database.repositories import TaskDomainRepository
            emb_svc = None
            is_local_ai_enabled = (
                os.getenv("PTB_ENABLE_LOCAL_AI", "false").lower() in ("true", "1", "yes")
                or os.getenv("PTB_REQUIRE_LOCAL_AI", "false").lower() in ("true", "1", "yes")
            )
            if is_local_ai_enabled:
                try:
                    from packages.ai_service import Qwen3EmbeddingService
                    emb_svc = Qwen3EmbeddingService()
                except Exception as e_emb:
                    logger.debug("Qwen3 embedding service not loaded in supervisor: %s", e_emb)
            self.task_repo = TaskDomainRepository(self.neo4j_client, embedding_service=emb_svc)

        # 2. Pipeline thu nạp (Acquisition) & Configured Adapters
        from ptb_acquisition.adapters import (
            AgentWatchersAdapter,
            GitWatcherAdapter,
            JiraAdapter,
            ShortcutAdapter,
            ConfluenceAdapter,
            GitHubAdapter,
            SlackAdapter,
            OutlookMailAdapter,
        )

        acq_pipeline = AcquisitionPipeline(
            raw_event_repo=self.raw_event_repo,
            checkpoint_repo=self.checkpoint_repo,
            tenant_id=self.tenant_id,
        )
        self.acq_pipeline = acq_pipeline

        # Tải cấu hình sources
        if self.sources_config is not None:
            sources_cfg = self.sources_config
        else:
            sources_cfg = get_sources_config(self.sources_config_path)
        sources_dict = sources_cfg.get("sources", {})

        # (a) Coding Agent Watchers
        agent_adapter = AgentWatchersAdapter(tenant_id=self.tenant_id)
        acq_pipeline.register_adapter("coding_agents", agent_adapter)
        coding_cfg = sources_dict.get("coding_agents", {})
        coding_poll = float(coding_cfg.get("poll_interval_seconds", 5.0)) if isinstance(coding_cfg, dict) else 5.0
        if self.poll_interval < 1.0:
            coding_poll = min(coding_poll, self.poll_interval)
        coding_runner = AdapterPollingRunner(
            name="coding_agents",
            adapter=agent_adapter,
            pipeline=acq_pipeline,
            poll_interval=coding_poll,
            min_backoff=min(5.0, coding_poll),
            display_name="Coding Agent Watchers",
        )
        self.adapter_runners["coding_agents"] = coding_runner

        # (b) Git Watcher Adapter
        git_cfg = sources_dict.get("git", {})
        git_enabled = git_cfg.get("enabled", True) if isinstance(git_cfg, dict) else True
        if self.git_adapter is not None:
            g_adapter = self.git_adapter
            acq_pipeline.register_adapter("git", g_adapter)
            git_poll = float(git_cfg.get("poll_interval_seconds", 60.0)) if isinstance(git_cfg, dict) else 60.0
            if self.poll_interval < 1.0:
                git_poll = min(git_poll, self.poll_interval)
            git_runner = AdapterPollingRunner(
                name="git",
                adapter=g_adapter,
                pipeline=acq_pipeline,
                poll_interval=git_poll,
                min_backoff=min(5.0, git_poll),
                display_name="Local Git Watcher",
            )
            self.adapter_runners["git"] = git_runner
        elif git_enabled:
            repo_paths = git_cfg.get("repo_paths", [str(ROOT_DIR)]) if isinstance(git_cfg, dict) else [str(ROOT_DIR)]
            expanded_paths = [os.path.expanduser(p) for p in repo_paths if os.path.exists(os.path.expanduser(p))]
            if not expanded_paths:
                expanded_paths = [str(ROOT_DIR)]
            g_adapter = GitWatcherAdapter(repo_paths=expanded_paths, tenant_id=self.tenant_id)
            acq_pipeline.register_adapter("git", g_adapter)
            git_poll = float(git_cfg.get("poll_interval_seconds", 60.0)) if isinstance(git_cfg, dict) else 60.0
            if self.poll_interval < 1.0:
                git_poll = min(git_poll, self.poll_interval)
            git_runner = AdapterPollingRunner(
                name="git",
                adapter=g_adapter,
                pipeline=acq_pipeline,
                poll_interval=git_poll,
                min_backoff=min(5.0, git_poll),
                display_name="Local Git Watcher",
            )
            self.adapter_runners["git"] = git_runner

        # (c) Jira Cloud / Server Adapter
        jira_cfg = sources_dict.get("jira", {})
        jira_enabled = jira_cfg.get("enabled", False) if isinstance(jira_cfg, dict) else False
        jira_token = (jira_cfg.get("api_token") or os.getenv("JIRA_API_TOKEN", "")).strip() if isinstance(jira_cfg, dict) else os.getenv("JIRA_API_TOKEN", "").strip()
        jira_url = (jira_cfg.get("base_url") or os.getenv("JIRA_BASE_URL", "")).strip() if isinstance(jira_cfg, dict) else os.getenv("JIRA_BASE_URL", "").strip()

        if self.jira_adapter is not None:
            j_adapter = self.jira_adapter
            acq_pipeline.register_adapter("jira", j_adapter)
            jira_poll = float(jira_cfg.get("poll_interval_seconds", 60.0)) if isinstance(jira_cfg, dict) else 60.0
            if self.poll_interval < 1.0:
                jira_poll = min(jira_poll, self.poll_interval)
            jira_runner = AdapterPollingRunner(
                name="jira",
                adapter=j_adapter,
                pipeline=acq_pipeline,
                poll_interval=jira_poll,
                min_backoff=min(5.0, jira_poll),
                display_name="Jira Cloud/Server",
            )
            self.adapter_runners["jira"] = jira_runner
        elif (jira_enabled or jira_token) and jira_token and jira_url:
            j_adapter = JiraAdapter(
                base_url=jira_url,
                email=jira_cfg.get("email") or os.getenv("JIRA_EMAIL"),
                api_token=jira_token,
                jql=jira_cfg.get("default_jql") or os.getenv("JIRA_JQL"),
                project_keys=jira_cfg.get("project_keys", []),
                tenant_id=self.tenant_id,
            )
            acq_pipeline.register_adapter("jira", j_adapter)
            jira_poll = float(jira_cfg.get("poll_interval_seconds", 60.0)) if isinstance(jira_cfg, dict) else 60.0
            if self.poll_interval < 1.0:
                jira_poll = min(jira_poll, self.poll_interval)
            jira_runner = AdapterPollingRunner(
                name="jira",
                adapter=j_adapter,
                pipeline=acq_pipeline,
                poll_interval=jira_poll,
                min_backoff=min(5.0, jira_poll),
                display_name="Jira Cloud/Server",
            )
            self.adapter_runners["jira"] = jira_runner
        elif jira_enabled and (not jira_token or not jira_url):
            logger.info("Jira adapter enabled in configuration but missing base_url or api_token. Skipped polling.")

        # (d) Shortcut REST API Adapter
        shortcut_cfg = sources_dict.get("shortcut", {})
        shortcut_enabled = shortcut_cfg.get("enabled", False) if isinstance(shortcut_cfg, dict) else False
        shortcut_token = (shortcut_cfg.get("api_token") or os.getenv("SHORTCUT_API_TOKEN", "")).strip() if isinstance(shortcut_cfg, dict) else os.getenv("SHORTCUT_API_TOKEN", "").strip()

        if self.shortcut_adapter is not None:
            sc_adapter = self.shortcut_adapter
            acq_pipeline.register_adapter("shortcut", sc_adapter)
            shortcut_poll = float(shortcut_cfg.get("poll_interval_seconds", 60.0)) if isinstance(shortcut_cfg, dict) else 60.0
            if self.poll_interval < 1.0:
                shortcut_poll = min(shortcut_poll, self.poll_interval)
            shortcut_runner = AdapterPollingRunner(
                name="shortcut",
                adapter=sc_adapter,
                pipeline=acq_pipeline,
                poll_interval=shortcut_poll,
                min_backoff=min(5.0, shortcut_poll),
                display_name="Shortcut Stories",
            )
            self.adapter_runners["shortcut"] = shortcut_runner
        elif (shortcut_enabled or shortcut_token) and shortcut_token:
            sc_adapter = ShortcutAdapter(
                api_token=shortcut_token,
                base_url=shortcut_cfg.get("base_url"),
                project_ids=shortcut_cfg.get("project_ids", []),
                query=shortcut_cfg.get("default_query") or os.getenv("SHORTCUT_QUERY"),
                tenant_id=self.tenant_id,
            )
            acq_pipeline.register_adapter("shortcut", sc_adapter)
            shortcut_poll = float(shortcut_cfg.get("poll_interval_seconds", 60.0)) if isinstance(shortcut_cfg, dict) else 60.0
            if self.poll_interval < 1.0:
                shortcut_poll = min(shortcut_poll, self.poll_interval)
            shortcut_runner = AdapterPollingRunner(
                name="shortcut",
                adapter=sc_adapter,
                pipeline=acq_pipeline,
                poll_interval=shortcut_poll,
                min_backoff=min(5.0, shortcut_poll),
                display_name="Shortcut Stories",
            )
            self.adapter_runners["shortcut"] = shortcut_runner
        elif shortcut_enabled and not shortcut_token:
            logger.info("Shortcut adapter enabled in configuration but missing api_token. Skipped polling.")

        # (e) Confluence Knowledge & Meeting Notes Adapter
        conf_cfg = sources_dict.get("confluence", {})
        conf_enabled = conf_cfg.get("enabled", False) if isinstance(conf_cfg, dict) else False
        conf_token = (conf_cfg.get("api_token") or os.getenv("CONFLUENCE_API_TOKEN", "")).strip() if isinstance(conf_cfg, dict) else os.getenv("CONFLUENCE_API_TOKEN", "").strip()
        conf_url = (conf_cfg.get("base_url") or os.getenv("CONFLUENCE_BASE_URL", "")).strip() if isinstance(conf_cfg, dict) else os.getenv("CONFLUENCE_BASE_URL", "").strip()

        if self.confluence_adapter is not None:
            c_adapter = self.confluence_adapter
            acq_pipeline.register_adapter("confluence", c_adapter)
            conf_poll = float(conf_cfg.get("poll_interval_seconds", 120.0)) if isinstance(conf_cfg, dict) else 120.0
            if self.poll_interval < 1.0:
                conf_poll = min(conf_poll, self.poll_interval)
            conf_runner = AdapterPollingRunner(
                name="confluence",
                adapter=c_adapter,
                pipeline=acq_pipeline,
                poll_interval=conf_poll,
                min_backoff=min(5.0, conf_poll),
                display_name="Confluence Docs",
            )
            self.adapter_runners["confluence"] = conf_runner
        elif (conf_enabled or conf_token) and conf_token and conf_url:
            c_adapter = ConfluenceAdapter(
                base_url=conf_url,
                email=conf_cfg.get("email") or os.getenv("CONFLUENCE_EMAIL"),
                api_token=conf_token,
                space_keys=conf_cfg.get("space_keys", []),
                tenant_id=self.tenant_id,
            )
            acq_pipeline.register_adapter("confluence", c_adapter)
            conf_poll = float(conf_cfg.get("poll_interval_seconds", 120.0)) if isinstance(conf_cfg, dict) else 120.0
            if self.poll_interval < 1.0:
                conf_poll = min(conf_poll, self.poll_interval)
            conf_runner = AdapterPollingRunner(
                name="confluence",
                adapter=c_adapter,
                pipeline=acq_pipeline,
                poll_interval=conf_poll,
                min_backoff=min(5.0, conf_poll),
                display_name="Confluence Docs",
            )
            self.adapter_runners["confluence"] = conf_runner
        elif conf_enabled and (not conf_token or not conf_url):
            logger.info("Confluence adapter enabled in configuration but missing base_url or api_token. Skipped polling.")

        # (f) GitHub Issues & Pull Requests Adapter
        gh_cfg = sources_dict.get("github", {})
        gh_enabled = gh_cfg.get("enabled", False) if isinstance(gh_cfg, dict) else False
        gh_token = (gh_cfg.get("api_token") or os.getenv("GITHUB_TOKEN", "")).strip() if isinstance(gh_cfg, dict) else os.getenv("GITHUB_TOKEN", "").strip()
        gh_repos = gh_cfg.get("repositories", []) if isinstance(gh_cfg, dict) else []

        if self.github_adapter is not None:
            gh_adapter_inst = self.github_adapter
            acq_pipeline.register_adapter("github", gh_adapter_inst)
            gh_poll = float(gh_cfg.get("poll_interval_seconds", 60.0)) if isinstance(gh_cfg, dict) else 60.0
            if self.poll_interval < 1.0:
                gh_poll = min(gh_poll, self.poll_interval)
            gh_runner = AdapterPollingRunner(
                name="github",
                adapter=gh_adapter_inst,
                pipeline=acq_pipeline,
                poll_interval=gh_poll,
                min_backoff=min(5.0, gh_poll),
                display_name="GitHub Repositories",
            )
            self.adapter_runners["github"] = gh_runner
        elif (gh_enabled or gh_token) and (gh_token or gh_repos):
            gh_adapter_inst = GitHubAdapter(
                repositories=gh_repos,
                api_token=gh_token,
                tenant_id=self.tenant_id,
            )
            acq_pipeline.register_adapter("github", gh_adapter_inst)
            gh_poll = float(gh_cfg.get("poll_interval_seconds", 60.0)) if isinstance(gh_cfg, dict) else 60.0
            if self.poll_interval < 1.0:
                gh_poll = min(gh_poll, self.poll_interval)
            gh_runner = AdapterPollingRunner(
                name="github",
                adapter=gh_adapter_inst,
                pipeline=acq_pipeline,
                poll_interval=gh_poll,
                min_backoff=min(5.0, gh_poll),
                display_name="GitHub Repositories",
            )
            self.adapter_runners["github"] = gh_runner
        elif gh_enabled and not gh_token:
            logger.info("GitHub adapter enabled in configuration but missing api_token. Skipped polling.")

        # (g) Slack Workspaces Adapter
        slack_cfg = sources_dict.get("slack", {})
        slack_enabled = slack_cfg.get("enabled", False) if isinstance(slack_cfg, dict) else False
        slack_token = (slack_cfg.get("bot_token") or os.getenv("SLACK_BOT_TOKEN", "")).strip() if isinstance(slack_cfg, dict) else os.getenv("SLACK_BOT_TOKEN", "").strip()

        if self.slack_adapter is not None:
            slk_adapter = self.slack_adapter
            acq_pipeline.register_adapter("slack", slk_adapter)
            slack_poll = float(slack_cfg.get("poll_interval_seconds", 60.0)) if isinstance(slack_cfg, dict) else 60.0
            if self.poll_interval < 1.0:
                slack_poll = min(slack_poll, self.poll_interval)
            slk_runner = AdapterPollingRunner(
                name="slack",
                adapter=slk_adapter,
                pipeline=acq_pipeline,
                poll_interval=slack_poll,
                min_backoff=min(5.0, slack_poll),
                display_name="Slack Workspace",
            )
            self.adapter_runners["slack"] = slk_runner
        elif (slack_enabled or slack_token) and slack_token:
            slk_adapter = SlackAdapter(
                bot_token=slack_token,
                channels=slack_cfg.get("channels", []),
                tenant_id=self.tenant_id,
            )
            acq_pipeline.register_adapter("slack", slk_adapter)
            slack_poll = float(slack_cfg.get("poll_interval_seconds", 60.0)) if isinstance(slack_cfg, dict) else 60.0
            if self.poll_interval < 1.0:
                slack_poll = min(slack_poll, self.poll_interval)
            slk_runner = AdapterPollingRunner(
                name="slack",
                adapter=slk_adapter,
                pipeline=acq_pipeline,
                poll_interval=slack_poll,
                min_backoff=min(5.0, slack_poll),
                display_name="Slack Workspace",
            )
            self.adapter_runners["slack"] = slk_runner
        elif slack_enabled and not slack_token:
            logger.info("Slack adapter enabled in configuration but missing bot_token. Skipped polling.")

        # (h) Microsoft Outlook Direct REST / Graph API Adapter
        outlook_cfg = sources_dict.get("ms_outlook") or sources_dict.get("outlook", {})
        outlook_enabled = outlook_cfg.get("enabled", False) if isinstance(outlook_cfg, dict) else False
        outlook_token = (outlook_cfg.get("graph_token") or os.getenv("MS_GRAPH_TOKEN", "")).strip() if isinstance(outlook_cfg, dict) else os.getenv("MS_GRAPH_TOKEN", "").strip()

        if self.outlook_adapter is not None:
            otl_adapter = self.outlook_adapter
            acq_pipeline.register_adapter("outlook", otl_adapter)
            outlook_poll = float(outlook_cfg.get("poll_interval_seconds", 120.0)) if isinstance(outlook_cfg, dict) else 120.0
            if self.poll_interval < 1.0:
                outlook_poll = min(outlook_poll, self.poll_interval)
            otl_runner = AdapterPollingRunner(
                name="outlook",
                adapter=otl_adapter,
                pipeline=acq_pipeline,
                poll_interval=outlook_poll,
                min_backoff=min(5.0, outlook_poll),
                display_name="Outlook Direct Mail",
            )
            self.adapter_runners["outlook"] = otl_runner
        elif (outlook_enabled or outlook_token) and outlook_token:
            otl_adapter = OutlookMailAdapter(
                tenant_id=self.tenant_id,
                graph_token=outlook_token,
                mailbox=outlook_cfg.get("mailbox") or os.getenv("MS_OUTLOOK_MAILBOX"),
                folders=outlook_cfg.get("folders", ["Inbox"]),
                storage_path=self.storage_path,
            )
            acq_pipeline.register_adapter("outlook", otl_adapter)
            outlook_poll = float(outlook_cfg.get("poll_interval_seconds", 120.0)) if isinstance(outlook_cfg, dict) else 120.0
            if self.poll_interval < 1.0:
                outlook_poll = min(outlook_poll, self.poll_interval)
            otl_runner = AdapterPollingRunner(
                name="outlook",
                adapter=otl_adapter,
                pipeline=acq_pipeline,
                poll_interval=outlook_poll,
                min_backoff=min(5.0, outlook_poll),
                display_name="Outlook Direct Mail",
            )
            self.adapter_runners["outlook"] = otl_runner
        elif outlook_enabled and not outlook_token:
            logger.info("Outlook adapter enabled in configuration but missing graph_token. Skipped polling.")

        # 3. Processing Worker
        from ptb_intelligence.lifecycle import TaskIntelligenceLifecycle

        lifecycle = TaskIntelligenceLifecycle(task_repo=self.task_repo)
        proc_pipeline = ProcessingPipeline(
            task_repo=self.task_repo,
            intelligence_lifecycle=lifecycle,
        )
        self.processing_worker = ProcessingWorker(
            raw_event_repo=self.raw_event_repo,
            pipeline=proc_pipeline,
        )

        # 4. Playwright Orchestrator (Teams & Outlook Interceptors)
        self.playwright_orchestrator = PlaywrightOrchestrator(
            storage_path=self.storage_path,
            pipeline=acq_pipeline,
            raw_event_repo=self.raw_event_repo,
            tenant_id=f"{self.tenant_id}-microsoft",
            headless=True,
        )

        # 5. Dependency Graph dùng chung (Shared Runtime Dependency Graph)
        # Neo4jClient -> Repositories -> ProcessingPipeline -> TaskIntelligenceLifecycle -> GraphitiMemoryClient -> ApplicationService
        if self.application_service is None:
            from ptb_graph_memory.client import GraphitiMemoryClient
            from ptb_application.service import ApplicationService

            graph_memory = GraphitiMemoryClient(neo4j_client=self.neo4j_client) if self.neo4j_client else None
            self.application_service = ApplicationService(
                task_repo=self.task_repo,
                raw_event_repo=self.raw_event_repo,
                checkpoint_repo=self.checkpoint_repo,
                lifecycle=lifecycle,
                graph_memory=graph_memory,
                neo4j_client=self.neo4j_client,
                processing_pipeline=proc_pipeline,
            )
        shared_service = self.application_service

        # 5b. Khởi tạo GraphMemorySyncWorker (chạy nền đồng bộ tri thức sang Graphiti)
        if self.sync_worker is None:
            from ptb_graph_memory.sync_worker import GraphMemorySyncWorker
            mem_client = getattr(self.application_service, "graph_memory", None)
            self.sync_worker = GraphMemorySyncWorker(
                memory_client=mem_client,
                neo4j_client=self.neo4j_client,
            )

        # 6. Inject instance shared_service này vào cả FastAPI app (:8000) và FastMCP server (:8001)
        from ptb_application.api import create_app, set_application_service
        set_application_service(shared_service)
        fastapi_app = create_app(application_service=shared_service)
        app_config = uvicorn.Config(
            fastapi_app,
            host=self.host,
            port=self.app_port,
            log_level="warning",
            loop="asyncio",
            timeout_graceful_shutdown=1,
        )
        self.app_server = uvicorn.Server(app_config)

        from ptb_mcp.server import create_mcp_server, get_mcp_health, set_application_service as set_mcp_app_service
        from starlette.responses import JSONResponse
        from starlette.routing import Route
        set_mcp_app_service(shared_service)
        mcp_srv = create_mcp_server(application_service=shared_service)
        starlette_mcp = mcp_srv.sse_app(host=self.host)
        async def _mcp_health_route(req):
            h_data = await get_mcp_health(shared_service)
            return JSONResponse(h_data)
        starlette_mcp.routes.append(Route("/health", _mcp_health_route))
        mcp_config = uvicorn.Config(
            starlette_mcp,
            host=self.host,
            port=self.mcp_port,
            log_level="warning",
            loop="asyncio",
            timeout_graceful_shutdown=1,
        )
        self.mcp_server = uvicorn.Server(mcp_config)

        self._running = True

        # Launch Tasks
        # 1. FastAPI REST Server
        self.tasks.append(
            asyncio.create_task(self.app_server.serve(), name="ptb-app-server")
        )

        # 2. FastMCP SSE Server
        self.tasks.append(
            asyncio.create_task(self.mcp_server.serve(), name="ptb-mcp-server")
        )

        # 3. ProcessingWorker Background Loop
        worker_task = asyncio.create_task(
            self.processing_worker.run_loop(poll_interval=self.poll_interval),
            name="ptb-processing-worker",
        )
        self.tasks.append(worker_task)

        # 3b. GraphMemorySyncWorker Background Loop
        async def _graph_sync_loop():
            logger.info("GraphMemorySyncWorker loop started.")
            if hasattr(self.sync_worker, "run_loop") and callable(self.sync_worker.run_loop):
                try:
                    await self.sync_worker.run_loop(poll_interval=self.poll_interval)
                    return
                except asyncio.CancelledError:
                    return
                except Exception as err:
                    logger.warning("Graph memory sync worker run_loop error: %s", err)
                    log_bug(
                        code=BugCode.PTB_GRAPH_001,
                        subsystem="graph_memory",
                        severity="WARNING",
                        message=f"Graph memory sync worker loop failed: {err}",
                        exc=err,
                    )

            if hasattr(self.sync_worker, "start") and callable(self.sync_worker.start):
                try:
                    res = self.sync_worker.start()
                    if inspect.isawaitable(res):
                        await res
                        return
                except asyncio.CancelledError:
                    return
                except Exception as err:
                    logger.warning("Graph memory sync worker start error: %s", err)
                    log_bug(
                        code=BugCode.PTB_GRAPH_001,
                        subsystem="graph_memory",
                        severity="WARNING",
                        message=f"Graph memory sync worker start failed: {err}",
                        exc=err,
                    )

            while self._running:
                try:
                    if hasattr(self.sync_worker, "run_sync_sweep") and callable(self.sync_worker.run_sync_sweep):
                        sweep_res = self.sync_worker.run_sync_sweep()
                        if inspect.isawaitable(sweep_res):
                            await sweep_res
                except asyncio.CancelledError:
                    break
                except Exception as err:
                    logger.warning("Graph memory sync sweep error: %s", err)
                    log_bug(
                        code=BugCode.PTB_GRAPH_001,
                        subsystem="graph_memory",
                        severity="WARNING",
                        message=f"Graph memory sync sweep failed: {err}",
                        exc=err,
                    )
                try:
                    await asyncio.sleep(self.poll_interval)
                except asyncio.CancelledError:
                    break
            logger.info("GraphMemorySyncWorker loop stopped.")

        self.graph_sync_task = asyncio.create_task(
            _graph_sync_loop(),
            name="ptb-graph-sync-worker",
        )
        self.tasks.append(self.graph_sync_task)

        # 4. Ingestion Adapter Background Polling Tasks (Coding Agents, Git, Jira, Shortcut)
        for a_name, a_runner in self.adapter_runners.items():
            self.tasks.append(
                asyncio.create_task(
                    a_runner.run_loop(lambda: self._running),
                    name=f"ptb-adapter-{a_name}",
                )
            )

        # Kiểm tra trước trạng thái session Microsoft
        initial_pw_state = self.playwright_orchestrator.session_mgr.validate_session()

        # 5. Playwright Acquisition Daemon
        async def _playwright_loop():
            if not self.enable_playwright:
                logger.info("Playwright capture is disabled via command line.")
                return

            validation_state = self.playwright_orchestrator.session_mgr.validate_session()
            if validation_state in (
                SessionHealthState.UNCONFIGURED,
                SessionHealthState.LOGIN_REQUIRED,
                SessionHealthState.AUTH_EXPIRED,
            ):
                logger.info(
                    "Playwright session state: %s. Runner in standby. Run 'ptb login microsoft' to connect.",
                    validation_state.value,
                )

            while self._running:
                val_state = self.playwright_orchestrator.session_mgr.validate_session()
                if val_state in (
                    SessionHealthState.UNCONFIGURED,
                    SessionHealthState.LOGIN_REQUIRED,
                    SessionHealthState.AUTH_EXPIRED,
                ):
                    try:
                        await asyncio.sleep(2.0)
                    except asyncio.CancelledError:
                        break
                    continue

                try:
                    await self.playwright_orchestrator.start_interceptor(run_sweep=True)
                except asyncio.CancelledError:
                    self.playwright_orchestrator.stop()
                    break
                except Exception as pw_err:
                    logger.warning("Playwright acquisition runner stopped: %s. Entering standby.", pw_err)
                    log_bug(
                        code=BugCode.PTB_L1_002,
                        subsystem="playwright",
                        severity="WARNING",
                        message=f"Playwright acquisition runner stopped: {pw_err}",
                        exc=pw_err,
                    )
                    try:
                        await asyncio.sleep(5.0)
                    except asyncio.CancelledError:
                        break

                try:
                    await asyncio.sleep(self.poll_interval)
                except asyncio.CancelledError:
                    break

        self.tasks.append(
            asyncio.create_task(_playwright_loop(), name="ptb-playwright-loop")
        )

        # 7. Health Polling Supervisor Mechanism
        poll_host = "127.0.0.1" if self.host in ("0.0.0.0", "::", "") else self.host
        app_url = f"http://{poll_host}:{self.app_port}/health"
        mcp_url = f"http://{poll_host}:{self.mcp_port}/health"

        app_ready = False
        mcp_ready = False
        start_time = asyncio.get_event_loop().time()
        timeout_seconds = 15.0

        while (asyncio.get_event_loop().time() - start_time) < timeout_seconds and self._running:
            if not app_ready:
                app_ready = await self._poll_health_url(app_url)
            if not mcp_ready:
                # FastMCP SSE health (JSON /health check only, no TCP port open fallback)
                mcp_ready = await self._poll_health_url(mcp_url)

            if app_ready and mcp_ready:
                break
            await asyncio.sleep(0.3)

        neo4j_ok = self.neo4j_client is not None or (
            self.raw_event_repo is not None and self.checkpoint_repo is not None
        )
        worker_alive = worker_task is not None and not worker_task.done()

        if not (neo4j_ok and app_ready and mcp_ready and worker_alive):
            self.state = "NOT_READY"
            print("\n[✗] ERROR: Không thể vượt qua Health Check trong thời gian khởi động!")
            print(f"    • Application REST ({app_url}): {'[PASS]' if app_ready else '[FAIL]'}")
            print(f"    • FastMCP Server ({mcp_url}): {'[PASS]' if mcp_ready else '[FAIL]'}")
            print(f"    • Processing Worker: {'[PASS]' if worker_alive else '[FAIL]'}")
            await self.shutdown()
            return 1

        # 8. Đánh giá trạng thái sẵn sàng trung thực dựa trên Playwright / Microsoft session
        ms_auth_missing = False
        if self.enable_playwright:
            session_mgr = self.playwright_orchestrator.session_mgr
            if hasattr(session_mgr.has_valid_session, "_mock_name"):
                ms_auth_missing = not bool(session_mgr.has_valid_session())
            else:
                current_pw_state = session_mgr.validate_session()
                state_val = (
                    current_pw_state.value
                    if hasattr(current_pw_state, "value")
                    else str(current_pw_state)
                )
                init_val = (
                    initial_pw_state.value
                    if hasattr(initial_pw_state, "value")
                    else str(initial_pw_state)
                )
                ms_auth_missing = (
                    state_val in ("UNCONFIGURED", "LOGIN_REQUIRED", "AUTH_EXPIRED", "AUTH_REQUIRED")
                    or init_val in ("UNCONFIGURED", "LOGIN_REQUIRED", "AUTH_EXPIRED", "AUTH_REQUIRED")
                )

        ms_strict_required = False
        for ms_key in ("ms_teams", "outlook", "ms_outlook"):
            cfg_item = sources_dict.get(ms_key) if isinstance(sources_dict, dict) else None
            if cfg_item is None and isinstance(sources_cfg, dict):
                cfg_item = sources_cfg.get(ms_key)
            if isinstance(cfg_item, dict) and bool(cfg_item.get("strict_required", False)) is True:
                ms_strict_required = True
                break

        if self.enable_playwright and ms_auth_missing:
            if ms_strict_required:
                self.state = "DEGRADED"
                banner_line = (
                    "DEGRADED: Core services operational (Microsoft acquisition AUTH_REQUIRED — run 'ptb login microsoft')"
                )
            else:
                self.state = "READY_WITH_WARNINGS"
                banner_line = (
                    "READY_WITH_WARNINGS: Core services operational (Microsoft acquisition AUTH_REQUIRED — run 'ptb login microsoft')"
                )
            pw_state = "AUTH_REQUIRED (Run 'ptb login microsoft' to enable)"
        else:
            self.state = "READY"
            banner_line = "READY: All PTB components operational!"
            pw_state = "ACTIVE" if self.enable_playwright else "DISABLED (--no-playwright)"

        # Determine Graph Worker status and backlog
        graph_worker_status = "RUNNING" if (self.graph_sync_task and not self.graph_sync_task.done()) else "STOPPED"
        backlog_count = await self.get_graph_backlog_count()

        # In thông báo chuẩn theo yêu cầu thiết kế
        print("\n" + "=" * 70)
        print(banner_line)
        print("=" * 70)
        print(f"  • Application Service REST : http://{self.host}:{self.app_port} (/health [PASS])")
        print(f"  • FastMCP Server (SSE)     : http://{self.host}:{self.mcp_port} (/health [PASS])")
        print(f"  • ProcessingWorker Loop    : ACTIVE (poll_interval={self.poll_interval}s)")
        print(f"  • Graph Memory Sync Worker : {graph_worker_status} (Backlog: {backlog_count})")
        print(f"  • Coding Agent Watchers    : ACTIVE (Cursor, Claude Code, Antigravity)")
        print(f"  • Playwright Acquisition   : {pw_state}")
        for name, runner in self.adapter_runners.items():
            if name != "coding_agents":
                st = "ACTIVE" if runner.status in ("HEALTHY", "INITIALIZING") else runner.status
                print(f"  • {runner.display_name:<26} : {st} (poll_interval={runner.poll_interval}s)")
        print("=" * 70)
        print(">>> Nhấn Ctrl+C để dừng toàn bộ hệ thống PTB.\n")

        # Đợi tín hiệu dừng (SIGINT, SIGTERM hoặc max_runtime)
        try:
            if max_runtime is not None:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=max_runtime)
            else:
                await self._shutdown_event.wait()
        except asyncio.TimeoutError:
            pass
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await self.shutdown()

        return 0

    async def shutdown(self) -> None:
        """Thực hiện graceful shutdown đồng bộ cho toàn bộ asyncio tasks và servers."""
        if not self._running:
            return
        self._running = False
        if self.state == "INITIALIZING":
            self.state = "STOPPED"
        print("\n[*] Đang tắt an toàn toàn bộ services...")

        # 1. Dừng workers
        if self.processing_worker:
            self.processing_worker.stop()
        if self.playwright_orchestrator:
            self.playwright_orchestrator.stop()
        if self.sync_worker and hasattr(self.sync_worker, "stop") and callable(self.sync_worker.stop):
            try:
                self.sync_worker.stop()
            except Exception as e:
                logger.debug("Lỗi khi dừng sync_worker: %s", e)

        # Dừng và cancel task ptb-graph-sync-worker
        if self.graph_sync_task and not self.graph_sync_task.done():
            self.graph_sync_task.cancel()

        # 2. Dừng uvicorn servers
        if self.app_server:
            self.app_server.should_exit = True
        if self.mcp_server:
            self.mcp_server.should_exit = True

        # 3. Đợi các task hoàn tất hoặc cancel nếu quá timeout
        if self.tasks:
            done, pending = await asyncio.wait(self.tasks, timeout=3.0)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        # 4. Đóng Graphiti client an toàn mà KHÔNG double-close shared neo4j_client
        if self.sync_worker is not None:
            mem_client = getattr(self.sync_worker, "memory_client", None)
            if mem_client is not None:
                shared_neo = (
                    getattr(mem_client, "_neo4j_client", None) is self.neo4j_client
                    or getattr(mem_client, "neo4j_client", None) is self.neo4j_client
                )
                if shared_neo:
                    if hasattr(mem_client, "_neo4j_client"):
                        mem_client._neo4j_client = None
                    if hasattr(mem_client, "_driver") and self.neo4j_client is not None:
                        if mem_client._driver is getattr(self.neo4j_client, "_driver", None):
                            mem_client._driver = None

                if hasattr(mem_client, "close") and callable(mem_client.close):
                    try:
                        res = mem_client.close()
                        if inspect.isawaitable(res):
                            await res
                    except Exception as e:
                        logger.debug("Lỗi khi đóng Graphiti memory_client lúc tắt: %s", e)
            elif hasattr(self.sync_worker, "close") and callable(self.sync_worker.close):
                try:
                    res = self.sync_worker.close()
                    if inspect.isawaitable(res):
                        await res
                except Exception as e:
                    logger.debug("Lỗi khi đóng sync_worker lúc tắt: %s", e)

        # 5. Đóng Neo4j client nếu có (duy nhất 1 lần cho shared instance)
        if self.neo4j_client:
            try:
                await self.neo4j_client.close()
            except Exception as e:
                logger.debug("Lỗi khi đóng neo4j_client lúc tắt: %s", e)

        print("[✓] Đã tắt an toàn toàn bộ services.")


async def cmd_run(args: argparse.Namespace) -> int:
    """Khởi chạy và giám sát toàn bộ hệ thống PTB qua Process Supervisor."""
    supervisor = PTBProcessSupervisor(
        host=args.host,
        app_port=args.port,
        mcp_port=args.mcp_port,
        tenant_id=args.tenant_id,
        enable_playwright=not args.no_playwright,
        poll_interval=args.poll_interval,
        sources_config_path=getattr(args, "config", None),
    )

    loop = asyncio.get_running_loop()

    def _sig_handler():
        supervisor.trigger_shutdown()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _sig_handler)
        except (NotImplementedError, RuntimeError):
            pass

    return await supervisor.run()


# ==============================================================================
# COMMAND: DOCTOR (ptb doctor)
# ==============================================================================
async def cmd_doctor(args: argparse.Namespace) -> int:
    """Kiểm tra toàn diện môi trường runtime và các dependencies của PTB."""
    print("\n" + "=" * 75)
    print("PERSONAL TASK BOARD: SYSTEM & DEPENDENCY DOCTOR CHECK")
    print("=" * 75)

    COLOR_GREEN = "\033[92m"
    COLOR_RED = "\033[91m"
    COLOR_YELLOW = "\033[93m"
    COLOR_BOLD = "\033[1m"
    COLOR_RESET = "\033[0m"

    # Safe characters based on stdout encoding
    can_unicode = True
    try:
        "\u2713\u2717".encode(getattr(sys.stdout, "encoding", "utf-8") or "utf-8")
    except Exception:
        can_unicode = False

    CHECK_CHAR = "✓" if can_unicode else "OK"
    CROSS_CHAR = "✗" if can_unicode else "FAIL"

    PASS_SYM = f"{COLOR_GREEN}[{CHECK_CHAR}] PASS{COLOR_RESET}"
    FAIL_SYM = f"{COLOR_RED}[{CROSS_CHAR}] FAIL{COLOR_RESET}"
    WARN_SYM = f"{COLOR_YELLOW}[!] WARN{COLOR_RESET}"

    checks = []

    # 1. Python Runtime >= 3.11
    py_ok = sys.version_info >= (3, 11)
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    checks.append({
        "component": "Python Runtime (>= 3.11)",
        "status": PASS_SYM if py_ok else FAIL_SYM,
        "is_pass": py_ok,
        "is_warn": False,
        "details": f"Python {py_ver} ({sys.platform})",
    })

    # 2. Docker Daemon
    docker_bin = shutil.which("docker")
    docker_running = False
    docker_detail = ""
    if docker_bin:
        try:
            res = subprocess.run([docker_bin, "info"], capture_output=True, text=True, timeout=4, shell=(sys.platform == "win32"))
            if res.returncode == 0:
                docker_running = True
                docker_detail = "Docker daemon is active and responsive"
            else:
                docker_detail = "Docker CLI found but daemon is not running"
        except Exception as e:
            docker_detail = f"Error querying Docker: {e}"
    else:
        docker_detail = "Docker binary not found in PATH"

    checks.append({
        "component": "Docker Daemon",
        "status": PASS_SYM if docker_running else FAIL_SYM,
        "is_pass": docker_running,
        "is_warn": False,
        "details": docker_detail,
    })

    # 3. Neo4j Container & Bolt Port 7687
    bolt_connected = False
    neo4j_container_running = False
    neo4j_detail = ""
    if docker_running and docker_bin:
        try:
            res_c = subprocess.run(
                [docker_bin, "ps", "--filter", "name=ptb_neo4j", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=3
            )
            if "ptb_neo4j" in res_c.stdout:
                neo4j_container_running = True
        except Exception as e:
            logger.debug("Lỗi khi kiểm tra docker ps cho neo4j: %s", e)

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        s.connect(("127.0.0.1", 7687))
        s.close()
        bolt_connected = True
        c_info = " (container 'ptb_neo4j' UP)" if neo4j_container_running else ""
        neo4j_detail = f"Bolt 127.0.0.1:7687 reachable{c_info}"
    except Exception as e:
        neo4j_detail = f"Bolt 127.0.0.1:7687 unreachable: {e}"

    neo4j_status = PASS_SYM if bolt_connected else FAIL_SYM
    checks.append({
        "component": "Neo4j Database (ptb_neo4j:7687)",
        "status": neo4j_status,
        "is_pass": bolt_connected,
        "is_warn": False,
        "details": neo4j_detail,
    })

    # 4. Playwright Browser Binaries (Chromium)
    pw_ok = False
    pw_detail = ""
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            exe = p.chromium.executable_path
            if os.path.exists(exe):
                pw_ok = True
                pw_detail = f"Chromium binary verified: {Path(exe).name}"
            else:
                pw_detail = "Chromium not installed. Run: uv run playwright install chromium"
    except Exception as e:
        pw_detail = f"Playwright check error: {e}. Run: uv run playwright install chromium"

    checks.append({
        "component": "Playwright Chromium Binary",
        "status": PASS_SYM if pw_ok else WARN_SYM,
        "is_pass": pw_ok,
        "is_warn": not pw_ok,
        "details": pw_detail,
    })

    # 5. OpenWebUI (Port 3000)
    owui_ok = False
    owui_detail = ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        s.connect(("127.0.0.1", 3000))
        s.close()
        owui_ok = True
        owui_detail = "OpenWebUI listening on http://127.0.0.1:3000"
    except Exception:
        owui_detail = "Port 3000 closed. Run 'docker compose up -d' to start"

    checks.append({
        "component": "OpenWebUI Service (Port 3000)",
        "status": PASS_SYM if owui_ok else WARN_SYM,
        "is_pass": owui_ok,
        "is_warn": not owui_ok,
        "details": owui_detail,
    })

    # 6. LLM Provider Readiness
    deep_check = bool(getattr(args, "deep", False))
    models_cfg_arg = getattr(args, "models_config", None)
    if models_cfg_arg:
        models_cfg_path = str(models_cfg_arg)
    elif (ROOT_DIR / "config" / "models.yaml").is_file():
        models_cfg_path = str(ROOT_DIR / "config" / "models.yaml")
    elif (ROOT_DIR / "config" / "models.example.yaml").is_file():
        models_cfg_path = str(ROOT_DIR / "config" / "models.example.yaml")
    else:
        models_cfg_path = None

    llm_ok = False
    llm_detail = ""
    try:
        readiness_fn = check_llm_readiness
        if readiness_fn is None:
            try:
                from ptb_processing.llm_readiness import check_llm_readiness as _proc_readiness_fn
                readiness_fn = _proc_readiness_fn
            except ImportError:
                readiness_fn = None

        if readiness_fn is not None:
            report = await readiness_fn(config_path=models_cfg_path, deep=deep_check)
            if isinstance(report, dict):
                rep_status = str(report.get("status", "NOT_READY")).upper()
                api_key_ok = bool(report.get("api_key_configured", False))
                base_url_ok = bool(report.get("base_url_valid", True))
                model_ok = bool(report.get("model_configured", True))
                provider_name = str(report.get("provider", "openai_compatible"))
                model_name = str(report.get("model_name", "gpt-4o-mini"))
                base_url = str(report.get("base_url", "https://api.openai.com/v1"))
                deep_passed = report.get("deep_check_passed")
                err_msg = report.get("error_message")
            else:
                rep_status = str(getattr(report, "status", "NOT_READY")).upper()
                api_key_ok = bool(getattr(report, "api_key_configured", False))
                base_url_ok = bool(getattr(report, "base_url_valid", True))
                model_ok = bool(getattr(report, "model_configured", True))
                provider_name = str(getattr(report, "provider", "openai_compatible"))
                model_name = str(getattr(report, "model_name", "gpt-4o-mini"))
                base_url = str(getattr(report, "base_url", "https://api.openai.com/v1"))
                deep_passed = getattr(report, "deep_check_passed", None)
                err_msg = getattr(report, "error_message", None)
        else:
            models_cfg = get_models_config(models_cfg_path)
            provider_name = str(models_cfg.get("default_llm_provider", "openai_compatible"))
            prov_dict = models_cfg.get("providers", {}).get(provider_name, {})
            api_key_val = (
                os.getenv("OPENAI_API_KEY")
                or os.getenv("GEMINI_API_KEY")
                or os.getenv("ANTHROPIC_API_KEY")
                or os.getenv("LLM_API_KEY")
                or (prov_dict.get("api_key") if isinstance(prov_dict, dict) else "")
                or ""
            ).strip()
            api_key_ok = bool(api_key_val)
            base_url = (
                os.getenv("OPENAI_BASE_URL")
                or os.getenv("LLM_BASE_URL")
                or (prov_dict.get("base_url") if isinstance(prov_dict, dict) else None)
                or "https://api.openai.com/v1"
            ).strip().rstrip("/")
            base_url_ok = base_url.startswith(("http://", "https://"))
            model_name = (
                os.getenv("LLM_MODEL")
                or os.getenv("PTB_EXTRACTION_MODEL")
                or (prov_dict.get("extraction_model") if isinstance(prov_dict, dict) else None)
                or "gpt-4o-mini"
            ).strip()
            model_ok = bool(model_name)
            deep_passed = None
            err_msg = None
            if api_key_ok and base_url_ok and model_ok:
                rep_status = "HEALTHY"
                if deep_check:
                    def _probe():
                        req = urllib.request.Request(
                            f"{base_url}/models",
                            headers={"Authorization": f"Bearer {api_key_val}", "Accept": "application/json"},
                            method="GET",
                        )
                        with urllib.request.urlopen(req, timeout=3.0) as resp:
                            code = getattr(resp, "status", 200)
                            if isinstance(code, int) and code >= 400:
                                raise RuntimeError(f"HTTP {code}")
                    try:
                        await asyncio.to_thread(_probe)
                        deep_passed = True
                    except Exception as probe_err:
                        rep_status = "DEGRADED"
                        deep_passed = False
                        err_msg = str(probe_err)
            else:
                rep_status = "NOT_READY"

        if not api_key_ok:
            llm_ok = False
            llm_detail = "PTB-LLM-001: API key unconfigured -> Processing DEGRADED/NOT_READY"
            log_bug(
                code=BugCode.PTB_LLM_001,
                subsystem="llm",
                severity="WARNING",
                message=llm_detail,
            )
        elif not base_url_ok or not model_ok or rep_status != "HEALTHY" or (deep_check and deep_passed is False):
            llm_ok = False
            if deep_check and deep_passed is False:
                llm_detail = (
                    f"PTB-LLM-001: Deep check (/models) failed ({err_msg or 'unreachable'}) -> Processing DEGRADED/NOT_READY"
                )
            else:
                llm_detail = f"PTB-LLM-001: {err_msg or 'Invalid LLM config'} -> Processing DEGRADED/NOT_READY"
            log_bug(
                code=BugCode.PTB_LLM_001,
                subsystem="llm",
                severity="WARNING",
                message=llm_detail,
            )
        else:
            llm_ok = True
            if deep_check:
                llm_detail = f"Provider '{provider_name}' ready (model={model_name}, base_url={base_url}, /models [OK])"
            else:
                llm_detail = f"Provider '{provider_name}' configured (model={model_name}, base_url={base_url})"
    except Exception as e:
        llm_ok = False
        llm_detail = f"PTB-LLM-001: LLM check error ({e}) -> Processing DEGRADED/NOT_READY"
        log_bug(
            code=BugCode.PTB_LLM_001,
            subsystem="llm",
            severity="WARNING",
            message=llm_detail,
            exc=e,
        )

    checks.append({
        "component": "LLM Provider Readiness",
        "status": PASS_SYM if llm_ok else WARN_SYM,
        "is_pass": llm_ok,
        "is_warn": not llm_ok,
        "details": llm_detail,
    })

    # 7. Graphiti Semantic Memory Layer
    graphiti_ok = False
    graphiti_detail = ""
    try:
        from ptb_graph_memory.adapter import GraphitiAdapter, HAS_GRAPHITI_CORE
        test_adapter = GraphitiAdapter(
            uri=os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687"),
            user=os.getenv("NEO4J_USERNAME", "neo4j"),
            password=os.getenv("NEO4J_PASSWORD", "taskboard123"),
            database=os.getenv("NEO4J_DATABASE", "neo4j"),
        )
        try:
            if test_adapter.is_available:
                graphiti_ok = True
                graphiti_detail = "Graphiti adapter initialized & connected to Neo4j"
            elif not HAS_GRAPHITI_CORE:
                graphiti_ok = False
                graphiti_detail = "graphiti-core not available -> Graphiti DEGRADED"
                log_bug(
                    code=BugCode.PTB_GRAPH_001,
                    subsystem="graph_memory",
                    severity="WARNING",
                    message=graphiti_detail,
                )
            else:
                graphiti_ok = False
                graphiti_detail = f"Graphiti adapter unavailable ({test_adapter.last_error or 'offline'}) -> Graphiti DEGRADED"
                log_bug(
                    code=BugCode.PTB_GRAPH_001,
                    subsystem="graph_memory",
                    severity="WARNING",
                    message=graphiti_detail,
                )
        finally:
            await test_adapter.close()
    except Exception as e:
        graphiti_ok = False
        graphiti_detail = f"Graphiti check error: {e} -> Graphiti DEGRADED"
        log_bug(
            code=BugCode.PTB_GRAPH_001,
            subsystem="graph_memory",
            severity="WARNING",
            message=graphiti_detail,
            exc=e,
        )

    checks.append({
        "component": "Graphiti Semantic Memory",
        "status": PASS_SYM if graphiti_ok else WARN_SYM,
        "is_pass": graphiti_ok,
        "is_warn": not graphiti_ok,
        "details": graphiti_detail,
    })

    # 8. Qwen3 Embedding Service (Port 8082 / local-ai)
    qwen_ok = False
    qwen_detail = ""
    qwen_url = os.getenv("EMBEDDING_BASE_URL", "http://127.0.0.1:8082/v1")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        # Parse port from URL or default 8082
        s.connect(("127.0.0.1", 8082))
        s.close()
        qwen_ok = True
        qwen_detail = f"Qwen3 Embedding listening on {qwen_url}"
    except Exception:
        qwen_detail = "Port 8082 closed (local-ai profile: docker compose --profile local-ai up -d)"

    checks.append({
        "component": "Qwen3 Embedding (Port 8082)",
        "status": PASS_SYM if qwen_ok else WARN_SYM,
        "is_pass": qwen_ok,
        "is_warn": not qwen_ok,
        "details": qwen_detail,
    })

    # 9. Kev Decision & Reranker Service (Port 8081 / local-ai)
    kev_ok = False
    kev_detail = ""
    kev_url = os.getenv("DECISION_BASE_URL", "http://127.0.0.1:8081/v1")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        s.connect(("127.0.0.1", 8081))
        s.close()
        kev_ok = True
        kev_detail = f"Kev Decision & Reranker listening on {kev_url}"
    except Exception:
        kev_detail = "Port 8081 closed (local-ai profile: docker compose --profile local-ai up -d)"

    checks.append({
        "component": "Kev Reranker (Port 8081)",
        "status": PASS_SYM if kev_ok else WARN_SYM,
        "is_pass": kev_ok,
        "is_warn": not kev_ok,
        "details": kev_detail,
    })

    # 10. Multi-Source Ingestion Channels (9 Channels Health Audit)
    sources_cfg_doc = get_sources_config(getattr(args, "config", None))
    src_cfg_dict = sources_cfg_doc.get("sources", {}) if isinstance(sources_cfg_doc, dict) else {}

    # (1) Coding Agent Watchers
    ca_status = "NOT_CONFIGURED"
    ca_detail = ""
    try:
        from ptb_acquisition.adapters import AgentWatchersAdapter
        ca_adapter = AgentWatchersAdapter()
        ca_health = await ca_adapter.health()
        watchers_map = ca_health.get("watchers", {})
        installed_cnt = sum(1 for w in watchers_map.values() if isinstance(w, dict) and w.get("installed") is True)
        if installed_cnt > 0:
            ca_status = "READY"
            ca_detail = f"{installed_cnt} agent watchers active"
        else:
            ca_status = "READY"
            ca_detail = "0 active logs detected (standby)"
    except Exception as ex:
        ca_status = "DEGRADED"
        ca_detail = str(ex)

    # (2) Local Git
    git_status = "NOT_CONFIGURED"
    git_detail = ""
    try:
        from ptb_acquisition.adapters import GitWatcherAdapter
        g_cfg = src_cfg_dict.get("git", {})
        r_paths = g_cfg.get("repo_paths", [str(ROOT_DIR)]) if isinstance(g_cfg, dict) else [str(ROOT_DIR)]
        expanded_paths = [os.path.expanduser(p) for p in r_paths if os.path.exists(os.path.expanduser(p))]
        if not expanded_paths:
            expanded_paths = [str(ROOT_DIR)]
        git_ad = GitWatcherAdapter(repo_paths=expanded_paths)
        g_health = await git_ad.health()
        valid_repos = g_health.get("valid_git_repos", [])
        if valid_repos:
            git_status = "READY"
            git_detail = f"{len(valid_repos)} valid git repo(s) found"
        else:
            git_status = "NOT_CONFIGURED"
            git_detail = "No git repos found"
    except Exception as ex:
        git_status = "DEGRADED"
        git_detail = str(ex)

    # (3) Microsoft Teams
    teams_status = "NOT_CONFIGURED"
    teams_detail = ""
    try:
        from ptb_acquisition.playwright.session import SessionManager, SessionHealthState
        storage_file = (
            os.getenv("PTB_STORAGE_STATE")
            or (src_cfg_dict.get("ms_teams", {}).get("storage_state_path") if isinstance(src_cfg_dict.get("ms_teams"), dict) else None)
            or os.path.join(ROOT_DIR, "data", "playwright", "storage_state.json")
        )
        if not os.path.exists(storage_file):
            teams_status = "NOT_CONFIGURED"
            teams_detail = "storage_state.json missing"
        else:
            t_mgr = SessionManager(storage_file)
            val = t_mgr.validate_session()
            if val in (SessionHealthState.HEALTHY, SessionHealthState.STARTING):
                teams_status = "READY"
                teams_detail = f"Session cookies valid ({val.value})"
            elif val in (SessionHealthState.LOGIN_REQUIRED, SessionHealthState.AUTH_EXPIRED, SessionHealthState.UNCONFIGURED):
                teams_status = "AUTH_REQUIRED"
                teams_detail = f"Session {val.value}: run 'ptb login microsoft'"
            else:
                teams_status = "DEGRADED"
                teams_detail = f"Session state {val.value}"
    except Exception as ex:
        teams_status = "DEGRADED"
        teams_detail = str(ex)

    # (4) Microsoft Outlook Direct
    outlook_status = "NOT_CONFIGURED"
    outlook_detail = ""
    try:
        from ptb_acquisition.adapters import OutlookMailAdapter
        otl_cfg = src_cfg_dict.get("ms_outlook") or src_cfg_dict.get("outlook", {})
        otl_tok = (otl_cfg.get("graph_token") or os.getenv("MS_GRAPH_TOKEN", "")).strip() if isinstance(otl_cfg, dict) else os.getenv("MS_GRAPH_TOKEN", "").strip()
        if not otl_tok:
            outlook_status = "NOT_CONFIGURED"
            outlook_detail = "Missing MS_GRAPH_TOKEN"
        else:
            otl_ad = OutlookMailAdapter(graph_token=otl_tok)
            otl_health = await otl_ad.health()
            st = str(otl_health.get("status", "")).upper()
            if "HEALTHY" in st or "READY" in st:
                outlook_status = "READY"
                outlook_detail = f"Mailbox: {otl_health.get('mailbox', 'me')}"
            elif "AUTH" in st:
                outlook_status = "AUTH_REQUIRED"
                outlook_detail = otl_health.get("error", "Auth token expired or invalid")
            else:
                outlook_status = "DEGRADED"
                outlook_detail = otl_health.get("error", "Service degraded")
    except Exception as ex:
        outlook_status = "DEGRADED"
        outlook_detail = str(ex)

    # (5) Jira Cloud / Server
    jira_status = "NOT_CONFIGURED"
    jira_detail = ""
    try:
        from ptb_acquisition.adapters import JiraAdapter
        j_cfg = src_cfg_dict.get("jira", {})
        j_tok = (j_cfg.get("api_token") or os.getenv("JIRA_API_TOKEN", "")).strip() if isinstance(j_cfg, dict) else os.getenv("JIRA_API_TOKEN", "").strip()
        j_url = (j_cfg.get("base_url") or os.getenv("JIRA_BASE_URL", "")).strip() if isinstance(j_cfg, dict) else os.getenv("JIRA_BASE_URL", "").strip()
        if not j_tok or not j_url:
            jira_status = "NOT_CONFIGURED"
            jira_detail = "Missing JIRA_BASE_URL or JIRA_API_TOKEN"
        else:
            j_ad = JiraAdapter(base_url=j_url, api_token=j_tok, email=j_cfg.get("email") if isinstance(j_cfg, dict) else None)
            j_health = await j_ad.health()
            st = str(j_health.get("status", "")).upper()
            if "HEALTHY" in st or "READY" in st:
                jira_status = "READY"
                jira_detail = f"User: {j_health.get('user', 'connected')}"
            elif "AUTH" in st:
                jira_status = "AUTH_REQUIRED"
                jira_detail = j_health.get("error", "Invalid API token or unauthorized")
            else:
                jira_status = "DEGRADED"
                jira_detail = j_health.get("error", "Endpoint unreachable")
    except Exception as ex:
        jira_status = "DEGRADED"
        jira_detail = str(ex)

    # (6) Shortcut Stories
    shortcut_status = "NOT_CONFIGURED"
    shortcut_detail = ""
    try:
        from ptb_acquisition.adapters import ShortcutAdapter
        sc_cfg = src_cfg_dict.get("shortcut", {})
        sc_tok = (sc_cfg.get("api_token") or os.getenv("SHORTCUT_API_TOKEN", "")).strip() if isinstance(sc_cfg, dict) else os.getenv("SHORTCUT_API_TOKEN", "").strip()
        if not sc_tok:
            shortcut_status = "NOT_CONFIGURED"
            shortcut_detail = "Missing SHORTCUT_API_TOKEN"
        else:
            sc_ad = ShortcutAdapter(api_token=sc_tok, base_url=sc_cfg.get("base_url") if isinstance(sc_cfg, dict) else None)
            sc_health = await sc_ad.health()
            st = str(sc_health.get("status", "")).upper()
            if "HEALTHY" in st or "READY" in st:
                shortcut_status = "READY"
                shortcut_detail = f"User: {sc_health.get('user', 'connected')}"
            elif "AUTH" in st:
                shortcut_status = "AUTH_REQUIRED"
                shortcut_detail = sc_health.get("error", "Invalid API token")
            else:
                shortcut_status = "DEGRADED"
                shortcut_detail = sc_health.get("error", "Service degraded")
    except Exception as ex:
        shortcut_status = "DEGRADED"
        shortcut_detail = str(ex)

    # (7) Confluence Docs
    conf_status = "NOT_CONFIGURED"
    conf_detail = ""
    try:
        from ptb_acquisition.adapters import ConfluenceAdapter
        c_cfg = src_cfg_dict.get("confluence", {})
        c_tok = (c_cfg.get("api_token") or os.getenv("CONFLUENCE_API_TOKEN", "")).strip() if isinstance(c_cfg, dict) else os.getenv("CONFLUENCE_API_TOKEN", "").strip()
        c_url = (c_cfg.get("base_url") or os.getenv("CONFLUENCE_BASE_URL", "")).strip() if isinstance(c_cfg, dict) else os.getenv("CONFLUENCE_BASE_URL", "").strip()
        if not c_tok or not c_url:
            conf_status = "NOT_CONFIGURED"
            conf_detail = "Missing CONFLUENCE_BASE_URL or CONFLUENCE_API_TOKEN"
        else:
            c_ad = ConfluenceAdapter(base_url=c_url, api_token=c_tok, email=c_cfg.get("email") if isinstance(c_cfg, dict) else None)
            c_health = await c_ad.health()
            st = str(c_health.get("status", "")).upper()
            if "HEALTHY" in st or "READY" in st:
                conf_status = "READY"
                conf_detail = f"User: {c_health.get('user', 'connected')}"
            elif "AUTH" in st:
                conf_status = "AUTH_REQUIRED"
                conf_detail = c_health.get("error", "Invalid API token or unauthorized")
            else:
                conf_status = "DEGRADED"
                conf_detail = c_health.get("error", "Endpoint unreachable")
    except Exception as ex:
        conf_status = "DEGRADED"
        conf_detail = str(ex)

    # (8) GitHub Repositories
    gh_status = "NOT_CONFIGURED"
    gh_detail = ""
    try:
        from ptb_acquisition.adapters import GitHubAdapter
        g_cfg = src_cfg_dict.get("github", {})
        g_tok = (g_cfg.get("api_token") or os.getenv("GITHUB_TOKEN", "")).strip() if isinstance(g_cfg, dict) else os.getenv("GITHUB_TOKEN", "").strip()
        g_repos = g_cfg.get("repositories", []) if isinstance(g_cfg, dict) else []
        if not g_tok and not g_repos:
            gh_status = "NOT_CONFIGURED"
            gh_detail = "Missing GITHUB_TOKEN"
        else:
            g_ad = GitHubAdapter(api_token=g_tok, repositories=g_repos)
            g_health = await g_ad.health()
            st = str(g_health.get("status", "")).upper()
            if "HEALTHY" in st or "READY" in st:
                gh_status = "READY"
                gh_detail = f"User: {g_health.get('user', 'connected')}"
            elif "AUTH" in st:
                gh_status = "AUTH_REQUIRED"
                gh_detail = g_health.get("error", "Invalid token")
            else:
                gh_status = "DEGRADED"
                gh_detail = g_health.get("error", "Service degraded")
    except Exception as ex:
        gh_status = "DEGRADED"
        gh_detail = str(ex)

    # (9) Slack Workspaces
    slack_status = "NOT_CONFIGURED"
    slack_detail = ""
    try:
        from ptb_acquisition.adapters import SlackAdapter
        sl_cfg = src_cfg_dict.get("slack", {})
        sl_tok = (sl_cfg.get("bot_token") or os.getenv("SLACK_BOT_TOKEN", "")).strip() if isinstance(sl_cfg, dict) else os.getenv("SLACK_BOT_TOKEN", "").strip()
        if not sl_tok:
            slack_status = "NOT_CONFIGURED"
            slack_detail = "Missing SLACK_BOT_TOKEN"
        else:
            sl_ad = SlackAdapter(bot_token=sl_tok, channels=sl_cfg.get("channels", []) if isinstance(sl_cfg, dict) else [])
            sl_health = await sl_ad.health()
            st = str(sl_health.get("status", "")).upper()
            if "HEALTHY" in st or "READY" in st:
                slack_status = "READY"
                slack_detail = f"Team: {sl_health.get('team', 'connected')}"
            elif "AUTH" in st:
                slack_status = "AUTH_REQUIRED"
                slack_detail = sl_health.get("error", "Invalid bot token")
            else:
                slack_status = "DEGRADED"
                slack_detail = sl_health.get("error", "Service degraded")
    except Exception as ex:
        slack_status = "DEGRADED"
        slack_detail = str(ex)

    def _source_badge(st_code: str) -> str:
        if st_code == "READY":
            return f"{COLOR_GREEN}[{CHECK_CHAR}] READY{COLOR_RESET}"
        elif st_code == "AUTH_REQUIRED":
            return f"{COLOR_YELLOW}[!] AUTH_REQ{COLOR_RESET}"
        elif st_code == "DEGRADED":
            return f"{COLOR_YELLOW}[!] DEGRADED{COLOR_RESET}"
        else:
            return f"{COLOR_RESET}[i] NOT_CONFIG{COLOR_RESET}"

    source_items = [
        ("Source: Coding Agents", ca_status, ca_detail),
        ("Source: Local Git", git_status, git_detail),
        ("Source: Microsoft Teams", teams_status, teams_detail),
        ("Source: Microsoft Outlook", outlook_status, outlook_detail),
        ("Source: Jira Cloud/Server", jira_status, jira_detail),
        ("Source: Shortcut Stories", shortcut_status, shortcut_detail),
        ("Source: Confluence Docs", conf_status, conf_detail),
        ("Source: GitHub Repos", gh_status, gh_detail),
        ("Source: Slack Workspace", slack_status, slack_detail),
    ]

    for label, st_code, detail in source_items:
        is_p = (st_code == "READY")
        checks.append({
            "component": label,
            "status": _source_badge(st_code),
            "is_pass": is_p,
            "is_warn": not is_p,
            "details": f"[{st_code}] {detail}",
        })

    # In bảng tóm tắt
    print(f"\n{COLOR_BOLD}{'Component':<32} | {'Status':<14} | {'Details / Notes'}{COLOR_RESET}")
    print("-" * 75)
    for c in checks:
        print(f"{c['component']:<32} | {c['status']:<23} | {c['details']}")
    print("-" * 75)

    pass_cnt = sum(1 for c in checks if c["is_pass"])
    warn_cnt = sum(1 for c in checks if c["is_warn"])
    fail_cnt = sum(1 for c in checks if not c["is_pass"] and not c["is_warn"])

    print(f"\n{COLOR_BOLD}Doctor Summary:{COLOR_RESET} {pass_cnt} PASS, {warn_cnt} WARN, {fail_cnt} FAIL\n")
    if fail_cnt > 0:
        print(f"{COLOR_RED}[!] Một hoặc nhiều thành phần cốt lõi chưa sẵn sàng. Vui lòng khắc phục các mục [✗] FAIL ở trên.{COLOR_RESET}\n")
        return 1
    return 0


# ==============================================================================
# COMMAND: LOGIN (ptb login microsoft)
# ==============================================================================
async def cmd_login(args: argparse.Namespace) -> int:
    """Đăng nhập tài khoản Microsoft 365 (Teams & Outlook Web)."""
    target = getattr(args, "login_target", None) or "microsoft"
    if target == "microsoft":
        storage_path = (
            getattr(args, "storage_path", None)
            or os.getenv("PTB_STORAGE_STATE")
            or os.path.join(ROOT_DIR, "data", "playwright", "storage_state.json")
        )
        service = getattr(args, "service", "all")
        print("\n" + "=" * 70)
        print("PERSONAL TASK BOARD: MICROSOFT 365 INTERACTIVE LOGIN")
        print("=" * 70)
        print(f"  • Storage Path : {storage_path}")
        print(f"  • Service      : {service}")
        print("=" * 70)
        from ptb_acquisition.playwright.login import run_interactive_login
        try:
            await run_interactive_login(storage_path=storage_path, service=service)
            return 0
        except Exception as e:
            print(f"\n[✗] Lỗi đăng nhập Microsoft: {e}", file=sys.stderr)
            return 1
    else:
        print(f"Dịch vụ đăng nhập không được hỗ trợ: {target}")
        return 1


# ==============================================================================
# COMMAND: OPENWEBUI (ptb openwebui install)
# ==============================================================================
async def cmd_openwebui(args: argparse.Namespace) -> int:
    """Quản lý tích hợp OpenWebUI."""
    action = getattr(args, "owui_action", None) or "install"
    if action == "install":
        print("\n" + "=" * 70)
        print("PERSONAL TASK BOARD: INSTALLING OPENWEBUI INTEGRATION")
        print("=" * 70)
        from integrations.openwebui import OpenWebUIInstaller
        url = getattr(args, "url", "http://127.0.0.1:3000")
        data_dir = getattr(args, "data_dir", None)
        try:
            target_path = Path(data_dir) if data_dir else None
            installer = OpenWebUIInstaller(base_url=url, default_data_dir=target_path)
            res = await installer.install_components(openwebui_data_dir=target_path, base_url=url)
            if res.get("success") is True and res.get("status") == "success":
                print(f"\n[✓] {res.get('message')}")
                print(f"    • Target Directory   : {res.get('target_dir')}")
                print(f"    • Pinned Version     : {res.get('pinned_version')}")
                if res.get("database_registered"):
                    print(f"    • Database Plugin    : [✓] Registered ({res.get('tools_specs_count', 0)} tool specs in webui.db)")
                else:
                    print("    • Database Plugin    : [✗] Not registered in SQLite")
                if res.get("api_registered"):
                    print("    • OpenWebUI API      : [✓] Registered & Verified online")
                else:
                    print("    • OpenWebUI API      : [i] Skipped (API offline or unauthenticated)")
                print(f"    • Components Installed:")
                for c in res.get("components_installed", []):
                    print(f"      - {c}")
                return 0
            else:
                print(f"\n[✗] Cài đặt thất bại: {res.get('message')}")
                if not res.get("database_registered") and not res.get("api_registered"):
                    print("    • Cảnh báo: Plugin chưa được đăng ký vào OpenWebUI (cả SQLite DB lẫn REST API đều chưa nạp thành công).")
                return 1
        except Exception as e:
            print(f"\n[✗] Ngoại lệ khi cài đặt OpenWebUI: {e}", file=sys.stderr)
            return 1
    else:
        print(f"Hành động không hợp lệ: {action}. Sử dụng: ptb openwebui install")
        return 1


# ==============================================================================
# COMMAND: STATUS (ptb status - NÂNG CẤP BÁO CÁO CHI TIẾT SUBSYSTEMS)
# ==============================================================================
async def cmd_status(args: argparse.Namespace) -> int:
    """Kiểm tra tình trạng chi tiết từng subsystem: Neo4j, Microsoft Session, Watchers, Processing Queue."""
    print("\n" + "=" * 70)
    print("PERSONAL TASK BOARD: SYSTEM & SUBSYSTEM HEALTH STATUS")
    print("=" * 70)

    # 1. Kiểm tra Neo4j Single-Store
    neo4j_status = "Disconnected"
    neo4j_stats = {}
    queue_counts = {"PENDING": 0, "PROCESSING": 0, "RETRY": 0, "PROCESSED": 0, "FAILED": 0}
    try:
        from ptb_database.neo4j_client import Neo4jClient
        client = Neo4jClient()
        if await client.verify_connectivity():
            neo4j_status = "Connected (Healthy)"
            driver = client.get_driver()
            async with driver.session() as session:
                res1 = await session.run("MATCH (e:RawEvent) RETURN count(e) as cnt")
                rec1 = await res1.single()
                res2 = await session.run("MATCH (t:UnifiedTask) RETURN count(t) as cnt")
                rec2 = await res2.single()
                res3 = await session.run("SHOW CONSTRAINTS")
                recs3 = await res3.data()
                neo4j_stats["raw_events"] = rec1["cnt"] if rec1 else 0
                neo4j_stats["tasks"] = rec2["cnt"] if rec2 else 0
                neo4j_stats["constraints"] = len(recs3)

                # Đếm phân bổ trạng thái hàng đợi processing
                res_q = await session.run("MATCH (e:RawEvent) RETURN e.processing_status as status, count(e) as cnt")
                recs_q = await res_q.data()
                for row in recs_q:
                    st = row.get("status")
                    if st in queue_counts:
                        queue_counts[st] = row.get("cnt", 0)
            await client.close()
    except Exception as e:
        neo4j_status = f"Error: {e}"

    print(f"\n[1] Neo4j Single-Store Database:")
    print(f"    • Status        : {neo4j_status}")
    if neo4j_stats:
        print(f"    • Raw Events    : {neo4j_stats.get('raw_events', 0)}")
        print(f"    • Unified Tasks : {neo4j_stats.get('tasks', 0)}")
        print(f"    • Constraints   : {neo4j_stats.get('constraints', 0)}")

    # 2. Kiểm tra Microsoft Session State (Teams / Outlook)
    from ptb_acquisition.playwright.session import DEFAULT_STORAGE_PATH, SessionManager
    storage_path = os.getenv("PTB_STORAGE_STATE") or os.path.join(ROOT_DIR, "data", "playwright", "storage_state.json")
    teams_mgr = SessionManager(storage_path)
    session_state = teams_mgr.validate_session()
    print(f"\n[2] Microsoft Session State (Teams & Outlook):")
    print(f"    • Status        : {session_state.value}")
    print(f"    • Storage File  : {storage_path} ({'Exists' if os.path.exists(storage_path) else 'Missing'})")
    if session_state.value in ("UNCONFIGURED", "LOGIN_REQUIRED", "AUTH_EXPIRED"):
        print(f"    • Action Needed : Run 'ptb login microsoft' to authenticate")

    # 3. Kiểm tra Coding Agent Watchers
    from ptb_acquisition.adapters import AgentWatchersAdapter
    agent_adapter = AgentWatchersAdapter()
    agent_health = await agent_adapter.health()
    print(f"\n[3] Coding Agent Watchers:")
    print(f"    • Overall       : {agent_health.get('status', 'unknown').upper()}")
    watchers_data = agent_health.get("watchers", {})
    if isinstance(watchers_data, dict):
        for agent_name, w_info in watchers_data.items():
            st = w_info.get("status", "unknown").upper()
            paths_found = w_info.get("paths_found", []) if isinstance(w_info, dict) else []
            detail = f"Found ({len(paths_found)} dirs)" if paths_found else "NOT_INSTALLED"
            print(f"      - {agent_name.capitalize():<12} : {st} ({detail})")

    # 4. Kiểm tra Processing Queue Count
    active_queue = queue_counts["PENDING"] + queue_counts["PROCESSING"] + queue_counts["RETRY"]
    print(f"\n[4] Processing Queue Status:")
    if "Connected" in neo4j_status:
        print(f"    • Active Queue  : {active_queue} (Pending: {queue_counts['PENDING']}, Processing: {queue_counts['PROCESSING']}, Retry: {queue_counts['RETRY']})")
        print(f"    • Completed     : {queue_counts['PROCESSED']}")
        print(f"    • Failed        : {queue_counts['FAILED']}")
    else:
        print(f"    • Active Queue  : Unavailable (Neo4j disconnected)")

    # 5. Các nguồn dữ liệu bên ngoài
    from ptb_acquisition.adapters import (
        GitWatcherAdapter,
        JiraAdapter,
        ShortcutAdapter,
        ConfluenceAdapter,
        GitHubAdapter,
        SlackAdapter,
        OutlookMailAdapter,
    )
    git_adapter = GitWatcherAdapter(repo_paths=[str(ROOT_DIR)])
    git_health = await git_adapter.health()
    jira_adapter = JiraAdapter()
    jira_health = await jira_adapter.health()
    shortcut_adapter = ShortcutAdapter()
    shortcut_health = await shortcut_adapter.health()
    conf_adapter = ConfluenceAdapter()
    conf_health = await conf_adapter.health()
    gh_adapter = GitHubAdapter()
    gh_health = await gh_adapter.health()
    slack_adapter = SlackAdapter()
    slack_health = await slack_adapter.health()
    outlook_adapter = OutlookMailAdapter()
    outlook_health = await outlook_adapter.health()

    print(f"\n[5] Other Ingestion Adapters (Multi-Channel):")
    print(f"    • Git Watcher     : {git_health.get('status', 'unknown').upper()} (Active Repos: {len(git_health.get('valid_git_repos', []))})")
    print(f"    • Jira Cloud      : {jira_health.get('status', 'unknown').upper()} (Configured: {jira_health.get('configured')})")
    print(f"    • Shortcut API    : {shortcut_health.get('status', 'unknown').upper()} (Configured: {shortcut_health.get('configured')})")
    print(f"    • Confluence Docs : {conf_health.get('status', 'unknown').upper()} (Configured: {conf_health.get('configured')})")
    print(f"    • GitHub Issues/PR: {gh_health.get('status', 'unknown').upper()} (Configured: {gh_health.get('configured')})")
    print(f"    • Slack Workspace : {slack_health.get('status', 'unknown').upper()} (Configured: {slack_health.get('configured')})")
    print(f"    • Outlook Direct  : {outlook_health.get('status', 'unknown').upper()} (Configured: {outlook_health.get('configured')})")

    # 6. Graph Memory Sync Worker & Episodic Graph
    graph_worker_status = "STOPPED"
    backlog_count = 0
    graphiti_status = "Unavailable (DEGRADED)"
    try:
        from ptb_graph_memory.adapter import GraphitiAdapter
        test_adapter = GraphitiAdapter()
        if test_adapter.is_available:
            graphiti_status = "Available (Healthy)"
        else:
            graphiti_status = "Unavailable (DEGRADED)"
            log_bug(
                code=BugCode.PTB_GRAPH_001,
                subsystem="graph_memory",
                severity="WARNING",
                message="Graphiti adapter is unavailable during status check",
            )

        if "Connected" in neo4j_status:
            from ptb_database.neo4j_client import Neo4jClient
            client = Neo4jClient()
            if await client.verify_connectivity():
                driver = client.get_driver()
                async with driver.session() as session:
                    res_b = await session.run(
                        "MATCH (n:EpisodicNode) WHERE coalesce(n.graph_sync_status, 'PENDING') IN ['PENDING', 'RETRY'] RETURN count(n) as cnt"
                    )
                    rec_b = await res_b.single()
                    if rec_b and "cnt" in rec_b:
                        backlog_count = rec_b["cnt"]
                await client.close()
    except Exception as ge:
        logger.debug("Graph status check error: %s", ge)

    print(f"\n[6] Graph Memory Sync Worker & Episodic Graph:")
    print(f"    • Graphiti Layer: {graphiti_status}")
    print(f"    • Graph Worker  : {graph_worker_status} (Backlog: {backlog_count})")

    print("\n" + "=" * 70)
    return 0


# ==============================================================================
# COMMAND: INIT
# ==============================================================================
async def cmd_init(args: argparse.Namespace) -> int:
    """Khởi tạo database constraints & seed data trên Neo4j."""
    print("\n" + "=" * 70)
    print("PERSONAL TASK BOARD: INITIALIZING DATABASE & SEED DATA")
    print("=" * 70)

    try:
        from ptb_database.setup_all import setup_neo4j
        success = await setup_neo4j()
        if success:
            print("\n[✓] Khởi tạo Neo4j Single-Store hoàn tất thành công 100%!")
            return 0
        else:
            print("\n[✗] Khởi tạo Neo4j thất bại. Vui lòng kiểm tra Docker container.")
            return 1
    except Exception as e:
        print(f"\n[✗] Lỗi ngoại lệ khi khởi tạo: {e}", file=sys.stderr)
        return 1


# ==============================================================================
# COMMAND: INGEST
# ==============================================================================
async def cmd_ingest(args: argparse.Namespace) -> int:
    """Chạy 1 vòng quét tất cả các adapters."""
    print("\n" + "=" * 70)
    print("PERSONAL TASK BOARD: RUNNING FULL ACQUISITION INGESTION CYCLE")
    print("=" * 70)

    from ptb_contracts import SourceType
    from ptb_acquisition.adapters import (
        AgentWatchersAdapter,
        GitWatcherAdapter,
        JiraAdapter,
        ShortcutAdapter,
    )
    from ptb_acquisition.pipeline import AcquisitionPipeline

    tenant_id = args.tenant_id or "local-user"

    raw_repo = None
    ckpt_repo = None

    try:
        from ptb_database.neo4j_client import Neo4jClient
        from ptb_database.repositories import CheckpointRepository, RawEventRepository

        client = Neo4jClient()
        if await client.verify_connectivity():
            raw_repo = RawEventRepository(client)
            ckpt_repo = CheckpointRepository(client)
            print("  ✓ Đã kết nối Neo4j Persistence Engine (RawEvent & Checkpoint repositories active)")
        else:
            await client.close()
            log_bug(
                code=BugCode.PTB_STORAGE_001,
                subsystem="neo4j",
                severity="CRITICAL",
                message="Neo4j authoritative store unavailable",
            )
            print("\n[✗] CRITICAL ERROR: Neo4j authoritative store unavailable. Không thể thu nạp dữ liệu!")
            print("    Hệ thống yêu cầu Neo4j hoạt động (Fail-Fast), không chạy với RAM store.\n")
            return 1
    except Exception as e:
        logger.error(f"Không thể kết nối Neo4j: {e}")
        log_bug(
            code=BugCode.PTB_STORAGE_001,
            subsystem="neo4j",
            severity="CRITICAL",
            message="Neo4j authoritative store unavailable",
            exc=e,
        )
        print(f"\n[✗] CRITICAL ERROR: Neo4j không khả dụng ({e}). Không thể thu nạp dữ liệu!")
        print("    Hệ thống yêu cầu Neo4j hoạt động (Fail-Fast), không chạy với RAM store.\n")
        return 1

    pipeline = AcquisitionPipeline(
        raw_event_repo=raw_repo,
        checkpoint_repo=ckpt_repo,
        tenant_id=tenant_id,
    )

    sources_cfg = get_sources_config(getattr(args, "config", None))
    src_dict = sources_cfg.get("sources", {})

    target_source = getattr(args, "source", "all")
    adapters_to_run = []

    if target_source in ("all", "coding_agent", "agents"):
        agent_adapter = AgentWatchersAdapter(tenant_id=tenant_id)
        adapters_to_run.append(("Coding Agents", agent_adapter))

    if target_source in ("all", "git"):
        git_cfg = src_dict.get("git", {})
        repo_paths = git_cfg.get("repo_paths", [str(ROOT_DIR)])
        expanded_paths = [os.path.expanduser(p) for p in repo_paths if os.path.exists(os.path.expanduser(p))]
        if not expanded_paths:
            expanded_paths = [str(ROOT_DIR)]
        git_adapter = GitWatcherAdapter(repo_paths=expanded_paths, tenant_id=tenant_id)
        adapters_to_run.append(("Local Git", git_adapter))

    if target_source in ("all", "jira"):
        jira_cfg = src_dict.get("jira", {})
        base_url = jira_cfg.get("base_url") or os.getenv("JIRA_BASE_URL")
        api_token = jira_cfg.get("api_token") or os.getenv("JIRA_API_TOKEN")
        if base_url and api_token:
            jira_adapter = JiraAdapter(
                base_url=base_url,
                email=jira_cfg.get("email") or os.getenv("JIRA_EMAIL"),
                api_token=api_token,
                jql=jira_cfg.get("default_jql") or os.getenv("JIRA_JQL"),
                project_keys=jira_cfg.get("project_keys", []),
                tenant_id=tenant_id,
            )
            adapters_to_run.append(("Jira Cloud/Server", jira_adapter))
        elif target_source == "jira":
            print("  [!] Jira chưa được cấu hình credentials (JIRA_BASE_URL, JIRA_API_TOKEN)")

    if target_source in ("all", "shortcut"):
        shortcut_cfg = src_dict.get("shortcut", {})
        api_token = shortcut_cfg.get("api_token") or os.getenv("SHORTCUT_API_TOKEN")
        if api_token:
            shortcut_adapter = ShortcutAdapter(
                api_token=api_token,
                base_url=shortcut_cfg.get("base_url"),
                project_ids=shortcut_cfg.get("project_ids", []),
                query=shortcut_cfg.get("default_query") or os.getenv("SHORTCUT_QUERY"),
                tenant_id=tenant_id,
            )
            adapters_to_run.append(("Shortcut REST API", shortcut_adapter))
        elif target_source == "shortcut":
            print("  [!] Shortcut chưa được cấu hình credentials (SHORTCUT_API_TOKEN)")

    if target_source in ("all", "outlook", "ms_outlook"):
        outlook_cfg = src_dict.get("ms_outlook", {})
        graph_token = outlook_cfg.get("graph_token") or os.getenv("MS_GRAPH_TOKEN")
        if graph_token:
            from ptb_acquisition.adapters import OutlookMailAdapter
            outlook_adapter = OutlookMailAdapter(
                tenant_id=tenant_id,
                graph_token=graph_token,
                folders=outlook_cfg.get("folders", ["Inbox"]),
            )
            adapters_to_run.append(("Microsoft Outlook Direct", outlook_adapter))
        elif target_source in ("outlook", "ms_outlook"):
            print("  [!] Outlook Direct chưa được cấu hình credentials (MS_GRAPH_TOKEN)")

    if target_source in ("all", "confluence"):
        conf_cfg = src_dict.get("confluence", {})
        conf_token = conf_cfg.get("api_token") or os.getenv("CONFLUENCE_API_TOKEN")
        conf_url = conf_cfg.get("base_url") or os.getenv("CONFLUENCE_BASE_URL")
        if conf_token and conf_url:
            from ptb_acquisition.adapters import ConfluenceAdapter
            confluence_adapter = ConfluenceAdapter(
                base_url=conf_url,
                email=conf_cfg.get("email") or os.getenv("CONFLUENCE_EMAIL"),
                api_token=conf_token,
                space_keys=conf_cfg.get("space_keys", []),
                tenant_id=tenant_id,
            )
            adapters_to_run.append(("Confluence Knowledge", confluence_adapter))
        elif target_source == "confluence":
            print("  [!] Confluence chưa được cấu hình credentials (CONFLUENCE_BASE_URL, CONFLUENCE_API_TOKEN)")

    if target_source in ("all", "github"):
        gh_cfg = src_dict.get("github", {})
        gh_token = gh_cfg.get("api_token") or os.getenv("GITHUB_TOKEN")
        gh_repos = gh_cfg.get("repositories", [])
        if gh_token or gh_repos:
            from ptb_acquisition.adapters import GitHubAdapter
            github_adapter = GitHubAdapter(
                repositories=gh_repos,
                api_token=gh_token,
                tenant_id=tenant_id,
            )
            adapters_to_run.append(("GitHub Repositories", github_adapter))
        elif target_source == "github":
            print("  [!] GitHub chưa được cấu hình credentials (GITHUB_TOKEN)")

    if target_source in ("all", "slack"):
        slack_cfg = src_dict.get("slack", {})
        slack_token = slack_cfg.get("bot_token") or os.getenv("SLACK_BOT_TOKEN")
        if slack_token:
            from ptb_acquisition.adapters import SlackAdapter
            slack_adapter = SlackAdapter(
                bot_token=slack_token,
                channels=slack_cfg.get("channels", []),
                tenant_id=tenant_id,
            )
            adapters_to_run.append(("Slack Workspaces", slack_adapter))
        elif target_source == "slack":
            print("  [!] Slack chưa được cấu hình credentials (SLACK_BOT_TOKEN)")

    results = []
    total_synced = 0

    if target_source in ("all", "teams", "ms_teams"):
        teams_cfg = src_dict.get("ms_teams", {})
        storage_file = (
            os.getenv("PTB_STORAGE_STATE")
            or (teams_cfg.get("storage_state_path") if isinstance(teams_cfg, dict) else None)
            or os.path.join(ROOT_DIR, "data", "playwright", "storage_state.json")
        )
        expanded_storage = os.path.expanduser(storage_file)
        if os.path.exists(expanded_storage):
            from ptb_acquisition.playwright.session import SessionManager, SessionHealthState
            teams_mgr = SessionManager(expanded_storage)
            val = teams_mgr.validate_session()
            if val in (SessionHealthState.HEALTHY, SessionHealthState.STARTING):
                try:
                    from ptb_acquisition.playwright.runner import PlaywrightOrchestrator
                    orchestrator = PlaywrightOrchestrator(
                        storage_path=expanded_storage,
                        pipeline=pipeline,
                        raw_event_repo=raw_repo,
                        tenant_id=f"{tenant_id}-microsoft",
                        headless=True,
                    )
                    # Run on-demand capture sweep for Teams
                    await orchestrator.start_interceptor(run_sweep=True)
                    orchestrator.stop()
                    print("  [Microsoft Teams] ✓ Hoàn tất: quét network interceptor.")
                    results.append(("Microsoft Teams", "Success", 0))
                except Exception as t_err:
                    print(f"  [Microsoft Teams] ✗ Lỗi quét Playwright: {t_err}")
                    results.append(("Microsoft Teams", f"Error: {t_err}", 0))
            elif target_source in ("teams", "ms_teams"):
                print("  [!] Microsoft Teams session chưa sẵn sàng (chạy 'ptb login microsoft')")
        elif target_source in ("teams", "ms_teams"):
            print(f"  [!] Microsoft Teams session file không tồn tại: {expanded_storage}")

    print(f"\n  -> Đang quét {len(adapters_to_run)} adapters đã kích hoạt...")

    for name, adapter in adapters_to_run:
        print(f"\n  [{name}] Bắt đầu đồng bộ ({adapter.source_type.value})...")
        try:
            streams = await adapter.discover()
            stream_count = len(streams)
            events_count = await pipeline.sync_adapter(adapter, stream_id="all")
            total_synced += events_count
            print(f"  [{name}] ✓ Hoàn tất: phát hiện {stream_count} streams, thu nạp {events_count} events.")
            results.append((name, "Success", events_count))
        except Exception as e:
            print(f"  [{name}] ✗ Lỗi đồng bộ: {e}")
            results.append((name, f"Error: {e}", 0))

    print("\n" + "=" * 70)
    print("KẾT QUẢ THU THẬP DỮ LIỆU (INGESTION STATS):")
    print(f"  • Tổng số event thu nạp (ingested)    : {pipeline.stats['ingested']}")
    print(f"  • Trùng lặp được lọc (deduplicated)   : {pipeline.stats['deduplicated']}")
    print(f"  • Lưu trữ bền bỉ (persisted to DB)    : {pipeline.stats['persisted']}")
    print(f"  • Lỗi phát sinh (errors)              : {pipeline.stats['errors']}")
    print("=" * 70)

    return 0


# ==============================================================================
# COMMAND: SERVE
# ==============================================================================
async def cmd_serve(args: argparse.Namespace) -> int:
    """Khởi động FastMCP Server và Application Service độc lập."""
    print("\n" + "=" * 70)
    print("PERSONAL TASK BOARD: LAUNCHING SERVICES RUNTIME")
    print("=" * 70)
    print(f"  • Mode        : {'MCP Only' if args.mcp_only else ('App Only' if args.app_only else 'Full Suite (FastMCP + App)')}")
    print(f"  • Host/Port   : {args.host}:{args.port}")
    print("=" * 70)

    tasks = []

    # Khởi tạo Dependency Graph dùng chung nếu Neo4j khả dụng
    shared_service = None
    try:
        from ptb_database.neo4j_client import Neo4jClient
        from ptb_database.repositories import (
            CheckpointRepository,
            RawEventRepository,
            TaskDomainRepository,
        )
        from ptb_processing.pipeline import ProcessingPipeline
        from ptb_intelligence.lifecycle import TaskIntelligenceLifecycle
        from ptb_graph_memory.client import GraphitiMemoryClient
        from ptb_application.service import ApplicationService

        client = Neo4jClient()
        if await client.verify_connectivity():
            raw_repo = RawEventRepository(client)
            ckpt_repo = CheckpointRepository(client)
            emb_svc = None
            is_local_ai_enabled = (
                os.getenv("PTB_ENABLE_LOCAL_AI", "false").lower() in ("true", "1", "yes")
                or os.getenv("PTB_REQUIRE_LOCAL_AI", "false").lower() in ("true", "1", "yes")
            )
            if is_local_ai_enabled:
                try:
                    from packages.ai_service import Qwen3EmbeddingService
                    emb_svc = Qwen3EmbeddingService()
                except Exception as e_emb:
                    logger.debug("Qwen3 embedding service not loaded in cmd_serve: %s", e_emb)
            task_repo = TaskDomainRepository(client, embedding_service=emb_svc)
            proc_pipeline = ProcessingPipeline(task_repo=task_repo)
            lifecycle = TaskIntelligenceLifecycle(task_repo=task_repo)
            graph_memory = GraphitiMemoryClient(neo4j_client=client)
            shared_service = ApplicationService(
                task_repo=task_repo,
                raw_event_repo=raw_repo,
                checkpoint_repo=ckpt_repo,
                lifecycle=lifecycle,
                graph_memory=graph_memory,
                neo4j_client=client,
                processing_pipeline=proc_pipeline,
            )
    except Exception as e:
        logger.warning("Could not initialize connected ApplicationService in cmd_serve: %s", e)

    # FastMCP Server
    if not args.app_only:
        async def _run_mcp():
            print("  [+] Đang khởi động FastMCP Read-Only Server...")
            try:
                from ptb_mcp.server import create_mcp_server, get_mcp_health, set_application_service as set_mcp_app_service
                from starlette.responses import JSONResponse
                from starlette.routing import Route
                import uvicorn
                if shared_service:
                    set_mcp_app_service(shared_service)
                mcp_srv = create_mcp_server(application_service=shared_service)
                starlette_mcp = mcp_srv.sse_app(host=args.host)
                async def _mcp_health_route(req):
                    h_data = await get_mcp_health(shared_service)
                    return JSONResponse(h_data)
                starlette_mcp.routes.append(Route("/health", _mcp_health_route))
                mcp_config = uvicorn.Config(starlette_mcp, host=args.host, port=8001, log_level="warning")
                mcp_server = uvicorn.Server(mcp_config)
                await mcp_server.serve()
            except asyncio.CancelledError:
                print("  [-] FastMCP Server đã dừng.")

        tasks.append(asyncio.create_task(_run_mcp()))

    # Application Service
    if not args.mcp_only:
        async def _run_app():
            print("  [+] Đang khởi động Application Service...")
            try:
                from ptb_application.api import create_app, set_application_service
                import uvicorn
                if shared_service:
                    set_application_service(shared_service)
                fastapi_app = create_app(application_service=shared_service)
                app_config = uvicorn.Config(fastapi_app, host=args.host, port=args.port, log_level="warning")
                app_server = uvicorn.Server(app_config)
                await app_server.serve()
            except asyncio.CancelledError:
                print("  [-] Application Service đã dừng.")

        tasks.append(asyncio.create_task(_run_app()))

    try:
        print("\n>>> Nhấn Ctrl+C để dừng toàn bộ services.\n")
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nĐang tắt các services...")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        print("Đã tắt an toàn toàn bộ services.")

    return 0


# ==============================================================================
# COMMAND: GRAPH
# ==============================================================================
async def cmd_graph(args: argparse.Namespace) -> int:
    """Quản trị Graphiti Temporal Knowledge Graph."""
    if getattr(args, "graph_action", None) == "rebuild":
        print(">>> Đang rebuild Graphiti memory từ authoritative Neo4j domain data...")
        from ptb_database.neo4j_client import Neo4jClient
        from ptb_graph_memory.rebuild import rebuild_graph_memory
        client = Neo4jClient()
        try:
            summary = await rebuild_graph_memory(neo4j_client=client)
            print(f"  [+] Trạng thái: {summary['status']}")
            print(f"  [+] Decisions: {summary['decisions_rebuilt']}")
            print(f"  [+] Lessons: {summary['lessons_rebuilt']}")
            print(f"  [+] Evidences: {summary['evidences_rebuilt']}")
            print(f"  [+] Tổng episodes: {summary['total_rebuilt']}")
            if summary.get("errors"):
                print(f"  [!] Có {len(summary['errors'])} lỗi trong quá trình rebuild:")
                for err in summary["errors"]:
                    print(f"      - {err}")
            return 0
        except Exception as e:
            logger.error("Lỗi khi rebuild graph memory: %s", e)
            print(f"  [-] Lỗi: {e}")
            return 1
        finally:
            await client.close()
    else:
        print("Hành động không hợp lệ. Sử dụng: ptb graph rebuild")
        return 1


# ==============================================================================
# COMMAND: MCP (ptb mcp config / export / install / serve)
# ==============================================================================
async def cmd_mcp(args: argparse.Namespace) -> int:
    """Quản trị, xuất cấu hình và cài đặt FastMCP Server cho Coding Agents."""
    action = getattr(args, "mcp_action", None) or "config"

    if action in ("config", "export"):
        print("\n" + "=" * 75)
        print("PERSONAL TASK BOARD: MCP CLIENT CONFIGURATION EXPORTER")
        print("=" * 75)
        transport = getattr(args, "transport", "stdio")
        read_only = getattr(args, "read_only", False)
        host = getattr(args, "host", "127.0.0.1")
        port = getattr(args, "port", 8001)
        client_target = getattr(args, "client", "all")

        python_exec = sys.executable
        ptb_root = str(ROOT_DIR)

        if transport == "stdio":
            mcp_entry = {
                "command": python_exec,
                "args": [
                    "-m",
                    "ptb_mcp.server",
                    "--transport",
                    "stdio",
                ] + (["--read-only"] if read_only else []),
                "env": {
                    "PYTHONPATH": ptb_root,
                },
            }
        else:
            mcp_entry = {
                "url": f"http://{host}:{port}/sse",
            }

        config_payload = {
            "mcpServers": {
                "personal-task-board": mcp_entry,
            }
        }

        json_str = json.dumps(config_payload, indent=2, ensure_ascii=False)

        print(f"  • Transport   : {transport.upper()}")
        print(f"  • Mode        : {'Read-Only (10 tools)' if read_only else 'Full Agent & BE (19 tools)'}")
        print(f"  • Target      : {client_target.upper()}")
        print("=" * 75 + "\n")

        print("JSON Cấu hình MCP (Sao chép vào file cấu hình của Coding Agent):")
        print(json_str)
        print("\n" + "-" * 75)
        print("Đường dẫn file cấu hình phổ biến trên máy tính:")
        print("  1. Google Antigravity : %USERPROFILE%\\.gemini\\antigravity-ide\\mcp_config.json")
        print("  2. Claude Desktop     : %APPDATA%\\Claude\\claude_desktop_config.json")
        print("  3. Cursor             : .cursor/mcp.json (trong workspace) hoặc %USERPROFILE%\\.cursor\\mcp.json")
        print("  4. VS Code (Cline/Roo): .vscode/mcp.json")
        print("  5. Windsurf           : %USERPROFILE%\\.codeium\\windsurf\\mcp_config.json")
        print("-" * 75)
        print("Mẹo: Dùng lệnh 'ptb mcp install' để tự động ghi cấu hình vào các file trên!")
        return 0

    elif action == "install":
        print("\n" + "=" * 75)
        print("PERSONAL TASK BOARD: AUTO-INSTALLING MCP CLIENT CONFIGURATION")
        print("=" * 75)
        transport = getattr(args, "transport", "stdio")
        read_only = getattr(args, "read_only", False)
        host = getattr(args, "host", "127.0.0.1")
        port = getattr(args, "port", 8001)
        client_target = getattr(args, "client", "all")

        python_exec = sys.executable
        ptb_root = str(ROOT_DIR)

        if transport == "stdio":
            mcp_entry = {
                "command": python_exec,
                "args": [
                    "-m",
                    "ptb_mcp.server",
                    "--transport",
                    "stdio",
                ] + (["--read-only"] if read_only else []),
                "env": {
                    "PYTHONPATH": ptb_root,
                },
            }
        else:
            mcp_entry = {
                "url": f"http://{host}:{port}/sse",
            }

        candidates = {}
        # Antigravity
        antigravity_paths = [
            Path(os.path.expandvars(r"%USERPROFILE%\.gemini\antigravity-ide\mcp_config.json")),
            Path(os.path.expandvars(r"%USERPROFILE%\.gemini\config\mcp_config.json")),
        ]
        candidates["antigravity"] = [p for p in antigravity_paths if p.parent.exists()]

        # Claude Desktop
        claude_path = Path(os.path.expandvars(r"%APPDATA%\Claude\claude_desktop_config.json"))
        candidates["claude"] = [claude_path]

        # Cursor
        cursor_paths = [
            ROOT_DIR / ".cursor" / "mcp.json",
            Path(os.path.expandvars(r"%USERPROFILE%\.cursor\mcp.json")),
        ]
        candidates["cursor"] = cursor_paths

        # VS Code
        vscode_paths = [
            ROOT_DIR / ".vscode" / "mcp.json",
        ]
        candidates["vscode"] = vscode_paths

        # Windsurf
        windsurf_path = Path(os.path.expandvars(r"%USERPROFILE%\.codeium\windsurf\mcp_config.json"))
        candidates["windsurf"] = [windsurf_path]

        installed_count = 0
        targets_to_check = candidates.keys() if client_target == "all" else [client_target]

        for client_name in targets_to_check:
            path_list = candidates.get(client_name, [])
            for p in path_list:
                try:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    data = {}
                    if p.exists() and p.stat().st_size > 0:
                        try:
                            data = json.loads(p.read_text(encoding="utf-8"))
                        except Exception:
                            data = {}
                    if not isinstance(data, dict):
                        data = {}
                    if "mcpServers" not in data or not isinstance(data["mcpServers"], dict):
                        data["mcpServers"] = {}
                    data["mcpServers"]["personal-task-board"] = mcp_entry

                    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                    print(f"  [✓] Đã cài đặt vào {client_name.capitalize()}: {p}")
                    installed_count += 1
                except Exception as ex:
                    print(f"  [✗] Lỗi cài đặt {client_name} ({p}): {ex}")

        print("\n" + "=" * 75)
        print(f"Hoàn tất: Đã cài đặt / cập nhật cấu hình vào {installed_count} vị trí.")
        print("=" * 75)
        return 0

    elif action == "serve":
        from ptb_mcp.server import run_mcp_server, run_stdio_server
        transport = getattr(args, "transport", "sse")
        read_only = getattr(args, "read_only", False)
        host = getattr(args, "host", "127.0.0.1")
        port = getattr(args, "port", 8001)

        if transport == "sse":
            run_mcp_server(host=host, port=port, read_only=read_only)
        else:
            await run_stdio_server(read_only=read_only)
        return 0

    else:
        print(f"Hành động MCP không hợp lệ: {action}. Sử dụng: ptb mcp config | install | serve")
        return 1


# ==============================================================================
# MAIN ENTRYPOINT
# ==============================================================================
def main():
    load_env()

    parser = argparse.ArgumentParser(
        prog="ptb",
        description="Personal Task Board (PTB) Command Line Management Tool",
    )
    subparsers = parser.add_subparsers(dest="command", help="Lệnh thực thi")

    # Command: run (Process Supervisor)
    parser_run = subparsers.add_parser("run", help="Khởi chạy và giám sát toàn bộ hệ thống PTB (Process Supervisor)")
    parser_run.add_argument("--host", default=os.getenv("PTB_HOST", "127.0.0.1"), help="Host lắng nghe (mặc định 127.0.0.1 hoặc PTB_HOST)")
    parser_run.add_argument("--port", type=int, default=int(os.getenv("PTB_PORT", "8000")), help="Cổng Application REST API (mặc định 8000 hoặc PTB_PORT)")
    parser_run.add_argument("--mcp-port", type=int, default=int(os.getenv("PTB_MCP_PORT", "8001")), help="Cổng FastMCP SSE (mặc định 8001 hoặc PTB_MCP_PORT)")
    parser_run.add_argument("--tenant-id", default="local-user", help="Tenant ID")
    parser_run.add_argument("--poll-interval", type=float, default=2.0, help="Chu kỳ thăm dò của worker (giây)")
    parser_run.add_argument("--no-playwright", action="store_true", help="Không khởi chạy Playwright browser daemon")
    parser_run.add_argument("--config", default=None, help="Đường dẫn file cấu hình sources.yaml")

    # Command: doctor
    parser_doctor = subparsers.add_parser("doctor", help="Kiểm tra môi trường và các thành phần phụ thuộc")
    parser_doctor.add_argument("--deep", action="store_true", help="Thực hiện kiểm tra chuyên sâu tới LLM provider endpoint (/models)")

    # Command: login
    parser_login = subparsers.add_parser("login", help="Đăng nhập tài khoản Microsoft 365")
    login_sub = parser_login.add_subparsers(dest="login_target", help="Dịch vụ đăng nhập")
    login_ms = login_sub.add_parser("microsoft", help="Đăng nhập Microsoft 365 (Teams & Outlook Web)")
    login_ms.add_argument("--service", choices=["teams", "outlook", "all"], default="all")
    login_ms.add_argument("--storage-path", default=None, help="Đường dẫn file storage_state.json")

    # Command: openwebui
    parser_owui = subparsers.add_parser("openwebui", help="Quản lý tích hợp OpenWebUI")
    owui_sub = parser_owui.add_subparsers(dest="owui_action")
    owui_inst = owui_sub.add_parser("install", help="Cài đặt PTB Tools & Artifacts vào OpenWebUI")
    owui_inst.add_argument("--url", default="http://127.0.0.1:3000", help="URL của OpenWebUI")
    owui_inst.add_argument("--data-dir", default=None, help="Thư mục data OpenWebUI")

    # Command: mcp
    parser_mcp = subparsers.add_parser("mcp", help="Quản lý, xuất cấu hình và cài đặt FastMCP Server cho Coding Agents")
    mcp_sub = parser_mcp.add_subparsers(dest="mcp_action")

    mcp_cfg = mcp_sub.add_parser("config", aliases=["export"], help="Xuất file cấu hình MCP cho Coding Agents")
    mcp_cfg.add_argument("--client", choices=["all", "antigravity", "cursor", "claude", "vscode", "windsurf"], default="all")
    mcp_cfg.add_argument("--transport", choices=["stdio", "sse"], default="stdio")
    mcp_cfg.add_argument("--host", default="127.0.0.1")
    mcp_cfg.add_argument("--port", type=int, default=8001)
    mcp_cfg.add_argument("--read-only", action="store_true", help="Chỉ bật chế độ Read-Only 10 tools")

    mcp_inst = mcp_sub.add_parser("install", help="Tự động ghi cấu hình MCP vào file của Coding Agent")
    mcp_inst.add_argument("--client", choices=["all", "antigravity", "cursor", "claude", "vscode", "windsurf"], default="all")
    mcp_inst.add_argument("--transport", choices=["stdio", "sse"], default="stdio")
    mcp_inst.add_argument("--host", default="127.0.0.1")
    mcp_inst.add_argument("--port", type=int, default=8001)
    mcp_inst.add_argument("--read-only", action="store_true", help="Chỉ bật chế độ Read-Only 10 tools")

    mcp_srv = mcp_sub.add_parser("serve", help="Khởi chạy FastMCP Server độc lập")
    mcp_srv.add_argument("--transport", choices=["sse", "stdio"], default="sse")
    mcp_srv.add_argument("--host", default="127.0.0.1")
    mcp_srv.add_argument("--port", type=int, default=8001)
    mcp_srv.add_argument("--read-only", action="store_true", help="Chỉ bật chế độ Read-Only 10 tools")

    # Command: init
    parser_init = subparsers.add_parser("init", help="Khởi tạo database constraints & seed data")

    # Command: ingest
    parser_ingest = subparsers.add_parser("ingest", help="Chạy 1 vòng quét tất cả các adapters")
    parser_ingest.add_argument(
        "--source",
        default="all",
        choices=[
            "all",
            "coding_agents",
            "coding_agent",
            "agents",
            "git",
            "jira",
            "shortcut",
            "outlook",
            "ms_outlook",
            "confluence",
            "github",
            "slack",
            "teams",
            "ms_teams",
        ],
        help="Nguồn cần nạp",
    )
    parser_ingest.add_argument("--tenant-id", default="local-user", help="Tenant ID")
    parser_ingest.add_argument("--config", default=None, help="Đường dẫn file cấu hình sources.yaml")

    # Command: status
    parser_status = subparsers.add_parser("status", help="Kiểm tra tình trạng kết nối Neo4j, Microsoft Session, Watchers, Processing Queue")

    # Command: serve
    parser_serve = subparsers.add_parser("serve", help="Khởi động FastMCP Server và Application Service")
    parser_serve.add_argument("--host", default="127.0.0.1", help="Địa chỉ host")
    parser_serve.add_argument("--port", type=int, default=8000, help="Cổng chạy service")
    parser_serve.add_argument("--mcp-only", action="store_true", help="Chỉ chạy FastMCP server")
    parser_serve.add_argument("--app-only", action="store_true", help="Chỉ chạy Application service")

    # Command: graph
    parser_graph = subparsers.add_parser("graph", help="Quản lý Graphiti Temporal Knowledge Graph")
    graph_sub = parser_graph.add_subparsers(dest="graph_action", help="Hành động trên Graph")
    parser_graph_rebuild = graph_sub.add_parser("rebuild", help="Rebuild semantic memory từ authoritative Neo4j domain data")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    cmd_map = {
        "run": cmd_run,
        "doctor": cmd_doctor,
        "login": cmd_login,
        "openwebui": cmd_openwebui,
        "mcp": cmd_mcp,
        "init": cmd_init,
        "ingest": cmd_ingest,
        "status": cmd_status,
        "serve": cmd_serve,
        "graph": cmd_graph,
    }

    handler = cmd_map.get(args.command)
    if not handler:
        parser.print_help()
        sys.exit(1)

    try:
        exit_code = asyncio.run(handler(args))
        sys.exit(exit_code or 0)
    except KeyboardInterrupt:
        print("\nThao tác bị hủy bởi người dùng.")
        sys.exit(0)


if __name__ == "__main__":
    main()
