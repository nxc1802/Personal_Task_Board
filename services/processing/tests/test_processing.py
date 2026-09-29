"""Unit and integration tests for Layer 2 Processing Pipeline.

Tests:
1. TeamsQuoteReplyParser (HTML cleaning, quote separation, emoji/metadata stripping).
2. IdentityResolver (Invariant exact match vs fuzzy candidate signal - no auto-merge).
3. HeuristicCandidateFilter (Vietnamese & English commitment/request detection, noise filtering).
4. LLMStructuredExtractor (OpenAI compatibility, mock fallback, confidence classification).
5. AttributionValidator (Owner = actual_author, Requester = quoted_author on commitment).
6. End-to-end integration test of Layer 2 processing pipeline.
"""

from datetime import datetime, timezone
import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from ptb_contracts import (
    EvidenceRecord,
    EvidenceType,
    ParsedMessageContent,
    ProcessingStatus,
    RawEventRecord,
    SourceType,
    TaskStatus,
    UnifiedTaskCandidate,
)
from ptb_processing import (
    AttributionReport,
    AttributionValidator,
    HeuristicCandidateFilter,
    IdentityResolutionResult,
    IdentityResolver,
    LLMStructuredExtractor,
    ProcessingPipeline,
    ProcessingWorker,
    TeamsQuoteReplyParser,
)
from ptb_processing.extractor.llm_extractor import LLMExtractedSchema, classify_review_status


# ==============================================================================
# 1. TESTS FOR TeamsQuoteReplyParser
# ==============================================================================

def test_teams_quote_reply_parser_basic_quote():
    parser = TeamsQuoteReplyParser()
    raw_html = (
        "<div>"
        "<blockquote itemscope='' itemtype='http://schema.skype.com/Reply' itemid='1726567000'>"
        "<strong>Nguyen Van Huy</strong>: Can you check why deployment failed?"
        "</blockquote>"
        "<p>Để em check nhé.</p>"
        "</div>"
    )
    result = parser.parse(raw_html, actual_author="Dam Quang Cuong")

    assert result.is_quote_reply is True
    assert result.quoted_author_raw == "Nguyen Van Huy"
    assert result.quoted_content_text == "Can you check why deployment failed?"
    assert result.actual_content_text == "Để em check nhé."
    assert result.actual_author_raw == "Dam Quang Cuong"


def test_teams_quote_reply_parser_itemprop_author():
    parser = TeamsQuoteReplyParser()
    raw_html = (
        "<div>"
        "<blockquote>"
        "<strong itemprop='author'>Leader John</strong>"
        "<div>Please fix OPS-99 before 5pm.</div>"
        "</blockquote>"
        "<div>I'm on it! <emoji alt='thumbsup'>(thumbsup)</emoji></div>"
        "</div>"
    )
    result = parser.parse(raw_html, actual_author="Dev Alex")

    assert result.is_quote_reply is True
    assert result.quoted_author_raw == "Leader John"
    assert "Please fix OPS-99 before 5pm" in result.quoted_content_text
    assert "I'm on it!" in result.actual_content_text
    # Emoji should be stripped
    assert "(thumbsup)" not in result.actual_content_text


def test_teams_quote_reply_parser_html_cleaning():
    parser = TeamsQuoteReplyParser()
    html_input = (
        "<p>Line 1<br/>Line 2&nbsp;&amp;&nbsp;Line 3</p>"
        "<div><system-event>User joined</system-event></div>"
        "<ul><li>Item 1</li><li>Item 2</li></ul>"
        "<span>Great job! (clap) ❤️ 2 [Like]</span>"
    )
    cleaned = parser.clean_html(html_input)

    assert "<p>" not in cleaned
    assert "<br" not in cleaned
    assert "&nbsp;" not in cleaned
    assert "&amp;" not in cleaned
    assert "&" in cleaned
    assert "Line 1\nLine 2 & Line 3" in cleaned
    assert "User joined" not in cleaned  # System event removed
    assert "• Item 1" in cleaned
    assert "• Item 2" in cleaned
    assert "(clap)" not in cleaned
    assert "❤️ 2" not in cleaned
    assert "[Like]" not in cleaned


def test_teams_quote_reply_parser_no_quote():
    parser = TeamsQuoteReplyParser()
    raw_text = "<p>Anh em chuẩn bị họp demo nhé.</p>"
    result = parser.parse(raw_text, actual_author="Huy")

    assert result.is_quote_reply is False
    assert result.quoted_author_raw is None
    assert result.quoted_content_text is None
    assert result.actual_content_text == "Anh em chuẩn bị họp demo nhé."
    assert result.actual_author_raw == "Huy"


