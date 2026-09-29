> **Dựng môi trường local thật → hoàn thành Init Phase → chạy Continuous Phase → chứng minh vertical pipeline E2E bằng dữ liệu thật → restart/resume không duplicate.**
---

# PLAN — Local Bring-up & Real E2E Verification

## 0. Phạm vi và nguyên tắc

Baseline E2E đầu tiên nên giảm tối đa biến số:

| Thành phần | Lựa chọn E2E đầu tiên |
|---|---|
| Python | **3.11** |
| Python env | `.venv` do `uv` quản lý |
| Package install | `uv sync --all-packages` |
| Infrastructure | **Docker Compose** |
| Database | **Neo4j 5.26 Community** |
| UI | **OpenWebUI container** |
| Microsoft acquisition | **Playwright**, không Graph API |
| Processing LLM | OpenAI-compatible cloud |
| Graphiti LLM | OpenAI |
| Embedding | OpenAI `text-embedding-3-small` |
| Jira | Disabled ở first E2E |
| Shortcut | Disabled ở first E2E |
| Git | Enabled |
| Coding Agents | Enabled |
| Teams/Outlook | Enabled |

Lý do chọn OpenAI cho first E2E: Graphiti hiện mặc định dùng OpenAI cho inference và embedding nếu repo không inject client riêng. Upstream cũng yêu cầu `OPENAI_API_KEY` cho default path; local/OpenAI-compatible model có thể dùng, nhưng phải cấu hình explicit clients. :chatgpt-content-reference{index="0"}

Do đó **không nên đưa Ollama/local LLM vào first E2E**. Hãy chứng minh V1 chạy trước, rồi mới tối ưu sang local inference.

---

# PHASE A — Machine Preflight

## A1. Các phần mềm bắt buộc

Máy cần có:

```text
Git
Python 3.11
pip
uv
Docker Engine / Docker Desktop
Docker Compose v2
Internet access
Microsoft 365 account
OpenAI API key
```

OpenWebUI hiện khuyến nghị Docker cho phần lớn local deployment và dùng `host.docker.internal` để container truy cập service trên host. :chatgpt-content-reference{index="1"}

## A2. Preflight commands

```bash
git --version
python --version
python -m pip --version
docker --version
docker compose version
docker info
```

Expected:

```text
Python >= 3.11
Docker daemon reachable
Docker Compose v2 reachable
```

**Hard gate:**

```text
docker info FAIL
→ STOP
```

Không tiếp tục Plan 2 cho tới khi Docker hoạt động.

## A3. Kiểm tra ports

Các port cần trống:

| Port | Service |
|---:|---|
| 3000 | OpenWebUI |
| 7474 | Neo4j Browser |
| 7687 | Neo4j Bolt |
| 8000 | PTB REST |
| 8001 | PTB MCP |

Nếu một port đã bị chiếm, giải quyết trước khi bring-up.

---

# PHASE B — Repository & Python Environment

## B1. Checkout source

```bash
git clone <repository-url>
cd Personal_Task_Board
git status
git rev-parse HEAD
```

Ghi lại SHA vào E2E report.

Nếu Plan 1 vừa được merge, checkout đúng commit đó.

## B2. Bootstrap pip + uv

```bash
python -m pip install --upgrade pip
python -m pip install --upgrade uv
```

## B3. Tạo virtual environment

Canonical path:

```bash
uv venv --python 3.11
```

Expected:

```text
./.venv
```

Không cần dùng `pip install -e` từng package.

## B4. Install toàn workspace

```bash
uv sync --all-packages
```

Đây là bước install chính thức.

Nó phải kéo được toàn bộ workspace bao gồm:

```text
ptb-contracts
ptb-database
ptb-acquisition
ptb-processing
ptb-intelligence
ptb-graph-memory
ptb-application
ptb-mcp

playwright
neo4j driver
graphiti-core
fastapi
mcp
pydantic
...
```

## B5. Cài Chromium cho Playwright

