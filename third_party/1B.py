# -*- coding: utf-8 -*-
"""
IDE-Native Coding Agent — Training Data Pipeline (.py)
======================================================
Trích xuất, làm sạch và chuẩn hóa dữ liệu hội thoại từ các AI Coding IDE:
  - Google Antigravity IDE
  - Cursor IDE
  - OpenAI Codex CLI
Sang định dạng chuẩn OpenAI Tool-Calling SFT (.jsonl) phục vụ fine-tuning.

Nguyên tắc xử lý:
  - GIỮ NGUYÊN: Đường dẫn gốc và toàn bộ nội dung tool output.
  - GIỮ NGUYÊN: Ngữ cảnh IDE (<USER_REQUEST>, <environment_context>, <editor_state>).
  - BỎ: Các tag nhiễu hệ thống (<USER_SETTINGS_CHANGE>, <CONVERSATION_HISTORY>), secrets/API keys.
  - ĐẦY ĐỦ: Khớp chính xác chu trình User -> Assistant (kèm tool_calls) -> Tool Output -> Artifacts & Metadata.
"""

import os
import sys
import json
import re
import argparse
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime

# UTF-8 trên Windows Console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ==============================================================================
# 0. CẤU HÌNH & HẰNG SỐ (Configuration)
# ==============================================================================

DEFAULT_PATHS = {
    "antigravity": os.path.expandvars(r"%USERPROFILE%\.gemini\antigravity-ide"),
    "cursor":      os.path.expandvars(r"%USERPROFILE%\.cursor\projects"),
    "codex":       os.path.expandvars(r"%USERPROFILE%\.codex"),
}

SELECTED_IDES = ["antigravity", "cursor", "codex"]

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__)) if "__file__" in locals() else os.getcwd()
DEFAULT_OUTPUT_FILE = os.path.join(CURRENT_DIR, "agent_sft_dataset.jsonl")

DEFAULT_SYSTEM_PROMPT = (
    "You are an autonomous AI coding agent integrated into the user's IDE. "
    "You can observe the editor state (active file, cursor position, open tabs), "
    "reason step-by-step inside <thought> blocks, and execute tools "
    "(view_file, replace_file_content, run_command, list_dir, grep_search, etc.) "
    "to assist the developer. You always verify your changes by reading tool outputs "
    "and self-correct when encountering errors."
)


# ==============================================================================
# 1. BỘ LÀM SẠCH DỮ LIỆU (DataCleaner)
# ==============================================================================

class DataCleaner:
    """Bộ tiền xử lý dữ liệu hội thoại — bảo tồn ngữ cảnh IDE, loại bỏ nhiễu và secrets."""

    _REMOVE_TAG_PATTERNS = [
        re.compile(r"<USER_SETTINGS_CHANGE>[\s\S]*?</USER_SETTINGS_CHANGE>"),
        re.compile(r"<CONVERSATION_HISTORY>[\s\S]*?</CONVERSATION_HISTORY>"),
    ]

    @classmethod
    def clean_ide_context(cls, text: str) -> str:
        """Làm sạch nội dung lượt người dùng, giữ nguyên ngữ cảnh IDE."""
        if not text:
            return ""

        # Loại bỏ các tag nhiễu bắt buộc
        for pattern in cls._REMOVE_TAG_PATTERNS:
            text = pattern.sub("", text)

        # Dọn khoảng trắng thừa
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def anonymize_paths(cls, text: str) -> str:
        """Giữ nguyên đường dẫn gốc (theo nguyên tắc không thay thế hay ẩn danh hóa)."""
        return text

    @classmethod
    def truncate_tool_output(cls, content: str, max_chars: Optional[int] = None) -> str:
        """Giữ nguyên toàn bộ tool output (không cắt bớt)."""
        if max_chars and content and len(content) > max_chars:
            return content[:max_chars] + "\n... [TRUNCATED]"
        return content if content else ""

    @classmethod
    def detect_secrets(cls, text: str) -> str:
        """Phát hiện và che giấu các token/key/secret tiềm ẩn."""
        if not text:
            return ""
        text = re.sub(
            r"(sk-[a-zA-Z0-9]{20,}|AKIA[A-Z0-9]{16}|AIza[a-zA-Z0-9_-]{35})",
            "[REDACTED_API_KEY]",
            text
        )
        return text