def test_teams_quote_reply_parser_from_payload_dict():
    parser = TeamsQuoteReplyParser()
    payload = {
        "body": {
            "contentType": "html",
            "content": "<blockquote><strong>Huy</strong>: Help me!</blockquote><p>On it.</p>",
        },
        "from": {
            "user": {
                "id": "user-cuong-01",
                "displayName": "Cuong Dam",
            }
        },
    }
    result = parser.parse(payload)

    assert result.is_quote_reply is True
    assert result.quoted_author_raw == "Huy"
    assert result.quoted_content_text == "Help me!"
    assert result.actual_content_text == "On it."
    assert result.actual_author_raw == "Cuong Dam"


# ==============================================================================
# 2. TESTS FOR IdentityResolver (Invariant rule)
# ==============================================================================

def test_identity_resolver_exact_match_aad():
    resolver = IdentityResolver()
    resolver.register_person(
        person_id="p-100",
        canonical_name="Nguyen Van Huy",
        primary_email="huy.nguyen@fpt.com",
        aad_object_id="aad-obj-12345",
    )

    res = resolver.resolve(aad_object_id="aad-obj-12345")
    assert res.is_exact_match is True
    assert res.person_id == "p-100"
    assert res.canonical_name == "Nguyen Van Huy"
    assert res.confidence == 1.0
    assert res.resolution_method == "aad_object_id"
    assert res.is_candidate_signal is False


def test_identity_resolver_exact_match_account_id():
    resolver = IdentityResolver()
    resolver.register_person(
        person_id="p-200",
        canonical_name="Dam Quang Cuong",
        primary_email="cuong.dam@fpt.com",
        account_id="acc-jira-cuong-99",
    )

    res = resolver.resolve(account_id="acc-jira-cuong-99")
    assert res.is_exact_match is True
    assert res.person_id == "p-200"
    assert res.confidence == 1.0
    assert res.resolution_method == "account_id"


def test_identity_resolver_exact_match_email():
    resolver = IdentityResolver()
    resolver.register_person(
        person_id="p-300",
        canonical_name="Le Thi Lan",
        primary_email="lan.le@company.com",
    )

    # Test case insensitivity
    res = resolver.resolve(email="LAN.LE@COMPANY.COM")
    assert res.is_exact_match is True
    assert res.person_id == "p-300"
    assert res.confidence == 1.0
    assert res.resolution_method == "email"


def test_identity_resolver_invariant_fuzzy_name_never_auto_merges():
    """Quy tắc bất biến: Nếu chỉ trùng họ tên (fuzzy name) hoặc tenant alias khác mà không có ID khớp,

    TUYỆT ĐỐI KHÔNG tự động merge vào Person cũ.
    """
    resolver = IdentityResolver()
    resolver.register_person(
        person_id="p-huy-original",
        canonical_name="Nguyen Van Huy",
        primary_email="huy.nguyen@fpt.com",
        tenant_id="tenant-internal",
    )

    # Gửi một identity mới chỉ có display_name "Nguyễn Văn Huy" hoặc "Huy Nguyen", không có AAD/email/account_id khớp
    res = resolver.resolve(
        display_name="Nguyễn Văn Huy",
        tenant_id="tenant-external-client",
        auto_create_if_not_found=False,
    )

    # Phải gắn nhãn candidate signal
    assert res.is_exact_match is False
    assert res.is_candidate_signal is True
    assert res.confidence == 0.50  # Thấp hơn threshold auto-accept 0.65
    assert res.resolution_method == "candidate_signal"

    # TUYỆT ĐỐI KHÔNG gán person_id về p-huy-original
    assert res.person_id is None
    assert len(res.candidate_hints) == 1
    hint = res.candidate_hints[0]
    assert hint["person_id"] == "p-huy-original"
    assert "Auto-merge disallowed by invariant rule" in hint["reason"]


def test_identity_resolver_unresolved():
    resolver = IdentityResolver()
    res = resolver.resolve(display_name="Ai Do Hoan Toan La")

    assert res.is_exact_match is False
    assert res.is_candidate_signal is False
    assert res.person_id is None
    assert res.resolution_method == "unresolved"
    assert res.confidence == 0.0


# ==============================================================================
# 3. TESTS FOR HeuristicCandidateFilter
# ==============================================================================

