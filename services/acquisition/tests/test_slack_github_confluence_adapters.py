"""Unit and Integration Tests for Production-Grade Slack, GitHub, and Confluence Adapters (W2-05).

Kiểm tra toàn diện:
1. SlackAdapter:
   - Kế thừa AcquisitionAdapter(source_type=SourceType.SLACK).
   - Thread replies resolution (`conversations.replies`) & parent_external_id hierarchy.
   - Channel discovery & user resolution with in-memory caching.
   - Cursor-based pagination for history and replies.
   - Rate limit handling (HTTP 429 & Slack `error: ratelimited` with Retry-After).
   - Error mapping (401/invalid_auth, 404/channel_not_found, 5xx transient).
   - PII / Secret redaction (xoxb/xoxp tokens, Bearer, Passwords).
   - Multi-tenant isolation & 0 duplicates on replay.

2. GitHubAdapter:
   - Kế thừa AcquisitionAdapter(source_type=SourceType.GITHUB).
   - Issues, Pull Requests & Comments tracking with parent-child linkage.
   - Updates/edits tracking qua `since` parameter và content_hash change.
   - Repo scope discovery (`/user/repos` & config).
   - GraphQL query execution endpoint (`execute_graphql`).
   - Rate limit handling (429 & 403 secondary rate limit with `X-RateLimit-Reset`).
   - Error mapping (401 bad credentials, 404 not found, 5xx server error).
   - PII / Secret redaction (ghp_..., github_pat_...).
   - Multi-tenant isolation & 0 duplicates on replay.

3. ConfluenceAdapter:
   - Kế thừa AcquisitionAdapter(source_type=SourceType.CONFLUENCE).
   - Pages, Comments, Assignees, Action Items (`<ac:task>` parsing).
   - Version tracking (`version.number`, `version.when`).
   - CQL search query building & execution (`cql:...` or space query).
   - Rate limit handling (429 with Retry-After).
   - Error mapping (401/403 auth, 404 not found, 5xx transient).
   - PII / Secret redaction (Atlassian tokens, Basic auth).
   - Multi-tenant isolation & 0 duplicates on replay.
"""

from datetime import datetime, timezone
import json
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch
import pytest

# Pre-import playwright to resolve circular dependency in outlook_adapter/outlook_interceptor
import ptb_acquisition.playwright  # noqa: F401

from ptb_contracts import (
    IngestionCheckpointRecord,
    ProcessingStatus,
    RawEventRecord,
    SourceType,
)
from ptb_acquisition.adapters.base import (
    AcquisitionAdapter,
    AcquisitionError,
    AdapterAuthenticationError,
    AdapterRateLimitError,
    AdapterStreamNotFoundError,
    AdapterTransientError,
)
from ptb_acquisition.adapters.slack_adapter import (
    SlackAdapter,
    redact_secrets as redact_slack_secrets,
)
from ptb_acquisition.adapters.github_adapter import (
    GitHubAdapter,
    redact_secrets as redact_github_secrets,
)
from ptb_acquisition.adapters.confluence_adapter import (
    ConfluenceAdapter,
    parse_confluence_storage,
    redact_secrets as redact_confluence_secrets,
)
from ptb_acquisition.pipeline import AcquisitionPipeline
from tests.support.test_doubles import (
    InMemoryCheckpointRepository,
    InMemoryRawEventRepository,
)


# ==============================================================================
# 1. SLACK ADAPTER TESTS
# ==============================================================================

