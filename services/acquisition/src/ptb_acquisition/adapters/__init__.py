"""Acquisition Adapters Module.

Provides standard AcquisitionAdapter base class and source-specific adapters.
"""

from ptb_acquisition.adapters.agent_adapters import AgentWatchersAdapter, CodingAgentAdapter
from ptb_acquisition.adapters.base import AcquisitionAdapter
from ptb_acquisition.adapters.git_adapter import GitWatcherAdapter
from ptb_acquisition.adapters.jira_adapter import JiraAdapter
from ptb_acquisition.adapters.shortcut_adapter import ShortcutAdapter

__all__ = [
    "AcquisitionAdapter",
    "AgentWatchersAdapter",
    "CodingAgentAdapter",
    "JiraAdapter",
    "ShortcutAdapter",
    "GitWatcherAdapter",
]
