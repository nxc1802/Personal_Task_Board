"""Multi-Channel Parsers for Personal Task Board Layer 2.

Cung cấp các parser chuyên biệt và chuẩn hóa cho toàn bộ các kênh thông tin:
1. TeamsQuoteReplyParser: Microsoft Teams chat & reply.
2. OutlookEmailParser: Microsoft Outlook Web & Desktop emails/threads.
3. JiraIssueParser: Jira Cloud & Server issues (plain text & ADF).
4. GitCommitParser: Local Git & remote repository commit records.
5. ShortcutStoryParser: Shortcut stories & epics.
6. ConfluenceParser: Confluence pages, meeting notes & action items.
7. GitHubParser: GitHub Issues & Pull Requests.
8. SlackParser: Slack messages, threads & mrkdwn.
9. UnifiedChannelParser: Điều phối viên tự động định tuyến theo source_type.
"""

from datetime import datetime, timezone
import html
import json
import logging
import re
from typing import Any, Dict, List, Optional, Union

from ptb_contracts.l1_acquisition import SourceType
from ptb_contracts.l2_processing import ParsedMessageContent
from ptb_processing.parsers.quote_reply import TeamsQuoteReplyParser

logger = logging.getLogger("ptb.processing.parsers.multi_channel")


# ==============================================================================
# 1. OUTLOOK EMAIL PARSER
# ==============================================================================
class OutlookEmailParser:
    """Parser chuyên biệt cho Microsoft Outlook emails (OWA & Graph/REST).
    
    Xử lý:
    - Bóc tách tiêu đề email (Subject), người gửi (From) và người nhận.
    - Nhận diện và tách chuỗi phản hồi (Quoted reply thread) dạng:
      * '-----Original Message-----' / '----- Tin nhắn gốc -----'
      * 'From: ... Sent: ... To: ... Subject: ...'
      * 'On <date>, <author> wrote:' / 'Vào ngày <date>, <author> đã viết:'
      * Thẻ HTML OWA: <div id="divRplyFwdMsg">, <hr tabindex="-1">, v.v.
    - Làm sạch chữ ký email (Signatures) và khoảng trắng dư thừa.
    """

    REPLY_DIV_PATTERNS = [
        re.compile(r'<div\b[^>]*\bid=["\']divRplyFwdMsg["\'][^>]*>(.*)', re.DOTALL | re.IGNORECASE),
        re.compile(r'<hr\b[^>]*\btabindex=["\']-1["\'][^>]*>(.*)', re.DOTALL | re.IGNORECASE),
        re.compile(r'<div\b[^>]*\bclass=["\'][^"\']*OutlookQuotedText[^"\']*["\'][^>]*>(.*)', re.DOTALL | re.IGNORECASE),
        re.compile(r'<blockquote\b[^>]*>(.*?)</blockquote>', re.DOTALL | re.IGNORECASE),
    ]

    ORIGINAL_MSG_HEADER_PATTERNS = [
        # Standard Outlook / Exchange English header
        re.compile(
            r"(?:^|\n)\s*-{3,}\s*Original Message\s*-{3,}\s*\n(?:\s*From:\s*(?P<author>[^\n]+)\n)?(?:\s*Sent:\s*[^\n]+\n)?(?:\s*To:\s*[^\n]+\n)?(?:\s*Subject:\s*[^\n]+\n)?(?P<quoted>.*)",
            re.DOTALL | re.IGNORECASE,
        ),
        # Vietnamese Outlook header
        re.compile(
            r"(?:^|\n)\s*-{3,}\s*Tin nhắn gốc\s*-{3,}\s*\n(?:\s*Từ:\s*(?P<author>[^\n]+)\n)?(?:\s*Gửi:\s*[^\n]+\n)?(?:\s*Đến:\s*[^\n]+\n)?(?:\s*Tiêu đề:\s*[^\n]+\n)?(?P<quoted>.*)",
            re.DOTALL | re.IGNORECASE,
        ),
        # On <date>, <author> wrote:
        re.compile(
            r"(?:^|\n)\s*On\s+[^\n]+,\s*(?P<author>[^\n<]+)(?:<[^>]+>)?\s+wrote:\s*\n(?P<quoted>.*)",
            re.DOTALL | re.IGNORECASE,
        ),
        # Vào ngày <date>, <author> đã viết:
        re.compile(
            r"(?:^|\n)\s*Vào\s+[^\n]+,\s*(?P<author>[^\n<]+)(?:<[^>]+>)?\s+đã viết:\s*\n(?P<quoted>.*)",
            re.DOTALL | re.IGNORECASE,
        ),
    ]

    SIGNATURE_PATTERNS = [
        re.compile(r"\n\s*--\s*\n.*$", re.DOTALL),
        re.compile(r"\n\s*(?:Best regards|Warm regards|Regards|Thanks & regards|Trân trọng|Thân mến),?\s*\n.*$", re.DOTALL | re.IGNORECASE),
    ]

    def __init__(self) -> None:
        self.teams_cleaner = TeamsQuoteReplyParser()

    def clean_html_email(self, html_text: str) -> str:
        """Làm sạch HTML email, chuyển các thẻ khối sang ngắt dòng và gỡ bỏ HTML entities."""
        return self.teams_cleaner.clean_html(html_text)

    def parse(
        self,
        content: Union[str, Dict[str, Any]],
        actual_author: Optional[str] = None,
    ) -> ParsedMessageContent:
        subject = ""
        body_text = ""
        resolved_author = actual_author

        if isinstance(content, dict):
            subject = str(content.get("Subject") or content.get("subject") or content.get("ConversationTopic") or "").strip()
            
            # Extract author if not passed
            if not resolved_author:
                sender_info = content.get("From") or content.get("from") or content.get("Sender") or {}
                if isinstance(sender_info, dict):
                    mailbox = sender_info.get("Mailbox") or sender_info.get("emailAddress") or sender_info
                    resolved_author = mailbox.get("Name") or mailbox.get("name") or mailbox.get("EmailAddress") or mailbox.get("address")
                elif isinstance(sender_info, str):
                    resolved_author = sender_info

            # Extract body
            body_obj = (
                content.get("Body")
                or content.get("body")
                or content.get("Preview")
                or content.get("bodyPreview")
                or content.get("content")
                or content.get("text")
                or ""
            )
            if isinstance(body_obj, dict):
                body_text = str(body_obj.get("Value") or body_obj.get("content") or "")
            else:
                body_text = str(body_obj)

            if not subject and "subject:" in body_text.lower():
                m_subj = re.search(r"subject:\s*([^\n]+)", body_text, re.IGNORECASE)
                if m_subj:
                    subject = m_subj.group(1).strip()
        else:
            body_text = str(content or "")

        # Check for HTML quote blocks
        quoted_html = ""
        actual_html = body_text

        for pat in self.REPLY_DIV_PATTERNS:
            match = pat.search(actual_html)
            if match:
                quoted_html = match.group(1) if match.lastindex else match.group(0)
                actual_html = pat.sub("", actual_html)
                break

        clean_actual = self.clean_html_email(actual_html)
        clean_quoted = self.clean_html_email(quoted_html) if quoted_html else ""
        quoted_author = None

        # Check for text-based header patterns in clean_actual
        if not clean_quoted:
            for header_pat in self.ORIGINAL_MSG_HEADER_PATTERNS:
                m = header_pat.search(clean_actual)
                if m:
                    quoted_author = m.group("author").strip() if "author" in m.groupdict() and m.group("author") else None
                    clean_quoted = m.group("quoted").strip() if "quoted" in m.groupdict() and m.group("quoted") else ""
                    clean_actual = header_pat.sub("", clean_actual).strip()
                    break

        # Remove email signatures from actual content
        for sig_pat in self.SIGNATURE_PATTERNS:
            clean_actual = sig_pat.sub("", clean_actual).strip()

        # Prepend subject if available and not redundant
        if subject and subject != "No Subject":
            if not clean_actual.lower().startswith(f"subject: {subject.lower()}"):
                clean_actual = f"Subject: {subject}\n\n{clean_actual}".strip()

        is_quote = bool(clean_quoted)

        return ParsedMessageContent(
            is_quote_reply=is_quote,
            quoted_author_raw=quoted_author,
            quoted_content_text=clean_quoted or None,
            actual_content_text=clean_actual or subject or "(Empty email body)",
            actual_author_raw=resolved_author,
        )


