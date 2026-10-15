"""
title: PTB Board Action & Assistant
author: Personal Task Board (Wave 2)
author_url: https://github.com/nxc1802/Personal_Task_Board
version: 1.2.0
license: MIT
description: OpenWebUI Action & Filter for rendering Personal Task Board interactive artifact, handling /board shortcut, and quick review queue actions.
"""

import json
import os
from pathlib import Path
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional
import urllib.error
import urllib.parse
import urllib.request
from pydantic import BaseModel, Field

OFFLINE_ERROR_MESSAGE = (
    "❌ [PTB-OWUI-001] APPLICATION SERVICE OFFLINE "
    "(Run 'ptb serve' or 'ptb run' and check port 8000): "
    "OpenWebUI cannot reach Application API."
)


def _sanitize_for_logging(data: Any) -> Any:
    """Mask sensitive tokens (API keys, bearer tokens) before logging."""
    if isinstance(data, dict):
        sanitized = {}
        for k, v in data.items():
            k_lower = str(k).lower()
            if any(secret in k_lower for secret in ("key", "token", "auth", "secret", "password")):
                sanitized[k] = "***REDACTED***"
            else:
                sanitized[k] = _sanitize_for_logging(v)
        return sanitized
    if isinstance(data, list):
        return [_sanitize_for_logging(item) for item in data]
    if isinstance(data, str):
        data = re.sub(r"(Bearer\s+)[A-Za-z0-9_\-\.]{6,}", r"\1***REDACTED***", data)
        data = re.sub(r"(X-PTB-API-KEY[:=]\s*)[A-Za-z0-9_\-\.]{6,}", r"\1***REDACTED***", data, flags=re.IGNORECASE)
        return data
    return data


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
        payload["context"] = _sanitize_for_logging(context)
    if exc is not None:
        payload["exception_type"] = type(exc).__name__
        payload["exception_message"] = _sanitize_for_logging(str(exc))
    sys.stderr.write(f"[PTB-OWUI-001] {json.dumps(payload, ensure_ascii=False)}\n")
    sys.stderr.flush()


