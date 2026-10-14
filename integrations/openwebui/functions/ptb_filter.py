"""
title: PTB Filter & Assistant
author: Personal Task Board
version: 1.0.0
description: Tự động kết nối Personal Task Board Tools và xử lý các lệnh /board
"""

import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Dict, Optional
import urllib.request
from pydantic import BaseModel, Field

class Filter:
    class Valves(BaseModel):
        app_service_url: str = Field(
            default=os.getenv("PTB_APP_URL", os.getenv("APP_SERVICE_URL", "http://host.docker.internal:8000")),
            description="Base URL for PTB Application Service"
        )
        board_html_path: str = Field(
            default="",
            description="Absolute or relative path to ptb_board.html"
        )

    def __init__(self):
        env_url = os.getenv("PTB_APP_URL") or os.getenv("APP_SERVICE_URL")
        if env_url:
            self.valves = self.Valves(app_service_url=env_url)
        else:
            self.valves = self.Valves()

    def _resolve_board_html_path(self) -> Path:
        if self.valves.board_html_path:
            p = Path(self.valves.board_html_path)
            if p.exists():
                return p
        candidate_paths = [
            Path("/app/backend/data/board/ptb_board.html"),
            Path("./data/openwebui/board/ptb_board.html"),
            Path("integrations/openwebui/board/ptb_board.html"),
        ]
        for p in candidate_paths:
            if p.exists() and p.is_file():
                return p
        return candidate_paths[0]

    def load_board_html(self) -> str:
        path = self._resolve_board_html_path()
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8")
        return "Board HTML file not found."

    def format_as_artifact(self, html_content: str) -> str:
        return f'\n:::artifact{{type="text/html" title="Personal Task Board"}}\n{html_content}\n:::\n'

    def _http_get(self, endpoint: str) -> Optional[dict]:
        try:
            url = f"{self.valves.app_service_url.rstrip('/')}{endpoint}"
            req = urllib.request.Request(url, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                if 200 <= resp.status < 300:
                    return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None
        return None

    def _format_today_summary(self, data: dict) -> str:
        if not data:
            return "Không thể kết nối đến Personal Task Board Application API."
        headline = data.get("summary_headline") or "Hôm nay không có task ưu tiên khẩn cấp."
        tasks = data.get("top_tasks") or []
        risks = data.get("risks") or []

        lines = [
            "### ☀️ Kế hoạch hôm nay (Morning Briefing)\n",
            f"> **{headline}**\n"
        ]

        if risks:
            lines.append("**⚠️ Rủi ro tiến độ cần chú ý:**")
            for r in risks[:3]:
                lines.append(f"- {r.get('risk_type', 'Rủi ro')}: **{r.get('description', '')}**")
            lines.append("")

        if tasks:
            lines.append("**📋 Danh sách công việc ưu tiên hàng đầu:**")
            for t in tasks:
                score = t.get("total_score", 0)
                title = t.get("title", "Không có tiêu đề")
                status = t.get("status", "TODO")
                source = t.get("source", "ms_teams")
                due = t.get("due_date") or "Chưa đặt hạn"
                lines.append(f"- **[{score:.1f}/100]** `{status}`: **{title}** (Nguồn: `{source}` | Hạn: {due})")
        else:
            lines.append("*Hiện tại không có task nào đang chờ thực hiện.*")

        lines.append("\n💡 *Gõ `/board` để mở giao diện bảng công việc tương tác đầy đủ!*")
        return "\n".join(lines)

    async def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[dict], Any]] = None,
    ) -> dict:
        # 1. Luôn kích hoạt tool ptb_tools
        tool_ids = body.get("tool_ids") or []
        if "ptb_tools" not in tool_ids:
            tool_ids.append("ptb_tools")
            body["tool_ids"] = tool_ids

        messages = body.get("messages", [])
        if not messages:
            return body

        last_message = messages[-1]
        raw_content = last_message.get("content", "").strip()

        # Chuẩn hóa nếu có tag [/board]
        clean_content = raw_content
        is_slash_cmd = False
        if clean_content.startswith("[/board]"):
            clean_content = clean_content[len("[/board]"):].strip() or "/board"
            is_slash_cmd = True
        elif clean_content.startswith("/board") or clean_content.startswith("/ptb"):
            is_slash_cmd = True

        # 2. Xử lý lệnh /board
        if is_slash_cmd:
            parts = clean_content.split()
            subcommand = parts[1].lower() if len(parts) > 1 else "view"

            if subcommand in ("view", "open") or len(parts) <= 1:
                html = self.load_board_html()
                artifact = self.format_as_artifact(html)
                resp = (
                    "### 🎯 Personal Task Board (Interactive Artifact)\n\n"
                    f"{artifact}\n\n"
                    "💡 *Gợi ý: Dùng `/board today` hoặc hỏi 'công việc hôm nay là gì' để xem tóm tắt.*"
                )
                if __event_emitter__:
                    await __event_emitter__({"type": "message", "data": {"content": resp}})
                    last_message["content"] = "[Executed /board action]"
                return body

            elif subcommand == "today":
                data = self._http_get("/api/today")
                resp = self._format_today_summary(data)
                if __event_emitter__:
                    await __event_emitter__({"type": "message", "data": {"content": resp}})
                    last_message["content"] = "[Executed /board today]"
                return body

        # 3. Xử lý câu hỏi tự nhiên về công việc hôm nay
        # Nếu người dùng hỏi: "công việc hôm nay là gì", "hôm nay có việc gì", "task hôm nay", v.v.
        lower_q = raw_content.lower()
        today_keywords = [
            "công việc hôm nay", "hôm nay là gì", "việc hôm nay",
            "task hôm nay", "hôm nay làm gì", "có việc gì hôm nay",
            "kế hoạch hôm nay", "today tasks", "what are today"
        ]
        if any(kw in lower_q for kw in today_keywords):
            data = self._http_get("/api/today")
            if data:
                resp = self._format_today_summary(data)
                if __event_emitter__:
                    await __event_emitter__({"type": "message", "data": {"content": resp}})
                    last_message["content"] = "[Answered today tasks automatically]"
                return body

        return body
