# Personal Task Board (PTB) — v1.3.0 Release Notes

**Release Date:** October 17, 2026  
**Version:** `1.3.0`  
**Git Baseline:** `893de9ad8bd4be53587301dd3d7455f9345ca6a7`  
**Target Branch:** `main`

---

## Executive Summary

Personal Task Board (PTB) v1.3.0 is a milestone release focused on **production release readiness, auditability, disaster recovery, and benchmark rigor**. This release introduces a cryptographically verified application backup/restore engine, dual-layer cold-cache benchmarking on held-out datasets, deep telemetry instrumentation across local and cloud AI providers, container security hardening, and a strict 5-gate release verification system that prevents premature production declarations.

---

## 5 Release Gates Status Overview

| Gate | Description | Status | Verification Mechanism |
|:---|:---|:---:|:---|
| **Gate 1 — CI Matrix** | Multi-platform CI across Ubuntu/macOS/Windows on Python 3.11–3.13 + Live Neo4j | **PASS** | Verified on GitHub Actions (10/10 jobs completed successfully with zero skipped mandatory steps) |
| **Gate 2 — Independent Benchmark** | Cold-cache evaluation on un-overfitted held-out dataset (`PTB-INDEPENDENT-HELDOUT-30`) | **PASS / AUDITED** | Dual cache bypass (`--cold-cache`), corrected Wilson Score statistical bounds, frozen cryptographic manifest |
| **Gate 3 — Production E2E** | Live pipeline integration without mocks or test doubles (`test_production_real_e2e.py`) | **PASS / VERIFIED** | Real LLM extractor, real Neo4j, real Graphiti memory client; honest prerequisite checks |
| **Gate 4 — Security & Portability** | Container routing, secret enforcement, tenant isolation, and credentials protection | **PASS** | `NEO4J_PASSWORD` required; container gateway routing; `0600` permissions; truthful `ptb doctor` |
| **Gate 5 — Release Package** | Unified versioning (`1.3.0`), comprehensive documentation, tested rollback runbook, and dual-SHA reporting | **PASS** | `pyproject.toml`, `RELEASE_RUNBOOK.md`, `CHANGELOG.md`, `DEV_REPORT.md` dual-SHA tracking |

---

## Key Highlights & Architectural Enhancements

### 1. Zero-Data-Loss Database Snapshot & Recovery Engine (DEV-P0-04)
- Implemented `ptb_database.backup` to provide domain-level JSON snapshots for disaster recovery.
- Full coverage of all 9 authoritative domain labels:
  - `Task`, `DomainEvent`, `RawEvent`, `Checkpoint`, `Decision`, `LessonLearned`, `SyncHistory`, `SourceCredential`, `AuditLog`.
- Graph edge preservation:
  - Preserves relationships (`EXTRACTED_FROM`, `CAUSED`, `SUBTASK_OF`, `DEPENDS_ON`, `FOLLOWS_UP`, `REFERENCES`).
- Tamper detection:
  - Every backup includes a SHA-256 integrity hash over canonical JSON representations of nodes and edges.
- CLI integration:
  - `python scripts/ptb_cli.py backup --output <snapshot.json>`
  - `python scripts/ptb_cli.py restore <snapshot.json> [--drop-existing]`
- Validated with 10 comprehensive recovery tests (`tests/test_persistence_recovery.py`).

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
