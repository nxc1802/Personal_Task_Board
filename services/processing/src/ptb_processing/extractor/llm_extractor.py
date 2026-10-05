"""LLM Structured Extractor.

Trích xuất UnifiedTaskCandidate từ nội dung tin nhắn hoặc raw event:
- Tương thích OpenAI-compatible API (LLM_BASE_URL, LLM_API_KEY, LLM_MODEL).
- Trích xuất theo Pydantic schema v2 (title, description, owner_name, requester_name,
  due_date, explicit_deadline, extraction_confidence, evidence_snippet).
- Phân loại extraction_confidence:
    * >= 0.65 -> auto_approved
    * 0.40 - 0.64 -> pending_review
    * < 0.40 -> ignore
- Cung cấp mock extractor và fallback rule-based khi offline hoặc không có API key.
"""

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import re
from typing import Any, Callable, Dict, Optional, Union
import urllib.error
import urllib.request
from uuid import uuid4

from pydantic import BaseModel, Field

from ptb_contracts import BugCode, log_bug
from ptb_contracts.l1_acquisition import RawEventRecord
from ptb_contracts.l2_processing import (
    EvidenceRecord,
    EvidenceType,
    ParsedMessageContent,
    TaskStatus,
    UnifiedTaskCandidate,
)

logger = logging.getLogger("ptb.processing.extractor.llm_extractor")


class LLMExtractedSchema(BaseModel):
    """Schema Pydantic trung gian chứa thông tin trích xuất từ LLM."""

    title: Optional[str] = Field(default=None, description="Tiêu đề chuẩn hóa của task hoặc cam kết")
    description: Optional[str] = Field(default=None, description="Mô tả chi tiết bối cảnh")
    owner_name: Optional[str] = Field(default=None, description="Tên người chịu trách nhiệm thực hiện")
    requester_name: Optional[str] = Field(default=None, description="Tên người yêu cầu hoặc giao việc")
    due_date: Optional[datetime] = Field(default=None, description="Thời hạn hoàn thành (nếu có)")
    explicit_deadline: bool = Field(default=False, description="True nếu deadline được nêu rõ ràng")
    extraction_confidence: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="Độ tin cậy trích xuất: >=0.65 (auto), 0.4-0.64 (review), <0.4 (ignore)",
    )
    evidence_snippet: Optional[str] = Field(default="", description="Đoạn trích bằng chứng nguyên văn")


def classify_review_status(confidence: float) -> str:
    """Phân loại trạng thái review theo threshold quy định tại docs/v1.md.

    - >= 0.65: auto_approved
    - 0.40 - 0.64: pending_review
    - < 0.40: ignore
    """
    if confidence >= 0.65:
        return "auto_approved"
    elif confidence >= 0.40:
        return "pending_review"
    else:
        return "ignore"


SYSTEM_PROMPT = """Bạn là trợ lý AI chuyên trích xuất Task và Commitment từ tin nhắn công việc (Teams, Chat, Email).
Hãy phân tích nội dung được cung cấp và trích xuất thông tin task dưới định dạng JSON với các trường:
- "title": Tiêu đề ngắn gọn, rõ ràng của hành động cần làm.
- "description": Bối cảnh hoặc ghi chú thêm.
- "owner_name": Tên người sẽ thực hiện (nếu có).
- "requester_name": Tên người giao việc hoặc yêu cầu (nếu có).
- "due_date": Thời hạn hoàn thành dạng ISO 8601 (e.g. "2026-09-18T17:00:00Z"), hoặc null.
- "explicit_deadline": boolean (true nếu có giờ/ngày cụ thể).
- "extraction_confidence": float từ 0.0 đến 1.0 đánh giá mức độ chắc chắn đây là task/cam kết.
  (>=0.65 nếu có cam kết rõ ràng, 0.40-0.64 nếu nghi ngờ cần người duyệt, <0.40 nếu là tin rác/không phải task).
- "evidence_snippet": Câu hoặc đoạn trích ngắn chứng minh cam kết/yêu cầu này.

CHỈ TRẢ VỀ DUY NHẤT MỘT ĐỐI TƯỢNG JSON HỢP LỆ, KHÔNG KÈM TEXT GIẢI THÍCH."""


