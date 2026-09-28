"""Tests for Captured Response Fixtures Parsing (Task 1).

Validates that real-world captured response JSON fixtures from Teams Web and Outlook Web
parse into v1-compliant RawEventRecord objects with complete idempotency_key, content_hash,
payload_json, timestamps, and thread metadata.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import pytest

from ptb_acquisition.playwright.outlook_interceptor import OutlookNetworkInterceptor
from ptb_acquisition.playwright.teams_interceptor import TeamsNetworkInterceptor
from ptb_contracts import ProcessingStatus, RawEventRecord, SourceType
from ptb_processing.extractor.heuristic_filter import HeuristicCandidateFilter
from ptb_processing.parsers.quote_reply import TeamsQuoteReplyParser


FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def test_teams_messages_fixture_parsing():
    """Verify parsing of tests/fixtures/teams_messages_fixture.json into RawEventRecord v1."""
    fixture_path = FIXTURES_DIR / "teams_messages_fixture.json"
    assert fixture_path.is_file(), f"Fixture file not found: {fixture_path}"

    with open(fixture_path, "r", encoding="utf-8") as f:
        teams_payload = json.load(f)

    tenant_id = "tenant-fpt-teams"
    interceptor = TeamsNetworkInterceptor(tenant_id=tenant_id)
    records = interceptor.parse_payload(teams_payload)

    # 1. Kiểm tra số lượng bản ghi được phân tích
    assert len(records) == 3, f"Expected 3 records from fixture, got {len(records)}"

    # 2. Kiểm tra chuẩn v1 cho toàn bộ records
    for rec in records:
        assert isinstance(rec, RawEventRecord)
        assert rec.source_type == SourceType.MS_TEAMS_WEB
        assert rec.tenant_id == tenant_id
        assert rec.processing_status == ProcessingStatus.PENDING

        # Idempotency key phải có độ dài 64 hex chars (SHA-256)
        assert len(rec.idempotency_key) == 64
        assert int(rec.idempotency_key, 16) > 0

        # Content hash phải khớp chính xác với SHA-256 của normalized_text
        expected_content_hash = hashlib.sha256(rec.normalized_text.encode("utf-8")).hexdigest()
        assert rec.content_hash == expected_content_hash

        # payload_json phải là chuỗi JSON hợp lệ và deserialize được
        assert rec.payload_json is not None
        deserialized = json.loads(rec.payload_json)
        assert isinstance(deserialized, dict)
        assert deserialized["id"] == rec.external_id

        # Timestamps
        assert isinstance(rec.event_timestamp, datetime)
        assert rec.event_timestamp.tzinfo is not None
        assert isinstance(rec.captured_at, datetime)
        assert rec.created_at is not None

    # 3. Kiểm tra chi tiết Message 1: Quoted Reply, Mention, Commitment, Attachment, Thread ID
    msg1 = records[0]
    assert msg1.external_id == "1727599200000"
    assert msg1.parent_external_id == "1727598800000"
    assert msg1.conversation_or_project_id == "19:devops_core_team@thread.v2"
    assert msg1.author_external_id == "user-cuong-001"
    assert msg1.author_display_name == "Dam Quang Cuong"
    assert msg1.deep_link == "https://teams.microsoft.com/l/message/19:devops_core_team@thread.v2/1727599200000"

    # Kiểm tra TeamsQuoteReplyParser trên payload của Message 1
    parser = TeamsQuoteReplyParser()
    parsed = parser.parse(msg1.raw_payload, actual_author=msg1.author_display_name)
    assert parsed.is_quote_reply is True
    assert parsed.quoted_author_raw == "Tran Van Lead"
    assert "OPS-88" in (parsed.quoted_content_text or "")
    assert "để em fix bug này trước 5h chiều" in (parsed.actual_content_text or "")
    assert "Tran Van Lead" in (parsed.actual_content_text or "")

    # Heuristic filter phải nhận diện được đây là task candidate cần trích xuất
    heuristic = HeuristicCandidateFilter()
    assert heuristic.should_extract(parsed.actual_content_text) is True

    # 4. Kiểm tra Message 3: Chatter / Tin nhắn thông báo (Heuristic filter từ chối)
    msg3 = records[2]
    parsed_msg3 = parser.parse(msg3.raw_payload, actual_author=msg3.author_display_name)
    assert heuristic.should_extract(parsed_msg3.actual_content_text) is False


def test_outlook_messages_fixture_parsing():
    """Verify parsing of tests/fixtures/outlook_messages_fixture.json into RawEventRecord v1."""
    fixture_path = FIXTURES_DIR / "outlook_messages_fixture.json"
    assert fixture_path.is_file(), f"Fixture file not found: {fixture_path}"

    with open(fixture_path, "r", encoding="utf-8") as f:
        outlook_payload = json.load(f)

    tenant_id = "tenant-fpt-outlook"
    interceptor = OutlookNetworkInterceptor(tenant_id=tenant_id)
    records = interceptor.parse_payload(outlook_payload)

    # 1. Kiểm tra số lượng bản ghi
    assert len(records) == 2, f"Expected 2 records from fixture, got {len(records)}"

    # 2. Kiểm tra chuẩn v1 cho toàn bộ records
    for rec in records:
        assert isinstance(rec, RawEventRecord)
        assert rec.source_type == SourceType.MS_OUTLOOK_WEB
        assert rec.tenant_id == tenant_id
        assert rec.processing_status == ProcessingStatus.PENDING

        # Idempotency key phải có độ dài 64 hex chars
        assert len(rec.idempotency_key) == 64
        assert int(rec.idempotency_key, 16) > 0

        # Content hash phải khớp chính xác với SHA-256 của normalized_text
        expected_content_hash = hashlib.sha256(rec.normalized_text.encode("utf-8")).hexdigest()
        assert rec.content_hash == expected_content_hash

        # payload_json hợp lệ
        assert rec.payload_json is not None
        deserialized = json.loads(rec.payload_json)
        assert isinstance(deserialized, dict)
        assert deserialized["id"] == rec.external_id

        # Timestamps
        assert isinstance(rec.event_timestamp, datetime)
        assert rec.event_timestamp.tzinfo is not None

    # 3. Kiểm tra chi tiết Email 1: Subject, Sender, Body Preview, Deadline cụ thể
    email1 = records[0]
    assert email1.external_id == "AAMkADhkOWE5YjEtMDcxNS00NDc4LTljNjQtZGFjNzQyOTJhOTAwBGAAAAA3"
    assert email1.parent_external_id == "AAQkADhkOWE5YjEtMDcxNS00NDc4LTljNjQtZGFjNzQyOTJhOTAwAQAAAAD1"
    assert email1.conversation_or_project_id == "AAQkADhkOWE5YjEtMDcxNS00NDc4LTljNjQtZGFjNzQyOTJhOTAwAQAAAAD1"
    assert email1.author_external_id == "huy.nguyen@partner.com"
    assert email1.author_display_name == "PM Huy Nguyen"
    assert email1.deep_link.startswith("https://outlook.office.com/mail/deeplink/read/")

    # Kiểm tra nội dung chứa deadline
    assert "17:00 ngày 30/09/2026" in email1.normalized_text
    assert "review giúp em" in email1.normalized_text

    # Heuristic filter nhận diện email yêu cầu review có deadline
    heuristic = HeuristicCandidateFilter()
    assert heuristic.should_extract(email1.normalized_text) is True


def test_interceptor_parsing_idempotency_determinism():
    """Verify that multiple parses of the same payload produce deterministic idempotency_key & content_hash."""
    fixture_path = FIXTURES_DIR / "teams_messages_fixture.json"
    with open(fixture_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    interceptor = TeamsNetworkInterceptor(tenant_id="fixed-tenant")
    run_1 = interceptor.parse_payload(payload)
    run_2 = interceptor.parse_payload(payload)

    assert len(run_1) == len(run_2)
    for r1, r2 in zip(run_1, run_2):
        assert r1.idempotency_key == r2.idempotency_key
        assert r1.content_hash == r2.content_hash
        assert r1.external_id == r2.external_id
        assert r1.source_type == r2.source_type
        assert r1.normalized_text == r2.normalized_text
