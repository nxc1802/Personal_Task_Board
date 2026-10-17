# Changelog

All notable changes to Personal Task Board (PTB) are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.3.0] - 2026-10-17

### Added
- **Application Domain Snapshot & Restore (DEV-P0-04)**:
  - New module `ptb_database.backup` providing zero-data-loss JSON export/restore across all 9 authoritative domain labels (`Task`, `DomainEvent`, `RawEvent`, `Checkpoint`, `Decision`, `LessonLearned`, `SyncHistory`, `SourceCredential`, `AuditLog`) and Neo4j graph relationships.
  - Cryptographic SHA-256 tamper verification for snapshots.
  - CLI commands `ptb backup --output <snapshot.json>` and `ptb restore <snapshot.json> [--drop-existing]`.
  - Recovery test suite covering REC-01 through REC-10 (`tests/test_persistence_recovery.py`).
- **Production Real End-to-End Suite (DEV-P0-03)**:
  - Complete integration test suite `tests/e2e/test_production_real_e2e.py` covering E2E-01 to E2E-12 using real pipeline components (`LLMStructuredExtractor`, `ProcessingPipeline`, `TaskDomainRepository`, `Neo4jClient`, `GraphitiMemoryClient`).
  - Zero test doubles or fake extractors in production paths. Honest socket/endpoint prereq checks skip cleanly when live services are offline.
  - Registered `production_e2e` pytest marker in `pyproject.toml`.
- **Independent Held-Out Benchmark Harness (DEV-P1-01)**:
  - Held-out evaluation dataset `PTB-INDEPENDENT-HELDOUT-30` (30 un-overfitted samples across 7 source types, 2 probe queries) with cryptographic manifest at `benchmarks/dataset/independent_heldout/`.
  - Explicit benchmark modes in `benchmarks.cli`: `fixture_regression` (Mode A), `cold_llm_eval` (Mode B), and `production_e2e_eval` (Mode C) with automatic dual-layer cold-cache bypass.
  - Benchmark manifest test coverage (`tests/benchmark_framework/test_manifest.py`).
- **Telemetry & Cost Audit Hardening (DEV-P1-04)**:
  - Added canonical `RequestAuditState` enum (`SUCCESS`, `FAILURE`, `FALLBACK`, `BYPASS`, `CACHE_HIT`, `SKIPPED`) in `ptb_contracts.telemetry`.
  - Strict separation of measured tokens vs. estimated tokens, and zero-dollar attribution for local AI models (Kev / Qwen3 / Ollama) vs. cloud API inference.
  - Subcall-level audit counters for Graphiti operations.
- **Kev Reranker & Qwen3 Auto-Wiring Hardening (DEV-P1-03)**:
  - Single-document rerank bypass (`last_call_status = "bypass"`, `last_usage_tokens = 0`) to eliminate redundant local cross-encoder overhead.
  - Vector dimension validation `Qwen3EmbeddingService.is_valid_embedding(vec, expected_dim=1024)`.
  - Cross-encoder auto-wiring telemetry in graph memory adapter.
- **Release Acceptance Dual-SHA Tracking (DEV-P1-05)**:
  - Added support for `--report-commit-sha` and `--tested-source-sha` in `scripts/generate_acceptance_report.py` and `scripts/ptb_cli.py`.
  - Added Section 5.2.1 in `RELEASE_RUNBOOK.md` for CLI domain snapshot operations.

### Changed
- **CI Matrix & Release Gate Verification (DEV-P0-01)**:
  - Updated `verify_github_ci_status` in `scripts/generate_acceptance_report.py` to enforce full 40-character SHA matching, API pagination, and verification of all 10 mandatory multi-platform jobs and steps.
  - Release Gate Guard strictly coerces overall verdict to `PENDING` unless all 5 release gates are verified `PASS`.
- **Statistical Wilson Score Calculation (DEV-P1-01)**:
  - Fixed Dimension 1 Wilson Score interval in `benchmarks/evaluator/scorecard.py`: restricted denominator to actual task cases `tot_tp / (tot_tp + tot_fn)` instead of polluting with noise negative cases (`TN`).
- **Production Container Hardening (DEV-P0-02)**:
  - Removed default passwords from `docker-compose.prod.yml`; enforced mandatory `${NEO4J_PASSWORD}` environment variable.
  - Added `host.docker.internal:host-gateway` and explicit local AI environment variables for containerized deployments.

### Fixed
- Fixed unwhitelisted silent exception handling across production components to ensure strict compliance with Anti-Fallback Guard (`tests/test_anti_fallback_guard.py` 4/4 PASS).
- Fixed newline CRLF vs. LF hashing inconsistencies in dataset manifest generators on Windows environments.

---

## [1.2.0] - 2026-10-16
- Initial release of Graphiti hybrid graph memory integration, FastMCP server, and multi-source event acquisition pipeline.
