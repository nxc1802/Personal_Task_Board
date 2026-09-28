"""TaskCandidateMatcher: Finds matching task candidates using deterministic anchors and semantic similarity."""

from dataclasses import dataclass
from datetime import datetime
import logging
import re
from typing import Any, List, Optional, Set
from urllib.parse import urlparse

from ptb_contracts.l2_processing import (
    EvidenceRecord,
    TaskStatus,
    UnifiedTaskCandidate,
)
from ptb_processing.correlation.scoring import (
    CorrelationScoreResult,
    CorrelationScorer,
)

logger = logging.getLogger("ptb.processing.correlation.candidate_matcher")

# Non-Jira false-positive uppercase tokens
COMMON_NON_JIRA = {
    "UTF-8", "UTF-16", "ISO-8859", "SHA-1", "SHA-256", "SHA-512",
    "IPV-4", "IPV-6", "UUID-4", "V-1", "V-2", "API-1", "HTTP-1",
}


@dataclass(frozen=True)
class DeterministicAnchor:
    """Represents a deterministic anchor identifying a task or external work unit."""

    anchor_type: str  # "jira", "shortcut", "pr", "commit", "thread", "url"
    value: str        # Normalized identifier (e.g. "OPS-88", "story-1204", "pr:org/repo:12")


@dataclass
class CandidateMatch:
    """Match result representing an existing task and the correlation score with the candidate."""

    target_task: UnifiedTaskCandidate
    score_result: CorrelationScoreResult


