"""Comprehensive Unit & Contract Tests for Jira Acquisition Adapter (Task W2-04).

Covers:
1. Contract compliance & inheritance (AcquisitionAdapter)
2. Cloud vs Server API compatibility (v3 vs v2, Basic auth, Bearer PAT, v3->v2 fallback on 404)
3. JQL query parsing & building (projects, default JQL, custom JQL, since UTC timestamp)
4. ADF (Atlassian Document Format) rich parsing (paragraphs, headings, lists, codeBlock, tables, mentions, marks)
5. Issue status, transitions, changelog, priority, resolution, labels, issuetype tracking
6. Comments ingestion as child RawEventRecords (parent_external_id, deep link, timestamp filtering)
7. Pagination (startAt/maxResults multi-page traversal)
8. Empty results handling
9. Error handling: 401/403 (AdapterAuthenticationError), 429 + Retry-After (rate limit handling & retry), 5xx (transient retry)
10. Health check states: NOT_CONFIGURED, AUTH_REQUIRED, DEGRADED, HEALTHY
11. Checkpoint isolation & Replay determinism (idempotency, pipeline deduplication, failure resilience)
"""

import asyncio
from datetime import datetime, timedelta, timezone
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
    SourceSyncState,
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
from ptb_acquisition.adapters.jira_adapter import JiraAdapter, _extract_jira_text, _parse_jira_timestamp
from ptb_acquisition.pipeline import AcquisitionPipeline
from tests.support.test_doubles import (
    InMemoryCheckpointRepository,
    InMemoryRawEventRepository,
)


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def sample_adf_complex():
    """Tạo tài liệu ADF Atlassian Document Format phức tạp với đầy đủ node types."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "heading",
                "attrs": {"level": 2},
                "content": [{"type": "text", "text": "Architecture Overview"}],
            },
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "This task implements "},
                    {
                        "type": "text",
                        "text": "W2-04 Jira Adapter",
                        "marks": [{"type": "strong"}, {"type": "code"}],
                    },
                    {"type": "text", "text": " for "},
                    {
                        "type": "text",
                        "text": "PTB Platform",
                        "marks": [{"type": "link", "attrs": {"href": "https://github.com/nxc1802/Personal_Task_Board"}}],
                    },
                    {"type": "text", "text": "."},
                ],
            },
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Supports Cloud ADF and Server plain text"}],
                            }
                        ],
                    },
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Handles 429 rate limits with Retry-After"}],
                            }
                        ],
                    },
                ],
            },
            {
                "type": "codeBlock",
                "attrs": {"language": "python"},
                "content": [{"type": "text", "text": "def test_sync():\n    return True"}],
            },
            {
                "type": "blockquote",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Important notice from lead architect."}],
                    }
                ],
            },
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Assigned to "},
                    {"type": "mention", "attrs": {"id": "acc-9999", "text": "@lead_architect"}},
                    {"type": "text", "text": " "},
                    {"type": "emoji", "attrs": {"shortName": ":rocket:"}},
                ],
            },
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableHeader", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Key"}]}]},
                            {"type": "tableHeader", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Status"}]}]},
                        ],
                    },
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableCell", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "W2-04"}]}]},
                            {"type": "tableCell", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "In Progress"}]}]},
                        ],
                    },
                ],
            },
        ],
    }


@pytest.fixture
def sample_jira_issue_with_comments_and_transitions(sample_adf_complex):
    """Jira Issue JSON đầy đủ summary, description ADF, statusCategory, changelog và comments."""
    return {
        "id": "20001",
        "key": "DEV-201",
        "fields": {
            "summary": "Implement durable ingestion pipeline for Jira Cloud",
            "description": sample_adf_complex,
            "status": {
                "name": "In Development",
                "statusCategory": {"id": 4, "key": "indeterminate", "name": "In Progress"},
            },
            "priority": {"name": "High"},
            "resolution": None,
            "issuetype": {"name": "Task"},
            "labels": ["backend", "acquisition", "w2-04"],
            "assignee": {"displayName": "Tech Lead", "name": "lead-dev"},
            "creator": {
                "accountId": "acc-pm-100",
                "displayName": "Product Manager",
                "emailAddress": "pm@example.com",
            },
            "project": {"key": "DEV", "name": "Core Dev"},
            "parent": {"key": "DEV-200"},
            "created": "2026-10-01T08:00:00.000+0000",
            "updated": "2026-10-01T11:30:00.000+0000",
            "comment": {
                "comments": [
                    {
                        "id": "c-101",
                        "author": {"accountId": "acc-qa-1", "displayName": "QA Engineer"},
                        "body": "Initial QA review passed for test doubles.",
                        "created": "2026-10-01T09:00:00.000+0000",
                        "updated": "2026-10-01T09:00:00.000+0000",
                    },
                    {
                        "id": "c-102",
                        "author": {"accountId": "acc-pm-100", "displayName": "Product Manager"},
                        "body": {
                            "type": "doc",
                            "version": 1,
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Please make sure 429 retries are covered."}],
                                }
                            ],
                        },
                        "created": "2026-10-01T10:15:00.000+0000",
                        "updated": "2026-10-01T10:15:00.000+0000",
                    },
                ]
            },
        },
        "changelog": {
            "histories": [
                {
                    "created": "2026-10-01T08:30:00.000+0000",
                    "author": {"displayName": "Product Manager"},
                    "items": [
                        {"field": "status", "fromString": "To Do", "toString": "In Development"}
                    ],
                }
            ]
        },
    }


# =============================================================================
# 1. CONTRACT COMPLIANCE & INITIALIZATION
# =============================================================================

def test_jira_adapter_is_acquisition_adapter():
    """Kiểm tra JiraAdapter kế thừa đúng AcquisitionAdapter và có source_type=JIRA."""
    adapter = JiraAdapter(tenant_id="tenant-jira-1")
    assert isinstance(adapter, AcquisitionAdapter)
    assert adapter.source_type == SourceType.JIRA
    assert adapter.tenant_id == "tenant-jira-1"
    assert "JiraAdapter" in repr(adapter)


def test_jira_adapter_env_configuration(monkeypatch):
    """Kiểm tra nạp cấu hình tự động từ environment variables."""
    monkeypatch.setenv("JIRA_BASE_URL", "https://jira.corp.internal")
    monkeypatch.setenv("JIRA_EMAIL", "bot@corp.internal")
    monkeypatch.setenv("JIRA_API_TOKEN", "secret-token")
    monkeypatch.setenv("JIRA_PROJECT_KEYS", "DEV, INFRA , SEC")
    monkeypatch.setenv("JIRA_JQL", "status != Closed")
    monkeypatch.setenv("JIRA_API_VERSION", "2")

    adapter = JiraAdapter(tenant_id="corp-user")
    assert adapter.base_url == "https://jira.corp.internal"
    assert adapter.email == "bot@corp.internal"
    assert adapter.api_token == "secret-token"
    assert adapter.project_keys == ["DEV", "INFRA", "SEC"]
    assert adapter.default_jql == "status != Closed"
    assert adapter.api_version == "2"


# =============================================================================
# 2. CLOUD VS SERVER API COMPATIBILITY
# =============================================================================

def test_jira_adapter_cloud_headers_basic_auth():
    """Cloud API dùng Basic Auth với base64(email:api_token)."""
    adapter = JiraAdapter(
        base_url="https://cloud.atlassian.net",
        email="dev@company.com",
        api_token="jira-token-123",
    )
    headers = adapter._get_headers()
    assert "Authorization" in headers
    assert headers["Authorization"].startswith("Basic ")
    assert headers["Accept"] == "application/json"


def test_jira_adapter_server_headers_bearer_pat():
    """Server/Data Center dùng Personal Access Token (Bearer)."""
    adapter = JiraAdapter(
        base_url="https://jira.onprem.local",
        api_token="pat-token-456",
        # email is None
    )
    headers = adapter._get_headers()
    assert headers["Authorization"] == "Bearer pat-token-456"


@pytest.mark.asyncio
async def test_jira_adapter_cloud_to_server_v3_fallback_to_v2():
    """Khi gọi Jira Server API v3 trả về 404, adapter tự động fallback về API v2."""
    calls = []

    def mock_fetcher(url: str, headers: Dict[str, str], body: Optional[bytes]):
        calls.append(url)
        if "/rest/api/3/" in url:
            # Giả lập Jira Server không hỗ trợ v3 (HTTP 404)
            err = urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            raise err
        if "/rest/api/2/myself" in url:
            return {"displayName": "Server Admin", "name": "admin"}
        return {}

    adapter = JiraAdapter(
        base_url="https://jira.server.local",
        api_token="pat-token",
        api_version="3",
        http_fetcher=mock_fetcher,
    )

    health = await adapter.health()
    assert health["status"] == AdapterHealthStatus.HEALTHY.value
    assert health["user"] == "Server Admin"
    assert adapter.api_version == "2"
    assert len(calls) == 2
    assert "/rest/api/3/myself" in calls[0]
    assert "/rest/api/2/myself" in calls[1]


# =============================================================================
# 3. JQL QUERY PARSING & BUILDING
# =============================================================================

def test_jira_jql_building_rules():
    adapter = JiraAdapter(
        project_keys=["PROJ", "CORE"],
        default_jql="assignee = currentUser()",
    )

    # 1. Stream id là project cụ thể
    jql_proj = adapter._build_jql("DEV")
    assert "project = 'DEV'" in jql_proj
    assert jql_proj.endswith("ORDER BY updated ASC")

    # 2. Stream id là "all" với default_jql
    jql_all = adapter._build_jql("all")
    assert "(assignee = currentUser())" in jql_all

    # 3. Stream id là "all" không có default_jql -> dùng project_keys
    adapter_no_default = JiraAdapter(project_keys=["PROJ", "CORE"])
    jql_keys = adapter_no_default._build_jql("all")
    assert "project in ('PROJ', 'CORE')" in jql_keys

    # 4. Stream id chứa cú pháp JQL trực tiếp
    jql_custom = adapter._build_jql("jql: priority = 'Critical' AND resolution is EMPTY")
    assert "(priority = 'Critical' AND resolution is EMPTY)" in jql_custom

    # 5. Có mốc thời gian since (phải format YYYY-MM-DD HH:mm chuẩn UTC)
    since_dt = datetime(2026, 10, 5, 14, 30, tzinfo=timezone.utc)
    jql_since = adapter._build_jql("DEV", since=since_dt)
    assert "project = 'DEV'" in jql_since
    assert "updated >= '2026-10-05 14:30'" in jql_since


# =============================================================================
# 4. RICH ADF (ATLASSIAN DOCUMENT FORMAT) PARSING
# =============================================================================

def test_jira_adf_rich_extraction(sample_adf_complex):
    """Trích xuất chi tiết từ ADF với đầy đủ các định dạng paragraph, code, list, table, mention."""
    extracted = _extract_jira_text(sample_adf_complex)

    # Headings
    assert "# Architecture Overview" in extracted
    # Text with marks (strong, code, link)
    assert "W2-04 Jira Adapter" in extracted
    assert "[PTB Platform](https://github.com/nxc1802/Personal_Task_Board)" in extracted
    # Bullet lists
    assert "- Supports Cloud ADF and Server plain text" in extracted
    assert "- Handles 429 rate limits with Retry-After" in extracted
    # Code block
    assert "```python" in extracted
    assert "def test_sync():" in extracted
    # Blockquote
    assert "> Important notice from lead architect." in extracted
    # Mention & emoji
    assert "@lead_architect" in extracted
    assert ":rocket:" in extracted
    # Table
    assert "| Key | Status |" in extracted
    assert "| W2-04 | In Progress |" in extracted


def test_jira_plain_text_fallback():
    """Hỗ trợ plain text từ Jira Server / API v2."""
    plain = "Issue reported by customer: database latency > 500ms."
    assert _extract_jira_text(plain) == plain
    assert _extract_jira_text(None) == ""
    assert _extract_jira_text({}) == ""


# =============================================================================
# 5. ISSUE STATUS, TRANSITIONS & METADATA TRACKING
# =============================================================================

def test_jira_issue_status_transitions_tracking(sample_jira_issue_with_comments_and_transitions):
    adapter = JiraAdapter(
        base_url="https://company.atlassian.net",
        tenant_id="tenant-test",
    )
    event = adapter.issue_to_raw_event(sample_jira_issue_with_comments_and_transitions)

    assert event.source_type == SourceType.JIRA
    assert event.external_id == "DEV-201"
    assert event.parent_external_id == "DEV-200"
    assert event.conversation_or_project_id == "DEV"

    # Status & metadata in text
    assert "Status: In Development" in event.normalized_text
    assert "Category: In Progress" in event.normalized_text
    assert "Priority: High" in event.normalized_text
    assert "Type: Task" in event.normalized_text
    assert "Assignee: Tech Lead" in event.normalized_text
    assert "Labels: backend, acquisition, w2-04" in event.normalized_text

    # Transitions tracking
    assert "Recent Transitions:" in event.normalized_text
    assert "To Do -> In Development by Product Manager" in event.normalized_text

    # Content hash & idempotency
    assert len(event.content_hash) == 64
    assert len(event.idempotency_key) == 64


# =============================================================================
# 6. COMMENTS INGESTION AS CHILD RAW EVENTS
# =============================================================================

@pytest.mark.asyncio
async def test_jira_comments_ingestion_backfill_and_incremental(
    sample_jira_issue_with_comments_and_transitions,
):
    """Backfill emit cả issue chính và các comments đi kèm dạng child records."""
    def mock_fetcher(url: str, headers: Dict[str, str], body: Optional[bytes]):
        if "/search" in url:
            return {
                "startAt": 0,
                "maxResults": 50,
                "total": 1,
                "issues": [sample_jira_issue_with_comments_and_transitions],
            }
        return {}

    adapter = JiraAdapter(
        base_url="https://company.atlassian.net",
        email="bot@company.com",
        api_token="token",
        tenant_id="tenant-comments",
        http_fetcher=mock_fetcher,
        include_comments=True,
    )

    # 1. Backfill yields 1 issue + 2 comments = 3 records
    records: List[RawEventRecord] = []
    async for rec in adapter.backfill(stream_id="DEV"):
        records.append(rec)

    assert len(records) == 3
    issue_rec, c1_rec, c2_rec = records[0], records[1], records[2]

    # Verify parent issue record
    assert issue_rec.external_id == "DEV-201"
    assert issue_rec.parent_external_id == "DEV-200"

    # Verify Comment 1
    assert c1_rec.external_id == "DEV-201-comment-c-101"
    assert c1_rec.parent_external_id == "DEV-201"
    assert c1_rec.author_external_id == "acc-qa-1"
    assert c1_rec.author_display_name == "QA Engineer"
    assert "Initial QA review passed" in c1_rec.normalized_text
    assert c1_rec.deep_link == "https://company.atlassian.net/browse/DEV-201?focusedCommentId=c-101"

    # Verify Comment 2 (ADF formatted)
    assert c2_rec.external_id == "DEV-201-comment-c-102"
    assert c2_rec.parent_external_id == "DEV-201"
    assert "Please make sure 429 retries are covered." in c2_rec.normalized_text

    # 2. Incremental poll with checkpoint filtering:
    # Set checkpoint at Comment 1 timestamp
    ckpt = IngestionCheckpointRecord(
        source_type=SourceType.JIRA,
        stream_id="DEV",
        tenant_id="tenant-comments",
        last_external_id=c1_rec.external_id,
        last_event_timestamp=c1_rec.event_timestamp,
    )

    incremental_records = []
    async for rec in adapter.poll_incremental(stream_id="DEV", checkpoint=ckpt):
        incremental_records.append(rec)

    # Comment 1 is filtered out; DEV-201 (updated at 11:30) and Comment 2 (updated at 10:15) are emitted
    inc_ext_ids = [r.external_id for r in incremental_records]
    assert "DEV-201-comment-c-101" not in inc_ext_ids
    assert "DEV-201" in inc_ext_ids
    assert "DEV-201-comment-c-102" in inc_ext_ids


# =============================================================================
# 7. PAGINATION (startAt / maxResults)
# =============================================================================

@pytest.mark.asyncio
async def test_jira_pagination_traversal():
    """Kiểm tra lặp phân trang qua nhiều trang kết quả của Jira Search API."""
    requests_made = []

    def mock_fetcher(url: str, headers: Dict[str, str], body: Optional[bytes]):
        payload = json.loads(body.decode("utf-8")) if body else {}
        start_at = payload.get("startAt", 0)
        requests_made.append(start_at)

        # 3 pages: 0-50, 50-100, 100-120 (total 120)
        if start_at == 0:
            return {
                "startAt": 0,
                "maxResults": 50,
                "total": 120,
                "issues": [{"id": f"id-{i}", "key": f"P-{i}", "fields": {"summary": f"Issue {i}"}} for i in range(1, 51)],
            }
        elif start_at == 50:
            return {
                "startAt": 50,
                "maxResults": 50,
                "total": 120,
                "issues": [{"id": f"id-{i}", "key": f"P-{i}", "fields": {"summary": f"Issue {i}"}} for i in range(51, 101)],
            }
        elif start_at == 100:
            return {
                "startAt": 100,
                "maxResults": 50,
                "total": 120,
                "issues": [{"id": f"id-{i}", "key": f"P-{i}", "fields": {"summary": f"Issue {i}"}} for i in range(101, 121)],
            }
        return {"startAt": start_at, "maxResults": 50, "total": 120, "issues": []}

    adapter = JiraAdapter(
        base_url="https://jira.company.com",
        api_token="token",
        http_fetcher=mock_fetcher,
        include_comments=False,
    )

    items = []
    async for item in adapter.backfill("PROJ"):
        items.append(item)

    assert len(items) == 120
    assert requests_made == [0, 50, 100]


# =============================================================================
# 8. EMPTY RESULTS HANDLING
# =============================================================================

@pytest.mark.asyncio
async def test_jira_empty_search_results():
    """Khi JQL không khớp với issue nào, adapter hoàn thành êm đẹp và emit 0 records."""
    def mock_fetcher(url: str, headers: Dict[str, str], body: Optional[bytes]):
        return {"startAt": 0, "maxResults": 50, "total": 0, "issues": []}

    adapter = JiraAdapter(
        base_url="https://jira.company.com",
        api_token="token",
        http_fetcher=mock_fetcher,
    )

    items = [rec async for rec in adapter.backfill("EMPTY")]
    assert len(items) == 0


# =============================================================================
# 9. ERROR HANDLING: 401, 403, 429 + RETRY, 5XX
# =============================================================================

@pytest.mark.asyncio
async def test_jira_authentication_error_401():
    """HTTP 401 Unauthorized phải raise AdapterAuthenticationError không retry lãng phí."""
    call_count = 0

    def mock_fetcher(url: str, headers: Dict[str, str], body: Optional[bytes]):
        nonlocal call_count
        call_count += 1
        raise urllib.error.HTTPError(url, 401, "Unauthorized: Token invalid or expired", {}, None)

    adapter = JiraAdapter(
        base_url="https://jira.company.com",
        api_token="bad-token",
        http_fetcher=mock_fetcher,
    )

    with pytest.raises(AdapterAuthenticationError) as exc_info:
        await adapter._fetch_issues("project = 'PROJ'")
    assert "HTTP 401" in str(exc_info.value)
    # 401 không được retry nhiều lần
    assert call_count == 1


@pytest.mark.asyncio
async def test_jira_rate_limit_429_with_retry_after():
    """HTTP 429 kèm Retry-After header: adapter tạm dừng đúng thời gian và retry thành công."""
    attempts = 0

    def mock_fetcher(url: str, headers: Dict[str, str], body: Optional[bytes]):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            err = urllib.error.HTTPError(url, 429, "Too Many Requests", {"Retry-After": "0.02"}, None)
            raise err
        return {
            "startAt": 0,
            "maxResults": 50,
            "total": 1,
            "issues": [{"id": "1", "key": "RATE-1", "fields": {"summary": "Rate limited issue"}}],
        }

    fast_retry = RetryPolicyConfig(max_retries=2, initial_delay_seconds=0.01, jitter=False)
    adapter = JiraAdapter(
        base_url="https://jira.company.com",
        api_token="token",
        http_fetcher=mock_fetcher,
        retry_policy=fast_retry,
    )

    issues = await adapter._fetch_issues("project = 'RATE'")
    assert len(issues) == 1
    assert issues[0]["key"] == "RATE-1"
    assert attempts == 2


@pytest.mark.asyncio
async def test_jira_transient_error_503_and_retry():
    """HTTP 503 Service Unavailable: adapter retry với exponential backoff và thành công."""
    attempts = 0

    def mock_fetcher(url: str, headers: Dict[str, str], body: Optional[bytes]):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            err = urllib.error.HTTPError(url, 503, "Service Temporarily Unavailable", {}, None)
            raise err
        return {
            "startAt": 0,
            "maxResults": 50,
            "total": 1,
            "issues": [{"id": "1", "key": "RETRY-1", "fields": {"summary": "Recovered issue"}}],
        }

    fast_retry = RetryPolicyConfig(max_retries=3, initial_delay_seconds=0.01, jitter=False)
    adapter = JiraAdapter(
        base_url="https://jira.company.com",
        api_token="token",
        http_fetcher=mock_fetcher,
        retry_policy=fast_retry,
    )

    issues = await adapter._fetch_issues("project = 'RETRY'")
    assert len(issues) == 1
    assert attempts == 3


# =============================================================================
# 10. HEALTH CHECK STATES
# =============================================================================

@pytest.mark.asyncio
async def test_jira_health_not_configured_when_missing_token():
    """Missing token hoặc base_url bắt buộc phải trả về status NOT_CONFIGURED."""
    adapter = JiraAdapter(base_url="", api_token="", email="")
    health = await adapter.health()
    assert health["configured"] is False
    assert health["status"] == AdapterHealthStatus.NOT_CONFIGURED.value
    assert "Thiếu JIRA_BASE_URL" in health["message"]


@pytest.mark.asyncio
async def test_jira_health_auth_required_on_401():
    """Health check ping /myself trả về 401 -> status = AUTH_REQUIRED."""
    def mock_fetcher(url: str, headers: Dict[str, str], body: Optional[bytes]):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    adapter = JiraAdapter(
        base_url="https://jira.company.com",
        api_token="expired-token",
        http_fetcher=mock_fetcher,
    )
    health = await adapter.health()
    assert health["configured"] is True
    assert health["status"] == AdapterHealthStatus.AUTH_REQUIRED.value


@pytest.mark.asyncio
async def test_jira_health_degraded_on_500():
    """Health check ping /myself trả về 500 -> status = DEGRADED."""
    def mock_fetcher(url: str, headers: Dict[str, str], body: Optional[bytes]):
        raise urllib.error.HTTPError(url, 500, "Internal Error", {}, None)

    fast_retry = RetryPolicyConfig(max_retries=1, initial_delay_seconds=0.01)
    adapter = JiraAdapter(
        base_url="https://jira.company.com",
        api_token="token",
        http_fetcher=mock_fetcher,
        retry_policy=fast_retry,
    )
    health = await adapter.health()
    assert health["configured"] is True
    assert health["status"] == AdapterHealthStatus.DEGRADED.value


# =============================================================================
# 11. CHECKPOINT ISOLATION & REPLAY DETERMINISM
# =============================================================================

@pytest.mark.asyncio
async def test_jira_replay_determinism_and_pipeline_deduplication(
    sample_jira_issue_with_comments_and_transitions,
):
    """Re-fetching cùng dữ liệu ngoại vi Jira phải sinh ra đúng idempotency_key và 0 duplicate."""
    raw_repo = InMemoryRawEventRepository()
    ckpt_repo = InMemoryCheckpointRepository()
    pipeline = AcquisitionPipeline(
        raw_event_repo=raw_repo,
        checkpoint_repo=ckpt_repo,
        tenant_id="tenant-deterministic",
    )

    def mock_fetcher(url: str, headers: Dict[str, str], body: Optional[bytes]):
        if "/search" in url:
            return {
                "startAt": 0,
                "maxResults": 50,
                "total": 1,
                "issues": [sample_jira_issue_with_comments_and_transitions],
            }
        return {}

    adapter = JiraAdapter(
        base_url="https://company.atlassian.net",
        api_token="token",
        tenant_id="tenant-deterministic",
        http_fetcher=mock_fetcher,
        include_comments=True,
    )

    # Lần nạp thứ nhất
    count_1 = await pipeline.sync_adapter(adapter, stream_id="DEV")
    assert count_1 == 3
    assert pipeline.stats["ingested"] == 3
    assert pipeline.stats["persisted"] == 3
    assert pipeline.stats["deduplicated"] == 0

    # Lần nạp thứ hai: sync_adapter dùng checkpoint đã có nên nhận diện 0 item mới
    count_2 = await pipeline.sync_adapter(adapter, stream_id="DEV")
    assert count_2 == 0

    # Kiểm tra replay trực tiếp qua pipeline.ingest_event: toàn bộ 3 events bị deduplicate
    replayed_records = [rec async for rec in adapter.backfill(stream_id="DEV")]
    assert len(replayed_records) == 3
    for rec in replayed_records:
        ingested = await pipeline.ingest_event(rec)
        assert ingested is False
    assert pipeline.stats["deduplicated"] == 3
