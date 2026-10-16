"""
Host Auth Helper Service (Port 8999)
Chạy trực tiếp trên máy chính (Windows Host) để lắng nghe yêu cầu đăng nhập từ Web UI (OpenWebUI :3000 hoặc Task Board SPA).
Khi nhận được yêu cầu, dịch vụ này sẽ mở trình duyệt Chromium/Edge thật trên màn hình máy chính Windows để người dùng đăng nhập.
"""

import os
import sys

# Ensure UTF-8 output on Windows console
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception as reconf_err:
        sys.stderr.write(f"Warning: could not reconfigure encoding: {reconf_err}\n")

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

ROOT_DIR = Path(__file__).resolve().parents[1]
PORT = 8999

# Tìm đường dẫn Python thực thi chuẩn trên máy chính
POSSIBLE_PYTHONS = [
    ROOT_DIR / ".venv" / "Scripts" / "python.exe",
    Path(sys.executable),
]
PYTHON_EXEC = next((str(p) for p in POSSIBLE_PYTHONS if p.exists()), sys.executable)
ACTIVE_LOGIN_PROCESS = None


HOST = os.getenv("HOST_AUTH_HELPER_HOST", "127.0.0.1")

ALLOWED_ORIGIN_PATTERNS = [
    re.compile(r"^https?://localhost(?::\d+)?$", re.IGNORECASE),
    re.compile(r"^https?://127\.0\.0\.1(?::\d+)?$", re.IGNORECASE),
    re.compile(r"^https?://host\.docker\.internal(?::\d+)?$", re.IGNORECASE),
]