def test_heuristic_candidate_filter_vietnamese_commitments():
    filter_ = HeuristicCandidateFilter()
    assert filter_.should_extract("Để em check nhé") is True
    assert filter_.should_extract("Em sẽ fix bug này trước 5h") is True
    assert filter_.should_extract("Đang xử lý sự cố staging nha anh") is True
    assert filter_.should_extract("Anh sẽ gửi trước chiều nay") is True
    assert filter_.should_extract("Em nhận task này rồi") is True


def test_heuristic_candidate_filter_vietnamese_action_requests():
    filter_ = HeuristicCandidateFilter()
    assert filter_.should_extract("Anh check giúp em PR này với") is True
    assert filter_.should_extract("Nhờ em hỗ trợ khách hàng gấp") is True
    assert filter_.should_extract("Cần làm xong báo cáo trước ngày mai") is True
    assert filter_.should_extract("Vui lòng kiểm tra log deploy") is True


def test_heuristic_candidate_filter_english():
    filter_ = HeuristicCandidateFilter()
    assert filter_.should_extract("I'm on it") is True
    assert filter_.should_extract("will do") is True
    assert filter_.should_extract("I will fix this issue") is True
    assert filter_.should_extract("pls fix urgent bug in production") is True
    assert filter_.should_extract("can you check why deployment failed?") is True
    assert filter_.should_extract("action required before 5pm") is True


def test_heuristic_candidate_filter_deadlines_and_tickets():
    filter_ = HeuristicCandidateFilter()
    assert filter_.should_extract("Please review OPS-88") is True
    assert filter_.should_extract("Deploy failed on production") is True
    assert filter_.should_extract("Hạn chót 17h chiều") is True


def test_heuristic_candidate_filter_noise_and_chatter_rejected():
    filter_ = HeuristicCandidateFilter()
    # 70% tin nhắn rác hoặc chào hỏi phải trả về False
    assert filter_.should_extract("Chào buổi sáng cả nhà!") is False
    assert filter_.should_extract("Good morning all") is False
    assert filter_.should_extract("Hello") is False
    assert filter_.should_extract("Cảm ơn anh nhiều") is False
    assert filter_.should_extract("Thanks!") is False
    assert filter_.should_extract("haha ok") is False
    assert filter_.should_extract("+1") is False
    assert filter_.should_extract("oke") is False
    assert filter_.should_extract("ab") is False  # Quá ngắn


def test_heuristic_candidate_filter_analyze():
    filter_ = HeuristicCandidateFilter()
    analysis = filter_.analyze("Để em fix ticket OPS-88 trước 5pm nhé")
    assert analysis["should_extract"] is True
    assert "để em" in analysis["matched_keywords"]
    assert any("OPS-88" in p for p in analysis["matched_patterns"])
    assert analysis["confidence_hint"] >= 0.8


# ==============================================================================
# 4. TESTS FOR LLMStructuredExtractor
# ==============================================================================

def test_llm_extractor_review_status_classification():
    assert classify_review_status(0.95) == "auto_approved"
    assert classify_review_status(0.65) == "auto_approved"
    assert classify_review_status(0.64) == "pending_review"
    assert classify_review_status(0.40) == "pending_review"
    assert classify_review_status(0.39) == "ignore"
    assert classify_review_status(0.10) == "ignore"


def test_llm_extractor_fallback_rule_based():
    extractor = LLMStructuredExtractor(mock_mode=True)
    parsed = ParsedMessageContent(
        is_quote_reply=True,
        quoted_author_raw="Huy",
        quoted_content_text="Can you check why deployment failed?",
        actual_content_text="Để em check nhé.",
        actual_author_raw="Cuong",
    )

    candidate = extractor.extract_from_parsed(parsed, raw_event_id="raw-101", source_type="ms_teams")

    assert isinstance(candidate, UnifiedTaskCandidate)
    assert "deployment failed" in candidate.title.lower()
    assert candidate.owner_name == "Cuong"
    assert candidate.requester_name == "Huy"
    assert candidate.extraction_confidence >= 0.65
    assert candidate.review_status == "auto_approved"
    assert len(candidate.evidences) == 1
    assert candidate.evidences[0].snippet == "Để em check nhé."
    assert candidate.evidences[0].evidence_type == EvidenceType.CHAT_COMMITMENT


