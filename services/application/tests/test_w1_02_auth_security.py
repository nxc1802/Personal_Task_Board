"""W1-02 Security & API Hardening Tests.

Verifies:
- P0-1: CORS whitelist enforcement (whitelisted origins allowed, unknown rejected)
- P0-2: API key authentication via X-PTB-API-KEY / Bearer token (401 on missing/bad, 200 on valid)
- P0-2: /health exempt from authentication
- P0-2: Unauthenticated dev mode when PTB_API_KEY is unset
- P0-5: Destructive database reset requires API auth & explicit admin auth (X-PTB-ADMIN-KEY)
- P1-6: Process execution (/api/auth/start-login, /api/auth/save-cookie) protected by auth
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from ptb_application.api import create_app, set_application_service
from ptb_application.service import ApplicationService


@pytest.fixture
def mock_app_service():
    """Create a mock ApplicationService for testing REST endpoints."""
    svc = MagicMock(spec=ApplicationService)
    svc.get_system_health = AsyncMock(return_value={"status": "healthy", "neo4j": "healthy"})
    svc.list_tasks = AsyncMock(return_value=[])
    svc.get_today_plan = AsyncMock()
    svc.execute_task_action = AsyncMock(return_value=MagicMock(success=True, task_id="t1", message="OK"))
    svc.update_task = AsyncMock(return_value=None)
    svc.create_task = AsyncMock(return_value=MagicMock(id="new-1", title="Test Task"))
    svc.delete_task = AsyncMock(return_value=True)
    svc.neo4j_client = MagicMock()
    svc.neo4j_client.execute_write = AsyncMock(return_value=[])
    return svc


class TestAPIKeyAuthentication:
    """Tests for P0-2: API Key Authentication."""

    def test_health_accessible_without_auth_when_api_key_configured(self, mock_app_service):
        """GET /health must be accessible with 200 without any API key even when PTB_API_KEY is configured."""
        with patch.dict(os.environ, {"PTB_API_KEY": "super-secret-key-123"}, clear=False):
            app = create_app(application_service=mock_app_service)
            client = TestClient(app)
            resp = client.get("/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "healthy"

    def test_endpoints_reject_missing_api_key_when_configured(self, mock_app_service):
        """Any non-health endpoint must return 401 when PTB_API_KEY is configured and no key is provided."""
        with patch.dict(os.environ, {"PTB_API_KEY": "super-secret-key-123"}, clear=False):
            app = create_app(application_service=mock_app_service)
            client = TestClient(app)

            # Test GET endpoint
            resp_tasks = client.get("/api/tasks")
            assert resp_tasks.status_code == 401
            assert "Missing API key" in resp_tasks.json()["detail"]

            # Test POST endpoint
            resp_today = client.get("/api/today")
            assert resp_today.status_code == 401

    def test_endpoints_reject_invalid_api_key(self, mock_app_service):
        """Endpoints must return 401 when an incorrect API key is provided."""
        with patch.dict(os.environ, {"PTB_API_KEY": "super-secret-key-123"}, clear=False):
            app = create_app(application_service=mock_app_service)
            client = TestClient(app)

            resp = client.get("/api/tasks", headers={"X-PTB-API-KEY": "wrong-key"})
            assert resp.status_code == 401
            assert "Invalid API key" in resp.json()["detail"]

    def test_endpoints_allow_valid_api_key(self, mock_app_service):
        """Endpoints must return 200 when the exact matching X-PTB-API-KEY is provided."""
        with patch.dict(os.environ, {"PTB_API_KEY": "super-secret-key-123"}, clear=False):
            app = create_app(application_service=mock_app_service)
            client = TestClient(app)

            resp = client.get("/api/tasks", headers={"X-PTB-API-KEY": "super-secret-key-123"})
            assert resp.status_code == 200

    def test_bearer_authorization_header_accepted(self, mock_app_service):
        """Endpoints accept Authorization: Bearer <token> as an alternate authentication mechanism."""
        with patch.dict(os.environ, {"PTB_API_KEY": "super-secret-key-123"}, clear=False):
            app = create_app(application_service=mock_app_service)
            client = TestClient(app)

            resp = client.get("/api/tasks", headers={"Authorization": "Bearer super-secret-key-123"})
            assert resp.status_code == 200

    def test_unauthenticated_dev_mode_when_api_key_unset(self, mock_app_service):
        """When PTB_API_KEY is not set or empty, requests are allowed (dev mode) with warning log."""
        env_without_key = os.environ.copy()
        env_without_key.pop("PTB_API_KEY", None)

        with patch.dict(os.environ, env_without_key, clear=True):
            app = create_app(application_service=mock_app_service)
            client = TestClient(app)

            resp = client.get("/api/tasks")
            assert resp.status_code == 200


class TestCORSWhitelist:
    """Tests for P0-1: CORS Whitelist and Wildcard Removal."""

    def test_cors_allows_whitelisted_origin(self, mock_app_service):
        """Requests from whitelisted origins receive the Access-Control-Allow-Origin header."""
        with patch.dict(os.environ, {"PTB_CORS_ORIGINS": "http://127.0.0.1:3000,http://localhost:3000"}, clear=False):
            app = create_app(application_service=mock_app_service)
            client = TestClient(app)

            resp = client.get("/health", headers={"Origin": "http://localhost:3000"})
            assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"
            assert resp.headers.get("access-control-allow-credentials") == "true"

    def test_cors_rejects_unwhitelisted_origin(self, mock_app_service):
        """Requests from unknown/unwhitelisted origins must NOT receive Access-Control-Allow-Origin."""
        with patch.dict(os.environ, {"PTB_CORS_ORIGINS": "http://127.0.0.1:3000,http://localhost:3000"}, clear=False):
            app = create_app(application_service=mock_app_service)
            client = TestClient(app)

            resp = client.get("/health", headers={"Origin": "http://malicious-attacker.com"})
            assert resp.headers.get("access-control-allow-origin") is None

    def test_cors_preflight_options_for_whitelisted_origin(self, mock_app_service):
        """OPTIONS preflight requests from whitelisted origins receive 200 with allowed methods and headers."""
        with patch.dict(os.environ, {"PTB_CORS_ORIGINS": "http://127.0.0.1:3000"}, clear=False):
            app = create_app(application_service=mock_app_service)
            client = TestClient(app)

            resp = client.options(
                "/api/tasks",
                headers={
                    "Origin": "http://127.0.0.1:3000",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "X-PTB-API-KEY,Content-Type",
                },
            )
            assert resp.status_code == 200
            assert resp.headers.get("access-control-allow-origin") == "http://127.0.0.1:3000"
            allowed_headers = resp.headers.get("access-control-allow-headers", "").lower()
            assert "x-ptb-api-key" in allowed_headers or "*" in allowed_headers


class TestDestructiveAndProcessEndpointsSecurity:
    """Tests for P0-5 (Destructive reset auth) and P1-6 (Unauthenticated process execution)."""

    def test_database_reset_requires_api_key_when_configured(self, mock_app_service):
        """POST /api/database/reset must return 401 if API key is not provided."""
        with patch.dict(os.environ, {"PTB_API_KEY": "app-key-xyz", "PTB_ENABLE_DESTRUCTIVE_API": "true"}, clear=False):
            app = create_app(application_service=mock_app_service)
            client = TestClient(app)

            resp = client.post("/api/database/reset")
            assert resp.status_code == 401

    def test_database_reset_requires_admin_key_when_configured(self, mock_app_service):
        """POST /api/database/reset must return 403 if PTB_ADMIN_KEY is configured but header is missing/invalid."""
        with patch.dict(
            os.environ,
            {
                "PTB_API_KEY": "app-key-xyz",
                "PTB_ADMIN_KEY": "admin-super-key",
                "PTB_ENABLE_DESTRUCTIVE_API": "true",
            },
            clear=False,
        ):
            app = create_app(application_service=mock_app_service)
            client = TestClient(app)

            # Valid API key, but missing admin key
            resp_no_admin = client.post(
                "/api/database/reset",
                headers={"X-PTB-API-KEY": "app-key-xyz"},
            )
            assert resp_no_admin.status_code == 403
            assert "Admin authentication required" in resp_no_admin.json()["detail"]

            # Valid API key, but wrong admin key
            resp_wrong_admin = client.post(
                "/api/database/reset",
                headers={"X-PTB-API-KEY": "app-key-xyz", "X-PTB-ADMIN-KEY": "wrong-admin-key"},
            )
            assert resp_wrong_admin.status_code == 403

    def test_database_reset_succeeds_with_valid_api_and_admin_keys(self, mock_app_service):
        """POST /api/database/reset succeeds when both valid API key and valid admin key are provided."""
        with patch.dict(
            os.environ,
            {
                "PTB_API_KEY": "app-key-xyz",
                "PTB_ADMIN_KEY": "admin-super-key",
                "PTB_ENABLE_DESTRUCTIVE_API": "true",
            },
            clear=False,
        ):
            with patch("ptb_database.setup_all.setup_neo4j", new=AsyncMock(return_value=True)):
                app = create_app(application_service=mock_app_service)
                client = TestClient(app)

                resp = client.post(
                    "/api/database/reset",
                    headers={"X-PTB-API-KEY": "app-key-xyz", "X-PTB-ADMIN-KEY": "admin-super-key"},
                )
                assert resp.status_code == 200
                assert resp.json()["status"] == "success"
                assert resp.json()["constraints_restored"] is True

    def test_start_login_and_save_cookie_require_auth(self, mock_app_service):
        """POST /api/auth/start-login and POST /api/auth/save-cookie (P1-6) require authentication."""
        with patch.dict(os.environ, {"PTB_API_KEY": "app-key-xyz"}, clear=False):
            app = create_app(application_service=mock_app_service)
            client = TestClient(app)

            # Unauthenticated start-login rejected
            resp_login = client.post("/api/auth/start-login", json={"service": "teams"})
            assert resp_login.status_code == 401

            # Unauthenticated save-cookie rejected
            resp_cookie = client.post("/api/auth/save-cookie", json={"cookie_data": []})
            assert resp_cookie.status_code == 401