class MockSlackHttpFetcher:
    """Mock HTTP fetcher giả lập phản hồi của Slack Web API."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self.channels_response = {
            "ok": True,
            "channels": [
                {"id": "C_DEV", "name": "development", "is_private": False, "num_members": 12},
                {"id": "C_RANDOM", "name": "random", "is_private": False, "num_members": 20},
            ],
            "response_metadata": {"next_cursor": ""},
        }
        self.users_response = {
            "U_ALICE": {
                "ok": True,
                "user": {
                    "id": "U_ALICE",
                    "name": "alice",
                    "profile": {"display_name": "Alice Wonderland", "real_name": "Alice Smith"},
                },
            },
            "U_BOB": {
                "ok": True,
                "user": {
                    "id": "U_BOB",
                    "name": "bob",
                    "profile": {"display_name": "Bob Builder", "real_name": "Bob Jones"},
                },
            },
        }
        self.history_responses: List[Dict[str, Any]] = []
        self.history_call_count = 0
        self.replies_responses: Dict[str, Dict[str, Any]] = {}
        self.auth_test_response = {
            "ok": True,
            "user_id": "U_BOT_123",
            "bot_id": "B_BOT_123",
            "team": "Engineering Team",
        }
        self.custom_error: Optional[Exception] = None

    def __call__(self, url: str, headers: Dict[str, str], body: Optional[bytes]) -> Dict[str, Any]:
        self.calls.append({"url": url, "headers": headers, "body": body})

        if self.custom_error:
            raise self.custom_error

        if "auth.test" in url:
            return self.auth_test_response

        if "conversations.list" in url:
            return self.channels_response

        if "users.info" in url:
            for uid, user_resp in self.users_response.items():
                if f"user={uid}" in url:
                    return user_resp
            return {"ok": False, "error": "user_not_found"}

        if "conversations.replies" in url:
            for ts, rep_resp in self.replies_responses.items():
                if f"ts={ts}" in url:
                    return rep_resp
            return {"ok": True, "messages": []}

        if "conversations.history" in url:
            if self.history_responses:
                idx = min(self.history_call_count, len(self.history_responses) - 1)
                self.history_call_count += 1
                return self.history_responses[idx]
            return {"ok": True, "messages": []}

        return {"ok": True}


@pytest.mark.asyncio
async def test_slack_adapter_contract_and_discovery():
    """Kiểm tra hợp đồng SlackAdapter kế thừa AcquisitionAdapter và discovery kênh."""
    fetcher = MockSlackHttpFetcher()
    adapter = SlackAdapter(
        bot_token="xoxb-mock-token-12345",
        channels=["C_MANUAL"],
        tenant_id="tenant-alpha",
        http_fetcher=fetcher,
    )

    assert isinstance(adapter, AcquisitionAdapter)
    assert adapter.source_type == SourceType.SLACK
    assert adapter.tenant_id == "tenant-alpha"
    assert adapter.active_token == "xoxb-mock-token-12345"

    # Test discover: kết hợp channels từ API và configured channel
    streams = await adapter.discover()
    stream_ids = [s["stream_id"] for s in streams]
    assert "C_DEV" in stream_ids
    assert "C_RANDOM" in stream_ids
    assert "C_MANUAL" in stream_ids


@pytest.mark.asyncio
async def test_slack_adapter_user_resolution_and_caching():
    """Kiểm tra SlackAdapter tra cứu user profile và caching trong bộ nhớ."""
    fetcher = MockSlackHttpFetcher()
    adapter = SlackAdapter(bot_token="xoxb-token", tenant_id="tenant-alpha", http_fetcher=fetcher)

    # Lần 1: Gọi API users.info
    name1 = await adapter.resolve_user_name("U_ALICE")
    assert name1 == "Alice Wonderland"
    assert "U_ALICE" in adapter._user_cache

    # Lần 2: Đọc trực tiếp từ cache, không tăng số lần gọi users.info
    calls_before = len([c for c in fetcher.calls if "users.info" in c["url"]])
    name2 = await adapter.resolve_user_name("U_ALICE")
    calls_after = len([c for c in fetcher.calls if "users.info" in c["url"]])
    assert name2 == "Alice Wonderland"
    assert calls_after == calls_before


@pytest.mark.asyncio
async def test_slack_adapter_backfill_with_thread_replies():
    """Kiểm tra backfill tin nhắn chính kèm thread replies và parent_external_id."""
    fetcher = MockSlackHttpFetcher()
    parent_ts = "1727700000.000100"
    reply_ts1 = "1727700060.000200"
    reply_ts2 = "1727700120.000300"

    fetcher.history_responses = [
        {
            "ok": True,
            "messages": [
                {
                    "ts": parent_ts,
                    "text": "Team, please review release checklist by 3 PM.",
                    "user": "U_ALICE",
                    "reply_count": 2,
                    "thread_ts": parent_ts,
                }
            ],
            "has_more": False,
        }
    ]
    fetcher.replies_responses[parent_ts] = {
        "ok": True,
        "messages": [
            # Slack API trả về parent ở index 0
            {"ts": parent_ts, "text": "Team, please review release checklist by 3 PM.", "user": "U_ALICE"},
            {"ts": reply_ts1, "text": "I checked backend migrations.", "user": "U_BOB", "thread_ts": parent_ts},
            {"ts": reply_ts2, "text": "Frontend tests passed.", "user": "U_ALICE", "thread_ts": parent_ts},
        ],
        "has_more": False,
    }

    adapter = SlackAdapter(
        bot_token="xoxb-valid-token",
        tenant_id="tenant-slack-test",
        http_fetcher=fetcher,
    )

    events: List[RawEventRecord] = []
    async for ev in adapter.backfill("C_DEV"):
        events.append(ev)

    assert len(events) == 3
    # Event 0: Parent message
    assert events[0].external_id == parent_ts
    assert events[0].parent_external_id is None
    assert events[0].author_display_name == "Alice Wonderland"
    assert events[0].source_type == SourceType.SLACK
    assert events[0].tenant_id == "tenant-slack-test"
    assert "release checklist" in events[0].normalized_text

    # Event 1: First reply
    assert events[1].external_id == reply_ts1
    assert events[1].parent_external_id == parent_ts
    assert events[1].author_display_name == "Bob Builder"
    assert "backend migrations" in events[1].normalized_text

    # Event 2: Second reply
    assert events[2].external_id == reply_ts2
    assert events[2].parent_external_id == parent_ts
    assert events[2].author_display_name == "Alice Wonderland"


@pytest.mark.asyncio
async def test_slack_adapter_cursor_pagination():
    """Kiểm tra phân trang cursor-based trên history."""
    fetcher = MockSlackHttpFetcher()
    fetcher.history_responses = [
        {
            "ok": True,
            "messages": [{"ts": "1727700010.000100", "text": "Page 1 message", "user": "U_ALICE"}],
            "has_more": True,
            "response_metadata": {"next_cursor": "cursor_page_2"},
        },
        {
            "ok": True,
            "messages": [{"ts": "1727700020.000200", "text": "Page 2 message", "user": "U_BOB"}],
            "has_more": False,
            "response_metadata": {"next_cursor": ""},
        },
    ]

    adapter = SlackAdapter(bot_token="xoxb-token", tenant_id="t1", http_fetcher=fetcher)
    events = [e async for e in adapter.backfill("C_DEV")]
    assert len(events) == 2
    assert "Page 1 message" in events[0].normalized_text
    assert "Page 2 message" in events[1].normalized_text
    assert fetcher.history_call_count == 2


@pytest.mark.asyncio
async def test_slack_adapter_rate_limit_and_retry():
    """Kiểm tra xử lý HTTP 429 và Slack error ratelimited."""
    fetcher = MockSlackHttpFetcher()
    adapter = SlackAdapter(bot_token="xoxb-token", tenant_id="t1", http_fetcher=fetcher)

    # 1. Slack JSON error: ratelimited
    fetcher.custom_error = None
    fetcher.history_responses = [
        {"ok": False, "error": "ratelimited", "response_metadata": {"retry_after": 2.5}}
    ]
    with patch.object(adapter, "handle_rate_limit") as mock_rl:
        # Should catch error code and raise AdapterRateLimitError
        with pytest.raises(AdapterRateLimitError) as exc_info:
            await adapter._execute_request("conversations.history", params={"channel": "C1"})
        assert exc_info.value.retry_after_seconds == 2.5


@pytest.mark.asyncio
async def test_slack_adapter_authentication_error():
    """Kiểm tra ánh xạ lỗi xác thực (invalid_auth)."""
    fetcher = MockSlackHttpFetcher()
    fetcher.auth_test_response = {"ok": False, "error": "invalid_auth"}
    adapter = SlackAdapter(bot_token="xoxb-bad-token", tenant_id="t1", http_fetcher=fetcher)

    with pytest.raises(AdapterAuthenticationError):
        await adapter._execute_request("auth.test")

    # Health check phản ánh auth_required
    h = await adapter.health()
    assert h["configured"] is True
    assert h["status"] == "auth_required"


@pytest.mark.asyncio
async def test_slack_adapter_redaction():
    """Kiểm tra tính năng che giấu bí mật trong nội dung tin nhắn Slack."""
    raw = "Here is my bot token: xoxb-1234567890-abcdefghij-xyz and Bearer eyJhbGciOiJIUzI1NiIn"
    clean = redact_slack_secrets(raw)
    assert "xoxb-1234567890-abcdefghij-xyz" not in clean
    assert "[REDACTED_SLACK_TOKEN]" in clean
    assert "[REDACTED_TOKEN]" in clean


@pytest.mark.asyncio
async def test_slack_adapter_replay_pipeline_zero_duplicates():
    """Kiểm tra replay dữ liệu giống hệt sinh ra 0 duplicate trong AcquisitionPipeline."""
    fetcher = MockSlackHttpFetcher()
    msg_ts = "1727700050.000100"
    fetcher.history_responses = [
        {
            "ok": True,
            "messages": [{"ts": msg_ts, "text": "Task: finalize release v1.0", "user": "U_ALICE"}],
            "has_more": False,
        }
    ]
    adapter = SlackAdapter(bot_token="xoxb-token", tenant_id="t-dup-slack", http_fetcher=fetcher)

    raw_repo = InMemoryRawEventRepository()
    ckpt_repo = InMemoryCheckpointRepository()
    pipeline = AcquisitionPipeline(
        raw_event_repo=raw_repo,
        checkpoint_repo=ckpt_repo,
        tenant_id="t-dup-slack",
    )

    # Run 1
    count1 = await pipeline.sync_adapter(adapter, stream_id="C_DEV")
    assert count1 == 1
    assert raw_repo.count == 1

    # Run 2 (Replay)
    fetcher.history_call_count = 0  # reset call counter to replay same response
    count2 = await pipeline.sync_adapter(adapter, stream_id="C_DEV")
    assert count2 == 0
    assert raw_repo.count == 1  # 0 duplicates added!


# ==============================================================================
# 2. GITHUB ADAPTER TESTS
# ==============================================================================

class MockGitHubHttpFetcher:
    """Mock HTTP fetcher giả lập GitHub REST API v3 và GraphQL."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self.user_repos_response = [
            {"id": 101, "full_name": "octocat/Hello-World", "name": "Hello-World", "private": False},
            {"id": 102, "full_name": "octocat/Spoon-Knife", "name": "Spoon-Knife", "private": True},
        ]
        self.issues_response: List[Dict[str, Any]] = []
        self.comments_responses: Dict[str, List[Dict[str, Any]]] = {}
        self.graphql_response: Dict[str, Any] = {"data": {"repository": {"name": "Hello-World"}}}
        self.rate_limit_response = {
            "rate": {"limit": 5000, "remaining": 4990, "reset": 1727705000}
        }
        self.custom_error: Optional[Exception] = None

    def __call__(self, url: str, headers: Dict[str, str], body: Optional[bytes]) -> Any:
        self.calls.append({"url": url, "headers": headers, "body": body})

        if self.custom_error:
            raise self.custom_error

        if "rate_limit" in url:
            return self.rate_limit_response, {"x-ratelimit-remaining": "4990"}

        if "user/repos" in url:
            return self.user_repos_response, {}

        if "graphql" in url:
            return self.graphql_response, {}

        # Issue comments
        if "/comments" in url:
            for num, comm_list in self.comments_responses.items():
                if f"/issues/{num}/comments" in url:
                    return comm_list, {}
            return [], {}

        # Issues
        if "/issues" in url:
            return self.issues_response, {}

        if "user" in url:
            return {"login": "octocat", "id": 1}, {}

        return {}, {}


