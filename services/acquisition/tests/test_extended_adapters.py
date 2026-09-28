"""Unit tests for Extended Source Adapters (Jira, Shortcut, Git).

Kiểm tra:
1. JiraAdapter:
   - Kế thừa AcquisitionAdapter(source_type=SourceType.JIRA).
   - Discover projects qua config hoặc REST API.
   - Backfill & Poll Incremental: ánh xạ Jira issue (cả plain text và Atlassian Document Format) thành RawEventRecord v1.
   - Idempotency key, content_hash, deep_link, parent issue key.
2. ShortcutAdapter:
   - Kế thừa AcquisitionAdapter(source_type=SourceType.SHORTCUT).
   - Discover qua config hoặc REST API /projects.
   - Backfill & Poll Incremental: ánh xạ Shortcut story thành RawEventRecord v1.
   - Idempotency key, content_hash, epic_id, app_url.
3. GitWatcherAdapter:
   - Kế thừa AcquisitionAdapter(source_type=SourceType.GIT).
   - Quét commit history và branch metadata từ local git repository thật.
   - Backfill & Poll Incremental: ánh xạ commit thành RawEventRecord v1.
   - Checkpoint resume theo commit hash.
4. Pipeline Integration:
   - Đăng ký cả 3 adapters vào AcquisitionPipeline và chạy sync_adapter.
"""

from datetime import datetime, timezone
import json
import os
import subprocess
import tempfile
from typing import Any, Dict, List, Optional
import pytest

from ptb_contracts import (
    IngestionCheckpointRecord,
    ProcessingStatus,
    RawEventRecord,
    SourceType,
)
from ptb_acquisition.adapters import (
    GitWatcherAdapter,
    JiraAdapter,
    ShortcutAdapter,
)
from ptb_acquisition.pipeline import (
    AcquisitionPipeline,
    InMemoryCheckpointRepository,
    InMemoryRawEventRepository,
)


# ==============================================================================
# 1. JIRA ADAPTER TESTS
# ==============================================================================

@pytest.fixture
def mock_jira_payload():
    """Tạo payload mẫu giả lập phản hồi từ Jira REST API."""
    return {
        "expand": "names,schema",
        "startAt": 0,
        "maxResults": 50,
        "total": 2,
        "issues": [
            {
                "id": "10001",
                "key": "PROJ-101",
                "fields": {
                    "summary": "Fix critical authentication timeout in OAuth flow",
                    "description": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Token refresh fails when server clock drifts by 5 seconds.",
                                    }
                                ],
                            }
                        ],
                    },
                    "status": {"name": "In Progress"},
                    "assignee": {"displayName": "Nguyen Van A", "name": "nva"},
                    "creator": {
                        "accountId": "acc-12345",
                        "displayName": "Product Owner",
                        "emailAddress": "po@example.com",
                    },
                    "project": {"key": "PROJ", "name": "Core Platform"},
                    "parent": {"key": "PROJ-100"},
                    "created": "2026-09-28T08:00:00.000+0000",
                    "updated": "2026-09-28T10:30:00.000+0000",
                },
            },
            {
                "id": "10002",
                "key": "PROJ-102",
                "fields": {
                    "summary": "Implement Neo4j Single-Store constraints",
                    "description": "Plain text description from Jira Server / API v2.",
                    "status": {"name": "Done"},
                    "assignee": None,
                    "creator": {"name": "lead-dev", "displayName": "Tech Lead"},
                    "project": {"key": "PROJ"},
                    "parent": None,
                    "created": "2026-09-28T09:00:00.000+0000",
                    "updated": "2026-09-28T11:00:00.000+0000",
                },
            },
        ],
    }


@pytest.mark.asyncio
async def test_jira_adapter_contract_and_discover(mock_jira_payload):
    def mock_fetcher(url: str, headers: Dict[str, str], body: Optional[bytes]):
        if "/rest/api/3/project" in url:
            return [
                {"id": "10", "key": "PROJ", "name": "Core Platform"},
                {"id": "20", "key": "FRONT", "name": "Frontend Web"},
            ]
        if "/rest/api/3/myself" in url:
            return {"displayName": "Test Bot", "emailAddress": "bot@example.com"}
        return {}

    adapter = JiraAdapter(
        base_url="https://company.atlassian.net",
        email="dev@company.com",
        api_token="dummy-jira-token",
        project_keys=["PROJ"],
        tenant_id="tenant-jira-test",
        http_fetcher=mock_fetcher,
    )

    assert adapter.source_type == SourceType.JIRA
    assert adapter.tenant_id == "tenant-jira-test"

    # Discover streams
    streams = await adapter.discover()
    stream_ids = [s["stream_id"] for s in streams]
    assert "all" in stream_ids
    assert "PROJ" in stream_ids
    assert "FRONT" in stream_ids

    # Health check
    health = await adapter.health()
    assert health["status"] == "healthy"
    assert health["configured"] is True
    assert health["source_type"] == SourceType.JIRA.value
    assert health["user"] == "Test Bot"


