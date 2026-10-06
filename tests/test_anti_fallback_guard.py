"""Static Guard Tests for Anti-Fallback and Test Double Exemption.

Enforces Wave 0 requirements per docs/v1_2.md and implementation_plan_v1_2.md:
1. Production sources in scripts/, services/, packages/, integrations/ must NOT contain:
   - enable_mock_fallback=True
   - mock_mode=True
   - fake success or setTimeout simulating mutation success without calling backend
   - MOCK_DATA
   - DEMO_MODE
   - InMemoryRawEventRepository / InMemoryCheckpointRepository fallback in runtime
2. tests/ and fixtures/ directories and test files are strictly EXEMPT and permitted
   to use test doubles, mock modes, and in-memory repositories.
3. Temporary whitelist with explicit Wave tracking (Wave 1 and Wave 4) and actionable
   cleanup warnings so subsequent waves clean up all lingering fallbacks.
"""

from dataclasses import dataclass
from pathlib import Path
import re
import warnings
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories containing production source code
PRODUCTION_DIRECTORIES = ["scripts", "services", "packages", "integrations"]

# File extensions scanned for production source code
SOURCE_EXTENSIONS = {".py", ".html", ".js", ".ts", ".sh", ".json", ".yaml", ".yml"}

# -----------------------------------------------------------------------------
# GUARD PATTERNS
# -----------------------------------------------------------------------------
GUARD_PATTERNS = {
    "enable_mock_fallback=True": re.compile(
        r"enable_mock_fallback\s*(?::\s*[^=]+)?=\s*(?:True\b|Field\s*\(\s*[^)]*default\s*=\s*True)",
        re.IGNORECASE,
    ),
    "mock_mode=True": re.compile(
        r"mock_mode\s*(?::\s*[^=]+)?=\s*(?:True\b|Field\s*\(\s*[^)]*default\s*=\s*True)",
        re.IGNORECASE,
    ),
    "fake_success_settimeout": re.compile(
        r"setTimeout\s*\(\s*(?:function\s*\([^)]*\)|\([^)]*\)\s*=>|\w+\s*=>)[^{;]{0,50}\{?[^;]{0,250}?[\'\"](?:[^\'\"]*\b)?success\b",
        re.IGNORECASE,
    ),
    "fake_success_literal": re.compile(r"\bfake[\s_-]+success\b", re.IGNORECASE),
    "MOCK_DATA": re.compile(r"\bMOCK_DATA\b"),
    "DEMO_MODE": re.compile(r"\bDEMO_MODE\b"),
    "in_memory_fallback": re.compile(
        r"\bInMemory(?:RawEvent|Checkpoint|TaskDomain)Repository\b"
    ),
    "silent_except_pass": re.compile(r"except\s+Exception:\s*(?:\n\s*)?pass\b"),
    "fabricated_health": re.compile(r"\bfabricat(?:ed|e)?[\s_-]*health\b", re.IGNORECASE),
    "default_dummy": re.compile(r"\bdefault[\s_-]*dummy", re.IGNORECASE),
    "allow_heuristic_fallback": re.compile(r"\ballow_heuristic_fallback\b", re.IGNORECASE),
    "PTB_ALLOW_HEURISTIC_FALLBACK": re.compile(r"\bPTB_ALLOW_HEURISTIC_FALLBACK\b"),
}


# -----------------------------------------------------------------------------
# TEMPORARY WHITELIST FOR PENDING WAVES
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class WhitelistEntry:
    path: str
    pending_wave: str
    responsible_subagent: str
    reason: str
    cleanup_action: str
    allowed_patterns: set[str]


TEMPORARY_WHITELIST: dict[str, WhitelistEntry] = {}


# -----------------------------------------------------------------------------
# HELPER FUNCTIONS
# -----------------------------------------------------------------------------
def is_exempt_test_or_fixture_path(path: Path | str) -> bool:
    """Determine whether a path belongs to tests, fixtures, or test double support.

    Exempt paths are permitted to use mocks, fakes, and test doubles.
    """
    posix_path = Path(path).as_posix()
    # Normalize with leading/trailing slashes for directory matching
    normalized = f"/{posix_path.strip('/')}/"

    # Exclude standard test and fixture directories
    if "/tests/" in normalized or "/fixtures/" in normalized:
        return True

    # Exclude test files
    name = Path(posix_path).name
    if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
        return True

    # Exclude build, cache, and virtual environment directories
    parts = Path(posix_path).parts
    if any(
        p.startswith(".") or p in {"__pycache__", ".venv", "venv", "build", "dist", "node_modules"}
        for p in parts
    ):
        return True

    return False


