"""Script to purge all user/task data from Neo4j and reset to fresh baseline."""

import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv()

from ptb_database.neo4j_client import Neo4jClient
from ptb_database.setup_all import setup_neo4j, seed_dev_data


async def clear_all_data():
    print("=" * 70)
    print("PERSONAL TASK BOARD: CLEARING ALL DATABASE DATA")
    print("=" * 70)

    client = Neo4jClient()
    driver = client.get_driver()

    async with driver.session(database=client.database) as session:
        # 1. Count before
        res = await session.run("MATCH (n) RETURN count(n) AS cnt")
        record = await res.single()
        before_cnt = record["cnt"] if record else 0
        print(f"[*] Total nodes before clearing: {before_cnt}")

        # 2. Detach delete all nodes & relationships
        print("[*] Executing DETACH DELETE on all nodes...")
        await session.run("MATCH (n) DETACH DELETE n")
        print("  -> All nodes and relationships purged successfully.")

        # 3. Verify clean
        res = await session.run("MATCH (n) RETURN count(n) AS cnt")
        record = await res.single()
        purged_cnt = record["cnt"] if record else 0
        print(f"[*] Total nodes after purge: {purged_cnt}")

    await client.close()

    # 4. Re-apply schema constraints & indexes
    print("\n[*] Reapplying schema constraints and indexes...")
    await setup_neo4j()

    # 5. Re-apply baseline canonical dev seed (Person, Project, Identities)
    print("\n[*] Seeding baseline canonical entities (Person, Project, Identities)...")
    await seed_dev_data()

    # 6. Final verification report
    client2 = Neo4jClient()
    driver2 = client2.get_driver()
    async with driver2.session(database=client2.database) as session:
        res = await session.run("MATCH (n) RETURN labels(n) AS labels, count(n) AS cnt")
        records = [r.data() async for r in res]
        print("\n" + "=" * 70)
        print("FINAL DATABASE STATE (CLEAN BASELINE):")
        print("=" * 70)
        for r in records:
            lbl = ":".join(r["labels"])
            print(f"  • {lbl}: {r['cnt']}")
    await client2.close()

    print("\n[✓] Database cleared and reset to fresh baseline successfully!")


if __name__ == "__main__":
    asyncio.run(clear_all_data())
