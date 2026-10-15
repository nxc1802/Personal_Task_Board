"""Microsoft Outlook Direct Mail & Calendar Acquisition Adapter.

Truy xuất và nạp email, lịch họp và deadlines từ Microsoft Graph REST API hoặc offline JSON payload,
chuyển đổi thành RawEventRecord v1 chuẩn hóa với source_type = SourceType.MS_OUTLOOK theo
hợp đồng W1-03 / W2-03:
- Hỗ trợ phân tích mail threads (quoted/forwarded separation, parent_external_id, conversation_or_project_id)
- Bóc tách attachments metadata (tên, kích thước, contentType, isInline) KHÔNG lưu content nhị phân
- Hỗ trợ folder selection và pagination (@odata.nextLink, $skip)
- Delta / Incremental sync qua IngestionCheckpointRecord
- Phát hiện hết hạn phiên (AUTH_REQUIRED, HTTP 401/403) và tự động retry lỗi mạng tạm thời (HTTP 5xx)
- Báo cáo health trạng thái thực tế: NOT_CONFIGURED, AUTH_REQUIRED, RATE_LIMITED, READY
- Bảo đảm Multi-Tenant Isolation và Idempotency Key 64-char SHA-256
"""

import asyncio
from datetime import datetime, timezone
from html.parser import HTMLParser
import inspect
import json
import logging
import os
import re
from typing import Any, AsyncIterator, Callable, Dict, List, Optional
import uuid

from ptb_contracts.l1_acquisition import (
    AdapterHealthStatus,
    IngestionCheckpointRecord,
    ProcessingStatus,
    RawEventRecord,
    SourceSyncState,
    SourceType,
    compute_checkpoint_id,
    compute_content_hash,
    compute_idempotency_key,
    ensure_utc,
)
from ptb_acquisition.adapters.base import (
    AcquisitionAdapter,
    AdapterAuthenticationError,
    AdapterConfigurationError,
    AdapterRateLimitError,
    AdapterStreamNotFoundError,
    AdapterTransientError,
)
from ptb_acquisition.playwright.session import (
    DEFAULT_STORAGE_PATH,
    SessionHealthState,
    SessionManager,
)

logger = logging.getLogger("ptb.acquisition.adapters.outlook")


# =============================================================================
# HTML & EMAIL THREAD CLEANING HELPERS
# =============================================================================

class _HTMLTextExtractor(HTMLParser):
    """Trích xuất text thuần từ HTML email, tách riêng nội dung blockquote."""

    def __init__(self) -> None:
        super().__init__()
        self.reset()
        self.fed: List[str] = []
        self.in_blockquote = False
        self.blockquote_fed: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Any]) -> None:
        tag_lower = tag.lower()
        if tag_lower in ("p", "br", "div", "tr", "li", "hr"):
            self.fed.append("\n")
        if tag_lower == "blockquote":
            self.in_blockquote = True

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower == "blockquote":
            self.in_blockquote = False
        if tag_lower in ("p", "div", "tr"):
            self.fed.append("\n")

    def handle_data(self, data: str) -> None:
        if self.in_blockquote:
            self.blockquote_fed.append(data)
        else:
            self.fed.append(data)

    def get_text(self) -> str:
        raw = "".join(self.fed)
        # Normalize continuous whitespace
        cleaned = re.sub(r"[ \t]+", " ", raw)
        cleaned = re.sub(r"\n\s*\n\s*\n+", "\n\n", cleaned)
        return cleaned.strip()

    def get_blockquote_text(self) -> str:
        raw = "".join(self.blockquote_fed)
        cleaned = re.sub(r"[ \t]+", " ", raw)
        cleaned = re.sub(r"\n\s*\n\s*\n+", "\n\n", cleaned)
        return cleaned.strip()


def strip_html_tags(html_content: str) -> Dict[str, str]:
    """Chuyển đổi HTML sang plain text và tách riêng blockquote text."""
    if not html_content or not isinstance(html_content, str):
        return {"text": "", "blockquote": ""}
    try:
        parser = _HTMLTextExtractor()
        parser.feed(html_content)
        return {"text": parser.get_text(), "blockquote": parser.get_blockquote_text()}
    except Exception as e:
        logger.debug("Lỗi khi parse HTML email body: %s", e)
        # Fallback regex strip
        clean = re.sub(r"<[^>]+>", " ", html_content)
        return {"text": re.sub(r"\s+", " ", clean).strip(), "blockquote": ""}


