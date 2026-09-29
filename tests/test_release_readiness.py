"""V1 Release Readiness Automated Audit Suite (Wave 7D).

Automates verification of all 13 V1 Release Freeze Checklist items defined in
docs/v1_2.md and documented in docs/v1_release_checklist.md.

Strategy: static/grep-based source inspection + lightweight import checks.
All tests use @pytest.mark.contract and require NO live infrastructure.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Production source directories (excludes tests/)
PROD_DIRS = [
    REPO_ROOT / "packages",
    REPO_ROOT / "services",
    REPO_ROOT / "scripts",
    REPO_ROOT / "integrations",
]


def _iter_prod_py_files():
    """Yield all .py files under production directories, skipping test folders."""
    for d in PROD_DIRS:
        if not d.exists():
            continue
        for f in d.rglob("*.py"):
            rel = str(f).replace("\\", "/")
            if "/tests/" in rel or "/test_" in rel.split("/")[-1]:
                continue
            yield f


# ---------------------------------------------------------------------------
# 1. no runtime mocks
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_01_no_runtime_mocks() -> None:
    """Checklist #1: OpenWebUI board, tools, and functions contain NO runtime mocks or demo modes."""
    target_files = [
        REPO_ROOT / "integrations" / "openwebui" / "board" / "ptb_board.html",
        REPO_ROOT / "integrations" / "openwebui" / "tools" / "ptb_tools.py",
        REPO_ROOT / "integrations" / "openwebui" / "functions" / "ptb_board_action.py",
    ]
    forbidden = ("MOCK_DATA", "DEMO_MODE", "enable_mock_fallback", "mock_mode")

    for fp in target_files:
        assert fp.exists(), f"Missing OpenWebUI file: {fp}"
        content = fp.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in content, f"Forbidden token '{token}' in {fp.name}"
        # Ensure explicit error handling is wired
        assert "PTB-OWUI-001" in content, f"Missing PTB-OWUI-001 error handling in {fp.name}"


# ---------------------------------------------------------------------------
# 2. no runtime in-memory fallback
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_02_no_runtime_in_memory_fallback() -> None:
    """Checklist #2: InMemory* classes exist ONLY in tests/support/; production code never imports them."""
    forbidden_classes = (
        "InMemoryRawEventRepository",
        "InMemoryCheckpointRepository",
        "InMemoryTaskDomainRepository",
    )

    for py_file in _iter_prod_py_files():
        source = py_file.read_text(encoding="utf-8")
        for cls_name in forbidden_classes:
            assert cls_name not in source, (
                f"Forbidden test double '{cls_name}' found in production file {py_file}"
            )

    # Confirm test doubles exist in the correct location
    test_doubles = REPO_ROOT / "tests" / "support" / "test_doubles.py"
    assert test_doubles.exists(), "tests/support/test_doubles.py must exist"
    doubles_src = test_doubles.read_text(encoding="utf-8")
    for cls_name in forbidden_classes:
        assert cls_name in doubles_src, f"{cls_name} should be defined in test_doubles.py"


# ---------------------------------------------------------------------------
# 3. checkpoint fixed
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_03_checkpoint_fixed() -> None:
    """Checklist #3: SHA-256 deterministic ID, composite MERGE, cleanup_duplicate_checkpoints()."""
    # Verify composite uniqueness constraint in Cypher
    constraints_dir = REPO_ROOT / "packages" / "database" / "neo4j" / "constraints"
    cypher_files = list(constraints_dir.glob("*.cypher")) if constraints_dir.exists() else []
    assert cypher_files, "No Cypher constraint files found"
    all_cypher = "\n".join(f.read_text(encoding="utf-8") for f in cypher_files)
    assert "tenant_id" in all_cypher and "source_type" in all_cypher and "stream_id" in all_cypher, (
        "Composite uniqueness constraint (tenant_id, source_type, stream_id) missing in Cypher"
    )

    # Verify CheckpointRepository has deterministic SHA-256 and cleanup logic
    cp_repo_file = REPO_ROOT / "packages" / "database" / "src" / "ptb_database" / "repositories" / "checkpoint_repo.py"
    assert cp_repo_file.exists(), "CheckpointRepository file missing"
    cp_src = cp_repo_file.read_text(encoding="utf-8")
    assert "sha256" in cp_src.lower() or "hashlib" in cp_src, "SHA-256 deterministic ID missing in CheckpointRepository"
    assert "MERGE" in cp_src, "MERGE upsert missing in CheckpointRepository"
    assert "cleanup_duplicate_checkpoints" in cp_src, "cleanup_duplicate_checkpoints() missing in CheckpointRepository"


