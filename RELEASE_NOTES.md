# Personal Task Board (PTB) — v1.3.0 Release Notes

**Release Date:** October 17, 2026  
**Version:** `1.3.0`  
**Git Baseline:** `893de9ad8bd4be53587301dd3d7455f9345ca6a7`  
**Target Branch:** `main`

---

## Executive Summary

Personal Task Board (PTB) v1.3.0 is a milestone release focused on **production release readiness, auditability, disaster recovery, and benchmark rigor**. This release introduces a cryptographically verified application backup/restore engine, dual-layer cold-cache benchmarking on held-out datasets, deep telemetry instrumentation across local and cloud AI providers, container security hardening, and a strict 5-gate release verification system that prevents premature production declarations.

---

## Acceptance status (evidence-based, NOT a production release approval)

This is a **release candidate changelog**, not a published GitHub Release.
The previous 5/5 PASS claims were not supported by live acceptance evidence.
At the time of the source audit, the prior CI matrix on a6c2731 was green,
but all 12 new production_e2e tests were SKIPPED in that CI. The latest
acceptance status must be rechecked at the current HEAD; do not borrow a
CI result from a different commit.

| Gate | Status | Evidence / outstanding work |
|---|---|---|
| 1 CI matrix | PENDING for current release SHA | [Actions runs](https://github.com/nxc1802/Personal_Task_Board/actions); verify exact HEAD 10/10 |
| 2 Independent benchmark | **FAIL / PENDING cold evaluation** | [Held-out report](reports/benchmarks/independent/heldout_report.md): fixture 20/32 PASS, 11 FAIL, 1 PENDING |
| 3 Real production E2E | **PENDING** | 12 tests existed but all SKIPPED in CI on a6c2731; require strict no-SKIP live run |
| 4 Security and portability | **PENDING** | Requires clean machine, real auth/connector, Docker and restart evidence |
| 5 Release package | **PENDING** | No verified release artifact/tag and no live restore acceptance |

## Scope and limitations

- The JSON tool `ptb backup` is a **partial PTB domain snapshot**, not a
  verified full Neo4j/Graphiti backup. Some labels, multi-label nodes,
  relationships and auxiliary data may be excluded. For disaster recovery,
  use an appropriate **native Neo4j database backup** and test a restore.
- A SHA-256 checksum detects accidental or untrusted modifications only when
  the original digest is trusted; it is **not** encryption or an authenticated signature.
- `ptb restore` currently merges selected snapshot nodes and relationships;
  it is **not** an atomic full database rollback and does not support
  `--drop-existing`. Do not use it as the sole recovery method.
- The production_e2e suite contains real Neo4j components, but the
  failure-injection test uses a mock intentionally; REST TestClient
  checks do not represent a live browser or MCP session.
- The held-out dataset is tracked in this repository and some held-out
  sample IDs appear in tracked extraction cache. Freeze a *new, unseen*
  evaluation set and use dual cache bypass for final model acceptance.
- **Required evidence/reporting:** [Dev acceptance handoff](docs/RELEASE_ACCEPTANCE_HANDOFF.md)
  and [the next Dev work plan](docs/DEV_REMAINING_RELEASE_WORK.md).

---

## Key Highlights & Architectural Enhancements

### 1. Partial Domain JSON Snapshot & Recovery Utilities (DEV-P0-04)
- Implemented `ptb_database.backup` with guarded domain JSON snapshots; a complete Neo4j backup is still required for disaster recovery.
- Explicit exported label list: `Task`, `Evidence`, `RawEvent`, `IngestionCheckpoint`, `Person`, `Project`, `Customer`, `Decision`, `Lesson` (other labels are not covered).
- Graph edge preservation:
  - Preserves relationships (`EXTRACTED_FROM`, `CAUSED`, `SUBTASK_OF`, `DEPENDS_ON`, `FOLLOWS_UP`, `REFERENCES`).
- Tamper detection:
  - Every backup includes a SHA-256 integrity hash over canonical JSON representations of nodes and edges.
- CLI integration:
  - `python scripts/ptb_cli.py backup --output <snapshot.json>`
  - `python scripts/ptb_cli.py restore <snapshot.json>` (merge only; no `--drop-existing`)
- Mock/in-memory recovery tests exist; a live multi-label Neo4j restore drill remains PENDING.

### 2. Independent Held-Out Benchmark & Statistical Rigor (DEV-P1-01)
- Created independent held-out dataset `PTB-INDEPENDENT-HELDOUT-30` (30 diverse enterprise events, 2 probe queries) with SHA-256 manifests.
- Added explicit execution modes in `benchmarks.cli`:
  - `fixture_regression` (Mode A): Warm-cache fixture regression suite.
  - `cold_llm_eval` (Mode B): Dual-cache bypass (`ExtractionCache` + `LLMStructuredExtractor` bypassed) evaluating pure LLM extraction quality.
  - `production_e2e_eval` (Mode C): End-to-end evaluation including graph ingestion and memory retrieval.
- Corrected Dimension 1 Wilson Score evaluation: Denominator now strictly tracks actual task instances `tot_tp / (tot_tp + tot_fn)` instead of diluting with non-task negative samples (`TN`).

### 3. Production Real End-to-End Test Suite (DEV-P0-03)
- Implemented `tests/e2e/test_production_real_e2e.py` covering E2E-01 through E2E-12.
- Executes full lifecycle using authentic components:
  - Real `LLMStructuredExtractor` with provider validation.
  - Real `ProcessingPipeline` with event deduplication and task synthesis.
  - Real `Neo4jClient` and `TaskDomainRepository`.
  - Real `GraphitiMemoryClient` episodic temporal memory.
- Enforces zero test doubles or fake extractors in production paths; skips honestly when upstream live infrastructure is offline.

### 4. Local AI Auto-Wiring, Dimension Validation & Bypass Telemetry (DEV-P1-03)
- Local cross-encoder Kev reranker automatically bypasses inference when input documents count is 1 (`last_call_status = "bypass"`, `last_usage_tokens = 0`), saving compute.
- `Qwen3EmbeddingService` validates embedding vector dimensions (`is_valid_embedding(vec, expected_dim=1024)`) to reject malformed embeddings immediately.
- Cross-encoder auto-wiring in graph memory adapter reports actual call states and gracefully degrades when local models are unavailable.

### 5. Cost Audit & Telemetry Hardening (DEV-P1-04)
- Canonical `RequestAuditState` enum (`SUCCESS`, `FAILURE`, `FALLBACK`, `BYPASS`, `CACHE_HIT`, `SKIPPED`).
- Separation of measured tokens from estimated tokens.
- Separation of local AI zero-cost inference ($0.0) from cloud provider inference costs.
- Sub-call breakdown for Graphiti episodic operations (`extract_nodes`, `generate_edges`, `embed_content`).

### 6. Production Security & Container Portability (DEV-P0-02, DEV-P1-02)
- Removed default passwords from `docker-compose.prod.yml`; enforced `${NEO4J_PASSWORD}` variable.
- Added `host.docker.internal:host-gateway` to enable secure container-to-host AI routing.
- Mandated `PTB_API_KEY` for REST API in production mode.
- `ptb doctor` provides honest diagnostics for all 9 data acquisition channels without simulated health.

---

## Deployment & Upgrade Instructions

1. **Pull the latest codebase**:
   ```bash
   git fetch origin
   git checkout main
   ```

2. **Verify Environment Configurations**:
   Ensure `.env` contains mandatory production secrets:
   ```ini
   NEO4J_PASSWORD=<SECURE_STRONG_PASSWORD>
   PTB_API_KEY=<SECURE_APPLICATION_API_KEY>
   ```

3. **Deploy with Docker Compose**:
   ```bash
   docker compose -f docker-compose.prod.yml up -d --build
   ```

4. **Verify Deployment Health**:
   ```bash
   docker compose -f docker-compose.prod.yml exec ptb-app python scripts/ptb_cli.py doctor
   ```

5. **Create a Domain Snapshot Before Operations**:
   ```bash
   docker compose -f docker-compose.prod.yml exec ptb-app python scripts/ptb_cli.py backup --output /data/backup_initial.json
   ```

---

## Contributors & Acknowledgements
- Developed and hardened by the PTB Engineering Team.
- Verified under Google DeepMind Antigravity Pair-Programming environment.
