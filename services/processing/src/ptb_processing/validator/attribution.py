"""Attribution Validator.

Xác thực và chuẩn hóa quyền sở hữu (owner) và người yêu cầu (requester) của Task/Commitment:
Quy tắc:
- Nếu người A hỏi/yêu cầu (quoted_author) và người B trả lời "để em làm" / cam kết (actual_author)
  -> owner BẮT BUỘC là người B (actual_author), requester là người A (quoted_author).
- Tự động phát hiện và sửa sai nếu extractor gán nhầm owner/requester.
"""

from dataclasses import dataclass
import logging
import re
from typing import Optional, Tuple

from pydantic import BaseModel, Field

from ptb_contracts.l2_processing import ParsedMessageContent, UnifiedTaskCandidate

logger = logging.getLogger("ptb.processing.validator.attribution")


class AttributionReport(BaseModel):
    """Báo cáo kết quả kiểm tra và điều chỉnh attribution."""

    is_valid: bool = Field(description="True nếu candidate ban đầu đã có attribution chính xác")
    corrected: bool = Field(description="True nếu validator đã phải điều chỉnh lại owner/requester")
    original_owner: Optional[str] = None
    final_owner: Optional[str] = None
    original_requester: Optional[str] = None
    final_requester: Optional[str] = None
    reason: str = Field(description="Lý giải quy tắc attribution được áp dụng")


class AttributionValidator:
    """Bộ kiểm tra và xác thực quan hệ trách nhiệm (Owner vs Requester)."""

    # Các mẫu biểu thị cam kết nhận việc của tác giả hiện tại (First-person commitments)
    FIRST_PERSON_COMMITMENT_PATTERNS = [
        re.compile(r"\b(?:để\s+(?:em|anh|mình|tớ|bên\s+em|team\s+em))\b", re.IGNORECASE),
        re.compile(r"\b(?:(?:em|anh|mình|tớ)\s+(?:sẽ|đang|nhận|lo|xử\s+lý|làm|check|fix))\b", re.IGNORECASE),
        re.compile(r"\b(?:on\s+it|i['’]?m\s+on\s+it|will\s+do|i['’]?ll\s+(?:do|fix|check|handle|take\s+care))\b", re.IGNORECASE),
        re.compile(r"\b(?:working\s+on\s+it|looking\s+into\s+it|leave\s+it\s+to\s+me)\b", re.IGNORECASE),
    ]

    # Các mẫu biểu thị yêu cầu hoặc ủy thác (Delegation / Requesting others)
    DELEGATION_PATTERNS = [
        re.compile(r"\b(?:nhờ\s+(?:anh|em|bác|team|mọi\s+người))\b", re.IGNORECASE),
        re.compile(r"\b(?:(?:anh|em|bác)\s+(?:check|fix|làm|xử\s+lý|gửi)\s+giúp)\b", re.IGNORECASE),
        re.compile(r"\b(?:pls\s+fix|please\s+fix|can\s+you\s+(?:check|fix|handle))\b", re.IGNORECASE),
    ]

    def is_first_person_commitment(self, text: Optional[str]) -> bool:
        """Kiểm tra xem nội dung văn bản có phải là phát biểu cam kết nhận việc của người nói không."""
        if not text:
            return False
        for pattern in self.FIRST_PERSON_COMMITMENT_PATTERNS:
            if pattern.search(text):
                return True
        return False

    def is_delegation(self, text: Optional[str]) -> bool:
        """Kiểm tra xem nội dung văn bản có phải là phát biểu giao việc/yêu cầu người khác không."""
        if not text:
            return False
        for pattern in self.DELEGATION_PATTERNS:
            if pattern.search(text):
                return True
        return False

    def validate_and_enforce(
        self,
        candidate: UnifiedTaskCandidate,
        parsed_message: ParsedMessageContent,
    ) -> Tuple[UnifiedTaskCandidate, AttributionReport]:
        """Xác thực và ép buộc quy tắc gán quyền sở hữu chính xác:

        1. Nếu là quote reply:
           - actual_content thể hiện cam kết ("để em làm", "on it"):
             => Owner = actual_author, Requester = quoted_author.
           - actual_content thể hiện ủy thác tiếp ("nhờ anh check tiếp"):
             => Owner = quoted_author (hoặc người được giao), Requester = actual_author.
        2. Nếu là standalone message:
           - Thể hiện cam kết ("Để em fix bug"):
             => Owner = actual_author.

        Returns:
            Tuple[UnifiedTaskCandidate, AttributionReport]: Candidate đã được cập nhật và bản báo cáo audit.
        """
        orig_owner = candidate.owner_name
        orig_requester = candidate.requester_name

        actual_author = parsed_message.actual_author_raw
        quoted_author = parsed_message.quoted_author_raw
        actual_text = parsed_message.actual_content_text or ""

        target_owner = orig_owner
        target_requester = orig_requester
        reason = "Attribution unchanged; no override required."

        if parsed_message.is_quote_reply:
            if self.is_first_person_commitment(actual_text):
                # Người B trả lời người A bằng cam kết "để em làm"
                target_owner = actual_author or orig_owner
                target_requester = quoted_author or orig_requester
                reason = (
                    f"Quote-reply commitment detected ('{actual_text[:50]}...'): "
                    f"Owner enforced to actual_author ({target_owner}), "
                    f"Requester enforced to quoted_author ({target_requester})."
                )
            elif self.is_delegation(actual_text):
                # Người B yêu cầu người A hoặc người khác làm
                target_owner = quoted_author or orig_owner
                target_requester = actual_author or orig_requester
                reason = (
                    f"Quote-reply delegation detected ('{actual_text[:50]}...'): "
                    f"Owner enforced to target ({target_owner}), "
                    f"Requester enforced to actual_author ({target_requester})."
                )
        else:
            # Không phải quote reply
            if self.is_first_person_commitment(actual_text) and actual_author:
                target_owner = actual_author
                reason = f"Standalone commitment: Owner set to actual_author ({actual_author})."

        corrected = (target_owner != orig_owner) or (target_requester != orig_requester)
        is_valid = not corrected

        # Áp dụng giá trị mới lên candidate
        candidate.owner_name = target_owner
        candidate.requester_name = target_requester

        report = AttributionReport(
            is_valid=is_valid,
            corrected=corrected,
            original_owner=orig_owner,
            final_owner=target_owner,
            original_requester=orig_requester,
            final_requester=target_requester,
            reason=reason,
        )

        return candidate, report

    def validate(
        self,
        candidate: UnifiedTaskCandidate,
        parsed_message: ParsedMessageContent,
    ) -> UnifiedTaskCandidate:
        """Helper gọi validate_and_enforce và trả về trực tiếp candidate đã chuẩn hóa."""
        updated_candidate, _ = self.validate_and_enforce(candidate, parsed_message)
        return updated_candidate
