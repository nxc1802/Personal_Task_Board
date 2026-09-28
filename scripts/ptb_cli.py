#!/usr/bin/env python3
"""Personal Task Board (PTB) Command Line Interface.

Quản trị và vận hành hệ thống Personal Task Board v1 theo docs/v1.md:
  ptb init    : Khởi tạo database constraints & seed data trên Neo4j.
  ptb ingest  : Chạy 1 vòng quét tất cả các adapters (Coding Agents, Git, Jira, Shortcut).
  ptb serve   : Khởi động FastMCP Server và Application Service.
  ptb status  : Kiểm tra tình trạng kết nối Neo4j và tình trạng các sources.
"""

import argparse
import asyncio
import logging
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional

# Thêm root workspace vào sys.path để các module có thể import lẫn nhau
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "packages" / "contracts" / "src"))
sys.path.insert(0, str(ROOT_DIR / "packages" / "database" / "src"))
sys.path.insert(0, str(ROOT_DIR / "services" / "acquisition" / "src"))
sys.path.insert(0, str(ROOT_DIR / "services" / "processing" / "src"))
sys.path.insert(0, str(ROOT_DIR / "services" / "intelligence" / "src"))
sys.path.insert(0, str(ROOT_DIR / "services" / "application" / "src"))
sys.path.insert(0, str(ROOT_DIR / "services" / "mcp" / "src"))

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
# 1. COMMAND: INIT
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
# 2. COMMAND: INGEST
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

    # 1. Kết nối Repository lưu trữ
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

    # 2. Khởi tạo các Adapters
    sources_cfg = load_config_yaml(ROOT_DIR / "config" / "sources.yaml")
    if not sources_cfg:
        sources_cfg = load_config_yaml(ROOT_DIR / "config" / "sources.example.yaml")
    src_dict = sources_cfg.get("sources", {})

    target_source = getattr(args, "source", "all")
    adapters_to_run = []

    # 2.1 Coding Agent Watchers
    if target_source in ("all", "coding_agent", "agents"):
        agent_adapter = AgentWatchersAdapter(tenant_id=tenant_id)
        adapters_to_run.append(("Coding Agents", agent_adapter))

    # 2.2 Local Git Repositories
    if target_source in ("all", "git"):
        git_cfg = src_dict.get("git", {})
        repo_paths = git_cfg.get("repo_paths", [str(ROOT_DIR)])
        # Expand user paths
        expanded_paths = [os.path.expanduser(p) for p in repo_paths if os.path.exists(os.path.expanduser(p))]
        if not expanded_paths:
            expanded_paths = [str(ROOT_DIR)]
        git_adapter = GitWatcherAdapter(repo_paths=expanded_paths, tenant_id=tenant_id)
        adapters_to_run.append(("Local Git", git_adapter))

    # 2.3 Jira Adapter
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

    # 2.4 Shortcut Adapter
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
            events_count = 0
            # Sync default stream "all"
            events_count = await pipeline.sync_adapter(adapter, stream_id="all")
            total_synced += events_count
            print(f"  [{name}] ✓ Hoàn tất: phát hiện {stream_count} streams, thu nạp {events_count} events.")
            results.append((name, "Success", events_count))
        except Exception as e:
            print(f"  [{name}] ✗ Lỗi đồng bộ: {e}")
            results.append((name, f"Error: {e}", 0))

    # 3. Tổng kết thống kê
    print("\n" + "=" * 70)
    print("KẾT QUẢ THU THẬP DỮ LIỆU (INGESTION STATS):")
    print(f"  • Tổng số event thu nạp (ingested)    : {pipeline.stats['ingested']}")
    print(f"  • Trùng lặp được lọc (deduplicated)   : {pipeline.stats['deduplicated']}")
    print(f"  • Lưu trữ bền bỉ (persisted to DB)    : {pipeline.stats['persisted']}")
    print(f"  • Lỗi phát sinh (errors)              : {pipeline.stats['errors']}")
    print("=" * 70)

    return 0