# ==============================================================================
# 2. LỚP CƠ SỞ ADAPTER (BaseIDEExtractor)
# ==============================================================================

class BaseIDEExtractor(ABC):
    """Lớp cơ sở trừu tượng cho tất cả các IDE Extractor."""

    def __init__(self, base_path: str, ide_name: str):
        self.base_path = base_path
        self.ide_name = ide_name

    @abstractmethod
    def is_available(self) -> bool:
        """Kiểm tra xem IDE có dữ liệu hội thoại trên máy hay không."""
        pass

    @abstractmethod
    def extract_sessions(self) -> List[Dict[str, Any]]:
        """Trích xuất toàn bộ sessions và trả về danh sách dict chuẩn hóa."""
        pass

    def _read_text_file(self, path: str) -> Optional[str]:
        """Đọc file text an toàn với fallback encoding."""
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        except Exception:
            return None

    def _read_jsonl(self, path: str) -> List[dict]:
        """Đọc file JSONL, bỏ qua dòng lỗi."""
        entries = []
        if not os.path.isfile(path):
            return entries
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return entries

    def _collect_artifacts_from_dir(self, dir_path: str,
                                     extensions: Tuple[str, ...] = (
                                         ".md", ".py", ".js", ".ts", ".html",
                                         ".css", ".json", ".txt", ".yaml", ".yml",
                                         ".sh", ".bat", ".ps1",
                                     )) -> Dict[str, str]:
        """Thu thập nội dung các file artifact trong thư mục."""
        artifacts = {}
        if not os.path.isdir(dir_path):
            return artifacts

        for item in os.listdir(dir_path):
            full_path = os.path.join(dir_path, item)
            if not os.path.isfile(full_path):
                continue
            if item.startswith("."):
                continue
            _, ext = os.path.splitext(item)
            if ext.lower() in extensions:
                content = self._read_text_file(full_path)
                if content:
                    artifacts[item] = content
        return artifacts


# ==============================================================================
# 3. ANTIGRAVITY EXTRACTOR (Đã sửa lỗi nhận diện Tool Output)
# ==============================================================================