# ==============================================================================
# 2. JIRA ISSUE PARSER
# ==============================================================================
class JiraIssueParser:
    """Parser chuyên biệt cho Jira Cloud & Server REST API issues."""

    @staticmethod
    def _extract_text_from_adf(adf: Any) -> str:
        """Trích xuất chuỗi văn bản từ Atlassian Document Format (ADF dict/JSON)."""
        if not adf:
            return ""
        if isinstance(adf, str):
            return adf.strip()
        if isinstance(adf, dict):
            parts: List[str] = []

            def _walk(node: Any):
                if isinstance(node, dict):
                    if node.get("type") == "text" and "text" in node:
                        parts.append(node["text"])
                    for child in node.get("content", []):
                        _walk(child)
                elif isinstance(node, list):
                    for item in node:
                        _walk(item)

            _walk(adf)
            return " ".join(parts).strip()
        return str(adf).strip()

    def parse(
        self,
        content: Union[str, Dict[str, Any]],
        actual_author: Optional[str] = None,
    ) -> ParsedMessageContent:
        if not isinstance(content, dict):
            # Plain string issue text
            clean_text = str(content or "").strip()
            return ParsedMessageContent(
                is_quote_reply=False,
                quoted_author_raw=None,
                quoted_content_text=None,
                actual_content_text=clean_text,
                actual_author_raw=actual_author,
            )

        issue_obj = content.get("issue", content)
        key = str(issue_obj.get("key") or content.get("key") or "").strip()
        fields = issue_obj.get("fields", {}) if isinstance(issue_obj, dict) else {}

        summary = str(fields.get("summary") or (content.get("summary") if isinstance(content, dict) else "") or "").strip()
        raw_desc = fields.get("description") or (content.get("description") if isinstance(content, dict) else None)
        description = self._extract_text_from_adf(raw_desc)
        status_val = fields.get("status") or (content.get("status") if isinstance(content, dict) else None)
        status_name = str(status_val.get("name") if isinstance(status_val, dict) else (status_val or "")).strip()
        priority_val = fields.get("priority") or (content.get("priority") if isinstance(content, dict) else None)
        priority_name = str(priority_val.get("name") if isinstance(priority_val, dict) else (priority_val or "")).strip()

        assignee = fields.get("assignee") or (content.get("assignee") if isinstance(content, dict) else None)
        assignee_name = None
        if isinstance(assignee, dict):
            assignee_name = assignee.get("displayName") or assignee.get("name") or assignee.get("accountId")
        elif isinstance(assignee, str):
            assignee_name = assignee

        creator = fields.get("creator") or fields.get("reporter") or (content.get("reporter") if isinstance(content, dict) else None)
        creator_name = None
        if isinstance(creator, dict):
            creator_name = creator.get("displayName") or creator.get("name") or creator.get("accountId")
        elif isinstance(creator, str):
            creator_name = creator

        resolved_author = actual_author or assignee_name or creator_name

        # Construct authoritative normalized text
        lines = []
        if key and summary:
            lines.append(f"[{key}] {summary}")
        elif summary:
            lines.append(summary)
        elif key:
            lines.append(f"Jira Issue: {key}")

        if status_name:
            lines.append(f"Status: {status_name}")
        if priority_name:
            lines.append(f"Priority: {priority_name}")
        if description:
            lines.append(f"\nDescription:\n{description}")

        actual_content = "\n".join(lines).strip()
        if not actual_content:
            actual_content = str(content)

        return ParsedMessageContent(
            is_quote_reply=False,
            quoted_author_raw=creator_name if creator_name != resolved_author else None,
            quoted_content_text=None,
            actual_content_text=actual_content,
            actual_author_raw=resolved_author,
        )


