"""Unit & regression tests for Host Auth Helper security controls (Review 2 P0)."""

import json
import os
import sys
import threading
from http.server import HTTPServer
from urllib import request as urllib_request
from urllib.error import HTTPError
import pytest

from scripts.host_auth_helper import HostAuthHandler


@pytest.fixture(scope="module")
def helper_server():
    """Spin up an ephemeral HostAuthHandler on loopback."""
    server = HTTPServer(("127.0.0.1", 0), HostAuthHandler)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://{host}:{port}"
    server.shutdown()
    server.server_close()


def test_get_health_healthy(helper_server):
    req = urllib_request.Request(f"{helper_server}/health")
    with urllib_request.urlopen(req) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data.get("status") == "HEALTHY"
        assert data.get("service") == "ptb-host-auth-helper"


def test_get_login_rejected_with_405(helper_server):
    """P0 Security: GET on state-mutating /login must be rejected with 405."""
    req = urllib_request.Request(f"{helper_server}/login?service=teams")
    with pytest.raises(HTTPError) as exc_info:
        urllib_request.urlopen(req)
    assert exc_info.value.code == 405
    err_body = json.loads(exc_info.value.read().decode("utf-8"))
    assert err_body.get("error") == "Method Not Allowed"


def test_get_clear_rejected_with_405(helper_server):
    """P0 Security: GET on state-mutating /clear must be rejected with 405."""
    req = urllib_request.Request(f"{helper_server}/clear")
    with pytest.raises(HTTPError) as exc_info:
        urllib_request.urlopen(req)
    assert exc_info.value.code == 405
    err_body = json.loads(exc_info.value.read().decode("utf-8"))
    assert err_body.get("error") == "Method Not Allowed"


def test_post_clear_requires_confirmation(helper_server):
    """P0 Security: POST /clear without explicit confirmation body must return 400."""
    # 1. No body
    req_no_body = urllib_request.Request(
        f"{helper_server}/clear",
        data=b"",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(HTTPError) as exc_info:
        urllib_request.urlopen(req_no_body)
    assert exc_info.value.code == 400

    # 2. Body with confirm=False
    req_false = urllib_request.Request(
        f"{helper_server}/clear",
        data=json.dumps({"confirm": False}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(HTTPError) as exc_info:
        urllib_request.urlopen(req_false)
    assert exc_info.value.code == 400

    # 3. Body with confirm=True
    req_confirmed = urllib_request.Request(
        f"{helper_server}/clear",
        data=json.dumps({"confirm": True}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib_request.urlopen(req_confirmed) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data.get("status") == "success"


def test_cors_origin_restriction(helper_server):
    """P0 Security: CORS must not return wildcard * for arbitrary untrusted origin."""
    # 1. Trusted origin (localhost)
    req_local = urllib_request.Request(
        f"{helper_server}/health",
        headers={"Origin": "http://localhost:3000"},
    )
    with urllib_request.urlopen(req_local) as resp:
        assert resp.headers.get("Access-Control-Allow-Origin") == "http://localhost:3000"

    # 2. Untrusted origin
    req_untrusted = urllib_request.Request(
        f"{helper_server}/health",
        headers={"Origin": "http://evil-attacker.com"},
    )
    with urllib_request.urlopen(req_untrusted) as resp:
        # Must not be * or evil-attacker.com
        allow_origin = resp.headers.get("Access-Control-Allow-Origin")
        assert allow_origin != "*"
        assert allow_origin != "http://evil-attacker.com"


def test_auth_enforcement_when_api_key_set(helper_server, monkeypatch):
    """P0 Security: When PTB_API_KEY is configured in env, require valid token."""
    monkeypatch.setenv("PTB_API_KEY", "super-secret-key-123")

    # 1. Unauthenticated request to /status
    req_unauth = urllib_request.Request(f"{helper_server}/status")
    with pytest.raises(HTTPError) as exc_info:
        urllib_request.urlopen(req_unauth)
    assert exc_info.value.code == 401

    # 2. Authenticated request with X-PTB-API-KEY
    req_auth = urllib_request.Request(
        f"{helper_server}/status",
        headers={"X-PTB-API-KEY": "super-secret-key-123"},
    )
    with urllib_request.urlopen(req_auth) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert "status" in data
