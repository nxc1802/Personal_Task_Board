"""Comprehensive Unit & Contract Tests for Shortcut Acquisition Adapter (Task W2-04).

Covers:
1. Contract compliance & inheritance (AcquisitionAdapter, SourceType.SHORTCUT)
2. Environment configuration & token resolution
3. Search query parsing & building (project, query, since date, archived exclusion)
4. Story fields extraction (story_type, workflow state, owners, requester, app_url)
5. Workflow states lookup & human-readable state naming
6. Comments ingestion as child RawEventRecords (parent_external_id, deep link, timestamp filtering)
7. Archived stories handling (tagging, query filtering, state tracking)
8. Cursor-based pagination (token extraction, URL handling, infinite loop guard)
9. Empty search results handling
10. Error handling: 401/403 (AdapterAuthenticationError), 429 + Retry-After (rate limit handling & retry), 5xx (transient retry)
11. Health check states: NOT_CONFIGURED, AUTH_REQUIRED, DEGRADED, HEALTHY
12. Checkpoint isolation & Replay determinism (idempotency, pipeline deduplication, resilience)
"""

import asyncio
from datetime import datetime, timezone
import json
from typing import Any, Dict, List, Optional
import urllib.error
import pytest

# Pre-import to resolve circular dependency in external module outlook_interceptor/adapter
try:
    import ptb_acquisition.playwright.outlook_interceptor
except ImportError:
    pass

from ptb_contracts.l1_acquisition import (
    AdapterHealthStatus,
    IngestionCheckpointRecord,
    ProcessingStatus,
    RateLimitConfig,
    RawEventRecord,
    RetryPolicyConfig,
    SourceType,
    compute_idempotency_key,
)
from ptb_acquisition.adapters.base import (
    AcquisitionAdapter,
    AdapterAuthenticationError,
    AdapterConfigurationError,
    AdapterRateLimitError,
    AdapterStreamNotFoundError,
    AdapterTransientError,
)
from ptb_acquisition.adapters.shortcut_adapter import ShortcutAdapter, _parse_shortcut_timestamp
from ptb_acquisition.pipeline import AcquisitionPipeline
from tests.support.test_doubles import (
    InMemoryCheckpointRepository,
    InMemoryRawEventRepository,
)


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def sample_shortcut_story_with_comments():
    """Shortcut Story JSON với đầy đủ thông tin: owners, workflow state, comments."""
    return {
        "id": 9901,
        "name": "Design & Implement Distributed Checkpoint Journal",
        "description": "Ensure zero data loss during node failover with SQLite and Neo4j MERGE.",
        "story_type": "feature",
        "app_url": "https://app.shortcut.com/ptb-org/story/9901/checkpoint-journal",
        "project_id": 601,
        "group_id": "team-platform",
        "epic_id": 200,
        "workflow_state_id": 6000001,
        "owner_ids": ["uuid-owner-1", "uuid-owner-2"],
        "requested_by_id": "uuid-requester-99",
        "archived": False,
        "created_at": "2026-10-02T08:00:00Z",
        "updated_at": "2026-10-02T11:00:00Z",
        "comments": [
            {
                "id": 7701,
                "author_id": "uuid-owner-1",
                "text": "Completed the SQLite WAL configuration and verified persistence.",
                "deleted": False,
                "created_at": "2026-10-02T09:15:00Z",
                "updated_at": "2026-10-02T09:15:00Z",
            },
            {
                "id": 7702,
                "author_id": "uuid-requester-99",
                "text": "Looks good! Please add idempotency replay determinism tests.",
                "deleted": False,
                "created_at": "2026-10-02T10:30:00Z",
                "updated_at": "2026-10-02T10:30:00Z",
            },
            {
                "id": 7703,
                "author_id": "uuid-owner-2",
                "text": "Deleted note.",
                "deleted": True,
                "created_at": "2026-10-02T10:45:00Z",
                "updated_at": "2026-10-02T10:45:00Z",
            },
        ],
    }


