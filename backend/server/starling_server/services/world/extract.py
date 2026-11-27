"""Readable text, title and publication time from a web page, using only the standard library."""
from __future__ import annotations

import email.utils
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser

MAX_TEXT_CHARS = 20_000
MAX_TITLE_CHARS = 300
ARTICLE_MIN_CHARS = 200

SKIP_TAGS = frozenset(
    {
        "script", "style", "noscript", "template", "svg", "math", "canvas", "iframe", "object",
        "nav", "footer", "aside", "form", "button", "select", "textarea", "dialog",
    }
)
VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
BLOCK_TAGS = frozenset(
    {
        "address", "article", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption", "h1", "h2", "h3",
        "h4", "h5", "h6", "header", "hr", "li", "main", "ol", "p", "pre", "section", "table", "tbody", "td",
        "tfoot", "th", "thead", "tr", "ul",
    }
)
PUBLISHED_META = (
    "article:published_time",
    "og:published_time",
    "datepublished",
    "pubdate",
    "publishdate",
    "publish-date",
    "publish_date",
    "date",
    "dc.date.issued",
    "dc.date",
    "parsely-pub-date",
    "sailthru.date",
)
_HEADER_CHARSET_RE = re.compile(r"charset=\"?([A-Za-z0-9_.:-]{1,40})", re.IGNORECASE)
_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset=["']?([A-Za-z0-9_.:-]{1,40})""", re.IGNORECASE)


@dataclass(frozen=True)
class Page:
    title: str | None
    text: str
    published_at: int


def parse_timestamp(value: str | None) -> int:
    """Unix seconds from ISO 8601 or RFC 822 text; 0 when missing or unparseable. A naive time is UTC."""
    text = (value or "").strip()
    if not text:
        return 0
    try:
        moment = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
    except ValueError:
        try:
            moment = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return 0
    if moment is None:
        return 0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        seconds = int(moment.timestamp())
    except (OverflowError, OSError, ValueError):
        return 0
    return seconds if seconds > 0 else 0


def _collapse(text: str) -> str:
    return " ".join(text.split())


class _Readable(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.times: list[str] = []
        self.ld_dates: list[str] = []
        self.blocks: list[str] = []
        self.article_blocks: list[str] = []
        self._buffer: list[str] = []
        self._skip: list[str] = []
        self._in_title = False
        self._article_depth = 0
        self._ld_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = {name.lower(): (value or "") for name, value in attrs}
        if tag == "meta":
            key = (values.get("property") or values.get("name") or values.get("itemprop") or "").strip().lower()
            content = values.get("content", "").strip()
            if key and content and key not in self.meta:
                self.meta[key] = content
            return
        if tag == "time" and values.get("datetime"):
            if "pubdate" in values or values.get("itemprop", "").lower() == "datepublished" or self._article_depth:
                self.times.append(values["datetime"])
        if self._skip:
            if tag in SKIP_TAGS and tag not in VOID_TAGS:
                self._skip.append(tag)
            return
        if tag == "script" and values.get("type", "").strip().lower() == "application/ld+json":
            self._skip.append(tag)
            self._ld_parts = []
            return
        if tag in SKIP_TAGS:
            if tag not in VOID_TAGS:
                self._skip.append(tag)
            return
        if tag == "title":
            self._in_title = True
            return
        if tag in BLOCK_TAGS:
            self._flush()
        if tag in ("article", "main"):
            self._article_depth += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip:
            if tag in self._skip:
                while self._skip and self._skip.pop() != tag:
                    pass
                if tag == "script" and self._ld_parts is not None and not self._skip:
                    self._collect_ld("".join(self._ld_parts))
                    self._ld_parts = None
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in BLOCK_TAGS:
            self._flush()
        if tag in ("article", "main") and self._article_depth > 0:
            self._article_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip:
            if self._ld_parts is not None and self._skip == ["script"]:
                self._ld_parts.append(data)
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        self._buffer.append(data)

    def _flush(self) -> None:
        if not self._buffer:
            return
        text = _collapse("".join(self._buffer))
        self._buffer.clear()
        if text:
            self.blocks.append(text)
            if self._article_depth:
                self.article_blocks.append(text)

    def _collect_ld(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except ValueError:
            return
        stack = [data]
        visited = 0
        while stack and visited < 500:
            item = stack.pop()
            visited += 1
            if isinstance(item, dict):
                value = item.get("datePublished")
                if isinstance(value, str):
                    self.ld_dates.append(value)
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)

    def finish(self) -> None:
        self._flush()


def decode_text(content: bytes, content_type: str | None) -> str:
    candidates: list[str] = []
    match = _HEADER_CHARSET_RE.search(content_type or "")
    if match:
        candidates.append(match.group(1))
    meta = _META_CHARSET_RE.search(content[:4096])
    if meta:
        candidates.append(meta.group(1).decode("ascii", "ignore"))
    for charset in (*candidates, "utf-8"):
        try:
            return content.decode(charset, errors="replace")
        except LookupError:
            continue
    return content.decode("utf-8", errors="replace")


def _join(blocks: list[str], limit: int) -> str:
    out: list[str] = []
    size = 0
    for block in blocks:
        extra = len(block) + (1 if out else 0)
        if size + extra > limit:
            if not out:
                out.append(block[:limit])
            break
        out.append(block)
        size += extra
    return "\n".join(out)


def extract_page(content: bytes, content_type: str | None = None) -> Page:
    """Plain text with block boundaries as newlines, the page title (or None) and `published_at` (0 = unknown)."""
    parser = _Readable()
    try:
        parser.feed(decode_text(content, content_type))
        parser.close()
    except Exception:  # the stdlib parser is lenient; keep whatever was read
        pass
    parser.finish()
    article_size = sum(len(block) for block in parser.article_blocks)
    blocks = parser.article_blocks if article_size >= ARTICLE_MIN_CHARS else parser.blocks
    title = _collapse(parser.meta.get("og:title") or "".join(parser.title_parts))[:MAX_TITLE_CHARS]
    published = 0
    for key in PUBLISHED_META:
        published = parse_timestamp(parser.meta.get(key))
        if published:
            break
    if not published:
        for value in (*parser.ld_dates, *parser.times):
            published = parse_timestamp(value)
            if published:
                break
    return Page(title=title or None, text=_join(blocks, MAX_TEXT_CHARS), published_at=published)


def extract_plain_text(content: bytes, content_type: str | None = None) -> Page:
    lines = [_collapse(line) for line in decode_text(content, content_type).splitlines()]
    return Page(title=None, text=_join([line for line in lines if line], MAX_TEXT_CHARS), published_at=0)
