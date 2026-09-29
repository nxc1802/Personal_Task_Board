"""Unit tests for LLM Provider Readiness & Fail-Loud Processing (Wave 5 - Sub-Agent 5C).

Tests:
1. Unconfigured API key -> api_key_configured=False, status="NOT_READY", emits PTB-LLM-001.
2. Placeholder API key ("your-api-key...") -> api_key_configured=False, status="NOT_READY", emits PTB-LLM-001.
3. Invalid base_url or empty model -> status="NOT_READY", emits PTB-LLM-001.
4. Fully configured API key, base URL, model (via env or YAML) -> status="HEALTHY".
5. deep=True with mock HTTP /models success -> status="HEALTHY", deep_check_passed=True.
6. deep=True with mock HTTP /models failure (401 / connection error) -> status="DEGRADED", deep_check_passed=False, emits PTB-LLM-001.
7. ProcessingPipeline fail-loud behavior: health_status="DEGRADED" and emits PTB-LLM-001 / PTB-L2-001 without swallowing exceptions.
"""

from datetime import datetime, timezone
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch
import urllib.error

import pytest

from ptb_contracts.l1_acquisition import ProcessingStatus, RawEventRecord, SourceType
from ptb_contracts.logging import BugCode
from ptb_processing import (
    LLMExtractionError,
    LLMReadinessReport,
    LLMStructuredExtractor,
    ProcessingPipeline,
    check_llm_readiness,
)

_LLM_ENV_KEYS = (
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
    "LLM_API_KEY",
    "OPENAI_BASE_URL",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "PTB_EXTRACTION_MODEL",
    "LLM_PROVIDER",
)


def _clear_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _LLM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _make_sample_raw_event(event_id: str = "raw-llm-check-01") -> RawEventRecord:
    return RawEventRecord(
        id=event_id,
        tenant_id="tenant-wave5",
        source_type=SourceType.MS_TEAMS,
        external_id=f"ext-{event_id}",
        idempotency_key=f"idemp-{event_id}",
        author_external_id="cuong.dam@fpt.com",
        author_display_name="Dam Quang Cuong",
        conversation_or_project_id="channel-ops",
        event_timestamp=datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc),
        raw_payload={
            "body": {
                "content": "<p>Để em xử lý gấp issue OPS-500 trước 5h chiều nhé.</p>",
                "contentType": "html",
            },
            "from": {"user": {"displayName": "Dam Quang Cuong", "id": "cuong.dam@fpt.com"}},
        },
        normalized_text="Để em xử lý gấp issue OPS-500 trước 5h chiều nhé.",
        processing_status=ProcessingStatus.PENDING,
    )