# ---------------------------------------------------------------------------
# 4. processing automatic
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_04_processing_automatic() -> None:
    """Checklist #4: ProcessingWorker auto-processes RawEvents; attempt count increments only on PROCESSING."""
    worker_file = REPO_ROOT / "services" / "processing" / "src" / "ptb_processing" / "worker.py"
    assert worker_file.exists(), "ProcessingWorker file missing"
    worker_src = worker_file.read_text(encoding="utf-8")
    assert "class ProcessingWorker" in worker_src, "ProcessingWorker class missing"
    assert "process_event" in worker_src or "process" in worker_src, "process method missing"

    # Verify attempt count logic in RawEventRepository
    raw_repo_file = REPO_ROOT / "packages" / "database" / "src" / "ptb_database" / "repositories" / "raw_event_repo.py"
    assert raw_repo_file.exists(), "RawEventRepository file missing"
    raw_src = raw_repo_file.read_text(encoding="utf-8")
    assert "processing_attempt_count" in raw_src, "processing_attempt_count tracking missing"
    assert "PROCESSING" in raw_src, "PROCESSING status transition missing in RawEventRepository"


# ---------------------------------------------------------------------------
# 5. intelligence automatic
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_05_intelligence_automatic() -> None:
    """Checklist #5: ProcessingPipeline auto-calls TaskIntelligenceLifecycle.on_task_changed()."""
    pipeline_file = REPO_ROOT / "services" / "processing" / "src" / "ptb_processing" / "pipeline.py"
    assert pipeline_file.exists(), "ProcessingPipeline file missing"
    pipeline_src = pipeline_file.read_text(encoding="utf-8")
    assert "intelligence_lifecycle" in pipeline_src, "intelligence_lifecycle not wired in ProcessingPipeline"
    assert "on_task_changed" in pipeline_src, "on_task_changed() not called in ProcessingPipeline"

    lifecycle_file = REPO_ROOT / "services" / "intelligence" / "src" / "ptb_intelligence" / "lifecycle.py"
    assert lifecycle_file.exists(), "TaskIntelligenceLifecycle file missing"
    lifecycle_src = lifecycle_file.read_text(encoding="utf-8")
    assert "class TaskIntelligenceLifecycle" in lifecycle_src
    assert "on_task_changed" in lifecycle_src


# ---------------------------------------------------------------------------
# 6. Graphiti sync automatic
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_06_graphiti_sync_automatic() -> None:
    """Checklist #6: GraphMemorySyncWorker runs async, logs PTB-GRAPH-001 on error, never breaks domain."""
    from ptb_graph_memory.sync_worker import GraphMemorySyncWorker, GraphSyncStatus

    # Verify canonical sync states
    expected = {"PENDING", "SYNCING", "SYNCED", "RETRY", "FAILED"}
    actual = {s.value for s in GraphSyncStatus}
    assert expected == actual, f"GraphSyncStatus mismatch: expected {expected}, got {actual}"

    sync_file = REPO_ROOT / "packages" / "graph_memory" / "src" / "ptb_graph_memory" / "sync_worker.py"
    sync_src = sync_file.read_text(encoding="utf-8")
    assert "PTB_GRAPH_001" in sync_src, "PTB-GRAPH-001 error telemetry missing in sync_worker"
    assert "class GraphMemorySyncWorker" in sync_src


# ---------------------------------------------------------------------------
# 7. REST real
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_07_rest_real_14_endpoints() -> None:
    """Checklist #7: FastAPI REST app exposes ≥14 real endpoints with shared ApplicationService."""
    from ptb_application.api import create_app

    app = create_app()
    registered: set[tuple[str, str]] = set()
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if methods and path:
            for method in methods:
                if method in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
                    registered.add((method, path))

    expected_14 = {
        ("GET", "/health"),
        ("GET", "/api/today"),
        ("GET", "/api/tasks"),
        ("GET", "/api/tasks/{task_id}"),
        ("GET", "/api/review"),
        ("POST", "/api/review/{task_id}/approve"),
        ("POST", "/api/review/{task_id}/dismiss"),
        ("PATCH", "/api/tasks/{task_id}"),
        ("POST", "/api/tasks/{task_id}/status"),
        ("POST", "/api/tasks/{task_id}/split"),
        ("GET", "/api/waiting"),
        ("GET", "/api/forgotten"),
        ("GET", "/api/knowledge"),
        ("GET", "/api/sources/health"),
    }

    missing = expected_14 - registered
    assert not missing, f"Missing REST endpoints: {missing}"
    assert len(registered) >= 14, f"Expected ≥14 REST endpoints, found {len(registered)}"


