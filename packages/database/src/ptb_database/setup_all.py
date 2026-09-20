"""Automated One-Click Setup Script for Supabase and Neo4j AuraDB.

Tự động thực thi toàn bộ schema DDL, seed data lên Supabase và constraints lên Neo4j AuraDB:
    uv run python -m ptb_database.setup_all
"""

import asyncio
import os
import sys
from pathlib import Path
import asyncpg

from ptb_database.neo4j_client import Neo4jClient


def load_env_file():
    """Tự động nạp file .env từ workspace root nếu có."""
    env_file = Path(__file__).parent.parent.parent.parent / ".env"
    if not env_file.exists():
        env_file = Path.cwd() / ".env"
        
    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    val = val.strip().strip("\"'")
                    if key not in os.environ or not os.environ[key]:
                        os.environ[key] = val


async def setup_supabase():
    """Kết nối và nạp schema + seed lên Supabase PostgreSQL."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("  [!] Bỏ qua Supabase: Chưa cấu hình biến DATABASE_URL trong .env")
        return False

    print("\n[1/2] ĐANG THIẾT LẬP SUPABASE POSTGRESQL...")
    print(f"  Connecting to: {db_url.split('@')[-1] if '@' in db_url else '...'}")

    # Đọc consolidated schema và seed script
    base_dir = Path(__file__).parent.parent.parent / "supabase"
    schema_file = base_dir / "consolidated_schema.sql"
    seed_file = base_dir / "seeds" / "001_dev_seed.sql"

    if not schema_file.exists():
        raise FileNotFoundError(f"Không tìm thấy file: {schema_file}")

    schema_sql = schema_file.read_text(encoding="utf-8")
    seed_sql = seed_file.read_text(encoding="utf-8") if seed_file.exists() else ""

    conn = await asyncpg.connect(db_url)
    try:
        print("  ✓ Đã kết nối thành công tới Supabase PostgreSQL.")
        
        print("  -> Đang thực thi consolidated_schema.sql (15 bảng, triggers, RLS)...")
        await conn.execute(schema_sql)
        print("  ✓ Đã tạo thành công toàn bộ bảng, triggers và policies!")

        if seed_sql:
            print("  -> Đang nạp dữ liệu seed ban đầu (workspace, user, identities)...")
            await conn.execute(seed_sql)
            print("  ✓ Đã nạp thành công seed data!")

        # Kiểm tra lại danh sách bảng
        tables = await conn.fetch("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = 'public' 
            ORDER BY table_name;
        """)
        table_names = [r["table_name"] for r in tables]
        print(f"  -> Tổng cộng {len(table_names)} bảng trong schema public:")
        for t in table_names:
            print(f"     • {t}")

        return True
    finally:
        await conn.close()


async def setup_neo4j():
    """Kết nối và nạp 11 constraints + 4 indexes lên Neo4j AuraDB."""
    uri = os.getenv("NEO4J_URI")
    if not uri:
        print("\n  [!] Bỏ qua Neo4j: Chưa cấu hình biến NEO4J_URI trong .env")
        return False

    print("\n[2/2] ĐANG THIẾT LẬP NEO4J AURADB...")
    print(f"  Target URI: {uri}")

    client = Neo4jClient()
    try:
        await client.verify_connectivity()
        print("  ✓ Đã kết nối thành công tới Neo4j AuraDB.")

        print("  -> Đang áp dụng 11 constraints và 4 indexes...")
        executed = await client.execute_cypher_file()
        print(f"  ✓ Đã thực thi thành công {len(executed)} câu lệnh Cypher.")

        constraints = await client.get_active_constraints()
        print(f"  -> Tổng số constraints đang hoạt động trên AuraDB: {len(constraints)}")
        for c in constraints:
            print(f"     • {c.get('name')}: ({c.get('labelsOrTypes', [])}) -> {c.get('properties', [])}")

        return True
    finally:
        await client.close()


async def main():
    load_env_file()
    
    print("=" * 70)
    print("PERSONAL TASK BOARD: AUTOMATED FULL STACK SETUP (PHASE 0 & 1)")
    print("=" * 70)

    supa_ok = False
    neo4j_ok = False

    try:
        supa_ok = await setup_supabase()
    except Exception as e:
        print(f"  [ERROR] Lỗi thiết lập Supabase: {e}", file=sys.stderr)

    try:
        neo4j_ok = await setup_neo4j()
    except Exception as e:
        print(f"  [ERROR] Lỗi thiết lập Neo4j AuraDB: {e}", file=sys.stderr)

    print("\n" + "=" * 70)
    print("KẾT QUẢ THIẾT LẬP NỀN TẢNG (LAYER 3 / PHASE 0 & 1):")
    print(f"  • Supabase PostgreSQL : {'[THÀNH CÔNG]' if supa_ok else '[CHƯA HOÀN TẤT / THIẾU ENV]'}")
    print(f"  • Neo4j AuraDB        : {'[THÀNH CÔNG]' if neo4j_ok else '[CHƯA HOÀN TẤT / THIẾU ENV]'}")
    print("=" * 70)

    if supa_ok and neo4j_ok:
        print("\n>>> CHÚC MỪNG: Toàn bộ nền tảng Layer 3 đã sẵn sàng 100%!")
        print(">>> Bạn có thể bắt đầu triển khai Phase 2 & Phase 3 ngay lập tức.")


if __name__ == "__main__":
    asyncio.run(main())
