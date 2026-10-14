"""Acquisition Adapters Module.

Provides standard AcquisitionAdapter base class and source-specific adapters:
- AgentWatchersAdapter, CodingAgentAdapter (Local Coding Agents)
- GitWatcherAdapter (Local Git repositories)
- JiraAdapter (Jira Cloud & Server)
- ShortcutAdapter (Shortcut stories)
- ConfluenceAdapter (Confluence documentation & action items)
- GitHubAdapter (GitHub issues & pull requests)
- SlackAdapter (Slack conversations & threads)
- OutlookMailAdapter (Microsoft Outlook REST / Graph API)
"""

from ptb_acquisition.adapters.agent_adapters import AgentWatchersAdapter, CodingAgentAdapter
from ptb_acquisition.adapters.base import AcquisitionAdapter
from ptb_acquisition.adapters.confluence_adapter import ConfluenceAdapter
from ptb_acquisition.adapters.git_adapter import GitWatcherAdapter
from ptb_acquisition.adapters.github_adapter import GitHubAdapter
from ptb_acquisition.adapters.jira_adapter import JiraAdapter
from ptb_acquisition.adapters.outlook_adapter import OutlookMailAdapter
from ptb_acquisition.adapters.shortcut_adapter import ShortcutAdapter
from ptb_acquisition.adapters.slack_adapter import SlackAdapter

__all__ = [
    "AcquisitionAdapter",
    "AgentWatchersAdapter",
    "CodingAgentAdapter",
    "JiraAdapter",
    "ShortcutAdapter",
    "GitWatcherAdapter",
    "ConfluenceAdapter",
    "GitHubAdapter",
    "SlackAdapter",
    "OutlookMailAdapter",
]
