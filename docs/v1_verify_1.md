Chưa nên gọi dự án là Ready to use trên main. Sau khi kiểm tra cả hai branch, trạng thái hợp lý nhất hiện tại là Release Candidate đã chạy E2E và đã tìm ra các lỗi production thật, nhưng các hotfix E2E chưa được hợp nhất về release branch.
Điểm quan trọng nhất: Plan 2 đã làm đúng công việc của nó — khi chạy với infrastructure thật, nó đã lộ ra những bug mà 376 test mock/contract trên main không bắt được.
Kết luận theo từng branch
Branch	Trạng thái
main — b8dfc9b	Chưa Ready to use
Qang2909 — 9954873	Gần runnable trên môi trường đã test, nhưng chưa phải release branch
Project tổng thể	Late RC / stabilization, chưa nên tag V1 Stable


Plan 1 trên main
Plan 1 về mặt code đã được triển khai đúng phần lớn. ApplicationService giờ có canonical registry cho ms_teams, ms_outlook, coding_agent, git, jira, shortcut; nó aggregate nhiều checkpoint, runtime status thắng checkpoint cũ, disabled thắng legacy checkpoint và không còn bỏ mất source khi chỉ một số source đã sync.
6 behavioral test A–F cũng đã được thêm.
Tuy nhiên CI của commit b8dfc9b đang đỏ. Không chỉ một platform: cả 9 matrix jobs đều fail ở bước Unit Tests. Ở job Ubuntu/Python 3.11 mà mình đọc log chi tiết, lỗi là:
test_a_mixed_state

Expected:
ms_outlook = NEVER_SYNCED

Actual:
ms_outlook = UNCONFIGURED

Nguyên nhân là test không set Playwright session healthy. Trên CI sạch không có Microsoft session, nên production logic hợp lý khi coi Outlook là UNCONFIGURED; test lại kỳ vọng NEVER_SYNCED.
Vì vậy đây nhiều khả năng là test nondeterminism / test setup bug, không phải lỗi nghiêm trọng của Source Health. Cách sửa hợp lý là test A phải explicit:
playwright_status="healthy"


nếu nó muốn kiểm tra:
authenticated + no checkpoint
→ NEVER_SYNCED

Nhưng bất kể nguyên nhân, release branch có CI đỏ thì chưa nên freeze Stable.
Quan trọng hơn: Plan 2 đã tìm ra P0 mà main chưa có
Mình so sánh snapshot hai branch bằng file SHA, vì Qang2909 là orphan history và GitHub không thể compare theo ancestor.
Hai snapshot gần như giống hệt nhau. Chỉ có 6 file khác, trong đó chỉ 3 file runtime khác:
packages/database/.../task_repo.py
scripts/ptb_cli.py
services/processing/.../llm_extractor.py

Ba file này chính là các hotfix phát sinh trong quá trình E2E.
1. P0 — Neo4j async transaction bug
Đây là blocker lớn nhất.
main hiện vẫn có:
async with session.begin_transaction() as tx:    ...


Trong Plan 2, commit b83bcc9 sửa thành:
tx = await session.begin_transaction()try:    ...    await tx.commit()except Exception:    await tx.rollback()    raise


Đây không phải refactor cosmetic. Đây là lỗi chỉ lộ khi chạy Neo4j async driver thật.
Ngoài ra E2E còn phải tách Cypher link:
Evidence → RawEvent

thành query riêng để tương thích runtime thật.
Điều đó cho thấy trước hotfix:
RawEvent
→ Processing
→ upsert_task_atomic()
→ real Neo4j
→ lỗi

có thể xảy ra.
Hotfix này chưa có trên main.
Do đó chỉ riêng điểm này đã đủ để kết luận:
main hiện chưa Ready to use.

2. LLM provider compatibility bug
Commit 282adf9 trên Qang2909 sửa extractor sau khi chạy với model OpenAI-compatible thật.
main gửi:
"response_format": {"type": "json_object"}


