"""CLI script to initialize Neo4j AuraDB schema constraints and verify connectivity.

Chạy:
    uv run python -m ptb_database.init_neo4j
"""

import asyncio
import os
import sys
from pathlib import Path

from ptb_database.neo4j_client import Neo4jClient


async def main():
    # Tự động nạp file .env nếu có
    env_file = Path(__file__).parent.parent.parent.parent.parent / ".env"
    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    if key not in os.environ:
                        os.environ[key] = val.strip("\"'")

    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    username = os.getenv("NEO4J_USERNAME", "neo4j")
    masked_pw = "***" if os.getenv("NEO4J_PASSWORD") else "(not set)"

    print("=" * 60)
    print("PERSONAL TASK BOARD: NEO4J AURADB INITIALIZER")
    print("=" * 60)
    print(f"Target URI : {uri}")
    print(f"Username   : {username}")
    print(f"Password   : {masked_pw}")
    print("-" * 60)

    client = Neo4jClient()

    try:
        print("[1/3] Đang kiểm tra kết nối tới Neo4j...")
        await client.verify_connectivity()
        print("  -> Kết nối thành công!")

        print("[2/3] Đang áp dụng 11 constraints & 4 indexes...")
        executed = await client.execute_cypher_file()
        for stmt in executed:
            print(f"  ✓ {stmt}")
        print(f"  -> Đã thực thi {len(executed)} câu lệnh Cypher.")

        print("[3/3] Đang truy vấn danh sách constraints hiện có...")
        constraints = await client.get_active_constraints()
        print(f"  -> Tổng số constraints trên AuraDB: {len(constraints)}")
        for c in constraints:
            print(f"     - {c.get('name')}: ({c.get('labelsOrTypes', [])}) -> {c.get('properties', [])}")

        print("=" * 60)
        print("Khởi tạo Neo4j AuraDB thành công 100%!")
        print("=" * 60)
    except Exception as e:
        print(f"\n[ERROR] Khởi tạo Neo4j thất bại: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