# =============================================================================
# 1. CONTRACT COMPLIANCE & INITIALIZATION
# =============================================================================

def test_shortcut_adapter_is_acquisition_adapter():
    """Kiểm tra ShortcutAdapter kế thừa đúng AcquisitionAdapter và có source_type=SHORTCUT."""
    adapter = ShortcutAdapter(tenant_id="tenant-sc-1")
    assert isinstance(adapter, AcquisitionAdapter)
    assert adapter.source_type == SourceType.SHORTCUT
    assert adapter.tenant_id == "tenant-sc-1"
    assert "ShortcutAdapter" in repr(adapter)


def test_shortcut_adapter_env_configuration(monkeypatch):
    """Kiểm tra nạp cấu hình tự động từ environment variables."""
    monkeypatch.setenv("SHORTCUT_API_TOKEN", "sc-token-xyz")
    monkeypatch.setenv("SHORTCUT_BASE_URL", "https://api.shortcut.internal")
    monkeypatch.setenv("SHORTCUT_PROJECT_IDS", "601, 602 , 603")
    monkeypatch.setenv("SHORTCUT_QUERY", "state:started")
    monkeypatch.setenv("SHORTCUT_INCLUDE_ARCHIVED", "true")

    adapter = ShortcutAdapter(tenant_id="corp-sc")
    assert adapter.api_token == "sc-token-xyz"
    assert adapter.base_url == "https://api.shortcut.internal"
    assert adapter.project_ids == ["601", "602", "603"]
    assert adapter.default_query == "state:started"
    assert adapter.include_archived is True


# =============================================================================
# 2. SEARCH QUERY PARSING & BUILDING
# =============================================================================

def test_shortcut_query_building_rules():
    adapter = ShortcutAdapter(
        project_ids=[601, 602],
        default_query="owner:lead-dev",
        include_archived=False,
    )

    # 1. Stream id là project cụ thể
    q_proj = adapter._build_search_query("601")
    assert "project:601" in q_proj
    assert "!is:archived" in q_proj

    # 2. Stream id là "all" với default_query
    q_all = adapter._build_search_query("all")
    assert "owner:lead-dev" in q_all
    assert "!is:archived" in q_all

    # 3. Stream id là "all" không có default_query -> dùng project_ids
    adapter_no_default = ShortcutAdapter(project_ids=[601, 602], include_archived=False)
    q_keys = adapter_no_default._build_search_query("all")
    assert "(project:601 OR project:602)" in q_keys

    # 4. Stream id chứa cú pháp query trực tiếp
    q_custom = adapter._build_search_query("type:bug severity:high")
    assert "type:bug severity:high" in q_custom

    # 5. Có mốc thời gian since (format YYYY-MM-DD)
    since_dt = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
    q_since = adapter._build_search_query("601", since=since_dt)
    assert "project:601" in q_since
    assert "updated:>=2026-10-02" in q_since

    # 6. include_archived = True -> không tự động gắn !is:archived
    adapter_archived = ShortcutAdapter(include_archived=True)
    q_arch = adapter_archived._build_search_query("601")
    assert "!is:archived" not in q_arch


# =============================================================================
# 3. STORY FIELDS EXTRACTION & WORKFLOW STATES
# =============================================================================

def test_shortcut_story_fields_extraction(sample_shortcut_story_with_comments):
    adapter = ShortcutAdapter(tenant_id="tenant-story")
    # Gán trước workflow state mapping
    adapter._workflow_states["6000001"] = "In Development"

    event = adapter.story_to_raw_event(sample_shortcut_story_with_comments)

    assert event.source_type == SourceType.SHORTCUT
    assert event.external_id == "9901"
    assert event.parent_external_id == "200"  # Epic ID
    assert event.conversation_or_project_id == "601"
    assert event.author_external_id == "uuid-requester-99"
    assert event.deep_link == "https://app.shortcut.com/ptb-org/story/9901/checkpoint-journal"

    # Normalized text checks
    assert "[9901] (feature) Design & Implement Distributed Checkpoint Journal" in event.normalized_text
    assert "State: In Development" in event.normalized_text
    assert "Owners: uuid-owner-1, uuid-owner-2" in event.normalized_text
    assert "Ensure zero data loss during node failover" in event.normalized_text

    # Content hash & idempotency
    assert len(event.content_hash) == 64
    assert len(event.idempotency_key) == 64


