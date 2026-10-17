# PTB Release Acceptance Report
- **Commit SHA:** `cd706ee4921c0e813b936e6fff2fab39c5885e73`
- **Date UTC:** 2026-10-17T04:10:31Z
- **Author/reviewer:** `QangQang`
- **OS/Architecture:** Windows 11 (AMD64)
- **Python / uv:** CPython 3.12.0 | uv 0.11.3 (45da18ac3 2026-04-01 x86_64-pc-windows-msvc)
- **Docker / Compose:** Docker version 29.8.1, build 4a63305 | Docker Compose version v5.5.1
- **Graphiti Core:** graphiti-core installed
- **Hardware:** 16 CPU cores, 23.2 GB RAM
- **LLM Provider / Model:** openai / gpt-4o-mini (Base URL: Not configured)
- **Kev / Embedding Endpoints:** Kev: http://127.0.0.1:8081/v1 | Embedding: http://127.0.0.1:8082/v1

## 1. Acceptance Task Status

| Task ID | Priority | Description | Status | Command and Exit Code | Evidence / Notes |
|---|---|---|---|---|---|
| DEV-P0-01 | P0 | Run full GitHub CI matrix on final SHA: Ubuntu/macOS/Windows × Python 3.11–3.13. | **PASS** | `GitHub Actions #38944067496 (Exit 0)` | Green 10/10 jobs (9 OS/Python matrix + 1 Live Neo4j E2E) on GitHub Actions run #38944067496. |
| DEV-P0-02 | P0 | Clean-clone + production Docker launch on another machine, no dev-machine shortcuts. | **PENDING** | `N/A — not executed in current run` | docker compose -f docker-compose.prod.yml config, up -d --build, ps, API health/doctor, OpenWebUI CRUD. |
| DEV-P0-03 | P0 | True end-to-end source → RawEvent → processing (real LLM) → Neo4j → Graphiti → REST/MCP → OpenWebUI CRUD. | **PENDING** | `N/A — not executed in current run` | External-service tests with no FakeDeterministicLLMExtractor in core acceptance path. |
| DEV-P0-04 | P0 | Verify persistence/recovery: restart & recreate containers, safe Neo4j backup + restore, failure/retry injection. | **PENDING** | `N/A — not executed in current run` | Before/after counts for tasks, evidence, checkpoints, cache and session; no duplicates; honest degraded health. |
| DEV-P1-01 | P1 | Independent held-out cold-cache benchmark, compared to current warm-cache fixture 500/500. | **PENDING** | `pytest tests/test_cold_cache_benchmark.py -q (Exit 0)` | Harness verification PASS (Exit 0). Live production/benchmark verification PENDING. |
| DEV-P1-02 | P1 | Cross-platform credentials and acquisition checks: Microsoft Teams/Outlook, Jira/Shortcut/Confluence/GitHub/Slack, local Git. | **PENDING** | `N/A — not executed in current run` | Real login/sync where authorized; source-specific health, 0600 on POSIX, masking, restart behavior. |
| DEV-P1-03 | P1 | Validate Kev and Qwen3 Embedding plus Graphiti from inside production containers. | **PENDING** | `pytest tests/test_kev_embedding_auto_wiring.py -q (Exit 0)` | Harness verification PASS (Exit 0). Live production/benchmark verification PENDING. |
| DEV-P1-04 | P1 | Replace Graphiti fixed-call estimates and default-only price multiplication with actual instrumentation. | **PENDING** | `pytest tests/test_graphiti_telemetry_instrumentation.py -q (Exit 0)` | Harness verification PASS (Exit 0). Live production/benchmark verification PENDING. |
| DEV-P1-05 | P1 | Align project version, release notes, Docker/runbook, backup instructions and official GitHub release. | **PENDING** | `N/A — not executed in current run` | Version/tag consistent; tested rollback and restore; signed-off release SHA; published Release only after gates PASS. |

## 2. Raw Measurements & Telemetry Audit
- **Telemetry snapshot:** LLM requests: 0, tokens in: 0, tokens out: 0, estimated cost: $0.0000. Graphiti episodes: 0, subcalls: 0 ({})
- **Cold-cache benchmark harness:** CLI option `--cold-cache` integrated and verified (DEV-P1-01).
- **Kev & Qwen3 container routing:** Container host fallback (`host.docker.internal`) and graceful degradation enabled (DEV-P1-03).
- **Graphiti instrumentation:** Detailed per-model rates and sub-call telemetry active (DEV-P1-04).

## 3. Release Gates Verdict

| Release Gate | PASS / FAIL / BLOCKED / PENDING | Supporting evidence |
|---|---|---|
| 1 — CI Matrix | **PASS** | Evaluated on commit `cd706ee492` |
| 2 — Independent Benchmark | **PENDING** | Evaluated on commit `cd706ee492` |
| 3 — Real Production E2E | **PENDING** | Evaluated on commit `cd706ee492` |
| 4 — Security & Portability | **PENDING** | Evaluated on commit `cd706ee492` |
| 5 — Release Package | **PENDING** | Evaluated on commit `cd706ee492` |

- **Overall Release Verdict:** **PENDING**
- **Next audit review note:** Final GO sign-off requires all 5 gates to show PASS at the identical SHA on the remote CI matrix.