# ---------------------------------------------------------------------------
# 8. MCP real
# ---------------------------------------------------------------------------
@pytest.mark.contract
@pytest.mark.asyncio
async def test_08_mcp_real_10_tools() -> None:
    """Checklist #8: MCP Server exposes all 10 read-only tools + health checking."""
    from ptb_mcp.server import create_mcp_server

    mcp = create_mcp_server()
    tools = await mcp.list_tools()
    tool_names = {t.name for t in tools}

    expected_10 = {
        "get_today_tasks",
        "get_tasks",
        "get_task_context",
        "get_waiting_items",
        "get_forgotten_commitments",
        "get_review_queue",
        "search_decisions",
        "search_lessons_learned",
        "search_context",
        "get_source_health",
    }
    missing = expected_10 - tool_names
    assert not missing, f"Missing MCP tools: {missing}"

    # Verify get_mcp_health function exists and is importable
    from ptb_mcp.server import get_mcp_health
    assert callable(get_mcp_health), "get_mcp_health must be callable"

    # Verify MCP server source contains PTB-MCP-001 error handling
    mcp_src_file = REPO_ROOT / "services" / "mcp" / "src" / "ptb_mcp" / "server.py"
    mcp_src = mcp_src_file.read_text(encoding="utf-8")
    assert "PTB_MCP_001" in mcp_src, "PTB-MCP-001 error handling missing in MCP server"
    assert "not_ready" in mcp_src, "not_ready status missing in MCP health"


# ---------------------------------------------------------------------------
# 9. OpenWebUI actions real
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_09_openwebui_actions_real() -> None:
    """Checklist #9: OpenWebUI board executes real REST mutations, no fake setTimeout."""
    board = REPO_ROOT / "integrations" / "openwebui" / "board" / "ptb_board.html"
    html = board.read_text(encoding="utf-8")

    # Required real mutation endpoint patterns (approve, dismiss, status transitions)
    mutation_patterns = [
        "approve",
        "dismiss",
        "/api/tasks/",
        "/api/review/",
    ]
    for pat in mutation_patterns:
        assert pat in html, f"Missing mutation pattern '{pat}' in ptb_board.html"

    # Board must use fetch() for real API calls
    assert "fetch(" in html, "Board must use fetch() for real API calls"

    # No fake sync handlers or mock timeouts
    assert "syncSingleSource" not in html, "Fake syncSingleSource still present"
    assert "syncAllSources" not in html, "Fake syncAllSources still present"


# ---------------------------------------------------------------------------
# 10. health truthful
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_10_health_truthful() -> None:
    """Checklist #10: Health reports not_ready/degraded truthfully; SourceSyncState has 9 values."""
    from ptb_contracts.l1_acquisition import SourceSyncState

    expected = {
        "DISABLED", "UNCONFIGURED", "NEVER_SYNCED", "STARTING",
        "HEALTHY", "DEGRADED", "AUTH_REQUIRED", "ERROR", "NOT_INSTALLED",
    }
    actual = {s.value for s in SourceSyncState}
    assert expected == actual, f"SourceSyncState mismatch: {expected - actual} missing, {actual - expected} extra"
    assert len(actual) >= 9, f"Expected ≥9 SourceSyncState values, got {len(actual)}"

    # Verify ApplicationService has get_system_health
    svc_file = REPO_ROOT / "services" / "application" / "src" / "ptb_application" / "service.py"
    svc_src = svc_file.read_text(encoding="utf-8")
    assert "get_system_health" in svc_src, "get_system_health() missing in ApplicationService"
    assert "not_ready" in svc_src, "not_ready status missing in ApplicationService health"
    assert "degraded" in svc_src, "degraded status missing in ApplicationService health"


# ---------------------------------------------------------------------------
# 11. full source supervisor
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_11_full_source_supervisor() -> None:
    """Checklist #11: PTBProcessSupervisor supervises all sources with failure isolation."""
    cli_file = REPO_ROOT / "scripts" / "ptb_cli.py"
    cli_src = cli_file.read_text(encoding="utf-8")

    assert "class PTBProcessSupervisor" in cli_src, "PTBProcessSupervisor class missing"
    assert "class AdapterPollingRunner" in cli_src, "AdapterPollingRunner class missing"

    # Verify all acquisition source types are referenced
    for source in ("git", "jira", "shortcut", "coding_agent"):
        assert source in cli_src.lower(), f"Source '{source}' not supervised in ptb_cli.py"

    # Verify failure isolation with BugCode
    assert "PTB_L1_002" in cli_src, "PTB-L1-002 error isolation missing in AdapterPollingRunner"
    assert "DEGRADED" in cli_src, "DEGRADED status missing in AdapterPollingRunner"
    assert "consecutive_failures" in cli_src, "consecutive_failures tracking missing"