@pytest.mark.asyncio
async def test_jira_adapter_backfill_and_poll(mock_jira_payload):
    def mock_fetcher(url: str, headers: Dict[str, str], body: Optional[bytes]):
        if "/search" in url:
            return mock_jira_payload
        return {}

    adapter = JiraAdapter(
        base_url="https://company.atlassian.net",
        email="dev@company.com",
        api_token="dummy-token",
        tenant_id="tenant-jira-test",
        http_fetcher=mock_fetcher,
    )

    # 1. Backfill
    records: List[RawEventRecord] = []
    async for rec in adapter.backfill(stream_id="PROJ"):
        records.append(rec)

    assert len(records) == 2

    r1 = records[0]
    assert r1.source_type == SourceType.JIRA
    assert r1.external_id == "PROJ-101"
    assert r1.parent_external_id == "PROJ-100"
    assert r1.author_external_id == "acc-12345"
    assert r1.author_display_name == "Product Owner"
    assert r1.conversation_or_project_id == "PROJ"
    assert r1.deep_link == "https://company.atlassian.net/browse/PROJ-101"
    assert "Fix critical authentication timeout" in r1.normalized_text
    assert "Token refresh fails" in r1.normalized_text
    assert r1.content_hash is not None
    assert r1.idempotency_key is not None
    assert r1.processing_status == ProcessingStatus.PENDING

    r2 = records[1]
    assert r2.external_id == "PROJ-102"
    assert r2.parent_external_id is None
    assert "Plain text description" in r2.normalized_text
    assert r2.author_display_name == "Tech Lead"

    # 2. Incremental poll with checkpoint
    checkpoint = IngestionCheckpointRecord(
        id="ckpt-jira-1",
        source_type=SourceType.JIRA,
        stream_id="PROJ",
        last_external_id="PROJ-101",
        last_event_timestamp=r1.event_timestamp,
    )

    incremental_records = []
    async for rec in adapter.poll_incremental(stream_id="PROJ", checkpoint=checkpoint):
        incremental_records.append(rec)

    # PROJ-101 should be skipped because it is at checkpoint timestamp & last_external_id
    assert len(incremental_records) == 1
    assert incremental_records[0].external_id == "PROJ-102"


# ==============================================================================
# 2. SHORTCUT ADAPTER TESTS
# ==============================================================================

@pytest.fixture
def mock_shortcut_payload():
    """Tạo payload mẫu giả lập phản hồi từ Shortcut REST API v3."""
    return {
        "data": [
            {
                "id": 8801,
                "name": "Implement GraphQL Schema for Shortcut Sync",
                "description": "Need to support bidirectional story sync with PTB.",
                "story_type": "feature",
                "app_url": "https://app.shortcut.com/org/story/8801/implement-graphql",
                "project_id": 501,
                "epic_id": 100,
                "workflow_state_id": 50000001,
                "requested_by_id": "user-sc-1",
                "created_at": "2026-09-28T07:00:00Z",
                "updated_at": "2026-09-28T09:30:00Z",
            },
            {
                "id": 8802,
                "name": "Fix crash on invalid date format",
                "description": "ISO timestamp parser raises exception on missing timezone.",
                "story_type": "bug",
                "app_url": "https://app.shortcut.com/org/story/8802/fix-crash",
                "project_id": 501,
                "epic_id": None,
                "workflow_state_id": 50000002,
                "requested_by_id": "user-sc-2",
                "created_at": "2026-09-28T08:00:00Z",
                "updated_at": "2026-09-28T11:00:00Z",
            },
        ],
        "next": None,
    }


