import asyncio
import json
import logging
from pathlib import Path
import sys

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

sys.path.insert(0, str(PROJECT_ROOT / "packages" / "contracts" / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "services" / "acquisition" / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "services" / "processing" / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "services" / "intelligence" / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "services" / "database" / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from ptb_contracts import RawEventRecord
from ptb_database.neo4j_client import Neo4jClient
from ptb_database.repositories.task_repo import TaskDomainRepository
from ptb_processing.pipeline import ProcessingPipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ptb.batch_teams")

SAMPLE_FILE = Path(r"d:\Working\PTB\raw_samples\teams\teams_messages.jsonl")


async def main(limit: int = 10):
    if not SAMPLE_FILE.exists():
        print(f"File not found: {SAMPLE_FILE}")
        return

    client = Neo4jClient()
    repo = TaskDomainRepository(client)
    pipeline = ProcessingPipeline(task_repo=repo)

    print("=" * 65)
    print(" BATCH INGESTION: MS TEAMS RAW SAMPLES -> NEO4J TASK BOARD")
    print("=" * 65)

    candidates = []
    with open(SAMPLE_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            text = data.get("normalized_text", "")
            if pipeline.heuristic_filter.should_extract(text, source_type="ms_teams_web"):
                candidates.append(data)

    print(f"[*] Found {len(candidates)} total message candidates matching heuristic filter.")
    to_process = candidates[:limit]
    print(f"[*] Processing top {len(to_process)} candidates with LLM...")

    successful_tasks = 0
    merged_tasks = 0

    for i, data in enumerate(to_process, 1):
        author = data.get("author_display_name")
        text = data.get("normalized_text", "")
        print(f"\n[{i}/{len(to_process)}] By {author}: {text[:60]}...")
        try:
            record = RawEventRecord.model_validate(data)
            res = await pipeline.process(record)
            if res.merged:
                merged_tasks += 1
                print(f"    -> Merged into task {res.target_task_id}")
            elif res.candidate:
                successful_tasks += 1
                print(f"    -> Created Task: {res.candidate.title} (ID: {res.candidate.id}, Conf: {res.candidate.extraction_confidence})")
            else:
                print(f"    -> Filtered out / ignored (Status: {res.status})")
        except Exception as e:
            print(f"    -> Extraction error: {e}")

    print("\n" + "=" * 65)
    print(f"[+] Ingestion completed: {successful_tasks} new tasks created, {merged_tasks} merged.")
    print("=" * 65)


if __name__ == "__main__":
    asyncio.run(main(limit=8))
