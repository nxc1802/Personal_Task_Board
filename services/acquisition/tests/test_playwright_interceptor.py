"""Tests for Layer 1A Playwright Network Interceptors, Session Lifecycle & Health."""

import asyncio
from datetime import datetime, timezone
import json
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