class AntigravityExtractor(BaseIDEExtractor):
    """Trích xuất hội thoại từ Google Antigravity IDE."""

    # Tập hợp các step_type đại diện cho kết quả thực thi tool trong Antigravity
    TOOL_STEP_TYPES = {
        "LIST_DIRECTORY", "VIEW_FILE", "SEARCH_WEB", "RUN_COMMAND",
        "GREP_SEARCH", "CODE_ACTION", "READ_URL_CONTENT", "BROWSER_SUBAGENT",
        "GENERATE_IMAGE", "ASK_QUESTION", "TOOL_OUTPUT", "ERROR_MESSAGE", "GENERIC"
    }

    # Bảng ánh xạ từ step_type sang tên tool chuẩn
    TOOL_NAME_MAP = {
        "LIST_DIRECTORY": "list_dir",
        "VIEW_FILE": "view_file",
        "SEARCH_WEB": "search_web",
        "RUN_COMMAND": "run_command",
        "GREP_SEARCH": "grep_search",
        "CODE_ACTION": "code_action",
        "READ_URL_CONTENT": "read_url_content",
        "BROWSER_SUBAGENT": "browser_subagent",
        "GENERATE_IMAGE": "generate_image",
        "ASK_QUESTION": "ask_question",
        "ERROR_MESSAGE": "error",
    }

    def __init__(self, base_path: str):
        super().__init__(base_path, "antigravity")
        self.brain_dir = os.path.join(base_path, "brain")

    def is_available(self) -> bool:
        return os.path.isdir(self.brain_dir)

    def extract_sessions(self) -> List[Dict[str, Any]]:
        results = []

        for sid in os.listdir(self.brain_dir):
            session_dir = os.path.join(self.brain_dir, sid)
            if not os.path.isdir(session_dir):
                continue

            # Tìm file transcript (ưu tiên transcript_full.jsonl)
            log_dir = os.path.join(session_dir, ".system_generated", "logs")
            transcript_path = os.path.join(log_dir, "transcript_full.jsonl")
            if not os.path.isfile(transcript_path):
                transcript_path = os.path.join(log_dir, "transcript.jsonl")
            if not os.path.isfile(transcript_path):
                continue

            # Thu thập artifacts (.md, script trong scratch/)
            artifacts = self._collect_artifacts_from_dir(session_dir)
            scratch_dir = os.path.join(session_dir, "scratch")
            if os.path.isdir(scratch_dir):
                scratch_arts = self._collect_artifacts_from_dir(scratch_dir)
                for name, content in scratch_arts.items():
                    artifacts[f"scratch/{name}"] = content

            # Phân tích transcript thành chuỗi messages chuẩn OpenAI
            messages, tool_calls_count = self._parse_antigravity_transcript(transcript_path)

            if len(messages) <= 1:
                continue

            results.append({
                "session_id": sid,
                "ide": self.ide_name,
                "messages": messages,
                "artifacts": artifacts,
                "stats": {
                    "total_turns": len(messages),
                    "tool_calls_count": tool_calls_count,
                    "artifacts_count": len(artifacts),
                }
            })

        return results

    def _parse_antigravity_transcript(self, path: str) -> Tuple[List[Dict], int]:
        messages = [{"role": "system", "content": DEFAULT_SYSTEM_PROMPT}]
        tool_calls_count = 0
        call_counter = 0

        steps = self._read_jsonl(path)
        pending_tool_calls: List[Dict[str, str]] = []  # [{"id": call_id, "name": tool_name}]

        for step in steps:
            step_type = step.get("type", "")
            source = step.get("source", "")
            content = step.get("content", "")
            step_idx = step.get("step_index", 0)

            # 1. USER TURN
            if step_type == "USER_INPUT":
                # Đóng bất kỳ pending calls nào trước đó nếu có
                self._flush_pending_calls(messages, pending_tool_calls)

                cleaned = DataCleaner.clean_ide_context(content)
                cleaned = DataCleaner.detect_secrets(cleaned)
                if cleaned:
                    messages.append({
                        "role": "user",
                        "content": cleaned,
                    })

            # 2. ASSISTANT TURN (PLANNER_RESPONSE)
            elif step_type == "PLANNER_RESPONSE":
                # Đóng bất kỳ pending calls chưa có kết quả từ turn trước
                self._flush_pending_calls(messages, pending_tool_calls)

                assistant_msg: Dict[str, Any] = {"role": "assistant"}

                # Ghép thinking (CoT) + content
                text_parts = []
                thinking = step.get("thinking", "")
                if thinking:
                    text_parts.append(f"<thought>\n{thinking.strip()}\n</thought>")
                if content:
                    text_parts.append(content.strip())

                combined_text = "\n\n".join(text_parts) if text_parts else None
                if combined_text:
                    combined_text = DataCleaner.anonymize_paths(combined_text)
                    combined_text = DataCleaner.detect_secrets(combined_text)
                assistant_msg["content"] = combined_text

                # Chuẩn hóa tool_calls sang schema OpenAI
                raw_calls = step.get("tool_calls", [])
                if raw_calls:
                    formatted_calls = []
                    for tc in raw_calls:
                        call_counter += 1
                        call_id = tc.get("id") or f"call_{step_idx}_{call_counter}"
                        tool_name = tc.get("name", "unknown_tool")

                        args = tc.get("args", {})
                        if isinstance(args, str):
                            args_str = args
                        else:
                            args_str = json.dumps(args, ensure_ascii=False)
                        args_str = DataCleaner.anonymize_paths(args_str)

                        formatted_calls.append({
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": args_str,
                            }
                        })
                        pending_tool_calls.append({"id": call_id, "name": tool_name})

                    assistant_msg["tool_calls"] = formatted_calls
                    tool_calls_count += len(formatted_calls)

                if assistant_msg.get("content") or assistant_msg.get("tool_calls"):
                    messages.append(assistant_msg)

            # 3. TOOL OUTPUT TURN (VIEW_FILE, LIST_DIRECTORY, SEARCH_WEB, ERROR_MESSAGE, v.v.)
            elif step_type in self.TOOL_STEP_TYPES or (source == "MODEL" and content and pending_tool_calls):
                tool_content = DataCleaner.truncate_tool_output(str(content) if content is not None else "")
                tool_content = DataCleaner.anonymize_paths(tool_content)
                tool_content = DataCleaner.detect_secrets(tool_content)

                if pending_tool_calls:
                    matched_call = pending_tool_calls.pop(0)
                    call_id = matched_call["id"]
                    tool_name = matched_call["name"]
                else:
                    call_id = f"call_{step_idx}"
                    tool_name = self.TOOL_NAME_MAP.get(step_type, step_type.lower())

                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": tool_name,
                    "content": tool_content,
                })

        # Cuối session: giải quyết nốt pending calls nếu còn sót
        self._flush_pending_calls(messages, pending_tool_calls)

        return messages, tool_calls_count

    @staticmethod
    def _flush_pending_calls(messages: List[Dict], pending_calls: List[Dict[str, str]]):
        """Bổ sung tool message trống nếu model có gọi tool nhưng session dừng đột ngột (đáp ứng chuẩn OpenAI)."""
        while pending_calls:
            call = pending_calls.pop(0)
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "name": call["name"],
                "content": "[Action completed or execution ended]",
            })