class Action:
    class Valves(BaseModel):
        app_service_url: str = Field(
            default=os.getenv(
                "PTB_APP_URL",
                os.getenv("APP_SERVICE_URL", "http://host.docker.internal:8000"),
            ),
            description="Base URL for PTB Application Service",
        )
        api_key: str = Field(
            default=os.getenv("PTB_API_KEY", ""),
            description="PTB API Key cho xác thực X-PTB-API-KEY",
        )
        request_timeout: float = Field(
            default=5.0,
            description="Timeout cho HTTP request (giây, 1.0 - 30.0)",
        )
        max_retries: int = Field(
            default=2,
            description="Số lần thử lại tối đa cho các lỗi mạng tạm thời hoặc 5xx",
        )
        allow_mutations: bool = Field(
            default=True,
            description="Cho phép thực hiện các thao tác thay đổi dữ liệu (approve, dismiss)",
        )
        board_html_path: str = Field(
            default="",
            description="Absolute or relative path to ptb_board.html. Auto-detected if blank.",
        )
        board_title: str = Field(
            default="Personal Task Board",
            description="Title displayed in OpenWebUI Artifact panel",
        )

    def __init__(self):
        env_url = os.getenv("PTB_APP_URL") or os.getenv("APP_SERVICE_URL")
        env_key = os.getenv("PTB_API_KEY", "")
        kwargs = {}
        if env_url:
            kwargs["app_service_url"] = env_url
        if env_key:
            kwargs["api_key"] = env_key
        self.valves = self.Valves(**kwargs)
        self._last_http_error_logged: bool = False
        self._last_error: Optional[str] = None

    def _resolve_board_html_path(self) -> Path:
        """Finds ptb_board.html path on filesystem."""
        if self.valves.board_html_path:
            custom_path = Path(self.valves.board_html_path)
            if custom_path.exists():
                return custom_path
            return custom_path

        candidate_paths = [
            Path(__file__).resolve().parent.parent / "board" / "ptb_board.html",
            Path("/app/backend/data/board/ptb_board.html"),
            Path("./data/openwebui/board/ptb_board.html"),
            Path("integrations/openwebui/board/ptb_board.html"),
        ]
        for p in candidate_paths:
            if p.exists() and p.is_file():
                return p

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
        timeout: Optional[float] = None,
    ) -> Optional[Any]:
        """Performs a synchronous HTTP request with auth, bounded timeout, and retries."""
        self._last_http_error_logged = False
        self._last_error = None

        req_timeout = timeout if timeout is not None else getattr(self.valves, "request_timeout", 5.0)
        bounded_timeout = max(1.0, min(float(req_timeout), 30.0))
        max_retries = max(0, min(int(getattr(self.valves, "max_retries", 2)), 5))

        base_url = self.valves.app_service_url.rstrip("/")
        url = f"{base_url}{endpoint}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None

        headers = {"Content-Type": "application/json"}
        api_key = (getattr(self.valves, "api_key", "") or os.getenv("PTB_API_KEY", "")).strip()
        if api_key:
            headers["X-PTB-API-KEY"] = api_key
            headers["Authorization"] = f"Bearer {api_key}"

        for attempt in range(max_retries + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=bounded_timeout) as response:
                    if 200 <= response.status < 300:
                        raw_content = response.read().decode("utf-8")
                        return json.loads(raw_content) if raw_content else {}
                self._last_http_error_logged = True
                _log_owui_error(
                    "OpenWebUI cannot reach Application API",
                    context={"endpoint": endpoint, "method": method, "url": url, "attempt": attempt},
                )
                return None
            except urllib.error.HTTPError as http_exc:
                err_body = ""
                err_detail = ""
                try:
                    err_body = http_exc.read().decode("utf-8")
                    err_json = json.loads(err_body)
                    err_detail = err_json.get("detail") or err_json.get("message") or ""
                except Exception:
                    err_detail = err_body[:200] if err_body else str(http_exc.reason)

                if http_exc.code >= 500 and attempt < max_retries:
                    time.sleep(0.3 * (attempt + 1))
                    continue

                self._last_http_error_logged = True
                _log_owui_error(
                    f"Application API HTTP Error {http_exc.code}",
                    exc=http_exc,
                    context={"endpoint": endpoint, "method": method, "status": http_exc.code, "detail": err_detail},
                )

                if http_exc.code == 401:
                    self._last_error = (
                        f"❌ [PTB-OWUI-401] XÁC THỰC THẤT BẠI: {err_detail or 'API Key không hợp lệ hoặc bị thiếu'}."
                    )
                elif http_exc.code == 403:
                    self._last_error = (
                        f"❌ [PTB-OWUI-403] TỪ CHỐI TRUY CẬP: {err_detail or 'Thao tác không được phép'}."
                    )
                elif http_exc.code == 404:
                    self._last_error = (
                        f"❌ [PTB-OWUI-404] KHÔNG TÌM THẤY: {err_detail or f'Không tìm thấy tại {endpoint}'}."
                    )
                else:
                    self._last_error = (
                        f"❌ [PTB-OWUI-{http_exc.code}] LỖI HỆ THỐNG: {err_detail or f'HTTP {http_exc.code}'}."
                    )
                return None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as net_exc:
                if attempt < max_retries:
                    time.sleep(0.3 * (attempt + 1))
                    continue

                self._last_http_error_logged = True
                _log_owui_error(
                    "OpenWebUI cannot reach Application API",
                    exc=net_exc,
                    context={"endpoint": endpoint, "method": method, "url": url, "attempts": attempt + 1},
                )
                return None
            except Exception as exc:
                self._last_http_error_logged = True
                _log_owui_error(
                    "OpenWebUI unexpected communication error",
                    exc=exc,
                    context={"endpoint": endpoint, "method": method, "url": url},
                )
                return None
        return None

    def _offline_error(self, endpoint: str = "") -> str:
        """Emits _log_owui_error if not already logged and returns normalized error or APPLICATION SERVICE OFFLINE."""
        if getattr(self, "_last_error", None):
            err = self._last_error
            self._last_error = None
            return err

        if not getattr(self, "_last_http_error_logged", False):
            _log_owui_error(
                "OpenWebUI cannot reach Application API",
                context={"endpoint": endpoint} if endpoint else None,
            )
        self._last_http_error_logged = False
        return OFFLINE_ERROR_MESSAGE

    def _check_mutation_permission(self, __user__: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Verifies if mutations are permitted for the current user/context."""
        if not getattr(self.valves, "allow_mutations", True):
            return "❌ [PTB-OWUI-403] Thao tác bị từ chối: Quyền sửa đổi (mutations) bị tắt trong cấu hình Valves."
        if __user__ and __user__.get("role") in ("pending", "read_only"):
            return f"❌ [PTB-OWUI-403] Thao tác bị từ chối: Quyền hạn của '{__user__.get('name', 'User')}' chỉ xem (role: {__user__.get('role')})."
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
                "data": {"description": "Đang mở Personal Task Board...", "done": False},
            })

        html = self.load_board_html()
        if html.startswith("❌ [PTB-OWUI-001]"):
            content = html
            if __event_emitter__:
                await __event_emitter__({
                    "type": "status",
                    "data": {"description": "Lỗi: Không tìm thấy file board HTML", "done": True},
                })
                await __event_emitter__({
                    "type": "message",
                    "data": {"content": content},
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
                "data": {"description": "Personal Task Board sẵn sàng!", "done": True},
            })
            await __event_emitter__({
                "type": "message",
                "data": {"content": content},
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
                            "reason": item.get("reason", ""),
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
                perm_err = self._check_mutation_permission(__user__)
                if perm_err:
                    response_text = perm_err
                else:
                    actor = f"OPENWEBUI:{__user__.get('name')}" if __user__ and __user__.get("name") else "USER"
                    safe_id = urllib.parse.quote(str(target_id).strip())
                    endpoint = f"/api/review/{safe_id}/approve"
                    res = self._http_request(endpoint, method="POST", payload={"actor": actor})
                    if res is None:
                        response_text = self._offline_error(endpoint)
                    elif isinstance(res, dict) and res.get("success") is False:
                        response_text = f"❌ **Phê duyệt thất bại:** {res.get('message', 'Backend từ chối yêu cầu')}."
                    else:
                        response_text = f"✅ **Đã phê duyệt task candidate `{target_id}`!** Task đã được chuyển sang trạng thái TODO trong authoritative board."

            elif subcommand == "dismiss" and target_id:
                perm_err = self._check_mutation_permission(__user__)
                if perm_err:
                    response_text = perm_err
                else:
                    actor = f"OPENWEBUI:{__user__.get('name')}" if __user__ and __user__.get("name") else "USER"
                    safe_id = urllib.parse.quote(str(target_id).strip())
                    endpoint = f"/api/review/{safe_id}/dismiss"
                    res = self._http_request(endpoint, method="POST", payload={"actor": actor})
                    if res is None:
                        response_text = self._offline_error(endpoint)
                    elif isinstance(res, dict) and res.get("success") is False:
                        response_text = f"❌ **Loại bỏ thất bại:** {res.get('message', 'Backend từ chối yêu cầu')}."
                    else:
                        response_text = f"🗑️ **Đã loại bỏ candidate `{target_id}` khỏi hàng đợi duyệt.**"

            elif subcommand == "today":
                user_id = (__user__.get("id") or __user__.get("email") or "default") if __user__ else "default"
                endpoint = f"/api/today?{urllib.parse.urlencode({'user_id': user_id})}"
                data = self._http_request(endpoint)
                if data is None:
                    response_text = self._offline_error(endpoint)
                else:
                    headline = data.get("summary_headline") or "Hôm nay không có task ưu tiên khẩn cấp."
                    tasks = data.get("top_tasks", [])[:5]
                    task_lines = []
                    for t in tasks:
                        p = t.get("priority", {})
                        score = p.get("total_score", 0.0)
                        task_lines.append(f"- `[{score}/100]` **{t.get('title')}** ({t.get('status')})")

                    tasks_summary = "\n".join(task_lines) if task_lines else "*Không có task nào trong danh sách ưu tiên.*"
                    response_text = (
                        f"### ☀️ Kế hoạch hôm nay (Morning Briefing)\n\n"
                        f"> **{headline}**\n\n"
                        f"{tasks_summary}\n\n"
                        f"Dùng lệnh `/board` để mở đầy đủ 8 views của Task Board tương tác!"
                    )

            elif subcommand == "tasks":
                endpoint = "/api/tasks?limit=10"
                data = self._http_request(endpoint)
                if data is None:
                    response_text = self._offline_error(endpoint)
                elif not data:
                    response_text = "📋 Hiện tại không có task nào trên hệ thống."
                else:
                    lines = [f"### 📋 Danh sách Task ({len(data)} task gần nhất):\n"]
                    for t in data[:10]:
                        lines.append(
                            f"- **`{t.get('id')}`** [{t.get('priority_score', 0)}/100] **{t.get('title')}** "
                            f"(`{t.get('status')}` | Dự án: `{t.get('project_key')}`)"
                        )
                    response_text = "\n".join(lines)

            else:
                response_text = (
                    "**Cú pháp lệnh `/board` khả dụng:**\n"
                    "- `/board` hoặc `/board view`: Mở giao diện interactive board artifact\n"
                    "- `/board today`: Xem tóm tắt kế hoạch hôm nay\n"
                    "- `/board tasks`: Xem danh sách task mới nhất\n"
                    "- `/board review`: Xem danh sách task candidate cần duyệt\n"
                    "- `/board approve <id>`: Duyệt nhanh task candidate\n"
                    "- `/board dismiss <id>`: Bỏ qua task candidate\n"
                )

            if __event_emitter__:
                await __event_emitter__({
                    "type": "message",
                    "data": {"content": response_text},
                })
                last_message["content"] = "[Executed /board action]"

        return body
