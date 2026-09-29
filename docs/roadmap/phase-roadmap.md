# Lộ Trình Triển Khai Mới: Lean Local-First Roadmap (6 Phase)

## 1. Triết Lý Thiết Kế Lộ Trình Mới

Lộ trình phát triển được điều chỉnh toàn diện theo phương châm **Lean, Local-First & Contract-First Isolation**:
1. **Loại bỏ sự cồng kềnh**: Bỏ hoàn toàn Next.js (thay bằng OpenWebUI), bỏ LangGraph (thay bằng tiến trình Python Ingestion Service tuần tự), bỏ Supabase Cloud / PostgreSQL (hợp nhất vào Single-Store Neo4j).
2. **Ưu tiên giá trị cục bộ trước**: Bắt đầu bằng nguồn dữ liệu ngay trên máy lập trình viên (**Layer 1B: Coding Agent Logs** từ Cursor, Claude Code, Antigravity) để thấy ngay giá trị quản trị cam kết và quyết định kỹ thuật, sau đó mở rộng sang **Layer 1A: Playwright Teams Interception**.
3. **Phát triển tách biệt - Đồng bộ bằng Contract**: Mỗi Layer kiểm thử độc lập 100% bằng mock fixtures từ `packages/contracts`.

---

## 2. Tổng Quan 6 Phase Triển Khai

```mermaid
gantt
    title Lộ Trình Phát Triển Lean Local-First (Personal Task Board)
    dateFormat  YYYY-MM-DD
    section Chuẩn Bị & Nền Tảng
    Phase 0: Shared Contracts & Mocks (Done)   :done, p0, 2026-09-15, 5d
    Phase 1: Neo4j Single-Store & Client       :active, p1, after p0, 4d
    section Thu Thập Dữ Liệu
    Phase 2: Layer 1B - Local Coding Agent Logs :p2, after p1, 5d
    Phase 3: Layer 1A - Playwright Interceptor  :p3, after p2, 6d
    section Xử Lý & Trí Tuệ
    Phase 4: Layer 2 - Python Ingestion Service:p4, after p2, 7d
    section Trải Nghiệm & Tích Hợp
    Phase 5: Layer 4 & 5 - OpenWebUI & FastMCP :p5, after p4, 6d
    Phase 6: Local E2E Verification & Tuning    :p6, after p3 p5, 5d
```

---

## 3. Chi Tiết Từng Phase

---

### Phase 0: Cross-Layer Contracts & Synchronization Backbone (ĐÃ HOÀN THÀNH)

- **Mục tiêu**: Thiết lập cấu trúc Monorepo, đóng băng các hợp đồng dữ liệu Pydantic v2 và bộ mock fixtures.
- **Trạng thái**: ✅ **ĐÃ HOÀN THÀNH (7/7 tests passed)**.
- **Sản phẩm bàn giao**:
  1. Monorepo cấu hình `uv workspace` và `pnpm-workspace`.
  2. Gói `packages/contracts`: Pydantic models C12, C23, C34, C45, C5Ext và TypeScript DTOs.
  3. Bộ Mock Fixtures (`mocks/`): `l1_raw_teams_message.json`, `l1_raw_jira_issue.json`, `l2_extracted_candidates.json`, `l3_tasks_with_context.json`, `l4_today_board_view.json`.

---

### Phase 1: Neo4j Single-Store Setup & Schema Constraints (ĐANG HOÀN THIỆN)

- **Mục tiêu**: Xây dựng nền tảng cơ sở dữ liệu duy nhất Neo4j (Local Docker) quản lý cả Task Board và Graphiti Temporal Memory.
- **Trạng thái**: 🟡 **ĐÃ CÓ NỀN TẢNG (15/15 tests passed)** - Đang tinh chỉnh dọn dẹp phần Supabase cũ.
- **Sản phẩm bàn giao**:
  1. Docker Compose cấu hình Neo4j Community (Port 7474/7687) có sẵn APOC plugin.
  2. `packages/database/neo4j/migrations/001_constraints.cypher`: Ràng buộc tính duy nhất cho Node IDs (`UnifiedTask`, `Person`, `Evidence`, `Decision`...).
  3. `packages/database/src/ptb_database/`:
     - `ontology.py`: Định nghĩa 11 Node Labels và 13 Edge Types cố định.
     - `validator.py`: Tier 2 Application Allowlist Validator ngăn ngừa sai lệch schema.
     - `neo4j_client.py`: Client kết nối Neo4j hỗ trợ connection pool và kiểm tra tính sẵn sàng.