# ==============================================================================
# 4. CURSOR EXTRACTOR
# ==============================================================================

class CursorExtractor(BaseIDEExtractor):
    """Trích xuất hội thoại từ Cursor IDE."""

    def __init__(self, base_path: str):
        super().__init__(base_path, "cursor")

    def is_available(self) -> bool:
        return os.path.isdir(self.base_path)

    def extract_sessions(self) -> List[Dict[str, Any]]:
        results = []

        for workspace_name in os.listdir(self.base_path):
            ws_dir = os.path.join(self.base_path, workspace_name)
            if not os.path.isdir(ws_dir):
                continue

            transcripts_dir = os.path.join(ws_dir, "agent-transcripts")
            if not os.path.isdir(transcripts_dir):
                continue

            canvases_dir = os.path.join(ws_dir, "canvases")
            ws_artifacts = {}
            if os.path.isdir(canvases_dir):
                ws_artifacts = self._collect_artifacts_from_dir(canvases_dir)

            for sid in os.listdir(transcripts_dir):
                sid_dir = os.path.join(transcripts_dir, sid)
                transcript_path = os.path.join(sid_dir, f"{sid}.jsonl")
                if not os.path.isfile(transcript_path):
                    continue

                messages, tool_calls_count = self._parse_cursor_transcript(transcript_path)

                if len(messages) <= 1:
                    continue

                results.append({
                    "session_id": sid,
                    "ide": self.ide_name,
                    "workspace": workspace_name,
                    "messages": messages,
                    "artifacts": ws_artifacts,
                    "stats": {
                        "total_turns": len(messages),
                        "tool_calls_count": tool_calls_count,
                        "artifacts_count": len(ws_artifacts),
                    }
                })

        return results

    def _parse_cursor_transcript(self, path: str) -> Tuple[List[Dict], int]:
        messages = [{"role": "system", "content": DEFAULT_SYSTEM_PROMPT}]
        tool_calls_count = 0
        call_counter = 0

        entries = self._read_jsonl(path)
        pending_tool_calls: List[Dict[str, str]] = []

        for entry in entries:
            role = entry.get("role", "")
            msg_data = entry.get("message", {})
            content_blocks = msg_data.get("content", [])

            # USER TURN
            if role == "user":
                self._flush_pending_calls(messages, pending_tool_calls)
                combined_text = ""
                for block in content_blocks:
                    if isinstance(block, dict) and block.get("type") == "text":
                        combined_text += block.get("text", "") + "\n"
                    elif isinstance(block, str):
                        combined_text += block + "\n"

                cleaned = DataCleaner.clean_ide_context(combined_text)
                cleaned = DataCleaner.detect_secrets(cleaned)
                if cleaned:
                    messages.append({"role": "user", "content": cleaned})

            # ASSISTANT TURN
            elif role == "assistant":
                self._flush_pending_calls(messages, pending_tool_calls)
                assistant_msg: Dict[str, Any] = {"role": "assistant"}
                text_parts = []
                tool_calls = []

                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")

                    if btype == "text":
                        t = block.get("text", "").strip()
                        if t:
                            text_parts.append(t)

                    elif btype == "tool_use":
                        call_counter += 1
                        call_id = block.get("id") or f"call_cursor_{call_counter}"
                        tool_name = block.get("name", "unknown_tool")
                        args = block.get("input", {})
                        args_str = json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args
                        args_str = DataCleaner.anonymize_paths(args_str)

                        tool_calls.append({
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": args_str,
                            }
                        })
                        pending_tool_calls.append({"id": call_id, "name": tool_name})

                combined = "\n\n".join(text_parts) if text_parts else None
                if combined:
                    combined = DataCleaner.anonymize_paths(combined)
                    combined = DataCleaner.detect_secrets(combined)
                assistant_msg["content"] = combined

                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                    tool_calls_count += len(tool_calls)

                if assistant_msg.get("content") or assistant_msg.get("tool_calls"):
                    messages.append(assistant_msg)

            # TOOL RESULT TURN
            elif role in ("tool", "tool_result") or entry.get("type") == "tool_result":
                tool_content = ""
                if isinstance(content_blocks, list):
                    for block in content_blocks:
                        if isinstance(block, dict) and block.get("type") == "text":
                            tool_content += block.get("text", "") + "\n"
                        elif isinstance(block, str):
                            tool_content += block + "\n"
                elif isinstance(content_blocks, str):
                    tool_content = content_blocks

                tool_content = DataCleaner.truncate_tool_output(tool_content.strip())
                tool_content = DataCleaner.anonymize_paths(tool_content)
                tool_content = DataCleaner.detect_secrets(tool_content)

                if pending_tool_calls:
                    matched = pending_tool_calls.pop(0)
                    cid = matched["id"]
                    tname = matched["name"]
                else:
                    cid = entry.get("tool_use_id", entry.get("id", f"call_cursor_{call_counter}"))
                    tname = entry.get("name", "tool")

                messages.append({
                    "role": "tool",
                    "tool_call_id": cid,
                    "name": tname,
                    "content": tool_content,
                })

        self._flush_pending_calls(messages, pending_tool_calls)
        return messages, tool_calls_count

    @staticmethod
    def _flush_pending_calls(messages: List[Dict], pending_calls: List[Dict[str, str]]):
        while pending_calls:
            call = pending_calls.pop(0)
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "name": call["name"],
                "content": "[Action completed]",
            })


