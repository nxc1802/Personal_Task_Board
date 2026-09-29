# Cẩm Nang Kỹ Thuật & Hướng Dẫn Vận Hành (PTB v1.3)

> Tài liệu chuẩn xác duy nhất (Source-of-Truth) hướng dẫn cài đặt môi trường, cấu hình, vận hành và kiểm thử hệ thống **Personal Task Board v1.3** theo kiến trúc **Local-First Production Edition** (Neo4j Authoritative Single-Store, Playwright Layer 1A, 9 Coding Agent Watchers Layer 1B, Auto-Wired Intelligence, GraphMemorySyncWorker Async Sync, Shared `ApplicationService`, FastAPI REST `:8000`, FastMCP `:8001`, OpenWebUI Live-Only `:3000`).

---

## 📑 Mục Lục

1. [Tổng Quan Kiến Trúc Kỹ Thuật & Luồng Chuẩn Hóa 8 Bước](#1-tổng-quan-kiến-trúc-kỹ-thuật--luồng-chuẩn-hóa-8-bước)
2. [Thiết Lập Môi Trường (macOS, Linux, Windows)](#2-thiết-lập-môi-trường-macos-linux-windows)
3. [Cấu Hình Chi Tiết (.env & config/*.yaml)](#3-cấu-hình-chi-tiết-env--configyaml)
4. [Cẩm Nang Lệnh CLI (CLI Reference Manual)](#4-cẩm-nang-lệnh-cli-cli-reference-manual)
5. [Cơ Chế Fail-Fast, Truthful Health & 10 Mã Lỗi Chuẩn Hóa](#5-cơ-chế-fail-fast-truthful-health--10-mã-lỗi-chuẩn-hóa)
6. [Quy Trình Vận Hành Hằng Ngày & Xử Lý Sự Cố](#6-quy-trình-vận-hành-hằng-ngày--xử-lý-sự-cố)
7. [Quy Chuẩn Kiểm Thử Theo 5 Pytest Markers](#7-quy-chuẩn-kiểm-thử-theo-5-pytest-markers)

---

## 1. Tổng Quan Kiến Trúc Kỹ Thuật & Luồng Chuẩn Hóa 8 Bước

### 1.1 Các Nguyên Tắc Kiến Trúc Bất Biến
Personal Task Board v1.3 được thiết kế theo triết lý **Local-First, Fail-Fast & Truthful Health**:
- **Neo4j Authoritative Single-Store**: Nơi lưu trữ bền bỉ tập trung duy nhất cho toàn bộ hệ thống. Khi Neo4j không khả dụng, hệ thống kích hoạt **Fail-Fast** (`PTB-STORAGE-001`), chuyển trạng thái sang `NOT_READY` và dừng tiến trình thay vì âm thầm chạy trên bộ nhớ tạm.
- **Định Danh Checkpoint Tất Định**: Mọi `IngestionCheckpoint` đều được định danh duy nhất theo khóa tổng hợp 3 thành phần `(tenant_id, source_type, stream_id)` và băm tất định `sha256(tenant_id:source_type:stream_id)`, đảm bảo cô lập đa tenant tuyệt đối và phục hồi chính xác sau khi khởi động lại.
- **Chuỗi Xử Lý Dọc Khép Kín (Complete Vertical Slice)**:
  1. `RawEvent` được ghi nhận bền bỉ vào Neo4j (`PENDING` → `PROCESSING` → `PROCESSED` / `RETRY` / `FAILED`, tăng `attempt_count` đúng 1 lần cho mỗi lượt xử lý).
  2. `ProcessingWorker` trích xuất và hợp nhất thành `UnifiedTask` cùng `Evidence`.
  3. `TaskIntelligenceLifecycle` tự động được kích hoạt ngay sau khi lưu task để tính điểm ưu tiên (`DeterministicPriorityEngine`) và suy luận trạng thái (`StatusInferenceMachine`).
  4. `GraphMemorySyncWorker` đồng bộ bất đồng bộ các `Evidence`, `Decision`, `Lesson` sang **Graphiti Temporal Memory** theo trạng thái `GraphSyncStatus` (`PENDING` → `SYNCING` → `SYNCED` / `RETRY` / `FAILED`) mà không làm nghẽn hay rollback các giao dịch lưu trữ task cốt lõi trong Neo4j.
- **Shared Runtime Dependency Graph**: `PTBProcessSupervisor` khởi tạo duy nhất một chuỗi phụ thuộc (`Neo4jClient` → `Repositories` → `ProcessingPipeline` → `TaskIntelligenceLifecycle` → `GraphitiMemoryClient` → `ApplicationService`) và chia sẻ cùng một instance `ApplicationService` cho cả FastAPI REST (`127.0.0.1:8000`) và FastMCP Server (`127.0.0.1:8001`).
- **OpenWebUI Live-Only & Self-Contained Plugins**: Giao diện bảng điều khiển (`127.0.0.1:3000`) gắn kết trực tiếp thư mục `./data/openwebui`, chỉ hiển thị dữ liệu thực và chỉ thực thi các thao tác biến đổi có REST endpoint thực. Các plugin (`ptb_tools.py`, `ptb_board_action.py`) được thiết kế self-contained (không import `ptb_contracts`), ghi log lỗi qua structured stderr với `[PTB-OWUI-001]`, và kết nối qua `http://host.docker.internal:8000`.

### 1.2 Luồng Cài Đặt & Vận Hành Chính Thức (8 Bước)

```text
  [1] Install                  : uv sync --all-packages && uv run playwright install chromium
       │
       ▼
  [2] Configure                : Thiết lập .env, config/sources.yaml, config/priority.yaml, config/models.yaml
       │
       ▼
  [3] Neo4j Available          : docker compose up -d (Neo4j :7687 & OpenWebUI :3000)
       │
       ▼
  [4] ptb init                 : Khởi tạo Neo4j Uniqueness Constraints & Indexes
       │
       ▼
  [5] ptb login microsoft      : Xác thực phiên Microsoft 365 lưu vào data/playwright/storage_state.json
       │
       ▼
  [6] ptb openwebui install    : Triển khai tools, functions, board vào ./data/openwebui
       │
       ▼
  [7] ptb doctor [--deep]      : Kiểm tra Neo4j, Playwright, OpenWebUI và LLM Provider Readiness
       │
       ▼
  [8] ptb run                  : Khởi chạy Process Supervisor giám sát REST (:8000) & MCP (:8001)
```

---

## 2. Thiết Lập Môi Trường (macOS, Linux, Windows)

### 2.1 Yêu Cầu Tiên Quyết
- **Python**: `>= 3.11` (Hỗ trợ và kiểm thử tự động trên 3.11, 3.12, 3.13)
- **Docker & Docker Compose**: Để khởi chạy container Neo4j (`127.0.0.1:7687`) và OpenWebUI (`127.0.0.1:3000`)
- **uv**: Trình quản lý gói và workspace cho Python ([https://astral.sh/uv](https://astral.sh/uv))
- **Trình duyệt Chromium (Playwright)**: Để chạy interceptor thu thập Microsoft Teams & Outlook Web

---

### 2.2 Hướng Dẫn Cài Đặt Trên macOS (Apple Silicon & Intel)

```bash
# 1. Cài đặt uv (nếu chưa có)
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.cargo/env

# 2. Clone mã nguồn dự án
git clone https://github.com/nxc1802/Personal_Task_Board.git
cd Personal_Task_Board

# 3. Bước 1 (Install): Đồng bộ toàn bộ packages và cài đặt Chromium
uv sync --all-packages
uv run playwright install chromium

# 4. Thiết lập alias lệnh ptb (Tùy chọn tiện ích)
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

# 3. Clone mã nguồn dự án
git clone https://github.com/nxc1802/Personal_Task_Board.git
cd Personal_Task_Board

# 4. Bước 1 (Install): Đồng bộ toàn bộ packages và cài đặt Chromium kèm thư viện hệ thống
uv sync --all-packages
uv run playwright install --with-deps chromium

# 5. Thiết lập alias lệnh ptb
echo 'alias ptb="uv run python scripts/ptb_cli.py"' >> ~/.bashrc
source ~/.bashrc
```

---

### 2.4 Hướng Dẫn Cài Đặt Trên Windows 10/11 (PowerShell)

Mở PowerShell:

```powershell
# 1. Cài đặt uv qua script chính thức
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2. Clone mã nguồn dự án
git clone https://github.com/nxc1802/Personal_Task_Board.git
cd Personal_Task_Board

# 3. Bước 1 (Install): Đồng bộ toàn bộ packages và cài đặt Chromium
uv sync --all-packages
uv run playwright install chromium

# 4. Thiết lập hàm lệnh ptb trong PowerShell Profile
if (!(Test-Path $PROFILE)) { New-Item -Type File -Path $PROFILE -Force }
Add-Content $PROFILE "`nfunction ptb { uv run python scripts/ptb_cli.py `$args }"
. $PROFILE
```

---

## 3. Cấu Hình Chi Tiết (.env & config/*.yaml)

Đây là **Bước 2 (Configure)** trong quy trình chuẩn 8 bước.

### 3.1 Cấu Hình Biến Môi Trường (`.env`)

Khởi tạo tệp `.env` từ tệp mẫu `.env.example`:
```bash
cp .env.example .env
```

Nội dung cấu hình chuẩn trong `.env`:
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
LLM_BASE_URL=http://127.0.0.1:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=qwen2.5:7b
LLM_TEMPERATURE=0.1

# ==============================================================================
# Playwright & Microsoft Session Configuration
# ==============================================================================
PTB_STORAGE_STATE=data/playwright/storage_state.json
PLAYWRIGHT_STORAGE_PATH=data/playwright/storage_state.json

# ==============================================================================
# Network Services & Ports (Localhost Canonical Ports)
# ==============================================================================
APP_HOST=127.0.0.1
APP_PORT=8000
MCP_PORT=8001
OPENWEBUI_PORT=3000
TENANT_ID=local-user
WORKSPACE_ID=local-workspace-001
CURRENT_USER_NAME="Nguyen Cuong"
```

---

### 3.2 Cấu Hình Nguồn Thu Thập Dữ Liệu (`config/sources.yaml`)

Quản lý trạng thái bật/tắt, chu kỳ quét và đường dẫn cho toàn bộ các nguồn thu thập ở Layer 1 (Teams, Outlook, Jira, Shortcut, 9 Coding Agents và Local Git):

```yaml
tenant_id: "local-user"

sources:
  ms_teams:
    enabled: true
    source_type: "ms_teams_web"
    url: "https://teams.microsoft.com"
    headless: true
    storage_state_path: "data/playwright/teams_storage_state.json"
    poll_interval_seconds: 60
    filters:
      chat_types: ["chat", "channel"]
      exclude_system_messages: true

  ms_outlook:
    enabled: true
    source_type: "ms_outlook_web"
    url: "https://outlook.office.com"
    headless: true
    storage_state_path: "data/playwright/outlook_storage_state.json"
    poll_interval_seconds: 120
    filters:
      folders: ["Inbox"]
      ignore_promotions: true

  jira:
    enabled: false
    source_type: "jira"
    base_url: "https://your-domain.atlassian.net"
    email: "${JIRA_EMAIL:-user@example.com}"
    api_token: "${JIRA_API_TOKEN:-}"
    default_jql: "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC"
    project_keys: ["PROJ", "DEV"]
    poll_interval_seconds: 60

  shortcut:
    enabled: false
    source_type: "shortcut"
    base_url: "https://api.app.shortcut.com/api/v3"
    api_token: "${SHORTCUT_API_TOKEN:-}"
    project_ids: []
    default_query: "!is:archived is:story owner:currentUser"
    poll_interval_seconds: 60

  coding_agents:
    enabled: true
    source_type: "coding_agent"
    poll_interval_seconds: 30
    agents:
      cursor:
        enabled: true
      claude_code:
        enabled: true
      antigravity:
        enabled: true
      codex:
        enabled: true
      windsurf:
        enabled: true
      copilot:
        enabled: true
      continue:
        enabled: true
      aider:
        enabled: true
      cline:
        enabled: true

  git:
    enabled: true
    source_type: "git"
    poll_interval_seconds: 60
    repo_paths:
      - "."
    scan_branches:
      - "main"
      - "master"
      - "develop"
```

---

### 3.3 Cấu Hình Trọng Số Ưu Tiên (`config/priority.yaml`)

File `config/priority.yaml` điều khiển công thức tính điểm ưu tiên tất định (`Score = 0–100`) của `DeterministicPriorityEngine` và ngưỡng xét duyệt:

```yaml
weights:
  deadline_weight: 35.0           # Điểm tối đa dựa trên khoảng cách tới deadline (0-35)
  customer_impact_weight: 25.0    # Điểm tối đa cho task liên quan khách hàng/VIP (0-25)
  production_impact_weight: 20.0  # Điểm tối đa cho sự cố production/hotfix/sev-1 (0-20)
  commitment_weight: 10.0         # Trọng số cho cam kết trực tiếp (0-10)
  stale_age_weight: 10.0          # Trọng số cho task tồn đọng lâu ngày (0-10)
  waiting_penalty: 5.0            # Điểm trừ khi task đang chờ phản hồi bên ngoài
  uncertainty_penalty_max: 15.0   # Điểm trừ tối đa khi độ tin cậy trích xuất thấp

thresholds:
  auto_approve_confidence: 0.65   # Tự động phê duyệt nếu confidence >= 0.65
  review_confidence_min: 0.40     # Đưa vào hàng chờ Review nếu 0.40 <= confidence < 0.65
  ignore_confidence_max: 0.40     # Bỏ qua nếu confidence < 0.40
  stale_days_warning: 3           # Cảnh báo tồn đọng sau 3 ngày không cập nhật
  stale_days_critical: 7          # Cảnh báo tồn đọng nghiêm trọng sau 7 ngày
```

---

### 3.4 Cấu Hình Mô Hình LLM & Vector Embeddings (`config/models.yaml`)

Tạo tệp `config/models.yaml` từ tệp mẫu:
```bash
cp config/models.example.yaml config/models.yaml
```

Nội dung cấu hình chuẩn:
```yaml
default_llm_provider: "openai_compatible"
default_embedding_provider: "openai_compatible"

providers:
  openai_compatible:
    base_url: "${LLM_BASE_URL:-https://api.openai.com/v1}"
    api_key: "${LLM_API_KEY:-}"
    extraction_model: "gpt-4o-mini"
    extraction_temperature: 0.1
    extraction_max_tokens: 2048
    planning_model: "gpt-4o"
    planning_temperature: 0.2
    planning_max_tokens: 4096
    embedding_model: "text-embedding-3-small"
    embedding_dimensions: 1536
    timeout_seconds: 45

  ollama:
    base_url: "${OLLAMA_BASE_URL:-http://localhost:11434}"
    extraction_model: "qwen2.5-coder:7b"
    extraction_temperature: 0.1
    extraction_max_tokens: 2048
    planning_model: "llama3.1:8b"
    planning_temperature: 0.2
    planning_max_tokens: 4096
    embedding_model: "nomic-embed-text"
    embedding_dimensions: 768
    timeout_seconds: 60
```

---

## 4. Cẩm Nang Lệnh CLI (CLI Reference Manual)

CLI của Personal Task Board được điều khiển qua entrypoint `scripts/ptb_cli.py` (hoặc alias `ptb`).

```text
Cú pháp tổng quát:
  ptb <lệnh> [tùy chọn...]
```

---

### 4.1 Bước 3 & 4: Khởi Động Neo4j & Lệnh `ptb init`
Trước khi chạy `ptb init`, đảm bảo container Neo4j đã được bật (**Bước 3**):
```bash
docker compose up -d
```

Sau đó thực thi **Bước 4** để khởi tạo schema và constraints trên Neo4j Single-Store:
```bash
ptb init
```

**Chi tiết khởi tạo:**
- Tạo đầy đủ các Uniqueness Constraints và Indexes cho: `RawEvent.id`, `RawEvent.idempotency_key`, `UnifiedTask.id`, `Evidence.id`, `Commitment.id`, `Person.canonical_id`, `SourceIdentity.id`, `IngestionCheckpoint.id`, composite `(tenant_id, source_type, stream_id)`, `ProcessingAttempt.id`, `StatusTransitionAudit.id`, `MergeAudit.id`.

---

### 4.2 Bước 5: Lệnh `ptb login microsoft`
Mở trình duyệt Chromium đồ họa (Headed Mode) để xác thực tài khoản Microsoft 365 phục vụ thu nạp Teams và Outlook Web.

```bash
# Đăng nhập cả Teams và Outlook (mặc định lưu vào data/playwright/storage_state.json)
ptb login microsoft

# Hoặc đăng nhập riêng từng dịch vụ
ptb login microsoft --service teams
ptb login microsoft --service outlook

# Tùy chỉnh đường dẫn lưu trữ session
ptb login microsoft --storage-path data/playwright/storage_state.json
```

---

### 4.3 Bước 6: Lệnh `ptb openwebui install`
Triển khai bộ công cụ PTB Tools, Pipe Functions và giao diện PTB Board vào thư mục dữ liệu `./data/openwebui` (được gắn kết trực tiếp với container OpenWebUI qua volume `./data/openwebui:/app/backend/data`).

```bash
# Triển khai mặc định vào ./data/openwebui và kết nối tới http://127.0.0.1:3000
ptb openwebui install

# Tùy chỉnh URL hoặc thư mục dữ liệu
ptb openwebui install --url http://127.0.0.1:3000 --data-dir ./data/openwebui
```

---

### 4.4 Bước 7: Lệnh `ptb doctor` và `ptb doctor --deep`
Kiểm tra chẩn đoán toàn diện tính sẵn sàng của môi trường và LLM Provider trước khi vận hành.

```bash
# Kiểm tra tiêu chuẩn (không phát sinh request tốn phí tới LLM API)
ptb doctor

# Kiểm tra chuyên sâu (thực hiện HTTP probe trực tiếp tới endpoint /models của LLM Provider)
ptb doctor --deep
```

**6 hạng mục kiểm tra của `ptb doctor`:**
1. **Python Runtime**: Xác nhận phiên bản `>= 3.11`.
2. **Docker Daemon**: Xác nhận tiến trình Docker đang hoạt động.
3. **Neo4j Database (`ptb_neo4j:7687`)**: Kiểm tra kết nối cổng Bolt `127.0.0.1:7687` và container `ptb_neo4j`.
4. **Playwright Chromium Binary**: Xác nhận trình duyệt Chromium đã được cài đặt.
5. **OpenWebUI Service (`Port 3000`)**: Kiểm tra cổng `127.0.0.1:3000`.
6. **LLM Provider Readiness**: Kiểm tra `api_key`, `base_url`, `extraction_model` trong `.env` / `config/models.yaml`. Khi truyền cờ `--deep`, thực hiện truy vấn thực tế tới `{base_url}/models`. Nếu thiếu cấu hình hoặc kết nối thất bại, phát mã lỗi `PTB-LLM-001` và cảnh báo trạng thái xử lý `DEGRADED/NOT_READY`.

---

### 4.5 Bước 8: Lệnh `ptb run` (Process Supervisor)
Khởi chạy và giám sát toàn bộ hệ sinh thái dịch vụ Personal Task Board trong một tiến trình duy nhất với **Shared Runtime Dependency Graph**.

```bash
# Khởi chạy chế độ chuẩn trên 127.0.0.1:8000 (REST) và 127.0.0.1:8001 (MCP)
ptb run

# Tùy chỉnh cổng lắng nghe hoặc chu kỳ worker
ptb run --port 8000 --mcp-port 8001 --poll-interval 2.0

# Chạy không bật trình duyệt Playwright (khi chỉ thu thập từ Coding Agents, Git, Jira, Shortcut)
ptb run --no-playwright

# Chỉ định file cấu hình nguồn thu thập tùy chỉnh
ptb run --config config/sources.yaml
```

**Các dịch vụ được giám sát đồng thời bởi `PTBProcessSupervisor`:**
1. **Neo4j Authoritative Store Verification**: Kiểm tra kết nối Neo4j ngay khi khởi động; nếu thất bại sẽ ghi log `PTB-STORAGE-001`, đặt trạng thái `NOT_READY` và thoát ngay với mã khác `0` (**Fail-Fast**).
2. **Application Service REST API (`127.0.0.1:8000`)**: Cung cấp 14 REST endpoints và kiểm tra sức khỏe sâu tại `GET /health`.
3. **FastMCP Read-Only Server (`127.0.0.1:8001`)**: Cung cấp 10 công cụ MCP qua giao thức SSE và endpoint `GET /health` dùng chung `ApplicationService`.
4. **ProcessingWorker Loop**: Quét `RawEvent` (`PENDING` / `RETRY`), trích xuất `UnifiedTask`, tự động kích hoạt `TaskIntelligenceLifecycle` và đánh dấu đồng bộ Graphiti.
5. **Supervised Acquisition Polling Runners**: Các bộ chạy định kỳ (`AdapterPollingRunner`) có cơ chế retry và exponential backoff độc lập cho:
   - **Coding Agent Watchers** (Cursor, Claude Code, Antigravity, Codex, Windsurf, Copilot, Continue, Aider, Cline)
   - **Local Git Watcher**
   - **Jira Cloud/Server Adapter** (khi được bật và cấu hình credentials)
   - **Shortcut Stories Adapter** (khi được bật và cấu hình credentials)
6. **Playwright Acquisition Daemon**: Chạy ngầm thu thập Microsoft Teams & Outlook Web.

Nhấn `Ctrl+C` để thực hiện **Graceful Shutdown** toàn diện, dừng an toàn các workers, đóng kết nối Uvicorn và giải phóng phiên Neo4j.

---

### 4.6 Lệnh `ptb ingest`
Kích hoạt thủ công một vòng quét và thu nạp dữ liệu tức thì vào Neo4j (yêu cầu Neo4j đang hoạt động theo cơ chế Fail-Fast):

```bash
# Quét toàn bộ nguồn đã kích hoạt
ptb ingest

# Quét riêng từng nguồn cụ thể
ptb ingest --source coding_agent
ptb ingest --source git
ptb ingest --source jira
ptb ingest --source shortcut
```

---

### 4.7 Lệnh `ptb status`
Báo cáo kiểm toán chi tiết về tình trạng hoạt động thực tế của từng phân hệ:
```bash
ptb status
```
- **Neo4j Single-Store**: Trạng thái kết nối, tổng số `RawEvent`, `UnifiedTask` và số lượng constraints.
- **Microsoft Session State**: Trạng thái thực tế (`VALID`, `UNCONFIGURED`, `LOGIN_REQUIRED`, `AUTH_EXPIRED`).
- **Coding Agent Watchers**: Trạng thái từng công cụ (`HEALTHY` kèm số thư mục phát hiện được, hoặc `NOT_INSTALLED` nếu chưa cài trên máy).
- **Processing Queue Status**: Số lượng sự kiện theo từng trạng thái `PENDING`, `PROCESSING`, `RETRY`, `PROCESSED`, `FAILED`.
- **Other Ingestion Adapters**: Trạng thái của Git Watcher, Jira Cloud và Shortcut API.

---

### 4.8 Lệnh `ptb graph rebuild` & `ptb serve`

```bash
# Tái tạo lại bộ nhớ ngữ nghĩa Graphiti từ dữ liệu chuẩn trong Neo4j
ptb graph rebuild

# Khởi chạy riêng lẻ REST API hoặc FastMCP Server
ptb serve --app-only --port 8000
ptb serve --mcp-only --port 8001
```

---

## 5. Cơ Chế Fail-Fast, Truthful Health & 10 Mã Lỗi Chuẩn Hóa

### 5.1 Cơ Chế Truthful Health & Readiness States

Hệ thống báo cáo trung thực tình trạng của mọi thành phần phụ thuộc, tuyệt đối không che giấu lỗi:

1. **Deep Health Endpoint (`GET /health` trên `:8000` và `:8001`)**:
   ```json
   {
     "status": "healthy | degraded | not_ready",
     "service": "ptb-application",
     "neo4j": "healthy | not_ready",
     "processing_worker": "healthy | degraded | not_ready",
     "graphiti": "healthy | degraded",
     "llm": "healthy | degraded",
     "playwright": "valid | unconfigured | login_required | auth_expired"
   }
   ```
   - `healthy`: Toàn bộ thành phần cốt lõi và phụ trợ hoạt động tốt.
   - `degraded`: Cơ sở dữ liệu Neo4j vẫn hoạt động bình thường nhưng phân hệ dẫn xuất (`graphiti`, `llm`, hoặc `processing_worker`) gặp sự cố.
   - `not_ready`: Thành phần cốt lõi (`neo4j` hoặc `processing_worker`) không khả dụng.

2. **Trạng Thái Khởi Động Supervisor (`ptb run`)**:
   - `READY`: Neo4j, REST API (`:8000/health`), FastMCP (`:8001/health`), ProcessingWorker và phiên Playwright đều hoạt động đầy đủ.
   - `READY_WITH_WARNINGS`: Các dịch vụ cốt lõi hoạt động tốt, nhưng phiên Microsoft 365 chưa đăng nhập (`AUTH_REQUIRED` — hướng dẫn chạy `ptb login microsoft`).
   - `DEGRADED`: Nguồn Microsoft được đánh dấu `strict_required: true` nhưng thiếu phiên xác thực, hoặc có phân hệ phụ trợ bị suy giảm.
   - `NOT_READY`: Neo4j không khả dụng hoặc dịch vụ cốt lõi không vượt qua kiểm tra sức khỏe khi khởi động — tiến trình lập tức dừng với `exit code != 0`.

3. **Trạng Thái Nguồn Thu Thập (`/api/sources` & `get_source_health`)**:
   - Phản ánh trung thực 9 trạng thái chuẩn hóa của `SourceSyncState`:
     - `NEVER_SYNCED`: Nguồn đã bật nhưng chưa từng có checkpoint nào được lưu (tuyệt đối không báo `HEALTHY` giả mạo khi chưa có bằng chứng đồng bộ).
     - `UNCONFIGURED`: Nguồn chưa được cung cấp thông tin cấu hình hoặc API token bắt buộc.
     - `AUTH_REQUIRED`: Phiên làm việc Microsoft Teams/Outlook chưa đăng nhập hoặc hết hạn (cần chạy `ptb login microsoft`).
     - `DEGRADED`: Nguồn gặp lỗi tạm thời hoặc đang trong chu kỳ exponential backoff sau khi ghi log `PTB-L1-002`.
     - `HEALTHY`: Nguồn hoạt động bình thường và checkpoint được cập nhật trong ngưỡng thời gian cho phép.
     - `DISABLED`: Nguồn bị tắt trong tệp cấu hình `config/sources.yaml`.
     - `NOT_INSTALLED`: Coding Agent không tìm thấy đường dẫn cài đặt trên máy chủ (không gây lỗi giả).
     - `STARTING`: Nguồn đang trong quá trình khởi động hoặc bắt đầu phiên kết nối.
     - `ERROR`: Nguồn gặp sự cố nghiêm trọng không thể tự phục hồi sau ngưỡng retry tối đa.

---

### 5.2 Bảng 10 Mã Lỗi Chuẩn Hóa (`BugCode`)

Mọi lỗi phát sinh trong runtime đều được ghi nhận qua hàm `log_bug()` (trong `ptb_contracts.logging`) thành bản ghi JSON chuẩn `BugLogRecord`:

| STT | Mã Lỗi (`BugCode`) | Phân Hệ (`subsystem`) | Ý Nghĩa Kỹ Thuật | Cơ Chế Xử Lý Chuẩn (Fail-Fast / Degraded) |
|:---:|:---|:---|:---|:---|
| 1 | `PTB-STORAGE-001` | `neo4j` | Neo4j authoritative store unavailable | Kích hoạt **Fail-Fast**: từ chối chạy trên bộ nhớ tạm, chuyển Supervisor sang `NOT_READY` và thoát tiến trình (`exit != 0`). |
| 2 | `PTB-CKPT-001` | `checkpoint` | Checkpoint read/write inconsistency | Ghi log lỗi bất nhất khi đọc/ghi `IngestionCheckpoint` (`sha256(tenant_id\|source_type\|stream_id)`) và bảo vệ tiến trình đồng bộ. |
| 3 | `PTB-L1-001` | `playwright` | Playwright authentication expired | Khi gặp mã HTTP `401`/`403` trên Teams/Outlook, lập tức cập nhật trạng thái phiên sang `AUTH_EXPIRED` / `AUTH_REQUIRED`, dừng capture và hiển thị yêu cầu đăng nhập lại trên UI. |
| 4 | `PTB-L1-002` | `playwright` / `adapter_*` | Playwright capture/persist failure | Ghi nhận lỗi thu thập tại Layer 1; áp dụng exponential backoff độc lập cho adapter gặp lỗi mà không làm sập các nguồn khác. |
| 5 | `PTB-L2-001` | `processing` | Processing pipeline failure | Lỗi trong pipeline trích xuất hoặc hợp nhất task tại Layer 2; chuyển `RawEvent` sang `RETRY` (tăng `attempt_count` đúng 1 lần) hoặc `FAILED` khi vượt ngưỡng thử lại. |
| 6 | `PTB-LLM-001` | `llm` | LLM provider unavailable | Không có API key, sai URL/model hoặc probe `/models` thất bại; đánh dấu trạng thái LLM/Processing là `DEGRADED` hoặc `NOT_READY`. |
| 7 | `PTB-GRAPH-001` | `graph_memory` | Graphiti unavailable | Lỗi khi đồng bộ episode vào Graphiti; dữ liệu miền (`UnifiedTask`, `Evidence`) vẫn được giữ an toàn trong Neo4j, đánh dấu `graphiti` là `degraded` và thử lại qua `GraphMemoryWorker`. |
| 8 | `PTB-APP-001` | `application` | Application dependency unhealthy | Thành phần phụ thuộc của `ApplicationService` (như Neo4j) không khỏe mạnh; `GET /health` trả về `status: "not_ready"`. |
| 9 | `PTB-MCP-001` | `mcp` | MCP server unhealthy | Máy chủ FastMCP hoặc kết nối tới `ApplicationService` gặp lỗi; `GET /health` trên cổng `:8001` phản ánh trạng thái không sẵn sàng. |
| 10 | `PTB-OWUI-001` | `openwebui` | OpenWebUI cannot reach Application API | Giao diện OpenWebUI Board hoặc Tools không kết nối được tới REST API (`127.0.0.1:8000`); hiển thị rõ thông báo `APPLICATION SERVICE OFFLINE` trên màn hình. |

---

## 6. Quy Trình Vận Hành Hằng Ngày & Xử Lý Sự Cố

### 6.1 Nhịp Vận Hành Hằng Ngày
```text
 08:30 Sáng: Mở OpenWebUI Board (http://127.0.0.1:3000)
             └── Tab 📌 Today: Xem Top tasks ưu tiên cao nhất từ /api/today.
             └── Tab 📥 Review: Phê duyệt (Approve) hoặc loại bỏ (Dismiss) các candidate tasks.
 
 Trong ngày: Làm việc trên Cursor / Claude Code / Antigravity
             └── Coding Agent truy vấn 10 Read-Only MCP Tools tại http://127.0.0.1:8001/sse.
             └── Supervisor tự động thu thập tin nhắn Teams/Outlook, commits Git và logs của Agents.
 
 17:30 Chiều: Rà soát cam kết
             └── Tab ⏳ Waiting: Kiểm tra các đầu việc đang chờ phản hồi (/api/waiting).
             └── Tab 🕰️ Forgotten: Rà soát các cam kết bị tồn đọng (/api/forgotten).
```

### 6.2 Xử Lý Sự Cố Thường Gặp (Troubleshooting FAQs)

- **Q1: `ptb run` hoặc `ptb doctor` báo lỗi `PTB-STORAGE-001` (`Neo4j authoritative store unavailable`)?**
  - **Nguyên nhân**: Container `ptb_neo4j` chưa chạy hoặc cổng `7687` chưa sẵn sàng.
  - **Khắc phục**:
    ```bash
    docker compose up -d
    ptb init
    ptb doctor
    ```

- **Q2: Nguồn Microsoft Teams / Outlook báo `AUTH_REQUIRED` hoặc `AUTH_EXPIRED` (`PTB-L1-001`)?**
  - **Nguyên nhân**: Chưa đăng nhập lần đầu hoặc cookie phiên Microsoft 365 đã hết hạn.
  - **Khắc phục**:
    ```bash
    ptb login microsoft
    ```

- **Q3: `ptb doctor --deep` báo `PTB-LLM-001`?**
  - **Nguyên nhân**: Dịch vụ LLM (Ollama cục bộ hoặc OpenAI-compatible API) chưa bật hoặc chưa cấu hình `LLM_API_KEY` / `LLM_BASE_URL` trong `.env` và `config/models.yaml`.
  - **Khắc phục**: Khởi động dịch vụ LLM (ví dụ `ollama serve`), kiểm tra biến môi trường trong `.env` và chạy lại `ptb doctor --deep`.

- **Q4: Giao diện OpenWebUI hiển thị `APPLICATION SERVICE OFFLINE` (`PTB-OWUI-001`)?**
  - **Nguyên nhân**: Tiến trình `ptb run` chưa được khởi chạy hoặc cổng REST API (`127.0.0.1:8000`) đang dừng.
  - **Khắc phục**: Khởi chạy `ptb run` và kiểm tra `curl http://127.0.0.1:8000/health`.

---

## 7. Quy Chuẩn Kiểm Thử Theo 5 Pytest Markers

Hệ thống kiểm thử của Personal Task Board được phân loại chặt chẽ thành **5 pytest markers** (định nghĩa trong `pyproject.toml`), cho phép thực thi toàn bộ kiểm thử logic, hợp đồng, luồng dọc E2E và khởi chạy Supervisor mà không cần cài đặt Docker trên máy phát triển:

| STT | Marker | Phạm Vi & Nội Dung Kiểm Thử | Cần Docker? |
|:---:|:---|:---|:---:|
| 1 | `unit` | Kiểm thử đơn vị cô lập cho thuật toán tính điểm ưu tiên (`DeterministicPriorityEngine`), máy trạng thái (`StatusInferenceMachine`), bộ phân tích cú pháp interceptor, watchers đa nền tảng và retry logic. | Không |
| 2 | `contract` | Kiểm thử đồng bộ schema giữa Python Pydantic, TypeScript interfaces và Cypher constraints; kiểm tra hợp đồng mã lỗi `BugCode` và kiểm tra tĩnh đảm bảo mã nguồn sạch. | Không |
| 3 | `fixture_e2e` | Kiểm thử luồng dọc toàn trình (Teams/Outlook fixtures → `AcquisitionPipeline` → `ProcessingWorker` → `ProcessingPipeline` → `TaskIntelligenceLifecycle` → `GraphMemoryWorker` → `ApplicationService` → `FastAPI TestClient`) với các kho dữ liệu kiểm thử đặt cô lập trong `tests/support/`. | Không |
| 4 | `runtime_smoke` | Kiểm thử khởi chạy `PTBProcessSupervisor`, gắn kết cổng HTTP cho REST & MCP, xác nhận dùng chung một instance `ApplicationService`, kiểm tra hành vi Fail-Fast khi Neo4j ngừng hoạt động và kiểm tra tắt tiến trình an toàn (graceful shutdown). | Không |
| 5 | `external_integration` | Kiểm thử tích hợp trực tiếp với container Neo4j Single-Store và OpenWebUI thực tế. | **Có** |

### Các Lệnh Thực Thi Kiểm Thử:

```bash
# 1. Chạy toàn bộ bộ kiểm thử mặc định KHÔNG CẦN DOCKER (370+ tests đạt 100% PASS):
uv run pytest -m "not external_integration"

# 2. Kiểm tra bộ 14 behavioral release freeze gates (14/14 PASS):
uv run pytest tests/test_release_readiness.py -v

# 3. Chạy riêng từng phân lớp kiểm thử theo marker:
uv run pytest -m unit
uv run pytest -m contract
uv run pytest -m fixture_e2e
uv run pytest -m runtime_smoke

# 4. Chạy kiểm thử tích hợp với Docker/Neo4j thực tế (khi có môi trường Docker):
uv run pytest -m external_integration
```

*Toàn bộ các bài kiểm thử thuộc nhóm `not external_integration` được thực thi tự động trên GitHub Actions CI Matrix qua 3 hệ điều hành (`ubuntu-latest`, `macos-latest`, `windows-latest`) và 3 phiên bản Python (`3.11`, `3.12`, `3.13`).*