Python package Playwright không bao gồm browser binary.

```bash
uv run playwright install chromium
```

Nếu Linux và browser thiếu system dependencies, xử lý các dependency mà Playwright báo trước khi đi tiếp.

## B6. Baseline code validation

Trước khi đưa Docker/LLM vào, chạy:

```bash
uv run pytest -m "not external_integration"
```

Expected theo baseline hiện tại:

```text
all non-external tests PASS
```

Nếu fail ở đây:

```text
STOP E2E
→ đây là regression code, không phải infrastructure issue
```

---

# PHASE C — Runtime Configuration

## C1. Tạo `.env`

Copy:

```text
.env.example
→ .env
```

Cấu hình tối thiểu:

```dotenv
NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=<choose-a-real-local-password>
NEO4J_DATABASE=neo4j

APP_HOST=127.0.0.1
APP_PORT=8000

MCP_HOST=127.0.0.1
MCP_PORT=8001

OPENWEBUI_PORT=3000

LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=<your-key>
LLM_MODEL=gpt-4o-mini
LLM_TEMPERATURE=0.1

OPENAI_API_KEY=<your-key>

TENANT_ID=local-user
WORKSPACE_ID=local-workspace-001

PTB_STORAGE_STATE=data/playwright/storage_state.json
```

Hai biến sau **không nên gộp về mặt ý nghĩa**, dù first E2E có thể dùng cùng key:

```text
LLM_API_KEY
→ PTB structured extraction

OPENAI_API_KEY
→ Graphiti default LLM/embedder
```

## C2. Models config

Tạo:

```text
config/models.example.yaml
→ config/models.yaml
```

First E2E profile:

```yaml
default_llm_provider: openai_compatible
default_embedding_provider: openai_compatible
```

Extraction:

```text
gpt-4o-mini
```

Embedding:

```text
text-embedding-3-small
dimensions = 1536
```

`text-embedding-3-small` cũng là default OpenAI embedder được Graphiti sử dụng trong các cấu hình hiện tại. :chatgpt-content-reference{index="2"}

## C3. Sources config

First E2E nên giữ:

```text
Teams         enabled
Outlook       enabled
Git           enabled
Coding Agents enabled

Jira          disabled
Shortcut      disabled
```

Không bật Jira/Shortcut chỉ để làm E2E nếu chưa có credential thật.

Mục tiêu là chứng minh pipeline, không phải tăng số external dependencies.

---

# INIT PHASE

Init Phase trả lời câu hỏi:

> **Máy có đủ dependency và infrastructure để PTB khởi động đúng hay chưa?**

Không test continuous processing ở đây.

---

# PHASE D — Docker Infrastructure Bring-up

## D1. Pull images

```bash
docker compose pull
```

Current compose cần:

```text
neo4j:5.26.0-community
ghcr.io/open-webui/open-webui:v0.5.10
```

## D2. Start containers

```bash
docker compose up -d
```

Sau đó:

```bash
docker compose ps
```

Expected:

```text
ptb_neo4j       Up
ptb_openwebui   Up
```

## D3. Inspect logs

```bash
docker compose logs neo4j
docker compose logs open-webui
```

Không được có repeated fatal/restart loop.

## D4. Basic network validation

Neo4j UI:

```text
http://127.0.0.1:7474
```

OpenWebUI:

```text
http://127.0.0.1:3000
```

Bolt:

```text
127.0.0.1:7687
```

OpenWebUI container đang dùng `host.docker.internal:host-gateway`, đúng với pattern hiện được tài liệu OpenWebUI khuyến nghị để container gọi service trên host. :chatgpt-content-reference{index="3"}

---

# PHASE E — Neo4j Initialization

## E1. Chạy init

```bash
uv run python scripts/ptb_cli.py init
```

Expected:

```text
exit code = 0
Neo4j connectivity = OK
constraints/indexes created
seed/setup completed
```

Nếu:

```text
PTB-STORAGE-001
```

thì Init Phase FAIL.

