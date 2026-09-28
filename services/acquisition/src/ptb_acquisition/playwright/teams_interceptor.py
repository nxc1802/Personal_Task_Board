"""Teams Web Network Response Interceptor (Layer 1A).

Hook vào sự kiện page.on('response') của Playwright để bắt gói tin JSON nội bộ
của Microsoft Teams Web mà không cần Azure AD Admin Consent.
"""

from datetime import datetime, timezone
import hashlib
import inspect
import json
import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional
import uuid

from playwright.async_api import Response
from ptb_contracts import ProcessingStatus, RawEventRecord, SourceType
from ptb_acquisition.queue import LocalIngestionQueue

if TYPE_CHECKING:
    from ptb_acquisition.pipeline import AcquisitionPipeline

logger = logging.getLogger("ptb.acquisition.playwright.teams")


class TeamsNetworkInterceptor:
    def __init__(
        self,
        queue: Optional[LocalIngestionQueue] = None,
        tenant_id: str = "local-teams",
        pipeline: Optional["AcquisitionPipeline"] = None,
        raw_event_repo: Optional[Any] = None,
    ):
        self.queue = queue
        self.tenant_id = tenant_id
        self.pipeline = pipeline
        self.raw_event_repo = raw_event_repo
        self.captured_records: List[RawEventRecord] = []

    async def handle_response(self, response: Response) -> List[RawEventRecord]:
        """Xử lý mọi network response trả về từ Teams Web theo cơ chế Persist-First."""
        url = response.url

        # Kiểm tra mã lỗi xác thực để cảnh báo phiên hết hạn
        if response.status in (401, 403) and ("teams.microsoft.com" in url or "api.spaces.skype.com" in url):
            logger.warning(f"Phiên đăng nhập Teams có thể đã hết hạn (Mã lỗi {response.status}): {url}")
            return []

        # Lọc các URL endpoint chứa dữ liệu tin nhắn chat
        is_teams_msg_api = (
            ("api/chats" in url and "messages" in url)
            or ("conversations" in url and "messages" in url)
            or ("threads" in url and "messages" in url)
        )

        if not is_teams_msg_api or response.status != 200:
            return []

        try:
            payload = await response.json()
        except Exception as e:
            logger.debug(f"Không thể đọc JSON từ response {url}: {e}")
            return []

        records = self.parse_payload(payload, request_url=url)
        persisted_records: List[RawEventRecord] = []

        for rec in records:
            persisted = False
            # Persist-first: gọi trực tiếp pipeline.ingest_event hoặc raw_event_repo.persist_raw_event
            if self.pipeline is not None:
                persisted = await self.pipeline.ingest_event(rec)
            elif self.raw_event_repo is not None:
                try:
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
                except Exception as e:
                    logger.error(f"Lỗi khi persist trực tiếp raw event {rec.id}: {e}")
                    persisted = False
            elif self.queue is not None:
                # Fallback backward-compatible khi chỉ truyền queue
                persisted = await self.queue.put(rec)
            else:
                # Chế độ standalone test không cắm kho lưu trữ
                persisted = True

            # Chỉ coi event là captured sau khi đã được lưu trữ bền vững vào Neo4j (Durable Persist-First)
            if persisted:
                self.captured_records.append(rec)
                persisted_records.append(rec)
            else:
                logger.debug(f"Bỏ qua event {rec.id} do không được lưu hoặc trùng lặp.")

        return persisted_records

    def parse_payload(self, payload: Dict[str, Any], request_url: str = "") -> List[RawEventRecord]:
        """Phân tích payload JSON nội bộ của Teams và chuẩn hóa thành RawEventRecord."""
        records: List[RawEventRecord] = []

        # Teams có thể trả về danh sách messages trong trường 'messages' hoặc 'value' hoặc payload là 1 message
        raw_messages = []
        if isinstance(payload, list):
            raw_messages = payload
        elif isinstance(payload, dict):
            if "messages" in payload and isinstance(payload["messages"], list):
                raw_messages = payload["messages"]
            elif "value" in payload and isinstance(payload["value"], list):
                raw_messages = payload["value"]
            elif "id" in payload and ("content" in payload or "body" in payload):
                raw_messages = [payload]

        for msg in raw_messages:
            if not isinstance(msg, dict):
                continue

            msg_id = str(msg.get("id") or msg.get("sequenceId") or uuid.uuid4().hex)
            conv_id = str(msg.get("conversationId") or msg.get("threadId") or "unknown_conversation")
            
            # Người gửi
            props = msg.get("properties") if isinstance(msg.get("properties"), dict) else {}
            im_display = (
                msg.get("imdisplayname")
                or msg.get("imDisplayName")
                or msg.get("displayName")
                or msg.get("userDisplayName")
                or props.get("imdisplayname")
                or props.get("imDisplayName")
                or props.get("userDisplayName")
            )
            from_info = msg.get("from") or {}
            if isinstance(from_info, dict):
                author_id = str(from_info.get("id") or from_info.get("userPrincipalName") or "unknown_author")
                author_name = from_info.get("displayName") or im_display
            else:
                raw_from = str(from_info)
                if "contacts/8:orgid:" in raw_from:
                    author_id = raw_from.split("contacts/8:orgid:")[-1].split("/")[0]
                elif "contacts/8:" in raw_from:
                    author_id = raw_from.split("contacts/8:")[-1].split("/")[0]
                else:
                    author_id = raw_from
                author_name = im_display

            # Nội dung
            body_info = msg.get("body") or msg.get("content") or {}
            if isinstance(body_info, dict):
                content_text = body_info.get("content", "")
            else:
                content_text = str(body_info)

            if not content_text.strip():
                continue

            # Timestamp
            ts_str = (
                msg.get("composeTime")
                or msg.get("composetime")
                or msg.get("originalArrivalTime")
                or msg.get("originalarrivaltime")
                or msg.get("createdDateTime")
            )
            if ts_str:
                try:
                    event_ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                except Exception:
                    event_ts = datetime.now(timezone.utc)
            else:
                event_ts = datetime.now(timezone.utc)

            # Idempotency Key
            full_content_hash = hashlib.sha256(content_text.strip().encode("utf-8")).hexdigest()
            idempotency_key = hashlib.sha256(
                f"{self.tenant_id}:ms_teams_web:{msg_id}:{full_content_hash[:16]}".encode("utf-8")
            ).hexdigest()

            now_utc = datetime.now(timezone.utc)
            record = RawEventRecord(
                id=f"raw-teams-{uuid.uuid4().hex[:12]}",
                tenant_id=self.tenant_id,
                source_type=SourceType.MS_TEAMS_WEB,
                external_id=msg_id,
                parent_external_id=msg.get("parentMessageId"),
                idempotency_key=idempotency_key,
                event_timestamp=event_ts,
                captured_at=now_utc,
                author_external_id=author_id,
                author_display_name=author_name,
                conversation_or_project_id=conv_id,
                deep_link=f"https://teams.microsoft.com/l/message/{conv_id}/{msg_id}",
                raw_payload=msg,
                payload_json=json.dumps(msg, default=str),
                normalized_text=content_text.strip(),
                content_hash=full_content_hash,
                processing_status=ProcessingStatus.PENDING,
                created_at=now_utc,
            )
            records.append(record)

        return records
