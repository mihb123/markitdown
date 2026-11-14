"""Convert shared AI chat pages (ChatGPT, Gemini, Claude) into Markdown transcripts.

- ChatGPT share pages embed the conversation in the HTML as a React Router
  turbo-stream payload, so the raw HTML is enough.
- Gemini share pages load the conversation with JavaScript; the URL fetcher
  renders them in a headless browser first (see ``needs_browser_render``) and
  this converter reads the rendered DOM.
- Claude share pages load the conversation from ``/api/chat_snapshots/<id>``.
  That API is behind a bot challenge, so it usually only works from a real
  browser: the converter accepts the snapshot JSON or a page saved from the
  browser ("Save page as... > Webpage, Complete").
"""

import base64
import binascii
import json
import re
from typing import Any, BinaryIO, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlparse, urlsplit, urlunsplit, parse_qsl, urlencode

import bs4

from .._base_converter import DocumentConverter, DocumentConverterResult
from .._exceptions import FileConversionException
from .._stream_info import StreamInfo
from ._markdownify import _CustomMarkdownify

CHATGPT = "ChatGPT"
GEMINI = "Gemini"
CLAUDE = "Claude"

_SHARE_URL_PATTERNS = (
    (CHATGPT, re.compile(r"^https?://(?:www\.)?(?:chatgpt\.com|chat\.openai\.com)/(?:s|share)/", re.I)),
    (GEMINI, re.compile(r"^https?://(?:gemini\.google\.com/share/|share\.gemini\.google/|g\.co/gemini/share/)", re.I)),
    (CLAUDE, re.compile(r"^https?://(?:www\.)?claude\.ai/share/", re.I)),
)
_CLAUDE_SHARE_ID = re.compile(
    r"^https?://(?:www\.)?claude\.ai/share/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})", re.I
)
# Where a page saved from a browser records its original URL.
_SAVED_URL_PATTERNS = (
    re.compile(rb"<!--\s*saved from url=\(\d+\)(\S+?)\s*-->", re.I),
    re.compile(rb"<meta[^>]+property=[\"']og:url[\"'][^>]+content=[\"']([^\"']+)", re.I),
    re.compile(rb"<link[^>]+rel=[\"']canonical[\"'][^>]+href=[\"']([^\"']+)", re.I),
)
_SNIFF_BYTES = 256 * 1024

_HTML_MIME_PREFIXES = ("text/html", "application/xhtml")
_HTML_EXTENSIONS = (".html", ".htm", ".mhtml")
_JSON_MIME_PREFIXES = ("application/json", "text/json")


def chat_share_platform(url: Optional[str]) -> Optional[str]:
    """Return the platform name if url is a shared AI chat link, else None."""
    if not url:
        return None
    for platform, pattern in _SHARE_URL_PATTERNS:
        if pattern.search(url):
            return platform
    return None


def needs_browser_render(url: Optional[str]) -> bool:
    """True when the share page only contains the conversation after JavaScript runs."""
    return chat_share_platform(url) == GEMINI


def claude_snapshot_api_url(url: str) -> Optional[str]:
    """The JSON endpoint the claude.ai share page reads its conversation from."""
    m = _CLAUDE_SHARE_ID.search(url)
    if m is None:
        return None
    return (
        f"https://claude.ai/api/chat_snapshots/{m.group(1)}"
        "?rendering_mode=messages&render_all_tools=true"
    )


def claude_blocked_message(url: str) -> str:
    api = claude_snapshot_api_url(url) or url
    return (
        "claude.ai only serves shared chats to a real browser (its API answers "
        "automated clients with a bot challenge), so this link cannot be fetched "
        "directly. Do one of these, then run markitdown on the saved file:\n"
        f"  1. Open {url} in your browser and save it with Ctrl+S as "
        '"Webpage, Complete" (or "Webpage, Single File").\n'
        f"  2. Or open {api} in your browser and save the JSON (Ctrl+S)."
    )


