# Personal Task Board (PTB) FastMCP Server (Layer 5)

FastMCP Server hoàn chỉnh xuất toàn bộ năng lực **Backend (BE)** và **AI Agent** của Personal Task Board thành chuẩn Model Context Protocol (MCP) dành cho các **Coding Agents** (Google Antigravity, Cursor, Claude Desktop / Claude Code, VS Code / Cline / Roo-Code, Windsurf) sử dụng song song và đồng bộ dữ liệu thời gian thực với người dùng trên WebOpenUI và REST API.

---

## 1. Kiến trúc Độc nhất (Single Source of Truth)

- **Authoritative Database**: Neo4j Single-Store duy nhất kết hợp Graphiti Semantic Memory.
- **Shared ApplicationService**: FastMCP Server và FastAPI REST API dùng chung 1 instance `ApplicationService`, tuyệt đối không duy trì database thứ hai. Mọi thay đổi qua MCP lập tức phản ánh lên Web UI và ngược lại.
- **Transports**:
  - `stdio`: Dành cho Coding Agent chạy local subprocess (Cursor, Claude Desktop, Antigravity, VS Code).
  - `sse` (HTTP Server-Sent Events): Dành cho remote / docker agent kết nối qua mạng `http://127.0.0.1:8001/sse`.

---

## 2. Mô hình Phân quyền & Bảo mật (Security & Permission Model)

### A. Chế độ Hoạt động (Operation Modes)
1. **Read-Only Mode (Mặc định)**:
   - Cung cấp chính xác **10 Read-Only Query Tools** cùng 3 Resources và 2 Prompts.
   - Tuyệt đối không đăng ký các công cụ ghi/sửa (mutation tools). Bất kỳ nỗ lực gọi tool ghi nào sẽ bị từ chối với lỗi `Unknown tool`.
   - Kích hoạt qua cờ `--read-only` hoặc biến môi trường `PTB_MCP_READ_ONLY=true`.
2. **Trusted Write Mode**:
   - Cung cấp đầy đủ **19 Tools** (10 query + 6 BE CRUD mutations + 3 AI agent / knowledge tools).
   - Kích hoạt khi chạy với `read_only=False` hoặc cấu hình IDE client phù hợp.

### B. Ngăn chặn Giả mạo Quyền hạn (P1-5 Impersonation Guard)
- Các tool đột biến (`update_task_status`, `delete_task`, `approve_task`, `dismiss_task`, `record_decision`) hỗ trợ tham số `actor`.
- Mặc định các Coding Agent sử dụng danh tính `actor="CODING_AGENT"` hoặc tên agent tương ứng (ví dụ `CURSOR_AGENT`, `CLAUDE_CODE`).
- **Chính sách bảo vệ**: Các danh tính đặc quyền (`ADMIN`, `SYSTEM`, `ROOT`, `SUPERUSER`) bị cấm sử dụng từ xa trừ khi client cung cấp `admin_key` trùng khớp với biến môi trường `PTB_ADMIN_KEY`. Nỗ lực mạo danh sẽ bị từ chối ngay lập tức với mã lỗi `Impersonation forbidden`.

### C. Cơ chế Xác nhận Thao tác Nguy hiểm (Destructive Action Safeguard)
- Tool `delete_task` yêu cầu tham số xác nhận `confirm: bool = True`.
- Nếu client truyền `confirm=False`, thao tác xóa sẽ bị hủy bỏ an toàn với thông báo `Destructive action aborted: confirmation required`.

### D. Xác thực API Key trên HTTP / SSE (P0-2 & W1-02)
- Khi biến môi trường `PTB_API_KEY` được thiết lập:
  - Endpoint giám sát `/health` luôn mở công khai (không cần API key) để phục vụ health check và Docker orchestration.
  - Endpoint `/sse` và các kết nối SSE bắt buộc phải cung cấp API key qua:
    1. Header `X-PTB-API-KEY: <key>`
    2. Header `Authorization: Bearer <key>`
    3. Query parameter `?api_key=<key>` (hỗ trợ các client EventSource không tùy biến được header).
  - Kết nối không có key hợp lệ sẽ bị từ chối với mã **HTTP 401 Unauthorized**.
- Khi `PTB_API_KEY` để trống: Server hoạt động ở chế độ phát triển nội bộ (`unauthenticated dev mode`) kèm cảnh báo trong log.

