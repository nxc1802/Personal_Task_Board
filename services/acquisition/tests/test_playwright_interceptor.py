"""Tests for Layer 1A Playwright Network Interceptors, Session Lifecycle & Health."""

import asyncio
from datetime import datetime, timezone
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from ptb_contracts import BugCode, RawEventRecord, SourceType
from ptb_acquisition.pipeline import AcquisitionPipeline
from tests.support.test_doubles import InMemoryRawEventRepository
from ptb_acquisition.playwright.session import (
    SessionHealthState,
    SessionManager,
    transition_session_state,
    verify_authenticated_session,
)
from ptb_acquisition.playwright.teams_interceptor import TeamsNetworkInterceptor
from ptb_acquisition.playwright.outlook_interceptor import OutlookNetworkInterceptor
from ptb_acquisition.playwright.runner import PlaywrightOrchestrator
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


@pytest.mark.asyncio
async def test_teams_interceptor_persist_first_with_pipeline():
    """Kiểm tra TeamsNetworkInterceptor gọi trực tiếp pipeline.ingest_event và chỉ capture khi persist thành công."""
    mock_pipeline = MagicMock(spec=AcquisitionPipeline)
    mock_pipeline.ingest_event = AsyncMock(return_value=True)

    interceptor = TeamsNetworkInterceptor(pipeline=mock_pipeline, tenant_id="test-teams")

    mock_response = MagicMock()
    mock_response.url = "https://teams.microsoft.com/api/chats/19:test/messages"
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={
        "messages": [
            {"id": "msg-001", "body": {"content": "Persist first check"}}
        ]
    })

    records = await interceptor.handle_response(mock_response)
    assert len(records) == 1
    assert len(interceptor.captured_records) == 1
    mock_pipeline.ingest_event.assert_awaited_once()

    # Kiểm tra trường hợp pipeline lưu thất bại -> không ghi nhận là captured
    mock_pipeline.ingest_event = AsyncMock(return_value=False)
    mock_response.json = AsyncMock(return_value={
        "messages": [
            {"id": "msg-002", "body": {"content": "Persist fail check"}}
        ]
    })
    failed_records = await interceptor.handle_response(mock_response)
    assert len(failed_records) == 0
    assert len(interceptor.captured_records) == 1


@pytest.mark.asyncio
async def test_teams_interceptor_persist_first_with_repo():
    """Kiểm tra TeamsNetworkInterceptor cắm trực tiếp raw_event_repo."""
    mock_repo = MagicMock()
    mock_repo.persist_raw_event = AsyncMock(return_value="evt-123")

    interceptor = TeamsNetworkInterceptor(raw_event_repo=mock_repo, tenant_id="test-teams")

    mock_response = MagicMock()
    mock_response.url = "https://teams.microsoft.com/api/chats/19:test/messages"
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={
        "messages": [
            {"id": "msg-101", "body": {"content": "Direct repo persist"}}
        ]
    })

    records = await interceptor.handle_response(mock_response)
    assert len(records) == 1
    assert len(interceptor.captured_records) == 1
    mock_repo.persist_raw_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_teams_interceptor_auth_expired():
    """Kiểm tra khi nhận status 401/403 thì interceptor cảnh báo, log PTB-L1-001 và chuyển AUTH_EXPIRED."""
    interceptor = TeamsNetworkInterceptor()
    mock_response = MagicMock()
    mock_response.url = "https://teams.microsoft.com/api/chats/19:test/messages"
    mock_response.status = 401

    with patch("ptb_acquisition.playwright.teams_interceptor.log_bug") as mock_log:
        records = await interceptor.handle_response(mock_response)

    assert len(records) == 0
    assert len(interceptor.captured_records) == 0
    assert interceptor.session_state == SessionHealthState.AUTH_EXPIRED
    mock_log.assert_called_once_with(
        BugCode.PTB_L1_001,
        subsystem="playwright",
        severity="ERROR",
        message="Microsoft session authentication expired (HTTP 401/403 or login redirect)",
        context={"source_type": SourceType.MS_TEAMS_WEB, "url": mock_response.url, "status": 401},
        source_type=SourceType.MS_TEAMS_WEB,
        tenant_id=interceptor.tenant_id,
    )

    # Khi đã AUTH_EXPIRED, không lặp lại capture
    mock_msg_resp = MagicMock()
    mock_msg_resp.url = "https://teams.microsoft.com/api/chats/19:test/messages"
    mock_msg_resp.status = 200
    ignored = await interceptor.handle_response(mock_msg_resp)
    assert len(ignored) == 0