class AiChatShareConverter(DocumentConverter):
    """Shared ChatGPT / Gemini / Claude conversations as a Markdown transcript."""

    def accepts(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,
    ) -> bool:
        return self._detect(file_stream, stream_info) is not None

    def convert(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,
    ) -> DocumentConverterResult:
        detected = self._detect(file_stream, stream_info)
        if detected is None:  # pragma: no cover - accepts() guards this
            raise FileConversionException("Not a shared AI chat page.")
        platform, kind, source_url = detected
        data = file_stream.read()

        if kind == "json":
            chat = _parse_claude_snapshot(json.loads(_decode(data, stream_info)))
        else:
            html = _decode(data, stream_info)
            if platform == CHATGPT:
                chat = _parse_chatgpt_html(html)
            elif platform == GEMINI:
                chat = _parse_gemini_html(html)
            else:
                chat = _parse_claude_html(html)

        if not chat.turns:
            if platform == CLAUDE and source_url:
                raise FileConversionException(claude_blocked_message(source_url))
            if platform == GEMINI:
                raise FileConversionException(
                    "The Gemini share page has no conversation in its HTML; it must "
                    "be rendered with JavaScript. Install Chromium (or set "
                    "MARKITDOWN_BROWSER to a Chromium-based browser) and retry, or "
                    "save the page from your browser and convert the saved file."
                )
            raise FileConversionException(
                f"No messages found in the shared {platform} page."
            )

        chat.platform = platform
        chat.url = chat.url or source_url
        markdown = chat.to_markdown()
        return DocumentConverterResult(markdown=markdown, title=chat.title)

    # -- detection ---------------------------------------------------------

    def _detect(
        self, file_stream: BinaryIO, stream_info: StreamInfo
    ) -> Optional[Tuple[str, str, Optional[str]]]:
        """Return (platform, "html"|"json", source url) or None."""
        mimetype = (stream_info.mimetype or "").lower()
        extension = (stream_info.extension or "").lower()
        is_html = extension in _HTML_EXTENSIONS or mimetype.startswith(_HTML_MIME_PREFIXES)
        is_json = extension == ".json" or mimetype.startswith(_JSON_MIME_PREFIXES)
        if not (is_html or is_json):
            return None

        url = stream_info.url
        platform = chat_share_platform(url)

        cur_pos = file_stream.tell()
        try:
            head = file_stream.read(_SNIFF_BYTES)
        finally:
            file_stream.seek(cur_pos)

        if is_json:
            # A Claude chat snapshot: {"chat_messages": [...], ...}
            if b'"chat_messages"' in head and platform in (None, CLAUDE):
                return CLAUDE, "json", url
            return None

        if platform is None:
            for pattern in _SAVED_URL_PATTERNS:
                m = pattern.search(head)
                if m:
                    candidate = m.group(1).decode("utf-8", "replace")
                    platform = chat_share_platform(candidate)
                    if platform is not None:
                        url = candidate
                        break
        if platform is None:
            return None
        return platform, "html", url


# ---------------------------------------------------------------------------
# Transcript model
# ---------------------------------------------------------------------------


class _Turn:
    def __init__(self, role: str, markdown: str):
        self.role = role  # "user" | "assistant"
        self.markdown = markdown.strip()


class _Chat:
    def __init__(self) -> None:
        self.title: Optional[str] = None
        self.platform: str = ""
        self.url: Optional[str] = None
        self.meta: List[str] = []
        self.turns: List[_Turn] = []

    def add(self, role: str, markdown: str) -> None:
        if markdown and markdown.strip():
            self.turns.append(_Turn(role, markdown))

    def to_markdown(self) -> str:
        title = self.title or f"{self.platform} conversation"
        out = [f"# {title}", ""]
        info = [f"- **Platform:** {self.platform}"]
        if self.url:
            info.append(f"- **Source:** <{self.url}>")
        info.extend(f"- {m}" for m in self.meta)
        out.extend(info)
        for turn in self.turns:
            who = "User" if turn.role == "user" else self.platform
            out.extend(["", "---", "", f"## {who}", "", turn.markdown])
        return _collapse_blank_lines("\n".join(out)) + "\n"