class HostAuthHandler(BaseHTTPRequestHandler):
    def _is_origin_allowed(self, origin: str) -> bool:
        if not origin:
            return True
        return any(pattern.match(origin) for pattern in ALLOWED_ORIGIN_PATTERNS)

    def _send_cors_headers(self):
        origin = self.headers.get("Origin", "").strip()
        if origin and self._is_origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        else:
            self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:3000")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-PTB-API-KEY")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _is_authenticated(self) -> bool:
        configured_key = os.getenv("PTB_API_KEY", "").strip()
        if not configured_key:
            return True
        header_key = self.headers.get("X-PTB-API-KEY", "").strip()
        auth_header = self.headers.get("Authorization", "").strip()
        bearer_key = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
        return (header_key == configured_key) or (bearer_key == configured_key)

    def _send_unauthorized(self):
        self.send_response(401)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        resp = {"error": "Unauthorized", "message": "PTB_API_KEY authentication failed."}
        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path in ("/login", "/clear", "/api/auth/start-login", "/api/auth/clear-cookies"):
            self.send_response(405)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            resp = {
                "error": "Method Not Allowed",
                "message": f"GET is not permitted on {path} to prevent state mutation. Use POST."
            }
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        if not self._is_authenticated():
            self._send_unauthorized()
            return

        if path == "" or path == "/health":
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            resp = {
                "status": "HEALTHY",
                "service": "ptb-host-auth-helper",
                "host_os": sys.platform,
                "python_exec": PYTHON_EXEC,
                "message": "Host Auth Helper đang chạy trên máy chính Windows sẵn sàng mở trình duyệt."
            }
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        elif path == "/status":
            self._check_status()
            return

        self.send_response(404)
        self._send_cors_headers()
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if not self._is_authenticated():
            self._send_unauthorized()
            return

        if path in ("/clear", "/api/auth/clear-cookies"):
            # Require explicit confirmation body {"confirm": true}
            confirmed = False
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length > 0:
                    body = self.rfile.read(length).decode("utf-8")
                    data = json.loads(body)
                    confirmed = bool(data.get("confirm", False))
            except Exception as parse_err:
                print(f"[!] Warning parsing clear request body: {parse_err}")

            if not confirmed:
                self.send_response(400)
                self._send_cors_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                resp = {
                    "error": "Confirmation Required",
                    "message": "Clearing cookies requires confirmation body: {'confirm': true}"
                }
                self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
                return

            self._clear_cookies()
            return

        elif path in ("/login", "/api/auth/start-login"):
            service = "all"
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length > 0:
                    body = self.rfile.read(length).decode("utf-8")
                    data = json.loads(body)
                    service = data.get("service", "all").lower()
            except Exception as parse_err:
                print(f"[!] Warning parsing login request body: {parse_err}")
            self._trigger_login(service)
            return

        self.send_response(404)
        self._send_cors_headers()
        self.end_headers()

    def _trigger_login(self, service: str):
        global ACTIVE_LOGIN_PROCESS
        if service not in ("teams", "outlook", "jira", "github", "all"):
            service = "all"

        web_urls = {
            "teams": "https://teams.microsoft.com",
            "outlook": "https://outlook.office.com/mail/",
            "jira": "https://id.atlassian.com/login",
            "github": "https://github.com/login"
        }
        web_url = web_urls.get(service, "https://teams.microsoft.com")

        storage_path = str((ROOT_DIR / "data" / "playwright" / "storage_state.json").resolve())
        os.makedirs(os.path.dirname(storage_path), exist_ok=True)

        # Hủy tiến trình login trước đó nếu còn đang chạy
        if ACTIVE_LOGIN_PROCESS is not None and ACTIVE_LOGIN_PROCESS.poll() is None:
            try:
                ACTIVE_LOGIN_PROCESS.terminate()
            except Exception as term_err:
                print(f"[!] Warning terminating login process: {term_err}")
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", "Get-Process -Name chrome, pythonw | Where-Object { $_.CommandLine -like '*ptb_acquisition.playwright.login*' } | Stop-Process -Force -ErrorAction SilentlyContinue"],
                    capture_output=True,
                    timeout=3
                )
        except Exception as kill_err:
            print(f"[!] Warning stopping old chrome/python processes: {kill_err}")

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [
            str(ROOT_DIR / "packages" / "contracts" / "src"),
            str(ROOT_DIR / "packages" / "database" / "src"),
            str(ROOT_DIR / "services" / "acquisition" / "src"),
            str(ROOT_DIR / "services" / "processing" / "src"),
            str(ROOT_DIR / "services" / "intelligence" / "src"),
            str(ROOT_DIR / "services" / "application" / "src"),
            str(ROOT_DIR / "services" / "mcp" / "src"),
            str(ROOT_DIR / "packages" / "graph_memory" / "src"),
            str(ROOT_DIR),
            env.get("PYTHONPATH", "")
        ]))

        log_path = ROOT_DIR / "data" / "playwright" / "host_login.log"
        log_file = open(log_path, "a", encoding="utf-8")

        pythonw_path = ROOT_DIR / ".venv" / "Scripts" / "pythonw.exe"
        exec_to_use = str(pythonw_path) if pythonw_path.exists() else (str(ROOT_DIR / ".venv" / "Scripts" / "python.exe") if (ROOT_DIR / ".venv" / "Scripts" / "python.exe").exists() else PYTHON_EXEC)

        if sys.platform == "win32":
            task_map = {
                "teams": "PTB_Teams_Login",
                "outlook": "PTB_Outlook_Login",
                "jira": "PTB_Jira_Login",
                "github": "PTB_Github_Login",
            }
            task_name = task_map.get(service, "PTB_Teams_Login")
            try:
                # Trigger via interactive scheduled task to guarantee popup on WinSta0\default without console window
                sch_res = subprocess.run(["schtasks", "/run", "/tn", task_name], capture_output=True, text=True, timeout=5)
                if sch_res.returncode == 0:
                    ACTIVE_LOGIN_PROCESS = None
                    self.send_response(200)
                    self._send_cors_headers()
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    resp = {
                        "status": "LAUNCHED",
                        "service": service,
                        "web_url": web_url,
                        "message": f"Đã mở cửa sổ trình duyệt đăng nhập {service.upper()} trên màn hình máy tính của bạn!",
                        "storage_path": storage_path,
                        "launcher": "schtasks_interactive"
                    }
                    self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
                    return
            except Exception as e:
                print(f"[host_auth_helper] schtasks trigger fallback: {e}")

            cmd = [
                exec_to_use,
                "-m",
                "ptb_acquisition.playwright.login",
                "--service", service,
                "--storage-path", storage_path
            ]
        else:
            cmd = [
                exec_to_use,
                "-m",
                "ptb_acquisition.playwright.login",
                "--service", service,
                "--storage-path", storage_path
            ]

        try:
            creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            ACTIVE_LOGIN_PROCESS = subprocess.Popen(
                cmd,
                cwd=str(ROOT_DIR),
                env=env,
                stdout=log_file,
                stderr=log_file,
                creationflags=creationflags
            )
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            resp = {
                "status": "LAUNCHED",
                "service": service,
                "message": f"Đã mở cửa sổ trình duyệt đăng nhập {service.upper()} trên màn hình máy chính của bạn! Vui lòng hoàn thành đăng nhập trên cửa sổ vừa xuất hiện.",
                "storage_path": storage_path,
                "pid": ACTIVE_LOGIN_PROCESS.pid
            }
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
        except Exception as e:
            self.send_response(500)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            resp = {
                "status": "ERROR",
                "error": str(e),
                "message": f"Không thể khởi chạy trình duyệt trên máy chính: {e}"
            }
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))

    def _check_status(self):
        storage_path = ROOT_DIR / "data" / "playwright" / "storage_state.json"
        cookie_count = 0
        valid_cookies = 0
        domains = []
        status = "LOGIN_REQUIRED"
        msg = "Chưa có phiên hoặc phiên đã hết hạn."

        if storage_path.exists():
            try:
                with open(storage_path, "r", encoding="utf-8") as f:
                    cdata = json.load(f)
                clist = cdata.get("cookies", [])
                cookie_count = len(clist)
                now_ts = datetime.now(timezone.utc).timestamp()
                for c in clist:
                    exp = c.get("expires", -1)
                    if exp is None or exp <= 0 or exp > now_ts:
                        valid_cookies += 1
                    dom = c.get("domain", "")
                    if dom and dom not in domains:
                        domains.append(dom)
                if valid_cookies >= 3:
                    status = "HEALTHY"
                    msg = "Phiên đăng nhập hợp lệ, sẵn sàng tự động thu thập."
            except Exception as e:
                msg = str(e)

        self.send_response(200)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        resp = {
            "status": status,
            "exists": storage_path.exists(),
            "cookie_count": cookie_count,
            "valid_cookies": valid_cookies,
            "domains": domains[:6],
            "message": msg
        }
        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))

    def _clear_cookies(self):
        storage_path = ROOT_DIR / "data" / "playwright" / "storage_state.json"
        if storage_path.exists():
            try:
                storage_path.write_text(json.dumps({"cookies": [], "origins": []}, indent=2), encoding="utf-8")
            except Exception as write_err:
                print(f"[!] Warning writing empty cookies state: {write_err}")
        self.send_response(200)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        resp = {
            "status": "success",
            "message": "Đã xóa sạch toàn bộ cookie trên máy chính!",
            "cookie_count": 0
        }
        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format, *args):
        # Tắt bớt log stdout để không làm nhiễu terminal
        pass


def main():
    print(f"[*] Khởi động PTB Host Auth Helper trên {HOST}:{PORT}...")
    print(f"[*] Python thực thi trên máy chính: {PYTHON_EXEC}")
    server = HTTPServer((HOST, PORT), HostAuthHandler)
    print(f"[✓] Đang lắng nghe yêu cầu mở trình duyệt từ Web UI tại {HOST}:{PORT}...")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Dừng Host Auth Helper.")
        server.server_close()


if __name__ == "__main__":
    main()