@pytest.mark.asyncio
async def test_llm_readiness_missing_api_key_returns_not_ready_and_logs_ptb_llm_001(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Khi chưa cấu hình API key -> api_key_configured=False, status='NOT_READY', phát sinh PTB-LLM-001."""
    _clear_llm_env(monkeypatch)
    non_existent_cfg = str(tmp_path / "missing_models.yaml")

    with caplog.at_level(logging.WARNING, logger="ptb.bugs.llm"):
        report = await check_llm_readiness(config_path=non_existent_cfg, deep=False)

    assert isinstance(report, LLMReadinessReport)
    assert report.api_key_configured is False
    assert report.status == "NOT_READY"
    assert report.deep_check_passed is None
    assert report.error_message is not None
    assert "API key" in report.error_message

    assert any("PTB-LLM-001" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_llm_readiness_placeholder_api_key_rejected(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """API key dạng placeholder 'your-api-key...' bị coi là chưa cấu hình."""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "your-api-key-here")
    non_existent_cfg = str(tmp_path / "missing_models.yaml")

    with caplog.at_level(logging.WARNING, logger="ptb.bugs.llm"):
        report = await check_llm_readiness(config_path=non_existent_cfg, deep=False)

    assert report.api_key_configured is False
    assert report.status == "NOT_READY"
    assert any("PTB-LLM-001" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_llm_readiness_invalid_base_url_or_empty_model(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Khi base_url không hợp lệ hoặc model rỗng -> status='NOT_READY', phát sinh PTB-LLM-001."""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-valid-key-12345")
    monkeypatch.setenv("OPENAI_BASE_URL", "ftp://invalid-endpoint.local/v1")
    monkeypatch.setenv("LLM_MODEL", "")
    non_existent_cfg = str(tmp_path / "missing_models.yaml")

    with caplog.at_level(logging.WARNING, logger="ptb.bugs.llm"):
        report = await check_llm_readiness(config_path=non_existent_cfg, deep=False)

    assert report.api_key_configured is True
    assert report.base_url_valid is False
    assert report.model_configured is False
    assert report.status == "NOT_READY"
    assert report.error_message is not None
    assert any("PTB-LLM-001" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_llm_readiness_healthy_when_fully_configured(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Khi cấu hình đầy đủ API key, base URL, model -> status='HEALTHY'."""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live-valid-token-999")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")
    non_existent_cfg = str(tmp_path / "missing_models.yaml")

    with caplog.at_level(logging.WARNING, logger="ptb.bugs.llm"):
        report = await check_llm_readiness(config_path=non_existent_cfg, deep=False)

    assert report.status == "HEALTHY"
    assert report.api_key_configured is True
    assert report.base_url_valid is True
    assert report.model_configured is True
    assert report.base_url == "https://api.openai.com/v1"
    assert report.model_name == "gpt-4o-mini"
    assert report.deep_check_passed is None
    assert report.error_message is None
    assert not any("PTB-LLM-001" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_llm_readiness_reads_from_yaml_config_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Đọc cấu hình từ file models.yaml khi biến môi trường không đặt trực tiếp."""
    _clear_llm_env(monkeypatch)
    cfg_file = tmp_path / "models.yaml"
    cfg_file.write_text(
        """
default_llm_provider: "openai_compatible"
providers:
  openai_compatible:
    base_url: "https://gateway.internal.example/v1"
    api_key: "sk-yaml-configured-key"
    extraction_model: "qwen2.5-coder:7b"
""",
        encoding="utf-8",
    )

    report = await check_llm_readiness(config_path=str(cfg_file), deep=False)
    assert report.status == "HEALTHY"
    assert report.api_key_configured is True
    assert report.base_url_valid is True
    assert report.model_configured is True
    assert report.provider == "openai_compatible"
    assert report.base_url == "https://gateway.internal.example/v1"
    assert report.model_name == "qwen2.5-coder:7b"


@pytest.mark.asyncio
async def test_llm_readiness_deep_check_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test deep=True với mock HTTP /models thành công -> deep_check_passed=True, status='HEALTHY'."""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "sk-deep-ok-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")
    non_existent_cfg = str(tmp_path / "missing_models.yaml")

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.read.return_value = b'{"object": "list", "data": [{"id": "gpt-4o-mini"}]}'
    mock_response.__enter__.return_value = mock_response
    mock_response.__exit__.return_value = None

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
        report = await check_llm_readiness(config_path=non_existent_cfg, deep=True)

    assert report.status == "HEALTHY"
    assert report.deep_check_passed is True
    assert report.error_message is None
    assert mock_urlopen.call_count == 1
    called_req = mock_urlopen.call_args.args[0]
    assert called_req.full_url == "https://api.openai.com/v1/models"


@pytest.mark.asyncio
async def test_llm_readiness_deep_check_failure_logs_ptb_llm_001_and_degrades(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Test deep=True với mock HTTP /models thất bại (401/network) -> status='DEGRADED', deep_check_passed=False, phát sinh PTB-LLM-001."""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-invalid-remote-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")
    non_existent_cfg = str(tmp_path / "missing_models.yaml")

    http_401 = urllib.error.HTTPError(
        url="https://api.openai.com/v1/models",
        code=401,
        msg="Unauthorized",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )

    with patch("urllib.request.urlopen", side_effect=http_401):
        with caplog.at_level(logging.WARNING, logger="ptb.bugs.llm"):
            report = await check_llm_readiness(config_path=non_existent_cfg, deep=True)

    assert report.status == "DEGRADED"
    assert report.api_key_configured is True
    assert report.base_url_valid is True
    assert report.model_configured is True
    assert report.deep_check_passed is False
    assert report.error_message is not None
    assert "401" in report.error_message or "Unauthorized" in report.error_message
    assert any("PTB-LLM-001" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_pipeline_fail_loud_on_llm_error_sets_degraded_and_logs_bug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Khi ProcessingPipeline gặp lỗi từ LLM extractor -> log PTB-LLM-001, health_status='DEGRADED', không nuốt lỗi."""
    extractor = LLMStructuredExtractor(
        api_key="",
        mock_mode=False,
        allow_heuristic_fallback=False,
    )
    pipeline = ProcessingPipeline(llm_extractor=extractor)
    assert pipeline.health_status == "HEALTHY"

    raw_event = _make_sample_raw_event("raw-llm-fail-loud-01")

    with caplog.at_level(logging.ERROR, logger="ptb.bugs.llm"):
        with pytest.raises(LLMExtractionError):
            await pipeline.process_raw_event(raw_event)

    assert pipeline.health_status == "DEGRADED"
    assert any(BugCode.PTB_LLM_001.value in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_pipeline_fail_loud_on_general_step_error_logs_ptb_l2_001(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Khi ProcessingPipeline gặp lỗi ở bước xử lý khác -> log PTB-L2-001, health_status='DEGRADED', không nuốt lỗi."""
    extractor = LLMStructuredExtractor(mock_mode=True)
    failing_repo = MagicMock()
    failing_repo.get_active_tasks.side_effect = RuntimeError("Database connection dropped mid-pipeline")

    pipeline = ProcessingPipeline(
        task_repo=failing_repo,
        llm_extractor=extractor,
    )
    assert pipeline.health_status == "HEALTHY"

    raw_event = _make_sample_raw_event("raw-step-fail-loud-02")

    with caplog.at_level(logging.ERROR, logger="ptb.bugs.processing"):
        with pytest.raises(RuntimeError, match="Database connection dropped mid-pipeline"):
            await pipeline.process(raw_event)

    assert pipeline.health_status == "DEGRADED"
    assert any(BugCode.PTB_L2_001.value in rec.message for rec in caplog.records)