def collect_production_files(repo_root: Path = REPO_ROOT) -> list[Path]:
    """Scan and collect all production source files in the designated production directories."""
    production_files: list[Path] = []

    for dir_name in PRODUCTION_DIRECTORIES:
        dir_path = repo_root / dir_name
        if not dir_path.exists():
            continue

        for file_path in dir_path.rglob("*"):
            if not file_path.is_file():
                continue

            rel_path = file_path.relative_to(repo_root)
            if is_exempt_test_or_fixture_path(rel_path):
                continue

            if file_path.suffix.lower() in SOURCE_EXTENSIONS:
                production_files.append(file_path)

    return sorted(production_files)


def scan_file_for_violations(
    file_path: Path,
    repo_root: Path = REPO_ROOT,
) -> tuple[list[dict], list[dict]]:
    """Scan a single file for anti-fallback pattern violations.

    Returns:
        (unwhitelisted_violations, whitelisted_matches)
    """
    rel_posix = file_path.relative_to(repo_root).as_posix()
    whitelist_entry = TEMPORARY_WHITELIST.get(rel_posix)

    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        return [
            {
                "file": rel_posix,
                "line": 0,
                "pattern": "FILE_READ_ERROR",
                "snippet": str(exc),
            }
        ], []

    unwhitelisted: list[dict] = []
    whitelisted: list[dict] = []

    for pattern_name, pattern_regex in GUARD_PATTERNS.items():
        for match in pattern_regex.finditer(content):
            line_no = content[: match.start()].count("\n") + 1
            line_start = content.rfind("\n", 0, match.start()) + 1
            line_end = content.find("\n", match.end())
            if line_end == -1:
                line_end = len(content)
            snippet = content[line_start:line_end].strip()

            finding = {
                "file": rel_posix,
                "line": line_no,
                "pattern": pattern_name,
                "snippet": snippet[:160],
            }

            if whitelist_entry and pattern_name in whitelist_entry.allowed_patterns:
                finding["wave"] = whitelist_entry.pending_wave
                finding["subagent"] = whitelist_entry.responsible_subagent
                finding["cleanup"] = whitelist_entry.cleanup_action
                whitelisted.append(finding)
            else:
                unwhitelisted.append(finding)

    return unwhitelisted, whitelisted


# =============================================================================
# TEST SUITE
# =============================================================================

def test_production_sources_free_of_unwhitelisted_fallback_patterns():
    """TEST 1: Ensure no production file contains unwhitelisted mock fallbacks or fake success.

    Checks:
    - enable_mock_fallback=True
    - mock_mode=True
    - fake success or setTimeout simulating mutation success without calling backend
    - MOCK_DATA
    - DEMO_MODE
    - InMemoryRawEventRepository / InMemoryCheckpointRepository fallback in runtime

    Any occurrence outside TEMPORARY_WHITELIST causes an immediate test failure.
    Whitelisted items emit visible warning messages with pending wave and responsible subagent.
    """
    prod_files = collect_production_files(REPO_ROOT)
    assert len(prod_files) > 0, "Production file collector must find production files"

    all_unwhitelisted_violations: list[dict] = []
    whitelisted_summary: dict[tuple[str, str], int] = {}

    for file_path in prod_files:
        unwhitelisted, whitelisted = scan_file_for_violations(file_path, REPO_ROOT)
        all_unwhitelisted_violations.extend(unwhitelisted)

        for match in whitelisted:
            key = (match["file"], match["pattern"])
            whitelisted_summary[key] = whitelisted_summary.get(key, 0) + 1

    # Emit warnings for all whitelisted findings so future waves have clear visibility
    for (file_name, pattern), count in sorted(whitelisted_summary.items()):
        entry = TEMPORARY_WHITELIST[file_name]
        warnings.warn(
            f"\n[GUARD WHITELIST WARNING] Temporary fallback permitted in '{file_name}':\n"
            f"  - Pattern: {pattern} ({count} occurrence{'s' if count > 1 else ''})\n"
            f"  - Pending Wave: {entry.pending_wave} ({entry.responsible_subagent})\n"
            f"  - Reason: {entry.reason}\n"
            f"  - Required Cleanup: {entry.cleanup_action}\n",
            category=UserWarning,
            stacklevel=2,
        )

    # Assert no unwhitelisted violations exist
    if all_unwhitelisted_violations:
        formatted_violations = "\n".join(
            f"  - {v['file']}:{v['line']} [{v['pattern']}]\n    Snippet: {v['snippet']}"
            for v in all_unwhitelisted_violations
        )
        pytest.fail(
            f"\nAnti-Fallback Guard detected {len(all_unwhitelisted_violations)} forbidden "
            f"fallback pattern(s) in production code:\n\n{formatted_violations}\n\n"
            f"Per docs/v1_2.md, production code must never use mock fallback, mock_mode=True, "
            f"fake success timeouts, or in-memory repositories in runtime.\n"
            f"Fail-fast error handling and real backend endpoints are mandatory."
        )