# ==============================================================================
# 5. CODEX EXTRACTOR (Hỗ trợ cả Format 1 Flat & Format 2 Envelope)
# ==============================================================================

class CodexExtractor(BaseIDEExtractor):
    """Trích xuất hội thoại từ OpenAI Codex CLI."""

    def __init__(self, base_path: str):
        super().__init__(base_path, "codex")
        self.sessions_dir = os.path.join(base_path, "sessions")

    def is_available(self) -> bool:
        return os.path.isdir(self.sessions_dir)

    def extract_sessions(self) -> List[Dict[str, Any]]:
        results = []

        for root, _, files in os.walk(self.sessions_dir):
            for filename in files:
                if not (filename.startswith("rollout-") and filename.endswith(".jsonl")):
                    continue

                filepath = os.path.join(root, filename)
                sid = filename.replace("rollout-", "").replace(".jsonl", "")

                messages, tool_calls_count = self._parse_codex_transcript(filepath)

                if len(messages) <= 1:
                    continue

                results.append({
                    "session_id": sid,
                    "ide": self.ide_name,
                    "messages": messages,
                    "artifacts": {},
                    "stats": {
                        "total_turns": len(messages),
                        "tool_calls_count": tool_calls_count,
                        "artifacts_count": 0,
                    }
                })

        return results

    def _parse_codex_transcript(self, path: str) -> Tuple[List[Dict], int]:
        messages = [{"role": "system", "content": DEFAULT_SYSTEM_PROMPT}]
        tool_calls_count = 0
        call_counter = 0

        entries = self._read_jsonl(path)
        pending_tool_calls: List[Dict[str, str]] = []

        # Tích lũy reasoning cho assistant turn
        current_reasoning = ""

        for entry in entries:
            # 1. Định dạng Envelope (Format 2: payload)
            if "payload" in entry and isinstance(entry["payload"], dict):
                p = entry["payload"]
                ptype = p.get("type", "")

                if ptype == "reasoning":
                    raw_r = p.get("summary") or p.get("content") or ""
                    content_r = self._extract_text(raw_r).strip()
                    if content_r:
                        current_reasoning = content_r
                    continue

                elif ptype == "message":
                    role = p.get("role", "")
                    content_val = p.get("content", "")
                    if role == "user":
                        self._flush_pending_calls(messages, pending_tool_calls)
                        cleaned = self._extract_text(content_val)
                        cleaned = DataCleaner.clean_ide_context(cleaned)
                        cleaned = DataCleaner.detect_secrets(cleaned)
                        if cleaned:
                            messages.append({"role": "user", "content": cleaned})

                    elif role == "assistant":
                        self._flush_pending_calls(messages, pending_tool_calls)
                        clean_text = self._extract_text(content_val)
                        text_parts = []
                        if current_reasoning:
                            text_parts.append(f"<thought>\n{current_reasoning}\n</thought>")
                            current_reasoning = ""
                        if clean_text:
                            text_parts.append(clean_text)

                        combined = "\n\n".join(text_parts) if text_parts else None
                        if combined:
                            messages.append({"role": "assistant", "content": combined})

                elif ptype == "function_call":
                    call_counter += 1
                    cid = p.get("call_id") or f"call_codex_{call_counter}"
                    tname = p.get("name", "unknown_tool")
                    args = p.get("arguments", {})
                    args_str = json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args

                    # Kiểm tra xem message cuối có phải assistant để đính kèm tool_calls
                    if messages and messages[-1].get("role") == "assistant":
                        if "tool_calls" not in messages[-1]:
                            messages[-1]["tool_calls"] = []
                        messages[-1]["tool_calls"].append({
                            "id": cid,
                            "type": "function",
                            "function": {"name": tname, "arguments": args_str}
                        })
                    else:
                        text_parts = []
                        if current_reasoning:
                            text_parts.append(f"<thought>\n{current_reasoning}\n</thought>")
                            current_reasoning = ""
                        messages.append({
                            "role": "assistant",
                            "content": "\n\n".join(text_parts) if text_parts else None,
                            "tool_calls": [{
                                "id": cid,
                                "type": "function",
                                "function": {"name": tname, "arguments": args_str}
                            }]
                        })
                    pending_tool_calls.append({"id": cid, "name": tname})
                    tool_calls_count += 1

                elif ptype == "function_call_output":
                    output = p.get("output", "")
                    output_str = DataCleaner.truncate_tool_output(str(output))
                    cid = p.get("call_id")
                    if pending_tool_calls:
                        matched = next((c for c in pending_tool_calls if c["id"] == cid), pending_tool_calls[0])
                        pending_tool_calls.remove(matched)
                        cid = matched["id"]
                        tname = matched["name"]
                    else:
                        cid = cid or f"call_codex_{call_counter}"
                        tname = "tool"

                    messages.append({
                        "role": "tool",
                        "tool_call_id": cid,
                        "name": tname,
                        "content": output_str,
                    })

                continue

            # 2. Định dạng Flat (Format 1: top-level role/type)
            if "record_type" in entry:
                continue
            if "id" in entry and "role" not in entry and "type" not in entry:
                continue

            entry_type = entry.get("type", "")
            role = entry.get("role", "")
            effective_role = role if role else (entry_type if entry_type in ("user", "assistant") else "")

            if effective_role == "user":
                self._flush_pending_calls(messages, pending_tool_calls)
                combined_text = self._extract_text(entry.get("content", []))
                cleaned = DataCleaner.clean_ide_context(combined_text)
                cleaned = DataCleaner.detect_secrets(cleaned)
                if cleaned:
                    messages.append({"role": "user", "content": cleaned})

            elif effective_role == "assistant":
                self._flush_pending_calls(messages, pending_tool_calls)
                assistant_msg: Dict[str, Any] = {"role": "assistant"}
                content = entry.get("content", "")

                if isinstance(content, list):
                    text_parts = []
                    tool_calls = []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type", "")
                        if btype in ("text", "output_text"):
                            t = block.get("text", "").strip()
                            if t:
                                text_parts.append(t)
                        elif btype == "function_call":
                            call_counter += 1
                            cid = block.get("call_id") or f"call_codex_{call_counter}"
                            tname = block.get("name", "unknown_tool")
                            args_raw = block.get("arguments", {})
                            args_str = json.dumps(args_raw, ensure_ascii=False) if not isinstance(args_raw, str) else args_raw
                            tool_calls.append({
                                "id": cid,
                                "type": "function",
                                "function": {"name": tname, "arguments": args_str}
                            })
                            pending_tool_calls.append({"id": cid, "name": tname})

                    combined = "\n\n".join(text_parts) if text_parts else None
                    if combined:
                        assistant_msg["content"] = combined
                    if tool_calls:
                        assistant_msg["tool_calls"] = tool_calls
                        tool_calls_count += len(tool_calls)

                elif isinstance(content, str) and content.strip():
                    assistant_msg["content"] = content.strip()

                if assistant_msg.get("content") or assistant_msg.get("tool_calls"):
                    messages.append(assistant_msg)

            elif entry_type == "function_call_output" or effective_role == "tool":
                output = entry.get("output", entry.get("content", ""))
                output_str = DataCleaner.truncate_tool_output(str(output))
                if pending_tool_calls:
                    matched = pending_tool_calls.pop(0)
                    cid = matched["id"]
                    tname = matched["name"]
                else:
                    cid = entry.get("call_id", f"call_codex_{call_counter}")
                    tname = entry.get("name", "tool")

                messages.append({
                    "role": "tool",
                    "tool_call_id": cid,
                    "name": tname,
                    "content": output_str,
                })

        self._flush_pending_calls(messages, pending_tool_calls)
        return messages, tool_calls_count

    @staticmethod
    def _extract_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            res = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") in ("input_text", "text", "output_text", "summary_text"):
                        res.append(item.get("text", ""))
                    elif "text" in item:
                        res.append(str(item["text"]))
                elif isinstance(item, str):
                    res.append(item)
            return "\n".join(res)
        if isinstance(content, dict):
            return content.get("text", str(content))
        return str(content) if content else ""

    @staticmethod
    def _flush_pending_calls(messages: List[Dict], pending_calls: List[Dict[str, str]]):
        while pending_calls:
            call = pending_calls.pop(0)
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "name": call["name"],
                "content": "[Function call executed]",
            })


