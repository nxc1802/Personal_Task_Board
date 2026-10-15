"""Outlook Web Network Response Interceptor (Layer 1A).

Hook vào sự kiện page.on('response') của Playwright để bắt gói tin JSON nội bộ
của Microsoft Outlook Web (OWA) mà không cần Graph API:
- Hỗ trợ phân tích mail threads (quoted/forwarded separation, parent_external_id, conversation_or_project_id)
- Bóc tách attachments metadata KHÔNG lưu content nhị phân
- Hỗ trợ xử lý tin nhắn được chỉnh sửa (edited) và tin nhắn bị xóa (deleted)
- Nhận diện lỗi xác thực (401/403/redirect) chuyển sang AUTH_EXPIRED và rate limit (429) chuyển DEGRADED
- Đảm bảo Persist-First và Idempotency Key 64-char SHA-256
"""

import asyncio
from datetime import datetime, timezone
from html.parser import HTMLParser
import hashlib
import inspect
import json
import logging
import re
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional
import uuid

from playwright.async_api import Response
from ptb_contracts import BugCode, ProcessingStatus, RawEventRecord, SourceType, log_bug
from ptb_acquisition.playwright.session import SessionHealthState, transition_session_state
from ptb_acquisition.queue import LocalIngestionQueue

if TYPE_CHECKING:
    from ptb_acquisition.pipeline import AcquisitionPipeline

logger = logging.getLogger("ptb.acquisition.playwright.outlook")


# =============================================================================
# HTML & EMAIL THREAD CLEANING HELPERS
# =============================================================================

class _HTMLTextExtractor(HTMLParser):
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
        cleaned = re.sub(r"[ \t]+", " ", raw)
        cleaned = re.sub(r"\n\s*\n\s*\n+", "\n\n", cleaned)
        return cleaned.strip()

    def get_blockquote_text(self) -> str:
        raw = "".join(self.blockquote_fed)
        cleaned = re.sub(r"[ \t]+", " ", raw)
        cleaned = re.sub(r"\n\s*\n\s*\n+", "\n\n", cleaned)
        return cleaned.strip()


def strip_html_tags(html_content: str) -> Dict[str, str]:
    if not html_content or not isinstance(html_content, str):
        return {"text": "", "blockquote": ""}
    try:
        parser = _HTMLTextExtractor()
        parser.feed(html_content)
        return {"text": parser.get_text(), "blockquote": parser.get_blockquote_text()}
    except Exception as e:
        logger.debug("Lỗi khi parse HTML: %s", e)
        clean = re.sub(r"<[^>]+>", " ", html_content)
        return {"text": re.sub(r"\s+", " ", clean).strip(), "blockquote": ""}


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
    subj_lower = subject.lower().strip()
    is_reply = subj_lower.startswith(("re:", "re :", "trả lời:", "tl:"))
    is_forwarded = subj_lower.startswith(("fwd:", "fw:", "fwd :", "fw :", "chuyển tiếp:"))

    extracted_quote = ""
    if content_type.lower() == "html" or ("<p>" in body_content or "<br" in body_content or "<html" in body_content):
        parsed = strip_html_tags(body_content)
        plain_text = parsed["text"]
        if parsed["blockquote"]:
            extracted_quote = parsed["blockquote"]
    else:
        plain_text = body_content.strip()

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
    sanitized = dict(item)
    if "attachments" in sanitized and isinstance(sanitized["attachments"], list):
        clean_atts = []
        for a in sanitized["attachments"]:
            if isinstance(a, dict):
                clean_a = {k: v for k, v in a.items() if k not in ("contentBytes", "ContentBytes", "rawBytes", "base64")}
                clean_atts.append(clean_a)
            else:
                clean_atts.append(a)
        sanitized["attachments"] = clean_atts
    return sanitized


# =============================================================================
# OUTLOOK NETWORK INTERCEPTOR
# =============================================================================

