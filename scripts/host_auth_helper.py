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
    except Exception:
        pass

import json
import subprocess
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


class HostAuthHandler(BaseHTTPRequestHandler):
    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-PTB-API-KEY")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

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

        elif path == "/login":
            qs = parse_qs(parsed.query)
            service = qs.get("service", ["all"])[0].lower()
            self._trigger_login(service)
            return

        elif path == "/status":
            self._check_status()
            return

        elif path == "/clear":
            self._clear_cookies()
            return

        self.send_response(404)
        self._send_cors_headers()
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/clear" or path == "/api/auth/clear-cookies":
            self._clear_cookies()
            return

        elif path == "/login" or path == "/api/auth/start-login":
            service = "all"
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length > 0:
                    body = self.rfile.read(length).decode("utf-8")
                    data = json.loads(body)
                    service = data.get("service", "all").lower()
            except Exception:
                pass
            self._trigger_login(service)
            return

        self.send_response(404)
        self._send_cors_headers()
        self.end_headers()

    def _trigger_login(self, service: str):
        global ACTIVE_LOGIN_PROCESS
        if service not in ("teams", "outlook", "all"):
            service = "all"

        storage_path = str((ROOT_DIR / "data" / "playwright" / "storage_state.json").resolve())
        os.makedirs(os.path.dirname(storage_path), exist_ok=True)

        # Hủy tiến trình login trước đó nếu còn đang chạy
        if ACTIVE_LOGIN_PROCESS is not None and ACTIVE_LOGIN_PROCESS.poll() is None:
            try:
                ACTIVE_LOGIN_PROCESS.terminate()
            except Exception:
                pass
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", "Get-Process -Name chrome, pythonw | Where-Object { $_.CommandLine -like '*ptb_acquisition.playwright.login*' } | Stop-Process -Force -ErrorAction SilentlyContinue"],
                    capture_output=True,
                    timeout=3
                )
        except Exception:
            pass

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

        python_path = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
        exec_to_use = str(python_path) if python_path.exists() else PYTHON_EXEC

        if sys.platform == "win32":
            task_name = f"PTB_{service.capitalize()}_Login" if service in ("teams", "outlook") else "PTB_Teams_Login"
            try:
                # Trigger via interactive scheduled task to guarantee popup on WinSta0\default
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
                        "message": f"Đã mở cửa sổ trình duyệt đăng nhập {service.upper()} trên màn hình máy tính của bạn!",
                        "storage_path": storage_path,
                        "launcher": "schtasks_interactive"
                    }
                    self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
                    return
            except Exception as e:
                print(f"[host_auth_helper] schtasks trigger fallback: {e}")

            cmd = [
                "cmd.exe",
                "/c",
                "start",
                f"PTB {service.upper()} Login",
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
            ACTIVE_LOGIN_PROCESS = subprocess.Popen(
                cmd,
                cwd=str(ROOT_DIR),
                env=env,
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
                import json
                from datetime import datetime, timezone
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
            except Exception:
                pass
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
    print(f"[*] Khởi động PTB Host Auth Helper trên cổng {PORT} (0.0.0.0)...")
    print(f"[*] Python thực thi trên máy chính: {PYTHON_EXEC}")
    server = HTTPServer(("0.0.0.0", PORT), HostAuthHandler)
    print(f"[✓] Đang lắng nghe yêu cầu mở trình duyệt từ Web UI tại cổng {PORT}...")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Dừng Host Auth Helper.")
        server.server_close()


if __name__ == "__main__":
    main()
