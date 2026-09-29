"""Test support package containing test doubles, fixtures, and fakes.

Strictly excluded from production scanning per anti-fallback rules.
"""

from tests.support.test_doubles import (
    InMemoryCheckpointRepository,
    InMemoryRawEventRepository,
)

__all__ = [
    "InMemoryRawEventRepository",
    "InMemoryCheckpointRepository",
]
