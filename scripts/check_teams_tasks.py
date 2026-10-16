import asyncio
import json
import sys
from pathlib import Path
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")
sys.stdout.reconfigure(encoding="utf-8")

from ptb_database.neo4j_client import Neo4jClient

async def check():
    client = Neo4jClient()
    driver = client.get_driver()
    async with driver.session() as s:
        res = await s.run("""
            MATCH (t:UnifiedTask)-[:HAS_EVIDENCE]->(e:Evidence)
            WHERE e.source_type CONTAINS 'teams'
            RETURN t.id AS tid, t.title AS title, e.source_type AS st, e.snippet AS snip, e.external_url AS url
        """)
        records = await res.data()
        print(f"Found {len(records)} tasks with MS Teams evidence:")
        for r in records:
            print("---")
            print(f"Task ID: {r['tid']}")
            print(f"Title:   {r['title']}")
            print(f"Source:  {r['st']}")
            print(f"URL:     {r['url']}")
            print(f"Snippet: {r['snip'][:100]}")

if __name__ == "__main__":
    asyncio.run(check())