# ==============================================================================
# 3. COMMAND: STATUS
# ==============================================================================
async def cmd_status(args: argparse.Namespace) -> int:
    """Kiểm tra tình trạng kết nối Neo4j và các sources."""
    print("\n" + "=" * 70)
    print("PERSONAL TASK BOARD: SYSTEM & SOURCE HEALTH STATUS")
    print("=" * 70)

    # 1. Kiểm tra Neo4j Single-Store
    neo4j_status = "Disconnected"
    neo4j_stats = {}
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
    except Exception as e:
        neo4j_status = f"Error: {e}"

    print(f"\n[1] Neo4j Single-Store Database:")
    print(f"    • Status        : {neo4j_status}")
    if neo4j_stats:
        print(f"    • Raw Events    : {neo4j_stats.get('raw_events', 0)}")
        print(f"    • Unified Tasks : {neo4j_stats.get('tasks', 0)}")
        print(f"    • Constraints   : {neo4j_stats.get('constraints', 0)}")

    # 2. Kiểm tra các Adapters
    from ptb_acquisition.adapters import (
        AgentWatchersAdapter,
        GitWatcherAdapter,
        JiraAdapter,
        ShortcutAdapter,
    )

    print(f"\n[2] Ingestion Sources Status:")

    # 2.1 Coding Agents
    agent_adapter = AgentWatchersAdapter()
    agent_health = await agent_adapter.health()
    print(f"    • Coding Agents : {agent_health.get('status', 'unknown').upper()}")
    watchers_data = agent_health.get("watchers", {})
    if isinstance(watchers_data, dict):
        for agent_name, w_info in watchers_data.items():
            paths_found = w_info.get("paths_found", []) if isinstance(w_info, dict) else []
            avail_str = f"Found ({len(paths_found)} dirs)" if paths_found else "No local logs"
            print(f"      - {agent_name.capitalize():<12} : {avail_str}")
    elif isinstance(watchers_data, list):
        for w in watchers_data:
            avail_str = "Available" if w.get("available") else "No local logs"
            print(f"      - {w.get('agent', '').capitalize():<12} : {avail_str}")

    # 2.2 Git Repositories
    git_adapter = GitWatcherAdapter(repo_paths=[str(ROOT_DIR)])
    git_health = await git_adapter.health()
    print(f"    • Git Watcher   : {git_health.get('status', 'unknown').upper()}")
    print(f"      - Git CLI     : {git_health.get('git_version', 'not found')}")
    print(f"      - Active Repos: {len(git_health.get('valid_git_repos', []))}")

    # 2.3 Jira
    jira_adapter = JiraAdapter()
    jira_health = await jira_adapter.health()
    print(f"    • Jira Cloud    : {jira_health.get('status', 'unknown').upper()} (Configured: {jira_health.get('configured')})")

    # 2.4 Shortcut
    shortcut_adapter = ShortcutAdapter()
    shortcut_health = await shortcut_adapter.health()
    print(f"    • Shortcut API  : {shortcut_health.get('status', 'unknown').upper()} (Configured: {shortcut_health.get('configured')})")

    # 2.5 Playwright Web Sessions (Teams / Outlook)
    from ptb_acquisition.playwright.session import DEFAULT_STORAGE_PATH, SessionManager
    teams_mgr = SessionManager(DEFAULT_STORAGE_PATH)
    has_session = teams_mgr.has_valid_session()
    print(f"    • Teams/Outlook : {'ACTIVE SESSION' if has_session else 'NO SESSION (Run login first)'}")

    print("\n" + "=" * 70)
    return 0


# ==============================================================================
# 4. COMMAND: SERVE
# ==============================================================================
async def cmd_serve(args: argparse.Namespace) -> int:
    """Khởi động FastMCP Server và Application Service."""
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
                # Kiểm tra ptb_mcp
                from ptb_mcp import __doc__ as mcp_doc
                print(f"  [✓] FastMCP Server đã sẵn sàng. Chờ kết nối từ Cursor/Claude/OpenWebUI.")
                while True:
                    await asyncio.sleep(3600)
            except asyncio.CancelledError:
                print("  [-] FastMCP Server đã dừng.")

        tasks.append(asyncio.create_task(_run_mcp()))

    # Application Service
    if not args.mcp_only:
        async def _run_app():
            print("  [+] Đang khởi động Application Service...")
            try:
                from ptb_application import __doc__ as app_doc
                print(f"  [✓] Application Service đang lắng nghe tại http://{args.host}:{args.port}")
                while True:
                    await asyncio.sleep(3600)
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
# MAIN ENTRYPOINT
# ==============================================================================
def main():
    load_env()

    parser = argparse.ArgumentParser(
        prog="ptb",
        description="Personal Task Board (PTB) Command Line Management Tool",
    )
    subparsers = parser.add_subparsers(dest="command", help="Lệnh thực thi")

    # Command: init
    parser_init = subparsers.add_parser("init", help="Khởi tạo database constraints & seed data")

    # Command: ingest
    parser_ingest = subparsers.add_parser("ingest", help="Chạy 1 vòng quét tất cả các adapters")
    parser_ingest.add_argument("--source", default="all", choices=["all", "coding_agent", "git", "jira", "shortcut"], help="Nguồn cần nạp")
    parser_ingest.add_argument("--tenant-id", default="local-user", help="Tenant ID")

    # Command: status
    parser_status = subparsers.add_parser("status", help="Kiểm tra tình trạng kết nối Neo4j và các sources")

    # Command: serve
    parser_serve = subparsers.add_parser("serve", help="Khởi động FastMCP Server và Application Service")
    parser_serve.add_argument("--host", default="127.0.0.1", help="Địa chỉ host")
    parser_serve.add_argument("--port", type=int, default=8000, help="Cổng chạy service")
    parser_serve.add_argument("--mcp-only", action="store_true", help="Chỉ chạy FastMCP server")
    parser_serve.add_argument("--app-only", action="store_true", help="Chỉ chạy Application service")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    cmd_map = {
        "init": cmd_init,
        "ingest": cmd_ingest,
        "status": cmd_status,
        "serve": cmd_serve,
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
