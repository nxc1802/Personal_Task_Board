# PTB — Remaining Release Work / Dev Acceptance Report (2026-10-17)

**Current source baseline:** `3ecb9100d1661cefbcd7c8be17b62a4e5e6157d7`. **Branch:** `main`.
**Audit principle:** A CI pass or a test-suite file is not proof of end-to-end production readiness. All source changes must have a 40-character SHA and be re-verified after push.

## Changes prepared by assistant in maintenance commit

- P0 backup utility: require canonical JSON SHA-256 checksum and validated format/identifiers **before** touching Neo4j; write backup snapshot atomically with POSIX 0600 permissions; ignore backups by Git.
- P0 E2E harness correctness: use LLM_BASE_URL instead of Kev DECISION_BASE_URL; correct Kev result shape; test actual evidence write followed by `search_context`; make REST-only tests describe REST instead of MCP or browser UI; optional strict mode `PTB_REQUIRE_PRODUCTION_E2E=1` turns unmet service prerequisites into test failure.
- P1 release reporting: remove premature 5/5 PASS, false full-backup/no-data-loss and `--drop-existing` statements from RELEASE_NOTES; disclose held-out benchmark FAIL and production E2E skips.
- Unit regression tests for malformed/tampered/unsigned snapshots and private atomic saves.

## Work left for Dev (must be run with real services and credentials)

| Task | Priority | Required work | Evidence / acceptance criterion |
|---|---|---|---|
| DEV-P0-A | P0 | **Fix held-out quality regressions**: Jira/GitHub owner missing, Teams requester wrong, Outlook task miss, IN_PROGRESS→TODO, deadline mismatch. Audit structured source identity fields, PersonaResolver, quote attribution, relative-date/timezone normalization, status state machine and comparator ground truth. Do not hardcode `ih-*` IDs, change labels to match outputs, or preload cache. | Before/after per-case results for all 11 FAIL + 1 PENDING, code-level root causes, unit regression tests with *new unseen phrasing*, negative controls. |
| DEV-P0-B | P0 | **Real full production E2E**: clean Docker deploy with Neo4j + real LLM + Qwen3 + Kev + Graphiti + API/MCP + browser OpenWebUI. Run `PTB_REQUIRE_PRODUCTION_E2E=1 uv run pytest tests/e2e/test_production_real_e2e.py -m production_e2e -v -rs`, zero SKIP/FAIL. E2E-10/11 are currently REST TestClient only; create separate live MCP transport and Playwright browser acceptance. | 12/12 non-skipped production E2E PASS plus independent real MCP/OpenWebUI acceptance with screenshots/IDs, logs redacted, model/inference call counts. No simulated success. |
| DEV-P0-C | P0 | **Full Neo4j/Graphiti backup/recovery redesign:** exported labels in `backup.py` are only a domain allowlist; actual schema includes `UnifiedTask`, Graphiti-managed entities, multi-label nodes, relationships/endpoints without `id`. Replace partial JSON with proven native full Neo4j snapshot OR a lossless graph-aware export preserving every node/label/property/edge. Make restore transactional or isolated, verify counts and relationships after restore, cover schema/indexes and Neo4j-native values. | Restore into clean separate Neo4j instance; exact before/after node/edge label counts, sample lineage queries, failure/retry, backup hash, no secret leakage, rollback tested. No claim of zero-data-loss before evidence. |
| DEV-P0-D | P0 | **Production deployment/restart drill**: cold clone on Windows + Linux/macOS as supported, run `docker compose -f docker-compose.prod.yml config`, `up -d --build`, doctor, restart/recreate, backup/restore, protect credentials, validate mounting and permissions, all required dependencies. | versioned commands + exit codes, redacted `docker compose ps`, health, screenshots, task/evidence/checkpoint/replay counts before/after. |
| DEV-P1-E | P1 | **Independent cold LLM benchmark**: held-out report at `reports/benchmarks/independent/heldout_report.md` is fixture **20/32 PASS, 11 FAIL, 1 PENDING** and tracked cache has sample aliases. Generate a new frozen holdout before retuning, no sample reuse; evaluate with real model, clean dual-layer cache, capture raw LLM calls/tokens/latency and quality. | Dataset manifest SHA, frozen thresholds, model/provider/prompt/seed, raw redacted response hashes, error analysis, measured vs estimated tokens. |
| DEV-P1-F | P1 | **Graphiti/Kev/embedding audit**: actual service identity and HTTP calls from inside Docker; correct all 0-vector/fallback/bypass counters, count Graphiti upstream calls rather than per-episode guesses. | Traceable per-model request/response counts and cost, outcome states SUCCESS/FAILURE/FALLBACK/BYPASS; concrete semantic retrieval evidence. |
| DEV-P1-G | P1 | **Security & source connectors**: Teams/Outlook/Jira/Shortcut/Confluence/GitHub/Slack authentication where authorized, Git/Coding Agent cross-platform mounts, session persistence, secret scanning, Neo4j/API password policy, backup ACLs. | Per-connector status and redacted proof; missing credentials = BLOCKED not PASS; no private content in repository. |
| DEV-P1-H | P1 | **Release QA**: rerun 10-job CI matrix on final tested source SHA, ensure version/tag and release package, prohibit GO unless all gates verified, reconcile `RELEASE_NOTES.md`, `DEV_REPORT.md` and CI. | Actual release SHA and exact 10/10 run URL, 5-gate evidence table, GitHub Release only after gates PASS. |

