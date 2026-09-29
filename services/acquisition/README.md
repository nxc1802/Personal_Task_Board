# PTB Acquisition Service (Layer 1)

Dịch vụ thu thập dữ liệu cục bộ cho **Personal Task Board**:
- **Layer 1A (Playwright Network Interception)**: Bắt các gói tin JSON nội bộ từ Teams Web và Outlook Web mà không cần Azure AD Admin Consent.
- **Layer 1B (Coding Agent Watcher)**: Trích xuất lịch sử làm việc, cam kết và quyết định kỹ thuật từ Cursor, Claude Code, và Antigravity IDE.
- Chuẩn hóa toàn bộ sự kiện thành **Contract C12** (`RawEventRecord`, `RawAgentSessionRecord`).
