"""Outlook Web Network Response Interceptor (Layer 1A).

Hook vào sự kiện page.on('response') của Playwright để bắt gói tin JSON nội bộ
của Microsoft Outlook Web (OWA) mà không cần Graph API.
"""

from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Any, Dict, List, Optional
import uuid

from playwright.async_api import Response
from ptb_contracts import ProcessingStatus, RawEventRecord, SourceType
from ptb_acquisition.queue import LocalIngestionQueue

logger = logging.getLogger("ptb.acquisition.playwright.outlook")


class OutlookNetworkInterceptor:
    def __init__(self, queue: Optional[LocalIngestionQueue] = None, tenant_id: str = "local-outlook"):
        self.queue = queue
        self.tenant_id = tenant_id
        self.captured_records: List[RawEventRecord] = []

    async def handle_response(self, response: Response) -> List[RawEventRecord]:
        """Xử lý response trả về từ Outlook Web."""
        url = response.url

        if response.status in (401, 403) and ("outlook.office.com" in url or "outlook.live.com" in url):
            logger.warning(f"Phiên đăng nhập Outlook có thể đã hết hạn (Mã lỗi {response.status}): {url}")
            return []

        is_outlook_api = (
            ("service.svc" in url and "action=" in url)
            or ("api/v2.0/me/messages" in url)
            or ("api/v2.0/me/mailfolders" in url)
            or ("api/v1.0/me/messages" in url)
        )

        if not is_outlook_api or response.status != 200:
            return []

        try:
            payload = await response.json()
        except Exception as e:
            logger.debug(f"Không thể đọc JSON từ response Outlook {url}: {e}")
            return []

        records = self.parse_payload(payload, request_url=url)
        for rec in records:
            self.captured_records.append(rec)
            if self.queue:
                await self.queue.put(rec)

        return records

    def parse_payload(self, payload: Dict[str, Any], request_url: str = "") -> List[RawEventRecord]:
        """Phân tích payload OWA/Outlook và chuẩn hóa thành RawEventRecord."""
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
                        # Conversation -> ConversationNodes -> Items
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
                            # Fallback legacy ConversationItems
                            for ci in conv.get("ConversationItems", []):
                                if isinstance(ci, dict):
                                    items.append(ci)

                        # RootFolder -> Items (FindItem)
                        if "RootFolder" in rm and isinstance(rm["RootFolder"], dict):
                            for rf_item in rm["RootFolder"].get("Items", []):
                                if isinstance(rf_item, dict):
                                    items.append(rf_item)

                        # Direct Items (GetItem)
                        if "Items" in rm and isinstance(rm["Items"], list):
                            for direct_item in rm["Items"]:
                                if isinstance(direct_item, dict):
                                    items.append(direct_item)

            elif "Id" in payload or "id" in payload:
                items = [payload]

        for item in items:
            if not isinstance(item, dict):
                continue

            # Xử lý cả dạng Email Item và Conversation Summary (FindConversation)
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

            # Nội dung
            body_obj = (
                item.get("Body")
                or item.get("body")
                or item.get("Preview")
                or item.get("bodyPreview")
                or subject
                or ""
            )
            if isinstance(body_obj, dict):
                content = body_obj.get("Value") or body_obj.get("content") or ""
            else:
                content = str(body_obj)

            if not content.strip():
                content = subject

            if not content.strip():
                continue

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

            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
            idempotency_key = hashlib.sha256(
                f"{self.tenant_id}:ms_outlook_web:{item_id}:{content_hash}".encode("utf-8")
            ).hexdigest()

            record = RawEventRecord(
                id=f"raw-outlook-{uuid.uuid4().hex[:12]}",
                tenant_id=self.tenant_id,
                source_type=SourceType.MS_OUTLOOK_WEB,
                external_id=item_id,
                parent_external_id=conv_id,
                idempotency_key=idempotency_key,
                event_timestamp=event_ts,
                author_external_id=author_id,
                author_display_name=author_name,
                conversation_or_project_id=conv_id,
                deep_link=f"https://outlook.office.com/mail/deeplink/read/{item_id}",
                raw_payload=item,
                processing_status=ProcessingStatus.PENDING,
                created_at=datetime.now(timezone.utc),
            )
            records.append(record)

        return records