- **Phương thức Test Độc Lập**: Chạy test unit và integration kiểm tra validator và cypher queries với mock fixtures mà không cần Layer 1 và 2.

---

### Phase 2: Layer 1B – Local Coding Agent Logs Ingestion

- **Mục tiêu**: Thu thập các cam kết, quyết định kiến trúc và bài học sửa bug từ lịch sử làm việc của Coding Agents trên máy cá nhân.
- **Thời lượng dự kiến**: 4 - 5 ngày.
- **Sản phẩm bàn giao**:
  1. `services/acquisition/src/watchers/`:
     - `cursor_watcher.py`: Kết nối SQLite `state.vscdb` của Cursor, trích xuất các lượt chat Composer.
     - `claude_code_watcher.py`: Đọc transcripts JSONL từ `~/.claude/projects/`.
     - `antigravity_watcher.py`: Đọc các lượt tương tác từ `brain/transcript.jsonl`.
  2. Bộ lọc turn hội thoại có chứa từ khóa kỹ thuật (Decision, Bug Fix, Task Promise).
  3. Đóng gói thành `RawAgentSessionRecord` (Contract C12).
- **Phương thức Test Độc Lập**: Kiểm thử với file SQLite và JSONL giả lập; xác nhận dữ liệu trích xuất chính xác 100%.

---

### Phase 3: Layer 1A – Playwright Network Interceptor (Teams / Outlook Web)

- **Mục tiêu**: Tự động bắt các gói tin JSON nội bộ từ Teams Web và Outlook Web bằng session browser có sẵn.
- **Thời lượng dự kiến**: 5 - 6 ngày.
- **Sản phẩm bàn giao**:
  1. `services/acquisition/src/playwright/`:
     - `login_helper.py`: Mở trình duyệt để người dùng đăng nhập lần đầu và lưu `storage_state.json`.
     - `teams_interceptor.py`: Chạy Chromium Headless, lắng nghe sự kiện `page.on('response')` để bắt các gói tin `/api/chats/.../messages`.
     - `outlook_interceptor.py`: Bắt các gói tin hội thoại Outlook.
  2. Cơ chế sinh `idempotency_key` chống trùng lặp dữ liệu khi kết nối lại.
  3. Kênh hàng đợi đẩy sự kiện sang Layer 2.
- **Phương thức Test Độc Lập**: Giả lập mạng bằng mock server; kiểm tra khả năng bắt gói tin JSON và đóng gói đúng Contract C12.

---

### Phase 4: Layer 2 – Pure Python Ingestion & Validation Pipeline

- **Mục tiêu**: Xây dựng pipeline xử lý trích xuất công việc, đảm bảo Attribution chính xác tuyệt đối, loại bỏ LangGraph.
- **Thời lượng dự kiến**: 6 - 7 ngày.
- **Sản phẩm bàn giao**:
  1. `services/processing/src/`:
     - `TeamsQuoteReplyParser`: Bóc tách triệt để `quoted_content` vs `actual_content`.
     - `IdentityResolver`: Ánh xạ tài khoản đa tenant về Canonical Person bằng RapidFuzz.
     - `RuleCandidateFilter`: Bộ lọc Regex tiết kiệm 70% chi phí gọi LLM.
     - `StructuredTaskExtractor`: Lời gọi LLM JSON mode kết hợp Pydantic v2.
     - `AttributionValidator`: Kiểm tra chéo author vs owner.
     - `ConfidenceGate`: Lọc 3 mức (<0.50 bỏ qua, 0.50-0.84 vào review queue, >=0.85 tự động ghi).
  2. Mã nguồn ghi trực tiếp vào Neo4j (Cypher Mutations) và Graphiti episodes.
