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
DEFAULT_DATA_DIR = Path("./data/openwebui")
CONTAINER_DATA_DIR = "/app/backend/data"

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

    DEFAULT_DATA_DIR: Path = DEFAULT_DATA_DIR
    CONTAINER_DATA_DIR: str = CONTAINER_DATA_DIR
    PINNED_VERSION: str = PINNED_OPENWEBUI_VERSION

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:3000",
        default_data_dir: Optional[Union[Path, str]] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.default_data_dir = (
            Path(default_data_dir).expanduser()
            if default_data_dir is not None
            else self._find_default_data_dir()
        )
        self._last_result: Optional[Dict[str, Any]] = None

    @classmethod
    def _find_default_data_dir(cls) -> Path:
        env_dir = os.getenv("OPENWEBUI_DATA_DIR")
        if env_dir:
            return Path(env_dir).expanduser()
        return cls.DEFAULT_DATA_DIR

    @classmethod
    def get_compose_volume_mount(cls) -> str:
        """Trả về chuỗi bind mount chuẩn cho docker-compose.yml (tương thích Windows/macOS/Linux)."""
        rel_posix = cls.DEFAULT_DATA_DIR.as_posix().lstrip("./")
        return f"./{rel_posix}:{cls.CONTAINER_DATA_DIR}"

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
        """Cài đặt các components: Tools, Functions, Board HTML và ptb_manifest.json vào OpenWebUI data directory."""
        raw_dir = openwebui_data_dir or kwargs.get("data_dir")
        raw_url = base_url or kwargs.get("openwebui_url") or kwargs.get("url")
        if raw_url:
            self.base_url = str(raw_url).rstrip("/")

        dest_dir = Path(raw_dir).expanduser() if raw_dir is not None else self.default_data_dir
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
        board_dest_dir = dest_dir / "board"
        artifacts_dest_dir = dest_dir / "artifacts"

        tools_dest_dir.mkdir(parents=True, exist_ok=True)
        functions_dest_dir.mkdir(parents=True, exist_ok=True)
        board_dest_dir.mkdir(parents=True, exist_ok=True)
        artifacts_dest_dir.mkdir(parents=True, exist_ok=True)

        # 1. Deployed files
        deployed_tools = tools_dest_dir / "ptb_tools.py"
        deployed_tools_meta = tools_dest_dir / "ptb_tools.json"
        deployed_action = functions_dest_dir / "ptb_board_action.py"
        deployed_action_meta = functions_dest_dir / "ptb_board_action.json"
        deployed_board = board_dest_dir / "ptb_board.html"
        deployed_artifact_board = artifacts_dest_dir / "ptb_board.html"
        deployed_manifest = dest_dir / "ptb_manifest.json"

        shutil.copy2(tools_src, deployed_tools)
        shutil.copy2(actions_src, deployed_action)
        shutil.copy2(board_src, deployed_board)
        shutil.copy2(board_src, deployed_artifact_board)

        # 2. Metadata schemas
        tools_manifest = {
            "name": "ptb_tools",
            "title": "Personal Task Board Tools",
            "version": "1.2.0",
            "openwebui_version": PINNED_OPENWEBUI_VERSION,
            "description": "Bộ công cụ truy vấn Personal Task Board cho OpenWebUI (Live-Only)",
            "file": "ptb_tools.py",
        }
        deployed_tools_meta.write_text(
            json.dumps(tools_manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        action_manifest = {
            "name": "ptb_board_action",
            "title": "PTB Board Action & Assistant",
            "version": "1.2.0",
            "openwebui_version": PINNED_OPENWEBUI_VERSION,
            "description": "Action and prompt shortcut filter for Personal Task Board (Live-Only)",
            "file": "ptb_board_action.py",
        }
        deployed_action_meta.write_text(
            json.dumps(action_manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # 3. Root installation manifest (ptb_manifest.json)
        default_dir_posix = f"./{self.DEFAULT_DATA_DIR.as_posix().lstrip('./')}"
        ptb_manifest = {
            "package": "personal-task-board-openwebui",
            "version": "1.2.0",
            "pinned_version": PINNED_OPENWEBUI_VERSION,
            "openwebui_version": PINNED_OPENWEBUI_VERSION,
            "default_data_dir": default_dir_posix,
            "container_data_dir": self.CONTAINER_DATA_DIR,
            "compose_volume_mount": self.get_compose_volume_mount(),
            "directories": ["tools", "functions", "board"],
            "components": {
                "tools": {
                    "file": Path("tools", "ptb_tools.py").as_posix(),
                    "manifest": Path("tools", "ptb_tools.json").as_posix(),
                },
                "functions": {
                    "file": Path("functions", "ptb_board_action.py").as_posix(),
                    "manifest": Path("functions", "ptb_board_action.json").as_posix(),
                },
                "board": {
                    "file": Path("board", "ptb_board.html").as_posix(),
                    "container_path": f"{self.CONTAINER_DATA_DIR}/board/ptb_board.html",
                },
            },
            "files": [
                Path("tools", "ptb_tools.py").as_posix(),
                Path("tools", "ptb_tools.json").as_posix(),
                Path("functions", "ptb_board_action.py").as_posix(),
                Path("functions", "ptb_board_action.json").as_posix(),
                Path("board", "ptb_board.html").as_posix(),
                "ptb_manifest.json",
            ],
        }
        deployed_manifest.write_text(
            json.dumps(ptb_manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        deployed_list = [
            str(deployed_tools),
            str(deployed_tools_meta),
            str(deployed_action),
            str(deployed_action_meta),
            str(deployed_board),
            str(deployed_artifact_board),
            str(deployed_manifest),
        ]

        result = {
            "status": "success",
            "success": True,
            "pinned_version": PINNED_OPENWEBUI_VERSION,
            "target_dir": str(dest_dir),
            "manifest_path": str(deployed_manifest),
            "deployed_files": deployed_list,
            "components_installed": [
                "ptb_tools.py",
                "ptb_tools.json",
                "ptb_board_action.py",
                "ptb_board_action.json",
                "ptb_board.html",
                "ptb_manifest.json",
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
            "  • Tools       : tools/ptb_tools.py (kèm schema tools/ptb_tools.json)",
            "  • Functions   : functions/ptb_board_action.py (kèm schema functions/ptb_board_action.json)",
            "  • Board       : board/ptb_board.html",
            "  • Manifest    : ptb_manifest.json",
            "=" * 70,
        ]
        return "\n".join(lines)
