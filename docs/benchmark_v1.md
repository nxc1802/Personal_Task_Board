# Plan — Personal Task Board Benchmark v1

**Mục tiêu\:** Xây dựng bộ benchmark đánh giá toàn diện Personal Task Board\, kế thừa **Small Benchmark SB\-v0\.1 \(12 events \+ 4 probes\)**\, sau đó mở rộng thành bộ dữ liệu có ground truth để đánh giá chất lượng thực tế\.

Plan bám theo `main` hiện tại\, commit [`2625a7dc`](https://github.com/nxc1802/Personal_Task_Board/commit/2625a7dc17492970ab3243c82f48c7f24be90f82)\.

PHẠM VI BENCHMARK V1

# 12

Case khởi đầu

# 4

Failure\/recovery probes

# 180

Sample mục tiêu v1

---

Không xây thêm feature production chỉ để phục vụ benchmark\. Benchmark phải kiểm tra và phát hiện lỗi của các module hiện có\.

## Wave 1 — Chuẩn hóa dataset và ground truth

**Đầu ra\:** Một bộ dữ liệu có thể replay và mở rộng mà không phải thay đổi cấu trúc\.

| Công việc | Yêu cầu |
|--------|--------|
| Dataset | Chuẩn hóa 12 RawEvents của SB\-v0\.1 |
| Ground truth | Task\/no\-task\, owner\, requester\, deadline\, status |
| Correlation | Expected cluster\, merge\/no\-merge\, anchor |
| Evidence | Source event ID\, snippet\, timestamp |
| Controls | Commitment\, Decision\, Lesson |
| Versioning | Dataset version\, hash\, source\, timestamp |
| Human review | Xác nhận nhãn CLEAR\/AMBIGUOUS |

Mọi sample phải có **input\, expected output và tiêu chí verify**\. Dữ liệu thật cần được ẩn thông tin nhạy cảm trước khi đưa vào benchmark có thể chia sẻ\.

**Gate\:** 12\/12 sample có ground truth hợp lệ\; không có nhãn mâu thuẫn\.

## Wave 2 — Xây Benchmark Runner và Evaluator

**Đầu ra\:** Một framework chạy benchmark tự động\.

| Thành phần | Nhiệm vụ |
|--------|--------|
| Dataset Loader | Đọc manifest và RawEvents |
| Environment Manager | Tạo môi trường benchmark tách biệt |
| Replay Runner | Chạy từng event theo đúng thứ tự |
| State Snapshot | Chụp trạng thái sau mỗi event |
| Deterministic Comparator | So sánh ID\, status\, deadline\, cluster |
| Semantic Comparator | So sánh title và summary khi cần |
| Graph Validator | Kiểm tra nodes\, edges\, provenance |
| Report Generator | Xuất PASS\/FAIL và chi tiết lỗi |

Runner phải có hai chế độ\: **clean run** từ database sạch và **replay run** để kiểm tra idempotency\.

Không sử dụng LLM\-as\-a\-judge làm ground truth cho các quyết định quan trọng\.

**Gate\:** Chạy được toàn bộ SB\-v0\.1 và xác định chính xác case\, module\, field gây lỗi\.

## Wave 3 — Evaluation đầy đủ từng module

Đây là phần chính của Benchmark v1\. Mỗi module cần có input\, expected behavior và cách verify độc lập\.

| Module | Nội dung kiểm tra |
|--------|--------|
| **L1 — Acquisition** | Teams\/Outlook qua Playwright\; Jira\, Shortcut\, Git\, coding\-agent watchers |
| Checkpoint | Initial\/incremental sync\, restart\, resume\, tenant isolation |
| RawEvent | Persistence\, deduplication\, timestamp\, payload integrity |
| **L2 — Parser** | HTML\, quote\/reply\, nội dung thực tế của người gửi |
| Heuristic Filter | Positive\, negative và false\-positive |
| LLM Extractor | Task detection\, confidence\, deadline\, evidence grounding |
| Attribution &amp; Identity | Owner\/requester\, exact identity\, fuzzy\-name isolation |
| Correlation &amp; Merge | Anchor merge\, semantic merge\, conflict\, split\, merge audit |
| Processing Worker | Retry\, failure\, processing attempts\, no silent fallback |
| **L3 — Neo4j** | Constraints\, task\/evidence\/commitment\, relationships\, provenance |
| Graphiti | Episode sync\, semantic retrieval\, temporal data\, retry |
| **L4 — Intelligence** | Priority\, manual override\, status inference\, status audit |
| Today\/Waiting\/Forgotten | Ranking\, filtering\, overdue\/stale detection |
| **L5 — REST** | Tất cả 14 business endpoints và 6 ingestion/portal endpoints (tổng cộng 20 endpoints), query và mutation |
| MCP | Cả 10 read\-only tools và consistency với REST |
| OpenWebUI | Các board views\, review\, task actions\, error handling |
| **Runtime** | Supervisor\, source health\, restart\, dependency failure |

**Gate\:** Mỗi module đều có ít nhất một positive case\, một negative\/failure case khi phù hợp và một oracle có thể xác minh kết quả\.

Đối với các coding\-agent watchers\, kiểm tra fixture cho từng loại được hỗ trợ\; chỉ đánh dấu live E2E PASS đối với agent thực sự được cài đặt và chạy\.

## Wave 4 — Real E2E và failure testing

Sử dụng dịch vụ thật\: Neo4j\, LLM\, embedding model\, Graphiti\, Playwright\, REST\, MCP và OpenWebUI\.

| Scenario | Expected |
|--------|--------|
| Source → Today Board | Task và Evidence xuất hiện đúng |
| Duplicate event | Không tạo duplicate |
| Restart | Checkpoint resume chính xác |
| LLM timeout\/500 | Retry\, ghi lỗi\, không fake success |
| Neo4j unavailable | Báo NOT\_READY\, không fallback RAM |
| Graphiti unavailable | Domain data còn nguyên\, sync retry |
| Session expired | Báo AUTH\_REQUIRED |
| Manual MARK\_DONE | Persist authoritative status và audit |
| OpenWebUI backend lỗi | Hiển thị lỗi\, không báo success giả |

**Gate\:** Toàn bộ 12 case và 4 probes của SB\-v0\.1 đạt yêu cầu trên môi trường phù hợp\. Scenario chưa có dịch vụ hoặc credentials thật phải được ghi `PENDING`\, không tính là PASS\.

## Wave 5 — Mở rộng thành Full Benchmark v1

Chỉ bắt đầu khi Small Benchmark chạy ổn định\.

**Mục tiêu ban đầu\: khoảng 180 RawEvents có nhãn**\, gồm sáu nguồn dữ liệu chính và những nhóm case khó như noise\, ambiguous request\, cross\-source duplicate\, near\-duplicate\, deadline\, identity và completion signal\.

Dữ liệu phải kết hợp synthetic có kiểm soát với dữ liệu thực đã được xác nhận\. Không chỉ lấy những task PTB tạo thành công\; phải lấy mẫu cả event bị ignore để đo missed tasks\.

Các metric cần xuất theo từng source và từng loại case\:

| Nhóm | Metric |
|--------|--------|
| Extraction | Precision\, Recall\, False Positive\, False Negative |
| Attribution | Owner\, requester\, deadline accuracy |
| Correlation | False merge\, missed merge\, duplicate rate |
| Evidence | Grounding\, provenance\, timestamp accuracy |
| Intelligence | Status accuracy\, priority agreement |
| Graphiti | Sync success\, retrieval accuracy |
| Reliability | Data loss\, retry recovery\, idempotency |
| UX | Review burden\, action success\, consistency |

Với 180 mẫu\, các tỷ lệ chủ yếu phục vụ **thiết lập baseline và phát hiện regression**\; chưa nên xem chúng là bằng chứng thống kê đầy đủ về độ chính xác trên mọi dữ liệu thực tế\.

**Gate\:** Dataset được human\-verify\, evaluator xuất được đầy đủ metric và danh sách error cases có thể tái hiện\.

---

## Cấu trúc bàn giao

::chatgpt-content-reference{index="0"}



## Điều kiện hoàn thành Benchmark v1

**Acceptance checklist** \/

**Thứ tự quan trọng nhất\:** Wave 1 → Wave 2 → Wave 3 và 4 trên small benchmark → Wave 5\. Không mở rộng lên 180 samples khi 12 case đầu chưa được verify thành công\; nếu không\, ta chỉ đang nhân số lượng lỗi thay vì cải thiện khả năng đánh giá\.