"""Global Pytest configuration and automatic test taxonomy classification (Wave 6)."""

from __future__ import annotations

from pathlib import Path
import socket
import pytest

TAXONOMY_MARKERS = {
    "unit",
    "contract",
    "fixture_e2e",
    "runtime_smoke",
    "external_integration",
}

EXTERNAL_SKIP_REASON = (
    "External integration skipped: live Neo4j/Docker not running on 127.0.0.1:7687"
)


def _is_tcp_port_open(host: str = "127.0.0.1", port: int = 7687, timeout: float = 0.2) -> bool:
    """Check whether a TCP port is accepting connections within the given timeout."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register custom command-line options for external integration tests."""
    parser.addoption(
        "--run-external",
        action="store_true",
        default=False,
        help="Run external_integration tests even if local Neo4j port probe fails.",
    )


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Automatically classify test items into the 5-tier Wave 6 test taxonomy
    and skip external_integration tests when live infrastructure is absent.
    """
    run_external = bool(config.getoption("--run-external", default=False))
    neo4j_port_open: bool | None = None
    skip_external = pytest.mark.skip(reason=EXTERNAL_SKIP_REASON)

    for item in items:
        existing_taxonomy = {
            mark.name for mark in item.iter_markers() if mark.name in TAXONOMY_MARKERS
        }

        if not existing_taxonomy:
            item_path = getattr(item, "path", None) or Path(str(item.fspath))
            path_str = Path(item_path).as_posix()

            if "tests/e2e/" in path_str:
                item.add_marker(pytest.mark.fixture_e2e)
            elif "test_cli_supervisor.py" in path_str or "test_runtime_smoke.py" in path_str:
                item.add_marker(pytest.mark.runtime_smoke)
            elif (
                "test_anti_fallback_guard.py" in path_str
                or "test_schema_contracts.py" in path_str
                or "test_ui_contracts.py" in path_str
                or "packages/contracts/tests/" in path_str
                or "test_validator.py" in path_str
            ):
                item.add_marker(pytest.mark.contract)
            elif "test_external_integration.py" in path_str:
                item.add_marker(pytest.mark.external_integration)
            else:
                item.add_marker(pytest.mark.unit)

        if item.get_closest_marker("external_integration") is not None:
            if not run_external:
                if neo4j_port_open is None:
                    neo4j_port_open = _is_tcp_port_open("127.0.0.1", 7687, timeout=0.2)
                if not neo4j_port_open:
                    item.add_marker(skip_external)