# ==============================================================================
# 3. GIT COMMIT PARSER
# ==============================================================================
class GitCommitParser:
    """Parser chuyên biệt cho Git commit records và Pull Requests."""

    CONVENTIONAL_PATTERN = re.compile(
        r"^(?P<type>feat|fix|perf|refactor|docs|test|build|ci|chore)(?:\((?P<scope>[^)]+)\))?(?P<breaking>!)?:\s*(?P<msg>.*)$",
        re.IGNORECASE,
    )
    TICKET_ANCHORS = re.compile(r"\b(?:fixes|closes|resolves|refs|ref)\s+(?P<anchor>[A-Z0-9_\-]+#\d+|[A-Z][A-Z0-9]+-\d+|#\d+)\b", re.IGNORECASE)

    def parse(
        self,
        content: Union[str, Dict[str, Any]],
        actual_author: Optional[str] = None,
    ) -> ParsedMessageContent:
        if not isinstance(content, dict):
            msg_str = str(content or "").strip()
            return ParsedMessageContent(
                is_quote_reply=False,
                quoted_author_raw=None,
                quoted_content_text=None,
                actual_content_text=msg_str,
                actual_author_raw=actual_author,
            )

        commit_hash = str(content.get("hash") or content.get("commit_hash") or content.get("sha") or content.get("comment_id") or "").strip()
        message = str(content.get("message") or content.get("commit_message") or content.get("body") or content.get("comment") or content.get("content") or "").strip()
        author = str(content.get("author") or content.get("author_name") or actual_author or "Git Committer").strip()
        branch = str(content.get("branch") or "").strip()
        files_changed = content.get("files") or content.get("files_changed") or []

        header = f"Git Commit: {commit_hash[:8]}" if commit_hash else "Git Commit"
        if branch:
            header += f" (branch: {branch})"

        body_parts = [header, f"Author: {author}", f"Message: {message}"]
        if isinstance(files_changed, list) and files_changed:
            body_parts.append(f"Changed files ({len(files_changed)}): {', '.join(str(f) for f in files_changed[:5])}")

        actual_content = "\n".join(body_parts).strip()

        # Check if commit references a parent ticket or issue
        anchor_match = self.TICKET_ANCHORS.search(message)
        quoted_content = f"Referenced anchor: {anchor_match.group('anchor')}" if anchor_match else None

        return ParsedMessageContent(
            is_quote_reply=bool(quoted_content),
            quoted_author_raw=None,
            quoted_content_text=quoted_content,
            actual_content_text=actual_content,
            actual_author_raw=author,
        )