@pytest.mark.asyncio
async def test_outlook_interceptor_persist_first_with_pipeline():
    """Kiểm tra OutlookNetworkInterceptor gọi trực tiếp pipeline.ingest_event."""
    mock_pipeline = MagicMock(spec=AcquisitionPipeline)
    mock_pipeline.ingest_event = AsyncMock(return_value=True)

    interceptor = OutlookNetworkInterceptor(pipeline=mock_pipeline, tenant_id="test-outlook")

    mock_response = MagicMock()
    mock_response.url = "https://outlook.office.com/api/v2.0/me/messages"
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={
        "value": [
            {"id": "email-001", "subject": "Test Outlook Persist", "body": {"content": "Content here"}}
        ]
    })

    records = await interceptor.handle_response(mock_response)
    assert len(records) == 1
    assert len(interceptor.captured_records) == 1
    mock_pipeline.ingest_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_pipeline_persist_first_order_and_queue():
    """Kiểm tra pipeline.ingest_event ghi trực tiếp vào repo trước, sau đó mới đẩy vào queue."""
    call_order = []

    mock_repo = MagicMock()
    async def fake_persist(record):
        call_order.append("repo")
        return "saved-id"
    mock_repo.persist_raw_event = AsyncMock(side_effect=fake_persist)

    queue = LocalIngestionQueue()
    original_put = queue.put
    async def monitored_put(record):
        call_order.append("queue")
        return await original_put(record)
    queue.put = monitored_put

    pipeline = AcquisitionPipeline(queue=queue, raw_event_repo=mock_repo)

    test_record = RawEventRecord(
        id="test-order-001",
        tenant_id="tenant-test",
        source_type=SourceType.MS_TEAMS_WEB,
        external_id="ext-order-1",
        idempotency_key="idemp-order-1",
        author_external_id="author-1",
        conversation_or_project_id="conv-1",
        event_timestamp=datetime.now(timezone.utc),
        raw_payload={"text": "hello"},
        normalized_text="hello",
    )

    success = await pipeline.ingest_event(test_record)
    assert success is True
    assert call_order == ["repo", "queue"]
    assert queue.qsize == 1

    # Nếu repo thất bại, không được đẩy vào queue
    call_order.clear()
    async def fake_fail_persist(record):
        call_order.append("repo")
        return False
    mock_repo.persist_raw_event = AsyncMock(side_effect=fake_fail_persist)
    failing_record = RawEventRecord(
        id="test-order-002",
        tenant_id="tenant-test",
        source_type=SourceType.MS_TEAMS_WEB,
        external_id="ext-order-2",
        idempotency_key="idemp-order-2",
        author_external_id="author-2",
        conversation_or_project_id="conv-2",
        event_timestamp=datetime.now(timezone.utc),
        raw_payload={"text": "fail"},
        normalized_text="fail",
    )
    success = await pipeline.ingest_event(failing_record)
    assert success is False
    assert call_order == ["repo"]
    assert queue.qsize == 1  # Không tăng thêm trong queue


def test_session_manager_validation(tmp_path):
    storage_file = tmp_path / "storage_state.json"
    mgr = SessionManager(str(storage_file))

    # 1. Khi chưa có file -> UNCONFIGURED
    assert mgr.validate_session() == SessionHealthState.UNCONFIGURED
    assert mgr.has_valid_session() is False

    # 2. Khi có file nhưng rỗng -> LOGIN_REQUIRED
    storage_file.write_text(json.dumps({"cookies": []}), encoding="utf-8")
    assert mgr.validate_session() == SessionHealthState.LOGIN_REQUIRED
    assert mgr.has_valid_session() is False

    # 3. Khi có cookie nhưng toàn bộ đã hết hạn -> AUTH_EXPIRED
    past_timestamp = 1000000000.0  # Năm 2001
    expired_cookies = [
        {"name": "AuthToken", "value": "old", "expires": past_timestamp}
    ]
    storage_file.write_text(json.dumps({"cookies": expired_cookies}), encoding="utf-8")
    assert mgr.validate_session() == SessionHealthState.AUTH_EXPIRED
    assert mgr.has_valid_session() is False

    # 4. Khi có cookie còn hạn -> STARTING
    future_timestamp = datetime.now(timezone.utc).timestamp() + 86400
    valid_cookies = [
        {"name": "AuthToken", "value": "xyz", "expires": future_timestamp}
    ]
    storage_file.write_text(json.dumps({"cookies": valid_cookies}), encoding="utf-8")
    assert mgr.validate_session() == SessionHealthState.STARTING
    assert mgr.has_valid_session() is True
    assert mgr.is_auth_error(401) is True
    assert mgr.is_auth_error(200) is False


@pytest.mark.asyncio
async def test_verify_authenticated_session_redirect_login():
    """Kiểm tra redirect sang login.microsoftonline.com được đánh dấu là AUTH_EXPIRED."""
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)

    mock_page.goto = AsyncMock(return_value=None)
    mock_page.url = "https://login.microsoftonline.com/common/oauth2/authorize?client_id=123"
    mock_page.close = AsyncMock()

    state = await verify_authenticated_session(mock_context)
    assert state == SessionHealthState.AUTH_EXPIRED
    mock_page.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_verify_authenticated_session_http_401():
    """Kiểm tra HTTP 401/403 được đánh dấu là AUTH_EXPIRED."""
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)

    mock_response = MagicMock()
    mock_response.status = 401
    mock_page.goto = AsyncMock(return_value=mock_response)
    mock_page.url = "https://teams.microsoft.com"
    mock_page.close = AsyncMock()

    state = await verify_authenticated_session(mock_context)
    assert state == SessionHealthState.AUTH_EXPIRED


@pytest.mark.asyncio
async def test_verify_authenticated_session_healthy():
    """Kiểm tra session sống và trang authenticated load thành công -> HEALTHY."""
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)

    mock_response = MagicMock()
    mock_response.status = 200
    mock_page.goto = AsyncMock(return_value=mock_response)
    mock_page.url = "https://teams.microsoft.com/v2/"
    mock_page.close = AsyncMock()

    state = await verify_authenticated_session(mock_context)
    assert state == SessionHealthState.HEALTHY


@pytest.mark.asyncio
async def test_verify_authenticated_session_degraded():
    """Kiểm tra HTTP 500 hoặc timeout trả về DEGRADED."""
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)

    mock_response = MagicMock()
    mock_response.status = 503
    mock_page.goto = AsyncMock(return_value=mock_response)
    mock_page.url = "https://teams.microsoft.com"
    mock_page.close = AsyncMock()

    state = await verify_authenticated_session(mock_context)
    assert state == SessionHealthState.DEGRADED


