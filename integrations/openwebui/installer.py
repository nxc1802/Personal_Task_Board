"""OpenWebUI Auto-Installer & Integration Bootstrap for Personal Task Board (Phase R12).

Tự động cài đặt / cập nhật các thành phần PTB vào OpenWebUI pinned version:
1. PINNED_OPENWEBUI_VERSION = "v0.5.10"
2. check_connection: Kiểm tra kết nối tới OpenWebUI endpoint qua httpx.
3. install_components: Sao chép ptb_tools, ptb_board_action, ptb_board.html và xuất JSON metadata schemas.
4. get_install_summary: Báo cáo tóm tắt cài đặt.
"""

import json
import logging
import os
from pathlib import Path
import shutil
from typing import Any, Dict, List, Optional, Union
import httpx

logger = logging.getLogger("ptb.openwebui.installer")

PINNED_OPENWEBUI_VERSION = "v0.5.10"

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OPENWEBUI_INTEGRATION_DIR = Path(__file__).resolve().parent


class class_or_instance_method:
    def __init__(self, fn):
        self.fn = fn

    def __get__(self, obj, cls=None):
        if obj is None:
            return lambda *args, **kwargs: self.fn(cls(), *args, **kwargs)
        return lambda *args, **kwargs: self.fn(obj, *args, **kwargs)


class OpenWebUIInstaller:
    """Quản lý quá trình cài đặt PTB components vào OpenWebUI."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:3000",
        default_data_dir: Optional[Path] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.default_data_dir = Path(default_data_dir) if default_data_dir else self._find_default_data_dir()
        self._last_result: Optional[Dict[str, Any]] = None

    @staticmethod
    def _find_default_data_dir() -> Path:
        env_dir = os.getenv("OPENWEBUI_DATA_DIR")
        if env_dir:
            return Path(env_dir)
        return REPO_ROOT / "data" / "openwebui"

    async def check_connection(self, url: Optional[str] = None) -> bool:
        """Kiểm tra OpenWebUI endpoint có phản hồi hay không qua httpx."""
        target_url = (url or self.base_url).rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(target_url)
                return resp.status_code < 500
        except Exception:
            return False

    @class_or_instance_method
    async def install_components(
        self,
        openwebui_data_dir: Optional[Union[Path, str]] = None,
        base_url: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Cài đặt các components: Tools, Actions, Board HTML vào OpenWebUI data directory."""
        raw_dir = openwebui_data_dir or kwargs.get("data_dir")
        raw_url = base_url or kwargs.get("openwebui_url") or kwargs.get("url")
        dest_dir = Path(raw_dir) if raw_dir else self.default_data_dir
        dest_dir.mkdir(parents=True, exist_ok=True)

        tools_src = OPENWEBUI_INTEGRATION_DIR / "tools" / "ptb_tools.py"
        actions_src = OPENWEBUI_INTEGRATION_DIR / "functions" / "ptb_board_action.py"
        board_src = OPENWEBUI_INTEGRATION_DIR / "board" / "ptb_board.html"

        if not tools_src.exists():
            raise FileNotFoundError(f"Missing PTB Tools source file at {tools_src}")
        if not actions_src.exists():
            raise FileNotFoundError(f"Missing PTB Board Action source file at {actions_src}")
        if not board_src.exists():
            raise FileNotFoundError(f"Missing PTB Board HTML source file at {board_src}")

        tools_dest_dir = dest_dir / "tools"
        functions_dest_dir = dest_dir / "functions"
        artifacts_dest_dir = dest_dir / "artifacts"

        tools_dest_dir.mkdir(parents=True, exist_ok=True)
        functions_dest_dir.mkdir(parents=True, exist_ok=True)
        artifacts_dest_dir.mkdir(parents=True, exist_ok=True)

        # 1. Deployed files
        deployed_tools = tools_dest_dir / "ptb_tools.py"
        deployed_tools_meta = tools_dest_dir / "ptb_tools.json"
        deployed_action = functions_dest_dir / "ptb_board_action.py"
        deployed_action_meta = functions_dest_dir / "ptb_board_action.json"
        deployed_board = artifacts_dest_dir / "ptb_board.html"

        shutil.copy2(tools_src, deployed_tools)
        shutil.copy2(actions_src, deployed_action)
        shutil.copy2(board_src, deployed_board)

        # 2. Metadata schemas
        tools_manifest = {
            "name": "ptb_tools",
            "title": "Personal Task Board Tools",
            "version": "1.0.0",
            "openwebui_version": PINNED_OPENWEBUI_VERSION,
            "description": "Bộ công cụ truy vấn Personal Task Board cho OpenWebUI",
            "file": "ptb_tools.py",
        }
        with open(deployed_tools_meta, "w", encoding="utf-8") as f:
            json.dump(tools_manifest, f, indent=2)

        action_manifest = {
            "name": "ptb_board_action",
            "title": "PTB Board Action & Assistant",
            "version": "1.0.0",
            "openwebui_version": PINNED_OPENWEBUI_VERSION,
            "description": "Action and prompt shortcut filter for Personal Task Board",
            "file": "ptb_board_action.py",
        }
        with open(deployed_action_meta, "w", encoding="utf-8") as f:
            json.dump(action_manifest, f, indent=2)

        deployed_list = [
            str(deployed_tools),
            str(deployed_tools_meta),
            str(deployed_action),
            str(deployed_action_meta),
            str(deployed_board),
        ]

        result = {
            "status": "success",
            "success": True,
            "pinned_version": PINNED_OPENWEBUI_VERSION,
            "target_dir": str(dest_dir),
            "deployed_files": deployed_list,
            "components_installed": [
                "ptb_tools.py",
                "ptb_tools.json",
                "ptb_board_action.py",
                "ptb_board_action.json",
                "ptb_board.html",
            ],
            "message": f"Successfully installed PTB components for OpenWebUI {PINNED_OPENWEBUI_VERSION}",
        }
        self._last_result = result
        return result

    def get_install_summary(self) -> str:
        """Trả về báo cáo cài đặt chi tiết cho người dùng."""
        if not self._last_result:
            return "Chưa có thành phần nào được cài đặt. Vui lòng chạy install_components()."

        res = self._last_result
        lines = [
            "=" * 70,
            "BÁO CÁO CÀI ĐẶT OPENWEBUI AUTO-INSTALLER",
            "=" * 70,
            f"Trạng thái      : {res.get('status', '').upper()}",
            f"Pinned Version  : {res.get('pinned_version', PINNED_OPENWEBUI_VERSION)}",
            f"Thư mục đích    : {res.get('target_dir')}",
            "Thành phần đã cài:",
            "  • Tools       : ptb_tools.py (kèm schema ptb_tools.json)",
            "  • Functions   : ptb_board_action.py (kèm schema ptb_board_action.json)",
            "  • Artifact    : ptb_board.html",
            "=" * 70,
        ]
        return "\n".join(lines)