def test_llm_extractor_with_mock_handler():
    extractor = LLMStructuredExtractor()

    # Giả lập phản hồi của model với các mức confidence khác nhau
    def mock_high(prompt: str):
        return LLMExtractedSchema(
            title="Fix authentication loop",
            description="Clear cookies and refresh session",
            owner_name="Dev Alex",
            requester_name="Tech Lead",
            due_date=datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
            explicit_deadline=True,
            extraction_confidence=0.92,
            evidence_snippet="I will resolve the auth loop by 10am tomorrow.",
        )

    extractor.set_mock_handler(mock_high)
    parsed = ParsedMessageContent(
        is_quote_reply=False,
        actual_content_text="I will resolve the auth loop by 10am tomorrow.",
        actual_author_raw="Dev Alex",
    )
    cand = extractor.extract_from_parsed(parsed)
    assert cand.title == "Fix authentication loop"
    assert cand.owner_name == "Dev Alex"
    assert cand.explicit_deadline is True
    assert cand.extraction_confidence == 0.92
    assert cand.review_status == "auto_approved"

    # Giả lập mức confidence trung bình (pending_review)
    def mock_medium(prompt: str):
        return LLMExtractedSchema(
            title="Maybe look at metrics",
            description="Ambiguous comment",
            owner_name="Dev Alex",
            requester_name=None,
            due_date=None,
            explicit_deadline=False,
            extraction_confidence=0.55,
            evidence_snippet="We might need to look at metrics sometime.",
        )

    extractor.set_mock_handler(mock_medium)
    cand_med = extractor.extract_from_parsed(parsed)
    assert cand_med.extraction_confidence == 0.55
    assert cand_med.review_status == "pending_review"

    # Giả lập mức confidence thấp (ignore)
    def mock_low(prompt: str):
        return LLMExtractedSchema(
            title="Vague chat",
            description=None,
            owner_name=None,
            requester_name=None,
            due_date=None,
            explicit_deadline=False,
            extraction_confidence=0.25,
            evidence_snippet="Ok cool",
        )

    extractor.set_mock_handler(mock_low)
    cand_low = extractor.extract_from_parsed(parsed)
    assert cand_low.extraction_confidence == 0.25
    assert cand_low.review_status == "ignore"


def test_llm_extractor_openai_http_mock():
    """Test gọi API OpenAI thành công thông qua mock urllib response."""
    extractor = LLMStructuredExtractor(
        base_url="https://api.openai.com/v1",
        api_key="sk-fake-key",
        model="gpt-4o-mini",
        mock_mode=False,
    )

    fake_openai_response = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps({
                        "title": "Investigate staging database crash",
                        "description": "DB connection timeout on staging",
                        "owner_name": "Cuong Dam",
                        "requester_name": "Huy Nguyen",
                        "due_date": "2026-09-19T17:00:00Z",
                        "explicit_deadline": True,
                        "extraction_confidence": 0.89,
                        "evidence_snippet": "Em sẽ kiểm tra DB staging ngay.",
                    }),
                },
                "finish_reason": "stop",
            }
        ],
    }

    mock_urlopen = MagicMock()
    mock_urlopen.read.return_value = json.dumps(fake_openai_response).encode("utf-8")
    mock_urlopen.__enter__.return_value = mock_urlopen

    with patch("urllib.request.urlopen", return_value=mock_urlopen):
        parsed = ParsedMessageContent(
            is_quote_reply=True,
            quoted_author_raw="Huy Nguyen",
            quoted_content_text="DB staging bị sập rồi",
            actual_content_text="Em sẽ kiểm tra DB staging ngay.",
            actual_author_raw="Cuong Dam",
        )
        cand = extractor.extract_from_parsed(parsed, raw_event_id="raw-db-crash")

        assert cand.title == "Investigate staging database crash"
        assert cand.owner_name == "Cuong Dam"
        assert cand.requester_name == "Huy Nguyen"
        assert cand.extraction_confidence == 0.89
        assert cand.review_status == "auto_approved"
        assert cand.explicit_deadline is True


# ==============================================================================
# 5. TESTS FOR AttributionValidator
# ==============================================================================