def _collapse_blank_lines(text: str) -> str:
    # Never touch the inside of fenced code blocks.
    parts = re.split(r"(^```.*?^```[^\n]*$)", text, flags=re.M | re.S)
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(r"\n{3,}", "\n\n", parts[i])
    return "".join(parts).strip()


def _decode(data: bytes, stream_info: StreamInfo) -> str:
    for encoding in (stream_info.charset, "utf-8"):
        if not encoding:
            continue
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _clean_url(url: str) -> str:
    """Drop tracking parameters (utm_*) and text fragments from a link."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.startswith("utm_")]
    fragment = "" if parts.fragment.startswith(":~:") else parts.fragment
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), fragment))


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host or url


def _md_link_text(text: str) -> str:
    return text.replace("[", "(").replace("]", ")").strip()


# ---------------------------------------------------------------------------
# ChatGPT
# ---------------------------------------------------------------------------

_ENQUEUE = re.compile(r'streamController\.enqueue\(("(?:[^"\\]|\\.)*")\)', re.S)
_HIDDEN_CONTENT_TYPES = {
    "thoughts",
    "reasoning_recap",
    "user_editable_context",
    "model_editable_context",
    "system_error",
    "tether_browsing_display",
    "tether_quote",
    "computer_output",
}


def _decode_turbo_stream(html: str) -> Any:
    """Decode the first React Router turbo-stream chunk embedded in the page."""
    chunks = [json.loads(m.group(1)) for m in _ENQUEUE.finditer(html)]
    if not chunks:
        return None
    first = chunks[0].split("\n", 1)[0]
    flat = json.loads(first)
    if not isinstance(flat, list) or not flat:
        return None

    memo: Dict[int, Any] = {}

    def hydrate(index: Any) -> Any:
        if not isinstance(index, int) or isinstance(index, bool):
            return index
        if index < 0:  # undefined / null / NaN / holes
            return None
        if index in memo:
            return memo[index]
        if index >= len(flat):
            return None
        value = flat[index]
        if isinstance(value, dict):
            obj: Dict[str, Any] = {}
            memo[index] = obj
            for key, vi in value.items():
                if key.startswith("_") and key[1:].isdigit():
                    name = flat[int(key[1:])]
                else:
                    name = key
                obj[str(name)] = hydrate(vi)
            return obj
        if isinstance(value, list):
            if value and isinstance(value[0], str):
                # Typed value: ["D", ts] date, ["P", id] promise, ["Y", ...], ...
                tag = value[0]
                memo[index] = value[1] if tag == "D" and len(value) > 1 else None
                return memo[index]
            arr: List[Any] = []
            memo[index] = arr
            arr.extend(hydrate(vi) for vi in value)
            return arr
        memo[index] = value
        return value

    return hydrate(0)


def _iter_messages(node: Any) -> Iterator[Dict[str, Any]]:
    """Depth-first, document-order walk yielding ChatGPT message objects."""
    if isinstance(node, dict):
        if isinstance(node.get("author"), dict) and isinstance(node.get("content"), dict):
            yield node
            return
        # Prefer the linear conversation when both it and the mapping are present.
        if isinstance(node.get("linear_conversation"), list):
            yield from _iter_messages(node["linear_conversation"])
            return
        for value in node.values():
            yield from _iter_messages(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_messages(value)


def _find_key(node: Any, key: str) -> Optional[Any]:
    if isinstance(node, dict):
        if isinstance(node.get(key), str) and node[key].strip():
            return node[key]
        for value in node.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_key(value, key)
            if found is not None:
                return found
    return None


def _find_conversation_title(node: Any) -> Optional[str]:
    """The title of a /share/<id> conversation (not of a search result or widget)."""
    if isinstance(node, dict):
        title = node.get("title")
        if isinstance(title, str) and title.strip() and (
            "mapping" in node or "linear_conversation" in node
        ):
            return title.strip()
        children = node.values()
    elif isinstance(node, list):
        children = node
    else:
        return None
    for child in children:
        found = _find_conversation_title(child)
        if found is not None:
            return found
    return None


def _parse_chatgpt_html(html: str) -> _Chat:
    chat = _Chat()
    root = _decode_turbo_stream(html)
    loader = root.get("loaderData") if isinstance(root, dict) else None
    if not isinstance(loader, dict):
        return chat
    routes = {k: v for k, v in loader.items() if k != "root"} or loader

    seen = set()
    for message in _iter_messages(routes):
        msg_id = message.get("id")
        if msg_id in seen:
            continue
        seen.add(msg_id)
        role = (message.get("author") or {}).get("role")
        metadata = message.get("metadata") or {}
        if role not in ("user", "assistant") or metadata.get("is_visually_hidden_from_conversation"):
            continue
        content = message.get("content") or {}
        if content.get("content_type") in _HIDDEN_CONTENT_TYPES:
            continue
        markdown = _chatgpt_content_markdown(content, metadata)
        if not markdown.strip():
            continue
        # Consecutive assistant messages (e.g. around tool calls) form one turn.
        if chat.turns and chat.turns[-1].role == role == "assistant":
            chat.turns[-1].markdown += "\n\n" + markdown.strip()
        else:
            chat.add(role, markdown)

    title = _find_conversation_title(routes)
    if title is None:
        # /s/<id> posts have a generic og_title; the answer's own heading reads better.
        first = chat.turns[0].markdown if chat.turns else ""
        heading = re.search(r"^#{1,2}\s+(.+)$", first, re.M)
        title = heading.group(1).strip() if heading else _find_key(routes, "og_title")
    chat.title = title
    return chat


def _chatgpt_content_markdown(content: Dict[str, Any], metadata: Dict[str, Any]) -> str:
    content_type = content.get("content_type")
    if content_type == "code":
        lang = content.get("language") or ""
        if lang == "unknown":
            lang = ""
        return f"```{lang}\n{content.get('text', '')}\n```"
    if "parts" not in content:
        return str(content.get("text") or "")

    pieces = []
    for part in content.get("parts") or []:
        if isinstance(part, str):
            pieces.append(part)
        elif isinstance(part, dict):
            if part.get("content_type") == "image_asset_pointer":
                pieces.append("*[image]*")
            elif isinstance(part.get("text"), str):
                pieces.append(part["text"])
    text = "\n\n".join(pieces)
    text = _apply_genui_components(text, metadata.get("genui_components") or [])
    text = _apply_content_references(text, metadata.get("content_references") or [])
    text = _render_genui_markup(text)
    # Leftover citation markers, e.g. "citeturn0search1" or 【4†source】.
    text = re.sub("[^]*", "", text)
    text = re.sub(r"【\d+(?::\d+)?†[^】]*】", "", text)
    return text


def _non_image_urls(component: Dict[str, Any]) -> List[str]:
    urls = []
    for item in component.get("safe_urls") or []:
        url = item.get("url") if isinstance(item, dict) else item
        if not isinstance(url, str) or "images.openai.com" in url:
            continue
        url = _clean_url(url)
        if url not in urls:
            urls.append(url)
    return urls


def _apply_genui_components(text: str, components: List[Dict[str, Any]]) -> str:
    """Replace inline GenUI components (<Link/>, <Cite/>, ...) using their resolved data."""
    spans = []
    for comp in components:
        start, end = comp.get("start_index"), comp.get("end_index")
        if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(text):
            continue
        source = text[start:end]
        kind = comp.get("type")
        if kind == "cite":
            urls = _non_image_urls(comp)
            replacement = (
                "(" + ", ".join(f"[{_domain(u)}]({u})" for u in urls) + ")" if urls else ""
            )
            if replacement and start > 0 and not text[start - 1].isspace():
                replacement = " " + replacement
        elif kind == "link":
            urls = _non_image_urls(comp)
            title = _attr(source, "title") or (urls[0] if urls else "")
            url = urls[0] if urls else _attr(source, "url")
            replacement = f"[{_md_link_text(title)}]({url})" if url else title
        elif kind == "entity":
            replacement = _attr(source, "value") or ""
        else:  # async images, automations, ...
            replacement = ""
        spans.append((start, end, replacement))
    for start, end, replacement in sorted(spans, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def _apply_content_references(text: str, references: List[Dict[str, Any]]) -> str:
    for ref in references:
        matched = ref.get("matched_text")
        alt = ref.get("alt")
        if not matched or ref.get("type") in ("hidden", "client_defined_widget"):
            continue
        if alt is None:
            if "" in matched or "【" in matched:
                text = text.replace(matched, "", 1)
            continue
        text = text.replace(matched, alt, 1)
    return text


def _attr(tag: str, name: str) -> Optional[str]:
    m = re.search(rf'\b{name}="([^"]*)"', tag) or re.search(rf"\b{name}='([^']*)'", tag)
    return m.group(1) if m else None


# Self-closing / inline GenUI components left after _apply_genui_components.
_INLINE_COMPONENTS = (
    (re.compile(r"<Entity\b[^>]*?/>"), lambda m: _attr(m.group(0), "value") or ""),
    (
        re.compile(r"<Link\b[^>]*?/>"),
        lambda m: (
            f"[{_md_link_text(_attr(m.group(0), 'title') or _attr(m.group(0), 'url') or '')}]"
            f"({_clean_url(_attr(m.group(0), 'url') or '')})"
            if _attr(m.group(0), "url")
            else ""
        ),
    ),
    (re.compile(r"<[A-Z][A-Za-z]*\b[^<>]*?/>"), lambda m: ""),
)
_BLOCK_TAGS = (
    "box", "row", "col", "column", "stack", "group", "card", "list", "list-item",
    "radio-group", "checkbox-group", "grid", "section", "callout",
)
_BLOCK_NAMES = "|".join(map(re.escape, sorted(_BLOCK_TAGS, key=len, reverse=True)))
# (?![\w-]) so that "<list" does not match the start of "<list-item>"
_BLOCK_OPEN = re.compile(r"^\s*<(%s)(?![\w-])[^>]*>\s*$" % _BLOCK_NAMES)
_BLOCK_CLOSE = re.compile(r"^\s*</(%s)>\s*$" % _BLOCK_NAMES)
_ARROWS = {"arrow-down": "↓", "arrow-up": "↑", "arrow-right": "→", "arrow-left": "←"}


def _render_inline_genui(line: str) -> str:
    for pattern, repl in _INLINE_COMPONENTS:
        line = pattern.sub(repl, line)
    line = re.sub(r"<badge\b[^>]*>(.*?)</badge>", r"**\1**", line)
    line = re.sub(r"<title\b[^>]*>(.*?)</title>", r"**\1**", line)
    line = re.sub(
        r"<text\b([^>]*)>(.*?)</text>",
        lambda m: f"*{m.group(2).strip()}*" if 'color="secondary"' in m.group(1) else m.group(2),
        line,
    )
    line = re.sub(r"<radio\b[^>]*>(.*?)</radio>", r"- \1", line)
    line = re.sub(r"<checkbox\b[^>]*>(.*?)</checkbox>", r"- [ ] \1", line)
    # Interactive buttons only make sense in the live UI.
    line = re.sub(r"<button\b.*>[^<>]*</button>", "", line)
    line = re.sub(
        r'<icon\b[^>]*?name="([^"]+)"[^>]*/>', lambda m: _ARROWS.get(m.group(1), ""), line
    )
    line = re.sub(r"<icon\b[^>]*/>", "", line)
    return line


def _render_genui_markup(text: str) -> str:
    """Turn ChatGPT's GenUI layout markup into plain Markdown (code fences untouched)."""
    out: List[str] = []
    depth = 0
    list_stack: List[List[Any]] = []  # [marker, counter, first_line_pending, depth]
    in_fence = False
    fence = ""
    for raw in text.split("\n"):
        stripped = raw.strip()
        fence_m = re.match(r"^\s*(`{3,}|~{3,})", raw)
        if in_fence:
            out.append(raw[min(len(raw) - len(raw.lstrip(" ")), 2 * depth):])
            if fence_m and fence_m.group(1).startswith(fence):
                in_fence = False
            continue
        if fence_m:
            in_fence, fence = True, fence_m.group(1)
            out.append(_indent_for(raw, depth, list_stack))
            continue

        if re.match(r"^\{@\w+.*\}$", stripped):  # {@body const [x,setX] = ...}
            continue
        open_m = _BLOCK_OPEN.match(raw)
        if open_m:
            tag = open_m.group(1)
            if tag == "list":
                marker = "number" if 'marker="number"' in raw else "bullet"
                list_stack.append([marker, 0, False, depth + 1])
            elif tag == "list-item" and list_stack:
                list_stack[-1][1] += 1
                list_stack[-1][2] = True
            depth += 1
            out.append("")
            continue
        if _BLOCK_CLOSE.match(raw):
            tag = _BLOCK_CLOSE.match(raw).group(1)  # type: ignore[union-attr]
            if tag == "list" and list_stack:
                list_stack.pop()
            depth = max(0, depth - 1)
            out.append("")
            continue
        if re.match(r"^\s*<divider\b[^>]*/>\s*$", raw):
            out.extend(["", "---", ""])
            continue
        if re.match(r"^\s*<spacer\b[^>]*/>\s*$", raw):
            continue

        line = _render_inline_genui(raw)
        if not line.strip() and stripped:
            continue  # the line held only components that render to nothing
        line = _indent_for(line, depth, list_stack)
        if depth and line.strip() and out and out[-1].strip() and not _is_md_block_line(line):
            out.append("")  # children of a GenUI container are separate blocks
        out.append(line)
    return "\n".join(out)


