# Hướng Dẫn Thiết Lập & Vận Hành (Local-First Lean Edition)

Tài liệu hướng dẫn cài đặt, cấu hình môi trường và vận hành hệ thống **Personal Task Board** theo kiến trúc **Local-First Lean Edition** (Single-Store Neo4j, Layer 1A Playwright, Layer 1B Coding Agent Watchers).

---

## 1. Cấu Hình Môi Trường (.env)

Hệ thống hoạt động hoàn toàn cục bộ, không phụ thuộc vào Supabase hay Neo4j Aura Cloud.

Tạo file `.env` từ `.env.example`:

```bash
cp .env.example .env
```

Các biến môi trường cơ bản:
```env
# Neo4j Single-Store (Khởi chạy bằng Docker Compose)
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=personal_task_board_secret_2026

# LLM Local (Ollama) hoặc OpenAI-compatible API
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=qwen2.5:7b
```

---

## 2. Khởi Động Neo4j Single-Store Bằng Docker

Khởi chạy container Neo4j Community (đã tích hợp APOC plugin):

```bash
docker compose up -d
```

* Neo4j Browser UI: [http://localhost:7474](http://localhost:7474) (User: `neo4j` / Password: `personal_task_board_secret_2026`)
* Bolt Protocol: `bolt://localhost:7687`

---

## 3. Cài Đặt Dependencies & Khởi Tạo Schema

### 3.1 Cài đặt packages trong Monorepo
```bash
# Cài đặt virtual environment và packages
uv venv
source .venv/bin/activate  # Trên macOS/Linux
# hoặc: .venv\Scripts\activate  # Trên Windows

# Cài đặt các package nội bộ ở chế độ editable
uv pip install -e packages/contracts -e packages/database -e services/acquisition pytest pytest-asyncio
```

### 3.2 Khởi tạo Constraints & Seed Data trên Neo4j
```bash
uv run python -m ptb_database.init_neo4j
```

Lệnh trên sẽ:
1. Kiểm tra kết nối tới container Neo4j cục bộ.
2. Thiết lập 11 Uniqueness Constraints & 4 Indexes cho Fixed Ontology (`UnifiedTask`, `Person`, `Evidence`, `Decision`...).
3. Xác minh tính sẵn sàng của cơ sở dữ liệu.

---

## 4. Vận Hành Layer 1 (Data Acquisition)

### 4.1 Layer 1B: Local Coding Agent Logs Ingestion
Thu thập lịch sử hội thoại, các cam kết kỹ thuật và quyết định kiến trúc từ các Coding Agent đang chạy trên máy của bạn:

```python
from ptb_acquisition.watchers import CursorWatcher, ClaudeCodeWatcher, AntigravityWatcher

# Quét Cursor
cursor_records = CursorWatcher().scan_sessions()
print(f"Trích xuất {len(cursor_records)} turn từ Cursor.")

# Quét Claude Code
claude_records = ClaudeCodeWatcher().scan_sessions()
print(f"Trích xuất {len(claude_records)} turn từ Claude Code.")

# Quét Antigravity IDE
antigravity_records = AntigravityWatcher().scan_sessions()
print(f"Trích xuất {len(antigravity_records)} turn từ Antigravity.")
```

### 4.2 Layer 1A: Playwright Network Interceptor (Teams & Outlook Web)
Bắt trực tiếp các gói tin JSON nội bộ từ Teams Web và Outlook Web:

* **Bước 1: Đăng nhập lần đầu (Interactive Login)**
  ```python
  import asyncio
  from ptb_acquisition.playwright import PlaywrightOrchestrator

  async def login():
      orch = PlaywrightOrchestrator(headless=False)
      await orch.login_interactive()

  asyncio.run(login())
  ```
  Trình duyệt Chromium sẽ mở ra để bạn đăng nhập tài khoản và hoàn tất MFA. Sau khi xong, cookie và token sẽ được lưu vào file `data/playwright/storage_state.json`.

* **Bước 2: Chạy Daemon bắt tin nhắn ngầm (Headless Mode)**
  ```python
  import asyncio
  from ptb_acquisition.queue import LocalIngestionQueue
  from ptb_acquisition.playwright import PlaywrightOrchestrator

  async def run():
      queue = LocalIngestionQueue()
      orch = PlaywrightOrchestrator(queue=queue, headless=True)
      await orch.start_interceptor()

  asyncio.run(run())
  ```

---

## 5. Kiểm Thử Hệ Thống (Test Verification)

Chạy bộ test suite toàn trình trên toàn bộ packages và services:

```bash
uv run pytest
```

Kỳ vọng: **31/31 tests passed** (Contracts C12-C5Ext, Ontology Validator, Neo4j Client, Layer 1B Agent Watchers, Layer 1A Playwright Network Interceptors).