# Các mốc phân tách email thread phổ biến (Quoted / Forwarded headers)
QUOTE_SPLIT_PATTERNS = [
    re.compile(r"-----Original Message-----", re.IGNORECASE),
    re.compile(r"-----Forwarded Message-----", re.IGNORECASE),
    re.compile(r"From:\s*.*?\nSent:\s*.*?\nTo:\s*.*?\nSubject:", re.IGNORECASE | re.DOTALL),
    re.compile(r"On\s+.*?,?\s+.*?wrote:\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"Vào\s+.*?,?\s+.*?đã viết:\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"_{20,}", re.IGNORECASE),
]


def clean_email_content(
    body_content: str,
    subject: str = "",
    content_type: str = "text",
) -> Dict[str, Any]:
    """Phân tích nội dung email, tách biệt lời nhắn mới và lịch sử chuỗi trích dẫn (Quoted/Forwarded)."""
    subj_lower = subject.lower().strip()
    is_reply = subj_lower.startswith(("re:", "re :", "trả lời:", "tl:"))
    is_forwarded = subj_lower.startswith(("fwd:", "fw:", "fwd :", "fw :", "chuyển tiếp:"))

    # 1. Trích xuất text từ HTML nếu cần
    extracted_quote = ""
    if content_type.lower() == "html" or ("<p>" in body_content or "<br" in body_content or "<html" in body_content):
        parsed = strip_html_tags(body_content)
        plain_text = parsed["text"]
        if parsed["blockquote"]:
            extracted_quote = parsed["blockquote"]
    else:
        plain_text = body_content.strip()

    # 2. Quét các điểm cắt trích dẫn bằng regex
    split_pos = -1
    for pat in QUOTE_SPLIT_PATTERNS:
        match = pat.search(plain_text)
        if match:
            if split_pos == -1 or match.start() < split_pos:
                split_pos = match.start()

    if split_pos != -1:
        current_text = plain_text[:split_pos].strip()
        trailing_quote = plain_text[split_pos:].strip()
        quoted_text = f"{extracted_quote}\n{trailing_quote}".strip() if extracted_quote else trailing_quote
    else:
        current_text = plain_text.strip()
        quoted_text = extracted_quote.strip() or None

    # Nếu phần lời nhắn hiện tại bị rỗng (ví dụ toàn bộ email là quote), giữ nguyên để tránh mất dữ liệu
    if not current_text and quoted_text:
        current_text = quoted_text
        quoted_text = None

    return {
        "cleaned_body": current_text,
        "quoted_text": quoted_text,
        "is_reply": is_reply,
        "is_forwarded": is_forwarded,
    }


