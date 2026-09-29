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
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional
import urllib.error
import urllib.request
from pydantic import BaseModel, Field

OFFLINE_ERROR_MESSAGE = (
    "❌ [PTB-OWUI-001] APPLICATION SERVICE OFFLINE "
    "(Run 'ptb serve' or 'ptb run' and check port 8000): "
    "OpenWebUI cannot reach Application API."
)


def _log_owui_error(
    message: str,
    exc: Optional[Exception] = None,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    """Ghi log lỗi nội bộ có cấu trúc ra stderr với mã [PTB-OWUI-001] mà không phụ thuộc ptb_contracts."""
    payload: Dict[str, Any] = {
        "bug_code": "PTB-OWUI-001",
        "subsystem": "openwebui",
        "severity": "ERROR",
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if context:
        payload["context"] = context
    if exc is not None:
        payload["exception_type"] = type(exc).__name__
        payload["exception_message"] = str(exc)
    sys.stderr.write(f"[PTB-OWUI-001] {json.dumps(payload, ensure_ascii=False)}\n")
    sys.stderr.flush()


class Action:
    class Valves(BaseModel):
        app_service_url: str = Field(
            default=os.getenv(
                "PTB_APP_URL",
                os.getenv("APP_SERVICE_URL", "http://host.docker.internal:8000")
            ),
            description="Base URL for PTB Application Service"
        )
        board_html_path: str = Field(
            default="",
            description="Absolute or relative path to ptb_board.html. Auto-detected if blank."
        )
        board_title: str = Field(
            default="Personal Task Board",
            description="Title displayed in OpenWebUI Artifact panel"
        )

    def __init__(self):
        env_url = os.getenv("PTB_APP_URL") or os.getenv("APP_SERVICE_URL")
        if env_url:
            self.valves = self.Valves(app_service_url=env_url)
        else:
            self.valves = self.Valves()
        self._last_http_error_logged: bool = False

    def _resolve_board_html_path(self) -> Path:
        """Finds ptb_board.html path on filesystem."""
        if self.valves.board_html_path:
            custom_path = Path(self.valves.board_html_path)
            if custom_path.exists():
                return custom_path
            return custom_path

        # Check default locations (no developer hardcoded paths)
        candidate_paths = [
            Path(__file__).resolve().parent.parent / "board" / "ptb_board.html",
            Path("/app/backend/data/board/ptb_board.html"),
            Path("./data/openwebui/board/ptb_board.html"),
            Path("integrations/openwebui/board/ptb_board.html"),
        ]
        for p in candidate_paths:
            if p.exists() and p.is_file():
                return p

        # Fallback to standard integration board path
        return candidate_paths[0]

    def load_board_html(self) -> str:
        """Loads and returns the HTML content of ptb_board.html.

        If the file does not exist, logs [PTB-OWUI-001] to stderr and returns
        an explicit text error message. Never returns synthetic HTML.
        """
        path = self._resolve_board_html_path()
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8")

        error_message = (
            f"❌ [PTB-OWUI-001] Board HTML file not found at '{path}'. "
            "Run 'ptb openwebui install' to install board assets."
        )
        _log_owui_error(
            f"Board HTML file not found at '{path}'",
            context={"path": str(path)},
        )
        return error_message

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
    ) -> Optional[Any]:
        """Performs a synchronous HTTP request using stdlib urllib."""
        self._last_http_error_logged = False
        url = f"{self.valves.app_service_url.rstrip('/')}{endpoint}"
        data = json.dumps(payload).encode("utf-8") if payload else None
        headers = {"Content-Type": "application/json"}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                if 200 <= response.status < 300:
                    raw_content = response.read().decode("utf-8")
                    return json.loads(raw_content) if raw_content else {}
            self._last_http_error_logged = True
            _log_owui_error(
                "OpenWebUI cannot reach Application API",
                context={"endpoint": endpoint, "method": method, "url": url},
            )
        except Exception as exc:
            self._last_http_error_logged = True
            _log_owui_error(
                "OpenWebUI cannot reach Application API",
                exc=exc,
                context={"endpoint": endpoint, "method": method, "url": url},
            )
            return None
        return None

    def _offline_error(self, endpoint: str = "") -> str:
        """Emits _log_owui_error if not already logged and returns APPLICATION SERVICE OFFLINE error."""
        if not getattr(self, "_last_http_error_logged", False):
            _log_owui_error(
                "OpenWebUI cannot reach Application API",
                context={"endpoint": endpoint} if endpoint else None,
            )
        self._last_http_error_logged = False
        return OFFLINE_ERROR_MESSAGE

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
        if html.startswith("❌ [PTB-OWUI-001]"):
            content = html
            if __event_emitter__:
                await __event_emitter__({
                    "type": "status",
                    "data": {"description": "Lỗi: Không tìm thấy file board HTML", "done": True}
                })
                await __event_emitter__({
                    "type": "message",
                    "data": {"content": content}
                })
            return {"content": content}

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
                if html.startswith("❌ [PTB-OWUI-001]"):
                    response_text = html
                else:
                    artifact_md = self.format_as_artifact(html)
                    response_text = (
                        f"### 🎯 Personal Task Board (Interactive Artifact)\n\n"
                        f"{artifact_md}\n\n"
                        f"💡 *Gợi ý: Dùng `/board review` để xem nhanh hàng đợi duyệt hoặc `/board today` để tóm tắt 5 việc hôm nay.*"
                    )

            elif subcommand == "review":
                endpoint = "/api/review"
                data = self._http_request(endpoint)
                if data is None:
                    response_text = self._offline_error(endpoint)
                else:
                    items = [
                        {
                            "id": item.get("id") or item.get("candidate_task", {}).get("id"),
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
                endpoint = f"/api/review/{target_id}/approve"
                res = self._http_request(endpoint, method="POST", payload={"actor": "USER"})
                if res is None:
                    response_text = self._offline_error(endpoint)
                else:
                    response_text = f"✅ **Đã phê duyệt task candidate `{target_id}`!** Task đã được chuyển sang trạng thái TODO trong authoritative board."

            elif subcommand == "dismiss" and target_id:
                endpoint = f"/api/review/{target_id}/dismiss"
                res = self._http_request(endpoint, method="POST", payload={"actor": "USER"})
                if res is None:
                    response_text = self._offline_error(endpoint)
                else:
                    response_text = f"🗑️ **Đã loại bỏ candidate `{target_id}` khỏi hàng đợi duyệt.**"

            elif subcommand == "today":
                endpoint = "/api/today"
                data = self._http_request(endpoint)
                if data is None:
                    response_text = self._offline_error(endpoint)
                else:
                    headline = data.get("summary_headline") or "Hôm nay không có task ưu tiên khẩn cấp."
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