@pytest.mark.asyncio
async def test_bootstrap_capture_sweep():
    """Kiểm tra runner thực hiện Bootstrap Capture Sweep trên Teams & Outlook."""
    orchestrator = PlaywrightOrchestrator(headless=True)

    mock_teams_page = MagicMock()
    mock_teams_page.wait_for_timeout = AsyncMock()
    mock_teams_page.wait_for_selector = AsyncMock()
    mock_teams_page.evaluate = AsyncMock()

    mock_outlook_page = MagicMock()
    mock_outlook_page.wait_for_timeout = AsyncMock()
    mock_outlook_page.wait_for_selector = AsyncMock()
    mock_outlook_page.evaluate = AsyncMock()

    # Thêm bản ghi giả lập được interceptor bắt được trong quá trình sweep
    test_rec = RawEventRecord(
        id="sweep-rec-01",
        tenant_id="tenant-test",
        source_type=SourceType.MS_TEAMS_WEB,
        external_id="ext-sweep-1",
        idempotency_key="idemp-sweep-1",
        author_external_id="sweep-author",
        conversation_or_project_id="sweep-conv",
        event_timestamp=datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc),
        raw_payload={},
        normalized_text="Sweep event",
    )
    orchestrator.teams_interceptor.captured_records.append(test_rec)

    stats = await orchestrator.run_bootstrap_sweep(
        teams_page=mock_teams_page,
        outlook_page=mock_outlook_page,
        max_scrolls=2,
    )

    assert stats["status"] == "COMPLETED"
    assert stats["total_captured_events"] == 1
    assert stats["oldest_captured_event"] is not None
    assert stats["newest_captured_event"] is not None

    health = orchestrator.get_health()
    assert health["bootstrap_sweep"] == "COMPLETED"
    assert health["teams_captured"] == 1


@pytest.mark.asyncio
async def test_teams_interceptor_redirect_login_triggers_auth_expired():
    """Kiểm tra khi interceptor gặp redirect login.microsoftonline.com thì kích hoạt PTB-L1-001 và AUTH_EXPIRED."""
    interceptor = TeamsNetworkInterceptor()
    mock_response = MagicMock()
    mock_response.url = "https://login.microsoftonline.com/common/oauth2/authorize?client_id=xyz"
    mock_response.status = 200

    with patch("ptb_acquisition.playwright.teams_interceptor.log_bug") as mock_log:
        records = await interceptor.handle_response(mock_response)

    assert len(records) == 0
    assert interceptor.session_state == SessionHealthState.AUTH_EXPIRED
    mock_log.assert_called_once()
    assert mock_log.call_args[0][0] == BugCode.PTB_L1_001
    assert mock_log.call_args[1]["severity"] == "ERROR"


@pytest.mark.asyncio
async def test_outlook_interceptor_auth_expired():
    """Kiểm tra khi nhận status 401/403 thì Outlook interceptor log PTB-L1-001 và chuyển AUTH_EXPIRED."""
    interceptor = OutlookNetworkInterceptor()
    mock_response = MagicMock()
    mock_response.url = "https://outlook.office.com/api/v2.0/me/messages"
    mock_response.status = 403

    with patch("ptb_acquisition.playwright.outlook_interceptor.log_bug") as mock_log:
        records = await interceptor.handle_response(mock_response)

    assert len(records) == 0
    assert len(interceptor.captured_records) == 0
    assert interceptor.session_state == SessionHealthState.AUTH_EXPIRED
    mock_log.assert_called_once_with(
        BugCode.PTB_L1_001,
        subsystem="playwright",
        severity="ERROR",
        message="Microsoft session authentication expired (HTTP 401/403 or login redirect)",
        context={"source_type": SourceType.MS_OUTLOOK_WEB, "url": mock_response.url, "status": 403},
        source_type=SourceType.MS_OUTLOOK_WEB,
        tenant_id=interceptor.tenant_id,
    )


@pytest.mark.asyncio
async def test_outlook_interceptor_redirect_login_triggers_auth_expired():
    """Kiểm tra Outlook gặp redirect login thì log PTB-L1-001 và chuyển AUTH_EXPIRED."""
    interceptor = OutlookNetworkInterceptor()
    mock_response = MagicMock()
    mock_response.url = "https://login.live.com/oauth20_authorize.srf"
    mock_response.status = 200

    with patch("ptb_acquisition.playwright.outlook_interceptor.log_bug") as mock_log:
        records = await interceptor.handle_response(mock_response)

    assert len(records) == 0
    assert interceptor.session_state == SessionHealthState.AUTH_EXPIRED
    mock_log.assert_called_once()
    assert mock_log.call_args[0][0] == BugCode.PTB_L1_001


@pytest.mark.asyncio
async def test_teams_interceptor_persist_failure_logs_bug_and_degraded():
    """Kiểm tra khi persist vào raw_event_repo gặp exception thì kích hoạt PTB-L1-002 và chuyển DEGRADED."""
    mock_repo = MagicMock()
    mock_repo.persist_raw_event = AsyncMock(side_effect=RuntimeError("Authoritative DB write failed"))

    interceptor = TeamsNetworkInterceptor(raw_event_repo=mock_repo)
    mock_response = MagicMock()
    mock_response.url = "https://teams.microsoft.com/api/chats/19:test/messages"
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={
        "messages": [
            {"id": "msg-err-1", "body": {"content": "Test error msg"}}
        ]
    })

    with patch("ptb_acquisition.playwright.teams_interceptor.log_bug") as mock_log:
        records = await interceptor.handle_response(mock_response)

    assert len(records) == 0
    assert len(interceptor.captured_records) == 0
    assert interceptor.session_state == SessionHealthState.DEGRADED
    mock_log.assert_called_once()
    assert mock_log.call_args[0][0] == BugCode.PTB_L1_002
    assert "Capture or persist failed" in mock_log.call_args[1]["message"]