## Benchmark confirmed root-cause samples from last report

- Owner missing: `ih-04`, `ih-05`, `ih-10`, `ih-11`, `ih-15`, `ih-28` (Jira/GitHub).
- Requester mismatch: `ih-06`, `ih-09`, `ih-14` (Teams).
- Status mismatch: `ih-27` (Slack).
- Extraction miss: `ih-30` (Outlook).
- Infrastructure PENDING: `ih-13` (HTTP 500).
- Deadline Dimension 4: 12/18, Status Dimension 5: 16/19, total p95≈26.9 s. Reproduce per case, also analyze aggregate mistakes not identified as the single case's primary failure.
- These samples are *diagnostic*, not an untouched final held-out set. Do not overfit them or advertise fixture PASS as live-LLM PASS.

## Mandatory report protocol

Dev creates `reports/release_acceptance/<FULL_TESTED_SOURCE_SHA>/DEV_REPORT.md`, plus sanitized `evidence/` or immutable CI artifacts. Include:

```markdown
# PTB Release Acceptance / Handoff
- tested_source_sha: <40-char sha>
- report_commit_sha: <40-char sha>
- UTC timestamp:
- Environment: OS/Python/uv/Docker/Neo4j/Graphiti/OpenWebUI
- Models & provider versions: (redacted URLs, no API keys)
- Actual CI workflow run URL & head_sha:
| Task ID | PASS/FAIL/BLOCKED/PENDING | Commands/exit code | Evidence URL | Defects and owner |
|---|---|---|---|---|
| DEV-P0-A | PENDING | | | |
| DEV-P0-B | PENDING | | | |
| DEV-P0-C | PENDING | | | |
| DEV-P0-D | PENDING | | | |
| DEV-P1-E | PENDING | | | |
| DEV-P1-F | PENDING | | | |
| DEV-P1-G | PENDING | | | |
| DEV-P1-H | PENDING | | | |
| Gate 1 CI | PENDING | | | |
| Gate 2 Independent Benchmark | PENDING | | | |
| Gate 3 Production E2E | PENDING | | | |
| Gate 4 Security & Portability | PENDING | | | |
| Gate 5 Release Package | PENDING | | | |
- Production verdict: PENDING/NO-GO/GO
```

If a gate has no evidence or any mandatory test was skipped, mark PENDING/BLOCKED, **never** PASS. Re-run on exact source SHA after changes. A documentation-only report commit must identify its *tested* SHA separately from the *report* SHA.

## Reviewer next session

Fetch latest `main` SHA and compare to this maintenance commit; review CI job logs, new holdout report with *execution_mode*, fresh cache hashes, live-service logs and full Neo4j restore proof. Do not treat documentation claims alone as acceptance. Report separate source-handoff, supervised beta, and production verdicts.
