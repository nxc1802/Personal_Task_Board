## ✅ Checklist lấy `.env`

### 1. Supabase

Vào **Supabase Dashboard → chọn Project**

* [ ] **`SUPABASE_URL`**

  * `Project Settings → API`
  * Copy **Project URL**
  * Dạng: `https://xxxxx.supabase.co`

* [ ] **`SUPABASE_ANON_KEY`**

  * `Project Settings → API Keys`
  * Copy **anon / anon public key**

* [ ] **`SUPABASE_SERVICE_ROLE_KEY`**

  * `Project Settings → API Keys`
  * Copy **service_role key**
  * ⚠️ Chỉ dùng backend, không đưa lên frontend/GitHub.

* [ ] **`DATABASE_URL`**

  * Nhấn **Connect** trong project
  * Chọn **Database / Connection string**
  * Copy PostgreSQL connection string
  * Dạng:

    ```env
    postgresql://postgres:PASSWORD@db.PROJECT-REF.supabase.co:5432/postgres
    ```
  * Nếu quên DB password → **Project Settings → Database → reset password**

---

### 2. Neo4j AuraDB

Vào **Neo4j Aura Console → chọn Instance**

* [ ] **`NEO4J_URI`**

  * Lấy từ **Connection details**
  * Dạng:

    ```env
    neo4j+s://INSTANCE-ID.databases.neo4j.io
    ```

* [ ] **`NEO4J_USERNAME`**

  * Thường là:

    ```env
    neo4j
    ```

* [ ] **`NEO4J_PASSWORD`**

  * Lấy password lúc tạo AuraDB instance / credentials file
  * Nếu mất → xử lý/reset credential trong Aura Console

---

### 3. `.env` hoàn chỉnh

```env
# Supabase
DATABASE_URL=postgresql://postgres:PASSWORD@db.PROJECT-REF.supabase.co:5432/postgres
SUPABASE_URL=https://PROJECT-REF.supabase.co
SUPABASE_ANON_KEY=eyJ...
SUPABASE_SERVICE_ROLE_KEY=eyJ...

# Neo4j AuraDB
NEO4J_URI=neo4j+s://INSTANCE-ID.databases.neo4j.io
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=...
```

### 🔐 Trước khi chạy

* [ ] `.env` nằm trong `.gitignore`
* [ ] Không commit `.env`
* [ ] Không gửi `SERVICE_ROLE_KEY`, DB password, Neo4j password lên GitHub
* [ ] Frontend **không được** chứa `SERVICE_ROLE_KEY` hoặc `NEO4J_PASSWORD`

**Tổng cộng cần lấy: 7 biến** → **4 Supabase + 3 Neo4j**.

---

## 🚀 Các Câu Lệnh Thực Thi Setup

### Bước 1: Cài đặt Dependencies (nếu chưa chạy)

Khởi tạo môi trường ảo và cài đặt các package trong Monorepo:

```bash
# 1. Tạo virtual environment
uv venv

# 2. Cài đặt các package nội bộ và công cụ test
uv pip install -e packages/contracts -e packages/database pytest pytest-asyncio
```

---

### Bước 2: Chạy Thiết Lập Tự Động Toàn Bộ (One-Click Setup - Khuyến Nghị)

Sau khi đã điền đầy đủ 7 biến môi trường vào file `.env`, chạy lệnh duy nhất này để tự động nạp toàn bộ schema lên Supabase và Neo4j AuraDB:

```bash
uv run python -m ptb_database.setup_all
```

*Lệnh trên sẽ tự động:*
1. Kết nối tới **Supabase PostgreSQL** qua `DATABASE_URL`.
2. Tạo 15 bảng, triggers cập nhật `updated_at`, trigger ghi lịch sử status và RLS policies (`consolidated_schema.sql`).
3. Nạp dữ liệu seed ban đầu: workspace, user hiện tại (`Cuong`), teammate (`Huy`, `Mai`), và ánh xạ tài khoản (`001_dev_seed.sql`).
4. Kết nối tới **Neo4j AuraDB** qua `NEO4J_URI` (`neo4j+s://`).
5. Áp dụng 11 Uniqueness Constraints và 4 Indexes cho Fixed Ontology (`001_constraints.cypher`).
6. Kiểm tra và in danh sách bảng Supabase + constraints Neo4j đã hoạt động.

---

### Bước 3: Chạy Thiết Lập Riêng Lẻ (Tùy Chọn)

Nếu bạn muốn chạy từng dịch vụ riêng biệt:

#### 3.1 Chỉ thiết lập Neo4j AuraDB qua script:
```bash
uv run python -m ptb_database.init_neo4j
```

#### 3.2 Chạy thủ công trên giao diện Web (nếu không dùng script):
* **Supabase**:
  * Mở **Supabase Dashboard → SQL Editor**.
  * Chạy file: `packages/database/supabase/consolidated_schema.sql` (tạo 15 bảng).
  * Chạy tiếp file: `packages/database/supabase/seeds/001_dev_seed.sql` (nạp seed data).
* **Neo4j AuraDB**:
  * Mở **Neo4j Aura Console → Open Query**.
  * Chạy file: `packages/database/neo4j/constraints/001_constraints.cypher` (tạo 11 constraints & 4 indexes).

---

### Bước 4: Kiểm Tra & Xác Minh (Test Verification)

Chạy bộ unit test để xác minh 100% contracts và validator đều hoạt động chuẩn:

```bash
uv run pytest
```

*Kết quả kỳ vọng:* **22/22 tests passed** (Contracts tests, Tier 2 Ontology Validator, Outbox Worker, Neo4j Client).

