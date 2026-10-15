"""
title: Personal Task Board Tools
author: Personal Task Board (Wave 2)
author_url: https://github.com/nxc1802/Personal_Task_Board
version: 1.2.0
license: MIT
description: Bộ công cụ truy vấn và thao tác Personal Task Board cho OpenWebUI, kết nối trực tiếp với PTB Application Service / MCP.
"""

import json
import os
from pathlib import Path
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
        # Mask Bearer or API keys matching pattern
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


class Tools:
    class Valves(BaseModel):
        app_service_url: str = Field(
            default=os.getenv(
                "PTB_APP_URL",
                os.getenv("APP_SERVICE_URL", "http://host.docker.internal:8000"),
            ),
            description="URL của PTB Application Service",
        )
        api_key: str = Field(
            default=os.getenv("PTB_API_KEY", ""),
            description="PTB API Key cho xác thực X-PTB-API-KEY",
        )
        request_timeout: float = Field(
            default=5.0,
            description="Timeout cho mỗi HTTP request tới Application Service (giây, 1.0 - 30.0)",
        )
        max_retries: int = Field(
            default=2,
            description="Số lần thử lại tối đa cho các lỗi mạng tạm thời hoặc 5xx",
        )
        allow_mutations: bool = Field(
            default=True,
            description="Cho phép thực hiện các thao tác thay đổi dữ liệu (create, update, delete, approve, dismiss)",
        )
        board_html_path: str = Field(
            default="",
            description="Đường dẫn file ptb_board.html (tự động phát hiện nếu để trống)",
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

    def _check_mutation_permission(self, __user__: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Kiểm tra quyền thực hiện thao tác thay đổi dữ liệu (create, edit, delete, approve, dismiss)."""
        if not getattr(self.valves, "allow_mutations", True):
            return (
                "❌ [PTB-OWUI-403] THAO TÁC BỊ TỪ CHỐI: Quyền sửa đổi dữ liệu (mutations) "
                "đã bị vô hiệu hóa trong cấu hình Valves (allow_mutations=False)."
            )
        if __user__ and __user__.get("role") in ("pending", "read_only"):
            role = __user__.get("role")
            user_name = __user__.get("name", "User")
            return (
                f"❌ [PTB-OWUI-403] THAO TÁC BỊ TỪ CHỐI: Người dùng '{user_name}' "
                f"không có quyền thay đổi dữ liệu trên Task Board (role: {role})."
            )
        return None

    def _http_call(
        self,
        endpoint: str,
        method: str = "GET",
        payload: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Optional[Any]:
        """Thực thi HTTP request đồng bộ tới Application Service với auth, retry và error normalization."""
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
                # HTTP Error response from backend (4xx, 5xx)
                err_body = ""
                err_detail = ""
                try:
                    err_body = http_exc.read().decode("utf-8")
                    err_json = json.loads(err_body)
                    err_detail = err_json.get("detail") or err_json.get("message") or ""
                except Exception:
                    err_detail = err_body[:200] if err_body else str(http_exc.reason)

                # Retry on 5xx transient server errors
                if http_exc.code >= 500 and attempt < max_retries:
                    time.sleep(0.3 * (attempt + 1))
                    continue

                self._last_http_error_logged = True
                _log_owui_error(
                    f"Application API HTTP Error {http_exc.code}",
                    exc=http_exc,
                    context={"endpoint": endpoint, "method": method, "status": http_exc.code, "detail": err_detail},
                )

                # Normalize 4xx client errors
                if http_exc.code == 401:
                    self._last_error = (
                        f"❌ [PTB-OWUI-401] XÁC THỰC THẤT BẠI: {err_detail or 'API Key không hợp lệ hoặc bị thiếu'}. "
                        "Vui lòng kiểm tra cấu hình 'api_key' trong Valves hoặc biến môi trường PTB_API_KEY."
                    )
                elif http_exc.code == 403:
                    self._last_error = (
                        f"❌ [PTB-OWUI-403] TỪ CHỐI TRUY CẬP: {err_detail or 'Thao tác không được phép trên tài nguyên này'}."
                    )
                elif http_exc.code == 404:
                    self._last_error = (
                        f"❌ [PTB-OWUI-404] KHÔNG TÌM THẤY: {err_detail or f'Không tìm thấy dữ liệu tại {endpoint}'}."
                    )
                elif http_exc.code == 400:
                    self._last_error = (
                        f"❌ [PTB-OWUI-400] DỮ LIỆU KHÔNG HỢP LỆ: {err_detail or 'Yêu cầu không đúng định dạng'}."
                    )
                else:
                    self._last_error = (
                        f"❌ [PTB-OWUI-{http_exc.code}] LỖI HỆ THỐNG: {err_detail or f'HTTP {http_exc.code}'}."
                    )
                return None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as net_exc:
                # Transient network error -> retry
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
        """Ghi log PTB-OWUI-001 (nếu chưa ghi) và trả về thông báo lỗi chuẩn hóa hoặc APPLICATION SERVICE OFFLINE."""
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

    def get_today_tasks(self, limit: int = 5, __user__: Optional[Dict[str, Any]] = None) -> str:
        """
        Lấy danh sách các công việc ưu tiên cao nhất hôm nay kèm hệ số điểm (0-100), breakdown chi tiết và lý do.
        :param limit: Số lượng task tối đa cần lấy (mặc định 5).
        """
        user_id = (__user__.get("id") or __user__.get("email") or "default") if __user__ else "default"
        params = {"limit": limit, "user_id": user_id}
        endpoint = f"/api/today?{urllib.parse.urlencode(params)}"
        data = self._http_call(endpoint)
        if data is None:
            return self._offline_error(endpoint)

        if not data.get("top_tasks"):
            return "🎉 **Hôm nay không có task nào khẩn cấp đang chờ xử lý!**"

        headline = data.get("summary_headline", "")
        tasks = data.get("top_tasks", [])[:limit]

        output = [f"### ☀️ Kế hoạch hôm nay (Morning Briefing)\n> {headline}\n"]
        if data.get("identified_risks"):
            output.append(f"⚠️ **Rủi ro tiến độ:** {data['identified_risks'][0]}\n")

        for t in tasks:
            p = t.get("priority", {})
            score = p.get("total_score", 0.0)
            output.append(
                f"#### [{score}/100] {t.get('title')}\n"
                f"- **ID**: `{t.get('task_id')}` | **Trạng thái**: `{t.get('status')}` | **Dự án**: `{t.get('project_key')}`\n"
                f"- **Người yêu cầu**: {t.get('requester_name', 'Chưa rõ')} | **Hạn chót**: {t.get('due_date', 'Không có')}\n"
                f"- **Bằng chứng**: *\"{t.get('primary_evidence_snippet', '')}\"*\n"
                f"- **Lý do ưu tiên**: {p.get('llm_explanation', 'Không có')}\n"
            )
        return "\n".join(output)

    def get_tasks(
        self,
        status: Optional[str] = None,
        project_key: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 10,
        __user__: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Tra cứu danh sách công việc trong Task Board với bộ lọc đa tiêu chí (status, project, source).
        :param status: Trạng thái cần lọc ('TODO', 'IN_PROGRESS', 'BLOCKED', 'DONE', 'DISMISSED')
        :param project_key: Mã dự án (e.g., 'OPS', 'CORE', 'PTB')
        :param source: Nguồn bắt đầu ('ms_teams', 'outlook', 'jira', 'cursor')
        :param limit: Giới hạn số lượng trả về (mặc định 10).
        """
        query_dict: Dict[str, Any] = {}
        if status:
            query_dict["status"] = status
        if project_key:
            query_dict["project"] = project_key
        if source:
            query_dict["source"] = source
        query_dict["limit"] = limit

        endpoint = f"/api/tasks?{urllib.parse.urlencode(query_dict)}"
        data = self._http_call(endpoint)
        if data is None:
            return self._offline_error(endpoint)

        if not data:
            return "Không tìm thấy task nào khớp với tiêu chí tìm kiếm."

        tasks_list = data if isinstance(data, list) else (data.get("tasks", []) if isinstance(data, dict) else [])
        if not tasks_list:
            return "Không tìm thấy task nào khớp với tiêu chí tìm kiếm."

        lines = [f"### 📋 Danh sách Task ({len(tasks_list)} kết quả):\n"]
        for t in tasks_list[:limit]:
            lines.append(
                f"- **`{t.get('id')}`** [{t.get('priority_score', 0)}/100] **{t.get('title')}**\n"
                f"  Trạng thái: `{t.get('status')}` | Dự án: `{t.get('project_key')}`"
            )
        return "\n".join(lines)

    def get_task_detail(
        self,
        task_id: str,
        __user__: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Tra cứu chi tiết toàn diện của một task (Task Context) bao gồm blockers, dependents, evidences, và tri thức liên quan.
        :param task_id: Mã định danh UUID của task cần tra cứu.
        """
        safe_id = urllib.parse.quote(str(task_id).strip())
        endpoint = f"/api/tasks/{safe_id}"
        data = self._http_call(endpoint)
        if data is None:
            return self._offline_error(endpoint)

        task = data.get("task", {})
        lines = [
            f"### 🔍 Chi tiết Task: {task.get('title')} (`{task.get('id')}`)\n",
            f"- **Trạng thái**: `{task.get('status')}` | **Độ ưu tiên**: `{task.get('priority_score', 0)}/100`",
            f"- **Dự án**: `{task.get('project_key', 'GLOBAL')}` | **Người tạo/yêu cầu**: `{task.get('requester_name', 'Chưa rõ')}`",
            f"- **Hạn chót**: {task.get('due_date', 'Không có')}",
        ]
        if task.get("description"):
            lines.append(f"- **Mô tả**: {task.get('description')}")

        evidences = data.get("evidences", [])
        if evidences:
            lines.append("\n**Bằng chứng thu thập (Evidences):**")
            for ev in evidences[:3]:
                lines.append(f"- *\"{ev.get('snippet', '')}\"* (Nguồn: `{ev.get('source_type')}`)")

        blockers = data.get("blockers", [])
        if blockers:
            lines.append("\n⚠️ **Công việc cản trở (Blockers):**")
            for b in blockers:
                lines.append(f"- `{b.get('id')}`: {b.get('title')} [{b.get('status')}]")

        dependents = data.get("dependents", [])
        if dependents:
            lines.append("\n🔗 **Công việc phụ thuộc (Dependents):**")
            for d in dependents:
                lines.append(f"- `{d.get('id')}`: {d.get('title')} [{d.get('status')}]")

        decisions = data.get("related_decisions", [])
        if decisions:
            lines.append("\n💡 **Quyết định kiến trúc liên quan (Decisions):**")
            for dec in decisions[:2]:
                lines.append(f"- **[{dec.get('decision_id')}]** {dec.get('summary')}")

        lessons = data.get("related_lessons", [])
        if lessons:
            lines.append("\n📚 **Bài học kinh nghiệm liên quan (Lessons):**")
            for les in lessons[:2]:
                lines.append(f"- **[{les.get('lesson_id')}]** {les.get('topic')}: {les.get('solution')}")

        return "\n".join(lines)

    def get_review_queue(self, __user__: Optional[Dict[str, Any]] = None) -> str:
        """
        Lấy danh sách các task trích xuất tự động (Confidence 0.40 - 0.64) đang chờ người dùng phê duyệt.
        """
        endpoint = "/api/review"
        data = self._http_call(endpoint)
        if data is None:
            return self._offline_error(endpoint)

        if not data:
            return "✨ **Hàng đợi duyệt (Review Queue) hiện đang trống!**"

        lines = ["### 📥 Hàng đợi duyệt Task Candidate:\n"]
        for it in data:
            cand = it.get("candidate_task", {})
            evidences = cand.get("evidences") or [{}]
            evidence = evidences[0].get("snippet", "") if evidences else ""
            conf = int(cand.get("extraction_confidence", 0.5) * 100)
            lines.append(
                f"- **ID duyệt:** `{it.get('id') or cand.get('id')}`\n"
                f"  **Tiêu đề đề xuất:** {cand.get('title')} (Độ tin cậy: {conf}%)\n"
                f"  **Lý do cần duyệt:** {it.get('reason')}\n"
                f"  **Bằng chứng:** *\"{evidence}\"*\n"
            )
        return "\n".join(lines)

    def approve_task(self, review_id: str, __user__: Optional[Dict[str, Any]] = None) -> str:
        """
        Phê duyệt một task candidate từ review queue đưa vào bảng công việc chính thức.
        :param review_id: Mã ID của review item cần duyệt.
        """
        perm_err = self._check_mutation_permission(__user__)
        if perm_err:
            return perm_err

        actor = (
            f"OPENWEBUI:{__user__.get('name')}"
            if __user__ and __user__.get("name")
            else "USER"
        )
        safe_id = urllib.parse.quote(str(review_id).strip())
        endpoint = f"/api/review/{safe_id}/approve"
        res = self._http_call(endpoint, method="POST", payload={"actor": actor})
        if res is None:
            return self._offline_error(endpoint)

        # BE confirmation verification (no optimistic success)
        if isinstance(res, dict) and res.get("success") is False:
            return f"❌ Phê duyệt thất bại từ hệ thống: {res.get('message', 'Backend từ chối duyệt task candidate')}."

        return (
            f"✅ **Đã phê duyệt thành công review item `{review_id}`!** "
            "Task đã được bổ sung vào Task Board với trạng thái TODO."
        )

    def dismiss_task(self, review_id: str, __user__: Optional[Dict[str, Any]] = None) -> str:
        """
        Từ chối hoặc loại bỏ một task candidate khỏi review queue.
        :param review_id: Mã ID của review item cần loại bỏ.
        """
        perm_err = self._check_mutation_permission(__user__)
        if perm_err:
            return perm_err

        actor = (
            f"OPENWEBUI:{__user__.get('name')}"
            if __user__ and __user__.get("name")
            else "USER"
        )
        safe_id = urllib.parse.quote(str(review_id).strip())
        endpoint = f"/api/review/{safe_id}/dismiss"
        res = self._http_call(endpoint, method="POST", payload={"actor": actor})
        if res is None:
            return self._offline_error(endpoint)

        # BE confirmation verification
        if isinstance(res, dict) and res.get("success") is False:
            return f"❌ Loại bỏ review item thất bại: {res.get('message', 'Backend từ chối loại bỏ')}."

        return f"🗑️ **Đã loại bỏ review item `{review_id}` khỏi hàng đợi.**"

    def update_task_status(
        self,
        task_id: str,
        new_status: str,
        __user__: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Cập nhật trạng thái của task (TODO, IN_PROGRESS, BLOCKED, DONE, DISMISSED).
        :param task_id: Mã UUID của task cần cập nhật.
        :param new_status: Trạng thái mới ('TODO', 'IN_PROGRESS', 'BLOCKED', 'DONE', 'DISMISSED').
        """
        perm_err = self._check_mutation_permission(__user__)
        if perm_err:
            return perm_err

        valid_statuses = ["TODO", "IN_PROGRESS", "BLOCKED", "DONE", "DISMISSED"]
        status_upper = str(new_status).strip().upper()
        if status_upper not in valid_statuses:
            return f"❌ Trạng thái `{new_status}` không hợp lệ. Vui lòng chọn trong {valid_statuses}."

        actor = (
            f"OPENWEBUI:{__user__.get('name')}"
            if __user__ and __user__.get("name")
            else "USER"
        )
        safe_id = urllib.parse.quote(str(task_id).strip())
        endpoint = f"/api/tasks/{safe_id}/status"
        payload = {"status": status_upper, "new_status": status_upper, "actor": actor}
        res = self._http_call(endpoint, method="POST", payload=payload)
        if res is None:
            return self._offline_error(endpoint)

        # BE confirmation verification
        if isinstance(res, dict) and res.get("success") is False:
            return f"❌ Cập nhật trạng thái thất bại: {res.get('message', 'Lỗi từ hệ thống')}."

        return f"✅ **Đã cập nhật task `{task_id}` sang trạng thái `{status_upper}` thành công!**"

    def edit_task(
        self,
        task_id: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        project_key: Optional[str] = None,
        due_date: Optional[str] = None,
        priority_score: Optional[float] = None,
        __user__: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Chỉnh sửa thông tin chi tiết của task (title, description, project_key, due_date, priority_score).
        :param task_id: Mã UUID của task cần chỉnh sửa.
        :param title: Tiêu đề mới (nếu muốn thay đổi).
        :param description: Nội dung mô tả mới.
        :param project_key: Mã dự án mới.
        :param due_date: Hạn chót mới định dạng ISO (ví dụ: '2026-10-30').
        :param priority_score: Điểm ưu tiên mới (0-100).
        """
        perm_err = self._check_mutation_permission(__user__)
        if perm_err:
            return perm_err

        actor = (
            f"OPENWEBUI:{__user__.get('name')}"
            if __user__ and __user__.get("name")
            else "USER"
        )
        safe_id = urllib.parse.quote(str(task_id).strip())
        endpoint = f"/api/tasks/{safe_id}"
        payload: Dict[str, Any] = {"actor": actor}
        if title is not None:
            payload["title"] = title.strip()
        if description is not None:
            payload["description"] = description.strip()
        if project_key is not None:
            payload["project_key"] = project_key.strip()
        if due_date is not None:
            payload["due_date"] = due_date.strip() if due_date.strip() else None
        if priority_score is not None:
            payload["priority_score"] = float(priority_score)

        res = self._http_call(endpoint, method="PATCH", payload=payload)
        if res is None:
            return self._offline_error(endpoint)

        # BE confirmation verification
        if not isinstance(res, dict) or not res.get("id"):
            return f"❌ Chỉnh sửa task thất bại: {res.get('detail') if isinstance(res, dict) else 'Không nhận được xác nhận từ hệ thống'}."

        return f"✅ **Đã cập nhật thành công task `{res.get('id')}`!** (Tiêu đề: {res.get('title')})"

    def create_task(
        self,
        title: str,
        description: str = "",
        project_key: str = "GLOBAL",
        due_date: str = "",
        priority_score: float = 50.0,
        __user__: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Tạo mới một công việc trong Personal Task Board.
        :param title: Tiêu đề công việc cần tạo.
        :param description: Mô tả chi tiết hoặc ghi chú.
        :param project_key: Mã dự án (e.g., 'CORE', 'OPS', 'PTB', 'GLOBAL').
        :param due_date: Hạn chót định dạng ISO hoặc chuỗi (ví dụ: '2026-10-15').
        :param priority_score: Điểm ưu tiên từ 0 đến 100 (mặc định 50).
        """
        perm_err = self._check_mutation_permission(__user__)
        if perm_err:
            return perm_err

        actor = (
            f"OPENWEBUI:{__user__.get('name')}"
            if __user__ and __user__.get("name")
            else "OPENWEBUI"
        )
        endpoint = "/api/tasks"
        payload = {
            "title": title,
            "description": description,
            "project_key": project_key,
            "due_date": due_date if due_date else None,
            "priority_score": priority_score,
            "actor": actor,
        }
        res = self._http_call(endpoint, method="POST", payload=payload)
        if res is None:
            return self._offline_error(endpoint)

        # BE confirmation verification
        if not isinstance(res, dict) or not res.get("id"):
            return f"❌ Tạo task thất bại: {res.get('detail') if isinstance(res, dict) else 'Không nhận được xác nhận ID từ hệ thống'}."

        return f"✅ **Đã tạo thành công task `{res.get('id')}`: {title}!** (Trạng thái: `{res.get('status')}`, Điểm: `{res.get('priority_score')}/100`)"

    def delete_task(self, task_id: str, __user__: Optional[Dict[str, Any]] = None) -> str:
        """
        Loại bỏ hoặc đánh dấu hủy bỏ (DISMISSED) một công việc khỏi bảng.
        :param task_id: Mã ID của task cần xóa hoặc bỏ qua.
        """
        perm_err = self._check_mutation_permission(__user__)
        if perm_err:
            return perm_err

        actor = (
            f"OPENWEBUI:{__user__.get('name')}"
            if __user__ and __user__.get("name")
            else "OPENWEBUI"
        )
        safe_id = urllib.parse.quote(str(task_id).strip())
        endpoint = f"/api/tasks/{safe_id}/delete"
        res = self._http_call(endpoint, method="POST", payload={"actor": actor})
        if res is None:
            return self._offline_error(endpoint)

        # BE confirmation verification
        if isinstance(res, dict) and res.get("success") is False:
            return f"❌ Xóa task thất bại: {res.get('detail', 'Lỗi từ hệ thống')}."

        return f"🗑️ **Đã loại bỏ công việc `{task_id}` thành công.**"

    def get_waiting_items(self, __user__: Optional[Dict[str, Any]] = None) -> str:
        """
        Lấy danh sách các việc đang bị nghẽn (BLOCKED) do chờ người khác.
        """
        user_id = (__user__.get("id") or __user__.get("email") or "default") if __user__ else "default"
        endpoint = f"/api/waiting?{urllib.parse.urlencode({'user_id': user_id})}" if user_id != "default" else "/api/waiting"
        data = self._http_call(endpoint)
        if data is None:
            return self._offline_error(endpoint)

        if not data:
            return "🎉 Hiện tại không có task nào đang bị nghẽn vì chờ người khác!"

        lines = ["### ⏳ Danh sách việc đang chờ người khác (Waiting on Others):\n"]
        for it in data:
            lines.append(
                f"- **{it.get('title')}**\n"
                f"  Đang chờ: **{it.get('waiting_for_person_name')}** ({it.get('waiting_days')} ngày)\n"
                f"  Lý do nghẽn: *{it.get('reason')}*"
            )
        return "\n".join(lines)

    def get_forgotten_commitments(self, __user__: Optional[Dict[str, Any]] = None) -> str:
        """
        Lấy danh sách các cam kết bằng lời hứa trong chat đã trôi quá hạn cần theo dõi (follow-up).
        """
        user_id = (__user__.get("id") or __user__.get("email") or "default") if __user__ else "default"
        endpoint = f"/api/forgotten?{urllib.parse.urlencode({'user_id': user_id})}" if user_id != "default" else "/api/forgotten"
        data = self._http_call(endpoint)
        if data is None:
            return self._offline_error(endpoint)

        if not data:
            return "✨ Tuyệt vời! Bạn không có cam kết hoặc lời hứa nào bị quên trôi quá hạn."

        lines = ["### 🕰️ Cam kết trôi quá hạn (Forgotten Commitments):\n"]
        for it in data:
            lines.append(
                f"- ⚠️ **{it.get('title')}** (Trôi {it.get('days_stale')} ngày)\n"
                f"  Hứa với: **{it.get('promised_to_name')}**\n"
                f"  Đoạn trích chat: *\"{it.get('last_conversation_snippet')}\"*\n"
                f"  Gợi ý hành động: {it.get('suggested_action')}"
            )
        return "\n".join(lines)

    def search_knowledge(
        self,
        query: str,
        knowledge_type: str = "all",
        project_key: Optional[str] = None,
        limit: int = 5,
        __user__: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Tìm kiếm các Quyết định Kiến trúc (decisions) và Bài học Kinh nghiệm sửa lỗi (lessons).
        :param query: Từ khóa tìm kiếm
        :param knowledge_type: Loại tri thức ('decision', 'lesson', 'all')
        :param project_key: Mã dự án cần lọc (tuỳ chọn)
        :param limit: Số lượng kết quả tối đa
        """
        params: Dict[str, Any] = {"query": query, "limit": limit}
        if project_key:
            params["project_key"] = project_key
        endpoint = f"/api/knowledge?{urllib.parse.urlencode(params)}"
        data = self._http_call(endpoint)
        if data is None:
            return self._offline_error(endpoint)

        lines = [f"### 💡 Kết quả tra cứu tri thức cho từ khóa '{query}':\n"]
        decisions = data.get("decisions", []) if isinstance(data, dict) else []
        lessons = data.get("lessons", []) if isinstance(data, dict) else []

        want_decisions = knowledge_type.lower() in ("decision", "decisions", "all")
        want_lessons = knowledge_type.lower() in ("lesson", "lessons", "all")

        found = False
        if want_decisions and decisions:
            found = True
            lines.append("**Quyết định Kiến trúc (ADR):**")
            for d in decisions:
                lines.append(f"- **[{d.get('decision_id')}]** {d.get('summary')} (Bởi: {d.get('decided_by')})")

        if want_lessons and lessons:
            found = True
            lines.append("\n**Bài học Kinh nghiệm sửa lỗi (Lessons):**")
            for l in lessons:
                lines.append(f"- **[{l.get('lesson_id')}]** {l.get('topic')}: `{l.get('solution')}`")

        if not found:
            return f"Không tìm thấy tri thức nào liên quan đến '{query}'."

        return "\n".join(lines)

    def get_source_health(self, __user__: Optional[Dict[str, Any]] = None) -> str:
        """
        Kiểm tra tình trạng hoạt động và đồng bộ của các Adapter nguồn dữ liệu (Teams, Outlook, Jira, Coding Agents).
        """
        endpoint = "/api/sources/health"
        data = self._http_call(endpoint)
        if data is None:
            return self._offline_error(endpoint)

        if not data:
            return "⚠️ Không thể kiểm tra tình trạng sức khỏe của các adapter nguồn."

        overall = str(data.get("overall_health", "unknown")).lower()
        status_icon = "🟢" if overall == "healthy" else "🟡"
        lines = [f"### 🩺 Tình trạng Đồng bộ Nguồn Dữ liệu: {status_icon} **{overall.upper()}**\n"]
        for t in data.get("tenants", []):
            st = "🟢" if str(t.get("status", "")).lower() == "healthy" else "🔴"
            lines.append(f"- {st} **{t.get('tenant_name')}** ({t.get('source_type')}): Đã bắt {t.get('items_synced_total')} sự kiện")
        return "\n".join(lines)

    def render_board_artifact(self) -> str:
        """
        Trả về Personal Task Board Artifact để OpenWebUI hiển thị trực quan trong panel tương tác bên cạnh.
        """
        candidate_paths = [
            Path(self.valves.board_html_path) if self.valves.board_html_path else None,
            Path(__file__).resolve().parent.parent / "board" / "ptb_board.html",
            Path("/app/backend/data/board/ptb_board.html"),
            Path("./data/openwebui/board/ptb_board.html"),
            Path("integrations/openwebui/board/ptb_board.html"),
        ]
        html_content = ""
        for p in candidate_paths:
            if p and p.exists() and p.is_file():
                html_content = p.read_text(encoding="utf-8")
                break

        if not html_content:
            err_msg = (
                "❌ [PTB-OWUI-001] Board HTML file not found. "
                "Run 'ptb openwebui install' to install board assets."
            )
            _log_owui_error("Board HTML file not found for artifact rendering")
            return err_msg

        return f"""
:::artifact{{type="text/html" title="Personal Task Board"}}
{html_content}
:::
"""