@pytest.mark.asyncio
async def test_outlook_interceptor_persist_failure_logs_bug_and_degraded():
    """Kiểm tra khi persist email vào raw_event_repo gặp exception thì kích hoạt PTB-L1-002 và chuyển DEGRADED."""
    mock_repo = MagicMock()
    mock_repo.persist_raw_event = AsyncMock(side_effect=RuntimeError("Disk I/O error"))

    interceptor = OutlookNetworkInterceptor(raw_event_repo=mock_repo)
    mock_response = MagicMock()
    mock_response.url = "https://outlook.office.com/api/v2.0/me/messages"
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={
        "value": [
            {"id": "email-err-1", "subject": "Test error email", "body": {"content": "Error email content"}}
        ]
    })

    with patch("ptb_acquisition.playwright.outlook_interceptor.log_bug") as mock_log:
        records = await interceptor.handle_response(mock_response)

    assert len(records) == 0
    assert len(interceptor.captured_records) == 0
    assert interceptor.session_state == SessionHealthState.DEGRADED
    mock_log.assert_called_once()
    assert mock_log.call_args[0][0] == BugCode.PTB_L1_002
    assert "Capture or persist failed" in mock_log.call_args[1]["message"]


@pytest.mark.asyncio
async def test_orchestrator_state_propagation_auth_expired():
    """Kiểm tra lỗi 401 trên interceptor lan truyền về orchestrator làm AUTH_EXPIRED và dừng runner."""
    orchestrator = PlaywrightOrchestrator()
    orchestrator.health_state = SessionHealthState.HEALTHY
    orchestrator._running = True

    mock_response = MagicMock()
    mock_response.url = "https://teams.microsoft.com/api/chats/19:test/messages"
    mock_response.status = 401

    await orchestrator.teams_interceptor.handle_response(mock_response)

    assert orchestrator.session_state == SessionHealthState.AUTH_EXPIRED
    assert orchestrator.health_state == SessionHealthState.AUTH_EXPIRED
    assert orchestrator.outlook_interceptor.session_state == SessionHealthState.AUTH_EXPIRED
    assert orchestrator._running is False


@pytest.mark.asyncio
async def test_orchestrator_state_propagation_degraded():
    """Kiểm tra lỗi persist lan truyền về orchestrator chuyển DEGRADED và chuyển AUTH_EXPIRED khi 401."""
    mock_repo = MagicMock()
    mock_repo.persist_raw_event = AsyncMock(side_effect=RuntimeError("DB timeout"))

    orchestrator = PlaywrightOrchestrator(raw_event_repo=mock_repo)
    orchestrator.health_state = SessionHealthState.HEALTHY

    mock_response = MagicMock()
    mock_response.url = "https://teams.microsoft.com/api/chats/19:test/messages"
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={
        "messages": [
            {"id": "msg-deg-1", "body": {"content": "Degraded check"}}
        ]
    })

    await orchestrator.teams_interceptor.handle_response(mock_response)

    assert orchestrator.session_state == SessionHealthState.DEGRADED
    assert orchestrator.teams_interceptor.session_state == SessionHealthState.DEGRADED

    # Tiếp theo nếu nhận 401, chuyển sang AUTH_EXPIRED
    mock_401 = MagicMock()
    mock_401.url = "https://teams.microsoft.com/api/chats/19:test/messages"
    mock_401.status = 401
    await orchestrator.teams_interceptor.handle_response(mock_401)
    assert orchestrator.session_state == SessionHealthState.AUTH_EXPIRED

    # Khi đã AUTH_EXPIRED, lỗi persist khác không được ghi đè về DEGRADED
    await orchestrator.teams_interceptor.handle_response(mock_response)
    assert orchestrator.session_state == SessionHealthState.AUTH_EXPIRED


@pytest.mark.asyncio
async def test_orchestrator_sweep_aborts_on_auth_expired():
    """Kiểm tra sweep không loop gửi request khi session ở trạng thái AUTH_EXPIRED."""
    orchestrator = PlaywrightOrchestrator()
    orchestrator.health_state = SessionHealthState.AUTH_EXPIRED

    mock_teams_page = MagicMock()
    mock_teams_page.wait_for_timeout = AsyncMock()
    mock_teams_page.evaluate = AsyncMock()

    mock_outlook_page = MagicMock()
    mock_outlook_page.wait_for_timeout = AsyncMock()
    mock_outlook_page.evaluate = AsyncMock()

    stats = await orchestrator.run_bootstrap_sweep(
        teams_page=mock_teams_page,
        outlook_page=mock_outlook_page,
        max_scrolls=5,
    )

    assert stats["teams_events_captured"] == 0
    assert stats["outlook_events_captured"] == 0
    mock_teams_page.evaluate.assert_not_called()
    mock_outlook_page.evaluate.assert_not_called()


# =============================================================================
# W2-03: OUTLOOK MAIL ADAPTER CONTRACT & PIPELINE TESTS
# =============================================================================

from datetime import timedelta
from pathlib import Path
from ptb_contracts.l1_acquisition import IngestionCheckpointRecord
from ptb_acquisition.playwright.session import DEFAULT_STORAGE_PATH
from ptb_acquisition.adapters.outlook_adapter import (
    OutlookMailAdapter,
    clean_email_content,
    extract_attachments_metadata,
    sanitize_email_payload,
)
from ptb_acquisition.adapters.base import (
    AdapterAuthenticationError,
    AdapterRateLimitError,
    AdapterTransientError,
)
from tests.support.test_doubles import (
    InMemoryCheckpointRepository,
    InMemoryRawEventRepository,
)


