ánh giá nhanh
Vấn đề cũ	Trạng thái
Neo4j async transaction P0	Đã sửa
LLM OpenAI-compatible/Qwen parsing	Đã sửa
Mixed-state health CI nondeterminism	Đã sửa
Cursor SQLite Windows locking	Đã sửa
Windows Docker doctor	Đã sửa phần lớn
Windows CI	Chưa xanh hoàn toàn
Strict zero production fallback	Chưa đóng
Evidence→RawEvent Cypher compatibility	Chưa port toàn bộ patch E2E
E2E trên chính release SHA mới	Chưa có bằng chứng mới


1. Neo4j P0: đã sửa đúng
Đây là thay đổi quan trọng nhất.
Trước:
async with session.begin_transaction() as tx:    ...


Trong Neo4j async driver thật, begin_transaction() cần được await.
Bây giờ:
tx_obj = session.begin_transaction()tx = await tx_obj if inspect.isawaitable(tx_obj) else tx_obj


Commit còn xử lý cả:
commit()rollback()


theo cùng cách:
if inspect.isawaitable(c_res):    await c_res


Điểm này thậm chí tốt hơn patch ban đầu trên Qang2909, vì nó tương thích cả:
real Neo4j AsyncSession
+
synchronous unit-test doubles

split_task() cũng được sửa cùng pattern.
=> P0 async transaction mình nêu trước đã đóng.
2. LLM OpenAI-compatible: đã sửa đúng
Patch từ Plan 2 đã được port.
Đã bỏ:
"response_format": {"type": "json_object"}