# ==============================================================================
# 4. SHORTCUT STORY PARSER
# ==============================================================================
class ShortcutStoryParser:
    """Parser chuyên biệt cho Shortcut stories (REST API v3)."""

    def parse(
        self,
        content: Union[str, Dict[str, Any]],
        actual_author: Optional[str] = None,
    ) -> ParsedMessageContent:
        if not isinstance(content, dict):
            return ParsedMessageContent(
                is_quote_reply=False,
                quoted_author_raw=None,
                quoted_content_text=None,
                actual_content_text=str(content or "").strip(),
                actual_author_raw=actual_author,
            )

        story_id = content.get("id")
        name = str(content.get("name") or content.get("title") or content.get("content") or content.get("text") or "").strip()
        desc = str(content.get("description") or "").strip()
        story_type = str(content.get("story_type") or "feature").strip()
        app_url = str(content.get("app_url") or "").strip()

        author = actual_author or content.get("author_name") or "Shortcut User"

        parts = []
        if story_id:
            parts.append(f"[Shortcut Story #{story_id}] {name}".strip())
        elif name:
            parts.append(name if name.startswith("[Shortcut") else f"[Shortcut Story] {name}")
        parts.append(f"Type: {story_type}")
        if app_url:
            parts.append(f"URL: {app_url}")
        if desc and desc != name:
            parts.append(f"\nDescription:\n{desc}")

        return ParsedMessageContent(
            is_quote_reply=False,
            quoted_author_raw=None,
            quoted_content_text=None,
            actual_content_text="\n".join(parts).strip() or name or "(Empty Shortcut story)",
            actual_author_raw=author,
        )


