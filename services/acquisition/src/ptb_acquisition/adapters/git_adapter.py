"""Local Git Repository Acquisition Adapter.

Quét commits, branch metadata và file diffs từ các local Git repositories,
chuyển đổi thành RawEventRecord v1 chuẩn hóa với source_type = SourceType.GIT.
"""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, AsyncIterator, Callable, Dict, List, Optional
import uuid

from ptb_contracts import (
    IngestionCheckpointRecord,
    ProcessingStatus,
    RawEventRecord,
    SourceType,
)
from ptb_acquisition.adapters.base import AcquisitionAdapter

logger = logging.getLogger("ptb.acquisition.adapters.git")

# Delimiters for git log format: Unit Separator (0x1f) and Record Separator (0x1e)
US = "\x1f"
RS = "\x1e"


def _parse_git_timestamp(ts_str: Optional[str]) -> datetime:
    """Parse timestamp ISO-8601 từ git log (%aI) sang datetime UTC."""
    if not ts_str:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(ts_str).astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _build_deep_link(remote_url: Optional[str], commit_hash: str, repo_path: str) -> str:
    """Tạo deep link đến commit trên web (GitHub, GitLab) hoặc local file URI."""
    if not remote_url:
        return f"file://{repo_path}#commit={commit_hash}"

    # Chuẩn hóa SSH remote (git@github.com:org/repo.git) -> HTTPS
    url = remote_url.strip()
    if url.endswith(".git"):
        url = url[:-4]

    ssh_match = re.match(r"^git@([^:]+):(.+)$", url)
    if ssh_match:
        host, repo = ssh_match.groups()
        return f"https://{host}/{repo}/commit/{commit_hash}"

    if url.startswith("http://") or url.startswith("https://"):
        return f"{url}/commit/{commit_hash}"

    return f"file://{repo_path}#commit={commit_hash}"


