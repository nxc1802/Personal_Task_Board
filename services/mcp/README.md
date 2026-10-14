# PTB FastMCP Server (Layer 5)

FastMCP Server hoàn chỉnh xuất toàn bộ năng lực **Backend (BE)** và **AI Agent** của Personal Task Board thành chuẩn Model Context Protocol (MCP) dành cho các **Coding Agents** (Google Antigravity, Cursor, Claude Desktop / Claude Code, VS Code, Windsurf, Cline) sử dụng song song với người dùng trên WebOpenUI.

## 1. Danh sách 19 Công Cụ MCP (Tools)

### A. 10 Công cụ Truy vấn Dữ liệu (Query Tools)
1. `get_today_tasks(limit: int = 5)`: Morning briefing & top tasks ưu tiên hôm nay kèm điểm số và lý do.
2. `get_tasks(filters: dict = None)`: Lọc tasks theo tiêu chí: status, project, owner, due_date, priority.
3. `get_task_context(task_id: str)`: Đồ thị ngữ cảnh đầy đủ: blockers, dependents, evidences, decisions, lessons.
4. `get_waiting_items()`: Danh sách công việc đang bị nghẽn (waiting on others / blocked).
5. `get_forgotten_commitments(days_stale: int = 3)`: Các cam kết kỹ thuật tồn đọng cần follow-up.
6. `get_review_queue(limit: int = 10)`: Hàng đợi candidate task đang chờ duyệt (confidence 0.40 - 0.64).
7. `search_decisions(query: str, project_key: str = None)`: Tìm kiếm quyết định kiến trúc (ADR) trong Graphiti memory.
8. `search_lessons_learned(error_or_topic: str)`: Tìm kiếm bài học kinh nghiệm và giải pháp sửa lỗi trước đây.
9. `search_context(query: str)`: Tra cứu ngữ nghĩa toàn diện trên bộ nhớ tri thức.
10. `get_source_health()`: Kiểm tra tình trạng kết nối, đồng bộ và checkpoints của các adapters.

### B. 6 Công cụ Thao tác Dữ liệu BE (CRUD & Mutation Tools)
11. `create_task(title, description, project_key, due_date, priority_score, owner_name)`: Tạo mới task vào Task Board.
12. `update_task_status(task_id, new_status, actor)`: Cập nhật trạng thái (`TODO`, `IN_PROGRESS`, `BLOCKED`, `DONE`, `DISMISSED`) kèm audit log.
13. `update_task(task_id, title, description, priority_score, due_date)`: Chỉnh sửa chi tiết nội dung task.
14. `delete_task(task_id, actor)`: Loại bỏ / đánh dấu hủy bỏ (DISMISSED) task.
15. `approve_task(review_id, new_status, actor)`: Phê duyệt candidate task từ review queue vào bảng công việc.
16. `dismiss_task(review_id, actor)`: Từ chối / loại bỏ candidate task khỏi review queue.

### C. 3 Công cụ AI Agent & Ghi nhận Tri thức (Agent & Knowledge Tools)
17. `ask_ptb_agent(prompt, history)`: Hỏi đáp trực tiếp với PTB Agent tổng hợp tri thức thực tế từ Neo4j (Tasks, Teams, Git, Coding Agents).
18. `record_decision(summary, rationale, project_key, decided_by)`: Lưu trữ quyết định kiến trúc (ADR) vào Graphiti memory.
19. `record_lesson_learned(topic, solution, error_context)`: Ghi nhận bài học kinh nghiệm hoặc giải pháp sửa lỗi vào Graphiti memory.

## 2. MCP Resources & Prompts

### Resources
- `ptb://today-plan`: Kế hoạch ngày hôm nay và danh sách công việc ưu tiên dạng JSON.
- `ptb://tasks`: Toàn bộ danh sách công việc dạng JSON.
- `ptb://system-health`: Báo cáo sức khỏe hệ thống và MCP server dạng JSON.

### Prompts
- `morning_briefing`: Prompt định dạng chuẩn cho Coding Agent tóm tắt công việc đầu ngày.
- `task_execution_context`: Prompt định dạng chuẩn thu thập đầy đủ ngữ cảnh để giải quyết 1 task cụ thể.

## 3. Khởi chạy & Cài đặt

```bash
# Xem cấu hình MCP cho Coding Agents
ptb mcp config

# Tự động ghi cấu hình vào Antigravity / Cursor / Claude Desktop / VS Code
ptb mcp install --client antigravity
ptb mcp install --client all

# Khởi chạy FastMCP Server độc lập
ptb mcp serve --transport sse   # Chạy HTTP/SSE trên cổng 8001
ptb mcp serve --transport stdio # Chạy stdio pipe cho IDE subprocess
```