class TaskCandidateMatcher:
    """Finds candidate tasks in Neo4j (or mock TaskDomainRepository) using:
    1) Deterministic Anchors (Jira key, Shortcut story, PR URL, Commit SHA, Thread ID).
    2) Semantic / Text Similarity (title/snippet token similarity).
    """

    def __init__(
        self,
        task_repo: Optional[Any] = None,
        neo4j_client: Optional[Any] = None,
        scorer: Optional[CorrelationScorer] = None,
    ) -> None:
        self.task_repo = task_repo
        self.neo4j_client = neo4j_client
        self.scorer = scorer or CorrelationScorer()

    @classmethod
    def extract_anchors_from_text(cls, text: str) -> Set[DeterministicAnchor]:
        """Extract deterministic anchors from a block of text or URL string."""
        anchors: Set[DeterministicAnchor] = set()
        if not text:
            return anchors

        # 1. Jira key: e.g. OPS-88, PROJ-123
        jira_matches = re.findall(r"\b[A-Z][A-Z0-9]+-\d+\b", text)
        for m in jira_matches:
            if m not in COMMON_NON_JIRA:
                anchors.add(DeterministicAnchor(anchor_type="jira", value=m.upper()))

        # Also Jira key from URL
        jira_url_matches = re.findall(r"(?:atlassian\.net|jira[^/\s]*)/browse/([A-Z][A-Z0-9]+-\d+)", text, re.IGNORECASE)
        for m in jira_url_matches:
            if m.upper() not in COMMON_NON_JIRA:
                anchors.add(DeterministicAnchor(anchor_type="jira", value=m.upper()))

        # 2. Shortcut story ID: e.g. story-1204, sc-1204, shortcut.com/.../story/1204
        shortcut_matches = re.findall(r"\b(?:story-|sc-|story/)(\d+)\b", text, re.IGNORECASE)
        for m in shortcut_matches:
            anchors.add(DeterministicAnchor(anchor_type="shortcut", value=f"story-{m}"))

        shortcut_url_matches = re.findall(r"shortcut\.com/[^\s]+/story/(\d+)", text, re.IGNORECASE)
        for m in shortcut_url_matches:
            anchors.add(DeterministicAnchor(anchor_type="shortcut", value=f"story-{m}"))

        shortcut_raw_matches = re.findall(r"\braw-(\d+)-shortcut\b", text, re.IGNORECASE)
        for m in shortcut_raw_matches:
            anchors.add(DeterministicAnchor(anchor_type="shortcut", value=f"story-{m}"))

        # 3. PR URL: e.g. github.com/owner/repo/pull/123
        gh_pr_matches = re.findall(r"github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)", text, re.IGNORECASE)
        for repo, pr_num in gh_pr_matches:
            anchors.add(DeterministicAnchor(anchor_type="pr", value=f"github:{repo.lower()}:{pr_num}"))

        gl_pr_matches = re.findall(r"gitlab\.com/([^/\s]+/[^/\s]+)/-/merge_requests/(\d+)", text, re.IGNORECASE)
        for repo, pr_num in gl_pr_matches:
            anchors.add(DeterministicAnchor(anchor_type="pr", value=f"gitlab:{repo.lower()}:{pr_num}"))

        # 4. Commit SHA: e.g. github.com/.../commit/abcdef123... or explicit commit: abcdef...
        commit_url_matches = re.findall(r"(?:github\.com|gitlab\.com)/[^/\s]+/[^/\s]+/(?:commit|commits)/([0-9a-fA-F]{7,40})", text)
        for c in commit_url_matches:
            anchors.add(DeterministicAnchor(anchor_type="commit", value=c[:8].lower()))

        commit_text_matches = re.findall(r"\b(?:commit|sha)[:\s]+([0-9a-fA-F]{7,40})\b", text, re.IGNORECASE)
        for c in commit_text_matches:
            anchors.add(DeterministicAnchor(anchor_type="commit", value=c[:8].lower()))

        # Standalone full 40-character git commit SHA
        standalone_sha_matches = re.findall(r"\b([0-9a-fA-F]{40})\b", text)
        for c in standalone_sha_matches:
            anchors.add(DeterministicAnchor(anchor_type="commit", value=c[:8].lower()))

        # 5. Conversation Thread ID: Teams thread ID e.g. 19:channel-devops-alerts or 19:...@thread...
        teams_thread_matches = re.findall(r"\b(19:[a-zA-Z0-9_\-\.]+@thread\.[a-zA-Z0-9]+)\b", text)
        for t in teams_thread_matches:
            anchors.add(DeterministicAnchor(anchor_type="thread", value=t.lower()))

        teams_chan_matches = re.findall(r"\b(19:[a-zA-Z0-9_\-\.]+)\b", text)
        for t in teams_chan_matches:
            anchors.add(DeterministicAnchor(anchor_type="thread", value=t.lower()))

        return anchors

    def extract_anchors(self, task: UnifiedTaskCandidate) -> Set[DeterministicAnchor]:
        """Extract all deterministic anchors from a task candidate and its evidences."""
        anchors: Set[DeterministicAnchor] = set()

        # Check title
        if task.title:
            anchors.update(self.extract_anchors_from_text(task.title))

        # Check description
        if task.description:
            anchors.update(self.extract_anchors_from_text(task.description))

        # Check project key
        if task.project_key:
            anchors.update(self.extract_anchors_from_text(task.project_key))

        # Check evidences
        for ev in task.evidences:
            if ev.snippet:
                anchors.update(self.extract_anchors_from_text(ev.snippet))
            if ev.external_url:
                anchors.update(self.extract_anchors_from_text(ev.external_url))
                # Add normalized URL anchor
                clean_url = self._normalize_url(ev.external_url)
                if clean_url:
                    anchors.add(DeterministicAnchor(anchor_type="url", value=clean_url))
            if ev.raw_event_id:
                anchors.update(self.extract_anchors_from_text(ev.raw_event_id))

        return anchors

    @staticmethod
    def _normalize_url(url: str) -> Optional[str]:
        """Normalize URL for exact match comparison."""
        if not url:
            return None
        try:
            parsed = urlparse(url)
            if not parsed.scheme or not parsed.netloc:
                return None
            clean_path = parsed.path.rstrip("/")
            return f"{parsed.netloc.lower()}{clean_path}"
        except Exception:
            return url.strip().rstrip("/").lower()

    @staticmethod
    def check_anchor_conflict(
        candidate_anchors: Set[DeterministicAnchor],
        target_anchors: Set[DeterministicAnchor],
    ) -> bool:
        """Check if candidate and target task have conflicting deterministic keys of the same type.

        For example:
        - Candidate has Jira key OPS-88, Target has Jira key OPS-89 -> Conflict!
        - Candidate has Shortcut story-1204, Target has Shortcut story-9999 -> Conflict!
        """
        # Check Jira keys
        cand_jira = {a.value for a in candidate_anchors if a.anchor_type == "jira"}
        target_jira = {a.value for a in target_anchors if a.anchor_type == "jira"}
        if cand_jira and target_jira and not (cand_jira & target_jira):
            return True

        # Check Shortcut stories
        cand_sc = {a.value for a in candidate_anchors if a.anchor_type == "shortcut"}
        target_sc = {a.value for a in target_anchors if a.anchor_type == "shortcut"}
        if cand_sc and target_sc and not (cand_sc & target_sc):
            return True

        # Check PRs
        cand_pr = {a.value for a in candidate_anchors if a.anchor_type == "pr"}
        target_pr = {a.value for a in target_anchors if a.anchor_type == "pr"}
        if cand_pr and target_pr and not (cand_pr & target_pr):
            return True

        return False

    async def _fetch_tasks_from_store(self) -> List[UnifiedTaskCandidate]:
        """Fetch active tasks from repository or Neo4j."""
        # 1. If task_repo has get_active_tasks
        if self.task_repo is not None and hasattr(self.task_repo, "get_active_tasks"):
            try:
                tasks = await self.task_repo.get_active_tasks()
                return tasks or []
            except Exception as e:
                logger.warning(f"Failed to fetch active tasks from task_repo.get_active_tasks: {e}")

        # 2. If task_repo has neo4j_client or client is directly provided
        client = None
        if self.task_repo is not None and hasattr(self.task_repo, "neo4j_client"):
            client = self.task_repo.neo4j_client
        elif self.neo4j_client is not None:
            client = self.neo4j_client

        if client is not None:
            return await self._query_neo4j_active_tasks(client)

        return []

    async def _query_neo4j_active_tasks(self, client: Any) -> List[UnifiedTaskCandidate]:
        """Query Neo4j for active UnifiedTasks and their evidences."""
        cypher = """
        MATCH (t:UnifiedTask)
        WHERE t.status IN ['TODO', 'IN_PROGRESS', 'BLOCKED']
        OPTIONAL MATCH (owner:Person)-[:ASSIGNED_TO]->(t)
        OPTIONAL MATCH (req:Person)-[:REQUESTED]->(t)
        OPTIONAL MATCH (t)-[:HAS_EVIDENCE]->(e:Evidence)
        RETURN t,
               owner.canonical_id AS owner_canonical_id,
               owner.canonical_name AS owner_name,
               req.canonical_id AS requester_canonical_id,
               req.canonical_name AS requester_name,
               collect(e) AS evidences
        ORDER BY t.updated_at DESC
        LIMIT 200
        """
        driver = client.get_driver()
        tasks: List[UnifiedTaskCandidate] = []
        try:
            async with driver.session(database=client.database) as session:
                result = await session.run(cypher)
                async for row in result:
                    if not row or not row["t"]:
                        continue
                    task_dict = dict(row["t"])
                    task_dict["owner_canonical_id"] = row["owner_canonical_id"]
                    task_dict["owner_name"] = row["owner_name"]
                    task_dict["requester_canonical_id"] = row["requester_canonical_id"]
                    task_dict["requester_name"] = row["requester_name"]

                    if "status" in task_dict and isinstance(task_dict["status"], str):
                        try:
                            task_dict["status"] = TaskStatus(task_dict["status"])
                        except Exception:
                            task_dict["status"] = TaskStatus.TODO
                    if "priority_score" not in task_dict or task_dict["priority_score"] is None:
                        task_dict["priority_score"] = 0.0
                    if "extraction_confidence" not in task_dict or task_dict["extraction_confidence"] is None:
                        task_dict["extraction_confidence"] = 1.0
                    if "review_status" not in task_dict or task_dict["review_status"] is None:
                        task_dict["review_status"] = "auto_approved"

                    for dt_field in ["due_date", "created_at", "updated_at"]:
                        if dt_field in task_dict and isinstance(task_dict[dt_field], str):
                            try:
                                task_dict[dt_field] = datetime.fromisoformat(task_dict[dt_field])
                            except Exception:
                                pass

                    evidences: List[EvidenceRecord] = []
                    for ev_node in (row["evidences"] or []):
                        if ev_node is not None:
                            ev_data = dict(ev_node)
                            if "timestamp" in ev_data and isinstance(ev_data["timestamp"], str):
                                try:
                                    ev_data["timestamp"] = datetime.fromisoformat(ev_data["timestamp"])
                                except Exception:
                                    pass
                            if "confidence" not in ev_data or ev_data["confidence"] is None:
                                ev_data["confidence"] = 1.0
                            if "extraction_version" not in ev_data or ev_data["extraction_version"] is None:
                                ev_data["extraction_version"] = "v1.0"
                            evidences.append(EvidenceRecord.model_validate(ev_data))
                    task_dict["evidences"] = evidences

                    tasks.append(UnifiedTaskCandidate.model_validate(task_dict))
        except Exception as e:
            logger.error(f"Error executing Neo4j active tasks query: {e}")
        return tasks

    async def find_candidates(
        self,
        candidate: UnifiedTaskCandidate,
        existing_tasks: Optional[List[UnifiedTaskCandidate]] = None,
    ) -> List[CandidateMatch]:
        """Find matching existing tasks for candidate, scored by correlation confidence."""
        if existing_tasks is None:
            existing_tasks = await self._fetch_tasks_from_store()

        cand_anchors = self.extract_anchors(candidate)
        matches: List[CandidateMatch] = []

        for target in existing_tasks:
            # Skip comparing against itself
            if target.id == candidate.id:
                continue

            target_anchors = self.extract_anchors(target)

            # Check if there is an anchor conflict (e.g. different Jira keys)
            has_conflict = self.check_anchor_conflict(cand_anchors, target_anchors)

            # Find matching anchors
            common_anchors = cand_anchors & target_anchors
            matching_anchor_values = sorted([f"{a.anchor_type}:{a.value}" for a in common_anchors])

            # Compute correlation score
            score_result = self.scorer.score(
                candidate=candidate,
                target_task=target,
                matching_anchors=matching_anchor_values,
                has_conflict=has_conflict,
            )

            # If score is non-trivial or anchors matched, record candidate match
            if score_result.correlation_confidence >= 0.10 or score_result.has_deterministic_anchor:
                matches.append(CandidateMatch(target_task=target, score_result=score_result))

        # Sort matches by correlation_confidence descending
        matches.sort(key=lambda m: m.score_result.correlation_confidence, reverse=True)
        return matches

    async def find_best_match(
        self,
        candidate: UnifiedTaskCandidate,
        existing_tasks: Optional[List[UnifiedTaskCandidate]] = None,
    ) -> Optional[CandidateMatch]:
        """Find the top matching candidate that qualifies for auto-merge, if any."""
        matches = await self.find_candidates(candidate, existing_tasks=existing_tasks)
        for m in matches:
            if m.score_result.should_merge:
                return m
        return None