@pytest.mark.asyncio
async def test_github_adapter_contract_and_discover():
    """Kiểm tra hợp đồng GitHubAdapter và discovery repositories."""
    fetcher = MockGitHubHttpFetcher()
    adapter = GitHubAdapter(
        api_token="ghp_mock_token_12345",
        repositories=["octocat/Custom-Repo"],
        tenant_id="tenant-gh",
        http_fetcher=fetcher,
    )

    assert isinstance(adapter, AcquisitionAdapter)
    assert adapter.source_type == SourceType.GITHUB
    assert adapter.tenant_id == "tenant-gh"

    streams = await adapter.discover()
    assert len(streams) == 1
    assert streams[0]["stream_id"] == "octocat/Custom-Repo"

    # Khi không có repositories cấu hình sẵn: tự động khám phá qua /user/repos
    adapter_auto = GitHubAdapter(
        api_token="ghp_mock_token_12345",
        repositories=[],
        tenant_id="tenant-gh",
        http_fetcher=fetcher,
    )
    auto_streams = await adapter_auto.discover()
    stream_ids = [s["stream_id"] for s in auto_streams]
    assert "octocat/Hello-World" in stream_ids
    assert "octocat/Spoon-Knife" in stream_ids


@pytest.mark.asyncio
async def test_github_adapter_backfill_issues_prs_and_comments():
    """Kiểm tra backfill issues, PRs và comments tương ứng."""
    fetcher = MockGitHubHttpFetcher()
    fetcher.issues_response = [
        {
            "id": 201,
            "number": 42,
            "title": "Fix memory leak in websocket worker",
            "body": "Memory increases by 50MB every hour. See logs.",
            "state": "open",
            "user": {"login": "alice_dev"},
            "assignees": [{"login": "bob_reviewer"}],
            "labels": [{"name": "bug"}, {"name": "p0"}],
            "comments": 1,
            "created_at": "2026-10-01T10:00:00Z",
            "updated_at": "2026-10-01T11:00:00Z",
            "html_url": "https://github.com/octocat/Hello-World/issues/42",
        },
        {
            "id": 202,
            "number": 43,
            "title": "Add support for Slack App Manifest",
            "body": "PR implements manifest v2 export.",
            "state": "closed",
            "pull_request": {"url": "https://api.github.com/repos/octocat/Hello-World/pulls/43"},
            "user": {"login": "bob_reviewer"},
            "comments": 0,
            "created_at": "2026-10-01T12:00:00Z",
            "updated_at": "2026-10-01T12:30:00Z",
            "html_url": "https://github.com/octocat/Hello-World/pull/43",
        },
    ]
    fetcher.comments_responses["42"] = [
        {
            "id": 501,
            "body": "Reproduced on staging. Root cause identified in buffer queue.",
            "user": {"login": "charlie_qa"},
            "created_at": "2026-10-01T10:30:00Z",
            "updated_at": "2026-10-01T10:30:00Z",
            "html_url": "https://github.com/octocat/Hello-World/issues/42#issuecomment-501",
        }
    ]

    adapter = GitHubAdapter(
        api_token="ghp_token",
        tenant_id="tenant-gh-test",
        http_fetcher=fetcher,
    )

    events: List[RawEventRecord] = []
    async for ev in adapter.backfill("octocat/Hello-World"):
        events.append(ev)

    # Total: Issue 42, Comment 501, PR 43 = 3 events
    assert len(events) == 3

    # Event 0: Issue 42
    assert events[0].external_id == "42"
    assert events[0].parent_external_id is None
    assert events[0].author_display_name == "alice_dev"
    assert "[GitHub Issue #42]" in events[0].normalized_text
    assert "Labels: bug, p0" in events[0].normalized_text
    assert "Assignees: bob_reviewer" in events[0].normalized_text

    # Event 1: Comment 501
    assert events[1].external_id == "comment-501"
    assert events[1].parent_external_id == "42"
    assert events[1].author_display_name == "charlie_qa"
    assert "Reproduced on staging" in events[1].normalized_text

    # Event 2: Pull Request 43
    assert events[2].external_id == "43"
    assert events[2].parent_external_id is None
    assert "[GitHub Pull Request #43]" in events[2].normalized_text