def _is_md_block_line(line: str) -> bool:
    """Lines that legitimately follow one another without a blank line."""
    return bool(re.match(r"^\s*(?:[-*+]\s|\d+[.)]\s|\|)", line))


def _indent_for(line: str, depth: int, list_stack: List[List[Any]]) -> str:
    """Dedent GenUI nesting, prefixing the first line of a <list-item>."""
    leading = len(line) - len(line.lstrip(" "))
    line = line[min(leading, 2 * depth):]
    if not list_stack or not line.strip():
        return line
    item = list_stack[-1]
    marker = f"{item[1]}. " if item[0] == "number" else "- "
    if item[2]:
        item[2] = False
        return marker + line
    return " " * len(marker) + line


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

_GEMINI_DROP = (
    "button", "mat-icon", "source-footnote", "processing-state", "script", "style",
    "gem-icon-button", "tts-control-v2", "message-actions", "sources-list",
)


def _parse_gemini_html(html: str) -> _Chat:
    chat = _Chat()
    soup = bs4.BeautifulSoup(html, "html.parser")
    container = soup.find("share-viewer") or soup

    title = container.find("h1")
    if title is not None and title.get_text(strip=True):
        chat.title = title.get_text(" ", strip=True)
    else:
        conv_title = soup.find(class_=re.compile(r"conversation-title"))
        if conv_title is not None:
            chat.title = conv_title.get_text(" ", strip=True)
    publish = container.find(class_="publish-time")
    if publish is not None:
        for span in publish.find_all("span", recursive=False):
            text = span.get_text(" ", strip=True)
            if text:
                chat.meta.append(text)

    for element in container.find_all(["user-query", "response-container", "model-response"]):
        if element.find_parent(["user-query", "response-container", "model-response"]):
            continue
        if element.name == "user-query":
            chat.add("user", _gemini_user_query(element))
        else:
            chat.add("assistant", _gemini_response(element))
    return chat


