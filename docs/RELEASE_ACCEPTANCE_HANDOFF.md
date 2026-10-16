# PTB — Release Acceptance & Developer Handoff Protocol

**Baseline**: `424f557c2c71171245e6dc995e381ad616001dd5` (main, 2026-10-16)  
**Owner**: Development team (live dependencies) + next audit reviewer (evidence validation)  
**Default release verdict**: **PENDING / NO-GO** until five gates show independent evidence at the **same commit SHA**.

> A report saying “500/500 PASS” is a fixture benchmark result, not evidence of live LLM, Embedding, Kev, Graphiti, connector authentication, UI or production recovery. Do not modify benchmark labels, split, cache, or expected outputs merely to increase scores.

## 1. Code fixes applied in this maintenance commit (subject to CI re-verification)

- Correct the production extraction cache root to `<repo>/data/cache/extraction_cache.json` (`/app/data/cache` in Docker), not `services/data`.
- Never bootstrap production results from `benchmarks/runner/.cache/extraction_cache.json`. Store production cache with atomic replacement and POSIX 0600 permissions where supported.
- Reject legacy cache aliases without matching content hash during hashed lookups.
- Use explicitly enumerated uv workspace members; the addition of `services/data` must never break later `uv run` commands.
- Add six unit regression guards; clarify docs and release claims. **Not validated until the GitHub CI run for this commit completes.**

## 2. Mandatory Dev tasks requiring real services / ownership

| Task ID | Priority | Dev action | Acceptance evidence |
|---|---|---|---|
| DEV-P0-01 | P0 | Run full GitHub CI matrix on final SHA: Ubuntu/macOS/Windows × Python 3.11–3.13. | Green 9/9 jobs; Fixture E2E, Runtime Smoke, Full Non-Docker, Manifest, Release gates all execute instead of being skipped. Link to Actions run and logs. |
| DEV-P0-02 | P0 | Clean-clone + production Docker launch on another machine, no dev-machine shortcuts. | `docker compose -f docker-compose.prod.yml config`, `up -d --build`, `ps`, API health/doctor, OpenWebUI screenshot and sanitized logs. |
| DEV-P0-03 | P0 | True end-to-end source → RawEvent → processing (real LLM) → Neo4j → Graphiti → REST/MCP → OpenWebUI CRUD. | External-service tests with no `FakeDeterministicLLMExtractor` in core acceptance path; actual event/task/evidence IDs, screenshots and redacted traces. |
| DEV-P0-04 | P0 | Verify persistence/recovery: restart & recreate containers, safe Neo4j backup + restore, failure/retry injection. | Before/after counts for tasks, evidence, checkpoints, cache and session; no duplicates; honest degraded health; restore exercise log. |
| DEV-P1-01 | P1 | Independent held-out **cold-cache** benchmark, compared to current warm-cache fixture 500/500. | Frozen independent test samples with SHA256 and thresholds fixed **before** execution, exact model/prompt version, real request/token counts and quality metrics. |
| DEV-P1-02 | P1 | Cross-platform credentials and acquisition checks: Microsoft Teams/Outlook, available Jira/Shortcut/Confluence/GitHub/Slack, local Git and Coding Agents. | Real login/sync where authorized; source-specific health, host home/workspace volume visibility, 0600 on POSIX, Windows ACLs, masking, restart behavior. |
| DEV-P1-03 | P1 | Validate Kev and Qwen3 Embedding plus Graphiti from **inside** production containers. | Explicit Docker-reachable `DECISION_BASE_URL` and `EMBEDDING_BASE_URL`; measured calls, ranking comparisons, degraded behavior, local model readiness. |
| DEV-P1-04 | P1 | Replace Graphiti fixed-call estimates and default-only price multiplication with actual instrumentation. | Real upstream LLM/embedding/KEV requests, retries/failures/tokens, per-provider/model rates, reproducible dollar figures. |
| DEV-P1-05 | P1 | Align project version, release notes, Docker/runbook, backup instructions and official GitHub release. | Version/tag consistent; tested rollback and restore; signed-off release SHA; published Release only after gates PASS. |

### Commands (run, record exit status; these are NOT test results)