class LLMExtractionError(RuntimeError):
    """Lỗi phát sinh khi gọi LLM thất bại và cấu hình không cho phép heuristic fallback."""
    pass


class LLMStructuredExtractor:
    """Bộ trích xuất cấu trúc sử dụng LLM hoặc Fallback Rule-based / Mock."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        mock_mode: bool = False,
        timeout: float = 30.0,
        allow_heuristic_fallback: Optional[bool] = None,
    ) -> None:
        self.base_url = (
            base_url
            or os.getenv("OPENAI_BASE_URL")
            or os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
        ).rstrip("/")
        self.api_key = (
            api_key
            if api_key is not None
            else (os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY", ""))
        )
        self.model = (
            model
            or os.getenv("PTB_EXTRACTION_MODEL")
            or os.getenv("LLM_MODEL", "gpt-4o-mini")
        )
        self.mock_mode = mock_mode
        self.timeout = float(os.getenv("LLM_TIMEOUT", str(timeout)))
        if allow_heuristic_fallback is not None:
            self.allow_heuristic_fallback = allow_heuristic_fallback
        else:
            self.allow_heuristic_fallback = (
                os.getenv("PTB_ALLOW_HEURISTIC_FALLBACK", "false").lower()
                in ("true", "1", "yes")
            )
        self._mock_handler: Optional[Callable[[str], Optional[LLMExtractedSchema]]] = None

    def set_mock_handler(self, handler: Optional[Callable[[str], Optional[LLMExtractedSchema]]]) -> None:
        """Cấu hình hàm mock tùy biến cho unit test."""
        self._mock_handler = handler

    def _call_openai_completion(self, prompt: str) -> Dict[str, Any]:
        """Gọi OpenAI-compatible API qua HTTP POST bằng urllib với retry."""
        import time

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        # Normalize model name for OpenAI-compatible endpoint (strip models/ if present)
        model_name = self.model
        if model_name.startswith("models/"):
            model_name = model_name[len("models/"):]

        clean_prompt = prompt[:3500] if len(prompt) > 3500 else prompt
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": clean_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": int(os.getenv("LLM_MAX_TOKENS", "2048")),
        }

        req_data = json.dumps(payload).encode("utf-8")
        max_attempts = 3
        last_error = None

        for attempt in range(1, max_attempts + 1):
            req = urllib.request.Request(
                url,
                data=req_data,
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    res_body = response.read().decode("utf-8")
                    return json.loads(res_body)
            except urllib.error.HTTPError as e:
                last_error = e
                # Retry on 500, 502, 503, 504
                if e.code in (500, 502, 503, 504) and attempt < max_attempts:
                    logger.warning(
                        "LLM HTTP %d on attempt %d/%d. Retrying in %ds...",
                        e.code, attempt, max_attempts, attempt * 3
                    )
                    time.sleep(attempt * 3)
                    continue
                raise
            except (TimeoutError, urllib.error.URLError) as e:
                last_error = e
                if attempt < max_attempts:
                    logger.warning(
                        "LLM connection error '%s' on attempt %d/%d. Retrying in %ds...",
                        e, attempt, max_attempts, attempt * 3
                    )
                    time.sleep(attempt * 3)
                    continue
                raise

        raise last_error or RuntimeError("LLM request failed after retries")

    @staticmethod
    def _parse_json_from_llm_response(text: str) -> Dict[str, Any]:
        """Robust JSON extraction from LLM response.

        Handles responses that include:
        - Raw JSON
        - Markdown code fences (```json ... ```)
        - Thinking tags (<think>...</think>) before JSON
        - Extra text before/after JSON object
        """
        if not text or not text.strip():
            raise ValueError("Empty LLM response")

        cleaned = text.strip()

        # Remove <think>...</think> and <thought>...</thought> blocks (Qwen/Gemma thinking mode)
        cleaned = re.sub(r"<(think|thought)>.*?</\1>", "", cleaned, flags=re.DOTALL).strip()

        # Try direct JSON parse first
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # Try extracting from markdown code fence
        fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", cleaned, re.DOTALL)
        if fence_match:
            try:
                return json.loads(fence_match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # Try finding first { ... } block
        brace_match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", cleaned, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass

        raise ValueError(f"Could not extract valid JSON from LLM response: {cleaned[:200]}")


    def _fallback_rule_based_extract(
        self,
        text: str,
        quoted_author: Optional[str] = None,
        quoted_content: Optional[str] = None,
        actual_author: Optional[str] = None,
    ) -> LLMExtractedSchema:
        """Fallback trích xuất dựa trên quy tắc heuristic khi không có LLM API."""
        lower_text = text.lower()

        # Kiểm tra cam kết
        is_commitment = any(kw in lower_text for kw in ["để em", "em sẽ", "mình sẽ", "on it", "will do", "will fix", "đang check"])
        is_request = any(kw in lower_text for kw in ["anh check", "nhờ em", "cần làm", "pls fix", "please check", "can you check"])

        # Xác định owner & requester
        if is_commitment:
            owner = actual_author or "Assignee"
            requester = quoted_author
            confidence = 0.88 if quoted_content else 0.75
        elif is_request:
            owner = quoted_author
            requester = actual_author
            confidence = 0.70
        else:
            owner = actual_author
            requester = quoted_author
            confidence = 0.35  # Dưới 0.40 -> ignore

        # Tiêu đề
        if quoted_content:
            # Rút gọn quoted content làm tiêu đề
            clean_q = re.sub(r"^(?:can you|please|nhờ|nhờ anh|nhờ em)\s*", "", quoted_content, flags=re.IGNORECASE).strip()
            title = clean_q[:100]
        else:
            title = text.strip()[:100]

        if not title:
            title = "Task from message"

        # Deadline
        explicit_deadline = bool(re.search(r"(?:before|by|trước|hạn chót)\s+\d+", lower_text))

        return LLMExtractedSchema(
            title=title,
            description=f"Auto-extracted context: {text}" if text != title else None,
            owner_name=owner,
            requester_name=requester,
            due_date=None,
            explicit_deadline=explicit_deadline,
            extraction_confidence=confidence,
            evidence_snippet=text[:250],
        )

    def extract_from_parsed(
        self,
        parsed: ParsedMessageContent,
        raw_event_id: str = "raw-local",
        source_type: str = "ms_teams",
        external_url: Optional[str] = None,
        project_key: Optional[str] = None,
    ) -> UnifiedTaskCandidate:
        """Trích xuất UnifiedTaskCandidate từ ParsedMessageContent."""
        actual_text = parsed.actual_content_text or ""
        quoted_text = parsed.quoted_content_text or ""
        quoted_author = parsed.quoted_author_raw
        actual_author = parsed.actual_author_raw

        # Chuẩn bị context prompt
        context_parts = []
        if quoted_text:
            context_parts.append(f"Tin nhắn gốc (từ {quoted_author or 'Người hỏi'}): \"{quoted_text}\"")
        context_parts.append(f"Tin nhắn phản hồi (từ {actual_author or 'Người trả lời'}): \"{actual_text}\"")
        combined_prompt = "\n".join(context_parts)

        extracted_schema: Optional[LLMExtractedSchema] = None

        # 1. Nếu có mock handler được cấu hình
        if self._mock_handler:
            extracted_schema = self._mock_handler(combined_prompt)

        # 2. Nếu mock_mode hoặc cần trích xuất
        if extracted_schema is None:
            if self.mock_mode:
                extracted_schema = self._fallback_rule_based_extract(
                    text=actual_text or quoted_text,
                    quoted_author=quoted_author,
                    quoted_content=quoted_text,
                    actual_author=actual_author,
                )
            elif not self.api_key:
                if self.allow_heuristic_fallback:
                    logger.warning("No API key configured, falling back to rule-based heuristic")
                    extracted_schema = self._fallback_rule_based_extract(
                        text=actual_text or quoted_text,
                        quoted_author=quoted_author,
                        quoted_content=quoted_text,
                        actual_author=actual_author,
                    )
                else:
                    log_bug(
                        code=BugCode.PTB_LLM_001,
                        subsystem="llm",
                        severity="ERROR",
                        message="OPENAI_API_KEY is not configured and PTB_ALLOW_HEURISTIC_FALLBACK is False",
                    )
                    raise LLMExtractionError(
                        "OPENAI_API_KEY is not configured and PTB_ALLOW_HEURISTIC_FALLBACK is False"
                    )
            else:
                try:
                    res_json = self._call_openai_completion(combined_prompt)
                    msg_content = res_json["choices"][0]["message"]["content"]
                    data = self._parse_json_from_llm_response(msg_content)
                    extracted_schema = LLMExtractedSchema.model_validate(data)
                except Exception as e:
                    if self.allow_heuristic_fallback:
                        logger.warning("LLM API call failed, falling back to rule-based: %s", e)
                        extracted_schema = self._fallback_rule_based_extract(
                            text=actual_text or quoted_text,
                            quoted_author=quoted_author,
                            quoted_content=quoted_text,
                            actual_author=actual_author,
                        )
                    else:
                        logger.error(
                            "LLM API call failed and heuristic fallback is disabled: %s", e
                        )
                        log_bug(
                            code=BugCode.PTB_LLM_001,
                            subsystem="llm",
                            severity="ERROR",
                            message=f"LLM API call failed: {e}",
                            exc=e,
                        )
                        raise LLMExtractionError(f"LLM extraction failed: {e}") from e

        # 3. Phân loại review_status
        confidence = extracted_schema.extraction_confidence
        review_status = classify_review_status(confidence)
        if not extracted_schema.title or confidence < 0.40:
            review_status = "ignore"

        # 4. Tạo UnifiedTaskCandidate
        task_id = str(uuid4())
        ev_id = str(uuid4())
        evidence_snippet = extracted_schema.evidence_snippet or actual_text or quoted_text or ""

        evidence = EvidenceRecord(
            id=ev_id,
            task_id=task_id,
            raw_event_id=raw_event_id,
            evidence_type=EvidenceType.CHAT_COMMITMENT if parsed.is_quote_reply else EvidenceType.CHAT_REQUEST,
            source_type=source_type,
            external_url=external_url,
            author_canonical_name=actual_author or extracted_schema.owner_name,
            timestamp=datetime.now(timezone.utc),
            snippet=evidence_snippet,
            confidence=confidence,
            extraction_version="v1.0",
        )

        return UnifiedTaskCandidate(
            id=task_id,
            title=extracted_schema.title or "(Không phải task)",
            description=extracted_schema.description,
            status=TaskStatus.TODO,
            owner_name=extracted_schema.owner_name,
            requester_name=extracted_schema.requester_name,
            project_key=project_key,
            due_date=extracted_schema.due_date,
            explicit_deadline=extracted_schema.explicit_deadline,
            extraction_confidence=confidence,
            review_status=review_status,
            evidences=[evidence],
        )

    def extract_from_raw_event(
        self,
        raw_event: RawEventRecord,
        parsed: Optional[ParsedMessageContent] = None,
    ) -> UnifiedTaskCandidate:
        """Helper trích xuất trực tiếp từ RawEventRecord."""
        if parsed is None:
            # Tạo ParsedMessageContent đơn giản nếu chưa parse
            body_content = ""
            if isinstance(raw_event.raw_payload, dict):
                body = raw_event.raw_payload.get("body", {})
                if isinstance(body, dict):
                    body_content = body.get("content", "")
                else:
                    body_content = str(raw_event.raw_payload.get("content", ""))
            parsed = ParsedMessageContent(
                is_quote_reply=False,
                actual_content_text=body_content,
                actual_author_raw=raw_event.author_display_name or raw_event.author_external_id,
            )

        source_type_val = (
            raw_event.source_type.value
            if hasattr(raw_event.source_type, "value")
            else str(raw_event.source_type)
        )
        return self.extract_from_parsed(
            parsed=parsed,
            raw_event_id=raw_event.id,
            source_type=source_type_val,
            external_url=raw_event.deep_link,
        )

    async def extract_async(
        self,
        parsed: ParsedMessageContent,
        raw_event_id: str = "raw-local",
        source_type: str = "ms_teams",
        external_url: Optional[str] = None,
        project_key: Optional[str] = None,
    ) -> UnifiedTaskCandidate:
        """Async wrapper gọi extract_from_parsed."""
        return await asyncio.to_thread(
            self.extract_from_parsed,
            parsed,
            raw_event_id,
            source_type,
            external_url,
            project_key,
        )
