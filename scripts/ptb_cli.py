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
import logging
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.request

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


def load_config_yaml(file_path: Path) -> Dict[str, Any]:
    """Đọc file YAML cấu hình nếu tồn tại."""
    if not file_path.exists():
        return {}
    try:
        import yaml
        with open(file_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning(f"Không thể đọc file YAML {file_path}: {e}")
        return {}


# ==============================================================================
# PROCESS SUPERVISOR (ptb run)
# ==============================================================================
class PTBProcessSupervisor:
    """CLI Process Supervisor quản lý và giám sát đồng thời toàn bộ các components:

    1. Playwright acquisition runner (Teams & Outlook interceptors)
    2. Coding Agent Watchers (Cursor, Claude Code, Antigravity)
    3. ProcessingWorker vòng lặp xử lý background
    4. Application Service FastAPI REST trên 127.0.0.1:8000
    5. FastMCP Server trên 127.0.0.1:8001
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
    ):
        self.host = host
        self.app_port = app_port
        self.mcp_port = mcp_port
        self.tenant_id = tenant_id
        self.enable_playwright = enable_playwright
        self.poll_interval = poll_interval
        self.storage_path = storage_path or os.getenv("PTB_STORAGE_STATE") or os.path.join(ROOT_DIR, "data", "playwright", "storage_state.json")

        self.raw_event_repo = raw_event_repo
        self.checkpoint_repo = checkpoint_repo
        self.task_repo = task_repo

        self._running = False
        self._shutdown_event = asyncio.Event()

        # Component references
        self.app_server: Optional[Any] = None
        self.mcp_server: Optional[Any] = None
        self.processing_worker: Optional[Any] = None
        self.playwright_orchestrator: Optional[Any] = None
        self.neo4j_client: Optional[Any] = None
        self.tasks: List[asyncio.Task] = []

    def trigger_shutdown(self) -> None:
        """Kích hoạt tín hiệu dừng graceful shutdown."""
        self._shutdown_event.set()

    async def _poll_health_url(self, url: str, timeout: float = 1.0) -> bool:
        """Kiểm tra HTTP endpoint trả về status 200 trong separate thread để không block loop."""
        def _check():
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "PTB-Supervisor"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return 200 <= resp.status < 300
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
        from ptb_acquisition.pipeline import (
            AcquisitionPipeline,
            InMemoryCheckpointRepository,
            InMemoryRawEventRepository,
        )
        from ptb_acquisition.playwright.runner import PlaywrightOrchestrator
        from ptb_acquisition.playwright.session import SessionHealthState
        from ptb_application.api import app as fastapi_app
        from ptb_mcp.server import create_mcp_server
        from ptb_processing.pipeline import ProcessingPipeline
        from ptb_processing.worker import ProcessingWorker

        print("\n" + "=" * 70)
        print("PERSONAL TASK BOARD: STARTING PROCESS SUPERVISOR (ptb run)")
        print("=" * 70)

        # 1. Khởi tạo kho dữ liệu (Neo4j hoặc In-Memory Fallback)
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
                    self.task_repo = TaskDomainRepository(client)
                    print("  [✓] Đã kết nối Neo4j Persistence Engine (Authoritative Domain Store)")
                else:
                    await client.close()
                    print("  [!] Không thể kết nối Neo4j, sử dụng In-Memory Repositories.")
                    self.raw_event_repo = InMemoryRawEventRepository()
                    self.checkpoint_repo = InMemoryCheckpointRepository()
                    self.task_repo = None
            except Exception as e:
                logger.warning(f"Neo4j connectivity check failed: {e}")
                print("  [!] Neo4j không khả dụng, sử dụng In-Memory Repositories.")
                self.raw_event_repo = InMemoryRawEventRepository()
                self.checkpoint_repo = InMemoryCheckpointRepository()
                self.task_repo = None

        # 2. Pipeline thu nạp (Acquisition) & Coding Agent Watchers
        acq_pipeline = AcquisitionPipeline(
            raw_event_repo=self.raw_event_repo,
            checkpoint_repo=self.checkpoint_repo,
            tenant_id=self.tenant_id,
        )
        agent_adapter = AgentWatchersAdapter(tenant_id=self.tenant_id)
        acq_pipeline.register_adapter("coding_agents", agent_adapter)

        # 3. Processing Worker
        proc_pipeline = ProcessingPipeline(task_repo=self.task_repo)
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

        # 5. Application Service (FastAPI REST)
        app_config = uvicorn.Config(
            fastapi_app,
            host=self.host,
            port=self.app_port,
            log_level="warning",
            loop="asyncio",
        )
        self.app_server = uvicorn.Server(app_config)

        # 6. FastMCP Server
        mcp_srv = create_mcp_server()
        starlette_mcp = mcp_srv.sse_app(host=self.host)
        mcp_config = uvicorn.Config(
            starlette_mcp,
            host=self.host,
            port=self.mcp_port,
            log_level="warning",
            loop="asyncio",
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
        self.tasks.append(
            asyncio.create_task(
                self.processing_worker.run_loop(poll_interval=self.poll_interval),
                name="ptb-processing-worker",
            )
        )

        # 4. Coding Agent Watchers Polling Loop
        async def _watchers_loop():
            while self._running:
                try:
                    await acq_pipeline.sync_adapter(agent_adapter, stream_id="all")
                except asyncio.CancelledError:
                    break
                except Exception as w_err:
                    logger.warning("Error in coding agent watchers loop: %s", w_err)
                try:
                    await asyncio.sleep(5.0)
                except asyncio.CancelledError:
                    break

        self.tasks.append(
            asyncio.create_task(_watchers_loop(), name="ptb-watchers-loop")
        )

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
                    try:
                        await asyncio.sleep(10.0)
                    except asyncio.CancelledError:
                        break
                return

            try:
                await self.playwright_orchestrator.start_interceptor(run_sweep=True)
            except asyncio.CancelledError:
                self.playwright_orchestrator.stop()
            except Exception as pw_err:
                logger.warning("Playwright acquisition runner stopped: %s. Entering standby.", pw_err)
                while self._running:
                    try:
                        await asyncio.sleep(10.0)
                    except asyncio.CancelledError:
                        break

        self.tasks.append(
            asyncio.create_task(_playwright_loop(), name="ptb-playwright-loop")
        )

        # 7. Health Polling Supervisor Mechanism
        app_url = f"http://{self.host}:{self.app_port}/health"
        mcp_url = f"http://{self.host}:{self.mcp_port}/health"

        app_ready = False
        mcp_ready = False
        start_time = asyncio.get_event_loop().time()
        timeout_seconds = 15.0

        while (asyncio.get_event_loop().time() - start_time) < timeout_seconds and self._running:
            if not app_ready:
                app_ready = await self._poll_health_url(app_url)
            if not mcp_ready:
                # FastMCP SSE health
                mcp_ready = await self._poll_health_url(mcp_url) or await self._poll_port_open(self.host, self.mcp_port)

            if app_ready and mcp_ready:
                break
            await asyncio.sleep(0.3)

        if not (app_ready and mcp_ready):
            print("\n[✗] ERROR: Không thể vượt qua Health Check trong thời gian khởi động!")
            print(f"    • Application REST ({app_url}): {'[PASS]' if app_ready else '[FAIL]'}")
            print(f"    • FastMCP Server ({mcp_url}): {'[PASS]' if mcp_ready else '[FAIL]'}")
            await self.shutdown()
            return 1

        # In thông báo chuẩn theo yêu cầu thiết kế
        print("\n" + "=" * 70)
        print("READY: All PTB components operational!")
        print("=" * 70)
        print(f"  • Application Service REST : http://{self.host}:{self.app_port} (/health [PASS])")
        print(f"  • FastMCP Server (SSE)     : http://{self.host}:{self.mcp_port} (Port bound [PASS])")
        print(f"  • ProcessingWorker Loop    : ACTIVE (poll_interval={self.poll_interval}s)")
        print(f"  • Coding Agent Watchers    : ACTIVE (Cursor, Claude Code, Antigravity)")
        pw_state = "ACTIVE" if (self.enable_playwright and self.playwright_orchestrator.session_mgr.has_valid_session()) else "STANDBY (Run 'ptb login microsoft' to enable)"
        print(f"  • Playwright Acquisition   : {pw_state}")
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
        print("\n[*] Đang tắt an toàn toàn bộ services...")

        # 1. Dừng workers
        if self.processing_worker:
            self.processing_worker.stop()
        if self.playwright_orchestrator:
            self.playwright_orchestrator.stop()

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

        # 4. Đóng Neo4j client nếu có
        if self.neo4j_client:
            try:
                await self.neo4j_client.close()
            except Exception:
                pass

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

    PASS_SYM = f"{COLOR_GREEN}[✓] PASS{COLOR_RESET}"
    FAIL_SYM = f"{COLOR_RED}[✗] FAIL{COLOR_RESET}"
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
            res = subprocess.run([docker_bin, "info"], capture_output=True, text=True, timeout=4)
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
        except Exception:
            pass

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
            if res.get("success") or res.get("status") == "success":
                print(f"\n[✓] {res.get('message')}")
                print(f"    • Target Directory   : {res.get('target_dir')}")
                print(f"    • Pinned Version     : {res.get('pinned_version')}")
                print(f"    • Components Installed:")
                for c in res.get("components_installed", []):
                    print(f"      - {c}")
                return 0
            else:
                print(f"\n[✗] Cài đặt thất bại: {res.get('message')}")
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
    )
    git_adapter = GitWatcherAdapter(repo_paths=[str(ROOT_DIR)])
    git_health = await git_adapter.health()
    jira_adapter = JiraAdapter()
    jira_health = await jira_adapter.health()
    shortcut_adapter = ShortcutAdapter()
    shortcut_health = await shortcut_adapter.health()

    print(f"\n[5] Other Ingestion Adapters:")
    print(f"    • Git Watcher   : {git_health.get('status', 'unknown').upper()} (Active Repos: {len(git_health.get('valid_git_repos', []))})")
    print(f"    • Jira Cloud    : {jira_health.get('status', 'unknown').upper()} (Configured: {jira_health.get('configured')})")
    print(f"    • Shortcut API  : {shortcut_health.get('status', 'unknown').upper()} (Configured: {shortcut_health.get('configured')})")

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
    from ptb_acquisition.pipeline import (
        AcquisitionPipeline,
        InMemoryCheckpointRepository,
        InMemoryRawEventRepository,
    )

    tenant_id = args.tenant_id or "local-user"

    raw_repo = None
    ckpt_repo = None
    neo4j_connected = False

    try:
        from ptb_database.neo4j_client import Neo4jClient
        from ptb_database.repositories import CheckpointRepository, RawEventRepository

        client = Neo4jClient()
        if await client.verify_connectivity():
            raw_repo = RawEventRepository(client)
            ckpt_repo = CheckpointRepository(client)
            neo4j_connected = True
            print("  ✓ Đã kết nối Neo4j Persistence Engine (RawEvent & Checkpoint repositories active)")
    except Exception as e:
        logger.debug(f"Không thể kết nối Neo4j, fallback in-memory: {e}")

    if not neo4j_connected:
        print("  ! Lưu ý: Neo4j không khả dụng, sử dụng In-Memory Repository để chạy thử nghiệm.")
        raw_repo = InMemoryRawEventRepository()
        ckpt_repo = InMemoryCheckpointRepository()

    pipeline = AcquisitionPipeline(
        raw_event_repo=raw_repo,
        checkpoint_repo=ckpt_repo,
        tenant_id=tenant_id,
    )

    sources_cfg = load_config_yaml(ROOT_DIR / "config" / "sources.yaml")
    if not sources_cfg:
        sources_cfg = load_config_yaml(ROOT_DIR / "config" / "sources.example.yaml")
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

    print(f"\n  -> Đang quét {len(adapters_to_run)} adapters đã kích hoạt...")

    total_synced = 0
    results = []

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

    # FastMCP Server
    if not args.app_only:
        async def _run_mcp():
            print("  [+] Đang khởi động FastMCP Read-Only Server...")
            try:
                from ptb_mcp.server import create_mcp_server
                import uvicorn
                mcp_srv = create_mcp_server()
                starlette_mcp = mcp_srv.sse_app(host=args.host)
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
                from ptb_application.api import app as fastapi_app
                import uvicorn
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
    parser_run.add_argument("--host", default="127.0.0.1", help="Host lắng nghe (mặc định 127.0.0.1)")
    parser_run.add_argument("--port", type=int, default=8000, help="Cổng Application REST API (mặc định 8000)")
    parser_run.add_argument("--mcp-port", type=int, default=8001, help="Cổng FastMCP SSE (mặc định 8001)")
    parser_run.add_argument("--tenant-id", default="local-user", help="Tenant ID")
    parser_run.add_argument("--poll-interval", type=float, default=2.0, help="Chu kỳ thăm dò của worker (giây)")
    parser_run.add_argument("--no-playwright", action="store_true", help="Không khởi chạy Playwright browser daemon")

    # Command: doctor
    parser_doctor = subparsers.add_parser("doctor", help="Kiểm tra môi trường và các thành phần phụ thuộc")

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

    # Command: init
    parser_init = subparsers.add_parser("init", help="Khởi tạo database constraints & seed data")

    # Command: ingest
    parser_ingest = subparsers.add_parser("ingest", help="Chạy 1 vòng quét tất cả các adapters")
    parser_ingest.add_argument("--source", default="all", choices=["all", "coding_agent", "git", "jira", "shortcut"], help="Nguồn cần nạp")
    parser_ingest.add_argument("--tenant-id", default="local-user", help="Tenant ID")

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