def test_attribution_validator_commitment_reply():
    """Nhiệm vụ 5: Nếu người A hỏi/yêu cầu và người B trả lời 'để em làm'

    -> Owner bắt buộc là người B (actual_author), requester là người A (quoted_author).
    """
    validator = AttributionValidator()

    # Giả sử LLM ban đầu gán sai: owner=Huy, requester=None
    candidate = UnifiedTaskCandidate(
        id="task-1",
        title="Check deployment",
        owner_name="Huy",
        requester_name=None,
    )

    parsed = ParsedMessageContent(
        is_quote_reply=True,
        quoted_author_raw="Huy",
        quoted_content_text="Can you check why deployment failed?",
        actual_content_text="Để em check nhé.",
        actual_author_raw="Dam Quang Cuong",
    )

    updated_cand, report = validator.validate_and_enforce(candidate, parsed)

    # Xác thực owner phải là người B (actual_author)
    assert updated_cand.owner_name == "Dam Quang Cuong"
    # Requester phải là người A (quoted_author)
    assert updated_cand.requester_name == "Huy"
    assert report.corrected is True
    assert report.original_owner == "Huy"
    assert report.final_owner == "Dam Quang Cuong"
    assert report.final_requester == "Huy"


def test_attribution_validator_english_on_it():
    validator = AttributionValidator()
    candidate = UnifiedTaskCandidate(
        id="task-2",
        title="Fix production issue",
        owner_name=None,
        requester_name=None,
    )

    parsed = ParsedMessageContent(
        is_quote_reply=True,
        quoted_author_raw="Alice",
        quoted_content_text="Server down in EU region",
        actual_content_text="I'm on it! Will fix ASAP.",
        actual_author_raw="Bob",
    )

    updated_cand, report = validator.validate_and_enforce(candidate, parsed)
    assert updated_cand.owner_name == "Bob"
    assert updated_cand.requester_name == "Alice"
    assert report.final_owner == "Bob"


def test_attribution_validator_delegation():
    validator = AttributionValidator()
    candidate = UnifiedTaskCandidate(
        id="task-3",
        title="Deploy to prod",
        owner_name=None,
        requester_name=None,
    )

    parsed = ParsedMessageContent(
        is_quote_reply=True,
        quoted_author_raw="Alice",
        quoted_content_text="We need to deploy build 45",
        actual_content_text="Nhờ anh Huy check giúp em nhé",
        actual_author_raw="Bob",
    )

    updated_cand, report = validator.validate_and_enforce(candidate, parsed)
    assert updated_cand.owner_name == "Alice"  # Hoặc target
    assert updated_cand.requester_name == "Bob"


# ==============================================================================
# 6. END-TO-END PROCESSING PIPELINE INTEGRATION TEST
# ==============================================================================

def test_layer2_end_to_end_pipeline():
    """Tích hợp toàn bộ Layer 2:

    1. Parse HTML Teams quote message.
    2. Lọc heuristic (should_extract).
    3. Trích xuất task candidate (LLMStructuredExtractor).
    4. Xác thực và chuẩn hóa attribution (AttributionValidator).
    5. Phân giải danh tính Owner và Requester (IdentityResolver).
    """
    raw_payload = {
        "body": {
            "contentType": "html",
            "content": (
                "<div>"
                "<blockquote itemscope='' itemtype='http://schema.skype.com/Reply' itemid='1726567000'>"
                "<strong>Nguyen Van Huy</strong>: Can you check why deployment failed on staging?"
                "</blockquote>"
                "<p>Để em check nhé anh. (y)</p>"
                "</div>"
            ),
        },
        "from": {
            "user": {
                "id": "cuong.dam@fpt.com",
                "displayName": "Dam Quang Cuong",
            }
        },
    }

    raw_event = RawEventRecord(
        id="raw-908-teams-fpt",
        tenant_id="tenant-fpt-internal",
        source_type=SourceType.MS_TEAMS,
        external_id="msg-teams-1726567080",
        idempotency_key="idemp-key-test-999",
        author_external_id="cuong.dam@fpt.com",
        author_display_name="Dam Quang Cuong",
        conversation_or_project_id="channel-devops-alerts",
        event_timestamp=datetime(2026, 9, 17, 14, 18, 0, tzinfo=timezone.utc),
        raw_payload=raw_payload,
    )

    # 1. Quote Parser
    quote_parser = TeamsQuoteReplyParser()
    parsed_msg = quote_parser.parse(raw_event.raw_payload)
    assert parsed_msg.is_quote_reply is True
    assert parsed_msg.quoted_author_raw == "Nguyen Van Huy"
    assert parsed_msg.actual_author_raw == "Dam Quang Cuong"
    assert "(y)" not in parsed_msg.actual_content_text
    assert "Để em check nhé anh." in parsed_msg.actual_content_text

    # 2. Heuristic Filter
    heuristic_filter = HeuristicCandidateFilter()
    should_extract = heuristic_filter.should_extract(parsed_msg.actual_content_text)
    assert should_extract is True

    # 3. LLM Structured Extractor (Fallback rule-based)
    extractor = LLMStructuredExtractor(mock_mode=True)
    candidate = extractor.extract_from_parsed(
        parsed_msg,
        raw_event_id=raw_event.id,
        source_type=raw_event.source_type.value,
    )
    assert candidate.review_status == "auto_approved"
    assert candidate.extraction_confidence >= 0.65

    # 4. Attribution Validator
    validator = AttributionValidator()
    validated_candidate, report = validator.validate_and_enforce(candidate, parsed_msg)
    assert validated_candidate.owner_name == "Dam Quang Cuong"
    assert validated_candidate.requester_name == "Nguyen Van Huy"

    # 5. Identity Resolver
    resolver = IdentityResolver()
    # Đăng ký thông tin Person mẫu
    resolver.register_person(
        person_id="00000000-0000-0000-0000-000000000002",
        canonical_name="Dam Quang Cuong",
        primary_email="cuong.dam@fpt.com",
        account_id="cuong.dam@fpt.com",
    )
    resolver.register_person(
        person_id="00000000-0000-0000-0000-000000000003",
        canonical_name="Nguyen Van Huy",
        primary_email="huy.nguyen@fpt.com",
        account_id="huy.nguyen@fpt.com",
    )

    owner_res = resolver.resolve(
        email="cuong.dam@fpt.com",
        display_name=validated_candidate.owner_name,
    )
    assert owner_res.is_exact_match is True
    assert owner_res.person_id == "00000000-0000-0000-0000-000000000002"

    req_res = resolver.resolve(
        email="huy.nguyen@fpt.com",
        display_name=validated_candidate.requester_name,
    )
    assert req_res.is_exact_match is True
    assert req_res.person_id == "00000000-0000-0000-0000-000000000003"

    # Gán canonical IDs lên candidate
    validated_candidate.owner_canonical_id = owner_res.person_id
    validated_candidate.requester_canonical_id = req_res.person_id

    assert validated_candidate.owner_canonical_id == "00000000-0000-0000-0000-000000000002"
    assert validated_candidate.requester_canonical_id == "00000000-0000-0000-0000-000000000003"
    assert len(validated_candidate.evidences) == 1
    assert validated_candidate.evidences[0].raw_event_id == raw_event.id


