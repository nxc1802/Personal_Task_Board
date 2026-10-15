#!/usr/bin/env python3
"""
patch_openwebui_board.py
Tích hợp trực tiếp Personal Task Board vào OpenWebUI (Cổng 3000):
- Thêm Tab "📋 Task Board" vào thanh tab Workspace (cạnh Models, Knowledge, Prompts, Tools).
- Thêm nút "📋 Task Board" vào Sidebar bên trái (ngay dưới Workspace).
- Bấm vào bất kỳ đâu sẽ mở trực tiếp bảng công việc Task Board NGAY TRONG GIAO DIỆN OpenWebUI,
  giữ nguyên Sidebar và thanh điều hướng, KHÔNG CẦN CHAT.
- Đăng ký route /board và /workspace/board trên FastAPI để truy cập trực tiếp không bao giờ 404.
"""

import os
import re
from pathlib import Path

BUILD_DIR = Path("/app/build")
BACKEND_DIR = Path("/app/backend")


def patch_main_py():
    """Ensure both /board and /workspace/board FastAPI routes exist and serve ptb_board.html."""
    main_py_path = BACKEND_DIR / "open_webui" / "main.py"
    if not main_py_path.exists():
        print("[-] main.py not found at", main_py_path)
        return False

    content = main_py_path.read_text(encoding="utf-8")

    # Clean any old decorators or functions
    content = re.sub(
        r'applications\.get_swagger_ui_html = swagger_ui_html.*?(?=if os\.path\.exists\(FRONTEND_BUILD_DIR\):)',
        'applications.get_swagger_ui_html = swagger_ui_html\n\n',
        content,
        flags=re.DOTALL
    )

    board_endpoints_code = """applications.get_swagger_ui_html = swagger_ui_html

from starlette.responses import HTMLResponse
from pathlib import Path

@app.get("/board", response_class=HTMLResponse)
@app.get("/workspace/board", response_class=HTMLResponse)
async def get_ptb_board_page():
    \"\"\"Serve Personal Task Board directly on OpenWebUI (port 3000).\"\"\"
    for p in [
        Path("/app/backend/data/board/ptb_board.html"),
        Path("/app/backend/data/openwebui/board/ptb_board.html"),
        Path("integrations/openwebui/board/ptb_board.html"),
    ]:
        if p.exists() and p.is_file():
            return HTMLResponse(content=p.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>PTB Board file not found</h1>", status_code=404)

"""
    target = "applications.get_swagger_ui_html = swagger_ui_html\n\n"
    if target in content:
        content = content.replace(target, board_endpoints_code)
    else:
        content = content.replace("applications.get_swagger_ui_html = swagger_ui_html", board_endpoints_code)

    main_py_path.write_text(content, encoding="utf-8")
    print("✓ Successfully patched main.py with /board and /workspace/board endpoints!")
    return True