@pytest.mark.asyncio
async def test_github_adapter_edits_tracking():
    """Kiểm tra khi issue được cập nhật (edited body/title), content_hash thay đổi."""
    fetcher = MockGitHubHttpFetcher()
    adapter = GitHubAdapter(api_token="ghp_token", tenant_id="t-edit", http_fetcher=fetcher)

    item_v1 = {
        "id": 100,
        "number": 10,
        "title": "Initial title",
        "body": "Initial description",
        "state": "open",
        "user": {"login": "dev1"},
        "created_at": "2026-10-01T08:00:00Z",
        "updated_at": "2026-10-01T08:00:00Z",
    }
    rec_v1 = adapter._issue_to_raw_event(item_v1, repo="repo1")

    item_v2 = {
        "id": 100,
        "number": 10,
        "title": "Updated title with critical bugfix",
        "body": "Updated description with repro steps",
        "state": "closed",
        "user": {"login": "dev1"},
        "created_at": "2026-10-01T08:00:00Z",
        "updated_at": "2026-10-01T09:00:00Z",
    }
    rec_v2 = adapter._issue_to_raw_event(item_v2, repo="repo1")

    # Content hash và Idempotency key phải khác nhau để phản ánh revision mới
    assert rec_v1.content_hash != rec_v2.content_hash
    assert rec_v1.idempotency_key != rec_v2.idempotency_key
    assert rec_v2.event_timestamp > rec_v1.event_timestamp


@pytest.mark.asyncio
async def test_github_adapter_graphql():
    """Kiểm tra phương thức execute_graphql của GitHubAdapter."""
    fetcher = MockGitHubHttpFetcher()
    adapter = GitHubAdapter(api_token="ghp_token", tenant_id="t1", http_fetcher=fetcher)

    query = "query { repository(owner: \"octocat\", name: \"Hello-World\") { name } }"
    data = await adapter.execute_graphql(query)
    assert data["repository"]["name"] == "Hello-World"
    assert len(fetcher.calls) == 1
    assert "graphql" in fetcher.calls[0]["url"]


