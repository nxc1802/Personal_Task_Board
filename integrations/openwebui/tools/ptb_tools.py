"""
title: Personal Task Board Tools
author: Personal Task Board (Sub-Agent Eta)
author_url: https://github.com/nxc1802/Personal_Task_Board
version: 1.0.0
license: MIT
description: Bộ công cụ truy vấn và thao tác Personal Task Board cho OpenWebUI, kết nối trực tiếp với PTB Application Service / MCP.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.request
from pydantic import BaseModel, Field

from ptb_contracts.logging import BugCode, log_bug

OFFLINE_ERROR_MESSAGE = (
    "❌ [PTB-OWUI-001] APPLICATION SERVICE OFFLINE "
    "(Run 'ptb serve' or 'ptb run' and check port 8000): "
    "OpenWebUI cannot reach Application API."
)


class Tools:
    class Valves(BaseModel):
        app_service_url: str = Field(
            default="http://localhost:8000",
            description="URL của PTB Application Service"
        )
        board_html_path: str = Field(
            default="",
            description="Đường dẫn file ptb_board.html (tự động phát hiện nếu để trống)"
        )

    def __init__(self):
        self.valves = self.Valves()
        self._last_http_error_logged: bool = False

    def _http_call(
        self,
        endpoint: str,
        method: str = "GET",
        payload: Optional[Dict[str, Any]] = None,
        timeout: float = 3.5
    ) -> Optional[Any]:
        """Thực thi HTTP request đồng bộ tới Application Service."""
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
            log_bug(
                BugCode.PTB_OWUI_001,
                subsystem="openwebui",
                severity="ERROR",
                message="OpenWebUI cannot reach Application API",
                context={"endpoint": endpoint, "method": method, "url": url},
            )
        except Exception as exc:
            self._last_http_error_logged = True
            log_bug(
                BugCode.PTB_OWUI_001,
                subsystem="openwebui",
                severity="ERROR",
                message="OpenWebUI cannot reach Application API",
                context={"endpoint": endpoint, "method": method, "url": url},
                exc=exc,
            )
            return None
        return None

    def _offline_error(self, endpoint: str = "") -> str:
        """Ghi log PTB-OWUI-001 (nếu chưa ghi trong _http_call) và trả về thông báo APPLICATION SERVICE OFFLINE."""
        if not getattr(self, "_last_http_error_logged", False):
            log_bug(
                BugCode.PTB_OWUI_001,
                subsystem="openwebui",
                severity="ERROR",
                message="OpenWebUI cannot reach Application API",
                context={"endpoint": endpoint} if endpoint else None,
            )
        self._last_http_error_logged = False
        return OFFLINE_ERROR_MESSAGE

    def get_today_tasks(self, limit: int = 5) -> str:
        """
        Lấy danh sách các công việc ưu tiên cao nhất hôm nay kèm hệ số điểm (0-100), breakdown chi tiết và lý do.
        :param limit: Số lượng task tối đa cần lấy (mặc định 5).
        """
        endpoint = f"/api/today?limit={limit}"
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
        limit: int = 10
    ) -> str:
        """
        Tra cứu danh sách công việc trong Task Board với bộ lọc đa tiêu chí (status, project, source).
        :param status: Trạng thái cần lọc ('TODO', 'IN_PROGRESS', 'BLOCKED', 'DONE')
        :param project_key: Mã dự án (e.g., 'OPS', 'CORE', 'PTB')
        :param source: Nguồn bắt đầu ('ms_teams', 'outlook', 'jira', 'cursor')
        :param limit: Giới hạn số lượng trả về (mặc định 10).
        """
        params = []
        if status:
            params.append(f"status={status}")
        if project_key:
            params.append(f"project={project_key}")
        if source:
            params.append(f"source={source}")
        params.append(f"limit={limit}")
        endpoint = "/api/tasks?" + "&".join(params)

        data = self._http_call(endpoint)
        if data is None:
            return self._offline_error(endpoint)

        if not data:
            return "Không tìm thấy task nào khớp với tiêu chí tìm kiếm."

        lines = [f"### 📋 Danh sách Task ({len(data)} kết quả):\n"]
        for t in data[:limit]:
            lines.append(
                f"- **`{t.get('id')}`** [{t.get('priority_score', 0)}/100] **{t.get('title')}**\n"
                f"  Trạng thái: `{t.get('status')}` | Dự án: `{t.get('project_key')}`"
            )
        return "\n".join(lines)

    def get_review_queue(self) -> str:
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

    def approve_task(self, review_id: str) -> str:
        """
        Phê duyệt một task candidate từ review queue đưa vào bảng công việc chính thức.
        :param review_id: Mã ID của review item cần duyệt.
        """
        endpoint = f"/api/review/{review_id}/approve"
        res = self._http_call(endpoint, method="POST", payload={"actor": "USER"})
        if res is None:
            return self._offline_error(endpoint)
        return f"✅ **Đã phê duyệt thành công review item `{review_id}`!** Task đã được bổ sung vào Task Board với trạng thái TODO."

    def dismiss_task(self, review_id: str) -> str:
        """
        Từ chối hoặc loại bỏ một task candidate khỏi review queue.
        :param review_id: Mã ID của review item cần loại bỏ.
        """
        endpoint = f"/api/review/{review_id}/dismiss"
        res = self._http_call(endpoint, method="POST", payload={"actor": "USER"})
        if res is None:
            return self._offline_error(endpoint)
        return f"🗑️ **Đã loại bỏ review item `{review_id}` khỏi hàng đợi.**"

    def update_task_status(self, task_id: str, new_status: str) -> str:
        """
        Cập nhật trạng thái của task (TODO, IN_PROGRESS, BLOCKED, DONE, DISMISSED).
        :param task_id: Mã UUID của task cần cập nhật.
        :param new_status: Trạng thái mới ('TODO', 'IN_PROGRESS', 'BLOCKED', 'DONE', 'DISMISSED').
        """
        valid_statuses = ["TODO", "IN_PROGRESS", "BLOCKED", "DONE", "DISMISSED"]
        status_upper = new_status.upper()
        if status_upper not in valid_statuses:
            return f"❌ Trạng thái `{new_status}` không hợp lệ. Vui lòng chọn trong {valid_statuses}."

        endpoint = f"/api/tasks/{task_id}/status"
        payload = {"status": status_upper, "new_status": status_upper, "actor": "USER"}
        res = self._http_call(endpoint, method="POST", payload=payload)
        if res is None:
            return self._offline_error(endpoint)
        return f"✅ **Đã cập nhật task `{task_id}` sang trạng thái `{status_upper}` thành công!**"

    def get_waiting_items(self) -> str:
        """
        Lấy danh sách các việc đang bị nghẽn (BLOCKED) do chờ người khác.
        """
        endpoint = "/api/waiting"
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

    def get_forgotten_commitments(self) -> str:
        """
        Lấy danh sách các cam kết bằng lời hứa trong chat đã trôi quá hạn cần theo dõi (follow-up).
        """
        endpoint = "/api/forgotten"
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

    def search_knowledge(self, query: str, knowledge_type: str = "all") -> str:
        """
        Tìm kiếm các Quyết định Kiến trúc (decisions) và Bài học Kinh nghiệm sửa lỗi (lessons).
        :param query: Từ khóa tìm kiếm
        :param knowledge_type: Loại tri thức ('decision', 'lesson', 'all')
        """
        endpoint = f"/api/knowledge?query={query}&type={knowledge_type}"
        data = self._http_call(endpoint)
        if data is None:
            return self._offline_error(endpoint)

        lines = [f"### 💡 Kết quả tra cứu tri thức cho từ khóa '{query}':\n"]
        decisions = data.get("decisions", []) if isinstance(data, dict) else []
        lessons = data.get("lessons", []) if isinstance(data, dict) else []

        if decisions:
            lines.append("**Quyết định Kiến trúc (ADR):**")
            for d in decisions:
                lines.append(f"- **[{d.get('decision_id')}]** {d.get('summary')} (Bởi: {d.get('decided_by')})")

        if lessons:
            lines.append("\n**Bài học Kinh nghiệm sửa lỗi (Lessons):**")
            for l in lessons:
                lines.append(f"- **[{l.get('lesson_id')}]** {l.get('topic')}: `{l.get('solution')}`")

        if not decisions and not lessons:
            return f"Không tìm thấy tri thức nào liên quan đến '{query}'."

        return "\n".join(lines)

    def get_source_health(self) -> str:
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
        # Resolve board HTML
        candidate_paths = [
            Path(self.valves.board_html_path) if self.valves.board_html_path else None,
            Path(__file__).parent.parent / "board" / "ptb_board.html",
            Path("/Volumes/WorkSpace/Project/Personal_Task_Board/integrations/openwebui/board/ptb_board.html"),
            Path("/app/backend/data/board/ptb_board.html"),
        ]
        html_content = ""
        for p in candidate_paths:
            if p and p.exists():
                html_content = p.read_text(encoding="utf-8")
                break

        if not html_content:
            html_content = "<div>Personal Task Board Artifact loaded.</div>"

        return f"""
:::artifact{{type="text/html" title="Personal Task Board"}}
{html_content}
:::
"""
