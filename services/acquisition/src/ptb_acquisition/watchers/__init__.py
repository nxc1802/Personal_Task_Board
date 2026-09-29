"""Watchers package for Layer 1B Coding Agent Logs (All Major Coding Agents)."""

from ptb_acquisition.watchers.base import BaseAgentWatcher
from ptb_acquisition.watchers.cursor_watcher import CursorWatcher
from ptb_acquisition.watchers.claude_code_watcher import ClaudeCodeWatcher
from ptb_acquisition.watchers.antigravity_watcher import AntigravityWatcher
from ptb_acquisition.watchers.codex_watcher import CodexWatcher
from ptb_acquisition.watchers.copilot_watcher import CopilotWatcher
from ptb_acquisition.watchers.windsurf_watcher import WindsurfWatcher
from ptb_acquisition.watchers.continue_watcher import ContinueWatcher
from ptb_acquisition.watchers.aider_watcher import AiderWatcher
from ptb_acquisition.watchers.cline_watcher import ClineWatcher
from ptb_acquisition.watchers.turn_filter import AgentTurnFilter, TurnClassification

ALL_WATCHER_CLASSES = [
    AntigravityWatcher,
    CursorWatcher,
    CodexWatcher,
    ClaudeCodeWatcher,
    CopilotWatcher,
    WindsurfWatcher,
    ContinueWatcher,
    AiderWatcher,
    ClineWatcher,
]

__all__ = [
    "BaseAgentWatcher",
    "CursorWatcher",
    "ClaudeCodeWatcher",
    "AntigravityWatcher",
    "CodexWatcher",
    "CopilotWatcher",
    "WindsurfWatcher",
    "ContinueWatcher",
    "AiderWatcher",
    "ClineWatcher",
    "ALL_WATCHER_CLASSES",
    "AgentTurnFilter",
    "TurnClassification",
]