# ---------------------------------------------------------------------------
# 12. cross-platform tests green
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_12_cross_platform_tests_green() -> None:
    """Checklist #12: 5 pytest markers configured; CI matrix covers 3 OS × 3 Python versions."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for marker in ("unit:", "contract:", "fixture_e2e:", "runtime_smoke:", "external_integration:"):
        assert marker in pyproject, f"Missing pytest marker '{marker}' in pyproject.toml"

    # Verify watcher module exists and exports ALL_WATCHER_CLASSES
    watchers_init = REPO_ROOT / "services" / "acquisition" / "src" / "ptb_acquisition" / "watchers" / "__init__.py"
    assert watchers_init.exists(), "Watcher __init__.py missing"
    watcher_src = watchers_init.read_text(encoding="utf-8")
    assert "ALL_WATCHER_CLASSES" in watcher_src, "ALL_WATCHER_CLASSES not exported"

    # Verify CI workflow exists and has cross-platform matrix
    ci_yaml = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    assert ci_yaml.exists(), "CI workflow file missing"
    ci_content = ci_yaml.read_text(encoding="utf-8")
    for os_name in ("ubuntu", "macos", "windows"):
        assert os_name in ci_content, f"Missing OS '{os_name}' in CI matrix"
    for py_ver in ("3.11", "3.12", "3.13"):
        assert py_ver in ci_content, f"Missing Python '{py_ver}' in CI matrix"


# ---------------------------------------------------------------------------
# 13. docs match implementation
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_13_docs_match_implementation() -> None:
    """Checklist #13: README, Guideline, .env.example, docker-compose all aligned."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    guideline = (REPO_ROOT / "Guideline.md").read_text(encoding="utf-8")
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    docker_compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    checklist = (REPO_ROOT / "docs" / "v1_release_checklist.md").read_text(encoding="utf-8")

    # docker-compose port bindings
    assert "127.0.0.1:7687:7687" in docker_compose, "Neo4j bolt port binding missing"
    assert "127.0.0.1:3000:8080" in docker_compose, "OpenWebUI port binding missing"
    assert "./data/openwebui:/app/backend/data" in docker_compose, "OpenWebUI bind mount missing"

    # .env.example placeholder password and ports
    assert "NEO4J_PASSWORD=change_me_in_production" in env_example
    assert "APP_PORT=8000" in env_example
    assert "MCP_PORT=8001" in env_example

    # All 10 BugCodes documented
    from ptb_contracts.logging import BugCode
    for bc in BugCode:
        assert bc.value in readme, f"Missing {bc.value} in README.md"
        assert bc.value in guideline, f"Missing {bc.value} in Guideline.md"

    # All 5 pytest markers documented
    for marker in ("unit", "contract", "fixture_e2e", "runtime_smoke", "external_integration"):
        assert marker in readme, f"Missing marker '{marker}' in README.md"
        assert marker in guideline, f"Missing marker '{marker}' in Guideline.md"

    # Release checklist covers all 13 items
    for item in (
        "no runtime mocks",
        "no runtime in-memory fallback",
        "checkpoint fixed",
        "processing automatic",
        "intelligence automatic",
        "Graphiti sync automatic",
        "REST real",
        "MCP real",
        "OpenWebUI actions real",
        "health truthful",
        "full source supervisor",
        "cross-platform tests green",
        "docs match implementation",
    ):
        assert item in checklist, f"Missing checklist item '{item}' in v1_release_checklist.md"


# ---------------------------------------------------------------------------
# Bonus: TEMPORARY_WHITELIST is empty (anti-fallback guard contract)
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_bonus_temporary_whitelist_empty() -> None:
    """Verify TEMPORARY_WHITELIST == {} in test_anti_fallback_guard.py."""
    guard_file = REPO_ROOT / "tests" / "test_anti_fallback_guard.py"
    assert guard_file.exists(), "test_anti_fallback_guard.py missing"
    content = guard_file.read_text(encoding="utf-8")
    # Check TEMPORARY_WHITELIST is empty (with or without type annotation)
    assert "TEMPORARY_WHITELIST" in content, "TEMPORARY_WHITELIST not found"
    # Match pattern: TEMPORARY_WHITELIST... = {}
    import re
    assert re.search(r"TEMPORARY_WHITELIST\b.*=\s*\{\}", content), "TEMPORARY_WHITELIST must be empty (= {})"
