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

from datetime import datetime, timezone
from ptb_contracts import IngestionCheckpointRecord, RawEventRecord, SourceType
from ptb_database.neo4j_client import Neo4jClient
from ptb_database.repositories.checkpoint_repo import CheckpointRepository
from ptb_database.repositories.task_repo import TaskDomainRepository
from ptb_processing.pipeline import ProcessingPipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ptb.batch_outlook")

SAMPLE_FILE = Path(r"d:\Working\PTB\raw_samples\outlook\outlook_messages.jsonl")


async def main(limit: int = 10):
    if not SAMPLE_FILE.exists():
        print(f"File not found: {SAMPLE_FILE}")
        return

    client = Neo4jClient()
    task_repo = TaskDomainRepository(client)
    ckpt_repo = CheckpointRepository(client)
    pipeline = ProcessingPipeline(task_repo=task_repo)

    print("=" * 65)
    print(" BATCH INGESTION: MS OUTLOOK RAW SAMPLES -> NEO4J TASK BOARD")
    print("=" * 65)

    candidates = []
    # 1. Thêm fixture mẫu chất lượng cao (PM Huy Nguyen review yêu cầu)
    fixture_path = PROJECT_ROOT / "tests" / "fixtures" / "outlook_messages_fixture.json"
    if fixture_path.exists():
        from ptb_acquisition.playwright.outlook_interceptor import OutlookNetworkInterceptor
        interceptor = OutlookNetworkInterceptor(tenant_id="local-user")
        with open(fixture_path, "r", encoding="utf-8") as ff:
            fix_payload = json.load(ff)
        fix_recs = interceptor.parse_payload(fix_payload)
        for r in fix_recs:
            if pipeline.heuristic_filter.should_extract(r.normalized_text, source_type="ms_outlook_web"):
                candidates.append(r.model_dump())

    # 2. Thêm các email công việc thực tế từ live crawl (bỏ qua newsletter / quảng cáo)
    with open(SAMPLE_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            author = (data.get("author_display_name") or "").lower()
            text = data.get("normalized_text", "")
            if any(skip in author for skip in ["noreply", "newsletter", "training", "digest"]):
                continue
            if any(skip in text.lower() for skip in ["sinh nhật", "birthday", "enroll here", "thành viên mới"]):
                continue
            if pipeline.heuristic_filter.should_extract(text, source_type="ms_outlook_web"):
                candidates.append(data)

    print(f"[*] Found {len(candidates)} total work email candidates matching heuristic filter.")
    to_process = candidates[:limit]
    print(f"[*] Processing top {len(to_process)} candidates with LLM...")

    successful_tasks = 0
    merged_tasks = 0
    last_item_id = None

    for i, data in enumerate(to_process, 1):
        author = data.get("author_display_name")
        text = data.get("normalized_text", "")
        last_item_id = data.get("external_id")
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

    # Cập nhật Checkpoint cho Outlook
    cp = IngestionCheckpointRecord(
        id="cp-ms-outlook-main",
        tenant_id="local-user",
        source_type=SourceType.MS_OUTLOOK_WEB,
        stream_id="outlook-inbox",
        last_external_id=last_item_id or "latest",
        last_event_timestamp=datetime.now(timezone.utc),
        cursor_token="outlook-82-synced",
        updated_at=datetime.now(timezone.utc),
    )
    await ckpt_repo.save_checkpoint(cp)
    print("\n[+] Checkpoint for Microsoft Outlook updated in Neo4j.")

    print("\n" + "=" * 65)
    print(f"[+] Outlook Ingestion completed: {successful_tasks} new tasks created, {merged_tasks} merged.")
    print("=" * 65)


if __name__ == "__main__":
    asyncio.run(main(limit=3))
