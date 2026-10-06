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

        # 4. Database & Plugin Registration into OpenWebUI SQLite DB (webui.db)
        tools_code = tools_src.read_text(encoding="utf-8")
        actions_code = actions_src.read_text(encoding="utf-8")
        db_reg = self.register_database_components(
            dest_dir=dest_dir,
            tools_code=tools_code,
            tools_meta=tools_manifest,
            actions_code=actions_code,
            actions_meta=action_manifest,
        )

        # 5. Optional API Registration if online
        api_reg_ok = False
        api_reg_error: Optional[str] = None
        api_token = os.getenv("OPENWEBUI_API_KEY")
        if await self.check_connection(self.base_url) and api_token:
            headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
            tool_payload = {
                "id": "ptb_tools",
                "name": tools_manifest.get("title", "Personal Task Board Tools"),
                "content": tools_code,
                "meta": {
                    "description": tools_manifest.get("description", "Bộ công cụ truy vấn Personal Task Board cho OpenWebUI (Live-Only)"),
                    "manifest": tools_manifest,
                },
            }
            action_payload = {
                "id": "ptb_board_action",
                "name": action_manifest.get("title", "PTB Board Action & Assistant"),
                "content": actions_code,
                "meta": {
                    "description": action_manifest.get("description", "Action and prompt shortcut filter for Personal Task Board (Live-Only)"),
                    "manifest": action_manifest,
                },
            }
            try:
                async with httpx.AsyncClient(timeout=4.0) as client:
                    # Register / update Tools
                    t_resp = await client.post(
                        f"{self.base_url}/api/v1/tools/create",
                        headers=headers,
                        json=tool_payload,
                    )
                    if t_resp.status_code == 400 and ("ID_TAKEN" in t_resp.text or "already taken" in t_resp.text.lower()):
                        t_resp = await client.post(
                            f"{self.base_url}/api/v1/tools/id/ptb_tools/update",
                            headers=headers,
                            json=tool_payload,
                        )
                    if not (200 <= t_resp.status_code < 300):
                        raise RuntimeError(f"Tools registration failed with HTTP {t_resp.status_code}: {t_resp.text}")

                    # Register / update Functions
                    f_resp = await client.post(
                        f"{self.base_url}/api/v1/functions/create",
                        headers=headers,
                        json=action_payload,
                    )
                    if f_resp.status_code == 400 and ("ID_TAKEN" in f_resp.text or "already taken" in f_resp.text.lower()):
                        f_resp = await client.post(
                            f"{self.base_url}/api/v1/functions/id/ptb_board_action/update",
                            headers=headers,
                            json=action_payload,
                        )
                    if not (200 <= f_resp.status_code < 300):
                        raise RuntimeError(f"Functions registration failed with HTTP {f_resp.status_code}: {f_resp.text}")

                    # Verify via GET
                    v_t_resp = await client.get(
                        f"{self.base_url}/api/v1/tools/id/ptb_tools",
                        headers=headers,
                    )
                    if not (200 <= v_t_resp.status_code < 300):
                        raise RuntimeError(f"Tools verification failed with HTTP {v_t_resp.status_code}: {v_t_resp.text}")

                    v_f_resp = await client.get(
                        f"{self.base_url}/api/v1/functions/id/ptb_board_action",
                        headers=headers,
                    )
                    if not (200 <= v_f_resp.status_code < 300):
                        raise RuntimeError(f"Functions verification failed with HTTP {v_f_resp.status_code}: {v_f_resp.text}")

                    api_reg_ok = True
            except Exception as e_api:
                api_reg_error = str(e_api)
                logger.warning("OpenWebUI API registration failed: %s", e_api)

        # 6. Post-installation validation on disk
        validation = self.validate_installation(
            dest_dir=dest_dir,
            raise_on_error=True,
        )

        db_registered = db_reg.get("registered", False)
        plugins_registered = db_registered or api_reg_ok
        install_success = validation["validation_ok"] and plugins_registered

        if install_success:
            msg = f"Successfully installed PTB components and verified plugin registration for OpenWebUI {PINNED_OPENWEBUI_VERSION}"
            status_label = "success"
        elif validation["validation_ok"]:
            msg = f"Deployed integration files on disk, but plugin registration into OpenWebUI failed (DB: {db_reg.get('error', 'unverified')}, API: {api_reg_error or 'offline/unauthenticated'})."
            status_label = "partial"
        else:
            msg = "Failed to deploy integration files to OpenWebUI data directory."
            status_label = "failed"

        result = {
            "status": status_label,
            "success": install_success,
            "validation_ok": validation["validation_ok"],
            "database_registered": db_registered,
            "tools_registered": db_reg.get("tools", []),
            "functions_registered": db_reg.get("functions", []),
            "tools_specs_count": db_reg.get("specs_count", 0),
            "api_registered": api_reg_ok,
            "api_error": api_reg_error,
            "db_path": db_reg.get("db_path"),
            "validated_files": validation["validated_files"],
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
            "message": msg,
        }
        self._last_result = result
        return result

    @classmethod
    def generate_tools_specs(cls, tools_source: Optional[Union[Path, str, Any]] = None) -> List[Dict[str, Any]]:
        """Tự động trích xuất JSON specs chuẩn OpenAI Function calling từ class Tools trong ptb_tools.py."""
        import inspect
        import re

        target_cls = None
        if isinstance(tools_source, type):
            target_cls = tools_source
        else:
            try:
                from integrations.openwebui.tools.ptb_tools import Tools as PtbTools
                target_cls = PtbTools
            except Exception as e_cls:
                logger.debug("Failed importing PtbTools class for specs generation: %s", e_cls)

        if target_cls is not None:
            specs: List[Dict[str, Any]] = []
            for attr_name in sorted(dir(target_cls)):
                if attr_name.startswith("_"):
                    continue
                attr = getattr(target_cls, attr_name)
                if not callable(attr) or inspect.isclass(attr):
                    continue
                sig = inspect.signature(attr)
                doc = inspect.getdoc(attr) or ""
                lines = [l.strip() for l in doc.splitlines() if l.strip()]
                desc = lines[0] if lines else attr_name
                params_doc: Dict[str, str] = {}
                for line in lines:
                    m = re.match(r":param\s+(\w+):\s*(.+)", line)
                    if m:
                        params_doc[m.group(1)] = m.group(2)

                props: Dict[str, Any] = {}
                required: List[str] = []
                for p_name, p in sig.parameters.items():
                    if p_name in ("self", "extra_params", "__user__"):
                        continue
                    p_type = "string"
                    if p.annotation in (int, "int"):
                        p_type = "integer"
                    elif p.annotation in (float, "float"):
                        p_type = "number"
                    elif p.annotation in (bool, "bool"):
                        p_type = "boolean"
                    p_desc = params_doc.get(p_name, f"Parameter {p_name}")
                    props[p_name] = {"type": p_type, "description": p_desc}
                    if p.default == inspect.Parameter.empty:
                        required.append(p_name)
                specs.append({
                    "name": attr_name,
                    "description": desc,
                    "parameters": {
                        "type": "object",
                        "properties": props,
                        "required": required,
                    },
                })
            if specs:
                return specs

        # Canonical fallback specs khớp 100% 11 methods của Tools trong ptb_tools.py
        return [
            {
                "name": "get_today_tasks",
                "description": "Lấy danh sách các công việc ưu tiên cao nhất hôm nay kèm hệ số điểm (0-100), breakdown chi tiết và lý do.",
                "parameters": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "description": "Số lượng task tối đa cần lấy (mặc định 5)."}},
                    "required": [],
                },
            },
            {
                "name": "get_tasks",
                "description": "Tra cứu danh sách công việc trong Task Board với bộ lọc đa tiêu chí (status, project, source).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "description": "Trạng thái cần lọc ('TODO', 'IN_PROGRESS', 'BLOCKED', 'DONE')"},
                        "project_key": {"type": "string", "description": "Mã dự án (e.g., 'OPS', 'CORE', 'PTB')"},
                        "source": {"type": "string", "description": "Nguồn bắt đầu ('ms_teams', 'outlook', 'jira', 'cursor')"},
                        "limit": {"type": "integer", "description": "Giới hạn số lượng trả về (mặc định 10)."},
                    },
                    "required": [],
                },
            },
            {
                "name": "get_review_queue",
                "description": "Lấy danh sách các task trích xuất tự động (Confidence 0.40 - 0.64) đang chờ người dùng phê duyệt.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            {
                "name": "approve_task",
                "description": "Phê duyệt một task candidate từ review queue đưa vào bảng công việc chính thức.",
                "parameters": {
                    "type": "object",
                    "properties": {"review_id": {"type": "string", "description": "Mã ID của review item cần duyệt."}},
                    "required": ["review_id"],
                },
            },
            {
                "name": "dismiss_task",
                "description": "Từ chối hoặc loại bỏ một task candidate khỏi review queue.",
                "parameters": {
                    "type": "object",
                    "properties": {"review_id": {"type": "string", "description": "Mã ID của review item cần loại bỏ."}},
                    "required": ["review_id"],
                },
            },
            {
                "name": "update_task_status",
                "description": "Cập nhật trạng thái của task (TODO, IN_PROGRESS, BLOCKED, DONE, DISMISSED).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "Mã UUID của task cần cập nhật."},
                        "new_status": {"type": "string", "description": "Trạng thái mới ('TODO', 'IN_PROGRESS', 'BLOCKED', 'DONE', 'DISMISSED')."},
                    },
                    "required": ["task_id", "new_status"],
                },
            },
            {
                "name": "get_waiting_items",
                "description": "Lấy danh sách các việc đang bị nghẽn (BLOCKED) do chờ người khác.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            {
                "name": "get_forgotten_commitments",
                "description": "Lấy danh sách các cam kết bằng lời hứa trong chat đã trôi quá hạn cần theo dõi (follow-up).",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            {
                "name": "search_knowledge",
                "description": "Tìm kiếm các Quyết định Kiến trúc (decisions) và Bài học Kinh nghiệm sửa lỗi (lessons).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Từ khóa tìm kiếm"},
                        "knowledge_type": {"type": "string", "description": "Loại tri thức ('decision', 'lesson', 'all')"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "get_source_health",
                "description": "Kiểm tra tình trạng hoạt động và đồng bộ của các Adapter nguồn dữ liệu.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            {
                "name": "render_board_artifact",
                "description": "Trả về Personal Task Board Artifact để OpenWebUI hiển thị trực quan trong panel tương tác bên cạnh.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        ]

    @classmethod
    def register_database_components(
        cls,
        dest_dir: Path,
        tools_code: str,
        tools_meta: Dict[str, Any],
        actions_code: str,
        actions_meta: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Đăng ký PTB Tools và Functions trực tiếp vào OpenWebUI SQLite database (webui.db).
        
        Quy tắc:
        - PTB tuyệt đối không tự sở hữu/migrate schema OpenWebUI (KHÔNG chạy CREATE TABLE).
        - Nếu webui.db chưa tồn tại hoặc tables ('tool', 'function') chưa được OpenWebUI tạo ra:
          báo lỗi và bỏ qua DB registration (yêu cầu dùng OpenWebUI REST API hoặc khởi động OpenWebUI trước).
        - Kiểm tra schema bảng qua PRAGMA table_info(...) và tự động ánh xạ các cột (bao gồm valves, access_control).
        """
        import sqlite3
        import time

        db_path = dest_dir / "webui.db"
        if not db_path.exists() or not db_path.is_file():
            return {
                "registered": False,
                "db_path": str(db_path),
                "error": (
                    f"OpenWebUI database file does not exist at '{db_path}'. "
                    "OpenWebUI must be started at least once to create database schema, or use REST API registration."
                ),
                "tools": [],
                "functions": [],
                "specs_count": 0,
            }

        try:
            conn = sqlite3.connect(str(db_path))
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('tool', 'function')"
                )
                existing_tables = {row[0] for row in cursor.fetchall()}
                if "tool" not in existing_tables or "function" not in existing_tables:
                    return {
                        "registered": False,
                        "db_path": str(db_path),
                        "error": (
                            f"OpenWebUI tables ('tool', 'function') do not exist in '{db_path}'. "
                            "OpenWebUI migrations have not run yet. Please start OpenWebUI first or use REST API registration."
                        ),
                        "tools": [],
                        "functions": [],
                        "specs_count": 0,
                    }

                cursor.execute("PRAGMA table_info(tool)")
                tool_cols = {row[1] for row in cursor.fetchall()}
                cursor.execute("PRAGMA table_info(function)")
                func_cols = {row[1] for row in cursor.fetchall()}

                required_tool_cols = {"id", "user_id", "name", "content", "specs", "meta", "created_at", "updated_at"}
                required_func_cols = {"id", "user_id", "name", "type", "content", "meta", "is_active", "is_global", "created_at", "updated_at"}

                missing_tool = required_tool_cols - tool_cols
                missing_func = required_func_cols - func_cols

                if missing_tool or missing_func:
                    return {
                        "registered": False,
                        "db_path": str(db_path),
                        "error": (
                            f"OpenWebUI database schema incomplete in '{db_path}': "
                            f"missing tool cols: {missing_tool}, missing function cols: {missing_func}"
                        ),
                        "tools": [],
                        "functions": [],
                        "specs_count": 0,
                    }

                now_ts = int(time.time())
                specs_list = cls.generate_tools_specs(tools_code)

                # Dynamically construct INSERT statement for tool based on present columns
                tool_insert_cols = ["id", "user_id", "name", "content", "specs", "meta"]
                tool_insert_vals = [
                    "ptb_tools",
                    "system",
                    tools_meta.get("title", "Personal Task Board Tools"),
                    tools_code,
                    json.dumps(specs_list, ensure_ascii=False),
                    json.dumps({
                        "description": tools_meta.get("description", "Personal Task Board Tools"),
                        "manifest": tools_meta,
                    }, ensure_ascii=False),
                ]
                if "valves" in tool_cols:
                    tool_insert_cols.append("valves")
                    tool_insert_vals.append(None)
                if "access_control" in tool_cols:
                    tool_insert_cols.append("access_control")
                    tool_insert_vals.append(None)

                tool_insert_cols.extend(["created_at", "updated_at"])
                tool_insert_vals.extend([now_ts, now_ts])

                tool_col_names = ", ".join(tool_insert_cols)
                tool_placeholders = ", ".join(["?"] * len(tool_insert_cols))
                tool_update_clauses = [
                    f"{c}=excluded.{c}" for c in tool_insert_cols if c not in ("id", "created_at")
                ]
                tool_update_sql = ", ".join(tool_update_clauses)

                cursor.execute(f"""
                    INSERT INTO tool ({tool_col_names})
                    VALUES ({tool_placeholders})
                    ON CONFLICT(id) DO UPDATE SET
                        {tool_update_sql}
                """, tool_insert_vals)

                # Dynamically construct INSERT statement for function based on present columns
                func_insert_cols = ["id", "user_id", "name", "type", "content", "meta"]
                func_insert_vals = [
                    "ptb_board_action",
                    "system",
                    actions_meta.get("title", "PTB Board Action & Assistant"),
                    "action",
                    actions_code,
                    json.dumps({
                        "description": actions_meta.get("description", "PTB Board Action & Assistant"),
                        "manifest": actions_meta,
                    }, ensure_ascii=False),
                ]
                if "valves" in func_cols:
                    func_insert_cols.append("valves")
                    func_insert_vals.append(None)

                func_insert_cols.extend(["is_active", "is_global", "created_at", "updated_at"])
                func_insert_vals.extend([1, 1, now_ts, now_ts])

                func_col_names = ", ".join(func_insert_cols)
                func_placeholders = ", ".join(["?"] * len(func_insert_cols))
                func_update_clauses = [
                    f"{c}=excluded.{c}" for c in func_insert_cols if c not in ("id", "created_at")
                ]
                func_update_sql = ", ".join(func_update_clauses)

                cursor.execute(f"""
                    INSERT INTO function ({func_col_names})
                    VALUES ({func_placeholders})
                    ON CONFLICT(id) DO UPDATE SET
                        {func_update_sql}
                """, func_insert_vals)

                conn.commit()
                cursor.execute("SELECT id, specs FROM tool WHERE id = 'ptb_tools'")
                tool_row = cursor.fetchone()
                cursor.execute("SELECT id FROM function WHERE id = 'ptb_board_action'")
                func_row = cursor.fetchone()
                has_tool = tool_row is not None
                has_func = func_row is not None
                parsed_specs = json.loads(tool_row[1]) if has_tool and tool_row[1] else []
                return {
                    "registered": bool(has_tool and has_func and len(parsed_specs) > 0),
                    "db_path": str(db_path),
                    "tools": ["ptb_tools"] if has_tool else [],
                    "functions": ["ptb_board_action"] if has_func else [],
                    "specs_count": len(parsed_specs),
                }
            finally:
                conn.close()
        except Exception as e_db:
            logger.warning("OpenWebUI DB registration note: %s", e_db)
            return {
                "registered": False,
                "db_path": str(db_path),
                "error": str(e_db),
                "tools": [],
                "functions": [],
                "specs_count": 0,
            }

    @class_or_instance_method
    def validate_installation(
        self,
        openwebui_data_dir: Optional[Union[Path, str]] = None,
        dest_dir: Optional[Union[Path, str]] = None,
        raise_on_error: bool = True,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Xác thực các thành phần đã cài đặt trên đĩa (tồn tại và dung lượng > 0 bytes)."""
        raw_dir = openwebui_data_dir or dest_dir or kwargs.get("data_dir")
        target_dir = Path(raw_dir).expanduser() if raw_dir is not None else self.default_data_dir

        expected_files = [
            "tools/ptb_tools.py",
            "tools/ptb_tools.json",
            "functions/ptb_board_action.py",
            "functions/ptb_board_action.json",
            "board/ptb_board.html",
            "ptb_manifest.json",
        ]

        validated_files: List[Dict[str, Any]] = []
        missing_files: List[str] = []
        empty_files: List[str] = []

        for rel_path in expected_files:
            file_path = target_dir / rel_path
            if not file_path.exists() or not file_path.is_file():
                missing_files.append(rel_path)
            elif file_path.stat().st_size == 0:
                empty_files.append(rel_path)
            else:
                validated_files.append({
                    "path": rel_path,
                    "size_bytes": file_path.stat().st_size,
                    "status": "OK",
                })

        failures = missing_files + empty_files
        validation_ok = (len(failures) == 0)

        validation_result: Dict[str, Any] = {
            "validation_ok": validation_ok,
            "target_dir": str(target_dir),
            "validated_files": validated_files,
            "missing_files": missing_files,
            "empty_files": empty_files,
            "failures": failures,
        }

        if not validation_ok and raise_on_error:
            raise RuntimeError(
                f"OpenWebUI post-install validation failed for directory '{target_dir}': "
                f"missing={missing_files}, empty={empty_files}"
            )

        return validation_result

    def get_install_summary(self) -> str:
        """Trả về báo cáo cài đặt chi tiết cho người dùng."""
        if not self._last_result:
            return "Chưa có thành phần nào được cài đặt. Vui lòng chạy install_components()."

        res = self._last_result
        validation_ok = res.get("validation_ok", False)
        validation_label = "PASSED (validation_ok=True)" if validation_ok else "FAILED (validation_ok=False)"

        lines = [
            "=" * 70,
            "BÁO CÁO CÀI ĐẶT OPENWEBUI AUTO-INSTALLER",
            "=" * 70,
            f"Trạng thái      : {res.get('status', '').upper()}",
            f"Pinned Version  : {res.get('pinned_version', PINNED_OPENWEBUI_VERSION)}",
            f"Thư mục đích    : {res.get('target_dir')}",
            f"Validation      : {validation_label}",
            "Thành phần đã cài:",
            "  • Tools       : tools/ptb_tools.py (kèm schema tools/ptb_tools.json)",
            "  • Functions   : functions/ptb_board_action.py (kèm schema functions/ptb_board_action.json)",
            "  • Board       : board/ptb_board.html",
            "  • Manifest    : ptb_manifest.json",
        ]

        if res.get("database_registered"):
            lines.append("Đăng ký OpenWebUI Plugins & Workspace:")
            lines.append(f"  ✓ Database (webui.db) : {res.get('db_path')}")
            for t in res.get("tools_registered", []):
                lines.append(f"  ✓ Tool Registered     : {t}")
            for fn in res.get("functions_registered", []):
                lines.append(f"  ✓ Function Registered : {fn}")

        validated_files = res.get("validated_files", [])
        if validated_files:
            lines.append("Files đã xác thực trên đĩa:")
            for vf in validated_files:
                p = vf.get("path") if isinstance(vf, dict) else str(vf)
                size_str = f" ({vf['size_bytes']} bytes)" if isinstance(vf, dict) and "size_bytes" in vf else ""
                lines.append(f"  ✓ {p}{size_str}")

        lines.append("=" * 70)
        return "\n".join(lines)
