"""ProcessingPipeline: Orchestrates Layer 2 event processing pipeline.

Tích hợp tuần tự các bước xử lý RawEvent theo kiến trúc L2 (docs/v1_1.md):
1. TeamsQuoteReplyParser: bóc tách quote/reply, clean HTML.
2. IdentityResolver: chuẩn hóa Person ID (tuân thủ quy tắc bất biến).
3. HeuristicCandidateFilter: kiểm tra should_extract(text). Nếu không có tín hiệu cam kết/yêu cầu -> mark PROCESSED (không cần gọi LLM).
4. LLMStructuredExtractor: trích xuất candidate.
5. AttributionValidator: xác thực owner/requester.
6. TaskCandidateMatcher & TaskMerger: correlation và auto-merge nếu khớp.
7. Lưu vào TaskDomainRepository.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re
from typing import Any, List, Optional
from uuid import uuid4

from ptb_contracts.l1_acquisition import (
    ProcessingStatus,
    RawEventRecord,
)
from ptb_contracts.l2_processing import (
    ParsedMessageContent,
    TaskStatus,
    UnifiedTaskCandidate,
)
from ptb_contracts.logging import BugCode, log_bug
from ptb_contracts.telemetry import get_cost_audit_tracker
from ptb_database.repositories.task_repo import TaskDomainRepository
from ptb_processing.correlation.candidate_matcher import TaskCandidateMatcher
from ptb_processing.correlation.merger import TaskMerger
from ptb_processing.extractor.heuristic_filter import HeuristicCandidateFilter
from ptb_processing.extractor.llm_extractor import (
    LLMExtractionError,
    LLMStructuredExtractor,
)
from ptb_processing.extractor.structured_extractor import DeterministicStructuredExtractor
from ptb_processing.identity.resolver import IdentityResolutionResult, IdentityResolver
from ptb_processing.parsers.multi_channel import UnifiedChannelParser
from ptb_processing.parsers.quote_reply import TeamsQuoteReplyParser
from ptb_processing.validator.attribution import AttributionReport, AttributionValidator

try:
    from ptb_intelligence.lifecycle import TaskIntelligenceLifecycle
except ImportError:
    TaskIntelligenceLifecycle = Any  # type: ignore

logger = logging.getLogger("ptb.processing.pipeline")


@dataclass
class PipelineResult:
    """Kết quả xử lý RawEvent qua ProcessingPipeline."""

    raw_event_id: str
    status: ProcessingStatus
    should_extract: bool = True
    candidate: Optional[UnifiedTaskCandidate] = None
    merged: bool = False
    target_task_id: Optional[str] = None
    task_id: Optional[str] = None
    attribution_report: Optional[AttributionReport] = None
    error: Optional[str] = None


class ProcessingPipeline:
    """Pipeline điều phối tuần tự quá trình trích xuất và tương quan task."""

    def __init__(
        self,
        task_repo: Optional[TaskDomainRepository] = None,
        quote_parser: Optional[TeamsQuoteReplyParser] = None,
        identity_resolver: Optional[IdentityResolver] = None,
        heuristic_filter: Optional[HeuristicCandidateFilter] = None,
        llm_extractor: Optional[LLMStructuredExtractor] = None,
        attribution_validator: Optional[AttributionValidator] = None,
        candidate_matcher: Optional[TaskCandidateMatcher] = None,
        task_merger: Optional[TaskMerger] = None,
        intelligence_lifecycle: Optional[TaskIntelligenceLifecycle] = None,
        structured_extractor: Optional[DeterministicStructuredExtractor] = None,
    ) -> None:
        self.task_repo = task_repo
        self.multi_channel_parser = UnifiedChannelParser()
        self.quote_parser = quote_parser or self.multi_channel_parser
        self.identity_resolver = identity_resolver or IdentityResolver()
        self.heuristic_filter = heuristic_filter or HeuristicCandidateFilter()
        self.structured_extractor = structured_extractor or DeterministicStructuredExtractor()
        self.llm_extractor = llm_extractor or LLMStructuredExtractor()
        self.attribution_validator = attribution_validator or AttributionValidator()
        if candidate_matcher is not None:
            self.candidate_matcher = candidate_matcher
        else:
            reranker = None
            try:
                from packages.ai_service import KevDecisionReranker
                reranker = KevDecisionReranker()
            except Exception:
                reranker = None
            self.candidate_matcher = TaskCandidateMatcher(task_repo=self.task_repo, reranker=reranker)
        self.task_merger = task_merger or TaskMerger(task_repo=self.task_repo)
        self.intelligence_lifecycle = intelligence_lifecycle
        self.health_status: str = "HEALTHY"

    async def _resolve_identity(
        self,
        name: Optional[str],
        external_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        source_type: Optional[str] = None,
    ) -> IdentityResolutionResult:
        """Resolve identity tuân thủ quy tắc bất biến (chỉ exact match mới link ID)."""
        email = external_id if (external_id and "@" in external_id) else None
        if not email and name and "@" in name:
            email = name
        account_id = external_id or (name if name and ("-" in name or "_" in name or "." in name or " " not in name) else None)

        if hasattr(self.identity_resolver, "resolve_async"):
            return await self.identity_resolver.resolve_async(
                email=email,
                account_id=account_id,
                display_name=name,
                tenant_id=tenant_id,
                source_type=source_type,
            )
        return self.identity_resolver.resolve(
            email=email,
            account_id=account_id,
            display_name=name,
            tenant_id=tenant_id,
            source_type=source_type,
        )

    async def process(self, raw_event: RawEventRecord) -> PipelineResult:
        """Chạy RawEvent qua chuỗi xử lý Layer 2."""
        source_type_str = (
            raw_event.source_type.value
            if hasattr(raw_event.source_type, "value")
            else str(raw_event.source_type)
        )
        bug_logged = False

        try:
            get_cost_audit_tracker().record_event_processed(1)

            # 1. Multi-Channel Parser: Bóc tách quote/reply, clean HTML & chuẩn hóa theo từng kênh
            content_to_parse = (
                raw_event.raw_payload
                if raw_event.raw_payload
                else (raw_event.normalized_text or "")
            )
            actual_author = raw_event.author_display_name or raw_event.author_external_id

            if hasattr(self.quote_parser, "get_parser_for_source"):
                parsed = self.quote_parser.parse(
                    content_to_parse,
                    source_type=raw_event.source_type,
                    actual_author=actual_author,
                )
            elif isinstance(self.quote_parser, TeamsQuoteReplyParser) and source_type_str not in ("ms_teams", "ms_teams_web"):
                parsed = self.multi_channel_parser.parse(
                    content_to_parse,
                    source_type=raw_event.source_type,
                    actual_author=actual_author,
                )
            else:
                parsed = self.quote_parser.parse(content_to_parse, actual_author=actual_author)

            if not parsed.actual_content_text and raw_event.normalized_text:
                parsed.actual_content_text = raw_event.normalized_text

            # 2. HeuristicCandidateFilter: Kiểm tra should_extract(text, source_type)
            text_for_heuristic = (
                parsed.actual_content_text
                or parsed.quoted_content_text
                or raw_event.normalized_text
                or ""
            )
            should_extract = self.heuristic_filter.should_extract(text_for_heuristic, source_type=raw_event.source_type)
            if not should_extract:
                logger.info(
                    "RawEvent %s rejected by HeuristicCandidateFilter. Marking as PROCESSED without LLM call.",
                    raw_event.id,
                )
                return PipelineResult(
                    raw_event_id=raw_event.id,
                    status=ProcessingStatus.PROCESSED,
                    should_extract=False,
                )

            # 3. Extraction: Structured source bypass first (zero LLM calls), otherwise LLMStructuredExtractor
            candidate = None
            if self.structured_extractor and self.structured_extractor.can_extract(raw_event):
                try:
                    candidate = self.structured_extractor.extract(raw_event, parsed=parsed)
                    if candidate is not None:
                        get_cost_audit_tracker().record_structured_bypass(1)
                        logger.info(
                            "RawEvent %s deterministically extracted via StructuredSourceExtractor (%s) with 0 LLM calls.",
                            raw_event.id,
                            source_type_str,
                        )
                except Exception as struct_err:
                    logger.warning("Structured extraction failed for %s, falling back to LLM: %s", raw_event.id, struct_err)
                    candidate = None

            if candidate is None:
                try:
                    candidate = await self.llm_extractor.extract_async(
                        parsed=parsed,
                        raw_event_id=raw_event.id,
                        source_type=source_type_str,
                        external_url=raw_event.deep_link,
                    )
                except Exception as llm_exc:
                    self.health_status = "DEGRADED"
                    bug_logged = True
                    log_bug(
                        BugCode.PTB_LLM_001,
                        subsystem="llm",
                        severity="ERROR",
                        message=f"LLM extraction failed for RawEvent {raw_event.id}: {llm_exc}",
                        exc=llm_exc,
                        raw_event_id=raw_event.id,
                        source_type=source_type_str,
                        tenant_id=raw_event.tenant_id,
                    )
                    raise

            # 3b. Gate: ignore candidates không được persist thành Task
            if getattr(candidate, "review_status", None) == "ignore":
                logger.info(
                    "RawEvent %s → candidate confidence %.2f (review_status='ignore'). "
                    "Marking PROCESSED without creating UnifiedTask.",
                    raw_event.id,
                    getattr(candidate, "extraction_confidence", 0.0),
                )
                return PipelineResult(
                    raw_event_id=raw_event.id,
                    status=ProcessingStatus.PROCESSED,
                    should_extract=True,
                    candidate=None,
                    merged=False,
                )

            # 4. AttributionValidator: Xác thực owner / requester
            validated_candidate, attr_report = self.attribution_validator.validate_and_enforce(
                candidate, parsed
            )

            # 4a. Preserve channel/conversation domain context in description
            if raw_event.conversation_or_project_id:
                clean_ctx = raw_event.conversation_or_project_id.replace("conv-teams-", "").replace("conv-slack-", "").replace("_", " ")
                if validated_candidate.description:
                    if clean_ctx not in validated_candidate.description:
                        validated_candidate.description = f"{validated_candidate.description} [Context: {clean_ctx}]"
                else:
                    validated_candidate.description = f"[Context: {clean_ctx}]"

            if isinstance(raw_event.raw_payload, dict):
                src_title = raw_event.raw_payload.get("subject") or raw_event.raw_payload.get("title")
                if src_title and isinstance(src_title, str):
                    clean_title = src_title.replace("_", " ").strip()
                    if validated_candidate.description:
                        if clean_title not in validated_candidate.description:
                            validated_candidate.description = f"{validated_candidate.description} [Source: {clean_title}]"
                    else:
                        validated_candidate.description = f"[Source: {clean_title}]"

            # 4b. Explicit deadline parsing from payload if missing from candidate
            if not validated_candidate.due_date and isinstance(raw_event.raw_payload, dict):
                raw_due = None
                fields = raw_event.raw_payload.get("fields")
                if isinstance(fields, dict):
                    raw_due = fields.get("duedate")
                if not raw_due:
                    raw_due = raw_event.raw_payload.get("duedate") or raw_event.raw_payload.get("due_date")
                if raw_due:
                    try:
                        parsed_due = datetime.fromisoformat(str(raw_due).replace("Z", "+00:00"))
                        validated_candidate.due_date = parsed_due.astimezone(timezone.utc)
                        validated_candidate.explicit_deadline = True
                    except Exception as err:
                        logger.debug("Failed parsing explicit deadline: %s", err)

            # 4c. Infer TaskStatus from explicit text signals (IN_PROGRESS, DONE, BLOCKED)
            raw_text_lower = f"{raw_event.normalized_text or ''} {parsed.actual_content_text or ''} {validated_candidate.title or ''} {validated_candidate.description or ''}".lower()
            if any(kw in raw_text_lower for kw in ("đã hoàn thành", "đã xong", "đã merge", "đã deploy", "đã fix", "đã đóng", "completed", "resolved")):
                validated_candidate.status = TaskStatus.DONE
            elif any(kw in raw_text_lower for kw in ("bị block", "bị chặn", "on hold", "waiting on", "waiting for")):
                validated_candidate.status = TaskStatus.BLOCKED
            elif any(kw in raw_text_lower for kw in (
                "đang bắt tay", "đang làm", "đang xử lý", "đang fix", "đang check",
                "đang tiến hành", "đang điều tra", "đang triển khai", "đang review",
                "in progress", "wip", "working on", "investigating", "currently working"
            )):
                validated_candidate.status = TaskStatus.IN_PROGRESS

            # 5. IdentityResolver: Chuẩn hóa Person ID tuân thủ quy tắc bất biến
            # 5a. Chuẩn hóa Owner
            owner_name = validated_candidate.owner_name
            owner_ext_id = None
            if isinstance(raw_event.raw_payload, dict):
                # 1. Jira
                fields = raw_event.raw_payload.get("fields", {})
                assignee = fields.get("assignee") if isinstance(fields, dict) else None
                if isinstance(assignee, dict):
                    if not owner_name:
                        owner_name = assignee.get("displayName") or assignee.get("name")
                    owner_ext_id = assignee.get("accountId")
                # 2. Shortcut
                elif isinstance(raw_event.raw_payload.get("owner_ids"), list) and raw_event.raw_payload.get("owner_ids"):
                    owner_ext_id = raw_event.raw_payload.get("owner_ids")[0]
                    if not owner_name:
                        owner_name = owner_ext_id
                # 3. GitHub
                assignees = raw_event.raw_payload.get("assignees")
                if isinstance(assignees, list) and assignees and isinstance(assignees[0], dict):
                    gh_login = assignees[0].get("login")
                    if gh_login:
                        owner_ext_id = gh_login
                        if not owner_name:
                            owner_name = gh_login
                elif isinstance(raw_event.raw_payload.get("assignee"), dict):
                    gh_login = raw_event.raw_payload.get("assignee", {}).get("login")
                    if gh_login:
                        owner_ext_id = gh_login
                        if not owner_name:
                            owner_name = gh_login

            if not owner_name:
                owner_name = raw_event.author_display_name or raw_event.author_external_id

            if not owner_ext_id and owner_name:
                if (
                    (parsed.actual_author_raw and owner_name == parsed.actual_author_raw)
                    or (raw_event.author_display_name and owner_name == raw_event.author_display_name)
                    or (raw_event.author_external_id and owner_name == raw_event.author_external_id)
                ):
                    owner_ext_id = raw_event.author_external_id

            if owner_name or owner_ext_id:
                owner_res = await self._resolve_identity(
                    name=owner_name,
                    external_id=owner_ext_id,
                    tenant_id=raw_event.tenant_id,
                    source_type=source_type_str,
                )
                if owner_res.is_exact_match:
                    validated_candidate.owner_canonical_id = owner_res.person_id
                    if not validated_candidate.owner_name:
                        validated_candidate.owner_name = owner_res.canonical_name or owner_name
                else:
                    # Quy tắc bất biến: Không tự động merge Person ID nếu chỉ fuzzy name/alias
                    validated_candidate.owner_canonical_id = None

            # 5b. Chuẩn hóa Requester
            req_name = None
            req_ext_id = None

            # Subordinate commitment check ("em sẽ", "để em", "em làm", "vâng anh", etc.)
            raw_content_lower = f"{raw_event.normalized_text or ''} {parsed.actual_content_text or ''} {str(raw_event.raw_payload.get('content', '') if isinstance(raw_event.raw_payload, dict) else '')}".lower()
            is_subordinate_commitment = any(kw in raw_content_lower for kw in ("để em", "em sẽ", "em làm", "em xử lý", "vâng anh", "dạ anh", "để team em"))

            if parsed.is_quote_reply and parsed.quoted_author_raw:
                req_name = parsed.quoted_author_raw
                req_ext_id = parsed.quoted_author_raw
            elif is_subordinate_commitment:
                lead = self.identity_resolver.get_lead_person(tenant_id=raw_event.tenant_id)
                if lead:
                    req_name = lead.canonical_name
                    req_ext_id = lead.person_id
                    validated_candidate.requester_canonical_id = lead.person_id
                    validated_candidate.requester_name = lead.canonical_name
            else:
                req_name = validated_candidate.requester_name
                if isinstance(raw_event.raw_payload, dict):
                    # 1. Jira
                    fields = raw_event.raw_payload.get("fields", {})
                    reporter = fields.get("reporter") if isinstance(fields, dict) else None
                    if isinstance(reporter, dict) and reporter.get("accountId"):
                        req_ext_id = reporter.get("accountId")
                        if not req_name:
                            req_name = reporter.get("displayName") or reporter.get("name")
                    # 2. GitHub
                    if source_type_str == "github":
                        user_info = raw_event.raw_payload.get("user")
                        if isinstance(user_info, dict) and user_info.get("login"):
                            req_ext_id = user_info.get("login")
                            if not req_name:
                                req_name = req_ext_id
                        elif isinstance(user_info, str):
                            req_ext_id = user_info
                            if not req_name:
                                req_name = user_info

                if req_name and (req_name == owner_name or req_name == raw_event.author_display_name):
                    req_name = None
                    req_ext_id = None
                    validated_candidate.requester_canonical_id = None
                    validated_candidate.requester_name = None

            if not req_ext_id and req_name:
                if (
                    (parsed.actual_author_raw and req_name == parsed.actual_author_raw)
                    or (raw_event.author_display_name and req_name == raw_event.author_display_name)
                ):
                    req_ext_id = raw_event.author_external_id
                elif parsed.is_quote_reply:
                    payload_str = str(raw_event.raw_payload)
                    mention_match = re.search(r'class=["\']mention["\'][^>]*itemid=["\']([^"\']+)["\']', payload_str)
                    if mention_match:
                        req_ext_id = mention_match.group(1)

            if req_name or req_ext_id:
                req_res = await self._resolve_identity(
                    name=req_name,
                    external_id=req_ext_id,
                    tenant_id=raw_event.tenant_id,
                    source_type=source_type_str,
                )
                if req_res.is_exact_match:
                    validated_candidate.requester_canonical_id = req_res.person_id
                    if not validated_candidate.requester_name:
                        validated_candidate.requester_name = req_res.canonical_name or req_name
                else:
                    validated_candidate.requester_canonical_id = None
            elif not is_subordinate_commitment and not (parsed.is_quote_reply and parsed.quoted_author_raw):
                validated_candidate.requester_canonical_id = None
                validated_candidate.requester_name = None

            # 6. TaskCandidateMatcher & TaskMerger: Correlation và auto-merge nếu khớp
            existing_tasks = None
            if self.task_repo is not None:
                if hasattr(self.task_repo, "get_active_tasks"):
                    existing_tasks = await self.task_repo.get_active_tasks()
                elif hasattr(self.task_repo, "list_tasks"):
                    existing_tasks = await self.task_repo.list_tasks(
                        {"status": [TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, TaskStatus.DONE]}
                    )

            best_match = await self.candidate_matcher.find_best_match(
                validated_candidate, existing_tasks=existing_tasks
            )

            if best_match and best_match.score_result.should_merge:
                reason = getattr(best_match.score_result, "reason", "")
                logger.info(
                    "Candidate %s auto-merging into %s (confidence: %.2f, reason: %s)",
                    validated_candidate.id,
                    best_match.target_task.id,
                    best_match.score_result.correlation_confidence,
                    reason,
                )
                merged_task = await self.task_merger.merge(
                    target_task=best_match.target_task,
                    candidate=validated_candidate,
                    score_result=best_match.score_result,
                    persist=False,
                )
                # 7. Lưu vào TaskDomainRepository
                if self.task_repo and hasattr(self.task_repo, "upsert_task_atomic"):
                    await self.task_repo.upsert_task_atomic(merged_task)
                if (
                    self.task_repo
                    and hasattr(self.task_repo, "record_merge_audit")
                    and getattr(merged_task, "merge_audit", None)
                ):
                    await self.task_repo.record_merge_audit(merged_task.merge_audit)

                if self.intelligence_lifecycle:
                    lifecycle_res = await self.intelligence_lifecycle.on_task_changed(merged_task.id)
                    if (
                        lifecycle_res is not None
                        and hasattr(lifecycle_res, "task")
                        and isinstance(lifecycle_res.task, UnifiedTaskCandidate)
                    ):
                        merged_task = lifecycle_res.task

                return PipelineResult(
                    raw_event_id=raw_event.id,
                    status=ProcessingStatus.PROCESSED,
                    should_extract=True,
                    candidate=merged_task,
                    merged=True,
                    target_task_id=best_match.target_task.id,
                    task_id=merged_task.id,
                    attribution_report=attr_report,
                )
            else:
                logger.info(
                    "No matching task for candidate %s. Creating as new UnifiedTask.",
                    validated_candidate.id,
                )
                # 7. Lưu vào TaskDomainRepository
                if self.task_repo and hasattr(self.task_repo, "upsert_task_atomic"):
                    await self.task_repo.upsert_task_atomic(validated_candidate)

                if self.intelligence_lifecycle:
                    lifecycle_res = await self.intelligence_lifecycle.on_task_changed(validated_candidate.id)
                    if (
                        lifecycle_res is not None
                        and hasattr(lifecycle_res, "task")
                        and isinstance(lifecycle_res.task, UnifiedTaskCandidate)
                    ):
                        validated_candidate = lifecycle_res.task

                return PipelineResult(
                    raw_event_id=raw_event.id,
                    status=ProcessingStatus.PROCESSED,
                    should_extract=True,
                    candidate=validated_candidate,
                    merged=False,
                    target_task_id=None,
                    task_id=validated_candidate.id,
                    attribution_report=attr_report,
                )
        except Exception as exc:
            self.health_status = "DEGRADED"
            if not bug_logged:
                bug_code = (
                    BugCode.PTB_LLM_001
                    if isinstance(exc, LLMExtractionError)
                    else BugCode.PTB_L2_001
                )
                subsystem = "llm" if bug_code == BugCode.PTB_LLM_001 else "processing"
                log_bug(
                    bug_code,
                    subsystem=subsystem,
                    severity="ERROR",
                    message=f"Processing pipeline failure for RawEvent {raw_event.id}: {exc}",
                    exc=exc,
                    raw_event_id=raw_event.id,
                    source_type=source_type_str,
                    tenant_id=raw_event.tenant_id,
                )
            raise

    async def process_raw_event(self, raw_event: RawEventRecord) -> PipelineResult:
        """Alias chuẩn hóa cho process(raw_event)."""
        return await self.process(raw_event)
