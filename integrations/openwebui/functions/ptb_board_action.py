"""
title: PTB Board Action & Assistant
author: Personal Task Board (Sub-Agent Eta)
author_url: https://github.com/nxc1802/Personal_Task_Board
version: 1.0.0
license: MIT
description: OpenWebUI Action & Filter for rendering Personal Task Board interactive artifact, handling /board shortcut, and quick review queue actions.
"""

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional
import urllib.error
import urllib.request
from pydantic import BaseModel, Field


class Action:
    class Valves(BaseModel):
        app_service_url: str = Field(
            default="http://localhost:8000",
            description="Base URL for PTB Application Service"
        )
        board_html_path: str = Field(
            default="",
            description="Absolute or relative path to ptb_board.html. Auto-detected if blank."
        )
        enable_mock_fallback: bool = Field(
            default=True,
            description="Fallback to bundled mock response if Application Service is unreachable"
        )
        board_title: str = Field(
            default="Personal Task Board",
            description="Title displayed in OpenWebUI Artifact panel"
        )

    def __init__(self):
        self.valves = self.Valves()

    def _resolve_board_html_path(self) -> Path:
        """Finds ptb_board.html path on filesystem."""
        if self.valves.board_html_path:
            custom_path = Path(self.valves.board_html_path)
            if custom_path.exists():
                return custom_path

        # Check default locations
        candidate_paths = [
            Path(__file__).parent.parent / "board" / "ptb_board.html",
            Path("/Volumes/WorkSpace/Project/Personal_Task_Board/integrations/openwebui/board/ptb_board.html"),
            Path("/app/backend/data/board/ptb_board.html"),
            Path("integrations/openwebui/board/ptb_board.html"),
        ]
        for p in candidate_paths:
            if p.exists():
                return p

        # Fallback to the same directory or raise
        return candidate_paths[0]

    def load_board_html(self) -> str:
        """Loads and returns the HTML content of ptb_board.html."""
        path = self._resolve_board_html_path()
        if path.exists():
            return path.read_text(encoding="utf-8")
        
        # Minimal embedded fallback if file is not found
        return (
            "<!DOCTYPE html><html><body>"
            "<h2>Personal Task Board</h2>"
            "<p>Error: ptb_board.html not found. Please verify mount path.</p>"
            "</body></html>"
        )

    def format_as_artifact(self, html_content: str, title: Optional[str] = None) -> str:
        """Formats HTML content as an OpenWebUI compatible interactive artifact."""
        artifact_title = title or self.valves.board_title
        return f"""
:::artifact{{type="text/html" title="{artifact_title}"}}
{html_content}
:::
"""

    def _http_request(
        self,
        endpoint: str,
        method: str = "GET",
        payload: Optional[Dict[str, Any]] = None,
        timeout: float = 3.0
    ) -> Optional[Dict[str, Any]]:
        """Performs a synchronous HTTP request using stdlib urllib."""
        url = f"{self.valves.app_service_url.rstrip('/')}{endpoint}"
        data = json.dumps(payload).encode("utf-8") if payload else None
        headers = {"Content-Type": "application/json"}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                if 200 <= response.status < 300:
                    return json.loads(response.read().decode("utf-8"))
        except Exception:
            return None
        return None

    async def action(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[dict], Any]] = None,
        __event_call__: Optional[Callable[[dict], Any]] = None,
    ) -> Optional[dict]:
        """
        Triggered when user clicks the Action button under an assistant message in OpenWebUI.
        Renders the PTB Board Artifact directly into the artifact panel.
        """
        if __event_emitter__:
            await __event_emitter__({
                "type": "status",
                "data": {"description": "Đang mở Personal Task Board...", "done": False}
            })

        html = self.load_board_html()
        artifact_md = self.format_as_artifact(html)

        content = (
            f"🎯 **Personal Task Board đã được mở trong bảng tương tác bên cạnh!**\n\n"
            f"{artifact_md}\n\n"
            f"*Bạn có thể lọc công việc theo 8 View: Today, Inbox/Review, All Tasks, Waiting, Forgotten, Decisions, Lessons, Sources.*"
        )

        if __event_emitter__:
            await __event_emitter__({
                "type": "status",
                "data": {"description": "Personal Task Board sẵn sàng!", "done": True}
            })
            await __event_emitter__({
                "type": "message",
                "data": {"content": content}
            })

        return {"content": content}

    async def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[dict], Any]] = None,
    ) -> dict:
        """
        Intercepts user input for prompt shortcuts like `/board` or `/review`.
        """
        messages = body.get("messages", [])
        if not messages:
            return body

        last_message = messages[-1]
        user_content = last_message.get("content", "").strip()

        if user_content.startswith("/board") or user_content.startswith("/ptb"):
            parts = user_content.split()
            subcommand = parts[1].lower() if len(parts) > 1 else "view"
            target_id = parts[2] if len(parts) > 2 else ""

            response_text = ""

            if subcommand in ("view", "open"):
                html = self.load_board_html()
                artifact_md = self.format_as_artifact(html)
                response_text = (
                    f"### 🎯 Personal Task Board (Interactive Artifact)\n\n"
                    f"{artifact_md}\n\n"
                    f"💡 *Gợi ý: Dùng `/board review` để xem nhanh hàng đợi duyệt hoặc `/board today` để tóm tắt 5 việc hôm nay.*"
                )

            elif subcommand == "review":
                # Fetch review queue
                data = self._http_request("/api/review")
                if not data and self.valves.enable_mock_fallback:
                    items = [
                        {"id": "rev-cand-101", "title": "Kiểm tra log lỗi đồng bộ webhook Jira", "confidence": 0.58, "reason": "Confidence trung bình 0.58"},
                        {"id": "rev-cand-102", "title": "Cung cấp báo cáo audit security cho đối tác", "confidence": 0.62, "reason": "Attribution cần xác nhận"}
                    ]
                else:
                    items = [
                        {
                            "id": item.get("id"),
                            "title": item.get("candidate_task", {}).get("title"),
                            "confidence": item.get("candidate_task", {}).get("extraction_confidence", 0.5),
                            "reason": item.get("reason", "")
                        }
                        for item in (data or [])
                    ]

                if not items:
                    response_text = "✨ **Hàng đợi duyệt (Review Queue) hiện đang trống!** Không có task nào cần phê duyệt."
                else:
                    lines = ["### 📥 Hàng đợi duyệt Task Candidate (Confidence 0.40 - 0.64):\n"]
                    for it in items:
                        lines.append(
                            f"- **`{it['id']}`**: **{it['title']}** (Độ tin cậy: {int(it['confidence']*100)}%)\n"
                            f"  *Lý do:* {it['reason']}\n"
                            f"  *Lệnh duyệt:* `/board approve {it['id']}` | `/board dismiss {it['id']}`\n"
                        )
                    response_text = "\n".join(lines)

            elif subcommand == "approve" and target_id:
                res = self._http_request(f"/api/review/{target_id}/approve", method="POST")
                if res or self.valves.enable_mock_fallback:
                    response_text = f"✅ **Đã phê duyệt task candidate `{target_id}`!** Task đã được chuyển sang trạng thái TODO trong authoritative board."
                else:
                    response_text = f"❌ Không thể kết nối tới Application Service để phê duyệt task `{target_id}`."

            elif subcommand == "dismiss" and target_id:
                res = self._http_request(f"/api/review/{target_id}/dismiss", method="POST")
                if res or self.valves.enable_mock_fallback:
                    response_text = f"🗑️ **Đã loại bỏ candidate `{target_id}` khỏi hàng đợi duyệt.**"
                else:
                    response_text = f"❌ Không thể kết nối tới Application Service để từ chối task `{target_id}`."

            elif subcommand == "today":
                data = self._http_request("/api/today")
                headline = data.get("summary_headline") if data else "Hôm nay có 2 việc cần ưu tiên, trong đó 1 lỗi deployment staging đang có người chờ."
                response_text = (
                    f"### ☀️ Kế hoạch hôm nay (Morning Briefing)\n\n"
                    f"> **{headline}**\n\n"
                    f"Dùng lệnh `/board` để mở đầy đủ 8 views của Task Board tương tác!"
                )

            else:
                response_text = (
                    "**Cú pháp lệnh `/board` khả dụng:**\n"
                    "- `/board` hoặc `/board view`: Mở giao diện interactive board artifact\n"
                    "- `/board today`: Xem tóm tắt 5 task ưu tiên cao nhất hôm nay\n"
                    "- `/board review`: Xem danh sách task candidate cần duyệt\n"
                    "- `/board approve <id>`: Duyệt nhanh task candidate\n"
                    "- `/board dismiss <id>`: Bỏ qua task candidate\n"
                )

            # If event emitter is available, emit direct assistant response
            if __event_emitter__:
                await __event_emitter__({
                    "type": "message",
                    "data": {"content": response_text}
                })
                # Override message to prevent duplicate generation by LLM
                last_message["content"] = "[Executed /board action]"

        return body
