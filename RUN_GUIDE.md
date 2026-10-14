# 📖 HƯỚNG DẪN CHẠY & VẬN HÀNH HỆ THỐNG PERSONAL TASK BOARD (PTB)

Tài liệu này hướng dẫn chi tiết cách khởi động, vận hành hàng ngày, quét dữ liệu và sử dụng các tính năng của hệ thống **Personal Task Board**.

---

## 📑 Mục Lục
1. [Lưu Ý Quan Trọng Trước Khi Khởi Động (Docker Desktop)](#1-lưu-ý-quan-trọng-trước-khi-khởi-động-docker-desktop)
2. [Khởi Động Hệ Thống](#2-khởi-động-hệ-thống)
3. [Các Địa Chỉ Truy Cập & Giao Diện Hoạt Động](#3-các-địa-chỉ-truy-cập--giao-diện-hoạt-động)
4. [Kích Hoạt & Cơ Chế Quét Dữ Liệu Tự Động (Ingestion)](#4-kích-hoạt--cơ-chế-quét-dữ-liệu-tự-động-ingestion)
5. [Hướng Dẫn Sử Dụng Các Tính Năng Chính](#5-hướng-dẫn-sử-dụng-các-tính-năng-chính)
6. [Theo Dõi Trạng Thái, Xem Logs & Xử Lý Sự Cố](#6-theo-dõi-trạng-thái-xem-logs--xử-lý-sự-cố)
7. [Tắt & Khởi Động Lại Hệ Thống](#7-tắt--khởi-động-lại-hệ-thống)

---

## 1. Lưu Ý Quan Trọng Trước Khi Khởi Động (Docker Desktop)

> ⚠️ **BẮT BUỘC TRƯỚC KHI CHẠY BẤT KỲ LỆNH DOCKER NÀO TRÊN WINDOWS / MACOS:**
> 
> 1. Mở ứng dụng **Docker Desktop** (từ Start Menu hoặc icon Desktop).
> 2. Quan sát góc dưới bên trái của cửa sổ Docker Desktop (hoặc icon cá voi trên thanh Taskbar): **Đợi chuyển sang màu xanh lá cây (`Docker Engine: Running`)**.
> 3. *Nếu chưa mở Docker Desktop hoặc Engine chưa khởi động xong, bạn sẽ gặp lỗi sau:*
>    ```text
>    failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine; 
>    check if the path is correct and if the daemon is running: open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified.
>    ```
>    👉 **Cách xử lý khi gặp lỗi trên**: Mở ứng dụng Docker Desktop, đợi 15 - 30 giây cho cá voi chuyển màu xanh rồi gõ lại lệnh.

---

## 2. Khởi Động Hệ Thống

Sau khi đã bật Docker Desktop (Engine Running), bạn chọn 1 trong 2 cách sau:

### Cách 1: Khởi chạy bằng Docker Compose (Khuyên Dùng — 1 Lệnh Duy Nhất)
Mở terminal PowerShell/Bash tại thư mục dự án:
```bash
docker compose up -d --build
```

> 💡 **Tại sao luôn cần cờ `--build`?** Tham số `--build` đảm bảo Docker đóng gói toàn bộ mã nguồn mới nhất và cài đặt đầy đủ các gói workspace vào container `ptb-app`. Nếu chạy thiếu `--build`, Docker có thể không phát hiện được container mới hoặc dùng cache cũ khiến dịch vụ cổng 8000 không khởi động.

> ⏱️ **Lưu ý về thời gian khởi động**: Sau khi gõ lệnh, hệ thống cần khoảng **20 – 40 giây** để container Neo4j khởi động, tự động nạp constraint/indexes và kiểm tra healthcheck. Trong 20 giây đầu nếu mở ngay `localhost:8000/portal` có thể thấy thông báo `ERR_CONNECTION_REFUSED`. Bạn hãy đợi khoảng 30 giây (hoặc kiểm tra `docker ps` thấy `(healthy)`) rồi nhấn F5 tải lại trang.

**Hệ thống sẽ tự động thực hiện ngầm:**
- Khởi động cơ sở dữ liệu đồ thị **Neo4j 5.26** (Cổng `7687` & `7474`).
- Tự động nạp toàn bộ Schema Indexes & Constraints (`ptb init`).
- Khởi động **PTB Process Supervisor** (REST API cổng `8000`, FastMCP cổng `8001`, Ingestion Workers).
- Khởi động giao diện **OpenWebUI** (Cổng `3000`).

---

### Cách 2: Khởi chạy Cục Bộ (Python Bare-Metal)
Dành cho khi bạn muốn trực tiếp debug source code:
```bash
# 1. Chỉ khởi động riêng Neo4j container
docker compose up -d neo4j

# 2. Khởi chạy Process Supervisor bằng Python UV
uv run ptb run
```
Màn hình sẽ hiển thị trạng thái sẵn sàng:
```text
======================================================================
  READY: All PTB components operational!
  REST API                 : http://127.0.0.1:8000
  FastMCP Server           : http://127.0.0.1:8001/sse
  ProcessingWorker Loop    : ACTIVE
  Coding Agent Watchers    : ACTIVE
======================================================================
```

---

## 3. Các Địa Chỉ Truy Cập & Giao Diện Hoạt Động

| Dịch Vụ / Giao Diện | Địa Chỉ Truy Cập | Mục Đích |
|---|---|---|
| 🌐 **OpenWebUI (Task Board & Chat AI)** | [http://localhost:3000](http://localhost:3000) | Giao diện kéo thả Kanban task, chat với AI hỏi đáp công việc. |
| 📊 **PTB Dev Control Portal** | [http://localhost:8000/portal](http://localhost:8000/portal) | Bảng điều khiển kỹ thuật: xem tasks thô, trigger đồng bộ, kiểm tra trạng thái worker. |
| 🔌 **API Health Check** | [http://localhost:8000/health](http://localhost:8000/health) | Kiểm tra nhanh tình trạng sống của backend. |
| 🤖 **FastMCP SSE Endpoint** | `http://localhost:8001/sse` | URL gắn vào Cursor IDE / Antigravity IDE để AI Coding Agent tương tác với Task Board. |
| 🗄️ **Neo4j Graph Browser** | [http://localhost:7474](http://localhost:7474) | Tra cứu trực tiếp đồ thị tri thức (Username: `neo4j`, Password: đã đặt trong `.env`). |

---

## 4. Kích Hoạt & Cơ Chế Quét Dữ Liệu Tự Động (Ingestion)

Hệ thống hoạt động với triết lý **Zero Manual Input** (không cần nhập liệu bằng tay):

### 4.1 Coding Agents (Cursor, Claude Code, Antigravity)
- **Tự động 100%**: Khi bạn code hoặc giao tiếp với AI trong Cursor, Claude Code CLI, Antigravity, Supervisor sẽ tự động quét log session định kỳ mỗi 10 giây và trích xuất các task/quyết định vào Neo4j.

### 4.2 Microsoft Teams & Outlook Web
- Để hệ thống lắng nghe tin nhắn giao việc từ Teams/Outlook:
  ```bash
  # Nếu chạy Bare-metal:
  uv run ptb login microsoft
  # Nếu chạy Docker:
  docker compose exec ptb-app ptb login microsoft
  ```
- Đăng nhập tài khoản Microsoft 365 trên trình duyệt hiện ra. Phiên đăng nhập được lưu vào `data/playwright/storage_state.json`. Lần sau hệ thống tự động chạy ngầm headless.

### 4.3 Local Git Repositories
- Mở file [config/sources.yaml](./config/sources.yaml), khai báo đường dẫn repository bạn đang làm việc:
  ```yaml
  git:
    enabled: true
    repositories:
      - path: "D:/Working/MyProject"
        branch: "main"
  ```
- Hệ thống sẽ tự động quét commit message để đồng bộ tiến độ vào task.

### 4.4 Kích hoạt quét ngay lập tức (On-demand)
Không cần đợi chu kỳ lặp:
```bash
uv run ptb ingest
# Hoặc qua docker:
docker compose exec ptb-app ptb ingest
```

---

## 5. Hướng Dẫn Sử Dụng Các Tính Năng Chính

### 5.1 Quản Lý Task Trên Giao Diện OpenWebUI
1. Truy cập [http://localhost:3000](http://localhost:3000).
2. Chuyển sang Tab **PTB Task Board**:
   - Các task đang làm (`In Progress`) sẽ tự động được xếp lên đầu.
   - Hỗ trợ đầy đủ thao tác: **Thêm mới**, **Sửa tiêu đề/nội dung**, **Đổi độ ưu tiên**, **Kéo thả chuyển trạng thái**, và **Xóa task**.

### 5.2 Trò Chuyện Với AI Trợ Lý Về Công Việc
Tại ô chat OpenWebUI, bạn có thể hỏi tự nhiên:
- *"Hôm nay tôi cần ưu tiên làm những việc gì?"*
- *"Có cam kết nào sắp đến hạn chót (deadline) không?"*
- *"Tôi đang bị nghẽn (blocked) ở những việc nào?"*
- *"Tổng hợp các quyết định kiến trúc kỹ thuật gần đây."*

### 5.3 Tích Hợp Với AI Coding Agent (Cursor / Claude Desktop / Antigravity)
Khi kết nối qua endpoint `http://localhost:8001/sse`:
- Agent có thể tự động đọc task hiện tại để bắt đầu code mà không cần bạn copy-paste yêu cầu.
- Sau khi hoàn thành giải pháp, Agent tự động gọi tool `record_decision` hoặc `record_lesson_learned` để lưu tri thức vào hệ thống.

---

## 6. Theo Dõi Trạng Thái, Xem Logs & Xử Lý Sự Cố

### 6.1 Xem Logs Hoạt Động (Docker)
```bash
# Xem log toàn bộ hệ thống
docker compose logs -f

# Chỉ xem log của container PTB App
docker compose logs -f ptb-app
```

### 6.2 Chẩn Đoán Toàn Diện Hệ Thống (PTB Doctor)
```bash
# Kiểm tra nhanh:
docker compose exec ptb-app ptb doctor

# Kiểm tra chuyên sâu (gồm cả kết nối LLM API thực tế):
docker compose exec ptb-app ptb doctor --deep
```

### 6.3 Bảng Xử Lý Sự Cố Nhanh

| Mã Lỗi / Hiện Tượng | Nguyên Nhân | Cách Xử Lý |
|---|---|---|
| **`failed to connect to docker API... pipe/dockerDesktopLinuxEngine`** | Chưa bật Docker Desktop hoặc Engine chưa sẵn sàng. | Mở Docker Desktop từ Start Menu, đợi icon cá voi chuyển màu xanh lá rồi chạy lại lệnh. |
| **`localhost đã từ chối kết nối (ERR_CONNECTION_REFUSED)` khi vào port 8000** | 1. Mở web quá sớm khi container `ptb_app` đang khởi động.<br>2. Chạy lệnh thiếu cờ `--build` khiến container `ptb_app` chưa được build. | 1. Đợi khoảng 20-30 giây rồi nhấn F5 tải lại trang.<br>2. Chạy lại lệnh: `docker compose up -d --build`<br>3. Xem log kiểm tra: `docker compose logs -f ptb-app` khi nào in banner `READY` là hoàn tất. |
| **`PTB_STORAGE_001`** | Mất kết nối Neo4j hoặc sai mật khẩu. | Kiểm tra `docker compose ps neo4j` và kiểm tra `NEO4J_PASSWORD` trong file `.env`. |
| **`PTB_LLM_001`** | Chưa cấu hình LLM API Key. | Điền `LLM_API_KEY` của Gemini hoặc OpenAI vào file `.env`. |
| **Cổng `8000`, `8001`, `3000` bị báo `already in use`** | Có phần mềm khác đang chiếm cổng. | Đổi `APP_PORT` hoặc `MCP_PORT` trong file `.env`. |

---

## 7. Tắt & Khởi Động Lại Hệ Thống

| Mục Đích | Lệnh Thực Hiện |
|---|---|
| **Tạm dừng hệ thống** | `docker compose stop` |
| **Bật lại sau khi tạm dừng** | `docker compose start` |
| **Tắt hoàn toàn và dọn containers** | `docker compose down` |
| **Cập nhật mã nguồn mới từ Git & Rebuild** | `git pull origin main`<br>`docker compose up -d --build` |
