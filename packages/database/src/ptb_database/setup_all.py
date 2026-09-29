"""Automated Setup Script for Neo4j Single-Store.

Tự động thực thi toàn bộ constraints và seed data lên Neo4j:
    uv run python -m ptb_database.setup_all
"""

import asyncio
import os
import sys
from pathlib import Path

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


async def setup_neo4j():
    """Kết nối và nạp các constraints + indexes lên Neo4j (không nạp dev seed)."""
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")

    print("\nĐANG THIẾT LẬP NEO4J SINGLE-STORE...")
    print(f"  Target URI: {uri}")

    client = Neo4jClient()
    try:
        await client.verify_connectivity()
        print("  ✓ Đã kết nối thành công tới Neo4j.")

        print("  -> Đang áp dụng constraints và indexes...")
        executed = await client.execute_cypher_file()
        print(f"  ✓ Đã thực thi thành công {len(executed)} câu lệnh Cypher constraints.")

        constraints = await client.get_active_constraints()
        print(f"  -> Tổng số constraints đang hoạt động trên Neo4j: {len(constraints)}")
        for c in constraints:
            print(f"     • {c.get('name')}: ({c.get('labelsOrTypes', [])}) -> {c.get('properties', [])}")

        return True
    finally:
        await client.close()


async def seed_dev_data(client: Neo4jClient | None = None) -> bool:
    """Nạp dữ liệu seed dev (Person, Project, Identities) lên Neo4j (chạy riêng biệt qua dev-seed)."""
    close_client = False
    if client is None:
        client = Neo4jClient()
        close_client = True
    try:
        await client.verify_connectivity()
        seed_path = Path(__file__).parent.parent.parent / "neo4j" / "seeds" / "001_dev_seed.cypher"
        if seed_path.exists():
            print("  -> Đang nạp dữ liệu seed ban đầu lên Neo4j (Person, Project, Identities)...")
            await client.execute_cypher_file(seed_path)
            print("  ✓ Đã nạp thành công seed data lên Neo4j!")
            return True
        else:
            print(f"  [WARN] File seed không tồn tại: {seed_path}", file=sys.stderr)
            return False
    finally:
        if close_client:
            await client.close()


async def main():
    load_env_file()
    
    print("=" * 70)
    print("PERSONAL TASK BOARD: SINGLE-STORE LOCAL NEO4J SETUP")
    print("=" * 70)

    neo4j_ok = False
    try:
        neo4j_ok = await setup_neo4j()
        if "--seed" in sys.argv or "--dev-seed" in sys.argv:
            print("\nĐANG NẠP DỮ LIỆU DEV SEED...")
            await seed_dev_data()
    except Exception as e:
        print(f"  [ERROR] Lỗi thiết lập Neo4j: {e}", file=sys.stderr)

    print("\n" + "=" * 70)
    print("KẾT QUẢ THIẾT LẬP NỀN TẢNG (SINGLE-STORE NEO4J):")
    print(f"  • Neo4j Single-Store  : {'[THÀNH CÔNG]' if neo4j_ok else '[CHƯA HOÀN TẤT / KIỂM TRA DOCKER]'}")
    print("=" * 70)

    if neo4j_ok:
        print("\n>>> Nền tảng Single-Store Neo4j (Layer 3) đã sẵn sàng 100%!")
        print(">>> Hệ thống hoạt động hoàn toàn ở chế độ Local-First Single-Store.")


if __name__ == "__main__":
    asyncio.run(main())