def _gemini_user_query(element: bs4.Tag) -> str:
    lines = [p.get_text(" ", strip=False).strip() for p in element.find_all(class_="query-text-line")]
    if not any(lines):
        box = element.find(class_="query-text") or element
        for hidden in box.find_all(class_="cdk-visually-hidden"):
            hidden.decompose()
        return box.get_text("\n", strip=True)
    files = [
        f.get_text(" ", strip=True)
        for f in element.find_all(class_=re.compile(r"file-name|new-file-name"))
        if f.get_text(strip=True)
    ]
    text = "\n\n".join(_escape_plain_text(line) for line in lines)
    if files:
        text += "\n\n" + "\n".join(f"*[attachment: {name}]*" for name in files)
    return text


def _escape_plain_text(text: str) -> str:
    """User prompts are plain text; keep them from turning into Markdown headings/lists."""
    return re.sub(r"^(\s*)([#>*+-]|\d+\.)(\s)", r"\1\\\2\3", text, flags=re.M)


def _gemini_source_chip(chip: bs4.Tag) -> str:
    button = chip.find("button")
    label = (button.get("aria-label") if button else "") or ""
    name_m = re.search(r"citation from (.+?)\.(?:\s|$)", label)
    name = name_m.group(1) if name_m else chip.get_text(" ", strip=True)
    url = None
    jslog = (button.get("jslog") if button else "") or ""
    for blob in re.findall(r"[A-Za-z0-9+/=_-]{40,}", jslog):
        try:
            decoded = base64.b64decode(blob + "=" * (-len(blob) % 4), altchars=b"-_" if "-" in blob or "_" in blob else None)
        except (binascii.Error, ValueError):
            continue
        m = re.search(rb'https?://[^"\\\s]+', decoded)
        if m:
            url = _clean_url(m.group(0).decode("utf-8", "replace"))
            break
    name = _md_link_text(re.sub(r"\s*\+\s*\d+$", "", name or ""))
    if url:
        return f" ([{name or _domain(url)}]({url}))"
    return f" ({name})" if name else ""