- **Phương thức Test Độc Lập**: Đưa mock raw events từ Phase 0 qua pipeline, xác minh các node `UnifiedTask` và `Evidence` được tạo chính xác trong Neo4j.

---

### Phase 5: Layer 4 & Layer 5 – OpenWebUI Integration & FastMCP Server

- **Mục tiêu**: Cung cấp giao diện tương tác người dùng qua OpenWebUI và bộ 9 công cụ MCP cho Coding Agents.
- **Thời lượng dự kiến**: 5 - 6 ngày.
- **Sản phẩm bàn giao**:
  1. File cấu hình Docker Compose khởi chạy **OpenWebUI Local Docker** (Port 3000).
  2. Bộ **OpenWebUI Custom Tools** (Python scripts):
     - `get_today_tasks`: Trả về danh sách việc theo công thức ưu tiên toán học (0-100).
     - `get_review_queue`: Xem và duyệt các candidate AI trích xuất.
     - `update_task_status`: Đổi trạng thái `TODO` $\rightarrow$ `IN_PROGRESS` $\rightarrow$ `DONE`.
     - `query_project_context`: Tra cứu quyết định kiến trúc và bài học bug từ Neo4j/Graphiti.
  3. `services/mcp/server.py`: FastMCP Server (Port 8000) cung cấp 9 công cụ chuẩn MCP cho Cursor/Claude Code.
- **Phương thức Test Độc Lập**: Sử dụng `@modelcontextprotocol/inspector` kiểm thử 9 công cụ MCP; kiểm thử giao diện OpenWebUI gọi các Custom Tools cục bộ.

---

### Phase 6: Local E2E Verification & System Tuning

- **Mục tiêu**: Chạy thử nghiệm toàn trình trên máy tính cá nhân của người dùng.
- **Thời lượng dự kiến**: 4 - 5 ngày.
- **Nội dung kiểm thử**:
  1. Khởi động toàn bộ cụm: Neo4j + OpenWebUI + Ingestion Service.
  2. Chat thử trên Teams $\rightarrow$ Playwright bắt gói tin $\rightarrow$ Ingestion Service bóc tách $\rightarrow$ Neo4j lưu Task.
  3. Thực hiện phiên code trên Cursor/Antigravity $\rightarrow$ Log Watcher bắt quyết định $\rightarrow$ Graphiti ghi nhận `:Decision`.
  4. Mở OpenWebUI hỏi *"Hôm nay tôi cần làm gì?"* $\rightarrow$ OpenWebUI gọi Tool hiển thị đúng các task từ Teams và Cursor kèm bằng chứng.
  5. Tinh chỉnh trọng số chấm điểm ưu tiên và ngưỡng Confidence Gate.

---

## 4. Bảng Ma Trận Độc Lập Giữa Các Layer

| Layer | Công nghệ chính | Mock Fixture kiểm thử độc lập | Đầu ra chuẩn hóa |
| :--- | :--- | :--- | :--- |
| **Layer 1B: Agent Logs** | Python (`sqlite3`, `jsonlines`) | File `.vscdb` và `.jsonl` mẫu | Contract C12 (`RawAgentSessionRecord`) |
| **Layer 1A: Playwright** | Playwright Chromium Headless | File mock network JSON | Contract C12 (`RawEventRecord`) |
| **Layer 2: Ingestion** | Python, BeautifulSoup, Pydantic | Mock raw events JSON từ Phase 0 | Direct Cypher Mutations & Graphiti |
| **Layer 3: Single-Store** | Neo4j Community (Docker) | Mock Cypher queries & Seed data | Neo4j Graph DB (ACID) |
| **Layer 4: Intelligence** | Python (Math logic) | Mock graph neighborhood JSON | Contract C45 (`TodayBoardView`) |
| **Layer 5: OpenWebUI/MCP**| OpenWebUI Tools & FastMCP | Mock Neo4j driver / Cypher records | Giao diện Chat & 9 MCP Tools |
