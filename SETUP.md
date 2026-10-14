# 🚀 Hướng Dẫn Cài Đặt & Thiết Lập Chi Tiết (Setup Guide) — Personal Task Board (PTB)

Tài liệu này hướng dẫn chi tiết từng bước (Step-by-Step) thiết lập và vận hành hệ thống **Personal Task Board (PTB)** trên một máy tính mới từ con số 0.

---

## 📑 Mục Lục
1. [Yêu Cầu Tiên Quyết (Prerequisites)](#1-yêu-cầu-tiên-quyết-prerequisites)
2. [Cách 1: Khởi Chạy Nhanh Bằng Docker Compose (Khuyên Dùng)](#2-cách-1-khởi-chạy-nhanh-bằng-docker-compose-khuyên-dùng)
3. [Cách 2: Khởi Chạy Cục Bộ Bare-Metal (Python + UV)](#3-cách-2-khởi-chạy-cục-bộ-bare-metal-python--uv)
4. [Hướng Dẫn Cấu Hình Chi Tiết (.env & config/)](#4-hướng-dẫn-cấu-hình-chi-tiết-env--config)
5. [Cấu Hình Nguồn Thu Thập Dữ Liệu (Data Ingestion)](#5-cấu-hình-nguồn-thu-thập-dữ-liệu-data-ingestion)
6. [Tích Hợp MCP Cho Coding Agents (Claude, Cursor, Antigravity)](#6-tích-hợp-mcp-cho-coding-agents-claude-cursor-antigravity)
7. [Kiểm Tra & Chẩn Đoán Hệ Thống (Health Check & Doctor)](#7-kiểm-tra--chẩn-đoán-hệ-thống-health-check--doctor)
8. [Xử Lý Sự Cố Thường Gặp (Troubleshooting)](#8-xử-lý-sự-cố-thường-gặp-troubleshooting)

---

## 1. Yêu Cầu Tiên Quyết (Prerequisites)

### 1.1 Yêu Cầu Phần Cứng
- **RAM**: Tối thiểu 4 GB (Khuyến nghị 8 GB trở lên nếu kích hoạt đồng thời OpenWebUI và LLM).
- **Dung lượng đĩa trống**: Tối thiểu 5 GB.
- **Hệ điều hành**: Windows 10/11 (bật WSL2), macOS (Apple Silicon hoặc Intel), hoặc Linux (Ubuntu 22.04+).

### 1.2 Các Phần Mềm Cần Cài Trước Trên Máy Mới
1. **Git**: Tải và cài đặt tại [git-scm.com](https://git-scm.com/).
2. **Docker Desktop** (hoặc Docker Engine trên Linux):
   - Windows: Bật tính năng WSL2 backend trong Docker Desktop Settings.
   - Đảm bảo Docker đang chạy: mở terminal gõ `docker --version` và `docker compose version`.
3. *(Nếu chọn Cách 2 - Bare-Metal)*:
   - **Python**: Phiên bản 3.11, 3.12 hoặc 3.13.
   - **uv** (Package manager siêu tốc cho Python):
     - **Windows (PowerShell)**:
       ```powershell
       powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
       ```
     - **Linux / macOS**:
       ```bash
       curl -LsSf https://astral.sh/uv/install.sh | sh
       ```

---

## 2. Cách 1: Khởi Chạy Nhanh Bằng Docker Compose (Khuyên Dùng)

Phương pháp này đóng gói toàn bộ: **Neo4j** (Cơ sở dữ liệu Đồ thị), **PTB App** (REST API & FastMCP Server), và **OpenWebUI** (Giao diện Web) vào các container độc lập. Bạn không cần cài đặt Python hay thư viện thủ công.

### Bước 1: Clone mã nguồn dự án
```bash
git clone https://github.com/nxc1802/Personal_Task_Board.git
cd Personal_Task_Board
```

### Bước 2: Tạo và chỉnh sửa file cấu hình môi trường `.env`
Sao chép từ file mẫu `.env.example`:
- **Linux / macOS**:
  ```bash
  cp .env.example .env
  ```
- **Windows (PowerShell)**:
  ```powershell
  Copy-Item .env.example .env
  ```

Mở file `.env` bằng trình soạn thảo (VS Code, Notepad, Vim,...) và điền các thông số cơ bản:
```ini
# 1. Mật khẩu Neo4j (Tùy chọn, đặt mật khẩu an toàn)
NEO4J_PASSWORD=personal_task_board_2026

# 2. Cấu hình LLM Provider (Ví dụ: Google Gemini qua OpenAI-compatible API)
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
LLM_API_KEY=AIzaSy...your_gemini_api_key_here...
LLM_MODEL=gemini-2.5-flash

# 3. Định danh người dùng
TENANT_ID=local-user
CURRENT_USER_NAME=Local User
```
> *(Xem thêm [Mục 4](#4-hướng-dẫn-cấu-hình-chi-tiết-env--config) nếu bạn muốn dùng OpenAI, Anthropic Claude, hoặc Ollama cục bộ)*.

### Bước 3: Khởi chạy toàn bộ hệ thống
Khởi chạy với một lệnh duy nhất:
```bash
docker compose up -d --build
```
Container `ptb-app` sẽ tự động:
1. Đợi Neo4j sẵn sàng kết nối.
2. Tự động áp dụng Cypher Constraints & Schema Indexes.
3. Khởi động REST API trên cổng `8000` và FastMCP SSE trên cổng `8001`.
4. Khởi động OpenWebUI trên cổng `3000`.

### Bước 4: Truy cập các dịch vụ
Sau khi Docker khởi động xong (khoảng 30-60 giây):
- 🌐 **Giao diện Task Board & Chat (OpenWebUI)**: [http://localhost:3000](http://localhost:3000)
- 📊 **Cổng Kiểm Soát PTB Developer Portal**: [http://localhost:8000/portal](http://localhost:8000/portal)
- 🔌 **Kiểm tra REST API Healthcheck**: [http://localhost:8000/health](http://localhost:8000/health)
- 🗄️ **Neo4j Browser UI**: [http://localhost:7474](http://localhost:7474) (Username: `neo4j`, Password: đã đặt trong `.env`).
- 🤖 **FastMCP Protocol URL cho Agent**: `http://localhost:8001/sse`

---

## 3. Cách 2: Khởi Chạy Cục Bộ Bare-Metal (Python + UV)

Dành cho lập trình viên muốn phát triển, chỉnh sửa code và debug trực tiếp trên máy.

### Bước 1: Khởi động riêng container Neo4j
```bash
cd Personal_Task_Board
docker compose up -d neo4j
```

### Bước 2: Cài đặt toàn bộ dependencies tự động với `uv`
```bash
uv sync --all-packages
```

### Bước 3: Cài đặt trình duyệt Playwright Chromium (cho thu thập Teams/Outlook)
```bash
uv run playwright install chromium
```

### Bước 4: Khởi tạo Schema & Indexes trên Neo4j
```bash
uv run ptb init
```
*(Hoặc `uv run python scripts/ptb_cli.py init`)*.

### Bước 5: Kiểm tra tính sẵn sàng bằng lệnh Doctor
```bash
uv run ptb doctor
```
Nếu có cấu hình LLM API Key, bạn có thể kiểm tra kết nối sâu:
```bash
uv run ptb doctor --deep
```

### Bước 6: Khởi chạy Process Supervisor
```bash
uv run ptb run
```
Supervisor sẽ tự động giám sát và bind:
- REST API: `http://127.0.0.1:8000`
- FastMCP SSE: `http://127.0.0.1:8001/sse`
- Ingestion Watchers: Quét phiên hoạt động của Coding Agents, Git local, Jira, Shortcut,...
- Processing Worker: Nhận và phân tích `RawEvents`.

---

## 4. Hướng Dẫn Cấu Hình Chi Tiết (`.env` & `config/`)

### 4.1 Cấu Hình LLM Provider trong `.env`

#### Tùy chọn A: Dùng Google Gemini (Khuyên dùng — Nhanh & Chi phí thấp)
```ini
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
LLM_API_KEY=AIzaSy...your_gemini_api_key...
LLM_MODEL=gemini-2.5-flash
PTB_EXTRACTION_MODEL=gemini-2.5-flash
```

#### Tùy chọn B: Dùng OpenAI chính thức
```ini
LLM_PROVIDER=openai
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-proj-...your_openai_api_key...
LLM_MODEL=gpt-4o-mini
PTB_EXTRACTION_MODEL=gpt-4o-mini
```

#### Tùy chọn C: Dùng Ollama Cục Bộ (Không cần Internet)
```ini
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=qwen2.5:7b
PTB_EXTRACTION_MODEL=qwen2.5:7b
```

### 4.2 Bật/Tắt các nguồn thu thập trong `config/sources.yaml`
Mở `config/sources.yaml` để tinh chỉnh nguồn dữ liệu cần quét:
```yaml
sources:
  ms_teams:
    enabled: true
    poll_interval_seconds: 30
  ms_outlook:
    enabled: true
    poll_interval_seconds: 60
  coding_agents:
    enabled: true
    watchers:
      cursor: true
      claude_code: true
      antigravity: true
  git:
    enabled: true
    repositories:
      - path: "."
        branch: "main"
  jira:
    enabled: false  # Bật true nếu có Jira Cloud
  slack:
    enabled: false  # Bật true nếu có Slack App Token
  github:
    enabled: false  # Bật true nếu thu thập qua GitHub REST API
```

---

## 5. Cấu Hình Nguồn Thu Thập Dữ Liệu (Data Ingestion)

Vì dữ liệu sẽ được quét mới trên máy này, bạn thực hiện cấu hình theo các kênh muốn thu thập:

### 5.1 Quét Tự Động Coding Agents (Cursor, Claude Code, Antigravity, Windsurf)
- **Không cần cấu hình gì thêm!**
- Khi bạn làm việc với Cursor, Claude Code CLI hoặc Antigravity trên máy tính này, `PTBProcessSupervisor` sẽ tự động tìm kiếm đường dẫn log phiên làm việc chuẩn của hệ điều hành (qua `platformdirs`) và trích xuất các task/decisions phát sinh.

### 5.2 Thu Thập Microsoft Teams & Outlook Web
Nếu bạn muốn hệ thống lắng nghe tin nhắn nhắc việc từ Teams / Outlook:
1. Chạy lệnh đăng nhập tương tác đồ họa:
   - Nếu chạy Bare-metal:
     ```bash
     uv run ptb login microsoft
     ```
   - Nếu chạy qua Docker:
     *(Khuyên dùng: đăng nhập 1 lần ở môi trường Bare-metal để lưu file cookie vào `data/playwright/storage_state.json`, Docker sẽ tự động mount file này)*.
2. Trình duyệt Chromium sẽ mở ra. Bạn đăng nhập tài khoản Microsoft 365 cơ quan/cá nhân và hoàn thành 2FA.
3. Sau khi đăng nhập thành công, trình duyệt tự đóng và lưu phiên vào `data/playwright/storage_state.json`. Lần sau hệ thống chạy ngầm headless mà không cần hỏi lại mật khẩu.

### 5.3 Thu Thập Local Git
Thêm đường dẫn thư mục repository Git bạn đang làm việc vào `config/sources.yaml` mục `sources.git.repositories`. Hệ thống sẽ theo dõi các commit và message để liên kết với các task tương ứng.

---

## 6. Tích Hợp MCP Cho Coding Agents (Claude, Cursor, Antigravity)

Personal Task Board cung cấp **19 FastMCP Tools** (CRUD Task, Đọc Context, Tìm Kiếm Semantic, Tra Cứu Quyết Định Kỹ Thuật) cho các AI trợ lý lập trình.

### 6.1 Cursor IDE
1. Mở Cursor Settings -> **Features** -> **MCP Servers**.
2. Nhấn **Add New MCP Server**:
   - **Name**: `personal-task-board`
   - **Type**: `sse`
   - **URL**: `http://localhost:8001/sse`
3. Cursor sẽ kết nối và nhận đủ 19 công cụ của PTB.

### 6.2 Claude Desktop
Mở file cấu hình Claude Desktop:
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`

Thêm cấu hình máy chủ:
```json
{
  "mcpServers": {
    "personal-task-board": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "<ĐƯỜNG_DẪN_TỚI_THƯ_MỤC_PERSONAL_TASK_BOARD>",
        "python",
        "-m",
        "scripts.ptb_cli",
        "mcp",
        "--serve-fastmcp"
      ]
    }
  }
}
```

### 6.3 Google Antigravity IDE
Trong Antigravity IDE Settings / MCP Settings, khai báo endpoint FastMCP Server:
- **URL**: `http://127.0.0.1:8001/sse`
- Hoặc kết nối stdio với lệnh: `uv run python -m scripts.ptb_cli mcp --serve-fastmcp`

---

## 7. Kiểm Tra & Chẩn Đoán Hệ Thống (Health Check & Doctor)

### 7.1 Kiểm tra tình trạng tất cả phân hệ (PTB Doctor)
```bash
# Bare-metal:
uv run ptb doctor

# Docker container:
docker compose exec ptb-app ptb doctor
```
Kết quả mong đợi:
```text
===========================================================================
PERSONAL TASK BOARD: SYSTEM & DEPENDENCY DOCTOR CHECK
===========================================================================
[✓] PASS : Python Runtime (>= 3.11)
[✓] PASS : Docker Daemon
[✓] PASS : Neo4j Database Server (:7687)
[✓] PASS : Playwright Chromium Engine
[✓] PASS : OpenWebUI Server (:3000)
[✓] PASS : LLM Provider Readiness
[✓] PASS : Graphiti Semantic Memory
===========================================================================
```

### 7.2 Chạy bộ kiểm thử tự động (Verification Test Suite)
Để đảm bảo môi trường trên máy mới hoạt động chuẩn 100%:
```bash
uv run pytest tests/test_tasks_t1_t2_t3.py tests/test_task_t4_multichannel.py tests/test_task_t5_mcp.py
```
Tất cả 25 tests phải chuyển sang màu xanh (`25 passed`).

---

## 8. Xử Lý Sự Cố Thường Gặp (Troubleshooting)

| Hiện Tượng / Lỗi | Nguyên Nhân | Cách Xử Lý |
|---|---|---|
| **`failed to connect to docker API... pipe/dockerDesktopLinuxEngine`** | Docker Desktop chưa được bật trên máy tính. | Bật ứng dụng Docker Desktop, đợi biểu tượng Docker chuyển sang màu xanh (Running) rồi chạy lại lệnh. |
| **`PTB_STORAGE_001: Mất kết nối Neo4j`** | Container Neo4j chưa chạy hoặc sai mật khẩu. | 1. Kiểm tra container: `docker compose ps neo4j`<br>2. Kiểm tra mật khẩu `NEO4J_PASSWORD` trong file `.env` đã khớp với container chưa. |
| **`PTB_LLM_001: LLM API key unconfigured`** | Chưa nhập `LLM_API_KEY` trong file `.env`. | Mở file `.env` và điền khóa API của Gemini hoặc OpenAI vào biến `LLM_API_KEY`. |
| **Cổng mạng bị xung đột (`8000`, `8001`, `3000`, hoặc `7687` already in use)** | Có ứng dụng khác đang chiếm cổng. | Đổi cổng ánh xạ trong file `.env` (ví dụ `APP_PORT=8010`, `MCP_PORT=8011`) hoặc tắt tiến trình đang chiếm cổng cũ. |
| **`AUTH_REQUIRED` khi quét Teams / Outlook** | Chưa có phiên đăng nhập Microsoft. | Chạy lệnh `ptb login microsoft` để mở trình duyệt và đăng nhập tài khoản một lần. |

---

> 💡 **Bạn cần hỗ trợ thêm?** Hãy tham khảo [DEPLOYMENT.md](./DEPLOYMENT.md) cho cấu hình Production chi tiết hoặc xem các yêu cầu bài toán tại [Task/README.md](./Task/README.md).
