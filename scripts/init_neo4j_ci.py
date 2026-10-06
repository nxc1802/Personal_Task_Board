"""Script to initialize Neo4j constraints and schema during CI or local startup."""

import asyncio
import os
import sys
from ptb_database.neo4j_client import Neo4jClient


async def main() -> int:
    uri = os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.getenv("NEO4J_USERNAME", "neo4j")
    pwd = os.getenv("NEO4J_PASSWORD", "taskboard123")
    client = Neo4jClient(uri=uri, username=user, password=pwd)

    for i in range(25):
        try:
            if await client.verify_connectivity():
                print(f"Connected to Neo4j successfully on {uri}!")
                applied = await client.execute_cypher_file()
                print(f"Successfully applied {len(applied)} Cypher statements/constraints.")
                await client.close()
                return 0
        except Exception as e:
            print(f"Attempt {i+1}/25 waiting for Neo4j: {e}")
            await asyncio.sleep(2)

    await client.close()
    print("Failed to initialize Neo4j after 25 attempts", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