# ==============================================================================
# 5. CONFLUENCE DOCUMENT & ACTION ITEM PARSER
# ==============================================================================
class ConfluenceParser:
    """Parser chuyên biệt cho Confluence pages và meeting action items."""

    TASK_LIST_REGEX = re.compile(
        r"<ac:task\b[^>]*>[\s\S]*?(?:<ac:task-status>(?P<status>[^<]+)</ac:task-status>)?[\s\S]*?(?:<ac:task-body>(?P<body>[\s\S]*?)</ac:task-body>)?[\s\S]*?</ac:task>",
        re.IGNORECASE,
    )
    TASK_BODY_FALLBACK = re.compile(r"<ac:task-body\b[^>]*>(.*?)</ac:task-body>", re.DOTALL | re.IGNORECASE)
    MARKDOWN_TASK_REGEX = re.compile(r"^\s*-\s*\[(?P<status>[ xX])\]\s*(?P<body>.+)$", re.MULTILINE)

    def parse(
        self,
        content: Union[str, Dict[str, Any]],
        actual_author: Optional[str] = None,
    ) -> ParsedMessageContent:
        title = ""
        space = ""
        raw_body = ""
        author = actual_author

        if isinstance(content, dict):
            title = str(content.get("title") or "").strip()
            space = str(content.get("space") or content.get("spaceKey") or "").strip()
            author = author or content.get("author") or content.get("creator") or "Confluence Author"
            body_val = content.get("body")
            if isinstance(body_val, dict):
                raw_body = str(body_val.get("storage", {}).get("value") or body_val.get("view", {}).get("value") or body_val.get("content") or "")
            elif body_val:
                raw_body = str(body_val)
            
            if not raw_body:
                raw_body = str(content.get("content") or content.get("text") or "")
            if not raw_body and title:
                raw_body = title
        else:
            raw_body = str(content or "")

        # Extract explicit action items
        action_items: List[str] = []
        for m in self.TASK_LIST_REGEX.finditer(raw_body):
            b = TeamsQuoteReplyParser().clean_html(m.group("body") or "")
            st = m.group("status") or "incomplete"
            if b:
                action_items.append(f"[Action Item ({st})] {b}")

        if not action_items:
            for m in self.TASK_BODY_FALLBACK.finditer(raw_body):
                b = TeamsQuoteReplyParser().clean_html(m.group(1) or "")
                if b:
                    action_items.append(f"[Action Item (incomplete)] {b}")

        clean_body = TeamsQuoteReplyParser().clean_html(raw_body)
        parts = []
        if title:
            header = f"[Confluence: {title}]"
            if space:
                header += f" (Space: {space})"
            parts.append(header)

        if action_items:
            parts.append("\nAction Items Extracted:")
            parts.extend(f"• {act}" for act in action_items)

        if clean_body:
            parts.append(f"\nDocument Content:\n{clean_body}")

        return ParsedMessageContent(
            is_quote_reply=bool(action_items),
            quoted_author_raw=None,
            quoted_content_text="\n".join(action_items) if action_items else None,
            actual_content_text="\n".join(parts).strip() or title or "Confluence Document",
            actual_author_raw=author,
        )


