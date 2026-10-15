"""
title: Personal Task Board Context Filter
author: Personal Task Board (Wave 2)
author_url: https://github.com/nxc1802/Personal_Task_Board
version: 1.2.0
license: MIT
description: OpenWebUI Filter for automatic task context grounding and injecting live PTB board data into LLM chat.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
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
        "subsystem": "openwebui_filter",
        "severity": "WARNING",
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


class Filter:
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
        inject_task_context: bool = Field(
            default=True,
            description="Tự động gắn context công việc thực tế khi người dùng hỏi về tasks, kế hoạch, hôm nay",
        )
        request_timeout: float = Field(
            default=3.5,
            description="Timeout cho HTTP request (giây, 1.0 - 15.0)",
        )
        max_retries: int = Field(
            default=1,
            description="Số lần thử lại khi lỗi mạng",
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

    def _http_get(
        self,
        endpoint: str,
        timeout: Optional[float] = None,
    ) -> Optional[Any]:
        """Thực thi HTTP GET an toàn với timeout và auth header."""
        self._last_http_error_logged = False
        req_timeout = timeout if timeout is not None else getattr(self.valves, "request_timeout", 3.5)
        bounded_timeout = max(1.0, min(float(req_timeout), 15.0))
        max_retries = max(0, min(int(getattr(self.valves, "max_retries", 1)), 3))

        base_url = self.valves.app_service_url.rstrip("/")
        url = f"{base_url}{endpoint}"

        headers = {"Content-Type": "application/json"}
        api_key = (getattr(self.valves, "api_key", "") or os.getenv("PTB_API_KEY", "")).strip()
        if api_key:
            headers["X-PTB-API-KEY"] = api_key
            headers["Authorization"] = f"Bearer {api_key}"

        for attempt in range(max_retries + 1):
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=bounded_timeout) as response:
                    if 200 <= response.status < 300:
                        raw_content = response.read().decode("utf-8")
                        return json.loads(raw_content) if raw_content else {}
                return None
            except urllib.error.HTTPError as http_exc:
                if http_exc.code >= 500 and attempt < max_retries:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                _log_owui_error(
                    f"Filter HTTP error {http_exc.code}",
                    exc=http_exc,
                    context={"endpoint": endpoint, "status": http_exc.code},
                )
                return None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as net_exc:
                if attempt < max_retries:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                _log_owui_error(
                    "Filter cannot connect to Application API",
                    exc=net_exc,
                    context={"endpoint": endpoint},
                )
                return None
            except Exception as exc:
                _log_owui_error(
                    "Filter unexpected error",
                    exc=exc,
                    context={"endpoint": endpoint},
                )
                return None
        return None

    def _should_inject_context(self, user_content: str) -> bool:
        """Determines if the user's message is asking about tasks, today's schedule, or PTB."""
        if not user_content:
            return False
        content_lower = user_content.lower()

        # Keywords in English and Vietnamese
        trigger_keywords = [
            "today",
            "hôm nay",
            "kế hoạch",
            "công việc",
            "task",
            "tasks",
            "briefing",
            "lịch làm",
            "việc cần làm",
            "priority",
            "ưu tiên",
            "deadline",
            "review queue",
            "waiting on",
            "đang chờ",
            "ptb",
            "/today",
            "/tasks",
        ]
        return any(kw in content_lower for kw in trigger_keywords)

    async def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
    ) -> dict:
        """
        OpenWebUI Filter inlet: intercepts outgoing user prompt to inject authoritative PTB task context.
        """
        if not getattr(self.valves, "inject_task_context", True):
            return body

        messages = body.get("messages", [])
        if not messages:
            return body

        last_user_msg = next((m for m in reversed(messages) if m.get("role") == "user"), None)
        if not last_user_msg:
            return body

        user_content = str(last_user_msg.get("content", ""))
        if not self._should_inject_context(user_content):
            return body

        user_id = (__user__.get("id") or __user__.get("email") or "default") if __user__ else "default"
        endpoint = f"/api/today?{urllib.parse.urlencode({'limit': 5, 'user_id': user_id})}"
        data = self._http_get(endpoint)

        if not data or not isinstance(data, dict):
            # API offline or error; don't disrupt user's conversation, just proceed
            return body

        top_tasks = data.get("top_tasks", [])
        headline = data.get("summary_headline", "")
        risks = data.get("identified_risks", [])

        if not top_tasks and not headline:
            return body

        context_lines = [
            "[Personal Task Board Grounding Context (Source of Truth)]:",
            f"Tóm tắt ngày hôm nay: {headline}" if headline else "",
        ]
        if risks:
            context_lines.append(f"Rủi ro cảnh báo: {'; '.join(risks[:2])}")

        if top_tasks:
            context_lines.append("Top công việc ưu tiên:")
            for t in top_tasks[:5]:
                p = t.get("priority", {})
                score = p.get("total_score", 0.0)
                context_lines.append(
                    f"- [{score}/100] ID: {t.get('task_id')} | Tiêu đề: {t.get('title')} | Trạng thái: {t.get('status')} | Hạn chót: {t.get('due_date', 'Không')}"
                )

        grounding_block = "\n".join(l for l in context_lines if l)

        # Inject into system prompt or as a preceding context message
        system_msg = next((m for m in messages if m.get("role") == "system"), None)
        if system_msg:
            existing_sys_content = system_msg.get("content", "")
            system_msg["content"] = f"{existing_sys_content}\n\n{grounding_block}".strip()
        else:
            messages.insert(0, {"role": "system", "content": grounding_block})

        return body

    async def outlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
    ) -> dict:
        """
        OpenWebUI Filter outlet: post-processes assistant response before displaying to user.
        """
        return body
