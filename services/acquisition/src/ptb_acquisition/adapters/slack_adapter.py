"""Slack Conversations, Threads & Mentions Acquisition Adapter.

Truy xuất và nạp tin nhắn, thread replies và commitments từ Slack Web API v2,
chuyển đổi thành RawEventRecord v1 chuẩn hóa với source_type = SourceType.SLACK.

Tính năng:
- Thread replies resolution qua `conversations.replies`
- Channel listing & user profile resolution (`conversations.list`, `users.info`)
- Phân trang cursor-based chuẩn hóa (`response_metadata.next_cursor`)
- Xử lý Rate Limit (HTTP 429 & Slack `error: ratelimited`, `Retry-After`)
- Hỗ trợ Bot token (`xoxb-...`) và User token (`xoxp-...`)
- Tự động khử trùng lặp (Deterministic Idempotency Key) & Tenant Isolation
- Loại bỏ/che giấu PII & Secrets (Slack tokens, Bearer, Passwords)
"""

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import re
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request
import uuid

from ptb_contracts import (
    IngestionCheckpointRecord,
    ProcessingStatus,
    RawEventRecord,
    SourceType,
)
from ptb_acquisition.adapters.base import (
    AcquisitionAdapter,
    AcquisitionError,
    AdapterAuthenticationError,
    AdapterConfigurationError,
    AdapterRateLimitError,
    AdapterStreamNotFoundError,
    AdapterTransientError,
)

logger = logging.getLogger("ptb.acquisition.adapters.slack")

# =============================================================================
# SECRET & PII REDACTION REGEXES
# =============================================================================
_SLACK_TOKEN_RE = re.compile(r"xox[baprs]-[0-9a-zA-Z-]{10,}", re.IGNORECASE)
_BEARER_TOKEN_RE = re.compile(r"Bearer\s+[A-Za-z0-9_\-\.~+/]+=*", re.IGNORECASE)
_URL_CREDS_RE = re.compile(r"(https?://)([^:]+):([^@]+)@", re.IGNORECASE)


def redact_secrets(text: Optional[str]) -> str:
    """Loại bỏ secrets, bot/user tokens và credentials khỏi text."""
    if not text:
        return ""
    clean = _SLACK_TOKEN_RE.sub("[REDACTED_SLACK_TOKEN]", str(text))
    clean = _BEARER_TOKEN_RE.sub("Bearer [REDACTED_TOKEN]", clean)
    clean = _URL_CREDS_RE.sub(r"\1[REDACTED_USER]:[REDACTED_PASS]@", clean)
    return clean