### E. Audit Logging
- Mọi thao tác đột biến đều được ghi nhận có cấu trúc vào log hệ thống: `AUDIT [MCP] actor=<actor> action=<action> task_id=<id>`.
- Token, mật khẩu và API key tuyệt đối **không bao giờ** xuất hiện trong log hoặc thông báo lỗi.

---

## 3. Danh sách 19 Công cụ MCP (Tool Inventory)

### A. 10 Công cụ Truy vấn Dữ liệu (Query Tools - Có ở cả 2 chế độ)
1. `get_today_tasks(limit: int = 5)`: Morning briefing & top tasks ưu tiên hôm nay kèm điểm số và bằng chứng theo Canonical Sort Contract.
2. `get_tasks(filters: Optional[dict] = None)`: Lọc tasks theo tiêu chí: status, project, customer, source, owner, priority, due_range.
3. `get_task_context(task_id: str)`: Đồ thị ngữ cảnh đầy đủ: blockers, dependents, evidences, decisions, lessons.
4. `get_waiting_items()`: Danh sách công việc đang bị nghẽn (waiting on others / blocked).
5. `get_forgotten_commitments(days_stale: int = 3)`: Các cam kết kỹ thuật tồn đọng cần follow-up.
6. `get_review_queue(limit: int = 10)`: Hàng đợi candidate task đang chờ duyệt (confidence 0.40 - 0.64).
7. `search_decisions(query: str, project_key: Optional[str] = None)`: Tìm kiếm quyết định kiến trúc (ADR) trong Graphiti memory.
8. `search_lessons_learned(error_or_topic: str)`: Tìm kiếm bài học kinh nghiệm và giải pháp sửa lỗi trước đây.
9. `search_context(query: str)`: Tra cứu ngữ nghĩa toàn diện trên bộ nhớ tri thức.
10. `get_source_health()`: Kiểm tra tình trạng kết nối, đồng bộ và checkpoints của các adapters.

### B. 6 Công cụ Thao tác Dữ liệu BE (CRUD & Mutation Tools - Trusted Write Mode)
11. `create_task(title, description, project_key, due_date, priority_score, owner_name, actor)`: Tạo mới task vào Task Board.
12. `update_task_status(task_id, new_status, actor)`: Cập nhật trạng thái (`TODO`, `IN_PROGRESS`, `BLOCKED`, `DONE`, `DISMISSED`) kèm audit trail.
13. `update_task(task_id, title, description, priority_score, due_date, actor)`: Chỉnh sửa chi tiết nội dung task.
14. `delete_task(task_id, actor, confirm)`: Loại bỏ / đánh dấu hủy bỏ (DISMISSED) task có cơ chế xác nhận an toàn.
15. `approve_task(review_id, new_status, actor)`: Phê duyệt candidate task từ review queue vào bảng công việc.
16. `dismiss_task(review_id, actor)`: Từ chối / loại bỏ candidate task khỏi review queue.

### C. 3 Công cụ AI Agent & Ghi nhận Tri thức (Agent & Knowledge Tools)
17. `ask_ptb_agent(prompt, history)`: Hỏi đáp trực tiếp với PTB Agent tổng hợp tri thức thực tế từ Neo4j (Tasks, Teams, Git, Coding Agents).
18. `record_decision(summary, rationale, project_key, decided_by)`: Lưu trữ quyết định kiến trúc (ADR) vào Graphiti memory.
19. `record_lesson_learned(topic, solution, error_context)`: Ghi nhận bài học kinh nghiệm hoặc giải pháp sửa lỗi vào Graphiti memory.

---

## 4. MCP Resources & Prompts

### Resources
- `ptb://today-plan`: Kế hoạch ngày hôm nay và danh sách công việc ưu tiên dạng JSON.
- `ptb://tasks`: Toàn bộ danh sách công việc dạng JSON.
- `ptb://system-health`: Báo cáo sức khỏe hệ thống và MCP server dạng JSON.

### Prompts
- `morning_briefing`: Prompt định dạng chuẩn cho Coding Agent tóm tắt công việc đầu ngày và phân tích rủi ro.
- `task_execution_context`: Prompt định dạng chuẩn thu thập đầy đủ ngữ cảnh để giải quyết 1 task cụ thể.

---

## 5. Cấu hình cho các Coding Agents (Client Configuration)

### A. Google Antigravity
Đường dẫn file cấu hình: `%USERPROFILE%\.gemini\antigravity-ide\mcp_config.json` hoặc `%USERPROFILE%\.gemini\config\mcp_config.json`

