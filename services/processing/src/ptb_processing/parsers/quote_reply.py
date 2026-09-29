"""Teams Quote/Reply Parser.

Extracts quoted author and content vs actual author and content from MS Teams HTML messages,
cleans HTML tags, normalizes whitespace, and eliminates emoji reactions or junk metadata.
"""

import html
import re
from typing import Any, Dict, Optional, Union

from ptb_contracts.l2_processing import ParsedMessageContent


class TeamsQuoteReplyParser:
    """Parser chuyên biệt cho tin nhắn Microsoft Teams dạng reply/quote.

    Xử lý:
    1. Tách trích dẫn: quoted_author, quoted_content vs actual_author, actual_content.
    2. Làm sạch HTML: gỡ tag, chuyển <br>/<p>/<div> thành newline, loại bỏ emoji reactions/metadata rác.
    3. Trả về ParsedMessageContent chuẩn contract L2.
    """

    # Regex nhận diện blockquote trong Teams (bao gồm schema.skype.com/Reply)
    BLOCKQUOTE_PATTERN = re.compile(
        r"<blockquote\b[^>]*>(.*?)</blockquote>",
        re.DOTALL | re.IGNORECASE,
    )

    # Regex trích xuất author từ thẻ strong, b, hoặc itemprop="author"
    AUTHOR_TAG_PATTERNS = [
        re.compile(r"<[^>]*\bitemprop=['\"]author['\"][^>]*>(.*?)</[^>]+>", re.DOTALL | re.IGNORECASE),
        re.compile(r"<(?:strong|b)\b[^>]*>(.*?)</(?:strong|b)>", re.DOTALL | re.IGNORECASE),
    ]

    # Regex nhận diện emoji tags hoặc reactions của Teams
    EMOJI_TAG_PATTERN = re.compile(r"<emoji\b[^>]*>(.*?)</emoji>", re.DOTALL | re.IGNORECASE)
    ATTACHMENT_TAG_PATTERN = re.compile(r"<attachment\b[^>]*>.*?</attachment>", re.DOTALL | re.IGNORECASE)
    SYSTEM_EVENT_PATTERN = re.compile(r"<system-event\b[^>]*>.*?</system-event>", re.DOTALL | re.IGNORECASE)

    # Emoji reaction shortcodes thường gặp trong Teams
    EMOJI_SHORTCODES_PATTERN = re.compile(
        r"\((?:y|n|thumbsup|thumbsdown|heart|smile|sad|laugh|surprised|wink|clap|ok|highfive|party|fire)\)",
        re.IGNORECASE,
    )

    # Reaction text rác e.g. "❤️ 2", "👍 1", "[Like]", "[Heart]"
    REACTION_METADATA_PATTERN = re.compile(
        r"(?:\[(?:Like|Heart|Laugh|Surprised|Sad|Angry)\]|[\u2600-\u27BF\U0001F300-\U0001F9FF]\s*\d*)",
        re.UNICODE,
    )

    def clean_html(self, text: Optional[str]) -> str:
        """Làm sạch HTML/format:

        - Chuyển <br>, </p>, </div> thành newline.
        - Gỡ bỏ emoji tags, attachments, system events.
        - Gỡ bỏ toàn bộ tag HTML.
        - Unescape HTML entities (&nbsp;, &amp;, &lt;, &gt;, ...).
        - Gỡ bỏ shortcodes emoji reactions và metadata rác.
        - Chuẩn hóa khoảng trắng và dòng trống liên tiếp.
        """
        if not text:
            return ""

        # Gỡ bỏ các tag system/attachment rác
        cleaned = self.ATTACHMENT_TAG_PATTERN.sub("", text)
        cleaned = self.SYSTEM_EVENT_PATTERN.sub("", cleaned)
        cleaned = self.EMOJI_TAG_PATTERN.sub("", cleaned)

        # Chuyển đổi ngắt dòng và thẻ khối
        cleaned = re.sub(r"<br\s*/?>", "\n", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"</p\s*>", "\n\n", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"</div\s*>", "\n", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"</span\s*>", " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"<li\s*>", "\n• ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"</li\s*>", "\n", cleaned, flags=re.IGNORECASE)

        # Gỡ toàn bộ tag HTML còn lại
        cleaned = re.sub(r"<[^>]+>", "", cleaned)

        # Unescape HTML entities
        cleaned = html.unescape(cleaned)
        # Thay thế non-breaking spaces bằng khoảng trắng thông thường
        cleaned = cleaned.replace("\xa0", " ")

        # Gỡ reaction shortcodes và metadata rác
        cleaned = self.EMOJI_SHORTCODES_PATTERN.sub("", cleaned)
        cleaned = self.REACTION_METADATA_PATTERN.sub("", cleaned)

        # Chuẩn hóa khoảng trắng
        lines = [line.strip() for line in cleaned.split("\n")]
        # Loại bỏ các dòng trống dư thừa (>2 dòng trống)
        result_lines = []
        consecutive_empty = 0
        for line in lines:
            if not line:
                consecutive_empty += 1
                if consecutive_empty <= 1:
                    result_lines.append("")
            else:
                consecutive_empty = 0
                result_lines.append(line)

        return "\n".join(result_lines).strip()

    def parse(
        self,
        content: Union[str, Dict[str, Any]],
        actual_author: Optional[str] = None,
    ) -> ParsedMessageContent:
        """Tách trích dẫn tin nhắn Teams và trích xuất nội dung thực tế.

        Args:
            content: Chuỗi HTML, hoặc dict payload Teams (e.g. raw_payload["body"]["content"]
                     hoặc raw_payload có chứa 'body' và 'from').
            actual_author: Tên hoặc display name của tác giả gửi tin nhắn (nếu có).

        Returns:
            ParsedMessageContent: Chứa is_quote_reply, quoted_author_raw,
                                  quoted_content_text, actual_content_text, actual_author_raw.
        """
        raw_html = ""
        resolved_author = actual_author

        # Nếu truyền vào dict raw_payload
        if isinstance(content, dict):
            # Tìm author nếu chưa có
            if not resolved_author:
                from_user = content.get("from", {}).get("user", {})
                resolved_author = (
                    from_user.get("displayName")
                    or from_user.get("id")
                    or content.get("author_display_name")
                    or content.get("author_external_id")
                )

            body = content.get("body", {})
            if isinstance(body, dict):
                raw_html = body.get("content", "")
            else:
                raw_html = content.get("content", "")
        else:
            raw_html = str(content or "")

        # Kiểm tra sự xuất hiện của blockquote
        blockquote_match = self.BLOCKQUOTE_PATTERN.search(raw_html)

        if not blockquote_match:
            cleaned_actual = self.clean_html(raw_html)
            return ParsedMessageContent(
                is_quote_reply=False,
                quoted_author_raw=None,
                quoted_content_text=None,
                actual_content_text=cleaned_actual,
                actual_author_raw=resolved_author,
            )

        # Có trích dẫn
        blockquote_inner = blockquote_match.group(1)
        # Nội dung thực tế nằm ngoài blockquote
        actual_raw_html = self.BLOCKQUOTE_PATTERN.sub("", raw_html)

        # Trích xuất tác giả được quote
        quoted_author = None
        quoted_body_html = blockquote_inner

        for author_pat in self.AUTHOR_TAG_PATTERNS:
            match = author_pat.search(blockquote_inner)
            if match:
                candidate_author = match.group(1)
                cleaned_author = self.clean_html(candidate_author)
                if cleaned_author:
                    quoted_author = cleaned_author
                    # Gỡ phần author tag khỏi quoted body
                    quoted_body_html = author_pat.sub("", quoted_body_html, count=1)
                    break

        # Nếu chưa tìm thấy author qua tag, thử regex pattern dạng "Author: Content"
        if not quoted_author:
            plain_inner = self.clean_html(blockquote_inner)
            match_author_colon = re.match(r"^([^:\n]{2,50}):\s*(.*)$", plain_inner, re.DOTALL)
            if match_author_colon:
                quoted_author = match_author_colon.group(1).strip()
                quoted_content = match_author_colon.group(2).strip()
            else:
                quoted_content = plain_inner
        else:
            # Làm sạch quoted content sau khi bỏ author tag
            quoted_content = self.clean_html(quoted_body_html)
            # Nếu còn sót dấu hai chấm ở đầu quoted content
            quoted_content = re.sub(r"^[:\s\-–—]+", "", quoted_content).strip()

        cleaned_actual = self.clean_html(actual_raw_html)

        return ParsedMessageContent(
            is_quote_reply=True,
            quoted_author_raw=quoted_author,
            quoted_content_text=quoted_content or None,
            actual_content_text=cleaned_actual,
            actual_author_raw=resolved_author,
        )