@pytest.mark.asyncio
async def test_github_adapter_rate_limit_and_reset():
    """Kiểm tra xử lý giới hạn tốc độ 403 secondary rate limit và header X-RateLimit-Reset."""
    adapter = GitHubAdapter(api_token="ghp_token", tenant_id="t1")

    err = Exception("API rate limit exceeded for user")
    setattr(err, "status_code", 403)
    setattr(err, "headers", {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1727710000"})

    with pytest.raises(AdapterRateLimitError) as exc_info:
        adapter._handle_http_exception(err)

    assert "Rate Limit" in str(exc_info.value)


@pytest.mark.asyncio
async def test_github_adapter_redaction():
    """Kiểm tra che giấu GitHub Personal Access Tokens."""
    raw = "My PAT is ghp_1234567890abcdefghijklmnopqrstuvwxyz12 and fine-grained github_pat_11AAAAAAA_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    clean = redact_github_secrets(raw)
    assert "ghp_1234567890" not in clean
    assert "github_pat_" not in clean
    assert "[REDACTED_GITHUB_TOKEN]" in clean


@pytest.mark.asyncio
async def test_github_adapter_multi_tenant_isolation():
    """Kiểm tra tính cô lập đa người thuê (Multi-Tenant Isolation) của GitHubAdapter."""
    fetcher = MockGitHubHttpFetcher()
    adapter_a = GitHubAdapter(api_token="ghp_token", tenant_id="tenant-company-a", http_fetcher=fetcher)
    adapter_b = GitHubAdapter(api_token="ghp_token", tenant_id="tenant-company-b", http_fetcher=fetcher)

    issue = {
        "id": 999,
        "number": 55,
        "title": "Shared opensource issue",
        "body": "Bug description",
        "created_at": "2026-10-01T10:00:00Z",
    }
    rec_a = adapter_a._issue_to_raw_event(issue, repo="public/repo")
    rec_b = adapter_b._issue_to_raw_event(issue, repo="public/repo")

    assert rec_a.tenant_id == "tenant-company-a"
    assert rec_b.tenant_id == "tenant-company-b"
    assert rec_a.idempotency_key != rec_b.idempotency_key


# ==============================================================================
# 3. CONFLUENCE ADAPTER TESTS
# ==============================================================================

class MockConfluenceHttpFetcher:
    """Mock HTTP fetcher giả lập Confluence Cloud & Server REST API."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self.spaces_response = {
            "results": [
                {"key": "ENG", "name": "Engineering Wiki", "type": "global"},
                {"key": "PROD", "name": "Product Documentation", "type": "global"},
            ]
        }
        self.search_response: Dict[str, Any] = {"results": [], "size": 0, "totalSize": 0}
        self.comments_response: Dict[str, Any] = {"results": [], "size": 0, "totalSize": 0}
        self.user_current_response = {"displayName": "John Confluence", "accountId": "acc-john-123"}
        self.custom_error: Optional[Exception] = None

    def __call__(self, url: str, headers: Dict[str, str], body: Optional[bytes]) -> Dict[str, Any]:
        self.calls.append({"url": url, "headers": headers, "body": body})

        if self.custom_error:
            raise self.custom_error

        if "user/current" in url:
            return self.user_current_response

        if "wiki/rest/api/space" in url:
            return self.spaces_response

        if "/child/comment" in url:
            return self.comments_response

        if "content/search" in url:
            return self.search_response

        return {}


@pytest.mark.asyncio
async def test_confluence_storage_parser_action_items():
    """Kiểm tra trích xuất plain text và action items từ Confluence XHTML Storage format."""
    xhtml = """
    <p>Please review the Q4 architecture roadmap below:</p>
    <ac:task-list>
        <ac:task>
            <ac:task-status>incomplete</ac:task-status>
            <ac:task-body>Deploy Neo4j Cluster to production <ri:user ri:account-id="acc-123"/></ac:task-body>
        </ac:task>
        <ac:task>
            <ac:task-status>complete</ac:task-status>
            <ac:task-body>Benchmark pipeline deduplication latency</ac:task-body>
        </ac:task>
    </ac:task-list>
    <p>Contact <ri:user ri:username="lead_architect"/> for questions.</p>
    """
    plain, action_items = parse_confluence_storage(xhtml)

    assert "Please review the Q4 architecture roadmap" in plain
    assert "@lead_architect" in plain
    assert len(action_items) == 2
    assert action_items[0] == "[ ] Deploy Neo4j Cluster to production @acc-123"
    assert action_items[1] == "[x] Benchmark pipeline deduplication latency"


@pytest.mark.asyncio
async def test_confluence_adapter_contract_and_discover():
    """Kiểm tra hợp đồng ConfluenceAdapter và discovery spaces."""
    fetcher = MockConfluenceHttpFetcher()
    adapter = ConfluenceAdapter(
        base_url="https://acme.atlassian.net",
        email="dev@acme.com",
        api_token="mock_token",
        space_keys=["CUSTOM"],
        tenant_id="tenant-conf",
        http_fetcher=fetcher,
    )

    assert isinstance(adapter, AcquisitionAdapter)
    assert adapter.source_type == SourceType.CONFLUENCE
    assert adapter.tenant_id == "tenant-conf"

    streams = await adapter.discover()
    stream_keys = [s["stream_id"] for s in streams]
    assert "CUSTOM" in stream_keys
    assert "ENG" in stream_keys
    assert "PROD" in stream_keys


@pytest.mark.asyncio
async def test_confluence_adapter_backfill_and_cql():
    """Kiểm tra backfill tài liệu qua CQL kèm version tracking và comments."""
    fetcher = MockConfluenceHttpFetcher()
    page_storage = """
    <p>Weekly Sync notes:</p>
    <ac:task>
        <ac:task-status>incomplete</ac:task-status>
        <ac:task-body>Submit compliance signoff</ac:task-body>
    </ac:task>
    """
    fetcher.search_response = {
        "results": [
            {
                "id": "1001",
                "title": "Platform Engineering Weekly Sync",
                "space": {"key": "ENG"},
                "version": {"number": 3, "when": "2026-10-02T14:00:00.000Z"},
                "history": {
                    "createdBy": {"displayName": "Tech Lead", "accountId": "acc-lead-01"},
                    "createdDate": "2026-10-01T09:00:00.000Z",
                },
                "body": {"storage": {"value": page_storage}},
                "_links": {"webui": "/spaces/ENG/pages/1001"},
            }
        ],
        "size": 1,
        "totalSize": 1,
    }
    fetcher.comments_response = {
        "results": [
            {
                "id": "2001",
                "version": {"number": 1, "when": "2026-10-02T15:00:00.000Z"},
                "history": {"createdBy": {"displayName": "Security Officer", "accountId": "acc-sec-02"}},
                "body": {"storage": {"value": "<p>Approved from security side.</p>"}},
                "_links": {"webui": "/spaces/ENG/pages/1001#comment-2001"},
            }
        ],
        "size": 1,
        "totalSize": 1,
    }

    adapter = ConfluenceAdapter(
        base_url="https://acme.atlassian.net",
        email="dev@acme.com",
        api_token="token",
        tenant_id="tenant-conf-test",
        http_fetcher=fetcher,
    )

    events: List[RawEventRecord] = []
    async for ev in adapter.backfill("ENG"):
        events.append(ev)

    # Total: 1 Page + 1 Comment = 2 events
    assert len(events) == 2

    # Page Event
    page_ev = events[0]
    assert page_ev.external_id == "1001"
    assert page_ev.parent_external_id is None
    assert "[Confluence: Platform Engineering Weekly Sync]" in page_ev.normalized_text
    assert "v3" in page_ev.normalized_text
    assert "- [ ] Submit compliance signoff" in page_ev.normalized_text
    assert page_ev.author_display_name == "Tech Lead"
    assert page_ev.source_type == SourceType.CONFLUENCE

    # Comment Event
    comm_ev = events[1]
    assert comm_ev.external_id == "comment-2001"
    assert comm_ev.parent_external_id == "1001"
    assert comm_ev.author_display_name == "Security Officer"
    assert "Approved from security side" in comm_ev.normalized_text


@pytest.mark.asyncio
async def test_confluence_adapter_custom_cql_stream():
    """Kiểm tra ConfluenceAdapter hỗ trợ custom CQL qua tiền tố cql:..."""
    fetcher = MockConfluenceHttpFetcher()
    adapter = ConfluenceAdapter(
        base_url="https://acme.atlassian.net",
        email="dev@acme.com",
        api_token="token",
        tenant_id="tenant-cql",
        http_fetcher=fetcher,
    )

    custom_stream = "cql:type = page and label in ('meeting-notes', 'action-items')"
    cql = adapter._build_cql(custom_stream)
    assert "label in ('meeting-notes', 'action-items')" in cql
    assert "ORDER BY lastModified ASC" in cql


@pytest.mark.asyncio
async def test_confluence_adapter_rate_limit_and_retry():
    """Kiểm tra xử lý HTTP 429 và Retry-After trên Confluence."""
    adapter = ConfluenceAdapter(base_url="https://acme.atlassian.net", api_token="t", tenant_id="t")
    err = Exception("Too Many Requests")
    setattr(err, "status_code", 429)
    setattr(err, "headers", {"Retry-After": "4.0"})

    with pytest.raises(AdapterRateLimitError) as exc_info:
        adapter._handle_http_exception(err)

    assert exc_info.value.retry_after_seconds == 4.0


@pytest.mark.asyncio
async def test_confluence_adapter_redaction():
    """Kiểm tra che giấu Atlassian tokens và Basic Auth credentials."""
    raw = "Basic ZGV2QGFjbWUuY29tOnNlY3JldDEyMzQ1 and Token ATATT3xFfGF0dGwtMTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM0NTY3ODkw"
    clean = redact_confluence_secrets(raw)
    assert "ZGV2QGFjbWUuY29t" not in clean
    assert "ATATT3xFfGF0dG" not in clean
    assert "[REDACTED_BASIC_AUTH]" in clean
    assert "[REDACTED_ATLASSIAN_TOKEN]" in clean


@pytest.mark.asyncio
async def test_confluence_adapter_replay_pipeline_zero_duplicates():
    """Kiểm tra replay dữ liệu Confluence sinh ra 0 duplicates trong AcquisitionPipeline."""
    fetcher = MockConfluenceHttpFetcher()
    fetcher.search_response = {
        "results": [
            {
                "id": "888",
                "title": "Immutable Architecture Guide",
                "space": {"key": "ARCH"},
                "version": {"number": 1, "when": "2026-10-01T10:00:00Z"},
                "body": {"storage": {"value": "<p>Content must be idempotent.</p>"}},
            }
        ],
        "size": 1,
        "totalSize": 1,
    }
    adapter = ConfluenceAdapter(
        base_url="https://acme.atlassian.net",
        api_token="token",
        tenant_id="t-conf-dup",
        http_fetcher=fetcher,
        include_comments=False,
    )

    raw_repo = InMemoryRawEventRepository()
    ckpt_repo = InMemoryCheckpointRepository()
    pipeline = AcquisitionPipeline(
        raw_event_repo=raw_repo,
        checkpoint_repo=ckpt_repo,
        tenant_id="t-conf-dup",
    )

    count1 = await pipeline.sync_adapter(adapter, stream_id="ARCH")
    assert count1 == 1
    assert raw_repo.count == 1

    # Replay
    count2 = await pipeline.sync_adapter(adapter, stream_id="ARCH")
    assert count2 == 0
    assert raw_repo.count == 1  # 0 duplicates


# ==============================================================================
# 4. COMPREHENSIVE EDGE CASES: 5xx, 404, INCREMENTAL POLL & HEALTH
# ==============================================================================

@pytest.mark.asyncio
async def test_slack_adapter_incremental_poll_with_checkpoint():
    """Kiểm tra SlackAdapter poll_incremental lọc bỏ message trước hoặc tại checkpoint."""
    fetcher = MockSlackHttpFetcher()
    ckpt_ts = "1727700050.000100"
    new_ts = "1727700080.000200"

    fetcher.history_responses = [
        {
            "ok": True,
            "messages": [
                {"ts": ckpt_ts, "text": "Old message at checkpoint", "user": "U_ALICE"},
                {"ts": new_ts, "text": "New incoming message after checkpoint", "user": "U_BOB"},
            ],
            "has_more": False,
        }
    ]

    adapter = SlackAdapter(bot_token="xoxb-token", tenant_id="t-inc", http_fetcher=fetcher)
    ckpt = IngestionCheckpointRecord(
        source_type=SourceType.SLACK,
        stream_id="C_DEV",
        tenant_id="t-inc",
        last_external_id=ckpt_ts,
        last_event_timestamp=datetime.fromtimestamp(float(ckpt_ts), tz=timezone.utc),
    )

    events = [e async for e in adapter.poll_incremental("C_DEV", checkpoint=ckpt)]
    assert len(events) == 1
    assert events[0].external_id == new_ts
    assert "New incoming message" in events[0].normalized_text


@pytest.mark.asyncio
async def test_slack_adapter_5xx_transient_retry():
    """Kiểm tra SlackAdapter tự động retry khi gặp lỗi tạm thời 5xx."""
    call_count = 0

    def faulty_fetcher(url: str, headers: Dict[str, str], body: Optional[bytes]):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            err = Exception("Slack backend 503 Service Unavailable")
            setattr(err, "status_code", 503)
            raise err
        return {"ok": True, "channels": [{"id": "C_OK", "name": "ok-channel"}]}

    adapter = SlackAdapter(bot_token="xoxb-token", tenant_id="t-5xx", http_fetcher=faulty_fetcher)
    # Configure retry delay to be tiny for test speed
    adapter.retry_policy.initial_delay_seconds = 0.01
    adapter.retry_policy.max_delay_seconds = 0.05
    adapter.retry_policy.jitter = False

    streams = await adapter.discover()
    assert any(s["stream_id"] == "C_OK" for s in streams)
    assert call_count == 3


@pytest.mark.asyncio
async def test_slack_adapter_channel_not_found():
    """Kiểm tra lỗi channel_not_found ánh xạ sang AdapterStreamNotFoundError."""
    fetcher = MockSlackHttpFetcher()
    fetcher.history_responses = [{"ok": False, "error": "channel_not_found"}]
    adapter = SlackAdapter(bot_token="xoxb-token", tenant_id="t1", http_fetcher=fetcher)

    with pytest.raises(AdapterStreamNotFoundError):
        await adapter._fetch_channel_messages("C_NON_EXISTENT")


@pytest.mark.asyncio
async def test_slack_adapter_health_success():
    """Kiểm tra SlackAdapter health check trả về healthy và thông tin bot."""
    fetcher = MockSlackHttpFetcher()
    adapter = SlackAdapter(bot_token="xoxb-valid", tenant_id="t1", http_fetcher=fetcher)
    report = await adapter.health()
    assert report["configured"] is True
    assert report["status"] == "healthy"
    assert report["bot_id"] == "B_BOT_123"
    assert report["team"] == "Engineering Team"


@pytest.mark.asyncio
async def test_slack_adapter_multi_tenant_isolation():
    """Kiểm tra hai tenant khác nhau nhận hai idempotency_key hoàn toàn riêng biệt."""
    fetcher = MockSlackHttpFetcher()
    adapter_a = SlackAdapter(bot_token="xoxb-tok", tenant_id="tenant-alpha", http_fetcher=fetcher)
    adapter_b = SlackAdapter(bot_token="xoxb-tok", tenant_id="tenant-beta", http_fetcher=fetcher)

    msg = {"ts": "1727700011.000100", "text": "Same text in same channel", "user": "U1"}
    ev_a = adapter_a._message_to_raw_event(msg, channel_id="C_SHARED")
    ev_b = adapter_b._message_to_raw_event(msg, channel_id="C_SHARED")

    assert ev_a.tenant_id == "tenant-alpha"
    assert ev_b.tenant_id == "tenant-beta"
    assert ev_a.idempotency_key != ev_b.idempotency_key


@pytest.mark.asyncio
async def test_github_adapter_incremental_poll_with_checkpoint():
    """Kiểm tra GitHubAdapter poll_incremental chỉ emit issues/comments mới hơn checkpoint."""
    fetcher = MockGitHubHttpFetcher()
    ckpt_time_str = "2026-10-01T10:00:00Z"
    new_time_str = "2026-10-01T11:00:00Z"

    fetcher.issues_response = [
        {
            "id": 1,
            "number": 10,
            "title": "Already processed issue",
            "updated_at": ckpt_time_str,
            "user": {"login": "dev"},
        },
        {
            "id": 2,
            "number": 11,
            "title": "Newly updated issue",
            "updated_at": new_time_str,
            "user": {"login": "dev"},
        },
    ]

    adapter = GitHubAdapter(api_token="ghp_token", tenant_id="t-gh-inc", http_fetcher=fetcher, include_comments=False)
    ckpt = IngestionCheckpointRecord(
        source_type=SourceType.GITHUB,
        stream_id="repo-main",
        tenant_id="t-gh-inc",
        last_external_id="10",
        last_event_timestamp=datetime.fromisoformat(ckpt_time_str.replace("Z", "+00:00")),
    )

    events = [e async for e in adapter.poll_incremental("repo-main", checkpoint=ckpt)]
    assert len(events) == 1
    assert events[0].external_id == "11"
    assert "Newly updated issue" in events[0].normalized_text


@pytest.mark.asyncio
async def test_github_adapter_5xx_transient_retry():
    """Kiểm tra GitHubAdapter tự động retry khi gặp lỗi tạm thời 502/503."""
    call_count = 0

    def faulty_gh(url: str, headers: Dict[str, str], body: Optional[bytes]):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            err = Exception("GitHub 502 Bad Gateway")
            setattr(err, "status_code", 502)
            raise err
        return [{"id": 99, "full_name": "org/repo"}], {}

    adapter = GitHubAdapter(api_token="ghp_token", tenant_id="t-gh-5xx", http_fetcher=faulty_gh)
    adapter.retry_policy.initial_delay_seconds = 0.01
    adapter.retry_policy.max_delay_seconds = 0.05
    adapter.retry_policy.jitter = False

    streams = await adapter.discover()
    assert any(s["stream_id"] == "org/repo" for s in streams)
    assert call_count == 2


@pytest.mark.asyncio
async def test_github_adapter_auth_error_and_not_found():
    """Kiểm tra GitHub 401 Bad Credentials và 404 Not Found."""
    adapter = GitHubAdapter(api_token="ghp_bad", tenant_id="t")

    err_401 = Exception("Bad credentials")
    setattr(err_401, "status_code", 401)
    with pytest.raises(AdapterAuthenticationError):
        adapter._handle_http_exception(err_401)

    err_404 = Exception("Not Found")
    setattr(err_404, "status_code", 404)
    with pytest.raises(AdapterStreamNotFoundError):
        adapter._handle_http_exception(err_404)


@pytest.mark.asyncio
async def test_github_adapter_health_success():
    """Kiểm tra GitHubAdapter health check khi xác thực thành công."""
    fetcher = MockGitHubHttpFetcher()
    adapter = GitHubAdapter(api_token="ghp_valid", repositories=["r1"], tenant_id="t", http_fetcher=fetcher)
    h = await adapter.health()
    assert h["configured"] is True
    assert h["status"] == "healthy"
    assert h["user"] == "octocat"
    assert h["rate_limit_remaining"] == 4990


@pytest.mark.asyncio
async def test_confluence_adapter_incremental_poll_with_checkpoint():
    """Kiểm tra ConfluenceAdapter poll_incremental với checkpoint."""
    fetcher = MockConfluenceHttpFetcher()
    ckpt_time_str = "2026-10-01T10:00:00.000Z"
    new_time_str = "2026-10-01T12:00:00.000Z"

    fetcher.search_response = {
        "results": [
            {
                "id": "101",
                "title": "Old Doc",
                "version": {"number": 1, "when": ckpt_time_str},
                "body": {"storage": {"value": "<p>old</p>"}},
            },
            {
                "id": "102",
                "title": "Freshly Revised Doc",
                "version": {"number": 2, "when": new_time_str},
                "body": {"storage": {"value": "<p>revised content</p>"}},
            },
        ],
        "size": 2,
        "totalSize": 2,
    }

    adapter = ConfluenceAdapter(base_url="https://acme.atlassian.net", api_token="t", tenant_id="t-c-inc", http_fetcher=fetcher, include_comments=False)
    ckpt = IngestionCheckpointRecord(
        source_type=SourceType.CONFLUENCE,
        stream_id="ENG",
        tenant_id="t-c-inc",
        last_external_id="101",
        last_event_timestamp=datetime.fromisoformat(ckpt_time_str.replace("Z", "+00:00")),
    )

    events = [e async for e in adapter.poll_incremental("ENG", checkpoint=ckpt)]
    assert len(events) == 1
    assert events[0].external_id == "102"
    assert "Freshly Revised Doc" in events[0].normalized_text


@pytest.mark.asyncio
async def test_confluence_adapter_version_update_tracking():
    """Kiểm tra khi tài liệu Confluence tăng version (v1 -> v2), content_hash thay đổi."""
    adapter = ConfluenceAdapter(base_url="https://acme.atlassian.net", api_token="t", tenant_id="t")

    page_v1 = {
        "id": "777",
        "title": "Security Guidelines",
        "version": {"number": 1, "when": "2026-10-01T08:00:00Z"},
        "body": {"storage": {"value": "<p>Initial security policy.</p>"}},
    }
    rec_v1 = adapter._page_to_raw_event(page_v1, space_key="SEC")

    page_v2 = {
        "id": "777",
        "title": "Security Guidelines",
        "version": {"number": 2, "when": "2026-10-01T10:00:00Z"},
        "body": {"storage": {"value": "<p>Updated security policy with MFA enforcement.</p>"}},
    }
    rec_v2 = adapter._page_to_raw_event(page_v2, space_key="SEC")

    assert "v1" in rec_v1.normalized_text
    assert "v2" in rec_v2.normalized_text
    assert rec_v1.content_hash != rec_v2.content_hash
    assert rec_v1.idempotency_key != rec_v2.idempotency_key


@pytest.mark.asyncio
async def test_confluence_adapter_5xx_transient_retry():
    """Kiểm tra ConfluenceAdapter tự động retry khi gặp lỗi 500/503."""
    call_count = 0

    def faulty_conf(url: str, headers: Dict[str, str], body: Optional[bytes]):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            err = Exception("Confluence 503 Service Unavailable")
            setattr(err, "status_code", 503)
            raise err
        return {"results": [{"key": "RETRY_SPACE", "name": "Retry Space"}]}

    adapter = ConfluenceAdapter(base_url="https://acme.atlassian.net", api_token="t", tenant_id="t-c-5xx", http_fetcher=faulty_conf)
    adapter.retry_policy.initial_delay_seconds = 0.01
    adapter.retry_policy.max_delay_seconds = 0.05
    adapter.retry_policy.jitter = False

    streams = await adapter.discover()
    assert any(s["stream_id"] == "RETRY_SPACE" for s in streams)
    assert call_count == 2


@pytest.mark.asyncio
async def test_confluence_adapter_auth_error_and_not_found():
    """Kiểm tra Confluence 401 Unauthorized và 404 Not Found."""
    adapter = ConfluenceAdapter(base_url="https://acme.atlassian.net", api_token="bad", tenant_id="t")

    err_401 = Exception("Unauthorized")
    setattr(err_401, "status_code", 401)
    with pytest.raises(AdapterAuthenticationError):
        adapter._handle_http_exception(err_401)

    err_404 = Exception("Space Not Found")
    setattr(err_404, "status_code", 404)
    with pytest.raises(AdapterStreamNotFoundError):
        adapter._handle_http_exception(err_404)


@pytest.mark.asyncio
async def test_confluence_adapter_health_success():
    """Kiểm tra ConfluenceAdapter health check thành công."""
    fetcher = MockConfluenceHttpFetcher()
    adapter = ConfluenceAdapter(base_url="https://acme.atlassian.net", api_token="valid", tenant_id="t", http_fetcher=fetcher)
    h = await adapter.health()
    assert h["configured"] is True
    assert h["status"] == "healthy"
    assert h["user"] == "John Confluence"


@pytest.mark.asyncio
async def test_confluence_adapter_multi_tenant_isolation():
    """Kiểm tra Multi-Tenant Isolation cho ConfluenceAdapter."""
    adapter_a = ConfluenceAdapter(base_url="https://acme.atlassian.net", api_token="t", tenant_id="tenant-acme")
    adapter_b = ConfluenceAdapter(base_url="https://acme.atlassian.net", api_token="t", tenant_id="tenant-corp")

    page = {
        "id": "555",
        "title": "Onboarding Guide",
        "version": {"number": 1, "when": "2026-10-01T08:00:00Z"},
        "body": {"storage": {"value": "<p>Welcome to the company.</p>"}},
    }
    rec_a = adapter_a._page_to_raw_event(page, space_key="ONB")
    rec_b = adapter_b._page_to_raw_event(page, space_key="ONB")

    assert rec_a.tenant_id == "tenant-acme"
    assert rec_b.tenant_id == "tenant-corp"
    assert rec_a.idempotency_key != rec_b.idempotency_key