nhưng provider/model dùng trong E2E không hỗ trợ hoàn toàn field đó.
Plan 2 đã phải bỏ field này và thêm parser xử lý:
raw JSON
Markdown ```json
<think>...</think>
text before/after JSON

Ví dụ:
_parse_json_from_llm_response(...)


Điều này đặc biệt liên quan vì kiến trúc bạn đã chọn là:
cho phép dùng model thông qua API chuẩn OpenAI-compatible.

Nếu chỉ support OpenAI chính chủ thì issue này nhẹ hơn. Nhưng với product contract hiện tại, hotfix của Plan 2 là hợp lý và nên port về main.
Mình xếp nó P1, hoặc P0 nếu môi trường model chính thức của bạn chính là endpoint Qwen/OpenAI-compatible đã dùng khi E2E.
3. Windows Docker doctor bug
Plan 2 cũng sửa:
subprocess.run([docker_bin, "info"], ...)


thành behavior phù hợp Windows.
Đây không ảnh hưởng core pipeline trên Linux/macOS, nhưng dự án đã freeze requirement:
Cross-platform

nên nó là một release fix hợp lệ.
Mình xếp P1/P2 usability, không phải P0.
Plan 2 có thực sự làm việc hữu ích không?
Có.
Commit history của Qang2909 rất đáng giá:
285215a
PTB E2E verification: init commit with task extraction test signal

282adf9
fix processing pipeline / LLM extractor

d3f05f0
task: em sẽ deploy hotfix...
→ test signal thực để Git watcher/extractor bắt

b83bcc9
fix real Neo4j transaction + Windows Docker

323506d
add E2E verification report

9954873
update E2E report

Đây đúng pattern của một E2E bring-up thật:
start system
→ hit provider incompatibility
→ fix
→ inject real Git signal
→ hit Neo4j runtime bug
→ fix
→ complete report

Nó có giá trị hơn nhiều so với fixture E2E trước đây.
Nhưng Qang2909 chưa phải release branch tốt
Có ba lý do.
Thứ nhất: branch là orphan history
GitHub báo:
No common ancestor between main and Qang2909

Commit đầu của Qang2909 không có parent.
Vì vậy không nên:
merge Qang2909 → main

theo cách thông thường.
May mắn là snapshot gần như giống main: ngoài report/test artifacts chỉ có ba runtime files khác. Vì vậy integration khá dễ.
Thứ hai: Qang2909 không có CI run
GitHub Actions:
main       → có runs
Qang2909   → 0 workflow runs

Workflow hiện chỉ trigger branch main/master.
Do đó các hotfix Plan 2 chưa được chạy lại qua 9 OS/Python matrix.
Thứ ba: automated external integration vẫn còn quá nông
tests/test_external_integration.py trên branch chỉ có hai test:
Neo4j Bolt reachable
OpenWebUI HTTP reachable

Nó chưa tự động test:
real Neo4j transaction
RawEvent → Task
Graphiti sync
LLM real call
Teams real interception
Outlook real interception
OpenWebUI registration
restart/resume
dedup

Các bước đó có thể đã được manual E2E và ghi trong PDF, nhưng không có machine-readable logs/results tương ứng trong repo.
Mình xác nhận PDF report tồn tại và đã được cập nhật, nhưng GitHub connector không thể decode binary PDF trực tiếp và browser PDF fetch trong phiên này không render được, nên mình không lấy nội dung chưa đọc được của PDF làm bằng chứng để tuyên bố PASS.
Một điều tích cực: Qang branch đã chứa Plan 1
Mặc dù lịch sử Git độc lập, snapshot hiện tại cho thấy:
services/application/.../service.py
services/application/tests/test_health_truthful.py

trên Qang2909 có cùng SHA với main.
Nghĩa là Plan 2 đã được chạy trên code có Plan 1, không phải trên một V1.3 cũ.
Đây là tín hiệu tốt.
Vậy dự án hiện đạt mức nào?
Mình sẽ cập nhật đánh giá:
Hạng mục	Mức
Architecture	98%
Feature implementation	97%
Unit/contract behavior	96%
Real infrastructure discovery	95%
Real Neo4j compatibility	đã tìm và sửa trên Qang, chưa có trên main
Real LLM compatibility	đã tìm và sửa trên Qang, chưa có trên main
Source Health	~95%, CI test cần deterministic fix
Continuous E2E confidence	cao hơn trước đáng kể
Release branch integration	~80%
Ready-to-use trên main	Chưa
Project overall	~92–94% tới Stable


Cần làm gì để thật sự Ready to use?
Không cần thêm Wave lớn hay feature nào nữa. Chỉ cần một final stabilization merge:
1. Port hotfix task_repo.py từ b83bcc9 về main. Đây là bắt buộc.
2. Port phần Windows ptb_cli.py từ b83bcc9.
3. Port LLM robust JSON patch từ 282adf9 về main. Không nhất thiết mang theo Embedding model.md.
4. Sửa test_a_mixed_state để deterministic với Playwright state.
5. Chạy non-Docker suite và để GitHub Actions 9/9 green.
6. Tạo một branch mới từ main sau các fix, rồi chạy lại critical E2E trên chính SHA đó: init, real Neo4j task write, LLM extraction, Git signal, Graph sync, REST/MCP, OpenWebUI, restart/checkpoint/dedup.
7. Khi PASS, merge về main và tag V1 Stable.
Điểm quan trọng là không merge nguyên Qang2909 vì nó không cùng Git history. Hãy coi nó là một “E2E laboratory branch” và chỉ port các hotfix đã chứng minh cần thiết.
Sau khi ba runtime hotfix được đưa về main, CI xanh và critical E2E được chạy lại trên chính release SHA, mình sẽ coi dự án Ready to use / V1 Stable. Hiện tại nó đã rất gần, nhưng main vẫn thiếu một P0 Neo4j hotfix, nên câu trả lời ở thời điểm này vẫn là chưa.