"""Playwright Network Interception package (Layer 1A)."""

from ptb_acquisition.playwright.session import (
    DEFAULT_STORAGE_PATH,
    SessionHealthState,
    SessionManager,
    verify_authenticated_session,
)
from ptb_acquisition.playwright.teams_interceptor import TeamsNetworkInterceptor
from ptb_acquisition.playwright.outlook_interceptor import OutlookNetworkInterceptor
from ptb_acquisition.playwright.runner import PlaywrightOrchestrator

__all__ = [
    "DEFAULT_STORAGE_PATH",
    "SessionHealthState",
    "SessionManager",
    "verify_authenticated_session",
    "TeamsNetworkInterceptor",
    "OutlookNetworkInterceptor",
    "PlaywrightOrchestrator",
]
