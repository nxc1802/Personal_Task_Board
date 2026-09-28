"""Tests for Layer 1A Playwright Network Interceptors."""

from datetime import datetime
import json
import pytest

from ptb_contracts import RawEventRecord, SourceType
from ptb_acquisition.playwright.session import SessionManager
from ptb_acquisition.playwright.teams_interceptor import TeamsNetworkInterceptor
from ptb_acquisition.playwright.outlook_interceptor import OutlookNetworkInterceptor
from ptb_acquisition.queue import LocalIngestionQueue


def test_teams_interceptor_parse_payload():
    mock_payload = {
        "messages": [
            {
                "id": "1727391000123",
                "conversationId": "19:meeting_chat_fpt@thread.v2",
                "composeTime": "2026-09-27T02:00:00.000Z",
                "from": {
                    "id": "user-cuong-001",
                    "displayName": "Nguyen Cuong"
                },
                "body": {
                    "contentType": "html",
                    "content": "<p>Để em check issue này trước 5h chiều nhé.</p>"
                }
            }
        ]
    }

    interceptor = TeamsNetworkInterceptor(tenant_id="fpt-internal")
    records = interceptor.parse_payload(mock_payload)

    assert len(records) == 1
    rec = records[0]
    assert rec.source_type == SourceType.MS_TEAMS_WEB
    assert rec.external_id == "1727391000123"
    assert rec.author_display_name == "Nguyen Cuong"
    assert "trước 5h chiều" in rec.raw_payload["body"]["content"]
    assert rec.idempotency_key is not None
    assert rec.tenant_id == "fpt-internal"


def test_outlook_interceptor_parse_payload():
    mock_payload = {
        "value": [
            {
                "id": "AAMkADhkOW...",
                "subject": "Review kiến trúc Personal Task Board",
                "conversationId": "AAQkADhkOW_conv...",
                "receivedDateTime": "2026-09-27T02:30:00Z",
                "from": {
                    "emailAddress": {
                        "name": "Huy Nguyen",
                        "address": "huy@partner.com"
                    }
                },
                "bodyPreview": "Nhờ anh Cường review giúp em phần contract C12.",
                "body": {
                    "contentType": "Text",
                    "content": "Nhờ anh Cường review giúp em phần contract C12 trước thứ Hai."
                }
            }
        ]
    }

    interceptor = OutlookNetworkInterceptor(tenant_id="client-tenant")
    records = interceptor.parse_payload(mock_payload)

    assert len(records) == 1
    rec = records[0]
    assert rec.source_type == SourceType.MS_OUTLOOK_WEB
    assert rec.external_id == "AAMkADhkOW..."
    assert rec.author_external_id == "huy@partner.com"
    assert rec.author_display_name == "Huy Nguyen"
    assert "review giúp em" in rec.raw_payload["body"]["content"]
    assert rec.tenant_id == "client-tenant"


def test_session_manager_validation(tmp_path):
    storage_file = tmp_path / "storage_state.json"
    mgr = SessionManager(str(storage_file))

    # Khi chưa có file
    assert mgr.has_valid_session() is False

    # Khi có file nhưng rỗng
    storage_file.write_text(json.dumps({"cookies": []}), encoding="utf-8")
    assert mgr.has_valid_session() is False

    # Khi có cookie
    storage_file.write_text(json.dumps({"cookies": [{"name": "AuthToken", "value": "xyz"}]}), encoding="utf-8")
    assert mgr.has_valid_session() is True
    assert mgr.is_auth_error(401) is True
    assert mgr.is_auth_error(200) is False