class GitWatcherAdapter(AcquisitionAdapter):
    """Adapter theo dõi và nạp commit log từ local git repository theo chuẩn AcquisitionAdapter."""

    def __init__(
        self,
        repo_paths: Optional[List[str]] = None,
        tenant_id: str = "local-user",
        git_runner: Optional[Callable[[List[str], str], Any]] = None,
    ):
        super().__init__(source_type=SourceType.GIT, tenant_id=tenant_id)
        if repo_paths is not None:
            self.repo_paths = [os.path.abspath(p) for p in repo_paths]
        else:
            default_path = os.getcwd()
            self.repo_paths = [default_path]
        self._git_runner = git_runner

    async def _run_git_cmd(self, args: List[str], cwd: str) -> str:
        """Thực thi lệnh git an toàn và bất đồng bộ."""
        if self._git_runner:
            res = self._git_runner(args, cwd)
            if asyncio.iscoroutine(res):
                return await res
            return res

        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                err_msg = stderr.decode("utf-8", errors="replace").strip()
                logger.debug(f"Git command failed ('git {' '.join(args)}' in {cwd}): {err_msg}")
                return ""
            return stdout.decode("utf-8", errors="replace").strip()
        except FileNotFoundError:
            logger.error("Lệnh 'git' không tìm thấy trên hệ thống.")
            return ""
        except Exception as e:
            logger.error(f"Lỗi khi thực thi git command in {cwd}: {e}")
            return ""

    async def _is_git_repo(self, path: str) -> bool:
        """Kiểm tra đường dẫn có phải là git repository hợp lệ."""
        out = await self._run_git_cmd(["rev-parse", "--is-inside-work-tree"], cwd=path)
        return out.strip() == "true"

    async def _get_repo_info(self, path: str) -> Dict[str, Any]:
        """Lấy thông tin branch, remote URL và root dir của git repository."""
        branch = await self._run_git_cmd(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
        remote_url = await self._run_git_cmd(["config", "--get", "remote.origin.url"], cwd=path)
        repo_root = await self._run_git_cmd(["rev-parse", "--show-toplevel"], cwd=path)
        repo_name = os.path.basename(repo_root or path)
        return {
            "name": repo_name,
            "root": repo_root or path,
            "branch": branch or "HEAD",
            "remote_url": remote_url or None,
        }

    def commit_to_raw_event(
        self,
        commit: Dict[str, Any],
        repo_name: str,
        repo_path: str,
        remote_url: Optional[str] = None,
    ) -> RawEventRecord:
        """Chuyển đổi dữ liệu commit thành RawEventRecord v1 chuẩn hóa."""
        commit_hash = commit.get("commit_hash", "UNKNOWN")
        parents = commit.get("parents", "")
        parent_id = parents.split()[0] if parents else None
        author_name = commit.get("author_name", "")
        author_email = commit.get("author_email", "git@local")
        subject = commit.get("subject", "")
        body = commit.get("body", "")
        files_changed = commit.get("files_changed", [])

        # Build normalized text
        text_lines = [f"Commit {commit_hash[:8]}: {subject}"]
        if body:
            text_lines.append(f"\n{body.strip()}")
        if files_changed:
            text_lines.append("\nFiles changed:")
            for f in files_changed[:20]:
                text_lines.append(f"  • {f}")
            if len(files_changed) > 20:
                text_lines.append(f"  ... và {len(files_changed) - 20} files khác")

        normalized_text = "\n".join(text_lines).strip()
        content_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()

        # Idempotency key chuẩn v1: SHA256(tenant_id + source_type + external_id + content_hash)
        idempotency_raw = f"{self.tenant_id}:{SourceType.GIT.value}:{commit_hash}:{content_hash}"
        idempotency_key = hashlib.sha256(idempotency_raw.encode("utf-8")).hexdigest()

        event_timestamp = _parse_git_timestamp(commit.get("author_date"))
        now_utc = datetime.now(timezone.utc)
        deep_link = _build_deep_link(remote_url, commit_hash, repo_path)

        payload_dict = {
            **commit,
            "repo_name": repo_name,
            "repo_path": repo_path,
            "remote_url": remote_url,
        }
        payload_json = json.dumps(payload_dict, default=str)

        return RawEventRecord(
            id=f"raw-git-{uuid.uuid4().hex[:12]}",
            tenant_id=self.tenant_id,
            source_type=SourceType.GIT,
            external_id=commit_hash,
            parent_external_id=parent_id,
            idempotency_key=idempotency_key,
            event_timestamp=event_timestamp,
            captured_at=now_utc,
            author_external_id=author_email,
            author_display_name=author_name,
            conversation_or_project_id=repo_name,
            deep_link=deep_link,
            raw_payload=payload_dict,
            payload_json=payload_json,
            normalized_text=normalized_text,
            content_hash=content_hash,
            processing_status=ProcessingStatus.PENDING,
            created_at=now_utc,
        )

    async def discover(self) -> List[Dict[str, Any]]:
        """Khám phá các streams (local repositories) có sẵn."""
        streams: List[Dict[str, Any]] = [
            {
                "stream_id": "all",
                "name": "All Git Repositories",
                "source_type": SourceType.GIT.value,
                "description": "Aggregate stream across all configured local git repositories",
            }
        ]

        for p in self.repo_paths:
            if not os.path.exists(p):
                continue
            is_git = await self._is_git_repo(p)
            if is_git:
                info = await self._get_repo_info(p)
                streams.append({
                    "stream_id": info["name"],
                    "name": f"Git Repo: {info['name']}",
                    "source_type": SourceType.GIT.value,
                    "path": info["root"],
                    "branch": info["branch"],
                    "remote_url": info["remote_url"],
                    "available": True,
                })

        return streams

    def _resolve_repo(self, stream_id: str) -> List[str]:
        """Tìm các repo path tương ứng với stream_id."""
        if stream_id in ("all", "git", ""):
            return self.repo_paths

        # So khớp theo repo_name hoặc đường dẫn tuyệt đối
        matched: List[str] = []
        for p in self.repo_paths:
            name = os.path.basename(p)
            if name == stream_id or p == stream_id:
                matched.append(p)

        return matched or self.repo_paths

    async def _scan_commits(
        self,
        repo_path: str,
        since_revision: Optional[str] = None,
        since_datetime: Optional[datetime] = None,
        max_count: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Quét danh sách commits từ repo bằng git log."""
        if not await self._is_git_repo(repo_path):
            return []

        # Định dạng chuẩn với RS (0x1e) ngăn cách record và US (0x1f) ngăn cách trường
        format_spec = f"{RS}%H{US}%P{US}%an{US}%ae{US}%aI{US}%s{US}%b"
        cmd = ["log", "--reverse", f"--format={format_spec}", "--name-status"]

        if since_revision:
            cmd.append(f"{since_revision}..HEAD")
        elif since_datetime:
            since_utc = since_datetime if since_datetime.tzinfo else since_datetime.replace(tzinfo=timezone.utc)
            cmd.append(f"--since={since_utc.isoformat()}")

        if max_count:
            cmd.extend(["-n", str(max_count)])

        raw_output = await self._run_git_cmd(cmd, cwd=repo_path)
        if not raw_output:
            return []

        commits: List[Dict[str, Any]] = []
        records = raw_output.split(RS)

        for rec in records:
            rec = rec.strip()
            if not rec:
                continue

            parts = rec.split(US)
            if len(parts) < 6:
                continue

            commit_hash = parts[0].strip()
            parents = parts[1].strip()
            author_name = parts[2].strip()
            author_email = parts[3].strip()
            author_date = parts[4].strip()
            subject = parts[5].strip()
            body_and_files = parts[6] if len(parts) > 6 else ""

            # Tách body và danh sách file thay đổi
            body_lines: List[str] = []
            files_changed: List[str] = []

            for line in body_and_files.split("\n"):
                line_stripped = line.strip()
                # Status format: M  file.py, A  test.py, D  old.py
                if re.match(r"^[MADRCUT]\d*\s+", line_stripped):
                    files_changed.append(line_stripped)
                else:
                    body_lines.append(line)

            commits.append({
                "commit_hash": commit_hash,
                "parents": parents,
                "author_name": author_name,
                "author_email": author_email,
                "author_date": author_date,
                "subject": subject,
                "body": "\n".join(body_lines).strip(),
                "files_changed": files_changed,
            })

        return commits

    async def backfill(
        self, stream_id: str, since: Optional[datetime] = None
    ) -> AsyncIterator[RawEventRecord]:
        """Thu thập toàn bộ commits từ stream_id (repo) từ mốc thời gian since."""
        target_paths = self._resolve_repo(stream_id)
        logger.info(f"GitWatcherAdapter backfill stream '{stream_id}' across {len(target_paths)} repos since={since}")

        for path in target_paths:
            if not await self._is_git_repo(path):
                continue
            info = await self._get_repo_info(path)
            commits = await self._scan_commits(path, since_datetime=since)
            for c in commits:
                yield self.commit_to_raw_event(
                    commit=c,
                    repo_name=info["name"],
                    repo_path=info["root"],
                    remote_url=info["remote_url"],
                )

    async def poll_incremental(
        self, stream_id: str, checkpoint: Optional[IngestionCheckpointRecord] = None
    ) -> AsyncIterator[RawEventRecord]:
        """Thu thập các commits mới phát sinh kể từ checkpoint (commit hash hoặc thời gian)."""
        target_paths = self._resolve_repo(stream_id)
        logger.info(f"GitWatcherAdapter poll_incremental stream '{stream_id}' with checkpoint={checkpoint}")

        for path in target_paths:
            if not await self._is_git_repo(path):
                continue
            info = await self._get_repo_info(path)

            since_rev = checkpoint.last_external_id if (checkpoint and checkpoint.last_external_id) else None
            since_dt = checkpoint.last_event_timestamp if (checkpoint and not since_rev) else None

            commits = await self._scan_commits(path, since_revision=since_rev, since_datetime=since_dt)
            for c in commits:
                # Bỏ qua nếu commit hash trùng với last_external_id
                if checkpoint and checkpoint.last_external_id and c["commit_hash"] == checkpoint.last_external_id:
                    continue
                yield self.commit_to_raw_event(
                    commit=c,
                    repo_name=info["name"],
                    repo_path=info["root"],
                    remote_url=info["remote_url"],
                )

    async def health(self) -> Dict[str, Any]:
        """Kiểm tra sự hiện diện của Git CLI và các repository đã cấu hình."""
        git_ver = await self._run_git_cmd(["--version"], cwd=os.getcwd())
        has_git = bool(git_ver and "git version" in git_ver)

        valid_repos: List[str] = []
        for p in self.repo_paths:
            if os.path.exists(p) and await self._is_git_repo(p):
                valid_repos.append(p)

        status = "healthy" if has_git and len(valid_repos) > 0 else ("degraded" if has_git else "unhealthy")

        return {
            "source_type": SourceType.GIT.value,
            "tenant_id": self.tenant_id,
            "status": status,
            "git_version": git_ver or "not found",
            "repo_paths_configured": self.repo_paths,
            "valid_git_repos": valid_repos,
        }
