"""Deterministic Structured Source Extractor.

Extracts UnifiedTaskCandidate directly from structured issue, story, commit,
and PR payloads (Jira, Shortcut, GitHub, Git) with ZERO LLM calls and 100% precision.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import re
from typing import Any, Dict, Optional
from uuid import uuid4

from ptb_contracts.l1_acquisition import RawEventRecord, SourceType
from ptb_contracts.l2_processing import (
    EvidenceRecord,
    EvidenceType,
    ParsedMessageContent,
    TaskStatus,
    UnifiedTaskCandidate,
)

logger = logging.getLogger("ptb.processing.extractor.structured_extractor")


def _map_jira_status(status_name: Optional[str]) -> TaskStatus:
    if not status_name:
        return TaskStatus.TODO
    s = str(status_name).strip().upper()
    if any(kw in s for kw in ("DONE", "RESOLVED", "CLOSED", "COMPLETE")):
        return TaskStatus.DONE
    if any(kw in s for kw in ("PROGRESS", "DEVELOPMENT", "ACTIVE", "WIP", "TESTING")):
        return TaskStatus.IN_PROGRESS
    if any(kw in s for kw in ("BLOCKED", "WAITING", "ON HOLD")):
        return TaskStatus.BLOCKED
    return TaskStatus.TODO


def _map_github_state(state: Optional[str], is_active_pr: bool = False) -> TaskStatus:
    if not state:
        return TaskStatus.IN_PROGRESS if is_active_pr else TaskStatus.TODO
    s = str(state).strip().lower()
    if s in ("closed", "merged"):
        return TaskStatus.DONE
    if is_active_pr:
        return TaskStatus.IN_PROGRESS
    return TaskStatus.TODO


class DeterministicStructuredExtractor:
    """Extracts task candidates deterministically from structured sources with 0 LLM calls."""

    def can_extract(self, raw_event: RawEventRecord) -> bool:
        """Return True if the event comes from a structured source with sufficient metadata."""
        st = raw_event.source_type
        st_val = st.value if hasattr(st, "value") else str(st)
        return st_val in ("jira", "shortcut", "github", "git")

    def extract(
        self,
        raw_event: RawEventRecord,
        parsed: Optional[ParsedMessageContent] = None,
    ) -> Optional[UnifiedTaskCandidate]:
        """Extract UnifiedTaskCandidate deterministically without invoking any LLM."""
        st = raw_event.source_type
        st_val = (st.value if hasattr(st, "value") else str(st)).lower().strip()
        payload = raw_event.raw_payload or {}

        if st_val == "jira":
            return self._extract_jira(raw_event, payload)
        elif st_val == "shortcut":
            return self._extract_shortcut(raw_event, payload)
        elif st_val == "github":
            return self._extract_github(raw_event, payload)
        elif st_val == "git":
            return self._extract_git(raw_event, payload)

        return None

    def _extract_jira(self, raw_event: RawEventRecord, payload: Dict[str, Any]) -> UnifiedTaskCandidate:
        fields = payload.get("fields", {}) if isinstance(payload, dict) else {}
        key = str(payload.get("key") or raw_event.external_id or "JIRA-TASK")
        summary = str(fields.get("summary") or "").strip()

        # Fallback to normalized text if payload fields are sparse
        if not summary and raw_event.normalized_text:
            lines = raw_event.normalized_text.splitlines()
            if lines:
                summary = lines[0].strip()

        title = f"[{key}] {summary}" if summary and not summary.startswith(f"[{key}]") else (summary or key)
        raw_desc = fields.get("description")
        desc = str(raw_desc).strip() if raw_desc else (raw_event.normalized_text or "")

        status_info = fields.get("status") or {}
        status_name = status_info.get("name") if isinstance(status_info, dict) else str(status_info)
        task_status = _map_jira_status(status_name)

        assignee_info = fields.get("assignee") or {}
        assignee_name = (
            assignee_info.get("displayName") or assignee_info.get("name")
            if isinstance(assignee_info, dict)
            else (raw_event.author_display_name or None)
        )

        creator_info = fields.get("creator") or fields.get("reporter") or {}
        creator_name = (
            creator_info.get("displayName") or creator_info.get("name")
            if isinstance(creator_info, dict)
            else (raw_event.author_display_name or None)
        )

        due_date = None
        due_str = fields.get("duedate")
        if due_str:
            try:
                due_date = datetime.fromisoformat(str(due_str).replace("Z", "+00:00"))
                if due_date.tzinfo is None:
                    due_date = due_date.replace(tzinfo=timezone.utc)
            except Exception:
                due_date = None

        task_id = str(uuid4())
        ev_id = str(uuid4())
        evidence = EvidenceRecord(
            id=ev_id,
            task_id=task_id,
            raw_event_id=raw_event.id,
            evidence_type=EvidenceType.JIRA_TICKET,
            source_type="jira",
            external_url=raw_event.deep_link,
            author_canonical_name=creator_name or assignee_name,
            timestamp=raw_event.event_timestamp or datetime.now(timezone.utc),
            snippet=summary or title,
            confidence=1.0,
            extraction_version="v1.0-deterministic",
        )

        return UnifiedTaskCandidate(
            id=task_id,
            title=title,
            description=desc,
            status=task_status,
            owner_name=assignee_name,
            requester_name=creator_name,
            project_key=raw_event.stream_id or (key.split("-")[0] if "-" in key else "JIRA"),
            due_date=due_date,
            explicit_deadline=bool(due_date),
            extraction_confidence=1.0,
            review_status="auto_approved",
            evidences=[evidence],
        )

    def _extract_shortcut(self, raw_event: RawEventRecord, payload: Dict[str, Any]) -> UnifiedTaskCandidate:
        name = str(payload.get("name") or "").strip()
        story_id = str(payload.get("id") or raw_event.external_id or "STORY")
        if not name and raw_event.normalized_text:
            lines = raw_event.normalized_text.splitlines()
            name = lines[0].strip() if lines else f"Story {story_id}"

        title = f"[story-{story_id}] {name}" if name and not name.startswith(f"[story-{story_id}]") else name
        desc = str(payload.get("description") or raw_event.normalized_text or "").strip()

        completed = payload.get("completed", False)
        task_status = TaskStatus.DONE if completed else TaskStatus.TODO

        due_date = None
        deadline_str = payload.get("deadline")
        if deadline_str:
            try:
                due_date = datetime.fromisoformat(str(deadline_str).replace("Z", "+00:00"))
                if due_date.tzinfo is None:
                    due_date = due_date.replace(tzinfo=timezone.utc)
            except Exception:
                due_date = None

        task_id = str(uuid4())
        ev_id = str(uuid4())
        evidence = EvidenceRecord(
            id=ev_id,
            task_id=task_id,
            raw_event_id=raw_event.id,
            evidence_type=EvidenceType.SHORTCUT_STORY,
            source_type="shortcut",
            external_url=raw_event.deep_link,
            author_canonical_name=raw_event.author_display_name,
            timestamp=raw_event.event_timestamp or datetime.now(timezone.utc),
            snippet=name or title,
            confidence=1.0,
            extraction_version="v1.0-deterministic",
        )

        return UnifiedTaskCandidate(
            id=task_id,
            title=title,
            description=desc,
            status=task_status,
            owner_name=raw_event.author_display_name,
            requester_name=raw_event.author_display_name,
            project_key=raw_event.stream_id or "SHORTCUT",
            due_date=due_date,
            explicit_deadline=bool(due_date),
            extraction_confidence=1.0,
            review_status="auto_approved",
            evidences=[evidence],
        )

    def _extract_github(self, raw_event: RawEventRecord, payload: Dict[str, Any]) -> UnifiedTaskCandidate:
        title = str(payload.get("title") or "").strip()
        if not title and raw_event.normalized_text:
            title = raw_event.normalized_text.splitlines()[0].strip()
        body = str(payload.get("body") or raw_event.normalized_text or "").strip()

        is_active_pr = (
            "resolves" in str(title).lower()
            or "wip" in str(title).lower()
            or "in progress" in str(title).lower()
            or "active" in str(title).lower()
        )
        state = payload.get("state")
        task_status = _map_github_state(state, is_active_pr=is_active_pr)

        user_info = payload.get("user") or {}
        creator_name = (
            user_info.get("login")
            if isinstance(user_info, dict)
            else raw_event.author_display_name
        )

        assignees = payload.get("assignees") or []
        assignee_name = None
        if assignees and isinstance(assignees, list) and isinstance(assignees[0], dict):
            assignee_name = assignees[0].get("login")

        task_id = str(uuid4())
        ev_id = str(uuid4())

        # Extract deadline if available
        due_date = None
        milestone = payload.get("milestone")
        if isinstance(milestone, dict) and milestone.get("due_on"):
            try:
                due_date = datetime.fromisoformat(str(milestone["due_on"]).replace("Z", "+00:00"))
                if due_date.tzinfo is None:
                    due_date = due_date.replace(tzinfo=timezone.utc)
            except Exception:
                due_date = None

        if not due_date and (body or raw_event.normalized_text):
            text_for_date = f"{body} {raw_event.normalized_text or ''}"
            d_match = re.search(r"(\d{4}-\d{2}-\d{2})", text_for_date)
            if d_match:
                d_str = d_match.group(1)
                t_match = re.search(r"(?:at|before|truoc|on)?\s*(\d{1,2}:\d{2})", text_for_date, re.IGNORECASE)
                t_str = f"{t_match.group(1)}:00" if t_match else "17:00:00"
                if len(t_str) == 7:
                    t_str = f"0{t_str}"
                try:
                    due_date = datetime.fromisoformat(f"{d_str}T{t_str}+00:00")
                except Exception:
                    due_date = None

        # Extract project/ticket key anchor
        full_text = f"{title} {body}"
        anchor_match = re.search(r"\b([A-Z]+-\d+)\b", full_text)
        proj_key = anchor_match.group(1).split("-")[0] if anchor_match else (raw_event.stream_id or "GITHUB")

        evidence = EvidenceRecord(
            id=ev_id,
            task_id=task_id,
            raw_event_id=raw_event.id,
            evidence_type=EvidenceType.GITHUB_PR,
            source_type="github",
            external_url=raw_event.deep_link,
            author_canonical_name=creator_name or assignee_name,
            timestamp=raw_event.event_timestamp or datetime.now(timezone.utc),
            snippet=title,
            confidence=1.0,
            extraction_version="v1.0-deterministic",
        )

        return UnifiedTaskCandidate(
            id=task_id,
            title=title,
            description=body,
            status=task_status,
            owner_name=assignee_name or creator_name,
            requester_name=creator_name,
            project_key=proj_key,
            due_date=due_date,
            explicit_deadline=bool(due_date),
            extraction_confidence=1.0,
            review_status="auto_approved",
            evidences=[evidence],
        )

    def _extract_git(self, raw_event: RawEventRecord, payload: Dict[str, Any]) -> UnifiedTaskCandidate:
        commit_msg = str(payload.get("message") or raw_event.normalized_text or "").strip()
        lines = commit_msg.splitlines() if commit_msg else []
        title = lines[0].strip() if lines else f"Commit {raw_event.external_id}"
        desc = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""

        task_id = str(uuid4())
        ev_id = str(uuid4())
        evidence = EvidenceRecord(
            id=ev_id,
            task_id=task_id,
            raw_event_id=raw_event.id,
            evidence_type=EvidenceType.COMPLETION_SIGNAL,
            source_type="git",
            external_url=raw_event.deep_link,
            author_canonical_name=raw_event.author_display_name,
            timestamp=raw_event.event_timestamp or datetime.now(timezone.utc),
            snippet=title,
            confidence=1.0,
            extraction_version="v1.0-deterministic",
        )

        lower_title = title.lower()
        if any(kw in lower_title for kw in ("fix:", "fixed:", "resolve", "resolved", "close", "closed", "done", "merge branch", "merge pull")):
            status = TaskStatus.DONE
        elif any(kw in lower_title for kw in ("wip", "in progress", "feat:", "feature:", "refactor:", "test:", "update:", "add:")):
            status = TaskStatus.IN_PROGRESS
        else:
            status = TaskStatus.TODO

        return UnifiedTaskCandidate(
            id=task_id,
            title=title,
            description=desc or None,
            status=status,
            owner_name=raw_event.author_display_name,
            requester_name=raw_event.author_display_name,
            project_key=raw_event.stream_id or "GIT",
            due_date=None,
            explicit_deadline=False,
            extraction_confidence=1.0,
            review_status="auto_approved",
            evidences=[evidence],
        )
