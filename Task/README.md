# Danh Sách & Tiến Độ Các Tác Vụ Dự Án Personal Task Board (T1 - T9, Review 1, Review 2)

Thư mục này lưu trữ đặc tả yêu cầu chi tiết của toàn bộ các tác vụ cốt lõi, tối ưu kiến trúc và rà soát nghiệm thu trong hệ thống Personal Task Board (PTB), từ hoàn thiện nghiệp vụ lõi đến chuẩn bị sẵn sàng triển khai chính thức (Ready-to-Deploy, Release v1.3).

> [!NOTE]  
> Chi tiết báo cáo chuyên sâu và bằng chứng kiểm thử tự động của từng task được tổng hợp tại: [`REPORT_ALL_TASKS.md`](./REPORT_ALL_TASKS.md).

---

## 1. Tổng Quan Các Tác Vụ & Trạng Thái Nghiệm Thu

| Mã Tác Vụ | File Đặc Tả | Mục Tiêu Yêu Cầu | Trạng Thái & Hiện Thực Trong Codebase | Kiểm Thử Tự Động |
|---|---|---|---|---|
| **T1** | [`T1.txt`](./T1.txt) | Ưu tiên Tasks đang "In progress" ở trên cùng, các Task "To do" hoặc "Pending" còn lại sắp xếp theo độ ưu tiên | **HOÀN THÀNH**: Sắp xếp chuẩn tắc 4 tầng tại `task_repo.py`, `planner.py`, `ptb_board.html`. | `tests/test_tasks_t1_t2_t3.py` (58/58 PASS) |
| **T2** | [`T2.txt`](./T2.txt) | Tích hợp OpenWebUI để sử dụng được PTB Agent BE | **HOÀN THÀNH**: Triển khai tại `integrations/openwebui/tools/ptb_tools.py` và `installer.py`. | `integrations/openwebui/tests/test_ui_contracts.py` (7/7 PASS) |
| **T3** | [`T3.txt`](./T3.txt) | Tích hợp vào Workspace của WebOpenUI (cổng 3000): Models, Tools, Prompts và Kanban Component CRUD | **HOÀN THÀNH**: Workspace Model `personal-task-board`, Tools function calling 2 chiều, Prompts `/board`, `/today`, Kanban Tab trực tiếp. | `integrations/openwebui/tests/test_integration_e2e.py` (17/17 PASS) |
| **T4** | [`T4.txt`](./T4.txt) | Mở rộng thu thập dữ liệu đa kênh bên cạnh MS Teams: Outlook, Jira, Shortcut, Confluence, GitHub, Slack | **HOÀN THÀNH**: 9 Ingestion Adapters tại `services/acquisition/` và vòng đời tương quan 4 kênh xuyên suốt. | `tests/test_task_t4_multichannel.py` (14/14 PASS) |
| **T5** | [`T5.txt`](./T5.txt) | Xuất Agent, BE thành chuẩn MCP (FastMCP) cho Coding Agent (Claude, Cursor, Antigravity) sử dụng | **HOÀN THÀNH**: 19 FastMCP tools tại `services/mcp/` (10 Read-Only cho Coding Agents, 9 Mutation có RBAC), hỗ trợ STDIO & SSE (:8001). | `tests/test_task_t5_mcp.py` (9/9 PASS) |
| **T6** | [`T6.txt`](./T6.txt) | Hoàn thiện Benchmark toàn diện, mục tiêu đạt 90%+ toàn bộ benchmark; truy vết nguyên nhân và tối ưu | **HOÀN THÀNH**: Bộ benchmark locked PTB-BENCH-500 (`benchmarks/dataset/ptb_500/`) đạt **500/500 PASS (100.0%)** trên cả 10 chiều scorecard. | `python -m benchmarks run --mode fixture` (500/500 PASS) |
| **T7** | [`T7.txt`](./T7.txt) | Tối ưu Workflow toàn bộ dự án, tăng tốc độ, loại bỏ code dư thừa, đưa dự án về trạng thái Ready-to-deploy | **HOÀN THÀNH**: Supervisor daemon, Doctor CLI, Docker non-root (`ptbuser`), cô lập cổng `127.0.0.1`, zero-mock production. | `tests/test_release_readiness.py` (14/14 PASS) |
| **T8** | [`T8.txt`](./T8.txt) | Khắc phục lỗi popup đăng nhập Microsoft Teams/Outlook, kiến trúc CDP và Host Auth Helper | **HOÀN THÀNH**: Dịch vụ `host_auth_helper.py` (:8999), launcher ẩn VBScript không cửa sổ cmd đen, volume mount session an toàn. | `tests/e2e/test_teams_live_system_e2e.py` (4/4 PASS, 1 skip) |
| **T9** | [`T9.txt`](./T9.txt) | Bảo mật dữ liệu local, quét dynamic coding agents/git, đồng bộ số đếm 8 Tabs tức thì & Dark mode | **HOÀN THÀNH**: Quét dynamic `~/.gemini`, `workspaceStorage`, `~/.codex`; modal Connect Platforms 4 cloud; badge số đếm 8 tabs load ngay. | `tests/test_portal_api.py` (11/11 PASS) |
| **Review 1** | [`Review 1.txt`](./Review%201.txt) | Tối ưu hóa chi phí AI, lớp Telemetry, Cache content-hash, Embedding de-dup, Structured bypass, Kev reranker | **HOÀN THÀNH**: Lớp Cost & Call Audit (`telemetry.py`), cache key SHA-256, structured extractor bypass LLM, Kev reranker & priority urgency. | `tests/test_review_1_optimizations.py` (11/11 PASS) |
| **Review 2** | [`Review 2.txt`](./Review%202.txt) | Tháo gỡ các rào cản release, siết bảo mật Host Auth Helper, bắt buộc Prod API key, chuyển NO-GO sang GO | **HOÀN THÀNH**: Host Auth Helper bảo mật localhost-only/CORS/405/Token, Prod API key bắt buộc, FMR/MMR 0.0%, chuyển từ NO-GO sang GO. | `tests/test_host_auth_helper_security.py` (6/6 PASS) |