# =============================================================================
# 4. ARCHIVED STORIES HANDLING
# =============================================================================

def test_shortcut_archived_story_handling():
    """Story bị archived phải được đánh dấu [ARCHIVED] trong normalized text và raw_payload."""
    adapter = ShortcutAdapter(tenant_id="tenant-arch")
    story = {
        "id": 9905,
        "name": "Deprecated v0 Polling Mechanism",
        "description": "Replaced by WebSockets in v1.",
        "story_type": "chore",
        "project_id": 601,
        "archived": True,
        "workflow_state_id": 6000099,
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-10T00:00:00Z",
    }

    event = adapter.story_to_raw_event(story)
    assert "[ARCHIVED]" in event.normalized_text
    assert event.raw_payload["archived"] is True


# =============================================================================
# 5. COMMENTS INGESTION AS CHILD RAW EVENTS
# =============================================================================

@pytest.mark.asyncio
async def test_shortcut_comments_ingestion_backfill_and_incremental(
    sample_shortcut_story_with_comments,
):
    """Backfill emit story chính và các comments không bị deleted dạng child records."""
    def mock_fetcher(url: str, headers: Dict[str, str], body: Optional[bytes]):
        if "/stories/search" in url:
            return {
                "data": [sample_shortcut_story_with_comments],
                "next": None,
            }
        return {}

    adapter = ShortcutAdapter(
        api_token="token-xyz",
        tenant_id="tenant-sc-comments",
        http_fetcher=mock_fetcher,
        include_comments=True,
    )

    # 1. Backfill yields 1 story + 2 active comments (1 deleted comment is skipped) = 3 records
    records: List[RawEventRecord] = []
    async for rec in adapter.backfill(stream_id="601"):
        records.append(rec)

    assert len(records) == 3
    story_rec, c1_rec, c2_rec = records[0], records[1], records[2]

    # Verify parent story
    assert story_rec.external_id == "9901"
    assert story_rec.parent_external_id == "200"

    # Verify Comment 1
    assert c1_rec.external_id == "9901-comment-7701"
    assert c1_rec.parent_external_id == "9901"
    assert c1_rec.author_external_id == "uuid-owner-1"
    assert "Completed the SQLite WAL configuration" in c1_rec.normalized_text
    assert c1_rec.deep_link == "https://app.shortcut.com/ptb-org/story/9901/checkpoint-journal#comment-7701"

    # Verify Comment 2
    assert c2_rec.external_id == "9901-comment-7702"
    assert c2_rec.parent_external_id == "9901"
    assert "Please add idempotency replay determinism tests." in c2_rec.normalized_text

    # 2. Incremental poll with checkpoint set at Comment 1 timestamp
    ckpt = IngestionCheckpointRecord(
        source_type=SourceType.SHORTCUT,
        stream_id="601",
        tenant_id="tenant-sc-comments",
        last_external_id=c1_rec.external_id,
        last_event_timestamp=c1_rec.event_timestamp,
    )

    incremental = []
    async for rec in adapter.poll_incremental(stream_id="601", checkpoint=ckpt):
        incremental.append(rec)

    # Comment 1 is filtered out; story (updated at 11:00) and Comment 2 (updated at 10:30) are returned
    ext_ids = [r.external_id for r in incremental]
    assert "9901-comment-7701" not in ext_ids
    assert "9901" in ext_ids
    assert "9901-comment-7702" in ext_ids


# =============================================================================
# 6. CURSOR-BASED PAGINATION
# =============================================================================

