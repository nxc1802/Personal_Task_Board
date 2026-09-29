"""UI Contract Tests for OpenWebUI Integration (Wave 4 — Sub-Agent 4D).

Validates static and runtime contracts of the OpenWebUI integration without browser or Docker:
1. test_board_contains_no_mock_data_or_demo_mode
2. test_tools_and_functions_contain_no_fallback
3. test_every_board_api_path_exists_in_fastapi_routes
4. test_every_mutation_maps_to_existing_endpoint
5. test_canonical_source_states_enum
"""

from pathlib import Path
import re

from ptb_application.api import app as fastapi_app
from ptb_contracts.l1_acquisition import SourceSyncState

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
BOARD_HTML_PATH = REPO_ROOT / "integrations" / "openwebui" / "board" / "ptb_board.html"
TOOLS_PY_PATH = REPO_ROOT / "integrations" / "openwebui" / "tools" / "ptb_tools.py"
FUNCTIONS_PY_PATH = (
    REPO_ROOT / "integrations" / "openwebui" / "functions" / "ptb_board_action.py"
)


def _extract_script_content(html: str) -> str:
    """Extract all <script>...</script> blocks from HTML content."""
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html, flags=re.DOTALL | re.IGNORECASE)
    return "\n".join(scripts) if scripts else html


def _normalize_board_path(raw_path: str) -> str:
    """Normalize a JS path template (e.g. '/api/tasks/${taskId}/status?foo=1') to '/api/tasks/{param}/status'."""
    path_without_query = raw_path.split("?", 1)[0].strip()
    # Replace JS template interpolations like ${taskId}, ${revId}, ${encodeURIComponent(id)} with {param}
    normalized = re.sub(r"\$\{[^}]+\}", "{param}", path_without_query)
    return normalized


def _normalize_fastapi_route_path(route_path: str) -> str:
    """Normalize a FastAPI route path (e.g. '/api/tasks/{task_id}/status') to '/api/tasks/{param}/status'."""
    return re.sub(r"\{[^}]+\}", "{param}", route_path.strip())


def _get_fastapi_route_map() -> dict[str, set[str]]:
    """Build a mapping of normalized route path -> set of HTTP methods from ptb_application.api.app."""
    route_map: dict[str, set[str]] = {}
    for route in fastapi_app.routes:
        route_path = getattr(route, "path", None)
        route_methods = getattr(route, "methods", None)
        if not route_path or not route_methods:
            continue
        norm_path = _normalize_fastapi_route_path(route_path)
        if norm_path not in route_map:
            route_map[norm_path] = set()
        route_map[norm_path].update(m.upper() for m in route_methods)
    return route_map


def test_board_contains_no_mock_data_or_demo_mode():
    """1. Read integrations/openwebui/board/ptb_board.html and assert no mock/demo/fake-sync tokens exist."""
    assert BOARD_HTML_PATH.exists(), f"Board HTML file not found at {BOARD_HTML_PATH}"
    content = BOARD_HTML_PATH.read_text(encoding="utf-8")

    forbidden_tokens = [
        "MOCK_DATA",
        "DEMO_MODE",
        "isMockMode",
        "syncSingleSource",
        "syncAllSources",
    ]
    found_tokens = [token for token in forbidden_tokens if token in content]
    assert not found_tokens, (
        f"ptb_board.html still contains forbidden mock/demo tokens: {found_tokens}"
    )


def test_tools_and_functions_contain_no_fallback():
    """2. Read ptb_tools.py and ptb_board_action.py and assert no enable_mock_fallback or mock_mode."""
    target_files = [TOOLS_PY_PATH, FUNCTIONS_PY_PATH]
    forbidden_tokens = ["enable_mock_fallback", "mock_mode"]

    for file_path in target_files:
        assert file_path.exists(), f"Expected file not found: {file_path}"
        content = file_path.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            assert token not in content, (
                f"{file_path.relative_to(REPO_ROOT)} must not contain '{token}'"
            )


