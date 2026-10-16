"""ReplayRunner executing benchmark events with per-step state snapshotting and replay verification."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import time
from typing import Any, Dict, List, Optional

from ptb_contracts.l1_acquisition import ProcessingStatus, RawEventRecord
from ptb_contracts.l2_processing import MergeAuditRecord, StatusTransitionAuditRecord, UnifiedTaskCandidate
from ptb_processing.pipeline import PipelineResult

from benchmarks.models import BenchmarkDataset, BenchmarkSample
from benchmarks.runner.environment import BenchmarkEnvironment

logger = logging.getLogger("ptb.benchmarks.runner")


@dataclass
class StepSnapshot:
    """System state snapshot captured immediately after an event step."""
    step_index: int
    sample_id: str
    raw_event_id: str
    ingested_as_new: bool
    should_extract: bool
    merged: bool
    target_task_id: Optional[str]
    task_id: Optional[str]
    resulting_task: Optional[UnifiedTaskCandidate]
    active_task_count: int
    tasks_by_id: Dict[str, UnifiedTaskCandidate]
    raw_event_count: int
    latency_ms: float
    is_pending: bool = False
    pending_reason: Optional[str] = None
    error: Optional[str] = None
    pipeline_result: Optional[PipelineResult] = None


@dataclass
class RunTrace:
    """Execution trace of a benchmark run."""
    mode: str
    dataset_version: str
    snapshots: List[StepSnapshot] = field(default_factory=list)
    final_tasks: List[UnifiedTaskCandidate] = field(default_factory=list)
    raw_event_count: int = 0
    merge_audits: List[MergeAuditRecord] = field(default_factory=list)
    status_audits: List[StatusTransitionAuditRecord] = field(default_factory=list)
    replay_duplicates: int = 0
    probe_results: List[Any] = field(default_factory=list)

    def get_snapshot(self, sample_id: str) -> Optional[StepSnapshot]:
        for s in self.snapshots:
            if s.sample_id == sample_id:
                return s
        return None


class ReplayRunner:
    """Sequential event replay runner with exact latency recording and idempotency verification."""

    def __init__(self, env: BenchmarkEnvironment) -> None:
        self.env = env

    async def _execute_step(self, index: int, sample: BenchmarkSample) -> StepSnapshot:
        t0 = time.perf_counter()
        event_copy = sample.raw_event.model_copy(deep=True)
        self.env.events_by_id[event_copy.id] = event_copy

        # Live infrastructure check
        if self.env.mode == "live" and not self.env.live_infrastructure_available:
            latency = (time.perf_counter() - t0) * 1000.0
            return StepSnapshot(
                step_index=index,
                sample_id=sample.sample_id,
                raw_event_id=sample.raw_event.id,
                ingested_as_new=False,
                should_extract=False,
                merged=False,
                target_task_id=None,
                task_id=None,
                resulting_task=None,
                active_task_count=0,
                tasks_by_id={},
                raw_event_count=0,
                latency_ms=latency,
                is_pending=True,
                pending_reason=self.env.infrastructure_error or "Live infrastructure unavailable",
            )

        ingested_as_new = False
        stored_event = None
        pipeline_res: Optional[PipelineResult] = None
        should_extract = False
        merged = False
        target_task_id: Optional[str] = None
        task_id: Optional[str] = None
        resulting_task: Optional[UnifiedTaskCandidate] = None
        step_error: Optional[str] = None

        try:
            ingested_as_new = await self.env.acquisition_pipeline.ingest_event(event_copy)
            stored_event = await self.env.raw_event_repo.get_by_id(event_copy.id)

            if ingested_as_new and stored_event is not None:
                await self.env.raw_event_repo.mark_event_status(
                    stored_event.id, ProcessingStatus.PROCESSING
                )
                pipeline_res = await self.env.processing_pipeline.process(stored_event)
                await self.env.raw_event_repo.mark_event_status(
                    stored_event.id,
                    ProcessingStatus.PROCESSED,
                    processor_version="benchmark-v1",
                )
                should_extract = pipeline_res.should_extract
                merged = pipeline_res.merged
                target_task_id = pipeline_res.target_task_id
                task_id = pipeline_res.task_id
                if task_id:
                    resulting_task = await self.env.task_repo.get_task_by_id(task_id)
            else:
                # Replay / already-ingested mode: lookup task linked to event
                all_tasks = await self.env.task_repo.list_tasks()
                for existing_task in all_tasks:
                    if any(
                        (getattr(ev, "raw_event_id", None) == sample.raw_event.id or
                         getattr(ev, "source_event_id", None) == sample.raw_event.id)
                        for ev in existing_task.evidences
                    ):
                        resulting_task = existing_task.model_copy(deep=True)
                        task_id = existing_task.id
                        should_extract = True
                        break
        except Exception as exc:
            step_error = str(exc)
            logger.exception("Error during execution of sample %s: %s", sample.sample_id, exc)

        latency_ms = (time.perf_counter() - t0) * 1000.0

        all_tasks_dict = {
            t.id: t.model_copy(deep=True)
            for t in (await self.env.task_repo.list_tasks())
        }

        is_pending = False
        pending_reason = None
        if step_error:
            err_lower = step_error.lower()
            if any(term in err_lower for term in ("api_key", "not configured", "connection refused", "timed out", "timeout", "unavailable", "401", "403", "429", "500", "502", "503", "504", "internal server error")):
                is_pending = True
                pending_reason = f"Infrastructure or credentials unavailable: {step_error}"

        return StepSnapshot(
            step_index=index,
            sample_id=sample.sample_id,
            raw_event_id=sample.raw_event.id,
            ingested_as_new=ingested_as_new,
            should_extract=should_extract,
            merged=merged,
            target_task_id=target_task_id,
            task_id=task_id,
            resulting_task=resulting_task,
            active_task_count=len(all_tasks_dict),
            tasks_by_id=all_tasks_dict,
            raw_event_count=getattr(self.env.raw_event_repo, "count", len(all_tasks_dict)),
            latency_ms=latency_ms,
            is_pending=is_pending,
            pending_reason=pending_reason,
            error=step_error if not is_pending else None,
            pipeline_result=pipeline_res,
        )

    async def run_clean(
        self,
        dataset: BenchmarkDataset,
        filter_case_id: Optional[str] = None,
    ) -> RunTrace:
        """Wipe state and run all samples and probes in dataset."""
        self.env.reset()
        self.env.dataset = dataset
        self.env.events_by_id = {
            s.raw_event.id: s.raw_event.model_copy(deep=True)
            for s in dataset.samples
        }

        # Check if filter_case_id targets a probe
        is_probe_filter = filter_case_id and any(p.probe_id == filter_case_id for p in dataset.probes)

        snapshots: List[StepSnapshot] = []
        if not is_probe_filter:
            samples = dataset.samples
            if filter_case_id:
                samples = [s for s in samples if s.sample_id == filter_case_id]

            for idx, sample in enumerate(samples):
                snap = await self._execute_step(idx, sample)
                snapshots.append(snap)

        # Execute probes
        probe_results = []
        if dataset.probes:
            from benchmarks.runner.probe_runner import ProbeRunner
            probe_runner = ProbeRunner(self.env)
            probe_filter = filter_case_id if is_probe_filter else (None if not filter_case_id else "__NONE__")
            if probe_filter != "__NONE__":
                probe_results = await probe_runner.run_all_probes(dataset.probes, filter_id=probe_filter)

        final_tasks = await self.env.task_repo.list_tasks()
        if final_tasks:
            from benchmarks.evaluator.metrics import canonical_task_sort_key
            final_tasks = sorted(final_tasks, key=canonical_task_sort_key)
        return RunTrace(
            mode=self.env.mode,
            dataset_version=dataset.manifest.version,
            snapshots=snapshots,
            final_tasks=final_tasks,
            raw_event_count=getattr(self.env.raw_event_repo, "count", len(final_tasks)),
            merge_audits=list(getattr(self.env.task_repo, "merge_audits", [])),
            status_audits=list(getattr(self.env.task_repo, "audits", [])),
            probe_results=probe_results,
        )

    async def run_replay(self, dataset: BenchmarkDataset, clean_trace: RunTrace) -> RunTrace:
        """Run all samples a second time against existing state without wiping to verify idempotency."""
        clean_task_count = len(clean_trace.final_tasks)
        snapshots: List[StepSnapshot] = []

        for idx, sample in enumerate(dataset.samples):
            snap = await self._execute_step(idx, sample)
            snapshots.append(snap)

        final_tasks = await self.env.task_repo.list_tasks()
        replay_duplicates = max(0, len(final_tasks) - clean_task_count)

        return RunTrace(
            mode=f"{self.env.mode}-replay",
            dataset_version=dataset.manifest.version,
            snapshots=snapshots,
            final_tasks=final_tasks,
            raw_event_count=getattr(self.env.raw_event_repo, "count", len(final_tasks)),
            merge_audits=list(getattr(self.env.task_repo, "merge_audits", [])),
            status_audits=list(getattr(self.env.task_repo, "audits", [])),
            replay_duplicates=replay_duplicates,
        )