## E2. Verify schema thật

Không chỉ tin CLI message.

Mở Neo4j Browser và kiểm tra constraints/indexes.

Ít nhất phải có constraint tương ứng cho:

```text
RawEvent.id
RawEvent.idempotency_key
UnifiedTask.id
Evidence.id
IngestionCheckpoint composite
ProcessingAttempt.id
StatusTransitionAudit.id
MergeAudit.id
```

Checkpoint phải có composite:

```text
tenant_id
source_type
stream_id
```

## E3. Simple read/write smoke

Từ Neo4j Browser hoặc script test:

```cypher
RETURN 1 AS ok;
```

Sau đó verify database đúng:

```cypher
SHOW CONSTRAINTS;
```

---

# PHASE F — LLM + Embedding Readiness

## F1. Check LLM endpoint

Dùng:

```bash
uv run python scripts/ptb_cli.py doctor --deep
```

Deep doctor phải xác nhận provider thực sự reachable.

Expected:

```text
LLM configured
/model endpoint or equivalent reachable
model usable
```

Không chỉ kiểm tra key tồn tại.

## F2. Verify Graphiti environment

Graphiti phải thấy:

```text
OPENAI_API_KEY
Neo4j URI
Neo4j credentials
```

First E2E chưa cần gọi full graph rebuild.

Chỉ cần đảm bảo Graphiti adapter initialize được.

## F3. Embedding smoke

Mục tiêu:

```text
one real embedding request succeeds
dimension = 1536
```

Nếu Graphiti sync sau này fail vì embedding model:

```text
INIT/Graphiti readiness = FAIL
```

Không coi `Processing` success là đủ.

---

# PHASE G — Microsoft Authentication

## G1. Login

```bash
uv run python scripts/ptb_cli.py login microsoft
```

Hoặc rõ hơn:

```bash
uv run python scripts/ptb_cli.py login microsoft --service all
```

Playwright phải chạy **headed** ở bước login.

Login cả:

```text
Teams Web
Outlook Web
```

## G2. Session artifact

Expected tồn tại:

```text
data/playwright/storage_state.json
```

File phải non-empty.

## G3. Re-validation

Sau login:

```bash
uv run python scripts/ptb_cli.py status
```

hoặc doctor tương ứng.

Expected Microsoft không còn:

```text
LOGIN_REQUIRED
AUTH_EXPIRED
```

---

# PHASE H — OpenWebUI Installation Verification

Đây là gate rất quan trọng.

## H1. Run installer

```bash
uv run python scripts/ptb_cli.py openwebui install
```

Installer phải báo:

```text
success
validation_ok=True
```

## H2. Filesystem validation

Expected ít nhất:

```text
data/openwebui/tools/ptb_tools.py
data/openwebui/tools/ptb_tools.json

data/openwebui/functions/ptb_board_action.py
data/openwebui/functions/ptb_board_action.json

data/openwebui/board/ptb_board.html

data/openwebui/ptb_manifest.json
```

## H3. Live OpenWebUI validation

Đây mới là phần quyết định.

Mở:

```text
http://127.0.0.1:3000
```

Xác minh:

```text
PTB Tool xuất hiện / load được
PTB Action/Function load được
Board mở được
```

Nếu:

```text
installer success
nhưng OpenWebUI không nhận Tool/Function
```

thì:

```text
INIT PHASE = FAIL
```

Không workaround bằng manual import rồi gọi installer là PASS.

Đây sẽ trở thành một coding issue riêng sau E2E.

---

# PHASE I — Final Init Doctor

Sau khi mọi dependency đã lên:

```bash
uv run python scripts/ptb_cli.py doctor --deep
```

Definition of Done cho **Init Phase**:

