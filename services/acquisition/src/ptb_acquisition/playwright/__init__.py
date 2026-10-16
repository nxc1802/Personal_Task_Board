"""Playwright Network Interception package (Layer 1A)."""

from ptb_acquisition.playwright.session import (
    DEFAULT_STORAGE_PATH,
    SessionHealthState,
    SessionManager,
    transition_session_state,
    verify_authenticated_session,
)
from ptb_acquisition.playwright.teams_interceptor import TeamsNetworkInterceptor
from ptb_acquisition.playwright.outlook_interceptor import OutlookNetworkInterceptor
from ptb_acquisition.playwright.runner import PlaywrightOrchestrator

from ptb_acquisition.playwright.teams_authenticator import (
    TeamsAuthenticatorSession,
    create_auth_session,
    get_auth_session,
)

__all__ = [
    "DEFAULT_STORAGE_PATH",
    "SessionHealthState",
    "SessionManager",
    "transition_session_state",
    "verify_authenticated_session",
    "TeamsNetworkInterceptor",
    "OutlookNetworkInterceptor",
    "PlaywrightOrchestrator",
    "TeamsAuthenticatorSession",
    "create_auth_session",
    "get_auth_session",
]