class SlackAdapter(AcquisitionAdapter):
    """Adapter tích hợp Slack Web API (Channels, Direct Messages, Threads) chuẩn v1."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        user_token: Optional[str] = None,
        channels: Optional[List[str]] = None,
        base_url: str = "https://slack.com/api",
        tenant_id: str = "local-user",
        http_fetcher: Optional[Callable[..., Any]] = None,
    ):
        super().__init__(source_type=SourceType.SLACK, tenant_id=tenant_id)
        # Hỗ trợ cả bot_token (xoxb-...) và user_token (xoxp-...)
        self.bot_token = (
            bot_token
            or os.getenv("SLACK_BOT_TOKEN")
            or os.getenv("SLACK_API_TOKEN")
            or ""
        )
        self.user_token = user_token or os.getenv("SLACK_USER_TOKEN") or ""
        self.channels = channels or []
        self.base_url = (base_url or "https://slack.com/api").rstrip("/")
        self._http_fetcher = http_fetcher
        # Cache tra cứu user_id -> tên hiển thị tránh gọi API dư thừa
        self._user_cache: Dict[str, str] = {}

    @property
    def active_token(self) -> str:
        """Trả về token hợp lệ ưu tiên bot_token, sau đó user_token."""
        return self.bot_token or self.user_token

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
        }
        token = self.active_token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    # -------------------------------------------------------------------------
    # HTTP Execution & Error Translation
    # -------------------------------------------------------------------------

    async def _execute_request(
        self,
        endpoint: str,
        method: str = "GET",
        params: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Thực thi HTTP request tới Slack Web API và chuẩn hóa mã lỗi."""
        # Chuẩn bị URL kèm query parameters
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        if params:
            clean_params = {k: v for k, v in params.items() if v is not None}
            if clean_params:
                query_string = urllib.parse.urlencode(clean_params)
                url = f"{url}?{query_string}"

        headers = self._get_headers()
        body_bytes = json.dumps(payload).encode("utf-8") if payload else None

        # 1. Hỗ trợ http_fetcher mock/injection
        if self._http_fetcher:
            try:
                res = self._http_fetcher(url, headers, body_bytes)
                if asyncio.iscoroutine(res):
                    res = await res
                if isinstance(res, dict):
                    data = res
                elif isinstance(res, (bytes, str)):
                    data = json.loads(res)
                elif hasattr(res, "json"):
                    data = res.json()
                elif hasattr(res, "read"):
                    data = json.loads(res.read().decode("utf-8"))
                else:
                    data = json.loads(str(res))
            except Exception as exc:
                # Nếu fetcher ném ngoại lệ HTTP hoặc mạng
                self._handle_http_exception(exc)
                raise
        else:
            # 2. urllib.request mặc định
            if not self.active_token:
                raise AdapterConfigurationError(
                    "Slack token chưa được cấu hình (SLACK_BOT_TOKEN hoặc SLACK_USER_TOKEN)",
                    source_type=self.source_type,
                )

            def _sync_request() -> Dict[str, Any]:
                req = urllib.request.Request(
                    url,
                    data=body_bytes,
                    headers=headers,
                    method=method,
                )
                try:
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        resp_data = resp.read().decode("utf-8")
                        return json.loads(resp_data)
                except urllib.error.HTTPError as h_err:
                    self._handle_urllib_http_error(h_err)
                    raise
                except urllib.error.URLError as u_err:
                    raise AdapterTransientError(
                        f"Lỗi mạng khi kết nối Slack: {u_err}",
                        source_type=self.source_type,
                    ) from u_err

            data = await asyncio.to_thread(_sync_request)

        # 3. Slack API Response Error Validation (ok == False)
        if isinstance(data, dict) and not data.get("ok", True):
            error_code = data.get("error", "unknown_slack_error")
            self._handle_slack_error_code(error_code, data)

        return data

    def _handle_http_exception(self, exc: Exception) -> None:
        """Chuyển đổi các ngoại lệ từ http_fetcher thành Adapter Error."""
        status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        if status_code == 429:
            retry_after = 5.0
            headers = getattr(exc, "headers", {}) or {}
            if "Retry-After" in headers:
                try:
                    retry_after = float(headers["Retry-After"])
                except (ValueError, TypeError):
                    pass
            raise AdapterRateLimitError(
                f"Slack HTTP 429 Rate Limit: {exc}",
                retry_after_seconds=retry_after,
                source_type=self.source_type,
            )
        if status_code in (401, 403):
            raise AdapterAuthenticationError(
                f"Slack HTTP {status_code} Auth Error: {exc}",
                source_type=self.source_type,
            )
        if status_code == 404:
            raise AdapterStreamNotFoundError(
                f"Slack HTTP 404 Not Found: {exc}",
                source_type=self.source_type,
            )
        if status_code and status_code >= 500:
            raise AdapterTransientError(
                f"Slack HTTP {status_code} Server Error: {exc}",
                source_type=self.source_type,
            )

    def _handle_urllib_http_error(self, err: urllib.error.HTTPError) -> None:
        """Xử lý HTTP error từ urllib."""
        if err.code == 429:
            retry_after = 5.0
            retry_hdr = err.headers.get("Retry-After")
            if retry_hdr:
                try:
                    retry_after = float(retry_hdr)
                except (ValueError, TypeError):
                    pass
            raise AdapterRateLimitError(
                f"Slack HTTP 429 Too Many Requests (retry-after: {retry_after}s)",
                retry_after_seconds=retry_after,
                source_type=self.source_type,
            )
        if err.code in (401, 403):
            raise AdapterAuthenticationError(
                f"Slack HTTP {err.code} Unauthorized/Forbidden: {err}",
                source_type=self.source_type,
            )
        if err.code == 404:
            raise AdapterStreamNotFoundError(
                f"Slack HTTP 404 Resource Not Found: {err}",
                source_type=self.source_type,
            )
        if err.code >= 500:
            raise AdapterTransientError(
                f"Slack HTTP {err.code} Transient Error: {err}",
                source_type=self.source_type,
            )

    def _handle_slack_error_code(self, error_code: str, data: Dict[str, Any]) -> None:
        """Ánh xạ mã lỗi logic từ Slack JSON response sang Error Hierarchy."""
        # 1. Auth errors
        if error_code in (
            "not_authed",
            "invalid_auth",
            "account_inactive",
            "token_revoked",
            "token_expired",
            "missing_scope",
        ):
            raise AdapterAuthenticationError(
                f"Slack authentication failed: {error_code}",
                source_type=self.source_type,
                details=data,
            )
        # 2. Rate limit
        if error_code == "ratelimited":
            retry_after = 5.0
            meta = data.get("response_metadata", {})
            if isinstance(meta, dict) and meta.get("retry_after"):
                try:
                    retry_after = float(meta["retry_after"])
                except (ValueError, TypeError):
                    pass
            raise AdapterRateLimitError(
                f"Slack API ratelimited (retry_after={retry_after}s)",
                retry_after_seconds=retry_after,
                source_type=self.source_type,
                details=data,
            )
        # 3. Stream / channel not found
        if error_code in ("channel_not_found", "is_archived"):
            raise AdapterStreamNotFoundError(
                f"Slack stream/channel error: {error_code}",
                source_type=self.source_type,
                details=data,
            )
        # 4. Transient errors
        if error_code in ("fatal_error", "service_unavailable", "request_timeout"):
            raise AdapterTransientError(
                f"Slack transient service error: {error_code}",
                source_type=self.source_type,
                details=data,
            )
        # 5. Generic acquisition error
        raise AcquisitionError(
            f"Slack API returned error '{error_code}'",
            source_type=self.source_type,
            details=data,
        )

    # -------------------------------------------------------------------------
    # User Resolution & Caching
    # -------------------------------------------------------------------------

    async def resolve_user_name(self, user_id: str) -> str:
        """Tra cứu tên người dùng từ Slack user_id có cơ chế in-memory caching."""
        if not user_id or user_id == "slack-user":
            return "slack-user"

        if user_id in self._user_cache:
            return self._user_cache[user_id]

        if not self.active_token:
            return user_id

        try:
            res = await self.execute_with_retry(
                self._execute_request,
                endpoint="users.info",
                params={"user": user_id},
            )
            user_data = res.get("user", {})
            profile = user_data.get("profile", {})
            display_name = (
                profile.get("display_name")
                or profile.get("real_name")
                or user_data.get("real_name")
                or user_data.get("name")
                or user_id
            )
            self._user_cache[user_id] = str(display_name)
            return self._user_cache[user_id]
        except Exception as e:
            logger.debug(f"Không thể tra cứu Slack user info cho {user_id}: {e}")
            self._user_cache[user_id] = user_id
            return user_id

    # -------------------------------------------------------------------------
    # Canonical RawEventRecord Mapping & Sanitization
    # -------------------------------------------------------------------------

    def _message_to_raw_event(
        self,
        msg: Dict[str, Any],
        channel_id: str,
        parent_ts: Optional[str] = None,
        author_display_name: Optional[str] = None,
    ) -> RawEventRecord:
        """Ánh xạ Slack message/reply thành RawEventRecord chuẩn v1."""
        ts = str(msg.get("ts") or uuid.uuid4().hex)
        thread_ts = msg.get("thread_ts")
        raw_text = str(msg.get("text") or "").strip()
        user_id = str(msg.get("user") or msg.get("username") or "slack-user").strip()

        # Sanitize secrets & PII
        clean_text = redact_secrets(raw_text)

        # Parse timestamp UTC
        event_ts = datetime.now(timezone.utc)
        try:
            event_ts = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except Exception as exc:
            logger.debug("Failed to parse Slack float timestamp '%s': %s", ts, exc)

        # Deep link
        clean_ts = ts.replace(".", "")
        deep_link = f"https://slack.com/archives/{channel_id}/p{clean_ts}"
        effective_parent = parent_ts or (str(thread_ts) if thread_ts and thread_ts != ts else None)
        if effective_parent:
            deep_link += f"?thread_ts={effective_parent}"

        # Đảm bảo raw_payload không rò rỉ token hoặc bí mật
        safe_payload = dict(msg)
        if "token" in safe_payload:
            safe_payload["token"] = "[REDACTED]"

        return self.build_raw_event(
            external_id=ts,
            content=clean_text,
            author_id=user_id,
            stream_id=channel_id,
            event_timestamp=event_ts,
            deep_link=deep_link,
            raw_payload=safe_payload,
            parent_external_id=effective_parent,
            author_name=author_display_name or user_id,
        )

    # -------------------------------------------------------------------------
    # Thread Replies Fetching
    # -------------------------------------------------------------------------

    async def _fetch_thread_replies(
        self, channel_id: str, thread_ts: str, oldest_ts: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """Lấy toàn bộ replies trong thread qua API `conversations.replies`."""
        all_replies: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            params: Dict[str, Any] = {
                "channel": channel_id,
                "ts": thread_ts,
                "limit": 100,
            }
            if oldest_ts is not None:
                params["oldest"] = str(oldest_ts)
            if cursor:
                params["cursor"] = cursor

            try:
                res = await self.execute_with_retry(
                    self._execute_request,
                    endpoint="conversations.replies",
                    params=params,
                )
            except Exception as e:
                logger.warning(f"Lỗi khi lấy thread replies cho channel {channel_id} ts {thread_ts}: {e}")
                break

            messages = res.get("messages", [])
            for m in messages:
                # Bỏ qua tin nhắn gốc (Slack trả về parent message ở vị trí đầu tiên)
                if m.get("ts") == thread_ts:
                    continue
                all_replies.append(m)

            # Cursor pagination
            meta = res.get("response_metadata", {})
            cursor = meta.get("next_cursor")
            if not cursor or not res.get("has_more", False):
                break

        return all_replies

    # -------------------------------------------------------------------------
    # Channel History Fetching
    # -------------------------------------------------------------------------

    async def _fetch_channel_messages(
        self,
        channel_id: str,
        oldest_ts: Optional[float] = None,
        latest_ts: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Truy xuất tin nhắn trong channel sử dụng cursor-based pagination."""
        all_messages: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            params: Dict[str, Any] = {
                "channel": channel_id,
                "limit": 100,
            }
            if oldest_ts is not None:
                params["oldest"] = str(oldest_ts)
            if latest_ts is not None:
                params["latest"] = str(latest_ts)
            if cursor:
                params["cursor"] = cursor

            res = await self.execute_with_retry(
                self._execute_request,
                endpoint="conversations.history",
                params=params,
            )

            messages = res.get("messages", [])
            all_messages.extend(messages)

            # Cursor pagination
            meta = res.get("response_metadata", {})
            cursor = meta.get("next_cursor")
            if not cursor or not res.get("has_more", False) or not messages:
                break

        return all_messages

    # -------------------------------------------------------------------------
    # Lifecycle Methods: discover, backfill, poll_incremental, health
    # -------------------------------------------------------------------------

    async def discover(self) -> List[Dict[str, Any]]:
        """Liệt kê các channels có sẵn tại Slack workspace."""
        streams: List[Dict[str, Any]] = []

        if self.active_token:
            try:
                cursor: Optional[str] = None
                while True:
                    params: Dict[str, Any] = {
                        "types": "public_channel,private_channel",
                        "limit": 100,
                    }
                    if cursor:
                        params["cursor"] = cursor

                    res = await self.execute_with_retry(
                        self._execute_request,
                        endpoint="conversations.list",
                        params=params,
                    )
                    channels_data = res.get("channels", [])
                    for c in channels_data:
                        cid = c.get("id")
                        cname = c.get("name", cid)
                        if cid and not any(s["stream_id"] == cid for s in streams):
                            streams.append({
                                "stream_id": cid,
                                "name": f"#{cname}",
                                "source_type": SourceType.SLACK.value,
                                "is_private": c.get("is_private", False),
                                "topic": (c.get("topic") or {}).get("value", ""),
                                "num_members": c.get("num_members", 0),
                            })

                    cursor = (res.get("response_metadata") or {}).get("next_cursor")
                    if not cursor:
                        break
            except Exception as e:
                logger.warning(f"Không thể tự động discover Slack channels: {e}")

        # Thêm các kênh đã chỉ định thủ công nếu chưa có trong kết quả
        for c in self.channels:
            if not any(s["stream_id"] == c for s in streams):
                streams.append({
                    "stream_id": c,
                    "name": f"#{c}",
                    "source_type": SourceType.SLACK.value,
                })

        if not streams:
            streams.append({
                "stream_id": "general",
                "name": "#general",
                "source_type": SourceType.SLACK.value,
            })

        return streams

    async def backfill(
        self, stream_id: str, since: Optional[datetime] = None
    ) -> AsyncIterator[RawEventRecord]:
        """Thu nạp lịch sử tin nhắn và toàn bộ thread replies từ mốc since."""
        oldest_ts = since.timestamp() if since else None
        messages = await self._fetch_channel_messages(stream_id, oldest_ts=oldest_ts)

        # Sắp xếp tin nhắn theo thứ tự thời gian tăng dần để emit nhất quán
        messages.sort(key=lambda m: float(m.get("ts", 0)))

        for msg in messages:
            uid = msg.get("user") or msg.get("username") or "slack-user"
            author_name = await self.resolve_user_name(str(uid))
            parent_event = self._message_to_raw_event(
                msg, channel_id=stream_id, author_display_name=author_name
            )
            yield parent_event

            # Kiểm tra nếu tin nhắn có thread replies
            reply_count = msg.get("reply_count", 0)
            thread_ts = msg.get("thread_ts")
            msg_ts = msg.get("ts")
            if reply_count > 0 or (thread_ts and thread_ts == msg_ts):
                replies = await self._fetch_thread_replies(
                    channel_id=stream_id,
                    thread_ts=str(msg_ts),
                    oldest_ts=oldest_ts,
                )
                replies.sort(key=lambda r: float(r.get("ts", 0)))
                for rep in replies:
                    r_uid = rep.get("user") or rep.get("username") or "slack-user"
                    r_name = await self.resolve_user_name(str(r_uid))
                    yield self._message_to_raw_event(
                        rep,
                        channel_id=stream_id,
                        parent_ts=str(msg_ts),
                        author_display_name=r_name,
                    )

    async def poll_incremental(
        self, stream_id: str, checkpoint: Optional[IngestionCheckpointRecord] = None
    ) -> AsyncIterator[RawEventRecord]:
        """Thu nạp các tin nhắn và replies mới phát sinh kể từ checkpoint."""
        oldest_ts: Optional[float] = None
        if checkpoint and checkpoint.last_event_timestamp:
            oldest_ts = checkpoint.last_event_timestamp.timestamp()

        messages = await self._fetch_channel_messages(stream_id, oldest_ts=oldest_ts)
        messages.sort(key=lambda m: float(m.get("ts", 0)))

        for msg in messages:
            msg_ts_str = str(msg.get("ts", 0))
            # Nếu trùng khớp với checkpoint đã nhận trước đó thì bỏ qua
            if (
                checkpoint
                and checkpoint.last_external_id
                and msg_ts_str == checkpoint.last_external_id
            ):
                pass
            else:
                uid = msg.get("user") or msg.get("username") or "slack-user"
                author_name = await self.resolve_user_name(str(uid))
                parent_event = self._message_to_raw_event(
                    msg, channel_id=stream_id, author_display_name=author_name
                )
                yield parent_event

            # Quét replies mới trong thread
            reply_count = msg.get("reply_count", 0)
            thread_ts = msg.get("thread_ts")
            msg_ts = msg.get("ts")
            if reply_count > 0 or (thread_ts and thread_ts == msg_ts):
                replies = await self._fetch_thread_replies(
                    channel_id=stream_id,
                    thread_ts=str(msg_ts),
                    oldest_ts=oldest_ts,
                )
                replies.sort(key=lambda r: float(r.get("ts", 0)))
                for rep in replies:
                    rep_ts = str(rep.get("ts", 0))
                    if (
                        checkpoint
                        and checkpoint.last_external_id
                        and rep_ts == checkpoint.last_external_id
                    ):
                        continue
                    r_uid = rep.get("user") or rep.get("username") or "slack-user"
                    r_name = await self.resolve_user_name(str(r_uid))
                    yield self._message_to_raw_event(
                        rep,
                        channel_id=stream_id,
                        parent_ts=str(msg_ts),
                        author_display_name=r_name,
                    )

    async def health(self) -> Dict[str, Any]:
        """Kiểm tra sức khỏe kết nối và thẩm định token của SlackAdapter."""
        configured = bool(self.active_token)
        report: Dict[str, Any] = {
            "source_type": self.source_type.value,
            "tenant_id": self.tenant_id,
            "configured": configured,
            "status": "unconfigured" if not configured else "healthy",
            "channels_monitored": len(self.channels),
            "token_type": "bot" if self.bot_token else ("user" if self.user_token else "none"),
        }

        if not configured:
            return report

        try:
            auth_res = await self._execute_request("auth.test", method="POST")
            report["status"] = "healthy"
            report["user_id"] = auth_res.get("user_id")
            report["bot_id"] = auth_res.get("bot_id")
            report["team"] = auth_res.get("team")
        except AdapterAuthenticationError as a_err:
            report["status"] = "auth_required"
            report["error"] = str(a_err)
        except Exception as err:
            report["status"] = "degraded"
            report["error"] = str(err)

        return report
