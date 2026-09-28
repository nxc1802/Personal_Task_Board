# Personal Task Board (PTB) v1.1

> **Local-First Personal Intelligence System** tự động hợp nhất tasks, commitments và tri thức kỹ thuật từ **Microsoft Teams, Outlook, Jira, Shortcut** và các **Coding Agents (Cursor, Claude Code, Antigravity, Codex)** vào một đồ thị tri thức duy nhất.

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
- [3. Ready-to-Use 5-Step Quickstart](#3-ready-to-use-5-step-quickstart)
- [4. OpenWebUI Board - 8 Views Chuyên Biệt](#4-openwebui-board---8-views-chuyên-biệt)
- [5. FastMCP 10 Read-Only Query Tools cho Coding Agents](#5-fastmcp-10-read-only-query-tools-cho-coding-agents)
- [6. Nguồn Dữ Liệu Thu Thập (Acquisition Layer)](#6-nguồn-dữ-liệu-thu-thập-acquisition-layer)
- [7. Kiểm Thử & CI Matrix](#7-kiểm-thử--ci-matrix)
- [8. Cấu Trúc Dự Án (Monorepo)](#8-cấu-trúc-dự-án-monorepo)

---

## 1. Tổng Quan & Giá Trị Cốt Lõi

Trong môi trường làm việc kỹ thuật hiện đại, công việc và cam kết của lập trình viên bị phân tán trầm trọng giữa:
1. **Kênh trao đổi doanh nghiệp**: Microsoft Teams (chat, mention, tin nhắn kênh), Outlook (emails, calendar meetings).
2. **Hệ thống theo dõi dự án**: Jira Cloud/Server, Shortcut tickets.
3. **Môi trường phát triển & Coding Agents**: Hội thoại và quyết định kỹ thuật từ Cursor IDE, Claude Code CLI, Google Antigravity, OpenAI Codex.

**Personal Task Board (PTB) v1.1** giải quyết bài toán này với triết lý **Local-First Lean & Zero Manual Input**:
- **Bắt tự động, không cần nhập liệu**: Lắng nghe network responses qua Playwright và quét session logs cục bộ từ các Coding Agents.
- **Neo4j Single-Store làm chân lý duy nhất**: Loại bỏ hoàn toàn sự phức tạp của cơ chế Outbox hay phân tán đa cơ sở dữ liệu. Mọi sự kiện thô (`RawEvent`), thực thể công việc (`UnifiedTask`), bằng chứng (`Evidence`), quyết định (`Decision`) và bài học (`Lesson`) đều nằm trong một Graph DB duy nhất.
- **Graphiti Temporal Memory**: Khả năng suy luận ngữ cảnh thời gian thực, liên kết nguyên nhân - kết quả giữa các sự kiện và tự động phát hiện cam kết tồn đọng.
- **Đa giao diện phục vụ song song**:
  - Giao diện người dùng đồ họa trực quan trên **OpenWebUI Board** (`http://127.0.0.1:3000`).
  - Giao thức chuẩn hóa **FastMCP** (`http://127.0.0.1:8001`) cung cấp ngữ cảnh trực tiếp cho Coding Agents.
  - **FastAPI REST API** chuẩn mực (`http://127.0.0.1:8000`).

---

## 2. Kiến Trúc Cốt Lõi (Core Architecture)

```text
                               PERSONAL TASK BOARD v1.1
                            Local-First Intelligence Flow

 ┌────────────────────────────────────────────────────────────────────────┐
 │ Layer 1: Data Acquisition Layer                                        │
 │  • Teams & Outlook Web  ──► Playwright Network Interceptor (Headless)  │
 │  • Coding Agents        ──► Watchers (Cursor, Claude Code, Antigravity)│
 │  • Local Git Repos      ──► Git Commit & Branch Watcher                │
 │  • Jira & Shortcut      ──► REST Polling Adapters                      │
 └───────────────────────────────────┬────────────────────────────────────┘
                                     │ (RawEventRecord / StreamCheckpoint)
                                     ▼
 ┌────────────────────────────────────────────────────────────────────────┐
 │ Layer 2: Authoritative Single-Store (Neo4j Community + APOC)           │
 │  • RawEvent (PENDING -> PROCESSING -> PROCESSED / RETRY / FAILED)      │
 │  • IngestionCheckpoint (Unique tenant + source_type + stream_id)       │
 │  • UnifiedTask, Evidence, Commitment, Person, Project, Customer        │
 │  • Decision, LessonLearned, Incident                                   │
 └──────────────────┬─────────────────────────────────┬───────────────────┘
                    │                                 │
                    ▼                                 ▼
 ┌────────────────────────────────────┐ ┌─────────────────────────────────┐
 │ Layer 3: Processing & Intelligence │ │ Graphiti Memory Engine          │
 │  • ProcessingWorker & Pipeline     │ │  • Temporal Knowledge Episodes  │
 │  • Heuristic & LLM Extraction      │ │  • Entity Resolution & Edges    │
 │  • Task Correlation & Identity     │ │  • Semantic Search & Context    │
 │  • Dynamic Priority Engine         │ │                                 │
 │  • Status Inference (Stale/Waiting)│ │                                 │
 └──────────────────┬─────────────────┘ └─────────────────┬───────────────┘
                    │                                     │
                    └──────────────────┬──────────────────┘
                                       ▼
 ┌────────────────────────────────────────────────────────────────────────┐
 │ Layer 4: Application Layer (ApplicationService & Supervisor)           │
 │  • Unified Facade: Queries, Today Plan, Review Queue, Knowledge Search │
 └──────────────────┬─────────────────────────────────┬───────────────────┘
                    │                                 │
                    ▼                                 ▼
 ┌────────────────────────────────────┐ ┌─────────────────────────────────┐
 │ Layer 5A: FastAPI REST (:8000)     │ │ Layer 5B: FastMCP Server (:8001)│
 │  • /health, /api/tasks             │ │  • 10 Read-Only Query Tools     │
 │  • /api/today, /api/review         │ │  • SSE / Stdio Transport        │
 │  • /api/sources, /api/stats        │ │  • Dành cho Coding Agents       │
 └──────────────────┬─────────────────┘ └─────────────────┬───────────────┘
                    │                                     │
                    ▼                                     ▼
 ┌────────────────────────────────────┐ ┌─────────────────────────────────┐
 │ OpenWebUI Board (:3000)            │ │ Coding Agents                   │
 │  • 8 Dedicated Views & Dashboards  │ │  • Cursor IDE, Claude Code      │
 │  • Live Task Management & Audit    │ │  • Google Antigravity, Codex    │
 └────────────────────────────────────┘ └─────────────────────────────────┘
```

### Các Trụ Cột Kỹ Thuật:
1. **Neo4j Single-Store**: Nơi lưu trữ bền bỉ tập trung duy nhất. Mọi sự kiện đều bắt buộc ghi `RawEvent` vào Neo4j trước khi thực hiện phân tích ngữ nghĩa, đảm bảo độ bền vững (durability) không phụ thuộc RAM queue.
2. **Graphiti-Core Temporal Memory**: Tự động xây dựng đồ thị thời gian từ các `Evidence`, `Decision` và `Lesson`, cho phép truy vết lịch sử quyết định và bối cảnh sự cố.
3. **Process Supervisor**: Quản lý vòng đời đồng thời của Playwright Interceptor, Agent Watchers, ProcessingWorker, FastAPI Server và FastMCP Server trong một process duy nhất với cơ chế graceful shutdown toàn diện.

---

## 3. Ready-to-Use 5-Step Quickstart

Chỉ cần đúng **5 bước chuẩn hóa** để khởi chạy toàn bộ hệ thống từ con số không:

```bash
# Bước 1: Khởi động container Neo4j Single-Store & OpenWebUI
docker compose up -d

# Bước 2: Khởi tạo database constraints & seed data lên Neo4j
ptb init

# Bước 3: Đăng nhập Microsoft 365 (Teams & Outlook Web) lưu session cookie
ptb login microsoft

# Bước 4: Cài đặt PTB Dashboard & Tools vào OpenWebUI
ptb openwebui install

# Bước 5: Chẩn đoán tính toàn vẹn của toàn bộ môi trường và dependencies
ptb doctor
```

Sau khi `ptb doctor` báo `[✓] PASS` toàn bộ, khởi động hệ thống qua Process Supervisor:

```bash
ptb run
```

> **💡 Mẹo:** Nếu chưa cài đặt alias hệ thống `ptb`, bạn có thể thực thi trực tiếp qua `uv run python scripts/ptb_cli.py <lệnh>`.

---

## 4. OpenWebUI Board - 8 Views Chuyên Biệt

Sau khi khởi chạy, truy cập [http://127.0.0.1:3000](http://127.0.0.1:3000) để trải nghiệm giao diện **PTB Board** được thiết kế riêng cho năng suất lập trình:

| STT | View | Icon / Route | Mục Đích Sử Dụng | Tính Năng Nổi Bật |
|:---:|:---|:---:|:---|:---|
| 1 | **Today's Plan** | `📌 #tab-today` | Không gian làm việc tập trung trong ngày | Hiển thị Top 5 tasks ưu tiên cao nhất, điểm priority động, tiến độ hoàn thành, thời gian ước tính, chế độ Focus Timer. |
| 2 | **Inbox / Review** | `📥 #tab-review` | Kiểm duyệt công việc trích xuất tự động | Chứa các task candidate có confidence từ `0.40` đến `0.64`. Cho phép người dùng Approve, Reject hoặc Edit trước khi đưa vào Domain Graph. |
| 3 | **All Tasks** | `📋 #tab-tasks` | Quản lý danh mục công việc tổng thể | Hiển thị dạng Kanban Board & Danh sách bảng; hỗ trợ lọc theo Trạng thái, Dự án, Khách hàng, Nguồn dữ liệu, Mức độ ưu tiên, Deadline. |
| 4 | **Waiting** | `⏳ #tab-waiting` | Theo dõi các tác vụ phụ thuộc bên ngoài | Liệt kê các công việc đang bị block hoặc đang chờ phản hồi từ đồng nghiệp/đối tác (`waiting_for_person`), hỗ trợ nhắc việc kịp thời. |
| 5 | **Forgotten** | `🕰️ #tab-forgotten` | Cảnh báo cam kết bị bỏ quên | Tự động phát hiện các task hoặc cam kết không có cập nhật mới sau `N` ngày (`days_stale >= 3`), ngăn ngừa tình trạng trôi việc. |
| 6 | **Decisions** | `💡 #tab-decisions` | Tra cứu tri thức quyết định kỹ thuật | Lưu trữ các quyết định kiến trúc, công nghệ và quy trình trích xuất từ cuộc trò chuyện và phiên coding của các AI Agents. |
| 7 | **Lessons** | `📖 #tab-lessons` | Sổ tay bài học kinh nghiệm | Tổng hợp bài học đúc kết sau các sự cố, bugs nghiêm trọng hoặc giải pháp tối ưu hệ thống để tái sử dụng trong tương lai. |
| 8 | **Sources / Health** | `🩺 #tab-health` | Giám sát hạ tầng & kết nối | Dashboard giám sát trạng thái thời gian thực của Neo4j, Playwright session (Teams/Outlook), Watchers, Processing Queue và Checkpoints. |

---

## 5. FastMCP 10 Read-Only Query Tools cho Coding Agents

Personal Task Board tích hợp máy chủ **FastMCP** (lắng nghe tại `http://127.0.0.1:8001`), cung cấp đúng **10 công cụ truy vấn chỉ đọc (Read-Only)** chuẩn hóa để các Coding Agent (Cursor, Claude Code, Antigravity) thấu hiểu bối cảnh công việc hiện tại của bạn:

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
| 1 | `get_today_tasks` | `limit: int = 5` | `list[UnifiedTask]` | Giúp Agent nắm bắt ngay 5 đầu việc quan trọng nhất mà lập trình viên cần hoàn thành hôm nay. |
| 2 | `get_tasks` | `filters: dict = None` | `list[UnifiedTask]` | Truy vấn danh sách tasks theo tiêu chí: `status`, `project`, `customer`, `source`, `priority`, v.v. |
| 3 | `get_task_context` | `task_id: str` | `TaskContextGraph` | Trả về mạng lưới ngữ cảnh trọn vẹn: Blockers, Dependents, Evidences liên quan, Decisions và Lessons. |
| 4 | `get_waiting_items` | Không | `list[UnifiedTask]` | Cung cấp danh sách các công việc bạn đang chờ phản hồi để Agent không đề xuất làm việc đang bị nghẽn. |
| 5 | `get_forgotten_commitments` | `days_stale: int = 3` | `list[UnifiedTask]` | Liệt kê các cam kết kỹ thuật bị tồn đọng không có tiến triển để Agent nhắc nhở bạn xử lý. |
| 6 | `get_review_queue` | `limit: int = 10` | `list[CandidateTask]` | Kiểm tra các công việc mới trích xuất đang chờ người dùng xác nhận tính chính xác. |
| 7 | `search_decisions` | `query: str, project_key: str = None` | `list[Decision]` | Tìm kiếm các quyết định kiến trúc trong quá khứ để Agent tuân thủ đúng convention của dự án. |
| 8 | `search_lessons_learned` | `error_or_topic: str` | `list[LessonLearned]` | Tìm kiếm giải pháp đã từng xử lý thành công khi gặp lỗi tương tự trong các lần sự cố trước. |
| 9 | `search_context` | `query: str` | `ContextSearchResult` | Tìm kiếm ngữ nghĩa tổng hợp trên Graphiti Temporal Memory (kết hợp cả Decisions, Lessons & Summaries). |
| 10 | `get_source_health` | Không | `SourcesHealthReport` | Kiểm tra tình trạng kết nối tới các nguồn dữ liệu, session Teams/Outlook và checkpoints thu thập. |

### Cấu hình kết nối cho Coding Agents:
Thêm cấu hình sau vào file cấu hình MCP của Cursor (`mcp.json`) hoặc Claude Code:

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

## 6. Nguồn Dữ Liệu Thu Thập (Acquisition Layer)

Hệ thống hỗ trợ cơ chế thu nạp dữ liệu đa dạng và tự động hóa cao:

- **Microsoft Teams Web**: Sử dụng Playwright Interceptor bắt các bản tin JSON chat nội bộ và activity feed. Không yêu cầu quyền Azure AD Admin của tổ chức.
- **Microsoft Outlook Web**: Đón bắt email công việc và lịch họp quan trọng trong Inbox mà không cần Microsoft Graph API scopes phức tạp.
- **Coding Agents**:
  - **Cursor**: Quét lịch sử hội thoại từ thư mục `workspaceStorage` cục bộ.
  - **Claude Code**: Phân tích các file transcripts và sessions từ `~/.claude/projects`.
  - **Google Antigravity**: Theo dõi phiên làm việc từ `~/.gemini/antigravity`.
  - **OpenAI Codex**: Phân tích lịch sử session từ `~/.codex/sessions`.
- **Local Git**: Tự động theo dõi commit messages, branches và pull requests của các repositories được chỉ định.
- **Jira & Shortcut**: REST API sync định kỳ lấy issues/stories được phân công trực tiếp cho bạn.

---

## 7. Kiểm Thử & CI Matrix

Personal Task Board duy trì chất lượng kiểm thử nghiêm ngặt với bộ test suite toàn trình bao phủ tất cả các tầng:

```bash
# Cài đặt toàn bộ dependencies trong monorepo
uv sync --all-packages

# Chạy toàn bộ 230+ tests tự động
uv run pytest
```

Hệ thống tích hợp **GitHub Actions CI Matrix** kiểm thử tự động trên:
- **Hệ điều hành**: `ubuntu-latest`, `macos-latest`, `windows-latest`
- **Phiên bản Python**: `Python 3.11`, `Python 3.12`, `Python 3.13`
- **Cache tối ưu**: Tăng tốc độ build thông qua `astral-sh/setup-uv@v5` kết hợp cache dependencies đa nền tảng.

---

## 8. Cấu Trúc Dự Án (Monorepo)

```text
Personal_Task_Board/
├── .github/workflows/          # GitHub Actions CI Matrix (ci.yml)
├── config/                     # Cấu hình hệ thống (priority.yaml, sources.yaml, models.yaml)
├── data/                       # Dữ liệu cục bộ (playwright sessions, sqlite caches)
├── docker-compose.yml          # Neo4j Single-Store & OpenWebUI containers
├── docs/                       # Tài liệu kỹ thuật chi tiết (v1.md, v1_1.md)
├── integrations/
│   └── openwebui/              # OpenWebUI Board integration & installer
├── packages/
│   ├── contracts/              # Shared Pydantic contracts & TypeScript types
│   ├── database/               # Neo4j client, schema constraints, repositories
│   └── graph_memory/           # Graphiti-Core memory engine adapter & rebuilders
├── scripts/
│   └── ptb_cli.py              # CLI chính: run, doctor, login, openwebui, init, status, v.v.
├── services/
│   ├── acquisition/            # Layer 1: Playwright interceptors & Watchers
│   ├── application/            # Layer 4: ApplicationService & FastAPI REST (:8000)
│   ├── intelligence/           # Layer 3: Priority Engine & Status Inference Machine
│   ├── mcp/                    # Layer 5B: FastMCP Server (:8001 Read-Only)
│   └── processing/             # Layer 2: RawEvent ProcessingWorker & Correlator
├── tests/                      # E2E & Supervisor integration tests
├── Guideline.md                # Cẩm nang kỹ thuật & hướng dẫn vận hành chi tiết
├── pyproject.toml              # UV workspace config & dependencies
└── README.md                   # Trang giới thiệu sản phẩm & tài liệu tổng quan
```

---

*Phát triển bởi đội ngũ kỹ sư hướng tới môi trường làm việc thông minh, tinh gọn và bảo mật tuyệt đối.*