# ==============================================================================
# 7. TESTS FOR INTELLIGENCE AUTO-WIRING (Sub-Agent 2A)
# ==============================================================================

class _InMemoryProcessingTaskRepo:
    """Mock repository for TaskDomainRepository used in intelligence auto-wiring tests."""

    def __init__(self, tasks=None):
        self.tasks = {t.id: t for t in (tasks or [])}
        self.upserted_tasks = []

    async def get_task_by_id(self, task_id: str):
        return self.tasks.get(task_id)

    async def list_tasks(self, filters=None):
        return list(self.tasks.values())

    async def get_active_tasks(self):
        return [
            t for t in self.tasks.values()
            if t.status in (TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED)
        ]

    async def upsert_task_atomic(self, task):
        self.tasks[task.id] = task
        self.upserted_tasks.append(task)
        return task.id

    async def record_merge_audit(self, audit):
        pass


class _InMemoryProcessingRawEventRepo:
    """Mock repository for RawEventRepository used in intelligence auto-wiring tests."""

    def __init__(self, events=None):
        self.events = {e.id: e for e in (events or [])}
        self.status_calls = []
        self.attempt_records = []

    async def get_pending_raw_events(self, limit=50):
        return list(self.events.values())[:limit]

    async def mark_event_status(
        self,
        event_id,
        status,
        error=None,
        next_retry_at=None,
        processed_at=None,
        processor_version=None,
    ):
        if event_id in self.events:
            self.events[event_id].processing_status = status
        self.status_calls.append({"event_id": event_id, "status": status})

    async def record_processing_attempt(self, attempt):
        self.attempt_records.append(attempt)


def _make_auto_wire_raw_event(
    event_id="raw-auto-wire-01",
    content="<p>Để em fix issue OPS-88 trước 5h chiều nay nhé anh.</p>",
    author_name="Dam Quang Cuong",
    author_email="cuong.dam@fpt.com",
):
    return RawEventRecord(
        id=event_id,
        tenant_id="tenant-auto-wire",
        source_type=SourceType.MS_TEAMS,
        external_id=f"ext-{event_id}",
        idempotency_key=f"idemp-{event_id}",
        author_external_id=author_email,
        author_display_name=author_name,
        conversation_or_project_id="channel-ops",
        event_timestamp=datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc),
        raw_payload={
            "body": {"content": content, "contentType": "html"},
            "from": {"user": {"displayName": author_name, "id": author_email}},
        },
        normalized_text="Để em fix issue OPS-88 trước 5h chiều nay nhé anh.",
        processing_status=ProcessingStatus.PENDING,
    )


