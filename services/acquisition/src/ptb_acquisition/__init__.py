"""Layer 1: Data Acquisition Service for Personal Task Board.

Includes:
- Layer 1A: Playwright Network Interceptor (Teams Web, Outlook Web)
- Layer 1B: Local Coding Agent Session Watchers (Cursor, Claude Code, Antigravity)
- Standard AcquisitionAdapter Contract and Pipeline
"""

from ptb_acquisition.adapters.agent_adapters import AgentWatchersAdapter, CodingAgentAdapter
from ptb_acquisition.adapters.base import AcquisitionAdapter
from ptb_acquisition.adapters.git_adapter import GitWatcherAdapter
from ptb_acquisition.adapters.jira_adapter import JiraAdapter
from ptb_acquisition.adapters.shortcut_adapter import ShortcutAdapter
from ptb_acquisition.pipeline import (
    AcquisitionPipeline,
    InMemoryCheckpointRepository,
    InMemoryRawEventRepository,
)
from ptb_acquisition.queue import LocalIngestionQueue

__all__ = [
    "LocalIngestionQueue",
    "AcquisitionAdapter",
    "AgentWatchersAdapter",
    "CodingAgentAdapter",
    "JiraAdapter",
    "ShortcutAdapter",
    "GitWatcherAdapter",
    "AcquisitionPipeline",
    "InMemoryRawEventRepository",
    "InMemoryCheckpointRepository",
]