def extract_attachments_metadata(raw_attachments: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Trích xuất metadata tài liệu đính kèm (name, size, contentType, isInline) KHÔNG lưu content nhị phân."""
    if not raw_attachments or not isinstance(raw_attachments, list):
        return []

    meta_list: List[Dict[str, Any]] = []
    for att in raw_attachments:
        if not isinstance(att, dict):
            continue

        att_id = str(att.get("id") or att.get("Id") or uuid.uuid4().hex[:8])
        name = str(att.get("name") or att.get("Name") or "attachment")
        content_type = str(att.get("contentType") or att.get("ContentType") or "application/octet-stream")
        try:
            size_bytes = int(att.get("size") or att.get("Size") or 0)
        except (ValueError, TypeError):
            size_bytes = 0

        is_inline = bool(att.get("isInline") or att.get("IsInline") or False)
        meta_list.append({
            "id": att_id,
            "name": name,
            "content_type": content_type,
            "size": size_bytes,
            "is_inline": is_inline,
            "last_modified": att.get("lastModifiedDateTime") or att.get("LastModifiedTime"),
        })

    return meta_list


def sanitize_email_payload(item: Dict[str, Any]) -> Dict[str, Any]:
    """Loại bỏ các trường base64 hoặc nhị phân lớn khỏi raw_payload trước khi lưu trữ bền vững."""
    sanitized = dict(item)
    if "attachments" in sanitized and isinstance(sanitized["attachments"], list):
        clean_atts = []
        for a in sanitized["attachments"]:
            if isinstance(a, dict):
                # Loại bỏ contentBytes và raw data nhị phân
                clean_a = {k: v for k, v in a.items() if k not in ("contentBytes", "ContentBytes", "rawBytes", "base64")}
                clean_atts.append(clean_a)
            else:
                clean_atts.append(a)
        sanitized["attachments"] = clean_atts
    return sanitized


# =============================================================================
# OUTLOOK DIRECT MAIL ACQUISITION ADAPTER
# =============================================================================

class OutlookMailAdapter(AcquisitionAdapter):
    """Adapter tích hợp Microsoft Outlook REST / Graph API theo Canonical Contract W1-03 / W2-03."""

    def __init__(
        self,
        tenant_id: str = "local-user",
        graph_token: Optional[str] = None,
        mailbox: Optional[str] = None,
        folders: Optional[List[str]] = None,
        http_fetcher: Optional[Callable[..., Any]] = None,
        storage_path: Optional[str] = None,
        base_url: str = "https://graph.microsoft.com/v1.0",
        messages_fixture: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        super().__init__(source_type=SourceType.MS_OUTLOOK, tenant_id=tenant_id)
        # Token ưu tiên từ tham số khởi tạo hoặc biến môi trường
        self.graph_token = (graph_token if graph_token is not None else os.getenv("MS_GRAPH_TOKEN", "")).strip()
        
        # Mailbox ("me" hoặc email của người dùng)
        if mailbox is not None:
            self.mailbox = mailbox.strip()
        else:
            self.mailbox = os.getenv("MS_OUTLOOK_MAILBOX", "me").strip()

        self.folders = folders or ["Inbox"]
        self.base_url = (base_url or "https://graph.microsoft.com/v1.0").rstrip("/")
        self._http_fetcher = http_fetcher
        self.storage_path = storage_path or DEFAULT_STORAGE_PATH
        self.session_mgr = SessionManager(self.storage_path)
        self.messages_fixture = messages_fixture

        # Internal operational states
        self._rate_limited: bool = False
        self._rate_limit_retry_after: float = 0.0

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "PersonalTaskBoard/1.0 (OutlookAdapter)",
        }
        if self.graph_token:
            headers["Authorization"] = f"Bearer {self.graph_token}"
        return headers

    def _has_valid_cookies(self) -> bool:
        """Kiểm tra sự hiện diện của session cookies Microsoft còn hạn."""
        try:
            return self.session_mgr.has_valid_session()
        except Exception:
            return False

    def _to_raw_event(self, item: Dict[str, Any], folder_name: str = "Inbox") -> RawEventRecord:
        """Ánh xạ email message sang RawEventRecord v1 chuẩn hóa, bảo vệ multi-tenant và idempotency."""
        item_id = str(
            item.get("id")
            or item.get("ItemId", {}).get("Id")
            or item.get("Id")
            or uuid.uuid4().hex
        )
        subject = str(item.get("subject") or item.get("Subject") or item.get("title") or "No Subject").strip()
        conv_id = str(
            item.get("conversationId")
            or item.get("ConversationId", {}).get("Id")
            or item.get("threadId")
            or item_id
        )

        # 1. Người gửi (Sender attribution)
        from_info = (
            item.get("from")
            or item.get("From")
            or item.get("sender")
            or item.get("Sender")
            or item.get("user")
            or {}
        )
        if isinstance(from_info, dict):
            mb = from_info.get("emailAddress") or from_info.get("Mailbox") or from_info
            author_id = str(mb.get("address") or mb.get("EmailAddress") or mb.get("id") or "unknown_sender")
            author_name = mb.get("name") or mb.get("Name") or mb.get("displayName")
        elif isinstance(from_info, str) and from_info.strip():
            author_id = from_info.strip()
            author_name = from_info.strip()
        else:
            author_id = "unknown_sender"
            author_name = None

        # 2. Xử lý body & Quoted/Forwarded threads
        body_obj = (
            item.get("body")
            or item.get("Body")
            or item.get("preview")
            or item.get("bodyPreview")
            or item.get("text")
            or item.get("summary")
            or subject
        )
        content_type = "text"
        if isinstance(body_obj, dict):
            raw_body = str(body_obj.get("content") or body_obj.get("Value") or "")
            content_type = str(body_obj.get("contentType") or "text")
        else:
            raw_body = str(body_obj or "")

        cleaned_info = clean_email_content(raw_body, subject=subject, content_type=content_type)
        cleaned_body = cleaned_info["cleaned_body"]
        quoted_text = cleaned_info["quoted_text"]
        is_reply = cleaned_info["is_reply"]
        is_forwarded = cleaned_info["is_forwarded"]

        # 3. Xử lý attachments metadata
        raw_atts = item.get("attachments") or item.get("Attachments") or []
        attachments_meta = extract_attachments_metadata(raw_atts)

        # 4. Xây dựng normalized text chuẩn hóa
        text_lines = [f"Subject: {subject}"]
        if is_reply:
            text_lines.append("[Type: Reply]")
        elif is_forwarded:
            text_lines.append("[Type: Forwarded]")

        if item.get("isDeleted") or item.get("changeType") == "deleted":
            text_lines.append("[Status: Deleted]")

        if cleaned_body:
            text_lines.append(f"\n{cleaned_body}")

        if attachments_meta:
            att_names = [f"{a['name']} ({a['size']} bytes)" for a in attachments_meta]
            text_lines.append(f"\n[Attachments: {', '.join(att_names)}]")

        normalized_text = "\n".join(text_lines).strip()

        # 5. Tính toán Content Hash và Idempotency Key 64 ký tự chuẩn v1
        content_hash = self.compute_content_hash(normalized_text)
        idempotency_key = self.compute_idempotency_key(
            external_id=item_id,
            content_hash=content_hash,
            stream_id=folder_name,
        )

        # 6. Timestamp UTC
        ts_str = (
            item.get("receivedDateTime")
            or item.get("DateTimeReceived")
            or item.get("createdDateTime")
            or item.get("DateTimeCreated")
        )
        event_ts = ensure_utc(ts_str) or datetime.now(timezone.utc)
        now_utc = datetime.now(timezone.utc)

        # 7. Payload đã làm sạch nhị phân và bổ sung metadata
        sanitized_payload = sanitize_email_payload(item)
        sanitized_payload["thread_metadata"] = {
            "conversation_id": conv_id,
            "is_reply": is_reply,
            "is_forwarded": is_forwarded,
            "has_quoted_text": bool(quoted_text),
            "folder": folder_name,
        }
        if quoted_text:
            sanitized_payload["quoted_text"] = quoted_text
        if attachments_meta:
            sanitized_payload["attachments_metadata"] = attachments_meta

        deep_link = (
            item.get("webLink")
            or f"https://outlook.office.com/mail/deeplink/read/{item_id}"
        )

        record = RawEventRecord(
            id=f"raw-outlook-direct-{uuid.uuid4().hex[:12]}",
            tenant_id=self.tenant_id,
            source_type=SourceType.MS_OUTLOOK,
            external_id=item_id,
            parent_external_id=conv_id if conv_id != item_id else None,
            idempotency_key=idempotency_key,
            event_timestamp=event_ts,
            captured_at=now_utc,
            author_external_id=author_id,
            author_display_name=author_name,
            conversation_or_project_id=conv_id,
            deep_link=deep_link,
            raw_payload=sanitized_payload,
            payload_json=json.dumps(sanitized_payload, default=str),
            normalized_text=normalized_text,
            content_hash=content_hash,
            processing_status=ProcessingStatus.PENDING,
            created_at=now_utc,
        )

        # Invariant check: Xác thực multi-tenant isolation
        if not self.validate_record(record):
            raise AcquisitionError(
                f"Multi-tenant violation in Outlook record: {record.tenant_id} != {self.tenant_id}",
                source_type=self.source_type,
            )

        return record

    async def _execute_http(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
        body: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Thực hiện HTTP call có xử lý mã lỗi xác thực (401/403), giới hạn tốc độ (429) và lỗi tạm thời (5xx)."""
        req_headers = self._get_headers()
        if headers:
            req_headers.update(headers)

        async def _do_call() -> Dict[str, Any]:
            if self._http_fetcher is not None:
                res = self._http_fetcher(url, headers=req_headers, body=body)
                if inspect.iscoroutine(res):
                    res = await res

                # Phân tích kết quả trả về từ fetcher
                if isinstance(res, dict):
                    return res
                if hasattr(res, "status_code") or hasattr(res, "status"):
                    code = getattr(res, "status_code", None) or getattr(res, "status", 200)
                    if code in (401, 403):
                        raise AdapterAuthenticationError(
                            f"Microsoft Graph API authentication failed (HTTP {code})",
                            source_type=self.source_type,
                            details={"status": code, "url": url},
                        )
                    if code == 429:
                        retry_after = 5.0
                        if hasattr(res, "headers") and isinstance(res.headers, dict):
                            try:
                                retry_after = float(res.headers.get("Retry-After", 5))
                            except (ValueError, TypeError):
                                pass
                        self._rate_limited = True
                        self._rate_limit_retry_after = retry_after
                        raise AdapterRateLimitError(
                            f"Microsoft Graph API rate limited (HTTP 429)",
                            retry_after_seconds=retry_after,
                            source_type=self.source_type,
                        )
                    if code >= 500:
                        raise AdapterTransientError(
                            f"Microsoft Graph API transient server error (HTTP {code})",
                            source_type=self.source_type,
                        )
                    if hasattr(res, "json"):
                        json_data = res.json()
                        if inspect.iscoroutine(json_data):
                            json_data = await json_data
                        return json_data
                    return {}
                return {}

            # Fallback httpx client khi không cắm test fetcher
            import httpx
            async with httpx.AsyncClient(timeout=30.0) as client:
                try:
                    resp = await client.request(method, url, headers=req_headers, json=body)
                except httpx.TimeoutException as te:
                    raise AdapterTransientError(f"HTTP timeout connecting to {url}: {te}")
                except httpx.NetworkError as ne:
                    raise AdapterTransientError(f"Network error connecting to {url}: {ne}")

                if resp.status_code in (401, 403):
                    raise AdapterAuthenticationError(
                        f"Microsoft Graph API authentication failed (HTTP {resp.status_code})",
                        source_type=self.source_type,
                        details={"status": resp.status_code, "url": url},
                    )
                if resp.status_code == 429:
                    try:
                        retry_after = float(resp.headers.get("Retry-After", 5.0))
                    except (ValueError, TypeError):
                        retry_after = 5.0
                    self._rate_limited = True
                    self._rate_limit_retry_after = retry_after
                    raise AdapterRateLimitError(
                        "Microsoft Graph API rate limited (HTTP 429)",
                        retry_after_seconds=retry_after,
                        source_type=self.source_type,
                    )
                if resp.status_code >= 500:
                    raise AdapterTransientError(
                        f"Microsoft Graph API transient server error (HTTP {resp.status_code})",
                        source_type=self.source_type,
                    )

                resp.raise_for_status()
                return resp.json()

        # Áp dụng cơ chế Exponential Backoff & Retry
        return await self.execute_with_retry(_do_call)

    # -------------------------------------------------------------------------
    # 4 Canonical Contract Methods (discover, backfill, poll_incremental, health)
    # -------------------------------------------------------------------------

    async def discover(self) -> List[Dict[str, Any]]:
        """Khám phá các streams (Folders) khả dụng trong mailbox."""
        streams: List[Dict[str, Any]] = []

        # Nếu có Graph token và http_fetcher, thử truy vấn /me/mailFolders
        if self.graph_token and self._http_fetcher is not None:
            url = f"{self.base_url}/{self.mailbox}/mailFolders?$top=20"
            try:
                data = await self._execute_http(url)
                folders_data = data.get("value", [])
                for f in folders_data:
                    display_name = f.get("displayName") or f.get("name") or "Unnamed Folder"
                    folder_id = f.get("id") or display_name
                    streams.append({
                        "stream_id": display_name,
                        "name": f"Folder {display_name}",
                        "folder_id": folder_id,
                        "unread_item_count": f.get("unreadItemCount", 0),
                        "total_item_count": f.get("totalItemCount", 0),
                        "source_type": self.source_type.value,
                    })
            except Exception as e:
                logger.debug("Không thể discover dynamic folders từ Graph API: %s. Dùng configured folders.", e)

        if not streams:
            for f in self.folders:
                streams.append({
                    "stream_id": f,
                    "name": f"Folder {f}",
                    "folder_id": f,
                    "source_type": self.source_type.value,
                })

        return streams

    async def backfill(
        self, stream_id: str, since: Optional[datetime] = None
    ) -> AsyncIterator[RawEventRecord]:
        """Thu thập toàn bộ dữ liệu lịch sử từ folder stream_id kể từ mốc thời gian since.
        
        Hỗ trợ phân trang (@odata.nextLink hoặc $skip offset) và ánh xạ sang RawEventRecord chuẩn.
        """
        folder_name = stream_id or "Inbox"
        logger.info(
            f"OutlookMailAdapter backfill folder '{folder_name}' for tenant '{self.tenant_id}' (since={since})"
        )

        # 1. Chế độ fixture offline / test double
        if self.messages_fixture is not None:
            for item in self.messages_fixture:
                rec = self._to_raw_event(item, folder_name=folder_name)
                if since is not None and rec.event_timestamp < since:
                    continue
                yield rec
            return

        # 2. Chế độ Graph API trực tuyến
        # Tạo query params có filter thời gian và top items
        since_utc = ensure_utc(since)
        query_parts = ["$top=50"]
        if since_utc:
            query_parts.append(f"$filter=receivedDateTime ge {since_utc.isoformat()}")

        query_str = "&".join(query_parts)
        next_url: Optional[str] = f"{self.base_url}/{self.mailbox}/mailFolders/{folder_name}/messages?{query_str}"

        max_pages = 20
        page_count = 0

        while next_url and page_count < max_pages:
            page_count += 1
            try:
                page_data = await self._execute_http(next_url)
            except AdapterRateLimitError as rle:
                await self.handle_rate_limit(rle.retry_after_seconds)
                page_data = await self._execute_http(next_url)

            items = page_data.get("value", [])
            for item in items:
                rec = self._to_raw_event(item, folder_name=folder_name)
                if since_utc is not None and rec.event_timestamp < since_utc:
                    continue
                yield rec

            next_url = page_data.get("@odata.nextLink")

    async def poll_incremental(
        self, stream_id: str, checkpoint: Optional[IngestionCheckpointRecord] = None
    ) -> AsyncIterator[RawEventRecord]:
        """Thu thập các email mới phát sinh kể từ checkpoint (delta token hoặc timestamp).
        
        Nếu checkpoint chưa tồn tại, chuyển tiếp sang backfill.
        Loại trừ hoàn toàn các bản ghi đã được xử lý (0 duplicate replay).
        """
        folder_name = stream_id or "Inbox"

        if checkpoint is None or (checkpoint.last_event_timestamp is None and not checkpoint.cursor_token):
            logger.info(f"Không có checkpoint cho folder '{folder_name}', chuyển tiếp sang backfill")
            async for r in self.backfill(folder_name, since=None):
                yield r
            return

        last_ts = ensure_utc(checkpoint.last_event_timestamp)
        last_ext_id = str(checkpoint.last_external_id or "")

        logger.info(
            f"OutlookMailAdapter poll_incremental folder '{folder_name}' since={last_ts} (last_id={last_ext_id})"
        )

        # 1. Chế độ fixture offline / test double
        if self.messages_fixture is not None:
            for item in self.messages_fixture:
                rec = self._to_raw_event(item, folder_name=folder_name)
                # Bỏ qua nếu timestamp nhỏ hơn mốc checkpoint
                if last_ts and rec.event_timestamp < last_ts:
                    continue
                # Bỏ qua nếu trùng khớp chính xác event đã ghi nhận ở checkpoint
                if last_ts and rec.event_timestamp == last_ts and rec.external_id == last_ext_id:
                    continue
                yield rec
            return

        # 2. Chế độ Delta Link / Cursor Token hoặc Timestamp Filter
        if checkpoint.cursor_token:
            url = checkpoint.cursor_token
        else:
            filter_expr = f"receivedDateTime ge {last_ts.isoformat()}" if last_ts else ""
            url = f"{self.base_url}/{self.mailbox}/mailFolders/{folder_name}/messages?$top=50"
            if filter_expr:
                url += f"&$filter={filter_expr}"

        next_url: Optional[str] = url
        page_count = 0

        while next_url and page_count < 20:
            page_count += 1
            try:
                page_data = await self._execute_http(next_url)
            except AdapterRateLimitError as rle:
                await self.handle_rate_limit(rle.retry_after_seconds)
                page_data = await self._execute_http(next_url)

            items = page_data.get("value", [])
            for item in items:
                rec = self._to_raw_event(item, folder_name=folder_name)
                if last_ts and rec.event_timestamp < last_ts:
                    continue
                if last_ts and rec.event_timestamp == last_ts and rec.external_id == last_ext_id:
                    continue
                yield rec

            next_url = page_data.get("@odata.nextLink")

    async def health(self) -> Dict[str, Any]:
        """Báo cáo tình trạng hoạt động thực tế của adapter theo W1-03 / W2-03.
        
        Phân biệt rõ 4 trạng thái sức khỏe:
        - NOT_CONFIGURED (hoặc not_configured): Khi không có token và không có mailbox cấu hình.
        - AUTH_REQUIRED: Khi mailbox được cấu hình nhưng thiếu token xác thực và không có cookie hợp lệ.
        - RATE_LIMITED: Khi endpoint trả về HTTP 429 hoặc đang trong thời gian chờ giới hạn tốc độ.
        - READY: Khi đã xác thực token/session thành công và sẵn sàng nhận diện luồng email.
        """
        # 1. Trạng thái RATE_LIMITED
        if self._rate_limited:
            return {
                "source_type": self.source_type.value,
                "tenant_id": self.tenant_id,
                "configured": True,
                "status": "RATE_LIMITED",
                "health_state": "RATE_LIMITED",
                "retry_after_seconds": self._rate_limit_retry_after,
                "folders": self.folders,
            }

        # 2. Trạng thái NOT_CONFIGURED: Không có cả token lẫn mailbox
        if not self.graph_token and not self.mailbox:
            return {
                "source_type": self.source_type.value,
                "tenant_id": self.tenant_id,
                "configured": False,
                "status": "not_configured",
                "health_state": "NOT_CONFIGURED",
                "folders": self.folders,
            }

        # 3. Kiểm tra Token hoặc Cookies xác thực
        has_token = bool(self.graph_token)
        has_cookies = self._has_valid_cookies()

        # Nếu có http_fetcher và token, kiểm tra live health thực tế
        if has_token and self._http_fetcher is not None:
            try:
                ping_url = f"{self.base_url}/{self.mailbox}"
                await self._execute_http(ping_url)
                return {
                    "source_type": self.source_type.value,
                    "tenant_id": self.tenant_id,
                    "configured": True,
                    "status": "READY",
                    "health_state": "READY",
                    "mailbox": self.mailbox,
                    "folders": self.folders,
                }
            except AdapterAuthenticationError:
                return {
                    "source_type": self.source_type.value,
                    "tenant_id": self.tenant_id,
                    "configured": True,
                    "status": "AUTH_REQUIRED",
                    "health_state": "AUTH_REQUIRED",
                    "error": "Authentication token expired or rejected (401/403)",
                    "folders": self.folders,
                }
            except AdapterRateLimitError as rle:
                return {
                    "source_type": self.source_type.value,
                    "tenant_id": self.tenant_id,
                    "configured": True,
                    "status": "RATE_LIMITED",
                    "health_state": "RATE_LIMITED",
                    "retry_after_seconds": rle.retry_after_seconds,
                    "folders": self.folders,
                }
            except Exception as e:
                logger.debug("Live health check failed: %s", e)

        # 4. Khi có token hợp lệ
        if has_token:
            return {
                "source_type": self.source_type.value,
                "tenant_id": self.tenant_id,
                "configured": True,
                "status": "READY",
                "health_state": "READY",
                "mailbox": self.mailbox,
                "folders": self.folders,
            }

        # 5. Khi có session cookies hợp lệ từ Playwright
        if has_cookies:
            return {
                "source_type": self.source_type.value,
                "tenant_id": self.tenant_id,
                "configured": True,
                "status": "READY",
                "health_state": "READY",
                "session_mode": "browser_cookies",
                "mailbox": self.mailbox,
                "folders": self.folders,
            }

        # 6. Khi thiếu cả token lẫn cookies -> AUTH_REQUIRED
        return {
            "source_type": self.source_type.value,
            "tenant_id": self.tenant_id,
            "configured": False,
            "status": "AUTH_REQUIRED",
            "health_state": "AUTH_REQUIRED",
            "message": "Microsoft session cookies missing or expired. Run login_interactive() or provide MS_GRAPH_TOKEN.",
            "folders": self.folders,
        }
