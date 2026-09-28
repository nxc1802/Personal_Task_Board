"""Heuristic Turn Filter for Coding Agent Sessions.

Phân loại và phát hiện các turn hội thoại kỹ thuật có giá trị cao:
- Architectural Decisions (Quyết định kiến trúc)
- Bug Fixes & Lessons Learned (Bài học sửa lỗi)
- Task Commitments & TODOs (Cam kết công việc kỹ thuật)
"""

import re
from typing import Dict, List, Set

DECISION_PATTERNS = [
    r"(?i)\b(decid(ed|ing)?|quyết định|lựa chọn|chốt|chọn|architecture|kiến trúc)\b",
    r"(?i)\b(thay vì|instead of|replace|migration|chuyển sang)\b",
    r"(?i)\b(design rationale|lý do thiết kế|design decision|adr)\b",
    r"(?i)\b(neo4j|postgres|sqlite|single-store|dual-storage|openwebui|fastmcp|langgraph)\b",
]

BUG_FIX_PATTERNS = [
    r"(?i)\b(fix(ed|ing)?|sửa lỗi|khắc phục|resolve(d)?|xử lý lỗi)\b",
    r"(?i)\b(root cause|nguyên nhân gốc|nguyên nhân lỗi|lỗi do)\b",
    r"(?i)\b(lesson(s)? learned|bài học|kinh nghiệm|tránh lỗi)\b",
    r"(?i)\b(error|exception|fail(ed|ure)?|crash|overflow|timeout)\b",
]

TASK_PROMISE_PATTERNS = [
    r"(?i)\b(todo|cần làm|sẽ làm|cần triển khai|bước tiếp theo|next step(s)?)\b",
    r"(?i)\b(will implement|need to fix|will refactor|cần tối ưu|phải bổ sung)\b",
    r"(?i)\b(cam kết|hứa|promise|deadline|hạn chót)\b",
]


class TurnClassification:
    IS_DECISION: str = "decision"
    IS_BUG_FIX: str = "bug_fix"
    IS_TASK_PROMISE: str = "task_promise"


class AgentTurnFilter:
    """Bộ lọc phát hiện turn hội thoại kỹ thuật quan trọng."""

    @staticmethod
    def classify_turn(text: str) -> Set[str]:
        """Trả về tập hợp các nhãn phân loại thỏa mãn điều kiện."""
        if not text:
            return set()

        tags = set()
        for p in DECISION_PATTERNS:
            if re.search(p, text):
                tags.add(TurnClassification.IS_DECISION)
                break

        for p in BUG_FIX_PATTERNS:
            if re.search(p, text):
                tags.add(TurnClassification.IS_BUG_FIX)
                break

        for p in TASK_PROMISE_PATTERNS:
            if re.search(p, text):
                tags.add(TurnClassification.IS_TASK_PROMISE)
                break

        return tags

    @classmethod
    def is_valuable_turn(cls, text: str) -> bool:
        """Kiểm tra turn có chứa nội dung kỹ thuật đáng lưu trữ hay không."""
        return len(cls.classify_turn(text)) > 0
