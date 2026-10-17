# Benchmark Evaluation Report: PTB-INDEPENDENT-HELDOUT-30 (1.0.0)

- **Execution Mode:** `fixture`
- **Timestamp:** `2026-10-17T12:38:13.934121+00:00`
- **Overall Verdict:** 🔴 **FAIL**
- **Case Pass Rate:** `25/32` (78.1%)
- **Breakdown:** `PASS=25`, `FAIL=7`, `PENDING=0`, `SKIPPED=0`

> [!NOTE]
> In accordance with anti-inflation audit rules, PENDING and SKIPPED cases explicitly reduce
> the effective score denominator and NEVER contribute to PASS counts.

## 1. Multi-Dimensional Scorecard (10 Dimensions)

| # | Dimension | Primary Metric | Target Bar | Hard Gate | Measured Value | 95% Confidence Interval | Verdict |
|---|---|---|---|---|---|---|:---:|
| 1 | **Task Extraction** | Macro-F1 across active channels | ≥ 92.0% | F1 ≥ 88.0% | `100.0%` | [0.8389, 1.0000] | ✅ PASS |
| 2 | **Noise & Negation Rejection** | Noise Rejection Rate [TN / (TN + FP)] | ≥ 85.0% | FPR ≤ 10.0% (Rejection ≥ 90.0%) | `100.0% (FPR 0.0%)` | [0.7225, 1.0000] | ✅ PASS |
| 3 | **Attribution (Owner & Requester)** | Exact Canonical Persona ID Accuracy | ≥ 90.0% | Owner ≥ 85.0% | `84.6% (Owner: 100.0%)` | [0.6647, 0.9385] | ✅ PASS |
| 4 | **Deadline Extraction** | Exact UTC Timestamp Match (±0s) | ≥ 85.0% | Exact ISO Match (±0s) | `84.2% (16/19)` | [0.6243, 0.9448] | ❌ FAIL |
| 5 | **Status Inference** | State Machine Match (Strict Enum) | ≥ 90.0% | Strict Enum (No TODO=IN_PROGRESS) | `100.0% (20/20)` | [0.8389, 1.0000] | ✅ PASS |
| 6 | **Ranking & Prioritization** | Kendall's tau rank correlation | ≥ 0.85 | In-Progress #1 | `tau = 1.000 (InProg #1: PASS)` | N/A | ✅ PASS |
| 7 | **Correlation Merging** | False Merge Rate (FMR) & Missed Merge Rate (MMR) | FMR = 0.0%, MMR ≤ 5.0% | FMR == 0.0%, MMR ≤ 8.0% | `FMR: 0.0%, MMR: 0.0%` | N/A | ✅ PASS |
| 8 | **Lineage & Provenance** | Orphan Tasks Count [tasks without evidence / total] | 0 (ZERO orphans) | Lineage 100% (0 orphans) | `0 orphans / 20 tasks` | N/A | ✅ PASS |
| 9 | **Idempotency** | Replay Duplicate Count [tasks_after_replay - tasks_clean] | 0 (ZERO duplicates) | Replay duplicates == 0 | `0 duplicates created` | N/A | ✅ PASS |
| 10 | **System Reliability** | Ingestion & Pipeline p95 Latency & Probe Resilience | ≤ 1500ms, 100% Probes PASS | No Unhandled Crash / Latency ≤ 1500ms / 0 Probe Fails | `p95: 26.1ms (crashes: 0, Probes: 2/2 passed)` | N/A | ✅ PASS |

### Task Extraction Channel Breakdown

| Channel | TP | FP | FN | TN | Precision | Recall | F1 Score |
|---|---|---|---|---|---|---|---|
| `ms_teams` | 6 | 0 | 0 | 4 | 100.0% | 100.0% | **100.0%** |
| `ms_outlook` | 4 | 0 | 0 | 2 | 100.0% | 100.0% | **100.0%** |
| `slack` | 4 | 0 | 0 | 4 | 100.0% | 100.0% | **100.0%** |
| `jira` | 3 | 0 | 0 | 0 | 100.0% | 100.0% | **100.0%** |
| `github` | 3 | 0 | 0 | 0 | 100.0% | 100.0% | **100.0%** |

## 2. Per-Case Evaluation Diagnostics