@pytest.mark.asyncio
async def test_outlook_adapter_discover_folders():
    """Kiểm tra discover() trả về danh sách folder streams."""
    # 1. Folders cấu hình mặc định
    adapter = OutlookMailAdapter(tenant_id="tenant-discover", folders=["Inbox", "SentItems", "Archive"])
    streams = await adapter.discover()
    assert len(streams) == 3
    stream_ids = [s["stream_id"] for s in streams]
    assert "Inbox" in stream_ids
    assert "SentItems" in stream_ids
    assert "Archive" in stream_ids
    assert all(s["source_type"] == SourceType.MS_OUTLOOK.value for s in streams)

    # 2. Dynamic discovery khi Graph API trả về danh sách folders
    async def fake_fetcher(url, headers=None, body=None):
        if "mailFolders" in url:
            return {
                "value": [
                    {"displayName": "Inbox", "id": "fld-inbox", "unreadItemCount": 5, "totalItemCount": 100},
                    {"displayName": "Projects", "id": "fld-proj", "unreadItemCount": 2, "totalItemCount": 45},
                ]
            }
        return {}

    dynamic_adapter = OutlookMailAdapter(
        tenant_id="tenant-dynamic",
        graph_token="valid-token-123",
        http_fetcher=fake_fetcher,
    )
    dyn_streams = await dynamic_adapter.discover()
    assert len(dyn_streams) == 2
    assert dyn_streams[0]["stream_id"] == "Inbox"
    assert dyn_streams[1]["stream_id"] == "Projects"


@pytest.mark.asyncio
async def test_outlook_adapter_backfill_pagination():
    """Kiểm tra backfill() phân trang qua @odata.nextLink và emit RawEventRecord chuẩn."""
    page1 = {
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages?$top=50&$skip=50",
        "value": [
            {
                "id": "msg-pg1-001",
                "conversationId": "conv-thread-1",
                "subject": "Email Page 1 Task 1",
                "receivedDateTime": "2026-09-29T10:00:00Z",
                "body": {"contentType": "text", "content": "Nội dung email page 1"},
                "from": {"emailAddress": {"name": "User One", "address": "user1@company.com"}},
            }
        ]
    }
    page2 = {
        "value": [
            {
                "id": "msg-pg2-002",
                "conversationId": "conv-thread-2",
                "subject": "Email Page 2 Task 2",
                "receivedDateTime": "2026-09-29T11:00:00Z",
                "body": {"contentType": "text", "content": "Nội dung email page 2"},
                "from": {"emailAddress": {"name": "User Two", "address": "user2@company.com"}},
            }
        ]
    }

    async def paged_fetcher(url, headers=None, body=None):
        if "$skip=50" in url:
            return page2
        return page1

    adapter = OutlookMailAdapter(
        tenant_id="tenant-paged",
        graph_token="test-token",
        http_fetcher=paged_fetcher,
    )

    records = []
    async for rec in adapter.backfill("Inbox"):
        records.append(rec)

    assert len(records) == 2
    assert records[0].external_id == "msg-pg1-001"
    assert records[1].external_id == "msg-pg2-002"
    assert records[0].tenant_id == "tenant-paged"
    assert records[1].tenant_id == "tenant-paged"
    assert records[0].source_type == SourceType.MS_OUTLOOK
    assert len(records[0].idempotency_key) == 64
    assert len(records[0].content_hash) == 64


@pytest.mark.asyncio
async def test_outlook_adapter_poll_incremental_with_checkpoint():
    """Kiểm tra poll_incremental() lọc bỏ dữ liệu cũ dựa trên IngestionCheckpointRecord."""
    t1 = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 29, 11, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)

    fixture_messages = [
        {
            "id": "msg-old-1",
            "conversationId": "conv-1",
            "subject": "Old Task",
            "receivedDateTime": t1.isoformat(),
            "body": {"content": "Old Content"},
        },
        {
            "id": "msg-ckpt-2",
            "conversationId": "conv-2",
            "subject": "Checkpoint Task",
            "receivedDateTime": t2.isoformat(),
            "body": {"content": "Checkpoint Content"},
        },
        {
            "id": "msg-new-3",
            "conversationId": "conv-3",
            "subject": "New Task",
            "receivedDateTime": t3.isoformat(),
            "body": {"content": "New Content"},
        },
    ]

    adapter = OutlookMailAdapter(
        tenant_id="tenant-ckpt",
        messages_fixture=fixture_messages,
    )

    # Checkpoint tại t2 với last_external_id="msg-ckpt-2"
    ckpt = IngestionCheckpointRecord(
        source_type=SourceType.MS_OUTLOOK,
        stream_id="Inbox",
        tenant_id="tenant-ckpt",
        last_external_id="msg-ckpt-2",
        last_event_timestamp=t2,
    )

    incremental_records = []
    async for rec in adapter.poll_incremental("Inbox", checkpoint=ckpt):
        incremental_records.append(rec)

    # Chỉ msg-new-3 được emit (msg-old-1 và msg-ckpt-2 bị bỏ qua)
    assert len(incremental_records) == 1
    assert incremental_records[0].external_id == "msg-new-3"
    assert incremental_records[0].event_timestamp == t3