def test_test_and_fixtures_are_exempt_and_allow_test_doubles():
    """TEST 2: Ensure tests/ and fixtures/ are strictly exempt and permitted to use test doubles.

    Validates:
    1. Paths under tests/, fixtures/, and test files are recognized as exempt.
    2. Real test files that use mock_mode=True, InMemory repositories, or fixtures
       are successfully excluded from production scanning.
    3. Production files are NOT mistakenly treated as exempt.
    """
    # 1. Path classification checks
    assert is_exempt_test_or_fixture_path("tests/test_cli_supervisor.py") is True
    assert is_exempt_test_or_fixture_path("tests/fixtures/outlook_messages_fixture.json") is True
    assert is_exempt_test_or_fixture_path("services/acquisition/tests/test_adapters.py") is True
    assert is_exempt_test_or_fixture_path("packages/contracts/tests/test_contracts.py") is True
    assert is_exempt_test_or_fixture_path("services/processing/tests/test_worker.py") is True
    assert is_exempt_test_or_fixture_path("services/processing/tests/fixtures/sample.json") is True

    # 2. Production paths must NOT be exempt
    assert is_exempt_test_or_fixture_path("services/application/src/ptb_application/service.py") is False
    assert is_exempt_test_or_fixture_path("scripts/ptb_cli.py") is False
    assert is_exempt_test_or_fixture_path("packages/database/src/ptb_database/neo4j_client.py") is False
    assert is_exempt_test_or_fixture_path("integrations/openwebui/tools/ptb_tools.py") is False
    assert is_exempt_test_or_fixture_path("integrations/openwebui/board/ptb_board.html") is False

    # 3. Verify actual test files using test doubles are excluded from collect_production_files
    prod_files = {p.relative_to(REPO_ROOT).as_posix() for p in collect_production_files(REPO_ROOT)}

    test_files_with_doubles = [
        "services/processing/tests/test_worker.py",
        "services/processing/tests/test_processing.py",
        "services/acquisition/tests/test_adapters.py",
        "services/acquisition/tests/test_extended_adapters.py",
        "services/acquisition/tests/test_playwright_interceptor.py",
        "integrations/openwebui/tests/test_openwebui.py",
        "tests/e2e/test_vertical_slice_e2e.py",
        "tests/test_cli_supervisor.py",
    ]

    for test_file in test_files_with_doubles:
        assert test_file not in prod_files, (
            f"Test file '{test_file}' must NOT be collected as a production file!"
        )

    # 4. Verify that actual test files containing mock_mode=True or InMemory repos
    # are confirmed to exist and contain test doubles without triggering any guard failure
    test_worker_path = REPO_ROOT / "services/processing/tests/test_worker.py"
    if test_worker_path.exists():
        content = test_worker_path.read_text(encoding="utf-8")
        assert "FakeDeterministicLLMExtractor" in content or "FakeRawEventRepo" in content, (
            "test_worker.py contains test doubles as expected"
        )


