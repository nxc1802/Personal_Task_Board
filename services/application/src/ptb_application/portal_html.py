PORTAL_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Personal Task Board - Control Center & Ingestion Portal</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #090d16;
      --surface: #111827;
      --surface-elevated: #1e293b;
      --surface-hover: #26354a;
      --border: #334155;
      --border-subtle: #1f2937;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --text-dim: #64748b;
      --accent: #3b82f6;
      --accent-gradient: linear-gradient(135deg, #3b82f6 0%, #6366f1 50%, #8b5cf6 100%);
      --accent-hover: #2563eb;
      --success: #10b981;
      --success-bg: rgba(16, 185, 129, 0.12);
      --warning: #f59e0b;
      --warning-bg: rgba(245, 158, 11, 0.12);
      --danger: #ef4444;
      --danger-bg: rgba(239, 68, 68, 0.12);
      --radius-sm: 8px;
      --radius-md: 12px;
      --radius-lg: 16px;
      --shadow-sm: 0 2px 8px rgba(0, 0, 0, 0.3);
      --shadow-lg: 0 12px 32px rgba(0, 0, 0, 0.45);
      --font: 'Plus Jakarta Sans', system-ui, -apple-system, sans-serif;
      --mono: 'JetBrains Mono', monospace;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: var(--bg);
      color: var(--text);
      font-family: var(--font);
      min-height: 100vh;
      line-height: 1.5;
      padding-bottom: 60px;
    }

    /* Header */
    header {
      background: rgba(17, 24, 39, 0.85);
      backdrop-filter: blur(12px);
      border-bottom: 1px solid var(--border);
      position: sticky;
      top: 0;
      z-index: 50;
      padding: 16px 32px;
    }
    .header-container {
      max-width: 1300px;
      margin: 0 auto;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .logo-area {
      display: flex;
      align-items: center;
      gap: 14px;
    }
    .logo-badge {
      width: 42px;
      height: 42px;
      border-radius: var(--radius-md);
      background: var(--accent-gradient);
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 800;
      font-size: 20px;
      box-shadow: 0 4px 16px rgba(99, 102, 241, 0.4);
    }
    .logo-text h1 {
      font-size: 19px;
      font-weight: 700;
      letter-spacing: -0.02em;
    }
    .logo-text p {
      font-size: 12px;
      color: var(--text-muted);
    }
    .nav-links {
      display: flex;
      gap: 12px;
      align-items: center;
    }
    .btn-link {
      background: var(--surface-elevated);
      color: var(--text);
      border: 1px solid var(--border);
      padding: 8px 14px;
      border-radius: var(--radius-sm);
      text-decoration: none;
      font-size: 13px;
      font-weight: 600;
      display: flex;
      align-items: center;
      gap: 8px;
      transition: all 0.2s ease;
    }
    .btn-link:hover {
      background: var(--surface-hover);
      border-color: var(--accent);
      color: #fff;
    }

    /* Main Container */
    main {
      max-width: 1300px;
      margin: 32px auto;
      padding: 0 24px;
      display: flex;
      flex-direction: column;
      gap: 32px;
    }

    /* System Status Bar */
    .status-bar {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 16px;
    }
    .status-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      padding: 16px 20px;
      display: flex;
      align-items: center;
      gap: 14px;
    }
    .status-indicator {
      width: 12px;
      height: 12px;
      border-radius: 50%;
      background: var(--success);
      box-shadow: 0 0 10px var(--success);
    }
    .status-info h4 {
      font-size: 13px;
      color: var(--text-muted);
      font-weight: 500;
    }
    .status-info p {
      font-size: 15px;
      font-weight: 700;
      color: var(--text);
    }

    /* Two-Column Layout */
    .grid-panels {
      display: grid;
      grid-template-columns: 1.2fr 0.8fr;
      gap: 28px;
    }
    @media (max-width: 960px) {
      .grid-panels { grid-template-columns: 1fr; }
    }

    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      padding: 24px;
      box-shadow: var(--shadow-sm);
      display: flex;
      flex-direction: column;
      gap: 18px;
    }
    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid var(--border);
      padding-bottom: 14px;
    }
    .card-title {
      font-size: 17px;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .badge {
      font-size: 11px;
      padding: 4px 8px;
      border-radius: 20px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .badge-primary { background: rgba(59, 130, 246, 0.15); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.3); }
    .badge-success { background: var(--success-bg); color: var(--success); border: 1px solid rgba(16, 185, 129, 0.3); }
    .badge-warning { background: var(--warning-bg); color: var(--warning); border: 1px solid rgba(245, 158, 11, 0.3); }

    /* Form Elements */
    .form-group {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    label {
      font-size: 13px;
      font-weight: 600;
      color: var(--text-muted);
    }
    textarea, input[type="text"] {
      width: 100%;
      background: var(--bg);
      border: 1px solid var(--border);
      color: var(--text);
      font-family: inherit;
      border-radius: var(--radius-sm);
      padding: 12px 14px;
      font-size: 14px;
      outline: none;
      transition: border-color 0.2s;
    }
    textarea:focus, input[type="text"]:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15);
    }
    textarea {
      resize: vertical;
      min-height: 120px;
    }

    /* Buttons */
    .btn {
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 10px;
      padding: 12px 20px;
      border-radius: var(--radius-sm);
      font-size: 14px;
      font-weight: 700;
      border: none;
      transition: all 0.2s ease;
    }
    .btn-primary {
      background: var(--accent-gradient);
      color: #ffffff;
      box-shadow: 0 4px 14px rgba(99, 102, 241, 0.35);
    }
    .btn-primary:hover:not(:disabled) {
      filter: brightness(1.1);
      transform: translateY(-1px);
    }
    .btn-secondary {
      background: var(--surface-elevated);
      color: var(--text);
      border: 1px solid var(--border);
    }
    .btn-secondary:hover:not(:disabled) {
      background: var(--surface-hover);
      border-color: var(--accent);
    }
    .btn-danger {
      background: var(--danger-bg);
      color: var(--danger);
      border: 1px solid rgba(239, 68, 68, 0.3);
    }
    .btn-danger:hover:not(:disabled) {
      background: rgba(239, 68, 68, 0.25);
    }
    .btn:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }

    /* Response / Logs Box */
    .log-box {
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      padding: 14px;
      font-family: var(--mono);
      font-size: 13px;
      color: #a5b4fc;
      max-height: 220px;
      overflow-y: auto;
      white-space: pre-wrap;
      word-break: break-word;
    }

    /* Task Table */
    .table-responsive {
      width: 100%;
      overflow-x: auto;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      text-align: left;
      font-size: 13px;
    }
    th {
      background: var(--surface-elevated);
      color: var(--text-muted);
      font-weight: 600;
      padding: 12px 16px;
      border-bottom: 1px solid var(--border);
    }
    td {
      padding: 14px 16px;
      border-bottom: 1px solid var(--border-subtle);
    }
    tr:hover td {
      background: rgba(255, 255, 255, 0.02);
    }
    .empty-state {
      text-align: center;
      padding: 40px;
      color: var(--text-dim);
    }

    /* Spinner */
    .spinner {
      width: 18px;
      height: 18px;
      border: 2px solid rgba(255, 255, 255, 0.2);
      border-top-color: #fff;
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
      display: none;
    }
    @keyframes spin {
      to { transform: rotate(360deg); }
    }
    .btn.loading .spinner { display: inline-block; }
  </style>
