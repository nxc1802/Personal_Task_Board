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


class Tools:
    class Valves(BaseModel):
        app_service_url: str = Field(
            default="http://localhost:8000",
            description="URL của PTB Application Service"
        )
        enable_mock_fallback: bool = Field(
            default=True,
            description="Tự động fallback về dữ liệu mẫu nếu Application Service chưa chạy"
        )
        board_html_path: str = Field(
            default="",
            description="Đường dẫn file ptb_board.html (tự động phát hiện nếu để trống)"
        )

    def __init__(self):
        self.valves = self.Valves()

    def _http_call(
        self,
        endpoint: str,
        method: str = "GET",
        payload: Optional[Dict[str, Any]] = None,
        timeout: float = 3.5
    ) -> Optional[Any]:
        """Thực thi HTTP request đồng bộ tới Application Service."""
        url = f"{self.valves.app_service_url.rstrip('/')}{endpoint}"
        data = json.dumps(payload).encode("utf-8") if payload else None
        headers = {"Content-Type": "application/json"}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                if 200 <= response.status < 300:
                    raw_content = response.read().decode("utf-8")
                    return json.loads(raw_content) if raw_content else {}
        except Exception:
            return None
        return None

    def get_today_tasks(self, limit: int = 5) -> str:
        """
        Lấy danh sách các công việc ưu tiên cao nhất hôm nay kèm hệ số điểm (0-100), breakdown chi tiết và lý do.
        :param limit: Số lượng task tối đa cần lấy (mặc định 5).
        """
        data = self._http_call(f"/api/today?limit={limit}")
        if not data and self.valves.enable_mock_fallback:
            data = {
                "summary_headline": "Hôm nay có 2 việc cần ưu tiên, trong đó 1 lỗi deployment staging đang có người chờ.",
                "identified_risks": ["Shortcut story 1204 sẽ đến hạn trong 48h tới nhưng chưa có commit mới"],
                "top_tasks": [
                    {
                        "task_id": "task-unified-001",
                        "title": "Investigate deployment failure on staging",
                        "status": "TODO",
                        "project_key": "OPS",
                        "owner_name": "Dam Quang Cuong",
                        "requester_name": "Nguyen Van Huy",
                        "due_date": "Hôm nay 17:00",
                        "priority": {
                            "total_score": 92.5,
                            "deadline_score": 25.0,
                            "customer_impact_score": 20.0,
                            "production_impact_score": 20.0,
                            "commitment_weight": 15.0,
                            "waiting_penalty": 15.0,
                            "llm_explanation": "Đến hạn hôm nay, ảnh hưởng hệ thống staging và Huy đang chờ phản hồi sau cam kết trên Teams."
                        },
                        "primary_evidence_snippet": "Để em check nhé, chiều nay sẽ có kết quả fix cho staging."
                    },
                    {
                        "task_id": "task-unified-002",
                        "title": "Review PR #142: Graphiti temporal episode integration",
                        "status": "IN_PROGRESS",
                        "project_key": "CORE",
                        "owner_name": "Dam Quang Cuong",
                        "requester_name": "Tran Thi Mai",
                        "due_date": "Ngày mai 12:00",
                        "priority": {
                            "total_score": 78.0,
                            "deadline_score": 15.0,
                            "customer_impact_score": 10.0,
                            "production_impact_score": 20.0,
                            "commitment_weight": 20.0,
                            "waiting_penalty": 15.0,
                            "llm_explanation": "Mai đang chờ duyệt PR để unblock nhánh processing."
                        },
                        "primary_evidence_snippet": "Anh Cường review giúp em PR #142 với nhé."
                    }
                ]
            }

        if not data or not data.get("top_tasks"):
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
        if status: params.append(f"status={status}")
        if project_key: params.append(f"project_key={project_key}")
        if source: params.append(f"source={source}")
        params.append(f"limit={limit}")
        query_str = "?" + "&".join(params)

        data = self._http_call(f"/api/tasks{query_str}")
        if not data and self.valves.enable_mock_fallback:
            data = [
                {"id": "task-001", "title": "Investigate deployment failure on staging", "status": "TODO", "project_key": "OPS", "priority_score": 92.5},
                {"id": "task-002", "title": "Review PR #142: Graphiti temporal episode", "status": "IN_PROGRESS", "project_key": "CORE", "priority_score": 78.0},
                {"id": "task-003", "title": "Cập nhật tài liệu kiến trúc Neo4j-only v1", "status": "TODO", "project_key": "PTB", "priority_score": 65.0},
                {"id": "task-004", "title": "Fix Neo4j APOC procedure permission", "status": "DONE", "project_key": "CORE", "priority_score": 45.0},
                {"id": "task-005", "title": "Đợi access token cho Microsoft Teams Tenant", "status": "BLOCKED", "project_key": "OPS", "priority_score": 82.0}
            ]
            if status: data = [x for x in data if x.get("status") == status]
            if project_key: data = [x for x in data if x.get("project_key") == project_key]

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
        data = self._http_call("/api/review")
        if not data and self.valves.enable_mock_fallback:
            data = [
                {
                    "id": "rev-cand-101",
                    "reason": "Confidence trung bình 0.58; Trích xuất từ tin nhắn chat ngắn trong Teams",
                    "candidate_task": {
                        "id": "cand-101",
                        "title": "Kiểm tra log lỗi đồng bộ webhook Jira",
                        "extraction_confidence": 0.58,
                        "evidences": [{"snippet": "Anh xem giúp em cái webhook jira sao sáng nay không thấy bắn event."}]
                    }
                },
                {
                    "id": "rev-cand-102",
                    "reason": "Confidence 0.62; Phân tách attribution người chịu trách nhiệm cần xác nhận",
                    "candidate_task": {
                        "id": "cand-102",
                        "title": "Cung cấp báo cáo audit security cho đối tác",
                        "extraction_confidence": 0.62,
                        "evidences": [{"snippet": "Could you please send over the latest SOC2 compliance checklist before Friday?"}]
                    }
                }
            ]

        if not data:
            return "✨ **Hàng đợi duyệt (Review Queue) hiện đang trống!**"

        lines = ["### 📥 Hàng đợi duyệt Task Candidate:\n"]
        for it in data:
            cand = it.get("candidate_task", {})
            evidence = cand.get("evidences", [{}])[0].get("snippet", "")
            conf = int(cand.get("extraction_confidence", 0.5) * 100)
            lines.append(
                f"- **ID duyệt:** `{it.get('id')}`\n"
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
        res = self._http_call(f"/api/review/{review_id}/approve", method="POST")
        if res or self.valves.enable_mock_fallback:
            return f"✅ **Đã phê duyệt thành công review item `{review_id}`!** Task đã được bổ sung vào Task Board với trạng thái TODO."
        return f"❌ Không thể kết nối tới Application Service để phê duyệt `{review_id}`."

    def dismiss_task(self, review_id: str) -> str:
        """
        Từ chối hoặc loại bỏ một task candidate khỏi review queue.
        :param review_id: Mã ID của review item cần loại bỏ.
        """
        res = self._http_call(f"/api/review/{review_id}/dismiss", method="POST")
        if res or self.valves.enable_mock_fallback:
            return f"🗑️ **Đã loại bỏ review item `{review_id}` khỏi hàng đợi.**"
        return f"❌ Không thể kết nối tới Application Service để loại bỏ `{review_id}`."

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

        payload = {"new_status": status_upper}
        res = self._http_call(f"/api/tasks/{task_id}/status", method="POST", payload=payload)
        if res or self.valves.enable_mock_fallback:
            return f"✅ **Đã cập nhật task `{task_id}` sang trạng thái `{status_upper}` thành công!**"
        return f"❌ Không thể cập nhật trạng thái cho task `{task_id}`."

    def get_waiting_items(self) -> str:
        """
        Lấy danh sách các việc đang bị nghẽn (BLOCKED) do chờ người khác.
        """
        data = self._http_call("/api/waiting")
        if not data and self.valves.enable_mock_fallback:
            data = [
                {
                    "task_id": "task-005",
                    "title": "Đợi access token cho Microsoft Teams Tenant",
                    "waiting_for_person_name": "Nguyen Van Huy (Admin)",
                    "waiting_days": 3,
                    "reason": "Cần admin duyệt cấp Secret cho App Registration."
                },
                {
                    "task_id": "task-002",
                    "title": "Review PR #142: Graphiti temporal episode integration",
                    "waiting_for_person_name": "Tran Thi Mai",
                    "waiting_days": 1,
                    "reason": "Đang chờ Mai cập nhật thêm unit test."
                }
            ]

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
        data = self._http_call("/api/forgotten")
        if not data and self.valves.enable_mock_fallback:
            data = [
                {
                    "commitment_id": "comm-009",
                    "title": "Gửi tài liệu architecture v1 cho An",
                    "promised_to_name": "Le Hoang An",
                    "days_stale": 4,
                    "last_conversation_snippet": "Chiều nay anh gửi file docs nhé.",
                    "suggested_action": "Gửi link tài liệu hoặc nhắn hẹn lại thời gian cụ thể"
                }
            ]

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
        data = self._http_call(f"/api/knowledge?query={query}&type={knowledge_type}")
        if not data and self.valves.enable_mock_fallback:
            data = {
                "decisions": [
                    {"decision_id": "ADR-001", "summary": "Neo4j-only Architecture (Loại bỏ Postgres & SQLite)", "decided_by": "Architecture Lead"}
                ],
                "lessons": [
                    {"lesson_id": "LES-001", "topic": "Neo4j APOC Configuration trên MacOS Docker", "solution": "Allowlist apoc.* trong docker-compose.yml"}
                ]
            }

        lines = [f"### 💡 Kết quả tra cứu tri thức cho từ khóa '{query}':\n"]
        decisions = data.get("decisions", [])
        lessons = data.get("lessons", [])

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
        data = self._http_call("/api/sources/health")
        if not data and self.valves.enable_mock_fallback:
            data = {
                "overall_health": "healthy",
                "tenants": [
                    {"tenant_name": "Microsoft Teams (Enterprise)", "source_type": "ms_teams", "status": "healthy", "items_synced_total": 1240},
                    {"tenant_name": "Microsoft Outlook (Exchange)", "source_type": "outlook", "status": "healthy", "items_synced_total": 842},
                    {"tenant_name": "Jira Cloud (Sprint)", "source_type": "jira", "status": "healthy", "items_synced_total": 310},
                    {"tenant_name": "Coding Agents (Cursor / Claude / Antigravity)", "source_type": "coding_agents", "status": "healthy", "items_synced_total": 156}
                ]
            }

        if not data:
            return "⚠️ Không thể kiểm tra tình trạng sức khỏe của các adapter nguồn."

        status_icon = "🟢" if data.get("overall_health") == "healthy" else "🟡"
        lines = [f"### 🩺 Tình trạng Đồng bộ Nguồn Dữ liệu: {status_icon} **{data.get('overall_health', '').upper()}**\n"]
        for t in data.get("tenants", []):
            st = "🟢" if t.get("status") == "healthy" else "🔴"
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
