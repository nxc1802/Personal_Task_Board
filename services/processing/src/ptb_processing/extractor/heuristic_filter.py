"""Heuristic Candidate Filter.

Lọc trước (pre-filtering) tin nhắn dựa trên từ khóa và biểu thức chính quy (Regex)
tiếng Việt & tiếng Anh để phát hiện cam kết hoặc yêu cầu hành động,
giúp loại bỏ ~70% tin nhắn rác/chatter trước khi tốn chi phí gọi LLM.
"""

import re
import unicodedata
from typing import Any, Dict, List, Set


class HeuristicCandidateFilter:
    """Bộ lọc Heuristic xác định tin nhắn có tiềm năng là Task/Commitment."""

    # 1. Từ khóa/cụm từ cam kết tiếng Việt
    VI_COMMITMENT_KEYWORDS = [
        "để em",
        "để anh",
        "để mình",
        "để tớ",
        "để team em",
        "để bên em",
        "em sẽ",
        "anh sẽ",
        "mình sẽ",
        "tớ sẽ",
        "team sẽ",
        "em làm",
        "anh làm",
        "mình làm",
        "em xử lý",
        "anh xử lý",
        "mình xử lý",
        "đang check",
        "đang fix",
        "đang xử lý",
        "đang làm",
        "đang điều tra",
        "sẽ gửi",
        "sẽ check",
        "sẽ update",
        "sẽ hoàn thành",
        "sẽ fix",
        "sẽ xem",
        "sẽ báo lại",
        "gửi trước",
        "xong trước",
        "hoàn thành trước",
        "em nhận",
        "mình nhận",
        "em lo",
        "mình lo",
        "nhận task",
        "chốt task",
        "hứa sẽ",
        "cam kết",
    ]

    # 2. Từ khóa/cụm từ yêu cầu hành động tiếng Việt
    VI_ACTION_REQUEST_KEYWORDS = [
        "anh check",
        "em check",
        "bác check",
        "check giúp",
        "check giùm",
        "check hộ",
        "nhờ em",
        "nhờ anh",
        "nhờ bác",
        "nhờ team",
        "cần làm",
        "cần fix",
        "cần xử lý",
        "cần gửi",
        "cần xong",
        "cần kiểm tra",
        "fix giúp",
        "xử lý giúp",
        "làm giúp",
        "hỗ trợ giúp",
        "vui lòng",
        "xin vui lòng",
        "hãy làm",
        "hạn chót",
        "deadline",
        "gấp",
        "khẩn cấp",
        "sự cố",
    ]

    # 3. Từ khóa cam kết tiếng Anh
    EN_COMMITMENT_KEYWORDS = [
        "on it",
        "i'm on it",
        "im on it",
        "i am on it",
        "will do",
        "will fix",
        "will check",
        "will handle",
        "will update",
        "will send",
        "will investigate",
        "i will",
        "i'll",
        "we will",
        "we'll",
        "i can do",
        "i can fix",
        "i can check",
        "i'll take care",
        "taking care of",
        "looking into",
        "working on it",
        "handling it",
    ]

    # 4. Từ khóa yêu cầu hành động tiếng Anh
    EN_ACTION_REQUEST_KEYWORDS = [
        "pls fix",
        "please fix",
        "pls check",
        "please check",
        "can you check",
        "could you please",
        "action required",
        "need to",
        "needs to be",
        "must be done",
        "should be done",
        "please do",
        "kindly check",
        "take a look",
        "can you handle",
        "assigned to",
        "task for",
        "urgent",
        "asap",
        "by eod",
        "by cob",
        "before 5pm",
    ]

    # 5. Regex patterns phát hiện deadline, tickets, bugs
    REGEX_PATTERNS = [
        # Deadline tiếng Anh & tiếng Việt: "before 5pm", "by tomorrow", "trước 17h", "hạn chót 15/10"
        re.compile(
            r"(?:before|by|trước|hạn chót|deadline:?)\s*(?:\d{1,2}(?::\d{2})?\s*(?:am|pm|h)?|\d{1,2}[/-]\d{1,2}|eod|cob|tomorrow|mai|chiều)",
            re.IGNORECASE,
        ),
        # Ticket ID patterns: e.g. OPS-88, PTB-102, JIRA-1234
        re.compile(r"\b[A-Z]{2,10}-\d+\b"),
        # Git commit / PR references: e.g. PR #12, commit abc123
        re.compile(r"\b(?:PR\s*#?\d+|commit\s+[a-f0-9]{7,40})\b", re.IGNORECASE),
        # Error / crash signals with action potential: "deployment failed", "server down", "lỗi 500"
        re.compile(r"\b(?:deploy(?:ment)?\s+failed|server\s+down|crash(?:ed)?|lỗi\s+\d{3})\b", re.IGNORECASE),
    ]

    # 6. Các từ/cụm từ thuần túy giao tiếp xã giao (chatter/noise)
    NOISE_PATTERNS = [
        re.compile(
            r"^(?:chào(?:\s+cả\s+nhà|\s+buổi\s+sáng|\s+anh|\s+em)?|hello|hi|good\s+morning|good\s+afternoon|good\s+night)[!.\s]*$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:cảm\s+ơn(?:\s+nhiều)?|thanks|thank\s+you|thx|ty|kaka|haha|hehe|hihi|ok|oke|okie|yes|yep|agree|\+1)[!.\s]*$",
            re.IGNORECASE,
        ),
    ]

    def _normalize_text(self, text: str) -> str:
        """Chuẩn hóa chuỗi văn bản để quét từ khóa."""
        if not text:
            return ""
        norm = text.lower()
        # Chuẩn hóa khoảng trắng
        norm = re.sub(r"\s+", " ", norm).strip()
        return norm

    def should_extract(self, text: str) -> bool:
        """Xác định xem một chuỗi nội dung có tiềm năng chứa task/cam kết hay không.

        Returns:
            bool: True nếu tìm thấy dấu hiệu cam kết, yêu cầu hành động, hoặc deadline/ticket.
                  False nếu chỉ là tin nhắn xã giao hoặc không có hành động.
        """
        if not text or len(text.strip()) < 3:
            return False

        norm_text = self._normalize_text(text)

        # 1. Kiểm tra nếu tin nhắn là thuần túy noise/chatter
        for noise_pat in self.NOISE_PATTERNS:
            if noise_pat.match(norm_text):
                return False

        # 2. Quét từ khóa cam kết
        for kw in self.VI_COMMITMENT_KEYWORDS:
            if kw in norm_text:
                return True

        for kw in self.EN_COMMITMENT_KEYWORDS:
            # Dùng regex boundary cho từ khóa ngắn tiếng Anh như "on it"
            if len(kw) <= 5:
                if re.search(r"\b" + re.escape(kw) + r"\b", norm_text):
                    return True
            elif kw in norm_text:
                return True

        # 3. Quét từ khóa yêu cầu hành động
        for kw in self.VI_ACTION_REQUEST_KEYWORDS:
            if kw in norm_text:
                return True

        for kw in self.EN_ACTION_REQUEST_KEYWORDS:
            if len(kw) <= 5:
                if re.search(r"\b" + re.escape(kw) + r"\b", norm_text):
                    return True
            elif kw in norm_text:
                return True

        # 4. Quét regex patterns (deadline, ticket id, etc.)
        for pat in self.REGEX_PATTERNS:
            if pat.search(text):
                return True

        return False

    def analyze(self, text: str) -> Dict[str, Any]:
        """Phân tích chi tiết các pattern và từ khóa khớp với nội dung."""
        if not text:
            return {
                "should_extract": False,
                "matched_keywords": [],
                "matched_patterns": [],
                "confidence_hint": 0.0,
            }

        norm_text = self._normalize_text(text)
        matched_kw: Set[str] = set()
        matched_pats: List[str] = []

        all_keywords = (
            self.VI_COMMITMENT_KEYWORDS
            + self.VI_ACTION_REQUEST_KEYWORDS
            + self.EN_COMMITMENT_KEYWORDS
            + self.EN_ACTION_REQUEST_KEYWORDS
        )

        for kw in all_keywords:
            if len(kw) <= 5:
                if re.search(r"\b" + re.escape(kw) + r"\b", norm_text):
                    matched_kw.add(kw)
            elif kw in norm_text:
                matched_kw.add(kw)

        for pat in self.REGEX_PATTERNS:
            match = pat.search(text)
            if match:
                matched_pats.append(match.group(0))

        should = self.should_extract(text)
        confidence_hint = 0.85 if (matched_kw and matched_pats) else (0.70 if matched_kw else (0.50 if matched_pats else 0.20))

        return {
            "should_extract": should,
            "matched_keywords": sorted(list(matched_kw)),
            "matched_patterns": matched_pats,
            "confidence_hint": confidence_hint if should else 0.10,
        }