@pytest.mark.asyncio
async def test_outlook_adapter_replay_zero_duplicates():
    """Kiểm tra replaying cùng một tập email sinh ra chính xác cùng idempotency_key và 0 duplicate trong DB."""
    raw_repo = InMemoryRawEventRepository()
    ckpt_repo = InMemoryCheckpointRepository()

    now = datetime(2026, 9, 29, 8, 0, 0, tzinfo=timezone.utc)
    messages = [
        {
            "id": f"email-replay-{i}",
            "conversationId": f"thread-{i}",
            "subject": f"Task Email #{i}",
            "receivedDateTime": (now + timedelta(minutes=i * 10)).isoformat(),
            "body": {"contentType": "text", "content": f"Please process task item #{i}."},
            "from": {"emailAddress": {"name": "Requester", "address": "requester@test.com"}},
        }
        for i in range(1, 4)
    ]

    adapter = OutlookMailAdapter(
        tenant_id="tenant-replay-zero",
        messages_fixture=messages,
    )

    pipeline = AcquisitionPipeline(
        raw_event_repo=raw_repo,
        checkpoint_repo=ckpt_repo,
        tenant_id="tenant-replay-zero",
    )

    # Lần nạp thứ nhất: Ingest cả 3 bản ghi
    count_first = await pipeline.sync_adapter(adapter, stream_id="Inbox")
    assert count_first == 3
    assert raw_repo.count == 3
    assert pipeline.stats["deduplicated"] == 0

    # Lần nạp thứ hai: Replay với dữ liệu không đổi -> 0 new records
    count_second = await pipeline.sync_adapter(adapter, stream_id="Inbox")
    assert count_second == 0
    assert raw_repo.count == 3

    # Kể cả khi nạp trực tiếp qua ingest_event, deduplication bằng idempotency_key cũng loại trừ 100%
    for m in messages:
        rec = adapter._to_raw_event(m, folder_name="Inbox")
        accepted = await pipeline.ingest_event(rec)
        assert accepted is False

    assert raw_repo.count == 3
    assert pipeline.stats["deduplicated"] == 3


@pytest.mark.asyncio
async def test_outlook_adapter_health_states(tmp_path):
    """Kiểm tra phân biệt đầy đủ 4 trạng thái sức khỏe: NOT_CONFIGURED, AUTH_REQUIRED, RATE_LIMITED, READY."""
    empty_storage = tmp_path / "empty_storage.json"
    empty_storage.write_text(json.dumps({"cookies": []}), encoding="utf-8")

    # 1. NOT_CONFIGURED: Không có token và không có mailbox
    unconfigured_adapter = OutlookMailAdapter(
        graph_token="",
        mailbox="",
        tenant_id="tenant-health-1",
        storage_path=str(empty_storage),
    )
    h_unconf = await unconfigured_adapter.health()
    assert h_unconf["configured"] is False
    assert h_unconf["status"] in ("not_configured", "unconfigured")
    assert h_unconf["health_state"] == "NOT_CONFIGURED"

    # 2. AUTH_REQUIRED: Mailbox được đặt ("me") nhưng không có token và không có cookies
    auth_req_adapter = OutlookMailAdapter(
        graph_token="",
        mailbox="me",
        tenant_id="tenant-health-2",
        storage_path=str(empty_storage),
    )
    h_auth = await auth_req_adapter.health()
    assert h_auth["configured"] is False
    assert h_auth["status"] == "AUTH_REQUIRED"
    assert h_auth["health_state"] == "AUTH_REQUIRED"

    # 3. RATE_LIMITED: Khi adapter nhận phản hồi 429
    rate_limited_adapter = OutlookMailAdapter(
        graph_token="some-token",
        mailbox="me",
        tenant_id="tenant-health-3",
    )
    rate_limited_adapter._rate_limited = True
    rate_limited_adapter._rate_limit_retry_after = 15.0
    h_rl = await rate_limited_adapter.health()
    assert h_rl["configured"] is True
    assert h_rl["status"] == "RATE_LIMITED"
    assert h_rl["health_state"] == "RATE_LIMITED"

    # 4. READY: Khi có token hợp lệ
    ready_adapter = OutlookMailAdapter(
        graph_token="valid-secret-token",
        mailbox="me",
        tenant_id="tenant-health-4",
    )
    h_ready = await ready_adapter.health()
    assert h_ready["configured"] is True
    assert h_ready["status"] == "READY"
    assert h_ready["health_state"] == "READY"


@pytest.mark.asyncio
async def test_outlook_adapter_auth_expiry_detection():
    """Kiểm tra khi nhận HTTP 401/403, adapter ném AdapterAuthenticationError và không retry."""
    call_count = 0

    async def unauthorized_fetcher(url, headers=None, body=None):
        nonlocal call_count
        call_count += 1
        resp = MagicMock()
        resp.status_code = 401
        return resp

    adapter = OutlookMailAdapter(
        tenant_id="tenant-auth-fail",
        graph_token="expired-token",
        http_fetcher=unauthorized_fetcher,
    )

    with pytest.raises(AdapterAuthenticationError) as exc_info:
        async for _ in adapter.backfill("Inbox"):
            pass

    assert "authentication failed" in str(exc_info.value).lower()
    # Lỗi xác thực là lỗi chí mạng (fatal), không được phép retry loop
    assert call_count == 1


@pytest.mark.asyncio
async def test_outlook_adapter_transient_error_exponential_backoff():
    """Kiểm tra khi gặp lỗi mạng tạm thời (HTTP 503), adapter tự động retry và hoàn tất khi hồi phục."""
    attempts = 0

    async def flaky_fetcher(url, headers=None, body=None):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            resp = MagicMock()
            resp.status_code = 503
            return resp
        return {
            "value": [
                {
                    "id": "msg-recovered-01",
                    "subject": "Recovered task",
                    "body": {"content": "Successfully recovered after retry"},
                    "receivedDateTime": "2026-09-29T10:00:00Z",
                }
            ]
        }

    adapter = OutlookMailAdapter(
        tenant_id="tenant-flaky",
        graph_token="valid-token",
        http_fetcher=flaky_fetcher,
    )
    # Rút ngắn delay để test chạy siêu tốc
    adapter.retry_policy.initial_delay_seconds = 0.01
    adapter.retry_policy.jitter = False

    records = []
    async for rec in adapter.backfill("Inbox"):
        records.append(rec)

    assert len(records) == 1
    assert records[0].external_id == "msg-recovered-01"
    assert attempts == 3