@pytest.mark.asyncio
async def test_pipeline_intelligence_lifecycle_auto_wired_on_new_task():
    """Tự động gọi intelligence_lifecycle.on_task_changed với đúng task ID khi tạo mới task."""
    task_repo = _InMemoryProcessingTaskRepo()
    extractor = LLMStructuredExtractor(mock_mode=True)
    mock_lifecycle = AsyncMock()

    pipeline = ProcessingPipeline(
        task_repo=task_repo,
        llm_extractor=extractor,
        intelligence_lifecycle=mock_lifecycle,
    )

    raw_event = _make_auto_wire_raw_event(event_id="raw-new-task-wire")
    result = await pipeline.process(raw_event)

    assert result.status == ProcessingStatus.PROCESSED
    assert result.merged is False
    assert result.task_id is not None

    # Task đã được lưu vào repository
    assert len(task_repo.upserted_tasks) == 1
    assert task_repo.upserted_tasks[0].id == result.task_id

    # on_task_changed được gọi đúng 1 lần với đúng task ID
    assert mock_lifecycle.on_task_changed.await_count == 1
    call_args = mock_lifecycle.on_task_changed.await_args
    assert call_args.args[0] == result.task_id


@pytest.mark.asyncio
async def test_pipeline_intelligence_lifecycle_auto_wired_on_merge():
    """Tự động gọi intelligence_lifecycle.on_task_changed với đúng task ID sau khi merge thành công."""
    existing_task = UnifiedTaskCandidate(
        id="task-existing-anchor-ops88",
        title="Resolve OPS-88 deployment bug",
        status=TaskStatus.TODO,
        owner_name="Dam Quang Cuong",
        evidences=[
            EvidenceRecord(
                id="ev-01",
                task_id="task-existing-anchor-ops88",
                raw_event_id="raw-prev-01",
                evidence_type=EvidenceType.JIRA_TICKET,
                source_type="jira",
                timestamp=datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc),
                snippet="OPS-88: Deployment bug on production",
            )
        ],
    )
    task_repo = _InMemoryProcessingTaskRepo(tasks=[existing_task])
    extractor = LLMStructuredExtractor(mock_mode=True)
    mock_lifecycle = AsyncMock()

    pipeline = ProcessingPipeline(
        task_repo=task_repo,
        llm_extractor=extractor,
        intelligence_lifecycle=mock_lifecycle,
    )

    # Raw event thứ 2 cũng có anchor OPS-88 -> sẽ merge
    raw_event = _make_auto_wire_raw_event(
        event_id="raw-followup-ops88",
        content="<p>Em đang xử lý hotfix cho OPS-88 rồi nha anh.</p>",
    )
    result = await pipeline.process(raw_event)

    assert result.status == ProcessingStatus.PROCESSED
    assert result.merged is True
    assert result.target_task_id == "task-existing-anchor-ops88"
    assert result.task_id == "task-existing-anchor-ops88"

    # on_task_changed được gọi đúng 1 lần với target task ID của task được merge
    assert mock_lifecycle.on_task_changed.await_count == 1
    call_args = mock_lifecycle.on_task_changed.await_args
    assert call_args.args[0] == "task-existing-anchor-ops88"


@pytest.mark.asyncio
async def test_pipeline_intelligence_lifecycle_none_safe():
    """Pipeline hoạt động hoàn toàn an toàn khi intelligence_lifecycle là None (không throw exception)."""
    task_repo = _InMemoryProcessingTaskRepo()
    extractor = LLMStructuredExtractor(mock_mode=True)

    # intelligence_lifecycle=None tường minh
    pipeline = ProcessingPipeline(
        task_repo=task_repo,
        llm_extractor=extractor,
        intelligence_lifecycle=None,
    )

    raw_event = _make_auto_wire_raw_event(event_id="raw-none-safe")
    result = await pipeline.process(raw_event)

    assert result.status == ProcessingStatus.PROCESSED
    assert result.task_id is not None
    assert len(task_repo.upserted_tasks) == 1