| Component | Required |
|---|:---:|
| Python 3.11 | PASS |
| `.venv` | PASS |
| Workspace dependencies | PASS |
| Chromium | PASS |
| Docker | PASS |
| Neo4j container | PASS |
| OpenWebUI container | PASS |
| Neo4j connectivity | PASS |
| Neo4j schema | PASS |
| LLM API | PASS |
| Embedding API | PASS |
| Graphiti init | PASS |
| Microsoft login | PASS |
| storage_state | PASS |
| OpenWebUI PTB installation | PASS |
| `doctor --deep` | PASS |

**Chỉ khi toàn bộ bảng PASS mới chuyển sang Continuous Phase.**

---

# CONTINUOUS PHASE

Continuous Phase trả lời:

> **PTB có chạy liên tục, ingest dữ liệu thật, xử lý đúng, expose REST/MCP/UI và restart đúng hay không?**

---

# PHASE J — Start Full Runtime

## J1. Run supervisor

```bash
uv run python scripts/ptb_cli.py run
```

Expected processes/tasks:

```text
Application REST             :8000
FastMCP                      :8001
ProcessingWorker
GraphMemorySyncWorker
Playwright acquisition
Git adapter
Coding Agent watcher
```

Nếu Teams/Outlook login hợp lệ:

```text
READY
```

Nếu vẫn:

```text
READY_WITH_WARNINGS
AUTH_REQUIRED
```

thì Microsoft E2E chưa sẵn sàng.

## J2. Keep this process running

Không chạy `ptb ingest` để thay thế Continuous Phase.

Mục tiêu là kiểm tra background supervisor thật.

---

# PHASE K — Runtime Health Gate

## K1. REST health

```text
GET http://127.0.0.1:8000/health
```

Expected:

```text
Neo4j      healthy
Processing healthy
Graphiti   healthy
LLM        healthy
```

Playwright phải phù hợp với trạng thái session.

## K2. MCP health

```text
GET http://127.0.0.1:8001/health
```

Expected non-503.

## K3. Sources

```text
GET /api/sources/health
```

Sau Plan 1 phải thấy **toàn bộ source**, kể cả source chưa sync.

---

# PHASE L — E2E Scenario 1: Git Deterministic Flow

Đây là E2E đầu tiên vì Git dễ kiểm soát nhất.

## L1. Create test signal

Tạo một thay đổi test có nội dung rõ ràng rồi commit.

Ví dụ commit message có một task/action rõ để extractor nhận biết.

## L2. Wait one poll cycle

Git hiện poll theo config khoảng:

```text
60 seconds
```

Không chỉnh xuống quá thấp cho production validation trừ khi dùng temporary E2E config rõ ràng.

## L3. Verify RawEvent

Neo4j:

```cypher
MATCH (r:RawEvent)
RETURN r
ORDER BY r.captured_at DESC
LIMIT 10;
```

Phải thấy event Git mới.

## L4. Verify Processing

Expected lifecycle:

```text
PENDING
→ PROCESSING
→ PROCESSED
```

Không:

```text
FAILED
infinite RETRY
```

## L5. Verify domain

Query:

```cypher
MATCH (t:UnifiedTask)
OPTIONAL MATCH (t)-[:HAS_EVIDENCE]->(e:Evidence)
RETURN t, collect(e)
ORDER BY t.updated_at DESC
LIMIT 10;
```

Expected:

```text
UnifiedTask exists
Evidence exists
Evidence links RawEvent
```

## L6. Verify Intelligence

Task phải có:

```text
priority/inferred priority
status/inferred status
updated fields
```

## L7. Verify Graphiti sync

Evidence:

```text
graph_sync_status = SYNCED
```

Không để:

```text
PENDING mãi
RETRY mãi
```

Nếu `RETRY`, đọc `graph_last_error`.

---

# PHASE M — E2E Scenario 2: Teams Real Acquisition

Đây là test quan trọng nhất cho architecture đã freeze.

## M1. Generate a controlled Teams event

Tạo một message/request thật nhưng dành riêng cho E2E, đủ rõ để extraction nhận biết.

## M2. Verify Layer 1

Expected:

```text
Teams browser response
→ Playwright interceptor
→ RawEvent Neo4j
```