@pytest.mark.asyncio
async def test_shortcut_adapter_contract_and_discover(mock_shortcut_payload):
    def mock_fetcher(url: str, headers: Dict[str, str], body: Optional[bytes]):
        if "/projects" in url:
            return [
                {"id": 501, "name": "Backend Platform", "app_url": "https://app.shortcut.com/org/project/501"}
            ]
        if "/member" in url:
            return {"id": "member-99", "profile": {"name": "Engineer Shortcut"}}
        return {}

    adapter = ShortcutAdapter(
        api_token="shortcut-token-xyz",
        project_ids=[501],
        tenant_id="tenant-sc-test",
        http_fetcher=mock_fetcher,
    )

    assert adapter.source_type == SourceType.SHORTCUT
    assert adapter.tenant_id == "tenant-sc-test"

    # Discover
    streams = await adapter.discover()
    stream_ids = [s["stream_id"] for s in streams]
    assert "all" in stream_ids
    assert "501" in stream_ids

    # Health
    health = await adapter.health()
    assert health["status"] == "healthy"
    assert health["configured"] is True
    assert health["source_type"] == SourceType.SHORTCUT.value
    assert health["user"] == "Engineer Shortcut"


@pytest.mark.asyncio
async def test_shortcut_adapter_backfill_and_poll(mock_shortcut_payload):
    def mock_fetcher(url: str, headers: Dict[str, str], body: Optional[bytes]):
        if "/stories/search" in url:
            return mock_shortcut_payload
        return {}

    adapter = ShortcutAdapter(
        api_token="shortcut-token-xyz",
        tenant_id="tenant-sc-test",
        http_fetcher=mock_fetcher,
    )

    # 1. Backfill
    records: List[RawEventRecord] = []
    async for rec in adapter.backfill(stream_id="501"):
        records.append(rec)

    assert len(records) == 2

    s1 = records[0]
    assert s1.source_type == SourceType.SHORTCUT
    assert s1.external_id == "8801"
    assert s1.parent_external_id == "100"
    assert s1.author_external_id == "user-sc-1"
    assert s1.conversation_or_project_id == "501"
    assert s1.deep_link == "https://app.shortcut.com/org/story/8801/implement-graphql"
    assert "Implement GraphQL Schema" in s1.normalized_text
    assert s1.content_hash is not None
    assert s1.idempotency_key is not None

    s2 = records[1]
    assert s2.external_id == "8802"
    assert s2.parent_external_id is None
    assert "(bug)" in s2.normalized_text

    # 2. Incremental poll
    checkpoint = IngestionCheckpointRecord(
        id="ckpt-sc-1",
        source_type=SourceType.SHORTCUT,
        stream_id="501",
        last_external_id="8801",
        last_event_timestamp=s1.event_timestamp,
    )

    incremental = []
    async for rec in adapter.poll_incremental(stream_id="501", checkpoint=checkpoint):
        incremental.append(rec)

    assert len(incremental) == 1
    assert incremental[0].external_id == "8802"


# ==============================================================================
# 3. GIT WATCHER ADAPTER TESTS
# ==============================================================================