---

## 2. Kiểm Thử Xác Minh Toàn Hệ Thống

Tất cả các tác vụ đều có bộ kiểm thử tự động tương ứng:
- `tests/test_tasks_t1_t2_t3.py`: Kiểm thử T1 (Sort & Priority), T2 (OpenWebUI Tools), T3 (OpenWebUI Board CRUD).
- `tests/test_task_t4_multichannel.py`: Kiểm thử T4 (Đa kênh Slack, GitHub, Confluence, Outlook, Jira, Git).
- `tests/test_task_t5_mcp.py`: Kiểm thử T5 (FastMCP 19 Tools).
- `tests/test_cli_supervisor.py`: Kiểm thử T7 (Process Supervisor, Doctor Deep Check, Health Check).
- `tests/test_host_auth_helper_security.py`: Kiểm thử T8 & Review 2 (Bảo mật Host Auth Helper).
- `tests/test_portal_api.py`: Kiểm thử T9 & Review 2 (Sync platforms, Connectors config, Tab counts).
- `tests/test_review_1_optimizations.py`: Kiểm thử Review 1 (Cost audit, Embedding dedup, Structured bypass, Kev reranker).
- `tests/test_anti_fallback_guard.py`: Kiểm thử bảo vệ nguyên tắc Anti-Fallback không chứa mock production.
- `tests/test_release_readiness.py`: Kiểm thử 14 Release Readiness Gates đóng băng chất lượng.

Chạy kiểm thử nhanh toàn bộ:
```bash
.venv\Scripts\python.exe -m pytest tests/test_anti_fallback_guard.py tests/test_host_auth_helper_security.py tests/test_review_1_optimizations.py tests/test_portal_api.py tests/test_release_readiness.py
```
