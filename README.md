# Personal Task Board (PTB) v1.3

> **Local-First Personal Intelligence System** tự động hợp nhất tasks, commitments và tri thức kỹ thuật từ **Microsoft Teams, Outlook, Jira, Shortcut, Local Git** và các **Coding Agents (Cursor, Claude Code, Antigravity, Codex, Windsurf, Copilot, Continue, Aider, Cline)** vào một đồ thị tri thức duy nhất trên **Neo4j Single-Store**.

[![CI Matrix](https://github.com/nxc1802/Personal_Task_Board/actions/workflows/ci.yml/badge.svg)](https://github.com/nxc1802/Personal_Task_Board/actions/workflows/ci.yml)
![Python 3.11 | 3.12 | 3.13](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)
![Database Neo4j Single-Store](https://img.shields.io/badge/database-Neo4j%20Single--Store-008CC1)
![FastMCP Server](https://img.shields.io/badge/FastMCP-Port%208001%20(Read--Only)-brightgreen)
![FastAPI REST](https://img.shields.io/badge/FastAPI-Port%208000-009688)
![OpenWebUI Board](https://img.shields.io/badge/OpenWebUI-Port%203000-orange)
![License MIT](https://img.shields.io/badge/license-MIT-green)

---

## 📖 Mục Lục

- [1. Tổng Quan & Giá Trị Cốt Lõi](#1-tổng-quan--giá-trị-cốt-lõi)
- [2. Kiến Trúc Cốt Lõi (Core Architecture)](#2-kiến-trúc-cốt-lõi-core-architecture)
- [3. Quy Trình Cài Đặt & Vận Hành Chuẩn Hóa (8 Bước)](#3-quy-trình-cài-đặt--vận-hành-chuẩn-hóa-8-bước)
- [4. Cơ Chế Fail-Fast, Truthful Health & 10 Mã Lỗi Chuẩn Hóa](#4-cơ-chế-fail-fast-truthful-health--10-mã-lỗi-chuẩn-hóa)
- [5. OpenWebUI Board - 8 Views Chuyên Biệt (Live-Only)](#5-openwebui-board---8-views-chuyên-biệt-live-only)
- [6. FastMCP 10 Read-Only Query Tools cho Coding Agents](#6-fastmcp-10-read-only-query-tools-cho-coding-agents)
- [7. Nguồn Dữ Liệu Thu Thập (Acquisition Layer)](#7-nguồn-dữ-liệu-thu-thập-acquisition-layer)
- [8. Kiểm Thử Theo 5 Markers & CI Matrix](#8-kiểm-thử-theo-5-markers--ci-matrix)
- [9. Cấu Trúc Dự Án (Monorepo)](#9-cấu-trúc-dự-án-monorepo)

---

## 1. Tổng Quan & Giá Trị Cốt Lõi

Trong môi trường làm việc kỹ thuật hiện đại, công việc và cam kết của lập trình viên bị phân tán giữa:
1. **Kênh trao đổi doanh nghiệp**: Microsoft Teams (chat, mention, tin nhắn kênh), Outlook (emails, lịch họp).
2. **Hệ thống quản lý dự án**: Jira Cloud/Server, Shortcut stories, Local Git repositories.
3. **Môi trường phát triển & Coding Agents**: Hội thoại và quyết định kỹ thuật từ Cursor IDE, Claude Code CLI, Google Antigravity, OpenAI Codex, Windsurf, GitHub Copilot, Continue, Aider, Cline.

**Personal Task Board (PTB)** giải quyết bài toán này với triết lý **Local-First, Fail-Fast & Zero Manual Input**:
- **Bắt tự động, không cần nhập liệu**: Lắng nghe network responses qua Playwright và quét session logs cục bộ từ các Coding Agents cùng Git, Jira, Shortcut.
- **Neo4j Single-Store làm chân lý duy nhất (Authoritative Store)**: Mọi sự kiện thô (`RawEvent`), con trỏ đồng bộ (`IngestionCheckpoint` với định danh tất định `sha256(tenant_id:source_type:stream_id)` dựa trên composite key 3 thành phần `(tenant_id, source_type, stream_id)`), thực thể công việc (`UnifiedTask`), bằng chứng (`Evidence`), quyết định (`Decision`) và bài học (`Lesson`) đều được lưu trữ bền bỉ trong một Graph DB duy nhất.
- **Chuỗi Xử Lý Tự Động Khép Kín (Vertical Slice)**: Khi `RawEvent` được ghi nhận, `ProcessingWorker` tự động trích xuất và hợp nhất thành `UnifiedTask`, kích hoạt `TaskIntelligenceLifecycle` tính toán điểm ưu tiên và trạng thái suy luận, đồng thời đồng bộ bất đồng bộ sang **Graphiti Temporal Memory** qua `GraphMemorySyncWorker` (theo dõi `GraphSyncStatus`: PENDING -> SYNCING -> SYNCED / RETRY / FAILED mà không khóa hay rollback giao dịch Neo4j).
- **Shared Runtime Dependency Graph**: `PTBProcessSupervisor` khởi tạo một đồ thị phụ thuộc dùng chung duy nhất (`Neo4jClient` → `Repositories` → `ProcessingPipeline` → `TaskIntelligenceLifecycle` → `GraphitiMemoryClient` → `ApplicationService`) và tiêm trực tiếp vào cả **FastAPI REST API** (`http://127.0.0.1:8000`) và **FastMCP Server** (`http://127.0.0.1:8001`).

---

## 2. Kiến Trúc Cốt Lõi (Core Architecture)

```text
                                PERSONAL TASK BOARD
                         Local-First Authoritative Flow

 ┌────────────────────────────────────────────────────────────────────────┐
 │ Layer 1: Data Acquisition Layer (Supervised Independent Polling)       │
 │  • Teams & Outlook Web  ──► Playwright Network Interceptor (Headless)  │
 │  • Coding Agents        ──► Watchers (Cursor, Claude Code, Antigravity)│
 │  • Local Git Repos      ──► Git Commit & Branch Watcher                │
 │  • Jira & Shortcut      ──► REST Polling Adapters                      │
 └───────────────────────────────────┬────────────────────────────────────┘
                                     │ (RawEventRecord / Deterministic Checkpoint)
                                     ▼
 ┌────────────────────────────────────────────────────────────────────────┐
 │ Layer 2: Authoritative Single-Store (Neo4j Community + APOC)           │
 │  • RawEvent (PENDING -> PROCESSING -> PROCESSED / RETRY / FAILED)      │
 │  • IngestionCheckpoint (sha256(tenant_id:source_type:stream_id))       │
 │  • UnifiedTask, Evidence, Commitment, Person, Project, Customer        │
 │  • Decision, LessonLearned, StatusTransitionAudit, MergeAudit          │
 └──────────────────┬─────────────────────────────────┬───────────────────┘
                    │                                 │
                    ▼                                 ▼
 ┌────────────────────────────────────┐ ┌─────────────────────────────────┐
 │ Layer 3: Processing & Intelligence │ │ Graphiti Memory Engine          │
 │  • ProcessingWorker & Pipeline     │ │  • GraphMemorySyncWorker (Async)│
 │  • LLM Structured Extraction       │ │  • GraphSyncStatus (PENDING ->  │
 │  • Task Correlation & Merge        │ │    SYNCING -> SYNCED / RETRY)   │
 │  • Auto-Wired IntelligenceLifecycle│ │  • Temporal Knowledge Episodes  │
 │  • Priority & Status Inference     │ │  • Semantic Search & Context    │
 └──────────────────┬─────────────────┘ └─────────────────┬───────────────┘
                    │                                     │
                    └──────────────────┬──────────────────┘
                                       ▼
 ┌────────────────────────────────────────────────────────────────────────┐
 │ Layer 4: Shared Application Layer (ApplicationService & Supervisor)    │
 │  • Single Shared Instance Injected into REST & MCP Transports          │
 └──────────────────┬─────────────────────────────────┬───────────────────┘
                    │                                 │
                    ▼                                 ▼
 ┌────────────────────────────────────┐ ┌─────────────────────────────────┐
 │ Layer 5A: FastAPI REST (:8000)     │ │ Layer 5B: FastMCP Server (:8001)│
 │  • Deep /health, /api/tasks        │ │  • Deep /health & 10 Read-Only  │
 │  • /api/today, /api/review         │ │    Query Tools (SSE / Stdio)    │
 │  • /api/sources,Real Mutations Only│ │  • Dành cho Coding Agents       │
 └──────────────────┬─────────────────┘ └─────────────────┬───────────────┘
                    │                                     │
                    ▼                                     ▼
 ┌────────────────────────────────────┐ ┌─────────────────────────────────┐
 │ OpenWebUI Board (:3000)            │ │ Coding Agents                   │
 │  • 8 Live-Only Views & Dashboards  │ │  • Cursor IDE, Claude Code      │
 │  • Self-Contained Plugins (PTB-001)│ │  • Google Antigravity, Codex    │
 │  • host.docker.internal:8000       │ │  • Windsurf, Copilot, etc.      │
 └────────────────────────────────────┘ └─────────────────────────────────┘
```

---

## 3. Quy Trình Cài Đặt & Vận Hành Chuẩn Hóa (8 Bước)

Hệ thống tuân thủ quy trình cài đặt và vận hành chính thức gồm **8 bước chuẩn hóa**:

### Bước 1: Install (Cài đặt dependencies & trình duyệt Chromium)
```bash
uv sync --all-packages
uv run playwright install chromium
```

### Bước 2: Configure (Thiết lập cấu hình hệ thống)
Sao chép và tinh chỉnh các tệp cấu hình môi trường, nguồn thu thập, trọng số ưu tiên và mô hình LLM:
```bash
cp .env.example .env
cp config/models.example.yaml config/models.yaml
# Kiểm tra và tùy chỉnh thêm:
# - .env (NEO4J_URI, NEO4J_PASSWORD, LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, ...)
# - config/sources.yaml (bật/tắt các nguồn Teams, Outlook, Coding Agents, Git, Jira, Shortcut)
# - config/priority.yaml (trọng số tính điểm ưu tiên và ngưỡng confidence)
# - config/models.yaml (cấu hình LLM provider và vector embeddings)
```

### Bước 3: Neo4j Available (Khởi động hạ tầng container)
Khởi chạy container Neo4j Single-Store (`127.0.0.1:7687`) và OpenWebUI (`127.0.0.1:3000` gắn kết trực tiếp thư mục `./data/openwebui`):
```bash
docker compose up -d
```

### Bước 4: `ptb init` (Khởi tạo Neo4j Constraints & Schema)
Thiết lập toàn bộ Uniqueness Constraints, Indexes và cấu trúc đồ thị trên Neo4j:
```bash
ptb init
```

### Bước 5: `ptb login microsoft` (Xác thực phiên Microsoft 365)
Mở trình duyệt Chromium ở chế độ đồ họa để đăng nhập Microsoft Teams & Outlook Web, lưu phiên xác thực vào `data/playwright/storage_state.json`:
```bash
ptb login microsoft
```

### Bước 6: `ptb openwebui install` (Triển khai giao diện và công cụ vào OpenWebUI)
Cài đặt trực tiếp PTB Tools, Functions và giao diện Board vào thư mục `./data/openwebui`:
```bash
ptb openwebui install
```

### Bước 7: `ptb doctor` / `ptb doctor --deep` (Chẩn đoán toàn diện hệ thống)
Kiểm tra tính sẵn sàng của Python Runtime, Docker Daemon, Neo4j Database (`:7687`), Playwright Chromium, OpenWebUI (`:3000`) và cấu hình LLM Provider:
```bash
# Kiểm tra nhanh cấu hình và kết nối hạ tầng
ptb doctor

# Kiểm tra chuyên sâu có xác thực kết nối thực tế tới endpoint /models của LLM Provider
ptb doctor --deep
```

### Bước 8: `ptb run` (Khởi chạy Process Supervisor)
Khởi chạy bộ điều phối trung tâm giám sát toàn bộ hệ thống (Playwright Interceptor, Coding Agent Watchers, Git/Jira/Shortcut Adapters, ProcessingWorker, Application REST API tại `127.0.0.1:8000` và FastMCP Server tại `127.0.0.1:8001`):
```bash
ptb run
```

> **💡 Mẹo:** Nếu chưa khai báo alias `ptb` trong shell, bạn có thể gọi trực tiếp qua `uv run python scripts/ptb_cli.py <lệnh>`.

---

## 4. Cơ Chế Fail-Fast, Truthful Health & 10 Mã Lỗi Chuẩn Hóa

Personal Task Board vận hành theo nguyên tắc **Fail-Fast & Truthful Health**: khi cơ sở dữ liệu hoặc dịch vụ phụ thuộc gặp sự cố, hệ thống ghi nhận log lỗi có cấu trúc (`BugLogRecord`), báo cáo đúng trạng thái sức khỏe thực tế và tuyệt đối không trả về dữ liệu giả hay thành công ảo.

### 4.1 Cơ Chế Truthful Health & Readiness States

1. **Application & MCP Deep Health (`GET /health` trên `:8000` và `:8001`)**:
   - `healthy`: Toàn bộ thành phần cốt lõi (`neo4j`, `processing_worker`, `graphiti`, `llm`, `playwright`) hoạt động bình thường.
   - `degraded`: Kho dữ liệu chính Neo4j vẫn hoạt động tốt nhưng phân hệ phụ trợ (`graphiti`, `llm`, hoặc `processing_worker`) gặp sự cố giảm cấp.
   - `not_ready`: Thành phần cốt lõi (`neo4j` hoặc `processing_worker`) ngừng hoạt động.
2. **Process Supervisor Banner States (`ptb run`)**:
   - `READY`: Neo4j, Application REST (`:8000/health`), FastMCP (`:8001/health`), ProcessingWorker và phiên thu thập đều hoạt động hoàn chỉnh.
   - `READY_WITH_WARNINGS`: Các dịch vụ cốt lõi (Neo4j, REST, MCP, Worker, Watchers) đã sẵn sàng, nhưng phiên Microsoft 365 chưa đăng nhập (`AUTH_REQUIRED` — cần chạy `ptb login microsoft`).
   - `DEGRADED`: Khi nguồn Microsoft được cấu hình bắt buộc (`strict_required: true`) mà chưa có phiên hợp lệ, hoặc khi một phân hệ phụ trợ bị suy giảm.
   - `NOT_READY`: Khi Neo4j không thể kết nối hoặc REST/MCP/Worker không vượt qua kiểm tra khởi động — Supervisor lập tức hủy khởi chạy với mã thoát `exit != 0`.
3. **Trạng Thái Nguồn Thu Thập (`/api/sources` & `get_source_health`)**:
   - Phản ánh trung thực 9 trạng thái chuẩn hóa của `SourceSyncState`:
     - `NEVER_SYNCED`: Nguồn đã bật nhưng chưa từng có checkpoint nào được lưu (tuyệt đối không báo `HEALTHY` giả tạo khi chưa có dữ liệu).
     - `UNCONFIGURED`: Nguồn chưa được cung cấp thông tin cấu hình hoặc API token cần thiết.
     - `AUTH_REQUIRED`: Phiên làm việc Microsoft Teams/Outlook chưa được xác thực hoặc đã hết hạn (cần chạy `ptb login microsoft`).
     - `DEGRADED`: Nguồn gặp sự cố tạm thời hoặc đang trong chu kỳ exponential backoff sau khi ghi nhận `PTB-L1-002`.
     - `HEALTHY`: Nguồn hoạt động ổn định và có checkpoint hợp lệ được cập nhật trong ngưỡng thời gian cho phép.
     - `DISABLED`: Nguồn bị tắt trong cấu hình `config/sources.yaml`.
     - `NOT_INSTALLED`: Coding Agent không tìm thấy đường dẫn cài đặt trên máy cục bộ (tránh tạo false positive lỗi).
     - `STARTING`: Nguồn đang trong quá trình khởi tạo kết nối ban đầu.
     - `ERROR`: Nguồn gặp lỗi nghiêm trọng không thể tự phục hồi sau các lượt retry.

### 4.2 Bảng 10 Mã Lỗi Chuẩn Hóa (`BugCode`)

Mỗi sự cố hệ thống đều được phát xạ qua `log_bug()` với đầy đủ trường ngữ cảnh (`timestamp`, `bug_code`, `severity`, `subsystem`, `message`, `exception_type`, `raw_event_id`, `task_id`, `source_type`, `tenant_id`):

| STT | Mã Lỗi (`BugCode`) | Phân Hệ (`subsystem`) | Mô Tả Sự Cố | Hành Vi Hệ Thống (Fail-Fast / Truthful Health) |
|:---:|:---|:---|:---|:---|
| 1 | `PTB-STORAGE-001` | `neo4j` | Neo4j authoritative store unavailable | Kích hoạt **Fail-Fast**: chuyển Supervisor sang `NOT_READY` và dừng tiến trình (`exit != 0`). |
| 2 | `PTB-CKPT-001` | `checkpoint` | Checkpoint read/write inconsistency | Ghi nhận lỗi đồng bộ con trỏ `IngestionCheckpoint` (`sha256(tenant_id\|source_type\|stream_id)`) và giữ nguyên tính toàn vẹn luồng thu thập. |
| 3 | `PTB-L1-001` | `playwright` | Playwright authentication expired | Phát hiện HTTP `401`/`403` từ Teams/Outlook, chuyển trạng thái phiên sang `AUTH_EXPIRED` / `AUTH_REQUIRED`, dừng capture an toàn và yêu cầu `ptb login microsoft`. |
| 4 | `PTB-L1-002` | `playwright` / `adapter_*` | Playwright capture/persist failure | Lỗi thu thập mạng hoặc đồng bộ adapter tại Layer 1; cô lập lỗi và thực hiện retry với exponential backoff độc lập cho từng nguồn. |
| 5 | `PTB-L2-001` | `processing` | Processing pipeline failure | Lỗi xử lý `RawEvent` tại Layer 2; cập nhật trạng thái sự kiện sang `RETRY` (tăng `attempt_count` đúng 1 lần mỗi lượt) hoặc `FAILED` khi chạm ngưỡng tối đa. |
| 6 | `PTB-LLM-001` | `llm` | LLM provider unavailable | Thiếu khóa API, sai cấu hình hoặc không kết nối được LLM endpoint; đánh dấu trạng thái LLM/Processing là `DEGRADED` hoặc `NOT_READY`. |
| 7 | `PTB-GRAPH-001` | `graph_memory` | Graphiti unavailable | Lỗi kết nối hoặc đồng bộ Graphiti Temporal Memory; bảo toàn `UnifiedTask`/`Evidence` trong Neo4j, đánh dấu `graph_sync_status=RETRY`/`FAILED` và chuyển `graphiti` sang `degraded`. |
| 8 | `PTB-APP-001` | `application` | Application dependency unhealthy | Thành phần phụ thuộc trọng yếu của `ApplicationService` (như Neo4j) mất kết nối; endpoint `GET /health` trả về `status: "not_ready"`. |
| 9 | `PTB-MCP-001` | `mcp` | MCP server unhealthy | Máy chủ FastMCP hoặc kết nối tới `ApplicationService` dùng chung gặp lỗi; endpoint `GET /health` của MCP báo cáo không khỏe mạnh. |
| 10 | `PTB-OWUI-001` | `openwebui` | OpenWebUI cannot reach Application API | Giao diện OpenWebUI Board hoặc Tools không gọi được REST API (`127.0.0.1:8000`); hiển thị rõ thông báo lỗi `APPLICATION SERVICE OFFLINE` tới người dùng. |

---

## 5. OpenWebUI Board - 8 Views Chuyên Biệt (Live-Only)

Sau khi khởi chạy, truy cập [http://127.0.0.1:3000](http://127.0.0.1:3000) để sử dụng giao diện **PTB Board**. Mọi dữ liệu và thao tác trên bảng điều khiển đều kết nối trực tiếp tới REST API thực (`127.0.0.1:8000`):

| STT | View | Icon / Route | Mục Đích Sử Dụng | Tính Năng & Thao Tác Thực Tế |
|:---:|:---|:---:|:---|:---|
| 1 | **Today's Plan** | `📌 #tab-today` | Không gian làm việc tập trung trong ngày | Hiển thị Top tasks ưu tiên cao nhất từ `/api/today`, điểm priority động (0–100), tiến độ hoàn thành, thời hạn. |
| 2 | **Inbox / Review** | `📥 #tab-review` | Kiểm duyệt công việc trích xuất tự động | Chứa các task candidate có confidence từ `0.40` đến `0.65` (`/api/review`). Hỗ trợ gọi API thực để **Approve** (`/api/review/{id}/approve`), **Dismiss** (`/api/review/{id}/dismiss`) hoặc **Edit** (`PATCH /api/tasks/{id}`). |
| 3 | **All Tasks** | `📋 #tab-tasks` | Quản lý danh mục công việc tổng thể | Hiển thị danh sách công việc thực tế (`/api/tasks`), hỗ trợ lọc theo trạng thái (`TODO`, `IN_PROGRESS`, `BLOCKED`, `DONE`, `DISMISSED`), dự án, khách hàng, nguồn dữ liệu và tách task (`POST /api/tasks/{id}/split`). |
| 4 | **Waiting** | `⏳ #tab-waiting` | Theo dõi các tác vụ phụ thuộc bên ngoài | Liệt kê các công việc đang bị chặn hoặc chờ phản hồi từ đồng nghiệp/đối tác (`/api/waiting`). |
| 5 | **Forgotten** | `🕰️ #tab-forgotten` | Cảnh báo cam kết bị bỏ quên | Tự động phát hiện các task hoặc cam kết không có cập nhật mới quá số ngày quy định (`/api/forgotten`). |
| 6 | **Decisions** | `💡 #tab-decisions` | Tra cứu tri thức quyết định kỹ thuật | Tìm kiếm các quyết định kiến trúc, công nghệ và quy trình trích xuất từ hội thoại và phiên làm việc (`/api/decisions`). |
| 7 | **Lessons** | `📖 #tab-lessons` | Sổ tay bài học kinh nghiệm | Tổng hợp bài học đúc kết sau các sự cố hoặc quá trình gỡ lỗi kỹ thuật (`/api/lessons`). |
| 8 | **Sources / Health** | `🩺 #tab-health` | Giám sát hạ tầng & kết nối | Dashboard giám sát trạng thái thực của từng nguồn dữ liệu (`/api/sources`) và sức khỏe hệ thống (`/health`). Khi mất kết nối tới backend, hiển thị cảnh báo `APPLICATION SERVICE OFFLINE` (`PTB-OWUI-001`). |

### 5.1 Kiến Trúc Plugins Tự Chứa (Self-Contained Plugins)
- **Không phụ thuộc Monorepo**: Các plugins tích hợp (`integrations/openwebui/tools/ptb_tools.py` và `integrations/openwebui/functions/ptb_board_action.py`) được đóng gói hoàn toàn **self-contained**, không `import ptb_contracts` hay bất kỳ thư viện nội bộ nào của dự án, cho phép nạp và chạy trơn tru trong container Python độc lập của OpenWebUI.
- **Structured Error Logging**: Ghi nhận sự cố trực tiếp ra standard error có cấu trúc kèm mã lỗi chuẩn `[PTB-OWUI-001]` khi không kết nối được tới backend.
- **Định tuyến mạng nhất quán**:
  - OpenWebUI Plugins chạy trong Docker container kết nối tới Application Service qua `http://host.docker.internal:8000`.
  - Giao diện Board chạy phía trình duyệt người dùng kết nối qua `http://127.0.0.1:8000`.

---

## 6. FastMCP 10 Read-Only Query Tools cho Coding Agents

Personal Task Board tích hợp máy chủ **FastMCP** (lắng nghe tại `http://127.0.0.1:8001` kèm endpoint kiểm tra sức khỏe `http://127.0.0.1:8001/health`), dùng chung instance `ApplicationService` với REST API và cung cấp đúng **10 công cụ truy vấn chỉ đọc (Read-Only)** cho các Coding Agents:

```text
 ┌─────────────────────────────────────────────────────────────┐
 │                FastMCP Server (:8001 / SSE)                 │
 ├──────────────────────────────┬──────────────────────────────┤
 │ 1. get_today_tasks           │ 6. get_review_queue          │
 │ 2. get_tasks                 │ 7. search_decisions          │
 │ 3. get_task_context          │ 8. search_lessons_learned    │
 │ 4. get_waiting_items         │ 9. search_context            │
 │ 5. get_forgotten_commitments │ 10. get_source_health        │
 └──────────────────────────────┴──────────────────────────────┘
```

### Bảng Chi Tiết 10 Read-Only Tools:

| STT | Tên Tool | Tham Số Đầu Vào | Định Dạng Dữ Liệu Trả Về | Tác Vụ Dành Cho Coding Agent |
|:---:|:---|:---|:---|:---|
| 1 | `get_today_tasks` | `limit: int = 5` | `list[UnifiedTask]` | Giúp Agent nắm bắt ngay các đầu việc quan trọng nhất mà lập trình viên cần hoàn thành hôm nay. |
| 2 | `get_tasks` | `filters: dict = None` | `list[UnifiedTask]` | Truy vấn danh sách tasks theo tiêu chí: `status`, `project`, `customer`, `source`, `priority`, v.v. |
| 3 | `get_task_context` | `task_id: str` | `TaskContextGraph` | Trả về mạng lưới ngữ cảnh trọn vẹn: Blockers, Dependents, Evidences liên quan, Decisions và Lessons. |
| 4 | `get_waiting_items` | Không | `list[UnifiedTask]` | Cung cấp danh sách các công việc đang chờ phản hồi để Agent biết các điểm nghẽn hiện tại. |
| 5 | `get_forgotten_commitments` | `days_stale: int = 3` | `list[UnifiedTask]` | Liệt kê các cam kết kỹ thuật bị tồn đọng không có tiến triển để Agent nhắc nhở xử lý. |
| 6 | `get_review_queue` | `limit: int = 10` | `list[CandidateTask]` | Kiểm tra các công việc mới trích xuất đang chờ người dùng xác nhận. |
| 7 | `search_decisions` | `query: str, project_key: str = None` | `list[Decision]` | Tìm kiếm các quyết định kiến trúc trong quá khứ để Agent tuân thủ đúng quy ước của dự án. |
| 8 | `search_lessons_learned` | `error_or_topic: str` | `list[LessonLearned]` | Tìm kiếm giải pháp đã từng xử lý thành công khi gặp lỗi tương tự trong các lần sự cố trước. |
| 9 | `search_context` | `query: str` | `ContextSearchResult` | Tìm kiếm ngữ nghĩa tổng hợp trên Graphiti Temporal Memory (kết hợp cả Decisions, Lessons & Evidences). |
| 10 | `get_source_health` | Không | `SourcesHealthReport` | Kiểm tra tình trạng thực tế của các nguồn dữ liệu, phiên Microsoft 365 và checkpoints thu thập. |

### Cấu hình kết nối cho Coding Agents:
Thêm cấu hình sau vào file cấu hình MCP của Cursor (`mcp.json`), Claude Code hoặc Antigravity:

```json
{
  "mcpServers": {
    "personal-task-board": {
      "url": "http://127.0.0.1:8001/sse"
    }
  }
}
```

---

## 7. Nguồn Dữ Liệu Thu Thập (Acquisition Layer)

Hệ thống thu nạp dữ liệu tự động qua `PTBProcessSupervisor` với cơ chế retry và exponential backoff độc lập cho từng nguồn:

- **Microsoft Teams Web**: Playwright Interceptor bắt các bản tin JSON chat nội bộ và activity feed; tự động phát hiện hết hạn phiên (`401`/`403` → `PTB-L1-001` → `AUTH_EXPIRED`).
- **Microsoft Outlook Web**: Đón bắt email công việc và lịch họp quan trọng trong Inbox qua Playwright Interceptor.
- **Coding Agents (9 công cụ hỗ trợ đa nền tảng qua `platformdirs`)**:
  - **Cursor**: Quét lịch sử từ `workspaceStorage` trên macOS, Linux, Windows.
  - **Claude Code**: Phân tích các file transcripts và sessions từ `~/.claude/projects` & `~/.claude/transcripts`.
  - **Google Antigravity**: Theo dõi phiên làm việc từ `~/.gemini/antigravity`.
  - **OpenAI Codex**: Phân tích lịch sử session từ `~/.codex/sessions`.
  - **Windsurf, GitHub Copilot, Continue, Aider, Cline / Roo Code**: Tự động phân giải đường dẫn theo hệ điều hành; nếu công cụ chưa được cài đặt trên máy sẽ báo cáo trạng thái `NOT_INSTALLED` (không gây lỗi giả).
- **Local Git**: Tự động theo dõi commit messages và branches của các repositories được cấu hình trong `config/sources.yaml`.
- **Jira & Shortcut**: Đồng bộ định kỳ qua REST API lấy issues/stories được phân công.

---

## 8. Kiểm Thử Theo 5 Markers & CI Matrix

Bộ kiểm thử của Personal Task Board được phân tách chuẩn hóa thành **5 pytest markers** giúp xác thực toàn diện logic, hợp đồng giao tiếp và luồng E2E ngay trên máy cục bộ mà không phụ thuộc vào Docker:

| Marker | Mục Đích Kiểm Thử | Yêu Cầu Docker? |
|:---|:---|:---:|
| `unit` | Kiểm thử đơn vị cho từng thành phần logic miền cô lập (Priority, Status Inference, Parsers, Watchers). | Không |
| `contract` | Kiểm thử hợp đồng Pydantic/TypeScript/Cypher schema, tính toàn vẹn mã lỗi `BugCode` và quy chuẩn mã nguồn. | Không |
| `fixture_e2e` | Kiểm thử luồng dọc toàn trình (Teams/Outlook fixture → Acquisition → ProcessingWorker → Intelligence → GraphSync → FastAPI REST) sử dụng kho dữ liệu kiểm thử tại `tests/support/`. | Không |
| `runtime_smoke` | Kiểm thử khởi chạy `PTBProcessSupervisor`, gắn cổng REST/MCP, vòng lặp Worker, chia sẻ `ApplicationService`, cơ chế Fail-Fast khi thiếu Neo4j và tắt tiến trình an toàn (graceful shutdown). | Không |
| `external_integration` | Kiểm thử tích hợp trực tiếp với hạ tầng container thực tế (Neo4j Single-Store, OpenWebUI). | **Có** |

### Các Lệnh Chạy Kiểm Thử Chuẩn:

```bash
# 1. Chạy toàn bộ test suite non-docker (370+ tests đạt 100% PASS, không cần Docker):
uv run pytest -m "not external_integration"

# 2. Kiểm tra bộ 14 behavioral release freeze gates (14/14 PASS):
uv run pytest tests/test_release_readiness.py -v

# 3. Chạy riêng từng nhóm kiểm thử theo marker:
uv run pytest -m unit
uv run pytest -m contract
uv run pytest -m fixture_e2e
uv run pytest -m runtime_smoke

# 4. Chạy kiểm thử tích hợp hạ tầng thực tế (yêu cầu đã bật docker compose up -d):
uv run pytest -m external_integration
```

Hệ thống tích hợp **GitHub Actions CI Matrix** tự động thực thi `uv run pytest -m "not external_integration"` trên:
- **Hệ điều hành**: `ubuntu-latest`, `macos-latest`, `windows-latest`
- **Phiên bản Python**: `Python 3.11`, `Python 3.12`, `Python 3.13`

---

## 9. Cấu Trúc Dự Án (Monorepo)

```text
Personal_Task_Board/
├── .github/workflows/          # GitHub Actions CI Matrix (ci.yml)
├── config/                     # Cấu hình hệ thống (priority.yaml, sources.yaml, models.example.yaml)
├── data/                       # Dữ liệu cục bộ (playwright sessions, ./data/openwebui bind mount)
├── docker-compose.yml          # Neo4j Single-Store (:7687) & OpenWebUI (:3000) containers
├── docs/                       # Tài liệu kiến trúc và kế hoạch phát hành (v1.md, v1_1.md, v1_2.md)
├── integrations/
│   └── openwebui/              # OpenWebUI Board (Live-Only), Tools, Functions & Installer
├── packages/
│   ├── contracts/              # Shared Pydantic contracts, BugCode/log_bug & TypeScript types
│   ├── database/               # Neo4j client, schema constraints, authoritative repositories
│   └── graph_memory/           # Graphiti-Core memory client, GraphMemoryWorker & rebuilders
├── scripts/
│   └── ptb_cli.py              # CLI & Process Supervisor: init, login, openwebui, doctor, run, status...
├── services/
│   ├── acquisition/            # Layer 1: Playwright interceptors, 9 Agent Watchers, Git/Jira/Shortcut
│   ├── application/            # Layer 4 & 5A: ApplicationService & FastAPI REST (:8000)
│   ├── intelligence/           # Layer 3: Priority Engine, Status Inference & Auto-Wired Lifecycle
│   ├── mcp/                    # Layer 5B: FastMCP Server (:8001 Read-Only & /health)
│   └── processing/             # Layer 2: RawEvent ProcessingWorker, LLM Readiness & Correlator
├── tests/                      # Bộ kiểm thử 5 markers & test repositories cô lập tại tests/support/
├── Guideline.md                # Cẩm nang kỹ thuật & hướng dẫn vận hành chi tiết (Source-of-Truth)
├── pyproject.toml              # UV workspace config & pytest markers configuration
└── README.md                   # Trang giới thiệu sản phẩm & tài liệu tổng quan
```

---

*Phát triển bởi đội ngũ kỹ sư hướng tới môi trường làm việc thông minh, minh bạch, tinh gọn và bảo mật tuyệt đối.*
