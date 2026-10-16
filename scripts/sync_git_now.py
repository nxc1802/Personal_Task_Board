import asyncio
import os
from pathlib import Path
import sys

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "packages" / "contracts" / "src"))
sys.path.insert(0, str(ROOT_DIR / "packages" / "database" / "src"))
sys.path.insert(0, str(ROOT_DIR / "services" / "acquisition" / "src"))
sys.path.insert(0, str(ROOT_DIR / "services" / "processing" / "src"))

from dotenv import load_dotenv
load_dotenv(ROOT_DIR / ".env")

from ptb_database import Neo4jClient, RawEventRepository, CheckpointRepository, TaskDomainRepository
from ptb_acquisition.adapters.git_adapter import GitWatcherAdapter
from ptb_acquisition.pipeline import AcquisitionPipeline
from ptb_processing.pipeline import ProcessingPipeline
from ptb_processing.worker import ProcessingWorker

async def sync_git():
    print("=" * 70)
    print("LOCAL GIT REPOSITORY DATA ACQUISITION & PROCESSING PIPELINE")
    print("=" * 70)

    neo4j_uri = os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687")
    neo4j_user = os.getenv("NEO4J_USERNAME", "neo4j")
    neo4j_password = os.getenv("NEO4J_PASSWORD", "personal_task_board_2026")

    neo4j_client = Neo4jClient(uri=neo4j_uri, username=neo4j_user, password=neo4j_password)
    raw_repo = RawEventRepository(neo4j_client)
    ckpt_repo = CheckpointRepository(neo4j_client)
    task_repo = TaskDomainRepository(neo4j_client)

    pipeline = AcquisitionPipeline(raw_event_repo=raw_repo, checkpoint_repo=ckpt_repo, tenant_id="local-user")
    adapter = GitWatcherAdapter(repo_paths=[str(ROOT_DIR)], tenant_id="local-user")

    print(f"[*] Đang quét Git commits từ repo: {ROOT_DIR}...")
    ingested_count = await pipeline.sync_adapter(adapter, stream_id="git-main")
    print(f"✓ Đã thu thập {ingested_count} commit mới từ Git repository!")

    # Process batch with Worker
    print("[*] Đang chạy tiến trình bóc tách công việc từ Git commits...")
    proc_pipe = ProcessingPipeline(task_repo=task_repo)
    worker = ProcessingWorker(raw_event_repo=raw_repo, pipeline=proc_pipe, max_retries=2)
    extracted = await worker.process_batch(limit=25)
    print(f"✓ Hoàn tất xử lý {len(extracted)} sự kiện Git!")

    await neo4j_client.close()

if __name__ == "__main__":
    asyncio.run(sync_git())