def test_every_board_api_path_exists_in_fastapi_routes():
    """3. Extract all /api/... and /health paths called in ptb_board.html and verify they exist in FastAPI routes."""
    assert BOARD_HTML_PATH.exists(), f"Board HTML file not found at {BOARD_HTML_PATH}"
    html_content = BOARD_HTML_PATH.read_text(encoding="utf-8")
    script_content = _extract_script_content(html_content)

    # Extract all quoted/template-literal API paths called in JS (with or without ${CONFIG.apiBase} prefix)
    path_pattern = re.compile(
        r"[`\'\"](?:\$\{[^}]+\})?((?:/api/[a-zA-Z0-9_/${}.()-]+|/health))(?:\?[^`\'\"]*)?[`\'\"]"
    )
    raw_matches = [m.group(1) for m in path_pattern.finditer(script_content)]
    assert len(raw_matches) > 0, "Expected to find API paths (/api/... or /health) in ptb_board.html"

    normalized_board_paths = {_normalize_board_path(p) for p in raw_matches}
    fastapi_route_map = _get_fastapi_route_map()
    available_routes = set(fastapi_route_map.keys())

    missing_paths = sorted(normalized_board_paths - available_routes)
    assert not missing_paths, (
        f"Board calls API path(s) that do not exist in FastAPI app.routes: {missing_paths}. "
        f"Available routes: {sorted(available_routes)}"
    )


def test_every_mutation_maps_to_existing_endpoint():
    """4. Scan fetch(...) / apiFetch(...) calls with POST or PATCH in ptb_board.html and verify route + HTTP method."""
    assert BOARD_HTML_PATH.exists(), f"Board HTML file not found at {BOARD_HTML_PATH}"
    html_content = BOARD_HTML_PATH.read_text(encoding="utf-8")
    script_content = _extract_script_content(html_content)

    # Match fetch(url, { ... }) or apiFetch(url, { ... }) calls
    call_pattern = re.compile(
        r"(?:apiFetch|fetch)\s*\(\s*[`\'\"](?:\$\{[^}]+\})?((?:/api/[^`\'\"?]+|/health))(?:\?[^`\'\"]*)?[`\'\"][ \t]*,[ \t]*\{((?:[^{}]|\{[^{}]*\})*)\}",
        re.DOTALL,
    )
    method_pattern = re.compile(r"method\s*:\s*[\'\"](POST|PATCH)[\'\"]", re.IGNORECASE)

    mutations_found: list[tuple[str, str]] = []
    for match in call_pattern.finditer(script_content):
        raw_path = match.group(1)
        options_block = match.group(2)
        method_match = method_pattern.search(options_block)
        if method_match:
            http_method = method_match.group(1).upper()
            norm_path = _normalize_board_path(raw_path)
            mutations_found.append((norm_path, http_method))

    assert len(mutations_found) > 0, (
        "Expected to find at least one POST or PATCH fetch(...) mutation in ptb_board.html"
    )

    fastapi_route_map = _get_fastapi_route_map()

    invalid_mutations: list[str] = []
    for norm_path, http_method in mutations_found:
        if norm_path not in fastapi_route_map:
            invalid_mutations.append(f"{http_method} {norm_path} (path not found in app.routes)")
        elif http_method not in fastapi_route_map[norm_path]:
            allowed = sorted(fastapi_route_map[norm_path])
            invalid_mutations.append(
                f"{http_method} {norm_path} (method not allowed; route supports {allowed})"
            )

    assert not invalid_mutations, (
        f"Found mutation call(s) in ptb_board.html without matching FastAPI endpoint: {invalid_mutations}"
    )


def test_canonical_source_states_enum():
    """5. Verify SourceSyncState has all 9 canonical states and ptb_board.html handles source state display."""
    expected_canonical_states = {
        "DISABLED",
        "UNCONFIGURED",
        "NEVER_SYNCED",
        "STARTING",
        "HEALTHY",
        "DEGRADED",
        "AUTH_REQUIRED",
        "ERROR",
        "NOT_INSTALLED",
    }

    actual_enum_names = {member.name for member in SourceSyncState}
    actual_enum_values = {member.value for member in SourceSyncState}

    assert expected_canonical_states.issubset(actual_enum_names), (
        f"SourceSyncState is missing canonical state names: "
        f"{sorted(expected_canonical_states - actual_enum_names)}"
    )
    assert expected_canonical_states.issubset(actual_enum_values), (
        f"SourceSyncState is missing canonical state values: "
        f"{sorted(expected_canonical_states - actual_enum_values)}"
    )

    # Verify ptb_board.html renders source health view and handles status badges
    assert BOARD_HTML_PATH.exists(), f"Board HTML file not found at {BOARD_HTML_PATH}"
    board_content = BOARD_HTML_PATH.read_text(encoding="utf-8")

    assert "renderHealthView" in board_content, "ptb_board.html must define renderHealthView()"
    assert "/api/sources/health" in board_content, "ptb_board.html must fetch /api/sources/health"
    assert "src.status" in board_content, "ptb_board.html must display source status from API payload"
