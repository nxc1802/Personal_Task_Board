"""LLM Provider Readiness & Health Check Module.

Kiểm tra tính sẵn sàng của LLM provider theo tiêu chuẩn Wave 5 (docs/v1_2.md):
- Đọc cấu hình từ `config/models.yaml` (nếu có) và các biến môi trường.
- Kiểm tra API key, base URL, và model name.
- Hỗ trợ kiểm tra sâu (`deep=True`) gọi tới endpoint `/models` với timeout ngắn (3s).
- Phát sinh structured bug log `PTB-LLM-001` khi chưa cấu hình hoặc kết nối thất bại.
"""

import asyncio
from dataclasses import asdict, dataclass
import os
from pathlib import Path
import re
from typing import Any, Dict, Optional
import urllib.error
import urllib.parse
import urllib.request

from ptb_contracts.logging import BugCode, log_bug

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore

_ENV_VAR_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}$")

_INVALID_API_KEY_PREFIXES = (
    "your-api-key",
    "your_api_key",
    "<your-api-key",
    "sk-your-api-key",
    "replace-with-your",
)

_INVALID_API_KEY_VALUES = {
    "changeme",
    "placeholder",
    "none",
    "null",
    "todo",
    "your-key-here",
    "xxx",
}


@dataclass
class LLMReadinessReport:
    """Báo cáo trạng thái sẵn sàng của LLM Provider."""

    status: str  # "HEALTHY", "DEGRADED", "NOT_READY"
    api_key_configured: bool
    base_url_valid: bool
    model_configured: bool
    provider: str = "openai_compatible"
    model_name: str = ""
    base_url: str = ""
    deep_check_passed: Optional[bool] = None
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Chuyển đổi báo cáo sang dictionary."""
        return asdict(self)

    def model_dump(self, **kwargs: Any) -> Dict[str, Any]:
        """Helper tương thích với giao diện Pydantic model_dump."""
        return asdict(self)


def _expand_yaml_env_str(value: Any) -> str:
    """Giải mã biểu thức ${VAR:-default} hoặc ${VAR} trong chuỗi cấu hình YAML."""
    if not isinstance(value, str):
        return ""
    raw = value.strip()
    match = _ENV_VAR_PATTERN.match(raw)
    if match:
        var_name = match.group(1)
        default_val = match.group(2) if match.group(2) is not None else ""
        env_val = os.getenv(var_name)
        if env_val is not None and env_val != "":
            return env_val.strip()
        return default_val.strip()
    return raw


def _is_valid_api_key(key: Optional[str]) -> bool:
    """Kiểm tra API key có hợp lệ (không rỗng và không phải chuỗi placeholder)."""
    if not key or not isinstance(key, str):
        return False
    cleaned = key.strip()
    if not cleaned:
        return False
    lower = cleaned.lower()
    if lower.startswith(_INVALID_API_KEY_PREFIXES):
        return False
    if lower in _INVALID_API_KEY_VALUES:
        return False
    return True


def _is_valid_base_url(url: Optional[str]) -> bool:
    """Kiểm tra URL hợp lệ (bắt đầu bằng http:// hoặc https:// và có host)."""
    if not url or not isinstance(url, str):
        return False
    cleaned = url.strip()
    if not (cleaned.startswith("http://") or cleaned.startswith("https://")):
        return False
    parsed = urllib.parse.urlparse(cleaned)
    return bool(parsed.scheme in ("http", "https") and parsed.netloc)


def _load_yaml_config(config_path: Optional[str]) -> Dict[str, Any]:
    """Đọc file cấu hình YAML nếu tồn tại."""
    candidate_path = Path(config_path) if config_path else Path("config/models.yaml")
    if not candidate_path.is_file() or yaml is None:
        return {}
    try:
        content = candidate_path.read_text(encoding="utf-8")
        loaded = yaml.safe_load(content)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _probe_models_endpoint(base_url: str, api_key: str, timeout: float = 3.0) -> None:
    """Gửi HTTP GET tới endpoint /models của base_url để kiểm tra kết nối thực tế."""
    models_url = f"{base_url.rstrip('/')}/models"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    req = urllib.request.Request(models_url, headers=headers, method="GET")
    raw_resp = urllib.request.urlopen(req, timeout=timeout)
    resp = raw_resp.__enter__() if hasattr(raw_resp, "__enter__") else raw_resp
    try:
        status_code = getattr(resp, "status", None)
        if not isinstance(status_code, int):
            getcode_fn = getattr(resp, "getcode", None)
            if callable(getcode_fn):
                code_val = getcode_fn()
                if isinstance(code_val, int):
                    status_code = code_val
        if isinstance(status_code, int) and status_code >= 400:
            raise RuntimeError(f"HTTP {status_code} returned from {models_url}")
        if hasattr(resp, "read"):
            resp.read()
    finally:
        if hasattr(raw_resp, "__exit__"):
            raw_resp.__exit__(None, None, None)
        elif hasattr(raw_resp, "close"):
            raw_resp.close()


async def check_llm_readiness(
    config_path: Optional[str] = None,
    deep: bool = False,
) -> LLMReadinessReport:
    """Kiểm tra cấu hình và trạng thái kết nối của LLM provider.

    Args:
        config_path: Đường dẫn tới file cấu hình YAML (mặc định kiểm tra `config/models.yaml`).
        deep: Nếu True và cấu hình cơ bản hợp lệ, thực hiện gọi HTTP GET `/models` (timeout 3s).

    Returns:
        LLMReadinessReport chứa chi tiết trạng thái sẵn sàng.
    """
    yaml_data = _load_yaml_config(config_path)

    # 1. Xác định provider
    provider = (
        os.getenv("LLM_PROVIDER")
        or yaml_data.get("default_llm_provider")
        or yaml_data.get("provider")
        or ""
    ).strip()

    providers_map = yaml_data.get("providers")
    provider_cfg: Dict[str, Any] = {}
    if isinstance(providers_map, dict):
        target_prov = provider or "openai_compatible"
        raw_prov_cfg = providers_map.get(target_prov)
        if isinstance(raw_prov_cfg, dict):
            provider_cfg = raw_prov_cfg

    # 2. Thu thập và kiểm tra API keys từ biến môi trường và file cấu hình
    env_key_candidates = [
        ("openai", os.getenv("OPENAI_API_KEY")),
        ("gemini", os.getenv("GEMINI_API_KEY")),
        ("anthropic", os.getenv("ANTHROPIC_API_KEY")),
        ("openai_compatible", os.getenv("LLM_API_KEY")),
    ]
    yaml_raw_key = provider_cfg.get("api_key") if "api_key" in provider_cfg else yaml_data.get("api_key")
    yaml_api_key = _expand_yaml_env_str(yaml_raw_key) if yaml_raw_key is not None else ""

    resolved_api_key = ""
    inferred_provider = ""
    for prov_label, key_val in env_key_candidates:
        if _is_valid_api_key(key_val):
            resolved_api_key = key_val.strip()  # type: ignore[union-attr]
            inferred_provider = prov_label
            break

    if not resolved_api_key and _is_valid_api_key(yaml_api_key):
        resolved_api_key = yaml_api_key

    if not provider:
        provider = inferred_provider or "openai_compatible"

    api_key_configured = bool(resolved_api_key)

    # 3. Xác định và kiểm tra base_url
    yaml_has_base_url = "base_url" in provider_cfg or "base_url" in yaml_data
    yaml_base_url = _expand_yaml_env_str(
        provider_cfg.get("base_url") if "base_url" in provider_cfg else yaml_data.get("base_url")
    )

    if "OPENAI_BASE_URL" in os.environ:
        base_url = os.environ["OPENAI_BASE_URL"].strip()
    elif "LLM_BASE_URL" in os.environ:
        base_url = os.environ["LLM_BASE_URL"].strip()
    elif yaml_has_base_url:
        base_url = yaml_base_url
    else:
        base_url = "https://api.openai.com/v1"

    base_url_valid = _is_valid_base_url(base_url)

    # 4. Xác định và kiểm tra model_name
    yaml_has_model = any(
        k in provider_cfg for k in ("extraction_model", "model", "model_name", "planning_model")
    ) or any(k in yaml_data for k in ("extraction_model", "model", "model_name"))

    raw_yaml_model = (
        provider_cfg.get("extraction_model")
        if "extraction_model" in provider_cfg
        else provider_cfg.get("model")
        if "model" in provider_cfg
        else provider_cfg.get("model_name")
        if "model_name" in provider_cfg
        else provider_cfg.get("planning_model")
        if "planning_model" in provider_cfg
        else yaml_data.get("extraction_model")
        if "extraction_model" in yaml_data
        else yaml_data.get("model")
        if "model" in yaml_data
        else yaml_data.get("model_name")
    )
    yaml_model = _expand_yaml_env_str(raw_yaml_model)

    if "LLM_MODEL" in os.environ:
        model_name = os.environ["LLM_MODEL"].strip()
    elif "PTB_EXTRACTION_MODEL" in os.environ:
        model_name = os.environ["PTB_EXTRACTION_MODEL"].strip()
    elif yaml_has_model:
        model_name = yaml_model
    else:
        model_name = "gpt-4o-mini"

    model_configured = bool(model_name and model_name.strip())

    # 5. Đánh giá tính hợp lệ của cấu hình cơ bản
    missing_reasons = []
    if not api_key_configured:
        missing_reasons.append("missing or invalid API key")
    if not base_url_valid:
        missing_reasons.append(f"invalid base_url '{base_url}'")
    if not model_configured:
        missing_reasons.append("missing model name")

    if missing_reasons:
        error_msg = f"LLM provider unconfigured or unavailable ({', '.join(missing_reasons)})"
        log_bug(
            BugCode.PTB_LLM_001,
            subsystem="llm",
            severity="WARNING",
            message="LLM provider unconfigured or unavailable",
            context={
                "provider": provider,
                "base_url": base_url,
                "model_name": model_name,
                "api_key_configured": api_key_configured,
                "base_url_valid": base_url_valid,
                "model_configured": model_configured,
                "reasons": missing_reasons,
            },
        )
        return LLMReadinessReport(
            status="NOT_READY",
            api_key_configured=api_key_configured,
            base_url_valid=base_url_valid,
            model_configured=model_configured,
            provider=provider,
            model_name=model_name,
            base_url=base_url,
            deep_check_passed=None,
            error_message=error_msg,
        )

    # 6. Nếu deep=True, thực hiện kiểm tra kết nối tới /models
    if deep:
        try:
            await asyncio.to_thread(_probe_models_endpoint, base_url, resolved_api_key, 3.0)
            return LLMReadinessReport(
                status="HEALTHY",
                api_key_configured=True,
                base_url_valid=True,
                model_configured=True,
                provider=provider,
                model_name=model_name,
                base_url=base_url,
                deep_check_passed=True,
                error_message=None,
            )
        except Exception as exc:
            error_msg = f"LLM provider deep check failed for {base_url}/models: {exc}"
            log_bug(
                BugCode.PTB_LLM_001,
                subsystem="llm",
                severity="WARNING",
                message="LLM provider unconfigured or unavailable",
                exc=exc,
                context={
                    "provider": provider,
                    "base_url": base_url,
                    "model_name": model_name,
                    "deep": True,
                    "error": str(exc),
                },
            )
            return LLMReadinessReport(
                status="DEGRADED",
                api_key_configured=True,
                base_url_valid=True,
                model_configured=True,
                provider=provider,
                model_name=model_name,
                base_url=base_url,
                deep_check_passed=False,
                error_message=error_msg,
            )

    return LLMReadinessReport(
        status="HEALTHY",
        api_key_configured=True,
        base_url_valid=True,
        model_configured=True,
        provider=provider,
        model_name=model_name,
        base_url=base_url,
        deep_check_passed=None,
        error_message=None,
    )