vì không phải mọi OpenAI-compatible provider đều support.
Thêm parser cho:
raw JSON
```json fences
<think>...</think>
text + JSON

Flow giờ:
LLM response
→ clean <think>
→ direct JSON
→ markdown JSON
→ extract JSON object
→ Pydantic validation

Điều này phù hợp hơn nhiều với yêu cầu:
dùng các model thông qua API chuẩn OpenAI-compatible.

=> issue compatibility với Qwen/custom gateway đã đóng.
3. Mixed-state Source Health: fix CI đúng
Lỗi lần trước là test:
Outlook expected NEVER_SYNCED

nhưng test environment không có Microsoft session nên production code đúng khi trả:
UNCONFIGURED

Commit mới đã thêm:
playwright_status="healthy"


vào test scenario.
Đây là sửa đúng test setup, không phải bóp production logic cho test pass.
Quan trọng hơn, trên CI mới:
Unit Tests              PASS
Fixture E2E             PASS
Runtime Smoke           PASS
Full Non-Docker Suite   PASS

trên Windows, Linux, macOS.
Vì vậy Plan 1 hiện đã đáng tin hơn hẳn.
4. Cursor watcher Windows locking: fix tốt
Có thêm:
conn = Nonetry:    conn = sqlite3.connect(...)    ...finally:    if conn:        conn.close()


Điều này xử lý WinError 32 khi Cursor SQLite database lỗi/đang bị giữ.
Đây là đúng loại bug chỉ dễ lộ trên Windows.
5. Test isolation Windows được cải thiện
Hai fix hợp lý:
platformdirs tests
→ không phụ thuộc Cursor thật đang cài trên GitHub runner

và:
CLI supervisor tests
→ không mock toàn bộ socket.socket

Mock toàn bộ socket.socket trước đây phá Windows ProactorEventLoop.
Bây giờ chỉ:
patch.object(socket.socket, "connect", ...)


Đúng scope hơn.
Nhưng CI vẫn chưa “100% green”
Đây là điểm commit message hiện đang overclaim.
GitHub Actions của chính SHA a644ae7:
Ubuntu 3.11  PASS
Ubuntu 3.12  PASS
Ubuntu 3.13  PASS

macOS 3.11   PASS
macOS 3.12   PASS
macOS 3.13   PASS

Windows 3.11 FAIL
Windows 3.12 FAIL
Windows 3.13 FAIL

Tức 6/9 jobs green, không phải 9/9.
Nhưng tin tốt là cả 3 Windows đều chạy được:
Anti-Fallback           PASS
Unit                    PASS
Fixture E2E             PASS
Runtime Smoke           PASS
Full Non-Docker Suite   PASS

Chúng chỉ fail ở bước cuối:
Verify CLI Help & Doctor Syntax

với:
uv run python scripts/ptb_cli.py --help

6. Windows CLI vẫn có Unicode bug
Error thật:
UnicodeEncodeError:
'charmap' codec can't encode character '\u1ec7'

Nguyên nhân: patch hiện chỉ bảo vệ:
✓
✗

trong doctor.
Nhưng argparse --help vẫn có hàng loạt text tiếng Việt:
help="Lệnh thực thi"help="Khởi chạy và giám sát..."help="Chu kỳ thăm dò..."help="Kiểm tra môi trường..."


GitHub Windows console sử dụng encoding không biểu diễn được một số ký tự tiếng Việt.
Do vậy:
ptb --help

crash trên Windows.
Severity
Mình xếp P1 cross-platform usability, không phải P0 core data.
Nhưng vì requirement frozen là:
Cross-platform

nó vẫn là release blocker trước khi gọi V1 Stable.
Fix hợp lý
Đừng đổi toàn bộ help sang ASCII chỉ để pass CI.
Nên configure stdout UTF-8 ở CLI entrypoint, ví dụ xử lý:
sys.stdout.reconfigure(encoding="utf-8", errors="replace")sys.stderr.reconfigure(encoding="utf-8", errors="replace")


khi supported.
Sau đó:
ptb --help
ptb doctor

phải pass trên Windows.
7. Một phần patch Neo4j từ E2E branch chưa được port
Đây là điểm cần chú ý.
Trên Qang2909, E2E hotfix còn thay:
Evidence
→ DERIVED_FROM
→ RawEvent

từ subquery:
CALL {
    ...
}

thành một Cypher query riêng.
Current main vẫn dùng subquery cũ:
MERGE (t)-[:HAS_EVIDENCE]->(e)
WITH e, ev
CALL {
    ...
    MATCH (re:RawEvent ...)
    MERGE (e)-[:DERIVED_FROM]->(re)
}

Trong khi E2E branch đã đổi thành:
Query 1:
Task → Evidence

Query 2:
Evidence → RawEvent

Mình chưa đủ evidence để nói subquery hiện tại chắc chắn sai trên Neo4j 5.26 — cú pháp nhìn về nguyên tắc có thể hợp lệ.
Nhưng vì thay đổi này được tạo trong quá trình live E2E, không nên bỏ qua.
Recommendation
Không nhất thiết port ngay mà không kiểm chứng.
Sau khi sửa Windows CLI, chạy trên chính a644ae7 + CLI fix:
real Neo4j
→ create RawEvent
→ process
→ TaskDomainRepository.upsert_task_atomic()
→ verify HAS_EVIDENCE
→ verify DERIVED_FROM

Nếu current query pass thật:
không cần port Qang version

Nếu fail:
port query separation

Mình xếp đây là P1 validation gap, chưa gọi là confirmed P0.
8. Zero-production-fallback vẫn chưa đúng theo requirement bạn đã đặt
Đây là vấn đề mình vẫn giữ nguyên đánh giá.
Production:
class LLMStructuredExtractor:


vẫn chứa:
mock_mode: bool = Falseallow_heuristic_fallbackset_mock_handler()_fallback_rule_based_extract()


và đặc biệt:
PTB_ALLOW_HEURISTIC_FALLBACK=true

có thể làm production:
LLM down
→ heuristic extraction
→ tiếp tục tạo Task

Trong khi requirement bạn từng đặt rất rõ:
Xóa toàn bộ fallback mockup, thay thế bằng log bug.

Anti-fallback guard hiện chỉ bắt:
mock_mode=True

chứ không bắt sự tồn tại của production fallback implementation.
Vì vậy test:
Anti-Fallback PASS

không đồng nghĩa production đã không còn fallback.
Theo architecture đã freeze, nên là
LLM unavailable
    ↓
PTB-LLM-001
    ↓
retry / FAILED
    ↓
DEGRADED

không:
LLM unavailable
    ↓
heuristic task extraction

Test fake extractor nên nằm riêng:
tests/support/

không nằm trong production extractor.
Mình xếp đây là P1 architectural compliance.
9. Graphiti “fallback” thì khác
GraphitiAdapter vẫn dùng terminology:
graceful fallback mode

nhưng mình không xếp nó cùng loại với LLM heuristic fallback.
Lý do Graphiti là derived semantic layer.
Sync worker hiện kiểm tra:
if not adapter.is_available:    raise RuntimeError(...)


và sau call còn kiểm tra:
adapter.last_error


Failure dẫn tới:
PTB-GRAPH-001
RETRY
FAILED
system degraded

chứ không fake:
SYNCED

Authoritative Task/Evidence vẫn tồn tại, đúng architecture.
Vì vậy phần Graphiti này chỉ nên đổi wording:
fallback mode
→ degraded/unavailable mode

Mình coi là P2 cleanup, không phải release blocker.
10. Có warnings đáng theo dõi trong CI
CI xanh phần tests nhưng có warnings:
RuntimeWarning:
coroutine 'Neo4jDriver._execute_index_query' was never awaited

xuất hiện trong một số mocked tests.
Hiện chưa làm test fail và khả năng cao là mock/test lifecycle issue, nhưng nên clean trước hoặc sau V1 stable để tránh che mất async resource bug thật.
P2 test hygiene.
11. Hai file documentation mới khá yếu
Commit thêm:
Embedding model.md
docs/v1_verify_1.md

nhưng chúng chủ yếu chứa Cloudflare temporary URLs/model endpoint notes.
Thậm chí có sample JSON malformed kiểu:
"input"🙁"Xin chào"]

và các Cloudflare URLs có tính ephemeral.
Không ảnh hưởng runtime, nhưng không nên coi đây là release source-of-truth.
Nên chuyển sang:
config / env examples

với placeholders:
LLM_BASE_URL=<your-openai-compatible-url>
EMBEDDING_BASE_URL=<...>

Còn endpoint E2E cụ thể nên ở private/local evidence log hoặc sanitized verification report.
P2 documentation cleanup.
Tổng quan commit a644ae7
Mình đánh giá commit này tốt và rất có giá trị. Nó thực sự port phần lớn bug được tìm ra từ E2E về main, chứ không chỉ chỉnh test.
Trước commit
Main:
- Neo4j real transaction broken
- custom LLM compatibility gap
- health CI red
- Windows watcher/test problems

Sau commit
Neo4j transaction           FIXED
LLM parsing                 FIXED
Mixed health                FIXED
Cursor Windows locking      FIXED
Unit tests                  GREEN all OS
Fixture E2E                 GREEN all OS
Runtime smoke               GREEN all OS
Full non-external suite     GREEN all OS
Windows CLI --help          STILL FAIL
Strict LLM no-fallback      STILL OPEN
Real release-SHA E2E        NEEDS RE-RUN

Readiness hiện tại
Mình sẽ nâng điểm so với lần trước:
Tiêu chí	Trước	Hiện tại
Architecture	98%	98%
Feature implementation	97%	98%
Storage correctness	95%	98%
OpenAI-compatible LLM	~85%	96%
Cross-platform tests	~80%	94%
Source health	~95%	98%
Runtime wiring	95%	97%
Production failure semantics	94%	94%
External E2E confidence	~90%	~92%
Code readiness	~94%	~97%
Ready-to-use confidence	~90%	~94–95%


Còn 3 việc mình coi là thực sự cần trước V1 Stable
1. Fix Windows ptb --help Unicode, rồi bắt buộc GitHub Actions 9/9 green.
2. Đóng strict LLM fallback issue nếu vẫn giữ yêu cầu “production không có heuristic/mock fallback”.
3. Chạy lại critical E2E trên chính commit release cuối cùng, ít nhất:
   init → real Neo4j → Git/RawEvent → LLM → Task/Evidence → DERIVED_FROM → Graphiti SYNCED → REST/MCP/OpenWebUI → restart/resume/dedup.
Sau ba việc đó, mình sẽ không còn thấy blocker đáng kể để gọi project là Ready to use / V1 Stable.