| Case ID | Category | Source | Verdict | Failure Category | Diagnostic Details | Reproducible Command |
|---|---|---|:---:|---|---|---|
| `ih-01-teams-review-pr` | `direct_task` | `ms_teams` | ✅ PASS | `-` | All assertions passed. | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-01-teams-review-pr` |
| `ih-02-outlook-db-migration` | `direct_task` | `ms_outlook` | ✅ PASS | `-` | All assertions passed. | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-02-outlook-db-migration` |
| `ih-03-slack-hotfix-deploy` | `direct_task` | `slack` | ✅ PASS | `-` | All assertions passed. | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-03-slack-hotfix-deploy` |
| `ih-04-jira-security-patch` | `direct_task` | `jira` | ❌ FAIL | `deadline_mismatch` | Due date is None (expected '2026-10-28T18:00:00Z') | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-04-jira-security-patch` |
| `ih-05-github-issue-triage` | `direct_task` | `github` | ✅ PASS | `-` | All assertions passed. | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-05-github-issue-triage` |
| `ih-06-teams-benchmark-ci` | `direct_task` | `ms_teams` | ❌ FAIL | `attribution_requester` | Requester ID mismatch: expected 'lead.tran', got 'None' | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-06-teams-benchmark-ci` |
| `ih-07-outlook-client-sync` | `direct_task` | `ms_outlook` | ✅ PASS | `-` | All assertions passed. | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-07-outlook-client-sync` |
| `ih-08-slack-graphiti-audit` | `direct_task` | `slack` | ✅ PASS | `-` | All assertions passed. | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-08-slack-graphiti-audit` |
| `ih-09-teams-openwebui-sync` | `direct_task` | `ms_teams` | ❌ FAIL | `attribution_requester` | Requester ID mismatch: expected 'lead.tran', got 'None' | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-09-teams-openwebui-sync` |
| `ih-10-jira-memory-leak` | `direct_task` | `jira` | ❌ FAIL | `deadline_mismatch` | Due date is None (expected '2026-11-03T17:00:00Z') | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-10-jira-memory-leak` |
| `ih-11-github-pr-review` | `direct_task` | `github` | ✅ PASS | `-` | All assertions passed. | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-11-github-pr-review` |
| `ih-12-outlook-backup-policy` | `direct_task` | `ms_outlook` | ✅ PASS | `-` | All assertions passed. | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-12-outlook-backup-policy` |
| `ih-13-slack-rate-limit-tune` | `direct_task` | `slack` | ❌ FAIL | `attribution_requester` | Requester ID mismatch: expected 'lead.tran', got 'None' | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-13-slack-rate-limit-tune` |
| `ih-14-teams-e2e-suite` | `direct_task` | `ms_teams` | ❌ FAIL | `attribution_requester` | Requester ID mismatch: expected 'lead.tran', got 'None' | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-14-teams-e2e-suite` |
| `ih-15-jira-docker-hardening` | `direct_task` | `jira` | ❌ FAIL | `deadline_mismatch` | Due date is None (expected '2026-11-08T18:00:00Z') | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-15-jira-docker-hardening` |
| `ih-16-teams-lunch-chat` | `chitchat_noise` | `ms_teams` | ✅ PASS | `-` | All assertions passed. | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-16-teams-lunch-chat` |
| `ih-17-slack-coffee-break` | `chitchat_noise` | `slack` | ✅ PASS | `-` | All assertions passed. | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-17-slack-coffee-break` |
| `ih-18-outlook-birthday-wishes` | `chitchat_noise` | `ms_outlook` | ✅ PASS | `-` | All assertions passed. | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-18-outlook-birthday-wishes` |
| `ih-19-teams-weather-smalltalk` | `chitchat_noise` | `ms_teams` | ✅ PASS | `-` | All assertions passed. | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-19-teams-weather-smalltalk` |
| `ih-20-slack-random-meme` | `chitchat_noise` | `slack` | ✅ PASS | `-` | All assertions passed. | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-20-slack-random-meme` |
| `ih-21-teams-traffic-warning` | `chitchat_noise` | `ms_teams` | ✅ PASS | `-` | All assertions passed. | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-21-teams-traffic-warning` |
| `ih-22-outlook-out-of-office` | `chitchat_noise` | `ms_outlook` | ✅ PASS | `-` | All assertions passed. | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-22-outlook-out-of-office` |
| `ih-23-slack-thank-you` | `chitchat_noise` | `slack` | ✅ PASS | `-` | All assertions passed. | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-23-slack-thank-you` |
| `ih-24-teams-weekend-plans` | `chitchat_noise` | `ms_teams` | ✅ PASS | `-` | All assertions passed. | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-24-teams-weekend-plans` |
| `ih-25-slack-welcome-newbie` | `chitchat_noise` | `slack` | ✅ PASS | `-` | All assertions passed. | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-25-slack-welcome-newbie` |
| `ih-26-teams-quote-attribution` | `direct_task` | `ms_teams` | ✅ PASS | `-` | All assertions passed. | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-26-teams-quote-attribution` |
| `ih-27-slack-status-in-progress` | `direct_task` | `slack` | ✅ PASS | `-` | All assertions passed. | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-27-slack-status-in-progress` |
| `ih-28-github-pr-status-update` | `direct_task` | `github` | ✅ PASS | `-` | All assertions passed. | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-28-github-pr-status-update` |
| `ih-29-teams-relative-deadline` | `direct_task` | `ms_teams` | ✅ PASS | `-` | All assertions passed. | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-29-teams-relative-deadline` |
| `ih-30-outlook-decision-record` | `direct_task` | `ms_outlook` | ✅ PASS | `-` | All assertions passed. | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id ih-30-outlook-decision-record` |
| `probe-ih-01-malformed-json` | `probe_robustness` | `L1.Acquisition` | ✅ PASS | `-` | All assertions passed. | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id probe-ih-01-malformed-json` |
| `probe-ih-02-future-timestamp` | `probe_temporal` | `L2.Processing` | ✅ PASS | `-` | All assertions passed. | `python -m benchmarks.cli run --dataset-dir benchmarks\dataset\independent_heldout --case-id probe-ih-02-future-timestamp` |