def test_outlook_adapter_mail_threads_and_quoted_text():
    """Kiểm tra bóc tách lời nhắn mới và chuỗi trích dẫn (quoted/forwarded) trong email HTML và text."""
    # 1. Reply email với blockquote và header reply
    html_reply = (
        "<html><body>"
        "<p>Vâng anh, em sẽ cập nhật kiến trúc PTB trước chiều nay.</p>"
        "<div id=\"divRplyFwdMsg\"><b>From:</b> Boss &lt;boss@corp.com&gt;<br><b>Subject:</b> Task v1</div>"
        "<blockquote><p>Nhờ em review kiến trúc trước thứ Hai nhé.</p></blockquote>"
        "</body></html>"
    )
    cleaned = clean_email_content(html_reply, subject="Re: Task v1", content_type="html")
    assert "em sẽ cập nhật kiến trúc PTB" in cleaned["cleaned_body"]
    assert "Nhờ em review kiến trúc" not in cleaned["cleaned_body"]
    assert "Nhờ em review kiến trúc" in cleaned["quoted_text"]
    assert cleaned["is_reply"] is True
    assert cleaned["is_forwarded"] is False

    # 2. Forwarded email dạng plain text
    text_fwd = (
        "Chuyển tiếp cho team phát triển xử lý.\n\n"
        "-----Original Message-----\n"
        "From: client@partner.com\n"
        "Sent: Monday, September 28, 2026 9:00 AM\n"
        "Subject: Urgent security patch\n"
        "Please apply patch PTB-SEC-01 immediately."
    )
    cleaned_fwd = clean_email_content(text_fwd, subject="FW: Urgent security patch", content_type="text")
    assert "Chuyển tiếp cho team phát triển xử lý." == cleaned_fwd["cleaned_body"]
    assert "Please apply patch" in cleaned_fwd["quoted_text"]
    assert cleaned_fwd["is_reply"] is False
    assert cleaned_fwd["is_forwarded"] is True


def test_outlook_adapter_attachments_metadata_only():
    """Kiểm tra trích xuất attachments metadata an toàn, loại bỏ hoàn toàn binary bytes."""
    raw_attachments = [
        {
            "id": "att-001",
            "name": "Design_Spec.pdf",
            "contentType": "application/pdf",
            "size": 1048576,
            "isInline": False,
            "contentBytes": "SGVsbG8gV29ybGQhIFRoaXMgaXMgYSBsYXJnZSBmaWxlIGJsb2I=",
        },
        {
            "id": "att-002",
            "name": "screenshot.png",
            "contentType": "image/png",
            "size": 524288,
            "isInline": True,
            "contentBytes": "aW1hZ2UgZGF0YSBibG9i",
        }
    ]

    meta = extract_attachments_metadata(raw_attachments)
    assert len(meta) == 2
    assert meta[0]["name"] == "Design_Spec.pdf"
    assert meta[0]["size"] == 1048576
    assert meta[0]["content_type"] == "application/pdf"
    assert meta[0]["is_inline"] is False
    assert "contentBytes" not in meta[0]

    # Sanitize payload: không còn contentBytes
    sanitized = sanitize_email_payload({"subject": "Email with files", "attachments": raw_attachments})
    assert len(sanitized["attachments"]) == 2
    assert "contentBytes" not in sanitized["attachments"][0]
    assert "contentBytes" not in sanitized["attachments"][1]


@pytest.mark.asyncio
async def test_outlook_fixtures_ingest_pipeline_full():
    """Kiểm tra nạp toàn bộ sanitized fixtures (tests/fixtures/outlook_messages_fixture.json) qua pipeline."""
    fixture_path = Path("tests/fixtures/outlook_messages_fixture.json")
    assert fixture_path.exists(), "outlook_messages_fixture.json must exist"

    with open(fixture_path, "r", encoding="utf-8") as f:
        fixture_data = json.load(f)

    messages = fixture_data.get("value", [])
    assert len(messages) >= 6, "Fixture must contain at least 6 diverse email scenarios"

    adapter = OutlookMailAdapter(
        tenant_id="fpt-ptb-tenant",
        messages_fixture=messages,
    )

    raw_repo = InMemoryRawEventRepository()
    ckpt_repo = InMemoryCheckpointRepository()
    pipeline = AcquisitionPipeline(
        raw_event_repo=raw_repo,
        checkpoint_repo=ckpt_repo,
        tenant_id="fpt-ptb-tenant",
    )

    ingested_count = await pipeline.sync_adapter(adapter, stream_id="Inbox")
    assert ingested_count == len(messages)
    assert raw_repo.count == len(messages)

    # Kiểm tra tính toàn vẹn metadata & provenance của từng bản ghi
    for rec in raw_repo.events.values():
        assert rec.tenant_id == "fpt-ptb-tenant"
        assert rec.source_type == SourceType.MS_OUTLOOK
        assert rec.external_id is not None
        assert rec.idempotency_key is not None
        assert len(rec.idempotency_key) == 64
        assert len(rec.content_hash) == 64
        assert rec.event_timestamp is not None
        assert rec.deep_link.startswith("https://outlook.office.com/mail/deeplink/read/")
        assert "thread_metadata" in rec.raw_payload

    # Replay ngay lập tức phải cho ra 0 duplicate
    replay_count = await pipeline.sync_adapter(adapter, stream_id="Inbox")
    assert replay_count == 0
    assert raw_repo.count == len(messages)