def _gemini_response(element: bs4.Tag) -> str:
    content = (
        element.find("structured-content-container")
        or element.find("message-content")
        or element.find(class_="markdown")
        or element
    )
    for chip in content.find_all("source-inline-chip"):
        chip.replace_with(bs4.NavigableString(_gemini_source_chip(chip)))
    for carousel in content.find_all("sources-carousel-inline"):
        # Keep the citation on the line of the sentence it belongs to.
        prev = carousel.previous_sibling
        if isinstance(prev, bs4.NavigableString) and not isinstance(prev, bs4.Comment):
            prev.replace_with(bs4.NavigableString(prev.rstrip()))
        carousel.unwrap()

    for block in content.find_all("code-block"):
        decoration = block.find(class_="code-block-decoration")
        lang = ""
        if decoration is not None:
            label = decoration.find("span")
            lang = (label.get_text(strip=True) if label else "").lower()
        code = block.find(attrs={"data-test-id": "code-content"}) or block.find("code")
        pre = bs4.BeautifulSoup("", "html.parser").new_tag("pre")
        pre["data-language"] = lang
        code_tag = bs4.BeautifulSoup("", "html.parser").new_tag("code")
        code_tag.string = code.get_text() if code is not None else block.get_text()
        pre.append(code_tag)
        block.replace_with(pre)

    for tag in content.find_all(_GEMINI_DROP):
        tag.decompose()
    for hidden in content.find_all(class_="cdk-visually-hidden"):
        hidden.decompose()

    markdown = _CustomMarkdownify(
        code_language_callback=lambda el: el.get("data-language") or ""
    ).convert_soup(content)
    # The chips sit after a space already: "sentence.  ([Source](url))"
    return re.sub(r"[ \t]{2,}\(\[", " ([", markdown)


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------