# ==============================================================================
# 6. PIPELINE QUẢN LÝ & BÁO CÁO (DatasetPipeline)
# ==============================================================================

class DatasetPipeline:
    """Pipeline điều phối trích xuất, làm sạch và xuất file chuẩn SFT."""

    def __init__(self, selected_ides: List[str], custom_paths: Optional[Dict[str, str]] = None):
        self.selected_ides = [ide.lower().strip() for ide in selected_ides]
        paths = DEFAULT_PATHS.copy()
        if custom_paths:
            paths.update(custom_paths)

        self._registry: Dict[str, BaseIDEExtractor] = {
            "antigravity": AntigravityExtractor(paths.get("antigravity", "")),
            "cursor":      CursorExtractor(paths.get("cursor", "")),
            "codex":       CodexExtractor(paths.get("codex", "")),
        }

    def run(self, output_file: str) -> Dict[str, Any]:
        all_sessions: List[Dict[str, Any]] = []
        stats_per_ide: Dict[str, Dict[str, Any]] = {}

        sep = "=" * 68
        print(f"\n{sep}")
        print("   IDE-NATIVE CODING AGENT — TRAINING DATA EXPORTER PIPELINE")
        print(f"{sep}\n")
        print(f"[START] Thời gian:    {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"[START] IDEs đã chọn: {', '.join(self.selected_ides)}\n")

        for ide_name in self.selected_ides:
            if ide_name not in self._registry:
                print(f"  [!] IDE '{ide_name}' chưa được hỗ trợ. Bỏ qua.")
                continue

            extractor = self._registry[ide_name]
            if not extractor.is_available():
                print(f"  [-] {ide_name.upper():<12}: Không tìm thấy dữ liệu trên máy. Bỏ qua.")
                stats_per_ide[ide_name] = {
                    "sessions": 0, "turns": 0, "tool_calls": 0, "artifacts": 0, "status": "NOT FOUND"
                }
                continue

            print(f"  [>] Đang trích xuất từ {ide_name.upper():<12}...", end="", flush=True)
            try:
                sessions = extractor.extract_sessions()
            except Exception as e:
                print(f" LỖI: {e}")
                stats_per_ide[ide_name] = {
                    "sessions": 0, "turns": 0, "tool_calls": 0, "artifacts": 0, "status": f"ERROR: {e}"
                }
                continue

            all_sessions.extend(sessions)
            stats_per_ide[ide_name] = {
                "sessions":   len(sessions),
                "turns":      sum(s["stats"]["total_turns"] for s in sessions),
                "tool_calls": sum(s["stats"]["tool_calls_count"] for s in sessions),
                "artifacts":  sum(s["stats"]["artifacts_count"] for s in sessions),
                "status":     "OK",
            }
            print(f" Xong! ({len(sessions)} sessions)")

        # Lọc session rỗng hoặc quá ngắn (ít nhất: system + 1 user + 1 assistant)
        valid_sessions = [
            s for s in all_sessions
            if len(s["messages"]) >= 3
        ]
        filtered_count = len(all_sessions) - len(valid_sessions)

        # Ghi ra file JSONL theo chuẩn khớp chính xác định dạng yêu cầu
        print(f"\n  [>] Đang ghi {len(valid_sessions)} mẫu ra file...", end="", flush=True)
        os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)

        total_tool_msgs = 0
        with open(output_file, "w", encoding="utf-8") as f:
            for session in valid_sessions:
                # Đếm tool messages thực tế
                for m in session["messages"]:
                    if m.get("role") == "tool":
                        total_tool_msgs += 1

                sample = {
                    "messages": session["messages"],
                    "artifacts": session.get("artifacts", {}),
                    "metadata": {
                        "session_id":  session["session_id"],
                        "source_ide":  session["ide"],
                        "total_turns": session["stats"]["total_turns"],
                        "tool_calls":  session["stats"]["tool_calls_count"],
                    }
                }
                f.write(json.dumps(sample, ensure_ascii=False) + "\n")

        print(" Xong!")
        print(f"  [+] Tổng số tin nhắn role 'tool' thực tế đã xuất: {total_tool_msgs}")

        self._print_report(stats_per_ide, output_file, len(valid_sessions), filtered_count)
        return {
            "stats_per_ide": stats_per_ide,
            "total_sessions": len(valid_sessions),
            "filtered_out": filtered_count,
            "output_file": output_file,
            "total_tool_messages": total_tool_msgs
        }

    def _print_report(self, stats: Dict[str, Dict], output_file: str, total_valid: int, filtered: int):
        sep = "=" * 68
        dash = "-" * 68
        print(f"\n{sep}")
        print("                 BÁO CÁO THỐNG KÊ KẾT QUẢ XUẤT DỮ LIỆU")
        print(f"{sep}")
        print(f"{'IDE':<16}| {'Sessions':>9} | {'Turns':>8} | {'Tools':>8} | {'Artifacts':>9} | {'Status'}")
        print(dash)

        sum_s = sum_t = sum_tc = sum_a = 0
        for ide_name, data in stats.items():
            s = data.get("sessions", 0)
            t = data.get("turns", 0)
            tc = data.get("tool_calls", 0)
            a = data.get("artifacts", 0)
            st = data.get("status", "?")
            print(f"{ide_name.upper():<16}| {s:>9} | {t:>8} | {tc:>8} | {a:>9} | {st}")
            sum_s += s; sum_t += t; sum_tc += tc; sum_a += a

        print(dash)
        print(f"{'TỔNG CỘNG':<16}| {sum_s:>9} | {sum_t:>8} | {sum_tc:>8} | {sum_a:>9} |")
        print(sep)

        if os.path.isfile(output_file):
            size_bytes = os.path.getsize(output_file)
            size_str = f"{size_bytes / (1024 * 1024):.2f} MB" if size_bytes > 1024 * 1024 else f"{size_bytes / 1024:.1f} KB"
        else:
            size_str = "N/A"

        print(f"\n  Mẫu hợp lệ:  {total_valid}")
        print(f"  Mẫu bị lọc:  {filtered}")
        print(f"  Tệp kết quả: {output_file}")
        print(f"  Dung lượng:  {size_str}")
        print(f"  Định dạng:   JSONL (OpenAI Tool-Calling SFT)")
        print(f"  Encoding:    UTF-8\n")


# ==============================================================================
# 7. MAIN CLI
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="IDE-Native Coding Agent Training Data Exporter")
    parser.add_argument("--ides", nargs="+", default=SELECTED_IDES,
                        help="Danh sách IDE cần trích xuất (antigravity cursor codex)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_FILE,
                        help="Đường dẫn file kết quả .jsonl")
    args = parser.parse_args()

    pipeline = DatasetPipeline(selected_ides=args.ides)
    pipeline.run(output_file=args.output)


if __name__ == "__main__":
    main()