def test_guard_regex_detection_effectiveness():
    """Verify regex patterns reliably detect forbidden patterns and avoid false positives."""
    # 1. enable_mock_fallback=True
    pat = GUARD_PATTERNS["enable_mock_fallback=True"]
    assert pat.search("enable_mock_fallback = True") is not None
    assert pat.search("enable_mock_fallback: bool = True") is not None
    assert pat.search("enable_mock_fallback: bool = Field(default=True, description='...')") is not None
    assert pat.search("enable_mock_fallback: bool = Field(\n    default=True\n)") is not None
    assert pat.search("enable_mock_fallback: bool = False") is None
    assert pat.search("enable_mock_fallback: bool = Field(default=False)") is None

    # 2. mock_mode=True
    pat = GUARD_PATTERNS["mock_mode=True"]
    assert pat.search("mock_mode = True") is not None
    assert pat.search("mock_mode: bool = True") is not None
    assert pat.search("mock_mode: bool = Field(default=True)") is not None
    assert pat.search("mock_mode: bool = False") is None
    assert pat.search("if self.mock_mode:") is None

    # 3. fake success / setTimeout simulating mutation success
    pat_timeout = GUARD_PATTERNS["fake_success_settimeout"]
    assert pat_timeout.search(
        'setTimeout(() => { showToast("Hoàn tất quét adapter!", "success"); }, 1000);'
    ) is not None
    assert pat_timeout.search(
        'setTimeout(function() { return "success"; }, 500);'
    ) is not None
    # Must NOT false-positive on UI animation timeouts
    assert pat_timeout.search('setTimeout(() => toast.remove(), 300);') is None
    # Must NOT false-positive on Python socket timeouts
    assert pat_timeout.search('s.settimeout(1.5)') is None

    # 4. fake_success_literal
    pat_literal = GUARD_PATTERNS["fake_success_literal"]
    assert pat_literal.search("return {'status': 'fake success'}") is not None
    assert pat_literal.search("# fake_success fallback") is not None
    assert pat_literal.search("real_success_result") is None

    # 5. MOCK_DATA & DEMO_MODE
    assert GUARD_PATTERNS["MOCK_DATA"].search("const MOCK_DATA = { today: [] };") is not None
    assert GUARD_PATTERNS["MOCK_DATA"].search("const REAL_DATA = {};") is None
    assert GUARD_PATTERNS["DEMO_MODE"].search("if (DEMO_MODE) { return []; }") is not None
    assert GUARD_PATTERNS["DEMO_MODE"].search("const PRODUCTION_MODE = true;") is None

    # 6. in_memory_fallback
    pat_inmem = GUARD_PATTERNS["in_memory_fallback"]
    assert pat_inmem.search("self.repo = InMemoryRawEventRepository()") is not None
    assert pat_inmem.search("self.repo = InMemoryCheckpointRepository()") is not None
    assert pat_inmem.search("self.repo = InMemoryTaskDomainRepository()") is not None
    assert pat_inmem.search("self.repo = Neo4jRawEventRepository(client)") is None

    # 7. silent_except_pass
    pat_silent = GUARD_PATTERNS["silent_except_pass"]
    assert pat_silent.search("except Exception:\n    pass") is not None
    assert pat_silent.search("except Exception: pass") is not None
    assert pat_silent.search("except Exception as e:\n    logger.error(e)") is None

    # 8. fabricated_health
    pat_fab = GUARD_PATTERNS["fabricated_health"]
    assert pat_fab.search("fabricated_health = True") is not None
    assert pat_fab.search("fabricateHealth()") is not None
    assert pat_fab.search("real_health_check()") is None

    # 9. default_dummy
    pat_dummy = GUARD_PATTERNS["default_dummy"]
    assert pat_dummy.search("default_dummy_value") is not None
    assert pat_dummy.search("DEFAULT_DUMMY = {}") is not None
    assert pat_dummy.search("default_value = {}") is None

    # 10. allow_heuristic_fallback & PTB_ALLOW_HEURISTIC_FALLBACK
    assert GUARD_PATTERNS["allow_heuristic_fallback"].search("allow_heuristic_fallback = True") is not None
    assert GUARD_PATTERNS["PTB_ALLOW_HEURISTIC_FALLBACK"].search("os.getenv('PTB_ALLOW_HEURISTIC_FALLBACK')") is not None

def test_whitelist_hygiene_and_contract_integrity():
    """Verify integrity of the temporary whitelist for Wave 1 and Wave 4.

    Ensures:
    - Every whitelisted file exists on disk.
    - Every whitelisted file actually contains the pattern it claims to whitelist.
    - Every entry specifies the pending wave, subagent, reason, and cleanup action.
    """
    for rel_path, entry in TEMPORARY_WHITELIST.items():
        file_path = REPO_ROOT / rel_path
        assert file_path.exists(), f"Whitelisted file '{rel_path}' does not exist on disk!"

        assert entry.pending_wave in {"Wave 1", "Wave 4"}, (
            f"Whitelisted file '{rel_path}' must target Wave 1 or Wave 4, got '{entry.pending_wave}'"
        )
        assert len(entry.responsible_subagent) > 0
        assert len(entry.reason) > 0
        assert len(entry.cleanup_action) > 0
        assert len(entry.allowed_patterns) > 0

        content = file_path.read_text(encoding="utf-8", errors="ignore")
        found_patterns = {
            pat_name
            for pat_name in entry.allowed_patterns
            if GUARD_PATTERNS[pat_name].search(content)
        }

        assert len(found_patterns) > 0, (
            f"Whitelisted file '{rel_path}' does not contain any of its declared patterns "
            f"{entry.allowed_patterns}! If it was cleaned up by {entry.pending_wave}, "
            f"please remove it from TEMPORARY_WHITELIST."
        )