def _parse_claude_snapshot(data: Any) -> _Chat:
    chat = _Chat()
    if not isinstance(data, dict):
        return chat
    chat.title = data.get("snapshot_name") or data.get("name") or None
    created = data.get("created_at")
    if isinstance(created, str) and created:
        chat.meta.append(f"**Shared:** {created[:19].replace('T', ' ')}")
    share_id = data.get("uuid")
    if isinstance(share_id, str) and share_id:
        chat.url = f"https://claude.ai/share/{share_id}"

    messages = data.get("chat_messages") or []
    if all(isinstance(m, dict) and isinstance(m.get("index"), int) for m in messages):
        messages = sorted(messages, key=lambda m: m["index"])
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = "user" if message.get("sender") == "human" else "assistant"
        chat.add(role, _claude_message_markdown(message, role))
    return chat


def _claude_message_markdown(message: Dict[str, Any], role: str) -> str:
    blocks = message.get("content")
    pieces: List[str] = []
    if not isinstance(blocks, list) or not blocks:
        text = message.get("text") or ""
        pieces.append(_escape_plain_text(text) if role == "user" else text)
    else:
        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                text = block.get("text") or ""
                if role == "user":
                    text = _escape_plain_text(text)
                pieces.append(text + _claude_citations(block.get("citations")))
            elif kind == "tool_use":
                pieces.append(_claude_tool_use(block))
            elif kind == "tool_result":
                sources = _claude_tool_sources(block)
                if sources:
                    pieces.append(sources)
            # "thinking" and other internal blocks are not part of the visible transcript.

    for attachment in message.get("attachments") or []:
        if isinstance(attachment, dict) and attachment.get("file_name"):
            pieces.append(f"*[attachment: {attachment['file_name']}]*")
    for file in message.get("files") or message.get("files_v2") or []:
        if isinstance(file, dict) and file.get("file_name"):
            pieces.append(f"*[file: {file['file_name']}]*")
    return "\n\n".join(p for p in pieces if p and p.strip())


