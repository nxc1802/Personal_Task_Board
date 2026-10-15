"""Batch Pre-extractor populating ExtractionCache for PTB-500.

Runs real LLMStructuredExtractor with controlled concurrency and exponential backoff
to pre-populate benchmarks/runner/.cache/extraction_cache.json.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
from pathlib import Path
import sys
import time
from typing import List, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

from ptb_processing.extractor.heuristic_filter import HeuristicCandidateFilter
from ptb_processing.extractor.llm_extractor import LLMStructuredExtractor
from ptb_processing.parsers.multi_channel import UnifiedChannelParser

from benchmarks.manifest import load_dataset
from benchmarks.models import BenchmarkDataset, BenchmarkSample
from benchmarks.runner.extraction_cache import ExtractionCache

logger = logging.getLogger("ptb.benchmarks.batch_extractor")


async def run_batch_pre_extraction(
    dataset_dir: Path,
    concurrency: int = 3,
    max_samples: Optional[int] = None,
) -> None:
    dataset = load_dataset(dataset_dir)
    cache = ExtractionCache()
    extractor = LLMStructuredExtractor()
    parser = UnifiedChannelParser()
    heuristic_filter = HeuristicCandidateFilter()

    # Filter samples that pass heuristic filter and are not in cache
    needed_samples: List[BenchmarkSample] = []
    for s in dataset.samples:
        if cache.contains(s.raw_event.id):
            continue
        content = s.raw_event.raw_payload if s.raw_event.raw_payload else (s.raw_event.normalized_text or "")
        parsed = parser.parse(content, source_type=s.raw_event.source_type)
        text = parsed.actual_content_text or parsed.quoted_content_text or s.raw_event.normalized_text or ""
        if heuristic_filter.should_extract(text, source_type=s.raw_event.source_type):
            needed_samples.append(s)

    if max_samples:
        needed_samples = needed_samples[:max_samples]

    print(f"[*] Total dataset samples: {len(dataset.samples)}")
    print(f"[*] Already cached:        {cache.count()}")
    print(f"[*] Need extraction:       {len(needed_samples)}")
    print(f"[*] Concurrency limit:     {concurrency}")

    if not needed_samples:
        print("[+] All samples already cached or filtered. Done!")
        return

    sem = asyncio.Semaphore(concurrency)
    completed = 0
    errors = 0
    t0 = time.time()

    async def process_one(sample: BenchmarkSample) -> None:
        nonlocal completed, errors
        async with sem:
            content = sample.raw_event.raw_payload if sample.raw_event.raw_payload else (sample.raw_event.normalized_text or "")
            parsed = parser.parse(content, source_type=sample.raw_event.source_type)
            source_type_str = (
                sample.raw_event.source_type.value
                if hasattr(sample.raw_event.source_type, "value")
                else str(sample.raw_event.source_type)
            )
            t_start = time.time()
            try:
                candidate = await extractor.extract_async(
                    parsed=parsed,
                    raw_event_id=sample.raw_event.id,
                    source_type=source_type_str,
                    external_url=sample.raw_event.deep_link,
                )
                if candidate is not None:
                    cache.put(sample.raw_event.id, candidate)
                completed += 1
                dur = time.time() - t_start
                if completed % 10 == 0 or completed == len(needed_samples):
                    cache.save()
                    elapsed = time.time() - t0
                    print(f"[{completed}/{len(needed_samples)}] Cached ({dur:.1f}s, total elapsed {elapsed/60:.1f}m, errors={errors})")
            except Exception as exc:
                errors += 1
                completed += 1
                print(f"[!] Error on {sample.sample_id}: {exc}")

    tasks = [process_one(s) for s in needed_samples]
    await asyncio.gather(*tasks)
    cache.save()
    print(f"[+] Batch extraction finished in {time.time()-t0:.1f}s. Total cached: {cache.count()}")


if __name__ == "__main__":
    ds_dir = Path("benchmarks/dataset/ptb_500")
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    conc = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    asyncio.run(run_batch_pre_extraction(ds_dir, concurrency=conc, max_samples=limit))