Không Graph API.

## M3. Verify complete slice

```text
RawEvent
→ ProcessingWorker
→ LLM extraction
→ correlation
→ UnifiedTask
→ Evidence
→ Intelligence
→ Graph sync
```

Ghi lại IDs:

```text
raw_event_id
task_id
evidence_id
checkpoint id
```

Đây sẽ là evidence cho E2E report.

---

# PHASE N — E2E Scenario 3: Outlook

Làm tương tự Teams bằng một email test có action/request rõ ràng.

Phải chứng minh độc lập:

```text
Outlook
→ Playwright
→ RawEvent
→ Task/Evidence
```

Không chỉ Teams pass rồi suy luận Outlook cũng pass.

---

# PHASE O — REST Functional Verification

Kiểm tra ít nhất:

```text
GET /api/tasks
GET /api/today
GET /api/review
GET /api/waiting
GET /api/forgotten
GET /api/sources/health
```

Task E2E vừa tạo phải xuất hiện qua REST.

Sau đó test một mutation thật:

```text
TODO → IN_PROGRESS
```

Reload API và Neo4j.

State phải persisted.

---

# PHASE P — MCP Functional Verification

Dùng MCP client/coding agent hoặc direct MCP test.

Bắt buộc gọi:

```text
get_today_tasks
get_tasks
get_task_context
get_source_health
search_context
```

Điều quan trọng:

```text
REST data == MCP data
```

Không được có hai ApplicationService universe khác nhau.

---

# PHASE Q — OpenWebUI Functional Verification

Mở:

```text
http://127.0.0.1:3000
```

Verify 8 views:

```text
Today
Inbox/Review
All Tasks
Waiting
Forgotten
Decisions
Lessons
Sources/Health
```

Task E2E phải xuất hiện.

Sau đó test một mutation từ UI:

```text
status change
```

Reload browser.

Reload REST.

Reload Neo4j.

Tất cả phải cùng state.

---

# PHASE R — Graphiti Semantic Verification

Không chỉ kiểm tra `graph_sync_status=SYNCED`.

Thực hiện semantic search:

```text
search_context
```

với nội dung liên quan task vừa ingest.

Expected:

```text
Graphiti returns relevant Evidence/Decision/Lesson context
```

Điều này chứng minh:

```text
Episode write
+
embedding
+
semantic retrieval
```

thật sự hoạt động.

---

# PHASE S — Restart & Checkpoint Resume

Đây là release gate quan trọng nhất sau vertical slice.

## S1. Snapshot state trước restart

Ghi lại:

```text
RawEvent count
UnifiedTask count
Evidence count
checkpoints
last_external_id
last_event_timestamp
```

## S2. Graceful stop

`Ctrl+C`

Expected supervisor shutdown sạch.

Không kill -9 ở test chính.

## S3. Restart

```bash
uv run python scripts/ptb_cli.py run
```

## S4. Verify no duplicate

Sau vài polling cycles:

```text
RawEvent cũ không duplicate
Task cũ không duplicate
Evidence cũ không duplicate
checkpoint vẫn dùng đúng tenant
```

## S5. Generate one new event

Tạo một Git/Teams event mới.

Expected:

```text
resume from previous checkpoint
→ only new event ingested
```

Đây là bằng chứng trực tiếp checkpoint fix hoạt động production-like.

---

# PHASE T — Sustained Continuous Run

Để supervisor chạy nhiều polling cycles.

Theo dõi:

```text
Processing backlog
Graph sync backlog
adapter errors
checkpoint progression
memory/process stability
```

Expected:

```text
Processing PENDING backlog → drains
Graph PENDING backlog → drains
retry counters không tăng vô hạn
no repeated critical PTB-* errors
```

Không cần stress/load test trong V1 E2E.

---

# PHASE U — Failure Semantics Smoke

Không cần destructive testing sâu, nhưng nên chứng minh các failure policy cốt lõi.

### Neo4j