@pytest.fixture
def temp_git_repo():
    """Tạo một local git repository thật trong temp directory với 2 commits."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        repo_dir = os.path.join(tmp_dir, "test_repo")
        os.makedirs(repo_dir)

        # git init
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test Committer"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "committer@test.local"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "remote.origin.url", "https://github.com/org/test_repo.git"], cwd=repo_dir, check=True)

        # Commit 1
        file1 = os.path.join(repo_dir, "file1.txt")
        with open(file1, "w", encoding="utf-8") as f:
            f.write("Initial commit content\n")
        subprocess.run(["git", "add", "file1.txt"], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit: setup project structure"], cwd=repo_dir, check=True)

        # Commit 2
        file2 = os.path.join(repo_dir, "adapter.py")
        with open(file2, "w", encoding="utf-8") as f:
            f.write("class GitWatcherAdapter: pass\n")
        subprocess.run(["git", "add", "adapter.py"], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "Add GitWatcherAdapter implementation\n\nFull details about adapter."], cwd=repo_dir, check=True)

        yield repo_dir


@pytest.mark.asyncio
async def test_git_watcher_adapter_lifecycle(temp_git_repo):
    adapter = GitWatcherAdapter(
        repo_paths=[temp_git_repo],
        tenant_id="tenant-git-test",
    )

    assert adapter.source_type == SourceType.GIT
    assert adapter.tenant_id == "tenant-git-test"

    # Discover
    streams = await adapter.discover()
    stream_ids = [s["stream_id"] for s in streams]
    assert "all" in stream_ids
    assert "test_repo" in stream_ids

    # Health
    health = await adapter.health()
    assert health["status"] == "healthy"
    assert health["source_type"] == SourceType.GIT.value
    assert len(health["valid_git_repos"]) == 1

    # Backfill
    records: List[RawEventRecord] = []
    async for rec in adapter.backfill(stream_id="test_repo"):
        records.append(rec)

    assert len(records) == 2
    c1, c2 = records[0], records[1]

    # Commit 1 verification
    assert c1.source_type == SourceType.GIT
    assert len(c1.external_id) == 40  # 40-char commit SHA
    assert c1.author_display_name == "Test Committer"
    assert c1.author_external_id == "committer@test.local"
    assert c1.conversation_or_project_id == "test_repo"
    assert "Initial commit: setup project structure" in c1.normalized_text
    assert "file1.txt" in c1.normalized_text
    assert f"https://github.com/org/test_repo/commit/{c1.external_id}" == c1.deep_link

    # Commit 2 verification
    assert c2.source_type == SourceType.GIT
    assert c2.parent_external_id == c1.external_id
    assert "Add GitWatcherAdapter implementation" in c2.normalized_text
    assert "adapter.py" in c2.normalized_text

    # Incremental poll using c1 commit hash as checkpoint
    checkpoint = IngestionCheckpointRecord(
        id="ckpt-git-1",
        source_type=SourceType.GIT,
        stream_id="test_repo",
        last_external_id=c1.external_id,
        last_event_timestamp=c1.event_timestamp,
    )

    incremental_records = []
    async for rec in adapter.poll_incremental(stream_id="test_repo", checkpoint=checkpoint):
        incremental_records.append(rec)

    assert len(incremental_records) == 1
    assert incremental_records[0].external_id == c2.external_id


# ==============================================================================
# 4. PIPELINE MULTI-SOURCE INTEGRATION TEST
# ==============================================================================

@pytest.mark.asyncio
async def test_acquisition_pipeline_with_all_new_adapters(
    mock_jira_payload, mock_shortcut_payload, temp_git_repo
):
    raw_repo = InMemoryRawEventRepository()
    ckpt_repo = InMemoryCheckpointRepository()
    pipeline = AcquisitionPipeline(
        raw_event_repo=raw_repo,
        checkpoint_repo=ckpt_repo,
        tenant_id="e2e-user",
    )

    # 1. Register Jira
    jira_adapter = JiraAdapter(
        base_url="https://company.atlassian.net",
        email="dev@company.com",
        api_token="token",
        http_fetcher=lambda url, h, b: mock_jira_payload,
        tenant_id="e2e-user",
    )
    pipeline.register_adapter("jira", jira_adapter)

    # 2. Register Shortcut
    shortcut_adapter = ShortcutAdapter(
        api_token="token",
        http_fetcher=lambda url, h, b: mock_shortcut_payload,
        tenant_id="e2e-user",
    )
    pipeline.register_adapter("shortcut", shortcut_adapter)

    # 3. Register Git
    git_adapter = GitWatcherAdapter(
        repo_paths=[temp_git_repo],
        tenant_id="e2e-user",
    )
    pipeline.register_adapter("git", git_adapter)

    # Run sync for each
    cnt_jira = await pipeline.sync_adapter(jira_adapter, stream_id="all")
    cnt_sc = await pipeline.sync_adapter(shortcut_adapter, stream_id="all")
    cnt_git = await pipeline.sync_adapter(git_adapter, stream_id="all")

    assert cnt_jira == 2
    assert cnt_sc == 2
    assert cnt_git == 2

    # Verify pipeline stats
    assert pipeline.stats["ingested"] == 6
    assert pipeline.stats["persisted"] == 6
    assert pipeline.stats["errors"] == 0

    # Verify checkponts saved
    ckpt_jira = await ckpt_repo.get_checkpoint(SourceType.JIRA.value, "all")
    ckpt_sc = await ckpt_repo.get_checkpoint(SourceType.SHORTCUT.value, "all")
    ckpt_git = await ckpt_repo.get_checkpoint(SourceType.GIT.value, "all")

    assert ckpt_jira is not None
    assert ckpt_sc is not None
    assert ckpt_git is not None

    assert ckpt_jira.last_external_id == "PROJ-102"
    assert ckpt_sc.last_external_id == "8802"
    assert ckpt_git.last_event_timestamp is not None