# =============================================================================
# W2-03: INTERCEPTOR TESTS (EDITED, DELETED, THROTTLING, PENDING)
# =============================================================================

def test_outlook_interceptor_edited_message():
    """Kiểm tra khi email được chỉnh sửa nội dung, OutlookNetworkInterceptor tạo ra content hash và idempotency key mới."""
    interceptor = OutlookNetworkInterceptor(tenant_id="tenant-edit")

    original_payload = {
        "value": [
            {
                "id": "email-edit-100",
                "subject": "Task v1.0",
                "body": {"content": "Hoàn thành trước 12:00."},
                "conversationId": "conv-edit-1",
            }
        ]
    }
    rec1 = interceptor.parse_payload(original_payload)[0]

    # Email được sửa deadline thành 17:00
    edited_payload = {
        "value": [
            {
                "id": "email-edit-100",
                "subject": "Task v1.0",
                "body": {"content": "Hoàn thành trước 17:00 (gia hạn thêm)."},
                "conversationId": "conv-edit-1",
            }
        ]
    }
    rec2 = interceptor.parse_payload(edited_payload)[0]

    assert rec1.external_id == rec2.external_id
    assert rec1.content_hash != rec2.content_hash
    assert rec1.idempotency_key != rec2.idempotency_key


def test_outlook_interceptor_deleted_message():
    """Kiểm tra OutlookNetworkInterceptor nhận diện tin nhắn bị xóa và gán cờ is_deleted."""
    interceptor = OutlookNetworkInterceptor(tenant_id="tenant-del")

    deleted_payload = {
        "value": [
            {
                "id": "email-del-200",
                "subject": "Cuộc họp đã hủy",
                "body": {"content": ""},
                "isDeleted": True,
                "changeType": "deleted",
            }
        ]
    }
    records = interceptor.parse_payload(deleted_payload)
    assert len(records) == 1
    assert records[0].raw_payload.get("thread_metadata", {}).get("is_deleted") is True
    assert "[Status: Deleted]" in records[0].normalized_text


@pytest.mark.asyncio
async def test_outlook_interceptor_throttling_429():
    """Kiểm tra khi Outlook Web gặp HTTP 429, interceptor cảnh báo và chuyển session sang DEGRADED."""
    interceptor = OutlookNetworkInterceptor()
    mock_resp = MagicMock()
    mock_resp.status = 429
    mock_resp.url = "https://outlook.office.com/api/v2.0/me/messages"

    records = await interceptor.handle_response(mock_resp)
    assert len(records) == 0
    assert interceptor.session_state == SessionHealthState.DEGRADED


def test_teams_interceptor_edited_message():
    """Kiểm tra TeamsNetworkInterceptor nhận diện tin nhắn chat được edit và sinh ra idempotency key mới."""
    interceptor = TeamsNetworkInterceptor(tenant_id="tenant-teams-edit")

    msg_orig = {
        "messages": [
            {
                "id": "teams-edit-001",
                "sequenceId": "1001",
                "version": "1001",
                "content": "Deploy lúc 14:00 nhé anh em.",
            }
        ]
    }
    rec1 = interceptor.parse_payload(msg_orig)[0]

    msg_edited = {
        "messages": [
            {
                "id": "teams-edit-001",
                "sequenceId": "1001",
                "version": "1002",
                "properties": {"edittime": "2026-09-29T14:05:00Z"},
                "content": "Đổi sang deploy lúc 16:00 nhé anh em.",
            }
        ]
    }
    rec2 = interceptor.parse_payload(msg_edited)[0]

    assert rec1.external_id == rec2.external_id
    assert rec1.content_hash != rec2.content_hash
    assert rec1.idempotency_key != rec2.idempotency_key
    assert rec2.raw_payload["is_edited"] is True


def test_teams_interceptor_deleted_message():
    """Kiểm tra TeamsNetworkInterceptor xử lý tin nhắn chat bị xóa."""
    interceptor = TeamsNetworkInterceptor(tenant_id="tenant-teams-del")

    deleted_msg = {
        "messages": [
            {
                "id": "teams-del-002",
                "isdeleted": True,
                "messageType": "Control/Status",
                "properties": {"deletetime": "2026-09-29T15:00:00Z"},
                "content": "",
            }
        ]
    }
    records = interceptor.parse_payload(deleted_msg)
    assert len(records) == 1
    assert records[0].raw_payload["is_deleted"] is True
    assert records[0].normalized_text == "[Message Deleted]"


@pytest.mark.asyncio
async def test_teams_interceptor_throttling_429():
    """Kiểm tra khi Teams Web gặp HTTP 429, interceptor cảnh báo và chuyển session sang DEGRADED."""
    interceptor = TeamsNetworkInterceptor()
    mock_resp = MagicMock()
    mock_resp.status = 429
    mock_resp.url = "https://teams.microsoft.com/api/chats/19:test/messages"

    records = await interceptor.handle_response(mock_resp)
    assert len(records) == 0
    assert interceptor.session_state == SessionHealthState.DEGRADED


def test_live_outlook_teams_session_pending_when_no_credentials():
    """Đánh dấu test kết nối Microsoft live thực tế là PENDING khi không có cookies / credentials."""
    storage_path = Path(DEFAULT_STORAGE_PATH)
    has_live_cookies = False
    if storage_path.exists():
        mgr = SessionManager(str(storage_path))
        has_live_cookies = mgr.has_valid_session()

    has_live_token = bool(os.getenv("MS_GRAPH_TOKEN"))

    if not has_live_cookies and not has_live_token:
        pytest.skip(
            "PENDING: Microsoft live auth credentials / storage_state.json cookies unavailable in this test environment. "
            "Run login_interactive() or set MS_GRAPH_TOKEN for live E2E verification."
        )

    assert True
