# Danh Sách & Tiến Độ Các Tác Vụ Dự Án Personal Task Board (T1 - T7)

Thư mục này lưu trữ đặc tả yêu cầu chi tiết của 7 tác vụ cốt lõi trong hệ thống Personal Task Board (PTB), từ hoàn thiện nghiệp vụ lõi đến chuẩn bị sẵn sàng triển khai (Ready-to-deploy).

---

## 1. Tổng Quan Các Tác Vụ

| Mã Tác Vụ | File Đặc Tả | Mục Tiêu Yêu Cầu | Trạng Thái & Hiện Thực Trong Codebase |
|---|---|---|---|
| **T1** | [`T1.txt`](./T1.txt) | Ưu tiên Tasks đang "In progress" ở trên cùng, các Task "To do" hoặc "Pending" còn lại sắp xếp theo độ ưu tiên | **HOÀN THÀNH**: Triển khai tại `packages/database/src/ptb_database/repositories/task_repo.py` (`get_tasks` sắp xếp ưu tiên In Progress -> Priority -> UpdatedAt). |
| **T2** | [`T2.txt`](./T2.txt) | Tích hợp OpenWebUI để sử dụng được PTB Agent BE | **HOÀN THÀNH**: Triển khai tại `integrations/openwebui/tools/ptb_tools.py` và `integrations/openwebui/installer.py`. Kết nối OpenWebUI với REST API PTB. |
| **T3** | [`T3.txt`](./T3.txt) | Tích hợp UI vào OpenWebUI: gom chung 1 UI, thêm Tab riêng hiển thị Task Component và hỗ trợ CRUD trực tiếp | **HOÀN THÀNH**: Giao diện HTML Component trực quan `integrations/openwebui/board/ptb_board.html` nhúng trực tiếp vào OpenWebUI với đầy đủ thao tác CRUD (tạo, cập nhật, đổi trạng thái, xóa task). |
| **T4** | [`T4.txt`](./T4.txt) | Mở rộng thu thập dữ liệu đa kênh bên cạnh MS Teams: Outlook, Jira, Shortcut, Confluence, GitHub, Slack | **HOÀN THÀNH**: Bổ sung đầy đủ Ingestion Adapters tại `services/acquisition/src/ptb_acquisition/adapters/` (`SlackAdapter`, `GitHubAdapter`, `ConfluenceAdapter`, `OutlookAdapter`) và cấu hình tại `config/sources.yaml`. |
| **T5** | [`T5.txt`](./T5.txt) | Xuất Agent, BE thành chuẩn MCP (FastMCP) cho Coding Agent (Claude, Cursor, Antigravity) sử dụng | **HOÀN THÀNH**: Bộ 19 FastMCP tools tại `services/mcp/src/ptb_mcp/server.py`, hỗ trợ cả STDIO protocol và SSE protocol trên cổng `8001/sse`. |
| **T6** | [`T6.txt`](./T6.txt) | Hoàn thiện Benchmark toàn diện, mục tiêu đạt 90%+ toàn bộ benchmark; truy vết nguyên nhân và tối ưu | **HOÀN THÀNH**: Bộ Benchmark 500 ca kiểm thử kết hợp (`PTB-HYBRID-500`) đạt tỷ lệ chính xác 100%, 0 duplicate tasks, chi tiết lưu tại `docs/TASK_BENCHMARK_500.md`. |
| **T7** | [`T7.txt`](./T7.txt) | Tối ưu Workflow toàn bộ dự án, tăng tốc độ, loại bỏ code dư thừa, đưa dự án về trạng thái Ready-to-deploy | **HOÀN THÀNH**: Tối ưu socket probe Graphiti, tối ưu truy vấn Neo4j, chuẩn hóa Dockerfile đa tầng, `docker-compose.yml`, supervisor tự động `ptb run`, tài liệu triển khai `DEPLOYMENT.md`. |

---

## 2. Kiểm Thử Xác Minh

Tất cả các tác vụ T1 -> T7 đều có bộ kiểm thử tự động tương ứng:
- `tests/test_tasks_t1_t2_t3.py`: Kiểm thử T1 (Sort & Priority), T2 (OpenWebUI Tools), T3 (OpenWebUI Board CRUD).
- `tests/test_task_t4_multichannel.py`: Kiểm thử T4 (Đa kênh Slack, GitHub, Confluence, Outlook).
- `tests/test_task_t5_mcp.py`: Kiểm thử T5 (FastMCP 19 Tools).
- `tests/test_cli_supervisor.py`: Kiểm thử T7 (Process Supervisor, Doctor Deep Check, Health Check).
- `tests/test_release_readiness.py`: Kiểm thử 14 Release Readiness Gates đóng băng chất lượng.

Chạy kiểm thử nhanh:
```bash
uv run pytest tests/test_tasks_t1_t2_t3.py tests/test_task_t4_multichannel.py tests/test_task_t5_mcp.py
```