</head>
<body>

  <header>
    <div class="header-container">
      <div class="logo-area">
        <div class="logo-badge">⚡</div>
        <div class="logo-text">
          <h1>Personal Task Board</h1>
          <p>Local-First Knowledge & Intelligence Hub</p>
        </div>
      </div>
      <div class="nav-links">
        <a href="http://127.0.0.1:3000" target="_blank" class="btn-link">💬 Mở OpenWebUI (:3000)</a>
        <a href="http://127.0.0.1:7474" target="_blank" class="btn-link">🌐 Neo4j Graph (:7474)</a>
        <a href="/docs" target="_blank" class="btn-link">📖 REST API Docs</a>
      </div>
    </div>
  </header>

  <main>
    <!-- System Status Bar -->
    <div class="status-bar">
      <div class="status-card">
        <div class="status-indicator"></div>
        <div class="status-info">
          <h4>LLM Engine</h4>
          <p>Gemma 4 26B MoE</p>
        </div>
      </div>
      <div class="status-card">
        <div class="status-indicator"></div>
        <div class="status-info">
          <h4>Vector Embedding</h4>
          <p>Qwen3 0.6B (:8082)</p>
        </div>
      </div>
      <div class="status-card">
        <div class="status-indicator"></div>
        <div class="status-info">
          <h4>Decision & Rerank</h4>
          <p>Kev 0.8B (:8081)</p>
        </div>
      </div>
      <div class="status-card">
        <div class="status-indicator"></div>
        <div class="status-info">
          <h4>Authoritative Store</h4>
          <p>Neo4j Single-Store</p>
        </div>
      </div>
    </div>

    <!-- Main Actions Grid -->
    <div class="grid-panels">
      <!-- Panel 1: Ingest Personal Message -->
      <div class="card">
        <div class="card-header">
          <div class="card-title">
            <span>💬</span> Nhập Tin Nhắn / Email Cá Nhân
          </div>
          <span class="badge badge-primary">Realtime AI Pipeline</span>
        </div>

        <p style="font-size: 13px; color: var(--text-muted);">
          Dán nội dung tin nhắn giao việc, email, biên bản họp thực tế của bạn. Hệ thống sẽ tự động bóc tách (Gemma 4 26B), tạo vector nhúng (Qwen3), xếp hạng ưu tiên (Kev 0.8B) và lưu trực tiếp vào Neo4j.
        </p>

        <div class="form-group">
          <label for="msgInput">Nội dung tin nhắn / Email thực tế:</label>
          <textarea id="msgInput" placeholder="Ví dụ: Anh Hùng ơi, em đang check log và sẽ đẩy bản hotfix payment gateway lên production trước 17:00 chiều nay nhé!"></textarea>
        </div>

        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px;">
          <div class="form-group">
            <label for="senderInput">Người gửi:</label>
            <input type="text" id="senderInput" placeholder="VD: Nguyễn Văn Dương">
          </div>
          <div class="form-group">
            <label for="sourceInput">Nguồn thu thập:</label>
            <input type="text" id="sourceInput" value="Teams / Email Direct Input">
          </div>
        </div>

        <button id="btnProcessMsg" class="btn btn-primary" onclick="processPersonalMessage()">
          <span class="spinner" id="spinnerMsg"></span>
          <span>🚀 Bóc Tách & Lưu Vào Database</span>
        </button>

        <div id="resultMsgBox" style="display: none;" class="log-box"></div>
      </div>

      <!-- Panel 2: Layer 1 Automated Ingestion & Database Controls -->
      <div class="card" style="justify-content: space-between;">
        <div>
          <div class="card-header">
            <div class="card-title">
              <span>⚡</span> Quét & Nạp Tự Động (Layer 1)
            </div>
            <span class="badge badge-success">Automated Scan</span>
          </div>

          <p style="font-size: 13px; color: var(--text-muted); margin-top: 10px;">
            Hệ thống sẽ tự động quét toàn bộ nguồn Layer 1 trên máy bạn (Git commits, Coding Agent sessions như Cursor, Claude Code, Antigravity) và bóc tách lưu thẳng vào database mà không cần chạy lệnh terminal.
          </p>

          <div style="margin-top: 14px; display: flex; flex-direction: column; gap: 12px;">
            <div class="form-group">
              <label for="gitRepoPath">Thư mục Git Repo (mặc định workspace hiện tại):</label>
              <input type="text" id="gitRepoPath" value="d:\Working\PTB\Personal_Task_Board-main" placeholder="Đường dẫn repo Git cá nhân...">
            </div>

            <button id="btnScanL1" class="btn btn-primary" style="width: 100%;" onclick="runLayer1Scan()">
              <span class="spinner" id="spinnerScan"></span>
              <span>🔄 Kích Hoạt Quét Toàn Bộ Layer 1 & Lưu Database</span>
            </button>
            <div id="scanResultBox" style="display: none;" class="log-box"></div>
          </div>
        </div>

        <!-- Database Maintenance: Init & Reset -->
        <div style="border-top: 1px solid var(--border); padding-top: 16px; margin-top: 18px;">
          <h5 style="font-size: 13px; font-weight: 700; color: #cbd5e1; margin-bottom: 6px;">Quản Trị Cơ Sở Dữ Liệu Neo4j</h5>
          <p style="font-size: 12px; color: var(--text-dim); margin-bottom: 12px;">Khởi tạo schema constraints hoặc xóa sạch database để chuẩn bị test data mới.</p>
          <div style="display: flex; gap: 10px; align-items: center;">
            <button class="btn btn-secondary" style="flex: 1; padding: 10px 14px; font-size: 13px;" onclick="initDatabase()">
              <span>🛠️ Khởi Tạo Schema (Init)</span>
            </button>
            <button class="btn btn-danger" style="flex: 1; padding: 10px 14px; font-size: 13px;" onclick="resetDatabase()">
              <span>🧹 Reset Sạch Data</span>
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Panel 3: Live Task Board View -->
    <div class="card">
      <div class="card-header">
        <div class="card-title">
          <span>📋</span> Danh Sách Công Việc Trong Cơ Sở Dữ Liệu (Neo4j Single-Store)
        </div>
        <div style="display: flex; gap: 10px; align-items: center;">
          <span id="taskCountBadge" class="badge badge-primary">0 Tasks</span>
          <button class="btn btn-secondary" style="padding: 6px 12px; font-size: 12px;" onclick="loadTasks()">
            <span>🔄 Tải Lại</span>
          </button>
        </div>
      </div>

      <div class="table-responsive">
        <table>
          <thead>
            <tr>
              <th>Tiêu Đề Công Việc</th>
              <th>Người Thực Hiện</th>
              <th>Người Giao Việc</th>
              <th>Hạn Chót (Deadline)</th>
              <th>Độ Tin Cậy</th>
              <th>Trạng Thái</th>
            </tr>
          </thead>
          <tbody id="taskTableBody">
            <tr>
              <td colspan="6" class="empty-state">Đang tải dữ liệu từ Neo4j...</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </main>

  <script>
    // Load tasks on startup
    document.addEventListener("DOMContentLoaded", () => {
      loadTasks();
    });

    async function loadTasks() {
      const tbody = document.getElementById("taskTableBody");
      try {
        const resp = await fetch("/api/tasks");
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        const tasks = await resp.json();
        
        document.getElementById("taskCountBadge").innerText = `${tasks.length} Tasks`;

        if (!tasks || tasks.length === 0) {
          tbody.innerHTML = `<tr><td colspan="6" class="empty-state">Chưa có công việc nào trong database. Hãy nhập tin nhắn ở trên hoặc kích hoạt quét Layer 1!</td></tr>`;
          return;
        }

        tbody.innerHTML = tasks.map(t => `
          <tr>
            <td>
              <div style="font-weight: 700; color: #fff;">${escapeHtml(t.title)}</div>
              <div style="font-size: 12px; color: var(--text-dim); margin-top: 4px;">${escapeHtml(t.description || '')}</div>
            </td>
            <td><span style="font-weight: 600; color: #93c5fd;">${escapeHtml(t.owner_name || 'Chưa gán')}</span></td>
            <td>${escapeHtml(t.requester_name || '-')}</td>
            <td>${t.due_date ? formatDateTime(t.due_date) : '<span style="color:var(--text-dim)">Không có</span>'}</td>
            <td>
              <span class="badge ${t.extraction_confidence >= 0.65 ? 'badge-success' : 'badge-warning'}">
                ${Math.round((t.extraction_confidence || 1.0) * 100)}% (${escapeHtml(t.review_status || 'auto')})
              </span>
            </td>
            <td>
              <span class="badge badge-primary">${escapeHtml(t.status || 'TODO')}</span>
            </td>
          </tr>
        `).join("");
      } catch (err) {
        tbody.innerHTML = `<tr><td colspan="6" class="empty-state" style="color: #f87171;">Lỗi tải dữ liệu: ${err.message}</td></tr>`;
      }
    }

    async function processPersonalMessage() {
      const msg = document.getElementById("msgInput").value.trim();
      const sender = document.getElementById("senderInput").value.trim() || "User";
      const source = document.getElementById("sourceInput").value.trim() || "Web Input";
      const btn = document.getElementById("btnProcessMsg");
      const box = document.getElementById("resultMsgBox");

      if (!msg) {
        alert("Vui lòng nhập nội dung tin nhắn!");
        return;
      }

      btn.disabled = true;
      btn.classList.add("loading");
      box.style.display = "block";
      box.innerText = "⏳ Đang gửi tin nhắn tới AI Pipeline (Gemma 4 26B -> Qwen3 -> Kev -> Neo4j)...";

      try {
        const resp = await fetch("/api/ingest/message", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: msg, sender: sender, source: source })
        });
        const data = await resp.json();

        if (resp.ok && data.status === "success") {
          box.innerText = `[✓] THÀNH CÔNG! Đã lưu task vào Database:\\n` +
            `• Tiêu đề: ${data.task.title}\\n` +
            `• Người làm: ${data.task.owner_name} | Người giao: ${data.task.requester_name}\\n` +
            `• Deadline: ${data.task.due_date || 'Không có'}\\n` +
            `• Độ ưu tiên: ${data.task.priority_level} (Urgency: ${data.task.urgency_score})\\n` +
            `• Trạng thái: ${data.task.status} (${data.task.review_status})`;
          document.getElementById("msgInput").value = "";
          loadTasks();
        } else {
          box.innerText = `[✗] LỖI: ${data.detail || data.error || JSON.stringify(data)}`;
        }
      } catch (err) {
        box.innerText = `[✗] Lỗi kết nối: ${err.message}`;
      } finally {
        btn.disabled = false;
        btn.classList.remove("loading");
      }
    }

    async function runLayer1Scan() {
      const btn = document.getElementById("btnScanL1");
      const box = document.getElementById("scanResultBox");
      const repoPath = document.getElementById("gitRepoPath").value.trim();

      btn.disabled = true;
      btn.classList.add("loading");
      box.style.display = "block";
      box.innerText = "⏳ Đang quét Git commits và Coding Agent logs trên máy...";

      try {
        const resp = await fetch("/api/ingest/layer1", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo_path: repoPath || null, limit: 3 })
        });
        const data = await resp.json();

        if (resp.ok && data.status === "success") {
          let detailStr = "";
          if (data.details) {
            detailStr = Object.entries(data.details).map(([k, v]) => `  • ${k}: ${v} events`).join("\\n");
          }
          box.innerText = `[✓] QUÉT VÀ NẠP LAYER 1 THÀNH CÔNG!\\n` +
            `• Tổng sự kiện thu nạp (Ingested): ${data.ingested}\\n` +
            detailStr + (detailStr ? "\\n" : "") +
            `• Sự kiện đã bóc tách & chuyển đổi thành Tasks: ${data.processed}\\n` +
            `• Đã lưu bền vững vào Neo4j Single-Store.`;
          loadTasks();
        } else {
          box.innerText = `[✗] Lỗi quét: ${data.detail || data.error || JSON.stringify(data)}`;
        }
      } catch (err) {
        box.innerText = `[✗] Lỗi kết nối: ${err.message}`;
      } finally {
        btn.disabled = false;
        btn.classList.remove("loading");
      }
    }

    async function initDatabase() {
      try {
        const resp = await fetch("/api/database/init", { method: "POST" });
        const data = await resp.json();
        if (resp.ok && data.status === "success") {
          alert("✓ Khởi tạo schema và constraints Neo4j thành công 100%!");
          loadTasks();
        } else {
          alert("Lỗi khởi tạo: " + (data.detail || data.error));
        }
      } catch (err) {
        alert("Lỗi kết nối: " + err.message);
      }
    }

    async function resetDatabase() {
      if (!confirm("CẢNH BÁO: Thao tác này sẽ xóa sạch TOÀN BỘ dữ liệu trong database và khôi phục 19 schema constraints ban đầu. Bạn có chắc chắn không?")) {
        return;
      }
      try {
        const resp = await fetch("/api/database/reset", { method: "POST" });
        const data = await resp.json();
        if (resp.ok && data.status === "success") {
          alert("✓ Database đã được xóa sạch và khôi phục constraints sẵn sàng 100%!");
          loadTasks();
        } else {
          alert("Lỗi reset: " + (data.detail || data.error));
        }
      } catch (err) {
        alert("Lỗi kết nối: " + err.message);
      }
    }

    function escapeHtml(str) {
      if (!str) return '';
      return String(str).replace(/[&<>"']/g, function(m) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m];
      });
    }

    function formatDateTime(isoStr) {
      try {
        const d = new Date(isoStr);
        return d.toLocaleString('vi-VN', { dateStyle: 'short', timeStyle: 'short' });
      } catch (e) {
        return isoStr;
      }
    }
  </script>
</body>
</html>
"""