```bash
git rev-parse HEAD
uv sync --all-packages
uv run pytest -m contract -q
uv run pytest -m unit -q
uv run pytest -m fixture_e2e -v
uv run pytest -m runtime_smoke -v
uv run pytest -m 'not external_integration' -q
uv run pytest tests/test_release_readiness.py -v
uv run python -m benchmarks.cli validate --dataset-dir benchmarks/dataset/mini_smoke
uv run python -m benchmarks.cli validate --dataset-dir benchmarks/dataset/ptb_500
uv run python -m benchmarks.cli run --dataset-dir benchmarks/dataset/mini_smoke --cold-cache
uv run python -m scripts.ptb_cli acceptance-report --run-checks
docker compose -f docker-compose.prod.yml config
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps
```

**Benchmark warning:** `benchmarks/runner/extraction_cache.py` persists sample-ID based warm cache under `benchmarks/runner/.cache/`; switching `--mode live` is not proof of a truly cold-cache real-DB benchmark. Build an isolated acceptance harness or use a temporary independent clone with an empty cache and truly unseen held-out data. Record the actual model/provider/network traffic and dataset hashes. Preserve tracked fixture reports; never commit private events, tokens, cookie state, unredacted user content, or private screenshots.

**Existing CI Neo4j E2E warning:** `tests/test_external_integration.py::test_live_e2e_full_vertical_slice` injects `FakeDeterministicLLMExtractor`. Its success shows Neo4j connectivity and domain integration, but **does not** prove real LLM extraction or Graphiti AI execution.

## 3. Required Dev reporting protocol

After coding and testing on a particular SHA, add:

`reports/release_acceptance/<FULL_40_CHAR_SHA>/DEV_REPORT.md`

For each task, include **PASS / FAIL / BLOCKED / PENDING**, executed commands and exit codes, artifact URL and redacted logs, measured outcome, remaining defect and owner. **Missing evidence = PENDING; skipped test = SKIPPED (not PASS).**

The report and evidence must identify the exact SHA under test. Any new code commit after testing requires CI rerun and impacted acceptance gates re-validation. No personal chat contents, credentials, browser cookies, access tokens, or private customer identifiers in commits/logs.

### Required report template

```markdown
# PTB Release Acceptance Report
- Commit SHA: <40 chars>
- Date UTC: <ISO8601>
- Author/reviewer: <handle>
- OS/architecture/Python/uv/Docker/Compose versions:
- Neo4j/OpenWebUI/Graphiti versions:
- LLM provider/model/prompt, embedding and Kev models/endpoints (redacted):
- CPU/GPU/RAM, relevant access limitations:

| Task ID | Status | Command and exit code | Evidence URL | Issue/owner |
|---|---|---|---|---|
| DEV-P0-01 | PENDING | | | |
| DEV-P0-02 | PENDING | | | |
| DEV-P0-03 | PENDING | | | |
| DEV-P0-04 | PENDING | | | |
| DEV-P1-01 | PENDING | | | |
| DEV-P1-02 | PENDING | | | |
| DEV-P1-03 | PENDING | | | |
| DEV-P1-04 | PENDING | | | |
| DEV-P1-05 | PENDING | | | |

## Raw measurements (no estimates labeled as actual)
- Dataset hash, held-out split and whether cache was cold:
- TP/FP/FN, attribution, deadline, FMR/MMR, confidence intervals:
- LLM requests, HTTP retries/errors, tokens, embeddings, Kev, Graphiti subcalls:
- System health, task/raw-event/checkpoint counts, p95 end-to-end latency:
- Backup/restore/restart and security/permission verification:

| Release Gate | PASS / FAIL / BLOCKED / PENDING | Supporting evidence |
|---|---|---|
| 1 — CI Matrix | PENDING | |
| 2 — Independent Benchmark | PENDING | |
| 3 — Real Production E2E | PENDING | |
| 4 — Security & Portability | PENDING | |
| 5 — Release Package | PENDING | |

- Dev verdict: NO-GO / PENDING / GO
- Unresolved defects, next responsible owner and follow-up date:
```

## 4. Audit protocol for next ChatGPT review

1. Fetch current `main` HEAD, compare against prior audit/maintenance SHA, inspect production diffs.
2. Read the report under `reports/release_acceptance/<sha>/DEV_REPORT.md` and verify each link with actual GitHub CI logs, run artifacts and source.
3. Reconcile any stale/older SHA evidence; if a gate cannot be independently verified, mark it PENDING.
4. Report P0/P1/P2 issues and separate verdicts: (a) developer source handoff, (b) supervised beta, (c) autonomous user/production release.
5. Never infer truth from `Task/REPORT_ALL_TASKS.md` GO claims, fixture 500/500 or tests injected with fake LLM in place of actual-service acceptance.