**Chế độ stdio (Khuyên dùng cho máy local):**
```json
{
  "mcpServers": {
    "personal-task-board": {
      "command": "D:\\Working\\PTB\\Personal_Task_Board-main\\.venv\\Scripts\\python.exe",
      "args": [
        "-m",
        "ptb_mcp.server",
        "--transport",
        "stdio"
      ],
      "env": {
        "PYTHONPATH": "D:\\Working\\PTB\\Personal_Task_Board-main",
        "PTB_API_KEY": "${PTB_API_KEY}"
      }
    }
  }
}
```

**Chế độ SSE (HTTP mạng):**
```json
{
  "mcpServers": {
    "personal-task-board": {
      "url": "http://127.0.0.1:8001/sse?api_key=${PTB_API_KEY}"
    }
  }
}
```

---

### B. Cursor
Đường dẫn file cấu hình: `.cursor/mcp.json` (trong workspace root) hoặc `%USERPROFILE%\.cursor\mcp.json`

```json
{
  "mcpServers": {
    "personal-task-board": {
      "command": "D:\\Working\\PTB\\Personal_Task_Board-main\\.venv\\Scripts\\python.exe",
      "args": [
        "-m",
        "ptb_mcp.server",
        "--transport",
        "stdio"
      ],
      "env": {
        "PYTHONPATH": "D:\\Working\\PTB\\Personal_Task_Board-main"
      }
    }
  }
}
```

Nếu kết nối qua SSE Server:
```json
{
  "mcpServers": {
    "personal-task-board": {
      "url": "http://127.0.0.1:8001/sse",
      "headers": {
        "X-PTB-API-KEY": "your-ptb-api-key"
      }
    }
  }
}
```

---

### C. Claude Desktop / Claude Code
Đường dẫn file cấu hình: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "personal-task-board": {
      "command": "D:\\Working\\PTB\\Personal_Task_Board-main\\.venv\\Scripts\\python.exe",
      "args": [
        "-m",
        "ptb_mcp.server",
        "--transport",
        "stdio"
      ],
      "env": {
        "PYTHONPATH": "D:\\Working\\PTB\\Personal_Task_Board-main"
      }
    }
  }
}
```

---

### D. VS Code (Cline, Roo-Code, Codex)
Đường dẫn file cấu hình: `.vscode/mcp.json`

```json
{
  "mcpServers": {
    "personal-task-board": {
      "command": "D:\\Working\\PTB\\Personal_Task_Board-main\\.venv\\Scripts\\python.exe",
      "args": [
        "-m",
        "ptb_mcp.server",
        "--transport",
        "stdio"
      ],
      "env": {
        "PYTHONPATH": "D:\\Working\\PTB\\Personal_Task_Board-main"
      }
    }
  }
}
```

---

### E. Windsurf
Đường dẫn file cấu hình: `%USERPROFILE%\.codeium\windsurf\mcp_config.json`

```json
{
  "mcpServers": {
    "personal-task-board": {
      "command": "D:\\Working\\PTB\\Personal_Task_Board-main\\.venv\\Scripts\\python.exe",
      "args": [
        "-m",
        "ptb_mcp.server",
        "--transport",
        "stdio"
      ],
      "env": {
        "PYTHONPATH": "D:\\Working\\PTB\\Personal_Task_Board-main"
      }
    }
  }
}
```

---

## 6. Kiểm thử tương tác với MCP Inspector

Bạn có thể kiểm tra trực tiếp toàn bộ 19 công cụ và tài nguyên qua công cụ chính thức của MCP:

```bash
# Chạy Inspector qua stdio
npx @modelcontextprotocol/inspector D:\Working\PTB\Personal_Task_Board-main\.venv\Scripts\python.exe -m ptb_mcp.server --transport stdio

# Hoặc khởi chạy server SSE trước:
.\.venv\Scripts\python.exe -m ptb_mcp.server --transport sse --port 8001
# Sau đó kết nối Inspector tới http://127.0.0.1:8001/sse
```

---

## 7. Lệnh CLI Quản trị MCP

```bash
# Xem JSON cấu hình mẫu cho tất cả các IDE
ptb mcp config

# Xem JSON cấu hình dạng SSE
ptb mcp config --transport sse

# Xem cấu hình chế độ Read-Only
ptb mcp config --read-only

# Tự động phát hiện và cài đặt cấu hình vào các IDE
ptb mcp install --client antigravity
ptb mcp install --client cursor
ptb mcp install --client claude
ptb mcp install --client all

# Khởi chạy server độc lập
ptb mcp serve --transport sse --port 8001
ptb mcp serve --transport stdio
```
