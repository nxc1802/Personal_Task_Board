"""Teams Web Network Response Interceptor (Layer 1A).

Hook vào sự kiện page.on('response') của Playwright để bắt gói tin JSON nội bộ
của Microsoft Teams Web mà không cần Azure AD Admin Consent.
"""

import asyncio
from datetime import datetime, timezone
import hashlib
import inspect
import json
import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional
import uuid

from playwright.async_api import Response
from ptb_contracts import BugCode, ProcessingStatus, RawEventRecord, SourceType, log_bug
from ptb_acquisition.playwright.session import SessionHealthState, transition_session_state
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
        on_state_change: Optional[Callable[[SessionHealthState], Any]] = None,
        initial_state: SessionHealthState = SessionHealthState.HEALTHY,
    ):
        self.queue = queue
        self.tenant_id = tenant_id
        self.pipeline = pipeline
        self.raw_event_repo = raw_event_repo
        self.on_state_change = on_state_change
        self.captured_records: List[RawEventRecord] = []
        self.source_type = SourceType.MS_TEAMS_WEB
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
                logger.debug(f"Error calling on_state_change in TeamsNetworkInterceptor: {cb_err}")

    async def handle_response(self, response: Response) -> List[RawEventRecord]:
        """Xử lý mọi network response trả về từ Teams Web theo cơ chế Persist-First."""
        if self._session_state == SessionHealthState.AUTH_EXPIRED:
            logger.debug(f"Bỏ qua response {getattr(response, 'url', '')} vì session đã AUTH_EXPIRED")
            return []

        url = response.url if hasattr(response, "url") else ""
        status = response.status if hasattr(response, "status") else 200

        # Kiểm tra redirect hoặc lỗi xác thực 401/403
        is_login_redirect = "login.microsoftonline.com" in url.lower() or "login.live.com" in url.lower()
        if hasattr(response, "headers") and isinstance(response.headers, dict):
            location = response.headers.get("location", "").lower()
            if "login.microsoftonline.com" in location or "login.live.com" in location:
                is_login_redirect = True

        if status in (401, 403) or is_login_redirect:
            logger.warning(f"Phiên đăng nhập Teams đã hết hạn (HTTP {status}, redirect={is_login_redirect}): {url}")
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

        # Lọc các URL endpoint chứa dữ liệu tin nhắn chat
        is_teams_msg_api = (
            ("api/chats" in url and "messages" in url)
            or ("conversations" in url and "messages" in url)
            or ("threads" in url and "messages" in url)
        )

        if not is_teams_msg_api or status != 200:
            return []

        try:
            payload = await response.json()
        except Exception as e:
            logger.debug(f"Không thể đọc JSON từ response {url}: {e}")
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
                    # Fallback backward-compatible khi chỉ truyền queue
                    persisted = await self.queue.put(rec)
                else:
                    # Chế độ standalone test không cắm kho lưu trữ
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