Tạm stop Neo4j sau khi đã có baseline:

```bash
docker compose stop neo4j
```

Expected:

```text
NOT_READY / health failure
không RAM fallback
PTB-STORAGE-001
```

Start lại:

```bash
docker compose start neo4j
```

### Graphiti/API provider

Có thể dùng invalid key trong một isolated run.

Expected:

```text
task authoritative data survives
Graph sync RETRY/FAILED
PTB-GRAPH-001
system DEGRADED
```

Không được rollback task.

### Microsoft auth

Session invalid/expired:

```text
AUTH_REQUIRED
```

không fabricated `HEALTHY`.

---

# PHASE V — External Integration Tests

Khi infrastructure đang chạy:

```bash
uv run pytest -m external_integration -v
```

Có hai trường hợp.

Nếu tests tồn tại và run:

```text
must PASS
```

Nếu:

```text
0 tests collected
```

thì phải ghi rõ:

```text
external integration automated coverage = missing
```

Manual E2E vẫn có giá trị, nhưng không được report automated external tests là PASS.

---

# PHASE W — Evidence Pack

Để quyết định Stable, không nên chỉ dựa vào “mình thấy chạy”.

Lưu lại:

| Evidence | Nội dung |
|---|---|
| Commit SHA | source version |
| Python version | exact |
| Docker version | exact |
| Compose version | exact |
| Neo4j image | exact |
| OpenWebUI image | exact |
| LLM model | exact |
| Embedding model | exact |
| `ptb init` | PASS output |
| `doctor --deep` | PASS output |
| REST health | JSON |
| MCP health | JSON |
| Git RawEvent ID | ID |
| Teams RawEvent ID | ID |
| Outlook RawEvent ID | ID |
| UnifiedTask IDs | IDs |
| Graph sync status | SYNCED |
| Restart counts | before/after |
| External tests | PASS/PENDING/none |

---

# Final Acceptance: Init Phase

**INIT PASS** chỉ khi:

```text
Python                    PASS
venv                      PASS
uv sync                   PASS
Chromium                  PASS
Docker                    PASS
Neo4j                     PASS
OpenWebUI                 PASS
Neo4j schema/init         PASS
LLM                       PASS
Embedding                 PASS
Graphiti                  PASS
Microsoft session         PASS
OpenWebUI integration     PASS
doctor --deep             PASS
```

---

# Final Acceptance: Continuous Phase

**CONTINUOUS PASS** chỉ khi:

```text
Supervisor READY                   PASS
REST                              PASS
MCP                               PASS

Git → RawEvent                    PASS
Teams → RawEvent                  PASS
Outlook → RawEvent                PASS

RawEvent → Processing             PASS
Processing → Task/Evidence        PASS
Intelligence                     PASS
GraphMemory PENDING→SYNCED       PASS
Semantic search                  PASS

REST reads same task             PASS
MCP reads same task              PASS
OpenWebUI reads same task        PASS
OpenWebUI mutation persists      PASS

Graceful restart                 PASS
Checkpoint resume                PASS
No duplicate RawEvent            PASS
No duplicate task/evidence       PASS
New events after restart         PASS
```

---

# Khi nào V1 được gọi là Stable?

Mình sẽ dùng gate:

```text
Plan 1 Coding Fix
        ↓
INIT PHASE PASS
        ↓
CONTINUOUS PHASE PASS
        ↓
RESTART/RESUME PASS
        ↓
NO P0/P1 runtime issue
        ↓
V1 STABLE
```

Nếu `Init` và `Continuous` đều PASS nhưng OpenWebUI Tool/Function không tự đăng ký, thì **core PTB có thể đã hoạt động**, nhưng V1 vẫn chưa Stable theo architecture đã freeze vì OpenWebUI là frontend chính thức.

Ngược lại, nếu toàn bộ flow trên PASS, lúc đó gần như không còn lý do kỹ thuật để tiếp tục thêm feature trước khi tag/release **V1 Stable**.