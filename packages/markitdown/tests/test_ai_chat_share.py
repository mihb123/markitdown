import base64
import io
import json

import pytest

from markitdown import MarkItDown, StreamInfo
from markitdown._exceptions import FileConversionException
from markitdown.converters._ai_chat_share_converter import (
    chat_share_platform,
    claude_snapshot_api_url,
    needs_browser_render,
)

CLAUDE_SHARE = "https://claude.ai/share/c226706e-e954-4795-95b1-4096f0e89fc7"


def _turbo_stream(value):
    """Encode value the way React Router's turbo-stream does (flat, deduplicated)."""
    flat = []
    index = {}

    def enc(v):
        key = json.dumps(v, sort_keys=True) if not isinstance(v, (dict, list)) else None
        if key is not None and key in index:
            return index[key]
        i = len(flat)
        flat.append(None)
        if key is not None:
            index[key] = i
        if isinstance(v, dict):
            flat[i] = {f"_{enc(k)}": enc(x) for k, x in v.items()}
        elif isinstance(v, list):
            flat[i] = [enc(x) for x in v]
        elif v is None:
            flat.pop()
            index.pop(key)
            return -5
        else:
            flat[i] = v
        return i

    enc(value)
    return json.dumps(flat)


def _chatgpt_html(loader_data):
    payload = _turbo_stream({"loaderData": loader_data, "actionData": None})
    return (
        "<html><head><title>ChatGPT</title></head><body><script>"
        f"window.__reactRouterContext.streamController.enqueue({json.dumps(payload)});"
        "</script></body></html>"
    )


def _convert_html(html, url):
    return MarkItDown().convert_stream(
        io.BytesIO(html.encode("utf-8")),
        stream_info=StreamInfo(mimetype="text/html", extension=".html", url=url),
    )


def test_share_url_detection():
    assert chat_share_platform("https://chatgpt.com/share/abc") == "ChatGPT"
    assert chat_share_platform("https://chatgpt.com/s/t_123") == "ChatGPT"
    assert chat_share_platform("https://share.gemini.google/5duDFgF7NXrq") == "Gemini"
    assert chat_share_platform("https://gemini.google.com/share/d690d56f64ce") == "Gemini"
    assert chat_share_platform(CLAUDE_SHARE) == "Claude"
    assert chat_share_platform("https://chatgpt.com/") is None
    assert needs_browser_render("https://g.co/gemini/share/xyz")
    assert not needs_browser_render("https://chatgpt.com/share/abc")
    assert claude_snapshot_api_url(CLAUDE_SHARE).startswith(
        "https://claude.ai/api/chat_snapshots/c226706e-e954-4795-95b1-4096f0e89fc7?"
    )


def test_chatgpt_shared_conversation():
    answer = "\n".join(
        [
            "Use <Entity value=\"Three.js\" category=\"software\"/> for 3D. <Cite refs={[\"turn0search0\"]}/>",
            "",
            "<box gap={2}>",
            "  <badge color=\"success\">Recommended</badge>",
            "  Docs: <Link url=\"https://threejs.org/\" title=\"threejs.org\"/>",
            "</box>",
            "",
            "<list marker=\"number\">",
            "  <list-item>",
            "    **Step one**",
            "  </list-item>",
            "  <list-item>",
            "    **Step two**",
            "  </list-item>",
            "</list>",
            "",
            "```tsx",
            "<group>",
            "  <mesh />",
            "</group>",
            "```",
            "<AsyncImage query=\"pretty pictures\"/>",
        ]
    )
    cite_src = '<Cite refs={["turn0search0"]}/>'
    cite_start = answer.index(cite_src)
    loader = {
        "routes/share.$shareId.($action)": {
            "serverResponse": {
                "data": {
                    "title": "Choosing a 3D stack",
                    "linear_conversation": [
                        {"id": "root", "message": None},
                        {
                            "id": "u1",
                            "message": {
                                "id": "u1",
                                "author": {"role": "user"},
                                "content": {"content_type": "text", "parts": ["Which 3D library?"]},
                                "metadata": {},
                            },
                        },
                        {
                            "id": "t1",
                            "message": {
                                "id": "t1",
                                "author": {"role": "assistant"},
                                "content": {"content_type": "thoughts", "thoughts": []},
                                "metadata": {},
                            },
                        },
                        {
                            "id": "a1",
                            "message": {
                                "id": "a1",
                                "author": {"role": "assistant"},
                                "content": {"content_type": "text", "parts": [answer]},
                                "metadata": {
                                    "genui_components": [
                                        {
                                            "type": "cite",
                                            "start_index": cite_start,
                                            "end_index": cite_start + len(cite_src),
                                            "safe_urls": [
                                                {"url": "https://threejs.org/docs/?utm_source=chatgpt.com"},
                                                {"url": "https://images.openai.com/thumb.png"},
                                            ],
                                        }
                                    ],
                                    "content_references": [
                                        {"type": "search_result", "title": "not the chat title", "matched_text": ""}
                                    ],
                                },
                            },
                        },
                    ],
                }
            }
        }
    }
    result = _convert_html(_chatgpt_html(loader), "https://chatgpt.com/share/abc")
    md = result.markdown

    assert result.title == "Choosing a 3D stack"
    assert md.startswith("# Choosing a 3D stack")
    assert md.index("## User") < md.index("Which 3D library?") < md.index("## ChatGPT")
    assert "Use Three.js for 3D. ([threejs.org](https://threejs.org/docs/))" in md
    assert "images.openai.com" not in md
    assert "**Recommended**" in md
    assert "Docs: [threejs.org](https://threejs.org/)" in md
    assert "1. **Step one**" in md and "2. **Step two**" in md
    # JSX inside code fences is code, not GenUI markup
    assert "```tsx\n<group>\n  <mesh />\n</group>\n```" in md
    assert "AsyncImage" not in md and "<box" not in md and "<badge" not in md
    assert md.count("## ChatGPT") == 1  # the hidden "thoughts" message is skipped