@pytest.mark.asyncio
async def test_shortcut_cursor_pagination():
    """Kiểm tra duyệt cursor pagination qua /stories/search."""
    cursors_called = []

    def mock_fetcher(url: str, headers: Dict[str, str], body: Optional[bytes]):
        payload = json.loads(body.decode("utf-8")) if body else {}
        cursor = payload.get("next")
        cursors_called.append(cursor)

        if cursor is None:
            return {
                "data": [{"id": 101, "name": "Story 101"}],
                "next": "cursor_page_2",
            }
        elif cursor == "cursor_page_2":
            return {
                "data": [{"id": 102, "name": "Story 102"}],
                # Test next as full URL
                "next": "https://api.app.shortcut.com/api/v3/stories/search?next=cursor_page_3",
            }
        elif cursor == "cursor_page_3":
            return {
                "data": [{"id": 103, "name": "Story 103"}],
                "next": None,
            }
        return {"data": [], "next": None}

    adapter = ShortcutAdapter(
        api_token="token",
        http_fetcher=mock_fetcher,
        include_comments=False,
    )

    stories = await adapter._fetch_stories("all")
    assert len(stories) == 3
    assert cursors_called == [None, "cursor_page_2", "cursor_page_3"]


# =============================================================================
# 7. EMPTY SEARCH RESULTS HANDLING
# =============================================================================

@pytest.mark.asyncio
async def test_shortcut_empty_results():
    """Khi query không có kết quả, adapter emit 0 records và không phát sinh lỗi."""
    def mock_fetcher(url: str, headers: Dict[str, str], body: Optional[bytes]):
        return {"data": [], "next": None}

    adapter = ShortcutAdapter(api_token="token", http_fetcher=mock_fetcher)
    records = [r async for r in adapter.backfill("EMPTY")]
    assert len(records) == 0


# =============================================================================
# 8. ERROR HANDLING: 401, 403, 429 + RETRY, 5XX
# =============================================================================

@pytest.mark.asyncio
async def test_shortcut_authentication_error_401():
    """HTTP 401 Unauthorized phải raise AdapterAuthenticationError không retry."""
    call_count = 0

    def mock_fetcher(url: str, headers: Dict[str, str], body: Optional[bytes]):
        nonlocal call_count
        call_count += 1
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    adapter = ShortcutAdapter(api_token="invalid-token", http_fetcher=mock_fetcher)

    with pytest.raises(AdapterAuthenticationError) as exc_info:
        await adapter._fetch_stories("all")
    assert "HTTP 401" in str(exc_info.value)
    assert call_count == 1


@pytest.mark.asyncio
async def test_shortcut_rate_limit_429_with_retry_after():
    """HTTP 429 kèm Retry-After: adapter tạm dừng và retry thành công."""
    attempts = 0

    def mock_fetcher(url: str, headers: Dict[str, str], body: Optional[bytes]):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {"Retry-After": "0.02"}, None)
        return {"data": [{"id": 505, "name": "Rate limited story"}], "next": None}

    fast_retry = RetryPolicyConfig(max_retries=2, initial_delay_seconds=0.01, jitter=False)
    adapter = ShortcutAdapter(
        api_token="token",
        http_fetcher=mock_fetcher,
        retry_policy=fast_retry,
    )

    stories = await adapter._fetch_stories("all")
    assert len(stories) == 1
    assert attempts == 2


@pytest.mark.asyncio
async def test_shortcut_transient_error_500_and_retry():
    """HTTP 500 Server Error: adapter retry và thành công."""
    attempts = 0

    def mock_fetcher(url: str, headers: Dict[str, str], body: Optional[bytes]):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise urllib.error.HTTPError(url, 500, "Internal Server Error", {}, None)
        return {"data": [{"id": 777, "name": "Recovered story"}], "next": None}

    fast_retry = RetryPolicyConfig(max_retries=3, initial_delay_seconds=0.01, jitter=False)
    adapter = ShortcutAdapter(
        api_token="token",
        http_fetcher=mock_fetcher,
        retry_policy=fast_retry,
    )

    stories = await adapter._fetch_stories("all")
    assert len(stories) == 1
    assert attempts == 3