# ==============================================================================
# 6. GITHUB EVENT PARSER
# ==============================================================================
class GitHubParser:
    """Parser chuyên biệt cho GitHub Issues và Pull Requests."""

    def parse(
        self,
        content: Union[str, Dict[str, Any]],
        actual_author: Optional[str] = None,
    ) -> ParsedMessageContent:
        if not isinstance(content, dict):
            return ParsedMessageContent(
                is_quote_reply=False,
                quoted_author_raw=None,
                quoted_content_text=None,
                actual_content_text=str(content or "").strip(),
                actual_author_raw=actual_author,
            )

        is_pr = "pull_request" in content or content.get("type") == "pull_request"
        item = content.get("pull_request") or content.get("issue") or content
        number = item.get("number") or content.get("number")
        title = str(item.get("title") or content.get("title") or "").strip()
        body = str(item.get("body") or content.get("body") or "").strip()
        state = str(item.get("state") or content.get("state") or "open").strip()
        user_info = item.get("user") or {}
        author = actual_author or (user_info.get("login") if isinstance(user_info, dict) else str(user_info)) or "GitHub User"
        html_url = item.get("html_url") or content.get("html_url") or ""

        prefix = "Pull Request" if is_pr else "Issue"
        parts = []
        if number:
            parts.append(f"[GitHub {prefix} #{number}] {title}")
        else:
            parts.append(f"[GitHub {prefix}] {title}")
        parts.append(f"Author: {author} | State: {state.upper()}")
        if html_url:
            parts.append(f"URL: {html_url}")
        if body:
            parts.append(f"\nDescription:\n{body}")

        return ParsedMessageContent(
            is_quote_reply=False,
            quoted_author_raw=None,
            quoted_content_text=None,
            actual_content_text="\n".join(parts).strip(),
            actual_author_raw=author,
        )


# ==============================================================================
# 7. SLACK MESSAGE PARSER
# ==============================================================================
class SlackParser:
    """Parser chuyên biệt cho Slack messages, thread replies và mrkdwn."""

    MRKDWN_USER = re.compile(r"<@([A-Z0-9]+)(?:\|([^>]+))?>")
    MRKDWN_CHANNEL = re.compile(r"<#([A-Z0-9]+)(?:\|([^>]+))?>")
    MRKDWN_LINK = re.compile(r"<([^|>]+)(?:\|([^>]+))?>")

    def clean_mrkdwn(self, text: str) -> str:
        """Chuyển đổi các định dạng mrkdwn nội bộ của Slack sang văn bản chuẩn."""
        if not text:
            return ""
        # Format user mentions: <@U123|Alice> -> @Alice
        cleaned = self.MRKDWN_USER.sub(lambda m: f"@{m.group(2) or m.group(1)}", text)
        # Format channels: <#C123|general> -> #general
        cleaned = self.MRKDWN_CHANNEL.sub(lambda m: f"#{m.group(2) or m.group(1)}", cleaned)
        # Format links: <https://abc.com|Link> -> Link (https://abc.com)
        cleaned = self.MRKDWN_LINK.sub(lambda m: f"{m.group(2)} ({m.group(1)})" if m.group(2) else m.group(1), cleaned)
        return cleaned.strip()

    def parse(
        self,
        content: Union[str, Dict[str, Any]],
        actual_author: Optional[str] = None,
    ) -> ParsedMessageContent:
        raw_text = ""
        author = actual_author
        quoted_text = None
        is_thread_reply = False

        if isinstance(content, dict):
            raw_text = str(content.get("text") or content.get("content") or content.get("body") or "").strip()
            author = author or content.get("user") or content.get("username") or "Slack User"
            thread_ts = content.get("thread_ts")
            ts = content.get("ts")
            is_thread_reply = bool(thread_ts and thread_ts != ts)
            if "parent_message" in content and isinstance(content["parent_message"], dict):
                p_text = content["parent_message"].get("text", "")
                p_user = content["parent_message"].get("user") or "Parent Author"
                quoted_text = f"{p_user}: {self.clean_mrkdwn(p_text)}"
        else:
            raw_text = str(content or "").strip()

        cleaned_text = self.clean_mrkdwn(raw_text)

        return ParsedMessageContent(
            is_quote_reply=is_thread_reply or bool(quoted_text),
            quoted_author_raw=None,
            quoted_content_text=quoted_text,
            actual_content_text=cleaned_text,
            actual_author_raw=author,
        )