def _claude_citations(citations: Any) -> str:
    if not isinstance(citations, list):
        return ""
    links: List[str] = []
    for citation in citations:
        if not isinstance(citation, dict):
            continue
        details = citation.get("details") if isinstance(citation.get("details"), dict) else {}
        url = details.get("url") or citation.get("url")
        if isinstance(url, str) and url:
            link = f"[{_domain(url)}]({_clean_url(url)})"
            if link not in links:
                links.append(link)
    return f" ({', '.join(links)})" if links else ""


def _claude_tool_use(block: Dict[str, Any]) -> str:
    name = block.get("name") or "tool"
    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
    if name in ("web_search", "web_fetch"):
        target = tool_input.get("query") or tool_input.get("url") or ""
        label = "Searched the web" if name == "web_search" else "Fetched"
        return f"> *{label}:* {target}".rstrip()
    if name in ("artifacts", "create_file", "str_replace", "text_editor") and isinstance(
        tool_input.get("content") or tool_input.get("file_text"), str
    ):
        body = tool_input.get("content") or tool_input.get("file_text")
        title = tool_input.get("title") or tool_input.get("path") or name
        lang = tool_input.get("language") or ""
        return f"**{title}**\n\n```{lang}\n{body}\n```"
    return f"> *Used tool:* `{name}`"


def _claude_tool_sources(block: Dict[str, Any]) -> str:
    items = block.get("content")
    if not isinstance(items, list):
        return ""
    links: List[str] = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("url"), str):
            title = _md_link_text(item.get("title") or _domain(item["url"]))
            link = f"- [{title}]({_clean_url(item['url'])})"
            if link not in links:
                links.append(link)
    if not links:
        return ""
    return "<details><summary>Sources</summary>\n\n" + "\n".join(links) + "\n\n</details>"


def _parse_claude_html(html: str) -> _Chat:
    """A claude.ai share page saved from a browser after it rendered."""
    chat = _Chat()
    soup = bs4.BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript", "button", "svg"]):
        tag.decompose()

    selectors = '[data-testid="user-message"], .font-claude-response, .font-claude-message'
    nodes = [n for n in soup.select(selectors) if not n.find_parent(class_=re.compile(r"font-claude-(response|message)"))]
    nodes = [n for n in nodes if not (n.get("data-testid") == "user-message" and n.find_parent(attrs={"data-testid": "user-message"}))]
    for node in nodes:
        if node.get("data-testid") == "user-message":
            chat.add("user", _escape_plain_text(node.get_text("\n", strip=True)))
        else:
            chat.add("assistant", _CustomMarkdownify().convert_soup(node))

    title = soup.find("title")
    if title is not None:
        text = re.sub(r"\s*[-|]\s*Claude\s*$", "", title.get_text(strip=True))
        if text and text.lower() != "claude":
            chat.title = text
    return chat
