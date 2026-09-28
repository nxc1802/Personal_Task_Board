"""Layer 1: Data Acquisition Service for Personal Task Board.

Includes:
- Layer 1A: Playwright Network Interceptor (Teams Web, Outlook Web)
- Layer 1B: Local Coding Agent Session Watchers (Cursor, Claude Code, Antigravity)
"""

from ptb_acquisition.queue import LocalIngestionQueue

__all__ = ["LocalIngestionQueue"]
