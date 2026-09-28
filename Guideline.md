# Cẩm Nang Kỹ Thuật & Hướng Dẫn Vận Hành (PTB v1.1)

> Tài liệu chuẩn xác duy nhất (Source-of-Truth) hướng dẫn cài đặt môi trường, cấu hình và vận hành hệ thống **Personal Task Board v1.1** theo kiến trúc **Local-First Lean Edition** (Neo4j Single-Store, Playwright Layer 1A, Coding Agent Watchers Layer 1B, Graphiti-Core Layer 3, FastAPI REST, FastMCP).

---

## 📑 Mục Lục

1. [Tổng Quan Kiến Trúc Kỹ Thuật](#1-tổng-quan-kiến-trúc-kỹ-thuật)
2. [Thiết Lập Môi Trường (macOS, Linux, Windows)](#2-thiết-lập-môi-trường-macos-linux-windows)
3. [Cấu Hình Chi Tiết (.env & config/*.yaml)](#3-cấu-hình-chi-tiết-env--configyaml)
4. [Cẩm Nang Lệnh CLI (CLI Reference Manual)](#4-cẩm-nang-lệnh-cli-cli-reference-manual)
5. [Quy Trình Vận Hành Hằng Ngày](#5-quy-trình-vận-hành-hằng-ngày)
6. [Xử Lý Sự Cố Thường Gặp (Troubleshooting FAQs)](#6-xử-lý-sự-cố-thường-gặp-troubleshooting-faqs)
7. [Quy Chuẩn Kiểm Thử (Testing Standards)](#7-quy-chuẩn-kiểm-thử-testing-standards)

---

## 1. Tổng Quan Kiến Trúc Kỹ Thuật

Personal Task Board v1.1 được xây dựng theo triết lý **Local-First Lean & Single-Store**:
- **Neo4j Single-Store**: Nơi lưu trữ bền bỉ tập trung duy nhất cho toàn bộ hệ thống. Toàn bộ kiến trúc cũ (Supabase dual-store, Outbox table trung gian, RAM-only queue) đã được thay thế hoàn toàn.
- **Quy tắc Durability cốt lõi**: Mọi sự kiện thu thập từ bên ngoài đều phải được ghi thành `RawEvent` trong Neo4j trước khi thực hiện trích xuất ngữ nghĩa. RAM queue chỉ đóng vai trò tối ưu hóa đệm, không phải ranh giới an toàn dữ liệu.
- **Graphiti-Core Temporal Memory**: Tự động quản lý đồ thị tri thức thời gian thực, liên kết các quyết định (`Decision`), bài học (`LessonLearned`) và bằng chứng (`Evidence`).
- **Process Supervisor**: Bộ điều phối trung tâm thống nhất khởi chạy và giám sát toàn bộ dịch vụ (Playwright, Watchers, ProcessingWorker, REST API, FastMCP).

---

## 2. Thiết Lập Môi Trường (macOS, Linux, Windows)

### 2.1 Yêu Cầu Tiên Quyết
- **Python**: `>= 3.11` (Khuyến nghị 3.12 hoặc 3.13)
- **Docker & Docker Compose**: Để khởi chạy container Neo4j và OpenWebUI
- **uv**: Package manager siêu tốc cho Python ([https://astral.sh/uv](https://astral.sh/uv))
- **Trình duyệt Chromium (Playwright)**: Để chạy interceptor thu thập Teams & Outlook

---

### 2.2 Hướng Dẫn Cài Đặt Trên macOS

Áp dụng cho cả Apple Silicon (M1/M2/M3/M4) và Intel Mac:

```bash
# 1. Cài đặt uv (nếu chưa có)
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.cargo/env

# 2. Clone mã nguồn dự án
git clone https://github.com/nxc1802/Personal_Task_Board.git
cd Personal_Task_Board

# 3. Tạo môi trường ảo và cài đặt toàn bộ dependencies trong monorepo
uv sync --all-packages

# 4. Cài đặt Chromium binary cho Playwright
uv run playwright install chromium

# 5. Thiết lập alias lệnh ptb (Tùy chọn tiện ích)
echo 'alias ptb="uv run python scripts/ptb_cli.py"' >> ~/.zshrc
source ~/.zshrc
```

---

### 2.3 Hướng Dẫn Cài Đặt Trên Linux (Ubuntu / Debian / Fedora)

```bash
# 1. Cài đặt uv
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.cargo/env

# 2. Cài đặt Docker & Docker Compose Plugin (nếu chưa có)
sudo apt update && sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER
newgrp docker

# 3. Clone và đồng bộ packages
git clone https://github.com/nxc1802/Personal_Task_Board.git
cd Personal_Task_Board
uv sync --all-packages

# 4. Cài đặt Playwright Chromium kèm các thư viện hệ thống (system dependencies)
uv run playwright install --with-deps chromium

# 5. Thiết lập alias lệnh ptb
echo 'alias ptb="uv run python scripts/ptb_cli.py"' >> ~/.bashrc
source ~/.bashrc
```

---

### 2.4 Hướng Dẫn Cài Đặt Trên Windows 10/11 (PowerShell)

Mở PowerShell với quyền Administrator:

```powershell
# 1. Cài đặt uv qua script chính thức
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2. Khởi động lại PowerShell, di chuyển đến thư mục làm việc và clone dự án
git clone https://github.com/nxc1802/Personal_Task_Board.git
cd Personal_Task_Board

# 3. Đồng bộ toàn bộ dependencies
uv sync --all-packages

# 4. Cài đặt Playwright Chromium
uv run playwright install chromium

# 5. Thiết lập hàm lệnh ptb trong PowerShell Profile
if (!(Test-Path $PROFILE)) { New-Item -Type File -Path $PROFILE -Force }
Add-Content $PROFILE "`nfunction ptb { uv run python scripts/ptb_cli.py `$args }"
. $PROFILE
```

---

## 3. Cấu Hình Chi Tiết (.env & config/*.yaml)

### 3.1 Cấu Hình Biến Môi Trường (`.env`)

Sao chép file mẫu:
```bash
cp .env.example .env
```

Nội dung chuẩn của `.env`:
```env
# ==============================================================================
# Neo4j Single-Store Configuration (Authoritative Store + Graphiti Memory)
# ==============================================================================
NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=personal_task_board_secret_2026
NEO4J_DATABASE=neo4j

# ==============================================================================
# LLM Provider Configuration (OpenAI-Compatible hoặc Ollama Local)
# ==============================================================================
# Sử dụng Ollama cục bộ:
LLM_BASE_URL=http://127.0.0.1:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=qwen2.5-coder:7b
LLM_TEMPERATURE=0.1

# Hoặc sử dụng OpenAI Cloud:
# LLM_BASE_URL=https://api.openai.com/v1
# LLM_API_KEY=sk-...
# LLM_MODEL=gpt-4o-mini

# ==============================================================================
# Playwright & Microsoft Session Configuration
# ==============================================================================
PTB_STORAGE_STATE=data/playwright/storage_state.json

# ==============================================================================
# Network Services & Ports
# ==============================================================================
APP_HOST=127.0.0.1
APP_PORT=8000
MCP_PORT=8001
OPENWEBUI_PORT=3000
TENANT_ID=local-user
CURRENT_USER_NAME="Nguyen Cuong"
```

---

### 3.2 Cấu Hình Trọng Số Ưu Tiên (`config/priority.yaml`)

File `config/priority.yaml` điều khiển thuật toán tính điểm ưu tiên (`PriorityScore = 0 - 100`) của các task dựa trên 5 chiều đánh giá:

```yaml
tenant_id: "local-user"

weights:
  source_type: 0.25        # Mức độ quan trọng theo kênh nguồn
  urgency: 0.30            # Tính cấp bách theo thời hạn (deadline proximity)
  project_tier: 0.20       # Cấp độ ưu tiên của dự án liên quan (P0 > P1 > P2)
  customer_tier: 0.15      # Mức độ ưu tiên của khách hàng / đối tác
  staleness_penalty: 0.10  # Trừ điểm đối với các công việc bị bỏ quên quá lâu

source_weights:
  ms_teams_web: 0.90       # Tin nhắn trực tiếp hoặc mention trong Teams
  ms_outlook_web: 0.85     # Email gửi trực tiếp tới cá nhân
  jira: 0.80               # Issue phân công chính thức
  shortcut: 0.80           # Story thẻ công việc
  coding_agent: 0.75       # Cam kết trong phiên coding (Cursor, Claude Code, Antigravity)
  git: 0.60                # Commit / branch activity

thresholds:
  urgent_threshold: 80     # Điểm >= 80 xếp vào nhóm URGENT
  high_threshold: 65       # Điểm >= 65 xếp vào nhóm HIGH
  medium_threshold: 45     # Điểm >= 45 xếp vào nhóm MEDIUM
  low_threshold: 0         # Dưới 45 xếp vào nhóm LOW
```

---

### 3.3 Cấu Hình Nguồn Thu Thập Dữ Liệu (`config/sources.yaml`)

Quản lý thông tin kết nối và bộ lọc cho Layer 1:

```yaml
tenant_id: "local-user"

sources:
  ms_teams:
    enabled: true
    source_type: "ms_teams_web"
    url: "https://teams.microsoft.com"
    headless: true
    storage_state_path: "data/playwright/storage_state.json"
    poll_interval_seconds: 60
    filters:
      chat_types: ["chat", "channel"]
      exclude_system_messages: true

  ms_outlook:
    enabled: true
    source_type: "ms_outlook_web"
    url: "https://outlook.office.com"
    headless: true
    storage_state_path: "data/playwright/storage_state.json"
    poll_interval_seconds: 120
    filters:
      folders: ["Inbox"]
      ignore_promotions: true

  coding_agents:
    enabled: true
    source_type: "coding_agent"
    poll_interval_seconds: 30
    agents:
      cursor:
        enabled: true
        paths:
          - "~/Library/Application Support/Cursor/User/workspaceStorage"
          - "%APPDATA%/Cursor/User/workspaceStorage"
          - "~/.config/Cursor/User/workspaceStorage"
      claude_code:
        enabled: true
        paths:
          - "~/.claude/projects"
          - "~/.claude/transcripts"
      antigravity:
        enabled: true
        paths:
          - "~/.gemini/antigravity"
      codex:
        enabled: true
        paths:
          - "~/.codex/sessions"

  git:
    enabled: true
    source_type: "git"
    repo_paths:
      - "."

  jira:
    enabled: false
    base_url: "https://your-domain.atlassian.net"
    email: "${JIRA_EMAIL}"
    api_token: "${JIRA_API_TOKEN}"
    default_jql: "assignee = currentUser() AND statusCategory != Done"

  shortcut:
    enabled: false
    api_token: "${SHORTCUT_API_TOKEN}"
```

---

### 3.4 Cấu Hình Mô Hình LLM & Vector Embeddings (`config/models.yaml`)

```yaml
default_llm_provider: "openai_compatible"
default_embedding_provider: "openai_compatible"

providers:
  openai_compatible:
    base_url: "${LLM_BASE_URL:-http://127.0.0.1:11434/v1}"
    api_key: "${LLM_API_KEY:-ollama}"
    
    # Model trích xuất thực thể và phát hiện task candidate (Layer 2)
    extraction_model: "qwen2.5-coder:7b"
    extraction_temperature: 0.1
    extraction_max_tokens: 2048

    # Model phân tích nguyên nhân và giải thích độ ưu tiên (Layer 3)
    planning_model: "qwen2.5-coder:7b"
    planning_temperature: 0.2
    planning_max_tokens: 4096

    # Model vector embeddings cho Graphiti Knowledge Graph
    embedding_model: "nomic-embed-text"
    embedding_dimensions: 768
    timeout_seconds: 45
```

---

## 4. Cẩm Nang Lệnh CLI (CLI Reference Manual)

CLI của Personal Task Board được điều khiển qua entrypoint `scripts/ptb_cli.py` (hoặc alias `ptb`).

```text
Cú pháp tổng quát:
  ptb <lệnh> [tùy chọn...]
```

---

### 4.1 Lệnh: `ptb doctor`
Kiểm tra chẩn đoán toàn diện tính sẵn sàng của môi trường trước khi vận hành.

```bash
ptb doctor
```

**Các hạng mục kiểm tra:**
1. **Python Runtime**: Xác nhận phiên bản `>= 3.11`.
2. **Docker Daemon**: Xác nhận tiến trình Docker đang chạy.
3. **Neo4j Database**: Kiểm tra cổng Bolt `127.0.0.1:7687` và container `ptb_neo4j`.
4. **Playwright Chromium**: Xác nhận file thực thi của trình duyệt Chromium đã được tải.
5. **OpenWebUI Service**: Kiểm tra trạng thái cổng `127.0.0.1:3000`.

**Mã trả về (Exit Codes):**
- `0`: Toàn bộ các thành phần cốt lõi đều đạt `[✓] PASS`.
- `1`: Có ít nhất một thành phần bị `[✗] FAIL` (cần sửa trước khi chạy).

---

### 4.2 Lệnh: `ptb login microsoft`
Mở trình duyệt Chromium đồ họa (Headed Mode) để đăng nhập tài khoản Microsoft 365 phục vụ thu nạp Teams và Outlook Web.

```bash
# Đăng nhập cả Teams và Outlook
ptb login microsoft

# Hoặc đăng nhập riêng từng dịch vụ
ptb login microsoft --service teams
ptb login microsoft --service outlook

# Tùy chỉnh file lưu trữ session
ptb login microsoft --storage-path data/playwright/storage_state.json
```

**Quy trình thực thi:**
1. Chromium tự động mở trang đăng nhập Microsoft.
2. Người dùng nhập email, mật khẩu và hoàn tất xác thực đa yếu tố (MFA).
3. Sau khi người dùng chuyển hướng thành công vào giao diện Teams hoặc Outlook, interceptor xác nhận trang `authenticated` và lưu toàn bộ cookies, local storage vào file `storage_state.json`.
4. Trình duyệt tự động đóng lại an toàn.

---

### 4.3 Lệnh: `ptb init`
Khởi tạo cơ sở dữ liệu Neo4j Single-Store, thiết lập toàn bộ Constraints và Indexes.

```bash
ptb init
```

**Chi tiết khởi tạo:**
- Tạo 11 Uniqueness Constraints cho: `RawEvent.id`, `RawEvent.idempotency_key`, `UnifiedTask.id`, `Evidence.id`, `Commitment.id`, `Person.canonical_id`, `SourceIdentity.id`, `IngestionCheckpoint.id`, `(tenant_id, source_type, stream_id)`, `ProcessingAttempt.id`, `StatusTransitionAudit.id`.
- Tạo các chỉ mục tìm kiếm và liên kết quan hệ trong Fixed Ontology.

---

### 4.4 Lệnh: `ptb openwebui install`
Tự động cài đặt thành phần mở rộng PTB Board và Tools vào OpenWebUI instance.

```bash
# Cài đặt mặc định tới OpenWebUI tại http://127.0.0.1:3000
ptb openwebui install

# Tùy chỉnh URL hoặc thư mục data của OpenWebUI
ptb openwebui install --url http://127.0.0.1:3000 --data-dir ./openwebui-data
```

**Kết quả thực thi:**
- Cài đặt Dashboard Web Component (`ptb_board.html`) vào OpenWebUI Artifacts.
- Cấu hình Tool Functions để gọi REST APIs của PTB trực tiếp từ cửa sổ chat AI của OpenWebUI.

---

### 4.5 Lệnh: `ptb run` (Process Supervisor)
Khởi chạy và giám sát toàn bộ hệ sinh thái dịch vụ Personal Task Board trong một tiến trình duy nhất.

```bash
# Khởi chạy chế độ mặc định
ptb run

# Tùy chỉnh cổng lắng nghe
ptb run --port 8000 --mcp-port 8001

# Chạy không cần browser daemon (nếu chỉ muốn dùng Watchers & REST/MCP)
ptb run --no-playwright

# Tùy chỉnh chu kỳ polling của worker
ptb run --poll-interval 2.0
```

**Các dịch vụ được quản lý đồng thời:**
1. **Application Service REST API**: Chạy trên `http://127.0.0.1:8000`.
2. **FastMCP Server (SSE)**: Chạy trên `http://127.0.0.1:8001`.
3. **ProcessingWorker Loop**: Vòng lặp nền quét `RawEvent` PENDING/RETRY trong Neo4j để trích xuất ra `UnifiedTask`.
4. **Coding Agent Watchers**: Tự động theo dõi các session mới từ Cursor, Claude Code, Antigravity.
5. **Playwright Acquisition Daemon**: Chạy ngầm chặn bản tin mạng từ Teams/Outlook.

Nhấn `Ctrl+C` để thực hiện **Graceful Shutdown** toàn diện, giải phóng ports và đóng kết nối Neo4j an toàn.

---

### 4.6 Lệnh: `ptb ingest`
Kích hoạt thủ công một vòng quét và thu nạp dữ liệu tức thì từ các nguồn đã cấu hình.

```bash
# Quét toàn bộ nguồn
ptb ingest

# Quét riêng Coding Agents
ptb ingest --source coding_agent

# Quét riêng Local Git Repos
ptb ingest --source git

# Quét riêng Jira hoặc Shortcut
ptb ingest --source jira
ptb ingest --source shortcut
```

Báo cáo tổng kết sẽ in ra số lượng: Ingested (Thu nạp), Deduplicated (Loại trừ trùng lặp), Persisted (Đã lưu Neo4j) và Errors (Lỗi phát sinh).

---

### 4.7 Lệnh: `ptb status`
Báo cáo kiểm toán chuyên sâu về tình trạng hoạt động của toàn bộ các phân hệ (subsystems).

```bash
ptb status
```

**Thông tin báo cáo:**
- **Neo4j Single-Store**: Trạng thái kết nối, số lượng `RawEvent`, `UnifiedTask`, số lượng constraints đang hoạt động.
- **Microsoft Session**: Trạng thái session Teams & Outlook (`VALID`, `UNCONFIGURED`, `LOGIN_REQUIRED`, `AUTH_EXPIRED`).
- **Coding Agent Watchers**: Danh sách chi tiết các công cụ (Cursor, Claude Code, Antigravity, Codex) và số lượng thư mục phát hiện được trên máy.
- **Processing Queue**: Phân bổ số lượng bản tin theo trạng thái: `PENDING`, `PROCESSING`, `RETRY`, `PROCESSED`, `FAILED`.
- **External Adapters**: Trạng thái các adapter Git, Jira, Shortcut.

---

### 4.8 Lệnh: `ptb graph rebuild`
Tái tạo lại toàn bộ bộ nhớ ngữ nghĩa Graphiti từ dữ liệu chuẩn (authoritative) trong Neo4j.

```bash
ptb graph rebuild
```

Sử dụng khi bạn muốn đồng bộ lại vector embeddings hoặc tái tạo các episodes và edges giữa các `Decision`, `LessonLearned`, `Evidence` sau khi thay đổi model embedding hoặc chỉnh sửa thủ công DB.

---

### 4.9 Lệnh: `ptb serve`
Khởi chạy độc lập máy chủ FastMCP hoặc Application REST API (phục vụ kiểm thử cô lập).

```bash
# Chỉ chạy FastMCP Server trên cổng 8001
ptb serve --mcp-only --port 8001

# Chỉ chạy Application REST API trên cổng 8000
ptb serve --app-only --port 8000
```

---

## 5. Quy Trình Vận Hành Hằng Ngày

Một ngày làm việc hiệu quả và liền mạch cùng Personal Task Board:

```text
 08:30 Sáng: Mở OpenWebUI Board (:3000)
             └── Tab 📌 Today: Nắm 5 tasks quan trọng nhất và cam kết trong ngày.
             └── Tab 📥 Review: Duyệt nhanh các tasks do AI tự động trích xuất hôm qua.
 
 Trong ngày: Lập trình với Cursor / Claude Code / Antigravity
             └── AI Agent tự động gọi FastMCP (:8001) để lấy context, blockers, decisions.
             └── Hệ thống tự động bắt tin nhắn Teams/Outlook & logs session.
 
 17:30 Chiều: Xem lại tiến độ
             └── Tab ⏳ Waiting: Xem các đầu việc đang chờ người khác để follow-up.
             └── Tab 🕰️ Forgotten: Kiểm tra các cam kết có dấu hiệu bị trôi việc.
```

---

## 6. Xử Lý Sự Cố Thường Gặp (Troubleshooting FAQs)

### Q1: `ptb doctor` báo lỗi `Neo4j Database unreachable`?
- **Nguyên nhân**: Docker chưa chạy hoặc container `ptb_neo4j` chưa được khởi động.
- **Khắc phục**:
  ```bash
  docker compose up -d
  # Chờ khoảng 10 giây cho Neo4j khởi động hoàn tất, sau đó kiểm tra lại:
  ptb doctor
  ```

### Q2: Microsoft Session chuyển sang trạng thái `AUTH_EXPIRED`?
- **Nguyên nhân**: Token đăng nhập Teams/Outlook hết hạn sau một khoảng thời gian theo chính sách bảo mật của doanh nghiệp.
- **Khắc phục**: Chỉ cần chạy lại lệnh đăng nhập tương tác:
  ```bash
  ptb login microsoft
  ```
  Sau khi hoàn tất đăng nhập, runtime sẽ tự động tái sử dụng session mới mà không làm gián đoạn hay trùng lặp dữ liệu đã thu nạp trước đó.

### Q3: Bị xung đột cổng (Port 8000 hoặc 8001 đã có ứng dụng khác chiếm)?
- **Khắc phục**: Thay đổi cổng thông qua tham số dòng lệnh:
  ```bash
  ptb run --port 8080 --mcp-port 8081
  ```

### Q4: Trình duyệt Chromium không khởi động được trên môi trường Linux Server headless?
- **Khắc phục**: Cài đặt bổ sung các thư viện đồ họa hệ thống:
  ```bash
  uv run playwright install --with-deps chromium
  ```

---

## 7. Quy Chuẩn Kiểm Thử (Testing Standards)

Hệ thống duy trì bộ test suite toàn trình tự động bao phủ trọn vẹn cả 5 tầng kiến trúc:
- **Tầng Contracts & Types**: Kiểm tra tính toàn vẹn của Pydantic models và schema.
- **Tầng Database & Repositories**: Kiểm tra kết nối Neo4j, Uniqueness Constraints, Ingestion Checkpoints.
- **Tầng Acquisition**: Kiểm tra các Watchers (Cursor, Claude Code, Antigravity) và Playwright Interceptors.
- **Tầng Processing & Intelligence**: Kiểm tra cơ chế Heuristic/LLM extraction, Priority calculation, Status inference.
- **Tầng FastMCP & Application API**: Kiểm tra 10 Read-Only Query Tools và các REST endpoints.
- **Tầng CLI & Supervisor**: Kiểm tra quá trình khởi động, điều phối và shutdown an toàn.

Thực thi bộ test suite:
```bash
uv run pytest
```

*Toàn bộ 230+ tests tự động được kiểm thử liên tục qua GitHub Actions CI Matrix trên 3 hệ điều hành (Ubuntu, macOS, Windows) và 3 phiên bản Python (3.11, 3.12, 3.13).*