# ==============================================================================
# 8. CODING AGENT PARSER
# ==============================================================================
class CodingAgentParser:
    """Parser chuyên biệt cho tương tác của Coding Agents (Cursor, Antigravity, Claude Code, Cline)."""

    def parse(
        self,
        content: Union[str, Dict[str, Any]],
        actual_author: Optional[str] = None,
    ) -> ParsedMessageContent:
        if not isinstance(content, dict):
            return ParsedMessageContent(
                is_quote_reply=False,
                quoted_author_raw=None,
                quoted_content_text=None,
                actual_content_text=str(content or "").strip(),
                actual_author_raw=actual_author,
            )

        text = str(
            content.get("content")
            or content.get("prompt")
            or content.get("message")
            or content.get("text")
            or ""
        ).strip()

        author = actual_author or content.get("author") or content.get("user") or "Coding Agent User"

        return ParsedMessageContent(
            is_quote_reply=False,
            quoted_author_raw=None,
            quoted_content_text=None,
            actual_content_text=text,
            actual_author_raw=author,
        )


# ==============================================================================
# 9. UNIFIED CHANNEL PARSER (REGISTRY & DISPATCHER)
# ==============================================================================
class UnifiedChannelParser:
    """Bộ điều phối parser đa kênh thống nhất cho toàn bộ hệ thống Layer 2.
    
    Tự động ánh xạ từng SourceType sang Parser chuyên biệt tương ứng.
    """

    def __init__(self) -> None:
        self.teams_parser = TeamsQuoteReplyParser()
        self.outlook_parser = OutlookEmailParser()
        self.jira_parser = JiraIssueParser()
        self.git_parser = GitCommitParser()
        self.shortcut_parser = ShortcutStoryParser()
        self.confluence_parser = ConfluenceParser()
        self.github_parser = GitHubParser()
        self.slack_parser = SlackParser()
        self.coding_agent_parser = CodingAgentParser()

    def get_parser_for_source(self, source_type: Union[str, SourceType]) -> Any:
        st = source_type.value if hasattr(source_type, "value") else str(source_type).lower().strip()
        if st in ("ms_teams", "ms_teams_web", "teams"):
            return self.teams_parser
        elif st in ("ms_outlook", "ms_outlook_web", "outlook"):
            return self.outlook_parser
        elif st in ("jira",):
            return self.jira_parser
        elif st in ("git",):
            return self.git_parser
        elif st in ("shortcut",):
            return self.shortcut_parser
        elif st in ("confluence",):
            return self.confluence_parser
        elif st in ("github",):
            return self.github_parser
        elif st in ("slack",):
            return self.slack_parser
        elif st in ("coding_agent", "coding_agents", "agent", "agents"):
            return self.coding_agent_parser
        else:
            # Default to TeamsQuoteReplyParser for backward compatibility
            return self.teams_parser

    def parse(
        self,
        content: Union[str, Dict[str, Any]],
        source_type: Union[str, SourceType] = SourceType.MS_TEAMS,
        actual_author: Optional[str] = None,
    ) -> ParsedMessageContent:
        parser = self.get_parser_for_source(source_type)
        return parser.parse(content, actual_author=actual_author)