def patch_index_html():
    """Inject native Workspace Tab and Sidebar button directly into OpenWebUI index.html."""
    index_file = BUILD_DIR / "index.html"
    if not index_file.exists():
        print(f"[-] index.html not found at {index_file}")
        return False

    content = index_file.read_text(encoding="utf-8")

    # The seamless in-app Workspace Task Board injection script
    injection_script = """
		<!-- PTB_WORKSPACE_NATIVE_TAB_V4 -->
		<style>
			#ptb-workspace-tab { display: none !important; }
		</style>
		<script>
		(() => {
			let isBoardActive = false;

			function showTaskBoard() {
				window.location.hash = "board";
				if (!window.location.pathname.startsWith("/workspace")) {
					const wsLink = document.querySelector('a[href="/workspace"]');
					if (wsLink) {
						wsLink.click();
						setTimeout(activateBoardInWorkspace, 200);
						return;
					} else {
						window.location.href = "/workspace#board";
						return;
					}
				}
				activateBoardInWorkspace();
			}

			function activateBoardInWorkspace() {
				const wsContainer = document.getElementById("workspace-container");
				if (!wsContainer) {
					setTimeout(activateBoardInWorkspace, 100);
					return;
				}

				isBoardActive = true;

				// Highlight sidebar Task Board item
				const sidebarBtn = document.getElementById("ptb-sidebar-tab");
				if (sidebarBtn) {
					sidebarBtn.style.background = "rgba(16, 185, 129, 0.18)";
					sidebarBtn.style.borderColor = "rgba(16, 185, 129, 0.5)";
					sidebarBtn.style.color = "#10b981";
					sidebarBtn.style.fontWeight = "600";
				}

				// Hide other children inside workspace-container and show Task Board iframe
				let iframe = document.getElementById("ptb-board-iframe");
				if (!iframe) {
					iframe = document.createElement("iframe");
					iframe.id = "ptb-board-iframe";
					iframe.src = "/board";
					iframe.title = "Personal Task Board";
					iframe.style.cssText = "width: 100%; height: calc(100vh - 65px); min-height: 520px; border: none; border-radius: 12px; display: block; background: transparent;";
					wsContainer.appendChild(iframe);
				} else {
					iframe.style.display = "block";
				}

				Array.from(wsContainer.children).forEach(child => {
					if (child !== iframe) {
						child.dataset.ptbHidden = "true";
						child.style.display = "none";
					}
				});
			}

			function hideTaskBoard() {
				isBoardActive = false;
				window.location.hash = "";
				const iframe = document.getElementById("ptb-board-iframe");
				if (iframe) {
					iframe.style.display = "none";
				}
				const wsContainer = document.getElementById("workspace-container");
				if (wsContainer) {
					Array.from(wsContainer.children).forEach(child => {
						if (child.dataset.ptbHidden === "true") {
							child.style.display = "";
							delete child.dataset.ptbHidden;
						}
					});
				}
				const sidebarBtn = document.getElementById("ptb-sidebar-tab");
				if (sidebarBtn) {
					sidebarBtn.style.background = "rgba(16, 185, 129, 0.08)";
					sidebarBtn.style.borderColor = "rgba(16, 185, 129, 0.25)";
					sidebarBtn.style.color = "#10b981";
					sidebarBtn.style.fontWeight = "500";
				}
			}

			function injectNavElements() {
				try {
					// 1. Clean up duplicate top toolbar tab (per user request)
					const oldTab = document.getElementById("ptb-workspace-tab");
					if (oldTab) oldTab.remove();

					// Also remove any element in header/nav containing Task Board that is not the sidebar item
					const topItems = document.querySelectorAll('header button, nav button, div[role="tablist"] button, div[role="tablist"] a');
					topItems.forEach(el => {
						if (el.id === "ptb-workspace-tab" || (el.innerText && el.innerText.includes("Task Board") && el.id !== "ptb-sidebar-tab")) {
							el.remove();
						}
					});

					// Listen for clicks on Workspace top tabs (Models, Knowledge, Prompts, Tools) to switch away from board
					const wsTabs = document.querySelectorAll('a[href^="/workspace/"]');
					wsTabs.forEach(t => {
						if (!t._ptbListener) {
							t._ptbListener = true;
							t.addEventListener("click", () => {
								hideTaskBoard();
							});
						}
					});

					// 2. Sidebar Item "Task Board" directly below Workspace
					const wsLink = document.querySelector('a[href="/workspace"]');
					if (wsLink && !document.getElementById("ptb-sidebar-tab")) {
						const btn = document.createElement("button");
						btn.id = "ptb-sidebar-tab";
						btn.type = "button";
						btn.style.cssText = `
							display: flex; align-items: center; justify-content: flex-start;
							width: calc(100% - 16px); margin: 3px 8px;
							padding: 7px 12px; border-radius: 8px;
							font-size: 13.5px; font-weight: 500;
							color: #10b981; background: rgba(16, 185, 129, 0.08);
							border: 1px solid rgba(16, 185, 129, 0.25);
							cursor: pointer; transition: all 0.15s ease;
							text-align: left;
						`;
						btn.innerHTML = `<span>Task Board</span>`;
						btn.title = "Mở bảng công việc Personal Task Board trong Workspace";
						btn.onmouseover = () => {
							btn.style.background = 'rgba(16, 185, 129, 0.18)';
							btn.style.borderColor = 'rgba(16, 185, 129, 0.45)';
						};
						btn.onmouseout = () => {
							btn.style.background = isBoardActive ? 'rgba(16, 185, 129, 0.18)' : 'rgba(16, 185, 129, 0.08)';
							btn.style.borderColor = isBoardActive ? 'rgba(16, 185, 129, 0.45)' : 'rgba(16, 185, 129, 0.25)';
						};
						btn.onclick = (e) => {
							e.preventDefault();
							showTaskBoard();
						};

						const parentRow = wsLink.closest("div") || wsLink.parentElement;
						if (parentRow && parentRow.parentElement) {
							parentRow.parentElement.insertBefore(btn, parentRow.nextSibling);
						}
					}

					// Auto-activate if URL hash is #board
					if (window.location.hash === "#board" && window.location.pathname.startsWith("/workspace")) {
						activateBoardInWorkspace();
					}
				} catch (err) {
					console.error("PTB Workspace injection error:", err);
				}
			}

			if (document.readyState === "loading") {
				document.addEventListener("DOMContentLoaded", () => setTimeout(injectNavElements, 200));
			} else {
				setTimeout(injectNavElements, 200);
			}

			const obs = new MutationObserver(() => {
				clearTimeout(obs._timer);
				obs._timer = setTimeout(injectNavElements, 120);
			});
			obs.observe(document.documentElement, { childList: true, subtree: true });

			// Listen to hash changes
			window.addEventListener("hashchange", () => {
				if (window.location.hash === "#board") {
					showTaskBoard();
				} else {
					hideTaskBoard();
				}
			});
		})();
		</script>
"""

    # Remove all previous PTB injection markers (including V4 and any others)
    content = re.sub(
        r'\s*<!-- PTB_[^>]*-->.*?</script>\s*',
        '\n',
        content,
        flags=re.DOTALL
    )

    # Inject before </head>
    content = content.replace("</head>", injection_script.strip() + "\n</head>")

    index_file.write_text(content, encoding="utf-8")
    print("✓ Successfully updated index.html with PTB_WORKSPACE_NATIVE_TAB_V4.")
    return True


if __name__ == "__main__":
    print("[*] Starting OpenWebUI PTB Integration Patch...")
    patch_main_py()
    patch_index_html()
    print("[✓] All patches completed successfully!")