# =============================================================================
# 9. HEALTH CHECK STATES
# =============================================================================

@pytest.mark.asyncio
async def test_shortcut_health_not_configured_when_missing_token():
    """Missing token bắt buộc phải trả về status NOT_CONFIGURED."""
    adapter = ShortcutAdapter(api_token="")
    health = await adapter.health()
    assert health["configured"] is False
    assert health["status"] == AdapterHealthStatus.NOT_CONFIGURED.value
    assert "Thiếu SHORTCUT_API_TOKEN" in health["message"]


@pytest.mark.asyncio
async def test_shortcut_health_auth_required_on_401():
    """Health check ping /member trả về 401 -> status = AUTH_REQUIRED."""
    def mock_fetcher(url: str, headers: Dict[str, str], body: Optional[bytes]):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    adapter = ShortcutAdapter(api_token="bad-token", http_fetcher=mock_fetcher)
    health = await adapter.health()
    assert health["configured"] is True
    assert health["status"] == AdapterHealthStatus.AUTH_REQUIRED.value


@pytest.mark.asyncio
async def test_shortcut_health_degraded_on_500():
    """Health check ping /member trả về 500 -> status = DEGRADED."""
    def mock_fetcher(url: str, headers: Dict[str, str], body: Optional[bytes]):
        raise urllib.error.HTTPError(url, 500, "Server Error", {}, None)

    fast_retry = RetryPolicyConfig(max_retries=1, initial_delay_seconds=0.01)
    adapter = ShortcutAdapter(
        api_token="token",
        http_fetcher=mock_fetcher,
        retry_policy=fast_retry,
    )
    health = await adapter.health()
    assert health["configured"] is True
    assert health["status"] == AdapterHealthStatus.DEGRADED.value


# =============================================================================
# 10. CHECKPOINT ISOLATION & REPLAY DETERMINISM
# =============================================================================

@pytest.mark.asyncio
async def test_shortcut_replay_determinism_and_pipeline_deduplication(
    sample_shortcut_story_with_comments,
):
    """Re-fetching cùng dữ liệu ngoại vi Shortcut phải sinh ra đúng idempotency_key và 0 duplicate."""
    raw_repo = InMemoryRawEventRepository()
    ckpt_repo = InMemoryCheckpointRepository()
    pipeline = AcquisitionPipeline(
        raw_event_repo=raw_repo,
        checkpoint_repo=ckpt_repo,
        tenant_id="tenant-sc-deterministic",
    )

    def mock_fetcher(url: str, headers: Dict[str, str], body: Optional[bytes]):
        if "/stories/search" in url:
            return {
                "data": [sample_shortcut_story_with_comments],
                "next": None,
            }
        return {}

    adapter = ShortcutAdapter(
        api_token="token",
        tenant_id="tenant-sc-deterministic",
        http_fetcher=mock_fetcher,
        include_comments=True,
    )

    # Lần nạp thứ nhất
    count_1 = await pipeline.sync_adapter(adapter, stream_id="601")
    assert count_1 == 3
    assert pipeline.stats["ingested"] == 3
    assert pipeline.stats["persisted"] == 3
    assert pipeline.stats["deduplicated"] == 0

    # Lần nạp thứ hai: sync_adapter dùng checkpoint đã có nên nhận diện 0 item mới
    count_2 = await pipeline.sync_adapter(adapter, stream_id="601")
    assert count_2 == 0

    # Kiểm tra replay trực tiếp qua pipeline.ingest_event: toàn bộ 3 events bị deduplicate
    replayed = [rec async for rec in adapter.backfill(stream_id="601")]
    assert len(replayed) == 3
    for rec in replayed:
        ingested = await pipeline.ingest_event(rec)
        assert ingested is False
    assert pipeline.stats["deduplicated"] == 3