@pytest.mark.asyncio
async def test_processing_worker_propagates_intelligence_lifecycle_to_pipeline():
    """ProcessingWorker nhận intelligence_lifecycle và tự động wire vào pipeline."""
    task_repo = _InMemoryProcessingTaskRepo()
    extractor = LLMStructuredExtractor(mock_mode=True)
    raw_event = _make_auto_wire_raw_event(event_id="raw-worker-wire")
    raw_repo = _InMemoryProcessingRawEventRepo(events=[raw_event])

    pipeline = ProcessingPipeline(task_repo=task_repo, llm_extractor=extractor)
    assert pipeline.intelligence_lifecycle is None

    mock_lifecycle = AsyncMock()
    worker = ProcessingWorker(
        raw_event_repo=raw_repo,
        pipeline=pipeline,
        intelligence_lifecycle=mock_lifecycle,
    )

    # Worker tự động kết nối intelligence_lifecycle vào pipeline
    assert worker.intelligence_lifecycle is mock_lifecycle
    assert pipeline.intelligence_lifecycle is mock_lifecycle

    status = await worker.process_event(raw_event)
    assert status == ProcessingStatus.PROCESSED
    assert raw_repo.events["raw-worker-wire"].processing_status == ProcessingStatus.PROCESSED

    # on_task_changed được gọi với đúng task ID
    assert mock_lifecycle.on_task_changed.await_count == 1
    saved_task_id = task_repo.upserted_tasks[0].id
    assert mock_lifecycle.on_task_changed.await_args.args[0] == saved_task_id


@pytest.mark.asyncio
async def test_processing_worker_safe_when_intelligence_lifecycle_none():
    """ProcessingWorker hoạt động an toàn khi intelligence_lifecycle là None."""
    task_repo = _InMemoryProcessingTaskRepo()
    extractor = LLMStructuredExtractor(mock_mode=True)
    raw_event = _make_auto_wire_raw_event(event_id="raw-worker-none-safe")
    raw_repo = _InMemoryProcessingRawEventRepo(events=[raw_event])

    pipeline = ProcessingPipeline(task_repo=task_repo, llm_extractor=extractor)
    worker = ProcessingWorker(
        raw_event_repo=raw_repo,
        pipeline=pipeline,
        intelligence_lifecycle=None,
    )

    assert worker.intelligence_lifecycle is None
    assert pipeline.intelligence_lifecycle is None

    status = await worker.process_event(raw_event)
    assert status == ProcessingStatus.PROCESSED
    assert raw_repo.events["raw-worker-none-safe"].processing_status == ProcessingStatus.PROCESSED
    assert len(task_repo.upserted_tasks) == 1


@pytest.mark.asyncio
async def test_pipeline_full_integration_with_real_intelligence_lifecycle():
    """Kiểm tra tích hợp hoàn chỉnh với TaskIntelligenceLifecycle thật:

    new evidence -> task persisted -> priority recomputed -> inferred status recomputed -> derived fields persisted.
    """
    from ptb_intelligence.lifecycle import TaskIntelligenceLifecycle
    from ptb_intelligence.priority import DeterministicPriorityEngine
    from ptb_intelligence.status_machine import StatusInferenceMachine

    task_repo = _InMemoryProcessingTaskRepo()
    priority_engine = DeterministicPriorityEngine()
    status_machine = StatusInferenceMachine()

    real_lifecycle = TaskIntelligenceLifecycle(
        task_repo=task_repo,
        priority_engine=priority_engine,
        status_machine=status_machine,
    )

    pipeline = ProcessingPipeline(
        task_repo=task_repo,
        llm_extractor=LLMStructuredExtractor(mock_mode=True),
        intelligence_lifecycle=real_lifecycle,
    )

    raw_event = _make_auto_wire_raw_event(
        event_id="raw-real-lifecycle-01",
        content="<blockquote><strong>Huy</strong>: Can you fix urgent bug on production?</blockquote><p>Để em xử lý ngay nhé.</p>",
    )

    result = await pipeline.process(raw_event)
    assert result.status == ProcessingStatus.PROCESSED
    assert result.task_id is not None

    # Kiểm tra task trong repo đã được cập nhật priority và status tự động
    persisted_task = await task_repo.get_task_by_id(result.task_id)
    assert persisted_task is not None
    assert persisted_task.priority_score is not None
    assert persisted_task.priority_score > 0.0
    assert result.candidate.priority_score == persisted_task.priority_score
    # Inferred status hoặc status được cập nhật
    assert persisted_task.status in (TaskStatus.TODO, TaskStatus.IN_PROGRESS)