class OutlookNetworkInterceptor:
    def __init__(
        self,
        queue: Optional[LocalIngestionQueue] = None,
        tenant_id: str = "local-outlook",
        pipeline: Optional["AcquisitionPipeline"] = None,
        raw_event_repo: Optional[Any] = None,
        on_state_change: Optional[Callable[[SessionHealthState], Any]] = None,
        initial_state: SessionHealthState = SessionHealthState.HEALTHY,
    ):
        self.queue = queue
        self.tenant_id = tenant_id
        self.pipeline = pipeline
        self.raw_event_repo = raw_event_repo
        self.on_state_change = on_state_change
        self.captured_records: List[RawEventRecord] = []
        self.source_type = SourceType.MS_OUTLOOK_WEB
        self._session_state: SessionHealthState = initial_state

    @property
    def session_state(self) -> SessionHealthState:
        return self._session_state

    @session_state.setter
    def session_state(self, new_state: SessionHealthState) -> None:
        self.set_session_state(new_state)

    @property
    def health_state(self) -> SessionHealthState:
        return self._session_state

    @health_state.setter
    def health_state(self, new_state: SessionHealthState) -> None:
        self.set_session_state(new_state)

    def set_session_state(self, new_state: SessionHealthState, allow_reset: bool = False) -> None:
        target = transition_session_state(self._session_state, new_state, allow_reset=allow_reset)
        if target == self._session_state and target != new_state:
            return
        self._session_state = target
        if self.on_state_change is not None:
            try:
                res = self.on_state_change(target)
                if inspect.iscoroutine(res):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(res)
                    except RuntimeError:
                        pass
            except Exception as cb_err:
                logger.debug(f"Error calling on_state_change in OutlookNetworkInterceptor: {cb_err}")

    async def handle_response(self, response: Response) -> List[RawEventRecord]:
        """Xử lý response trả về từ Outlook Web theo cơ chế Persist-First."""
        if self._session_state == SessionHealthState.AUTH_EXPIRED:
            logger.debug(f"Bỏ qua response {getattr(response, 'url', '')} vì session đã AUTH_EXPIRED")
            return []

        url = response.url if hasattr(response, "url") else ""
        status = response.status if hasattr(response, "status") else 200

        # 1. Kiểm tra redirect hoặc lỗi xác thực 401/403
        is_login_redirect = "login.microsoftonline.com" in url.lower() or "login.live.com" in url.lower()
        if hasattr(response, "headers") and isinstance(response.headers, dict):
            location = response.headers.get("location", "").lower()
            if "login.microsoftonline.com" in location or "login.live.com" in location:
                is_login_redirect = True

        if status in (401, 403) or is_login_redirect:
            logger.warning(f"Phiên đăng nhập Outlook đã hết hạn (HTTP {status}, redirect={is_login_redirect}): {url}")
            log_bug(
                BugCode.PTB_L1_001,
                subsystem="playwright",
                severity="ERROR",
                message="Microsoft session authentication expired (HTTP 401/403 or login redirect)",
                context={"source_type": self.source_type, "url": response.url, "status": response.status},
                source_type=self.source_type,
                tenant_id=self.tenant_id,
            )
            self.session_state = SessionHealthState.AUTH_EXPIRED
            return []

        # 2. Xử lý Rate Limiting (HTTP 429 Too Many Requests)
        if status == 429:
            logger.warning(f"Outlook Web gặp phản hồi giới hạn tần suất (HTTP 429 Too Many Requests): {url}")
            self.session_state = SessionHealthState.DEGRADED
            return []

        is_outlook_api = (
            ("service.svc" in url and "action=" in url)
            or ("api/v2.0/me/messages" in url)
            or ("api/v2.0/me/mailfolders" in url)
            or ("api/v1.0/me/messages" in url)
        )

        if not is_outlook_api or status != 200:
            return []

        try:
            payload = await response.json()
        except Exception as e:
            logger.debug(f"Không thể đọc JSON từ response Outlook {url}: {e}")
            return []

        try:
            records = self.parse_payload(payload, request_url=url)
        except Exception as e:
            logger.error(f"Lỗi khi parse payload từ {url}: {e}")
            log_bug(
                BugCode.PTB_L1_002,
                subsystem="playwright",
                severity="ERROR",
                message=f"Capture or persist failed for {self.source_type}",
                exc=e,
                source_type=self.source_type,
                tenant_id=self.tenant_id,
            )
            self.session_state = SessionHealthState.DEGRADED
            return []

        persisted_records: List[RawEventRecord] = []

        for rec in records:
            persisted = False
            # Persist-first: gọi trực tiếp pipeline.ingest_event hoặc raw_event_repo.persist_raw_event
            try:
                if self.pipeline is not None:
                    persisted = await self.pipeline.ingest_event(rec)
                elif self.raw_event_repo is not None:
                    if hasattr(self.raw_event_repo, "persist_raw_event"):
                        fn = self.raw_event_repo.persist_raw_event
                    elif hasattr(self.raw_event_repo, "save_raw_event"):
                        fn = self.raw_event_repo.save_raw_event
                    elif hasattr(self.raw_event_repo, "save"):
                        fn = self.raw_event_repo.save
                    else:
                        fn = None

                    if fn:
                        res = await fn(rec) if inspect.iscoroutinefunction(fn) else fn(rec)
                        persisted = res is not False
                    else:
                        persisted = False

                    if persisted and self.queue is not None:
                        await self.queue.put(rec)
                elif self.queue is not None:
                    persisted = await self.queue.put(rec)
                else:
                    persisted = True
            except Exception as e:
                logger.error(f"Lỗi khi persist trực tiếp raw event {rec.id}: {e}")
                log_bug(
                    BugCode.PTB_L1_002,
                    subsystem="playwright",
                    severity="ERROR",
                    message=f"Capture or persist failed for {self.source_type}",
                    exc=e,
                    raw_event_id=rec.id,
                    source_type=self.source_type,
                    tenant_id=self.tenant_id,
                )
                self.session_state = SessionHealthState.DEGRADED
                persisted = False

            if persisted:
                self.captured_records.append(rec)
                persisted_records.append(rec)
            else:
                logger.debug(f"Bỏ qua event {rec.id} do không được lưu hoặc trùng lặp.")

        return persisted_records

    def parse_payload(self, payload: Dict[str, Any], request_url: str = "") -> List[RawEventRecord]:
        """Phân tích payload OWA/Outlook và chuẩn hóa thành RawEventRecord v1."""
        records: List[RawEventRecord] = []

        items = []
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            # Graph/REST format
            if "value" in payload and isinstance(payload["value"], list):
                items = payload["value"]
            # OWA service.svc format
            elif "Body" in payload and isinstance(payload["Body"], dict):
                body = payload["Body"]

                # 1. action=FindConversation
                if "Conversations" in body and isinstance(body["Conversations"], list):
                    for conv in body["Conversations"]:
                        if isinstance(conv, dict):
                            items.append(conv)

                # 2. action=GetConversationItems / GetItem / FindItem
                if "ResponseMessages" in body and isinstance(body["ResponseMessages"], dict):
                    response_messages = body["ResponseMessages"].get("Items", [])
                    for rm in response_messages:
                        if not isinstance(rm, dict):
                            continue
                        if "Conversation" in rm and isinstance(rm["Conversation"], dict):
                            conv = rm["Conversation"]
                            conv_id = str(conv.get("ConversationId", {}).get("Id") or "")
                            nodes = conv.get("ConversationNodes", [])
                            for node in nodes:
                                if isinstance(node, dict):
                                    for ni in node.get("Items", []):
                                        if isinstance(ni, dict):
                                            if "conversationId" not in ni and conv_id:
                                                ni["conversationId"] = conv_id
                                            items.append(ni)
                            for ci in conv.get("ConversationItems", []):
                                if isinstance(ci, dict):
                                    items.append(ci)

                        if "RootFolder" in rm and isinstance(rm["RootFolder"], dict):
                            for rf_item in rm["RootFolder"].get("Items", []):
                                if isinstance(rf_item, dict):
                                    items.append(rf_item)

                        if "Items" in rm and isinstance(rm["Items"], list):
                            for direct_item in rm["Items"]:
                                if isinstance(direct_item, dict):
                                    items.append(direct_item)

            elif "Id" in payload or "id" in payload:
                items = [payload]

        for item in items:
            if not isinstance(item, dict):
                continue

            item_id = str(
                item.get("ItemId", {}).get("Id")
                or item.get("ConversationId", {}).get("Id")
                or item.get("id")
                or item.get("Id")
                or uuid.uuid4().hex
            )
            subject = str(item.get("Subject") or item.get("subject") or item.get("ConversationTopic") or "No Subject")
            conv_id = str(
                item.get("ConversationId", {}).get("Id")
                or item.get("conversationId")
                or item_id
            )

            # Người gửi
            sender_info = item.get("From") or item.get("from") or item.get("Sender") or item.get("LastSender") or {}
            unique_senders = item.get("UniqueSenders") or []
            if isinstance(sender_info, dict):
                mailbox = sender_info.get("Mailbox") or sender_info.get("emailAddress") or sender_info
                author_id = str(mailbox.get("EmailAddress") or mailbox.get("address") or "unknown_sender")
                author_name = mailbox.get("Name") or mailbox.get("name")
            elif isinstance(sender_info, str) and sender_info:
                author_id = sender_info
                author_name = sender_info
            elif unique_senders:
                author_name = ", ".join(unique_senders)
                author_id = unique_senders[0]
            else:
                author_id = "unknown_sender"
                author_name = None

            # Kiểm tra trạng thái bị xóa
            is_deleted = bool(
                item.get("isDeleted")
                or item.get("changeType") == "deleted"
                or "deleteitem" in request_url.lower()
            )

            # Nội dung & tách mail threads (quoted/forwarded)
            body_obj = (
                item.get("Body")
                or item.get("body")
                or item.get("Preview")
                or item.get("bodyPreview")
                or subject
                or ""
            )
            content_type = "text"
            if isinstance(body_obj, dict):
                content = body_obj.get("Value") or body_obj.get("content") or ""
                content_type = str(body_obj.get("contentType") or "text")
            else:
                content = str(body_obj)

            if not content.strip() and not is_deleted:
                content = subject

            if not content.strip() and not is_deleted:
                continue

            cleaned_info = clean_email_content(content, subject=subject, content_type=content_type)
            cleaned_body = cleaned_info["cleaned_body"]
            quoted_text = cleaned_info["quoted_text"]
            is_reply = cleaned_info["is_reply"]
            is_forwarded = cleaned_info["is_forwarded"]

            # Xử lý attachments metadata
            raw_atts = item.get("Attachments") or item.get("attachments") or []
            attachments_meta = extract_attachments_metadata(raw_atts)

            norm_text = cleaned_body if cleaned_body else content.strip()
            if not norm_text and is_deleted:
                norm_text = "[Status: Deleted]"

            full_content_hash = hashlib.sha256(norm_text.encode("utf-8")).hexdigest()
            idempotency_key = hashlib.sha256(
                f"{self.tenant_id}:ms_outlook_web:{item_id}:{full_content_hash[:16]}".encode("utf-8")
            ).hexdigest()

            # Sanitize raw payload để loại bỏ dữ liệu nhị phân dung lượng lớn
            sanitized_item = sanitize_email_payload(item)
            sanitized_item["thread_metadata"] = {
                "conversation_id": conv_id,
                "is_reply": is_reply,
                "is_forwarded": is_forwarded,
                "has_quoted_text": bool(quoted_text),
                "is_deleted": is_deleted,
            }
            if quoted_text:
                sanitized_item["quoted_text"] = quoted_text
            if attachments_meta:
                sanitized_item["attachments_metadata"] = attachments_meta

            # Timestamp
            ts_str = (
                item.get("DateTimeReceived")
                or item.get("receivedDateTime")
                or item.get("createdDateTime")
                or item.get("LastDeliveryTime")
                or item.get("LastDeliveryOrRenewTime")
            )
            if ts_str:
                try:
                    event_ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                except Exception:
                    event_ts = datetime.now(timezone.utc)
            else:
                event_ts = datetime.now(timezone.utc)

            now_utc = datetime.now(timezone.utc)
            record = RawEventRecord(
                id=f"raw-outlook-{uuid.uuid4().hex[:12]}",
                tenant_id=self.tenant_id,
                source_type=SourceType.MS_OUTLOOK_WEB,
                external_id=item_id,
                parent_external_id=conv_id if conv_id != item_id else None,
                idempotency_key=idempotency_key,
                event_timestamp=event_ts,
                captured_at=now_utc,
                author_external_id=author_id,
                author_display_name=author_name,
                conversation_or_project_id=conv_id,
                deep_link=f"https://outlook.office.com/mail/deeplink/read/{item_id}",
                raw_payload=sanitized_item,
                payload_json=json.dumps(sanitized_item, default=str),
                normalized_text=norm_text,
                content_hash=full_content_hash,
                processing_status=ProcessingStatus.PENDING,
                created_at=now_utc,
            )
            records.append(record)

        return records