def test_gemini_rendered_share_page():
    jslog = "262206;BardVeMetadataKey:" + base64.b64encode(
        b'[["r_1",null,["https://example.com/article#:~:text=hello"]]]'
    ).decode()
    html = f"""
    <html><body><share-viewer>
      <h1>Web 3D research</h1>
      <div class="publish-time"><span>Created with Fast</span><span>Published today</span></div>
      <share-turn-viewer>
        <user-query><div class="query-text">
          <h5 class="cdk-visually-hidden">You said</h5>
          <p class="query-text-line"># not a heading</p>
          <p class="query-text-line">second line</p>
        </div></user-query>
        <response-container><structured-content-container class="message-content">
          <p>Use <b>Three.js</b>.
            <sources-carousel-inline><source-inline-chip>
              <button aria-label="View source details for citation from Example Site. Press Enter." jslog="{jslog}">Example Site</button>
            </source-inline-chip></sources-carousel-inline>
          </p>
          <code-block><div class="code-block-decoration"><span>Bash</span><button>Copy</button></div>
            <pre><code data-test-id="code-content">npm i three</code></pre></code-block>
          <source-footnote><sup>1</sup></source-footnote>
        </structured-content-container></response-container>
      </share-turn-viewer>
    </share-viewer></body></html>
    """
    result = _convert_html(html, "https://gemini.google.com/share/d690d56f64ce")
    md = result.markdown

    assert result.title == "Web 3D research"
    assert "- Published today" in md
    assert "\\# not a heading\n\nsecond line" in md
    assert "You said" not in md
    assert "Use **Three.js**. ([Example Site](https://example.com/article))" in md
    assert "```bash\nnpm i three\n```" in md
    assert "Copy" not in md


def test_gemini_page_saved_from_browser_is_detected():
    html = (
        "<!-- saved from url=(0043)https://gemini.google.com/share/d690d56f64ce -->\n"
        "<html><body><share-viewer><h1>T</h1><user-query><p class='query-text-line'>hi</p>"
        "</user-query><response-container><message-content><p>hello</p></message-content>"
        "</response-container></share-viewer></body></html>"
    )
    result = MarkItDown().convert_stream(io.BytesIO(html.encode()), stream_info=StreamInfo(extension=".html"))
    assert "- **Platform:** Gemini" in result.markdown
    assert "## User\n\nhi" in result.markdown and "## Gemini\n\nhello" in result.markdown


def test_claude_snapshot_json():
    snapshot = {
        "uuid": "c226706e-e954-4795-95b1-4096f0e89fc7",
        "snapshot_name": "3D web stack",
        "created_at": "2026-11-14T10:00:00.000000+00:00",
        "chat_messages": [
            {
                "index": 1,
                "sender": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "secret reasoning"},
                    {"type": "tool_use", "name": "web_search", "input": {"query": "three.js release"}},
                    {
                        "type": "tool_result",
                        "name": "web_search",
                        "content": [{"type": "knowledge", "title": "three.js", "url": "https://threejs.org/"}],
                    },
                    {
                        "type": "text",
                        "text": "Use **Three.js**.",
                        "citations": [{"details": {"url": "https://threejs.org/?utm_source=x"}}],
                    },
                ],
            },
            {"index": 0, "sender": "human", "content": [{"type": "text", "text": "- which stack?"}]},
        ],
    }
    result = MarkItDown().convert_stream(
        io.BytesIO(json.dumps(snapshot).encode()), stream_info=StreamInfo(extension=".json")
    )
    md = result.markdown
    assert result.title == "3D web stack"
    assert "<https://claude.ai/share/c226706e-e954-4795-95b1-4096f0e89fc7>" in md
    assert md.index("## User") < md.index("\\- which stack?") < md.index("## Claude")
    assert "> *Searched the web:* three.js release" in md
    assert "- [three.js](https://threejs.org/)" in md
    assert "Use **Three.js**. ([threejs.org](https://threejs.org/))" in md
    assert "secret reasoning" not in md


class _FakeResponse:
    def __init__(self, status, content_type, body=b""):
        self.status_code = status
        self.ok = 200 <= status < 300
        self.headers = {"content-type": content_type}
        self.content = body


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self.response


def test_claude_share_url_blocked_explains_workaround():
    session = _FakeSession(_FakeResponse(403, "text/html", b"<title>Just a moment...</title>"))
    with pytest.raises(FileConversionException, match="Webpage, Complete"):
        MarkItDown(requests_session=session).convert(CLAUDE_SHARE)
    assert session.urls == [claude_snapshot_api_url(CLAUDE_SHARE)]


def test_claude_share_url_uses_snapshot_api():
    body = json.dumps(
        {"chat_messages": [{"sender": "human", "text": "hi", "content": []}, {"sender": "assistant", "content": [{"type": "text", "text": "hello"}]}]}
    ).encode()
    session = _FakeSession(_FakeResponse(200, "application/json", body))
    result = MarkItDown(requests_session=session).convert(CLAUDE_SHARE)
    assert "## User\n\nhi" in result.markdown and "## Claude\n\nhello" in result.markdown
    assert f"<{CLAUDE_SHARE}>" in result.markdown
