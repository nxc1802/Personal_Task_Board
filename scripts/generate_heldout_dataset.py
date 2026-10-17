"""Script to generate independent held-out benchmark dataset (PTB-INDEPENDENT-HELDOUT-30).

Creates 30 held-out samples with independent ground truth across MS Teams, Slack, Outlook,
Jira, and GitHub with immutable SHA-256 manifest.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

TARGET_DIR = Path("benchmarks/dataset/independent_heldout")
TARGET_DIR.mkdir(parents=True, exist_ok=True)

samples = []

# Personas
P_CUONG = "cuong.dam"
P_LEAD = "lead.tran"
P_HUY = "huy.nguyen"
P_MAI = "mai.le"

# 15 Direct Tasks (Teams, Slack, Outlook, Jira, GitHub)
task_definitions = [
    (
        "ih-01-teams-review-pr",
        "ms_teams",
        "Dam Quang Cuong",
        "cuong.dam@company.com",
        "Vâng anh Lead, để em review PR-201 trước 17:00 ngày 2026-10-25 nhé.",
        "Em sẽ review PR-201 trước 17:00 ngày 2026-10-25.",
        ["review", "PR-201"],
        P_CUONG,
        P_LEAD,
        "2026-10-25T17:00:00Z",
        True,
        "TODO",
    ),
    (
        "ih-02-outlook-db-migration",
        "ms_outlook",
        "Nguyen Van Huy",
        "huy.nguyen@company.com",
        "Tôi sẽ hoàn thành script migration cơ sở dữ liệu Neo4j trước 12:00 ngày 2026-10-26.",
        "Sẽ hoàn thành migration Neo4j trước 12:00 ngày 2026-10-26.",
        ["migration", "Neo4j"],
        P_HUY,
        None,
        "2026-10-26T12:00:00Z",
        True,
        "TODO",
    ),
    (
        "ih-03-slack-hotfix-deploy",
        "slack",
        "Mai Le",
        "mai.le@company.com",
        "Mình sẽ deploy hotfix v1.3.1 lên staging vào lúc 16:00 ngày 2026-10-27.",
        "Deploy hotfix v1.3.1 lên staging lúc 16:00 ngày 2026-10-27.",
        ["deploy", "hotfix"],
        P_MAI,
        None,
        "2026-10-27T16:00:00Z",
        True,
        "TODO",
    ),
    (
        "ih-04-jira-security-patch",
        "jira",
        "Tran Van Lead",
        "lead.tran@company.com",
        "[SEC-402] Patch authentication vulnerability in FastMCP server endpoint",
        "[SEC-402] Patch authentication vulnerability in FastMCP",
        ["SEC-402", "FastMCP"],
        P_LEAD,
        None,
        "2026-10-28T18:00:00Z",
        True,
        "TODO",
    ),
    (
        "ih-05-github-issue-triage",
        "github",
        "Dam Quang Cuong",
        "cuong.dam@company.com",
        "Em sẽ kiểm tra issue #88 và viết test case bổ sung trước 10:00 ngày 2026-10-29.",
        "Kiểm tra issue #88 và viết test bổ sung trước 10:00.",
        ["issue", "#88"],
        P_CUONG,
        None,
        "2026-10-29T10:00:00Z",
        True,
        "TODO",
    ),
    (
        "ih-06-teams-benchmark-ci",
        "ms_teams",
        "Mai Le",
        "mai.le@company.com",
        "Để em tối ưu benchmark CI execution time trước 15:00 ngày 2026-10-30.",
        "Tối ưu benchmark CI execution time trước 15:00.",
        ["benchmark", "CI"],
        P_MAI,
        P_LEAD,
        "2026-10-30T15:00:00Z",
        True,
        "TODO",
    ),
    (
        "ih-07-outlook-client-sync",
        "ms_outlook",
        "Tran Van Lead",
        "lead.tran@company.com",
        "Anh sẽ cập nhật tài liệu API integration cho phía khách hàng trước 17:00 ngày 2026-10-31.",
        "Cập nhật tài liệu API integration trước 17:00 ngày 2026-10-31.",
        ["tài liệu", "API"],
        P_LEAD,
        None,
        "2026-10-31T17:00:00Z",
        True,
        "TODO",
    ),
    (
        "ih-08-slack-graphiti-audit",
        "slack",
        "Nguyen Van Huy",
        "huy.nguyen@company.com",
        "Mình sẽ kiểm tra toàn bộ telemetry log của Graphiti trước 11:00 ngày 2026-11-01.",
        "Kiểm tra telemetry log Graphiti trước 11:00 ngày 2026-11-01.",
        ["telemetry", "Graphiti"],
        P_HUY,
        None,
        "2026-11-01T11:00:00Z",
        True,
        "TODO",
    ),
    (
        "ih-09-teams-openwebui-sync",
        "ms_teams",
        "Dam Quang Cuong",
        "cuong.dam@company.com",
        "Em sẽ tích hợp tool OpenWebUI mới vào hệ thống trước 16:30 ngày 2026-11-02.",
        "Tích hợp tool OpenWebUI mới trước 16:30 ngày 2026-11-02.",
        ["OpenWebUI", "tool"],
        P_CUONG,
        P_LEAD,
        "2026-11-02T16:30:00Z",
        True,
        "TODO",
    ),
    (
        "ih-10-jira-memory-leak",
        "jira",
        "Nguyen Van Huy",
        "huy.nguyen@company.com",
        "[PERF-105] Resolve memory consumption spike during batch extraction sweep",
        "[PERF-105] Resolve memory consumption spike",
        ["PERF-105", "memory"],
        P_HUY,
        None,
        "2026-11-03T17:00:00Z",
        True,
        "TODO",
    ),
    (
        "ih-11-github-pr-review",
        "github",
        "Mai Le",
        "mai.le@company.com",
        "Mình sẽ approve và merge PR-205 sau khi CI xanh trước 14:00 ngày 2026-11-04.",
        "Merge PR-205 sau khi CI xanh trước 14:00.",
        ["merge", "PR-205"],
        P_MAI,
        None,
        "2026-11-04T14:00:00Z",
        True,
        "TODO",
    ),
    (
        "ih-12-outlook-backup-policy",
        "ms_outlook",
        "Tran Van Lead",
        "lead.tran@company.com",
        "Tôi sẽ chạy bài test phục hồi từ snapshot sao lưu trước 18:00 ngày 2026-11-05.",
        "Chạy bài test phục hồi từ snapshot trước 18:00 ngày 2026-11-05.",
        ["test", "snapshot"],
        P_LEAD,
        None,
        "2026-11-05T18:00:00Z",
        True,
        "TODO",
    ),
    (
        "ih-13-slack-rate-limit-tune",
        "slack",
        "Dam Quang Cuong",
        "cuong.dam@company.com",
        "Để em tinh chỉnh rate limit của Jira adapter trước 15:00 ngày 2026-11-06.",
        "Tinh chỉnh rate limit Jira adapter trước 15:00 ngày 2026-11-06.",
        ["rate limit", "Jira"],
        P_CUONG,
        P_LEAD,
        "2026-11-06T15:00:00Z",
        True,
        "TODO",
    ),
    (
        "ih-14-teams-e2e-suite",
        "ms_teams",
        "Mai Le",
        "mai.le@company.com",
        "Em sẽ bổ sung 5 test case E2E cho luồng Microsoft 365 trước 17:00 ngày 2026-11-07.",
        "Bổ sung 5 test case E2E trước 17:00 ngày 2026-11-07.",
        ["test case", "E2E"],
        P_MAI,
        P_LEAD,
        "2026-11-07T17:00:00Z",
        True,
        "TODO",
    ),
    (
        "ih-15-jira-docker-hardening",
        "jira",
        "Nguyen Van Huy",
        "huy.nguyen@company.com",
        "[OPS-330] Enforce non-root execution and secret masking in docker-compose.prod.yml",
        "[OPS-330] Enforce non-root execution in production container",
        ["OPS-330", "production"],
        P_HUY,
        None,
        "2026-11-08T18:00:00Z",
        True,
        "TODO",
    ),
]

for idx, (sid, src, author, author_email, content, norm, kw, owner, req, due, explicit, status) in enumerate(task_definitions, start=1):
    samples.append({
        "sample_id": sid,
        "case_category": "direct_task",
        "origin_reference": f"independent://{src}/{idx:02d}",
        "raw_event": {
            "id": f"raw-{sid}",
            "tenant_id": "tenant-fpt-internal",
            "source_type": src,
            "external_id": f"ext-{sid}",
            "conversation_or_project_id": f"conv-{src}-main",
            "event_timestamp": "2026-10-17T08:00:00Z",
            "captured_at": "2026-10-17T08:00:05Z",
            "author_external_id": author_email,
            "author_display_name": author,
            "raw_payload": {"content": content},
            "normalized_text": norm,
            "idempotency_key": f"idemp-{sid}",
        },
        "ground_truth": {
            "expected_is_task": True,
            "clarity": "CLEAR",
            "control_type": "COMMITMENT",
            "expected_title_keywords": kw,
            "expected_owner_id": owner,
            "expected_owner_name": author if owner == P_CUONG else None,
            "expected_requester_id": req,
            "expected_due_date": due,
            "expected_explicit_deadline": explicit,
            "expected_status": status,
            "expected_review_status": "auto_approved",
            "correlation": {
                "expected_merge": False,
                "expected_anchors": kw,
            },
            "evidence": {
                "source_event_id": f"raw-{sid}",
                "expected_snippet_substrings": [kw[0]],
            },
        },
    })

# 10 Non-Work Noise / Chitchat Cases (0 tasks created)
noise_definitions = [
    ("ih-16-teams-lunch-chat", "ms_teams", "Trưa nay mọi người ăn gì? Đi bún chả hay cơm tấm?"),
    ("ih-17-slack-coffee-break", "slack", "Ai order cà phê chiều không, mình đặt chung nhé."),
    ("ih-18-outlook-birthday-wishes", "ms_outlook", "Chúc mừng sinh nhật Mai! Chúc bạn tuổi mới nhiều niềm vui."),
    ("ih-19-teams-weather-smalltalk", "ms_teams", "Hà Nội hôm nay mưa to quá, mọi người đi làm cẩn thận."),
    ("ih-20-slack-random-meme", "slack", "Xem cái meme này hài vãi mọi người ơi :joy:"),
    ("ih-21-teams-traffic-warning", "ms_teams", "Đoạn đường Cầu Giấy đang tắc cứng, ai đi qua né ra nhé."),
    ("ih-22-outlook-out-of-office", "ms_outlook", "Tôi đang nghỉ phép từ 17/10 đến 20/10. Khẩn cấp xin liên hệ qua số điện thoại."),
    ("ih-23-slack-thank-you", "slack", "Cảm ơn cả team vì đã phối hợp rất mượt mà trong sprint vừa rồi!"),
    ("ih-24-teams-weekend-plans", "ms_teams", "Cuối tuần này có ai đi đá bóng không mọi người?"),
    ("ih-25-slack-welcome-newbie", "slack", "Chào mừng bạn Huy gia nhập engineering team PTB!"),
]

for idx, (sid, src, content) in enumerate(noise_definitions, start=16):
    samples.append({
        "sample_id": sid,
        "case_category": "chitchat_noise",
        "origin_reference": f"independent://{src}/{idx:02d}",
        "raw_event": {
            "id": f"raw-{sid}",
            "tenant_id": "tenant-fpt-internal",
            "source_type": src,
            "external_id": f"ext-{sid}",
            "conversation_or_project_id": f"conv-{src}-random",
            "event_timestamp": "2026-10-17T08:30:00Z",
            "captured_at": "2026-10-17T08:30:05Z",
            "author_external_id": "user@company.com",
            "author_display_name": "Teammate",
            "raw_payload": {"content": content},
            "normalized_text": content,
            "idempotency_key": f"idemp-{sid}",
        },
        "ground_truth": {
            "expected_is_task": False,
            "clarity": "CLEAR",
            "control_type": "NONE",
            "expected_title_keywords": [],
            "expected_owner_id": None,
            "expected_owner_name": None,
            "expected_requester_id": None,
            "expected_due_date": None,
            "expected_explicit_deadline": False,
            "expected_status": None,
            "expected_review_status": "ignore",
            "correlation": {
                "expected_merge": False,
                "expected_anchors": [],
            },
            "evidence": {
                "source_event_id": f"raw-{sid}",
                "expected_snippet_substrings": [],
            },
        },
    })

# 5 Multi-Person Quote/Reply & Progress Updates
edge_definitions = [
    (
        "ih-26-teams-quote-attribution",
        "ms_teams",
        "Dam Quang Cuong",
        "cuong.dam@company.com",
        "<blockquote><span itemid=\"lead.tran\">Tran Van Lead</span>: Nhờ Cường fix bug timeout ở search engine trước 16:00 ngày 2026-11-09</blockquote>Vâng anh, em sẽ fix bug timeout này trước 16:00.",
        "Em sẽ fix bug timeout này trước 16:00 ngày 2026-11-09.",
        ["timeout", "search engine"],
        P_CUONG,
        P_LEAD,
        "2026-11-09T16:00:00Z",
        True,
        "TODO",
    ),
    (
        "ih-27-slack-status-in-progress",
        "slack",
        "Mai Le",
        "mai.le@company.com",
        "Mình đang bắt tay vào refactor adapter Outlook Web, dự kiến xong trước 17:00 ngày 2026-11-10.",
        "Đang xử lý refactor adapter Outlook Web trước 17:00.",
        ["refactor", "Outlook"],
        P_MAI,
        None,
        "2026-11-10T17:00:00Z",
        True,
        "IN_PROGRESS",
    ),
    (
        "ih-28-github-pr-status-update",
        "github",
        "Nguyen Van Huy",
        "huy.nguyen@company.com",
        "Đã hoàn thành merge PR-220 và deploy lên cluster production ngày 2026-11-11.",
        "Đã xong merge PR-220 và deploy production.",
        ["merge", "PR-220"],
        P_HUY,
        None,
        "2026-11-11T18:00:00Z",
        True,
        "DONE",
    ),
    (
        "ih-29-teams-relative-deadline",
        "ms_teams",
        "Tran Van Lead",
        "lead.tran@company.com",
        "Anh sẽ chuẩn bị slide báo cáo quý 4 trước 17:00 ngày 2026-11-12.",
        "Chuẩn bị slide báo cáo quý 4 trước 17:00 ngày 2026-11-12.",
        ["slide", "báo cáo"],
        P_LEAD,
        None,
        "2026-11-12T17:00:00Z",
        True,
        "TODO",
    ),
    (
        "ih-30-outlook-decision-record",
        "ms_outlook",
        "Dam Quang Cuong",
        "cuong.dam@company.com",
        "Quyết định chọn Neo4j 5.26 LTS làm nền tảng đồ thị chính thức cho PTB v1.3.",
        "Quyết định chọn Neo4j 5.26 LTS làm nền tảng đồ thị chính thức.",
        ["Neo4j", "đồ thị"],
        P_CUONG,
        None,
        None,
        False,
        "TODO",
    ),
]

for idx, (sid, src, author, author_email, content, norm, kw, owner, req, due, explicit, status) in enumerate(edge_definitions, start=26):
    samples.append({
        "sample_id": sid,
        "case_category": "direct_task",
        "origin_reference": f"independent://{src}/{idx:02d}",
        "raw_event": {
            "id": f"raw-{sid}",
            "tenant_id": "tenant-fpt-internal",
            "source_type": src,
            "external_id": f"ext-{sid}",
            "conversation_or_project_id": f"conv-{src}-main",
            "event_timestamp": "2026-10-17T09:00:00Z",
            "captured_at": "2026-10-17T09:00:05Z",
            "author_external_id": author_email,
            "author_display_name": author,
            "raw_payload": {"content": content},
            "normalized_text": norm,
            "idempotency_key": f"idemp-{sid}",
        },
        "ground_truth": {
            "expected_is_task": True,
            "clarity": "CLEAR",
            "control_type": "COMMITMENT",
            "expected_title_keywords": kw,
            "expected_owner_id": owner,
            "expected_owner_name": author if owner == P_CUONG else None,
            "expected_requester_id": req,
            "expected_due_date": due,
            "expected_explicit_deadline": explicit,
            "expected_status": status,
            "expected_review_status": "auto_approved",
            "correlation": {
                "expected_merge": False,
                "expected_anchors": kw,
            },
            "evidence": {
                "source_event_id": f"raw-{sid}",
                "expected_snippet_substrings": [kw[0]],
            },
        },
    })

# Probes
probes = [
    {
        "probe_id": "probe-ih-01-malformed-json",
        "probe_group": "robustness",
        "name": "Malformed JSON payload recovery",
        "target_module": "L1.Acquisition",
        "fault_injection": "Set raw_payload to unparseable corrupted string",
        "expected_behavior": "Pipeline assigns normalized_text without unhandled crash",
        "expected_state_or_status": "PROCESSED",
        "origin_reference": "independent://probes/01",
    },
    {
        "probe_id": "probe-ih-02-future-timestamp",
        "probe_group": "temporal",
        "name": "Clock skew tolerance",
        "target_module": "L2.Processing",
        "fault_injection": "Set event_timestamp 24 hours into the future",
        "expected_behavior": "Processing accepts event without discarding",
        "expected_state_or_status": "PROCESSED",
        "origin_reference": "independent://probes/02",
    },
]

# Write samples and probes
samples_json = json.dumps(samples, indent=2, ensure_ascii=False)
probes_json = json.dumps(probes, indent=2, ensure_ascii=False)

samples_file = TARGET_DIR / "samples.json"
probes_file = TARGET_DIR / "probes.json"

samples_file.write_text(samples_json, encoding="utf-8")
probes_file.write_text(probes_json, encoding="utf-8")

from benchmarks.manifest import compute_file_sha256

samples_sha = compute_file_sha256(samples_file)
probes_sha = compute_file_sha256(probes_file)

samples_bytes = samples_file.read_bytes().replace(b"\r\n", b"\n")
probes_bytes = probes_file.read_bytes().replace(b"\r\n", b"\n")

manifest = {
    "dataset_name": "PTB-INDEPENDENT-HELDOUT-30",
    "version": "1.0.0",
    "commit_baseline": "893de9ad8bd4be53587301dd3d7455f9345ca6a7",
    "created_at": "2026-10-17T12:00:00Z",
    "evaluation_now": "2026-10-17T12:00:00Z",
    "samples_sha256": samples_sha,
    "probes_sha256": probes_sha,
    "dataset_files": {
        "samples.json": {
            "sha256": samples_sha,
            "size_bytes": len(samples_bytes),
            "item_count": len(samples),
        },
        "probes.json": {
            "sha256": probes_sha,
            "size_bytes": len(probes_bytes),
            "item_count": len(probes),
        },
    },
    "allowed_email_domains": ["company.com", "fpt.com"],
    "supported_controls": ["COMMITMENT", "NONE", "DECISION", "LESSON"],
    "personas": [
        {
            "canonical_id": "cuong.dam",
            "canonical_name": "Dam Quang Cuong",
            "email": "cuong.dam@company.com",
            "account_ids": ["cuong.dam", "cuong.dam_jira"],
            "aliases": ["Cuong", "anh Cường", "anh Cuong"],
            "tenant_id": "tenant-fpt-internal",
            "is_current_user": True,
        },
        {
            "canonical_id": "lead.tran",
            "canonical_name": "Tran Van Lead",
            "email": "lead.tran@company.com",
            "account_ids": ["lead.tran", "lead.tran_jira"],
            "aliases": ["Lead", "anh Lead", "Tran Van Lead"],
            "tenant_id": "tenant-fpt-internal",
            "is_current_user": False,
        },
        {
            "canonical_id": "huy.nguyen",
            "canonical_name": "Nguyen Van Huy",
            "email": "huy.nguyen@company.com",
            "account_ids": ["huy.nguyen"],
            "aliases": ["Huy", "anh Huy"],
            "tenant_id": "tenant-fpt-internal",
            "is_current_user": False,
        },
        {
            "canonical_id": "mai.le",
            "canonical_name": "Mai Le",
            "email": "mai.le@company.com",
            "account_ids": ["mai.le"],
            "aliases": ["Mai", "chị Mai"],
            "tenant_id": "tenant-fpt-internal",
            "is_current_user": False,
        },
    ],
}

manifest_file = TARGET_DIR / "manifest.json"
manifest_file.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"[+] Successfully generated held-out dataset at: {TARGET_DIR}")
print(f"    - Samples : {len(samples)} items (SHA-256: {samples_sha[:16]}...)")
print(f"    - Probes  : {len(probes)} items (SHA-256: {probes_sha[:16]}...)")
print(f"    - Manifest: {manifest_file}")
