"""
Cloud vision-LLM client (OpenAI-compatible) for image `--text` / `--des`.

Reads its configuration from environment variables or a `.env` file and calls
an OpenAI-compatible ``/chat/completions`` endpoint with the image attached as a
data URI. Used by :class:`ImageConverter` when the caller explicitly requests
cloud processing; otherwise the converter uses its offline models.

Only the core ``requests`` dependency is used — no extra SDK is required.

Configuration keys (env var or `.env`):
    MARKITDOWN_VLM_BASE_URL   OpenAI-compatible base URL (e.g. https://host/v1)
    MARKITDOWN_VLM_API_KEY    API key
    MARKITDOWN_VLM_MODEL      Vision-capable model id

The `.env` file is looked up via $MARKITDOWN_ENV_FILE, then a `.env` in the
current directory or any parent, then ~/.config/markitdown/.env.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import threading
import time
from typing import Any, BinaryIO, Dict, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ._timing import log_timing
from .. import _env
from .._stream_info import StreamInfo

logger = logging.getLogger("markitdown.vlm")

_DEFAULT_TIMEOUT_SECONDS = 120
_DEFAULT_MAX_RETRIES = 1

# One pooled session for the whole process instead of a fresh connection (and TLS
# handshake) per image. Under a queue of receipts that is the difference between
# reusing one connection and opening hundreds of them.
_session_lock = threading.Lock()
_session: Optional["requests.Session"] = None


# Read a positive int setting, falling back to the default when unset or unusable.
def _int_setting(key: str, default: int, *, minimum: int = 0) -> int:
    raw = _env.resolve(key)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value >= minimum else default


def request_timeout_seconds() -> int:
    return _int_setting("MARKITDOWN_VLM_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_SECONDS, minimum=1)


def max_retries() -> int:
    return _int_setting("MARKITDOWN_VLM_MAX_RETRIES", _DEFAULT_MAX_RETRIES)


# Build the shared session. Retries cover exactly the failures that are worth
# repeating — a rate limit or a gateway hiccup at the provider — and honour the
# Retry-After header when one is sent. A 4xx that is not 429 is the caller's own
# fault and is never retried.
def _get_session() -> "requests.Session":
    global _session

    if _session is not None:
        return _session

    with _session_lock:
        if _session is None:
            retry = Retry(
                total=max_retries(),
                connect=max_retries(),
                read=max_retries(),
                status=max_retries(),
                backoff_factor=1.0,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(["POST"]),
                respect_retry_after_header=True,
                raise_on_status=False,
            )
            session = requests.Session()
            adapter = HTTPAdapter(
                pool_connections=4, pool_maxsize=8, max_retries=retry
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            _session = session
        return _session

_OCR_PROMPT = (
    "You are an OCR engine. Extract ALL text from this image exactly as it "
    "appears. Preserve the reading order, line breaks, lists, headings, and "
    "table structure. Output only the extracted text as clean Markdown, with "
    "no explanations, comments, or code fences."
)

_DESCRIPTION_LANGUAGE_NAMES = {
    "en": "English",
    "vi": "Vietnamese",
}


class VisionApiError(RuntimeError):
    """Raised when the configured cloud VLM request fails."""


# Resolve configuration, with real environment variables taking precedence over
# any `.env` file value.
def _config() -> Dict[str, Optional[str]]:
    return {
        "base_url": _env.resolve("MARKITDOWN_VLM_BASE_URL"),
        "api_key": _env.resolve("MARKITDOWN_VLM_API_KEY"),
        "model": _env.resolve("MARKITDOWN_VLM_MODEL"),
    }


# Report whether a usable cloud VLM configuration is present.
def is_configured() -> bool:
    config = _config()
    return all(config.get(key) for key in ("base_url", "api_key", "model"))


# Ensure cloud conversion has the configuration needed to call the VLM API.
def ensure_configured() -> None:
    if is_configured():
        return

    raise VisionApiError(
        "Cloud VLM API is not configured. Set MARKITDOWN_VLM_BASE_URL, "
        "MARKITDOWN_VLM_API_KEY, and MARKITDOWN_VLM_MODEL before using --cloud."
    )


# Encode the image stream as a data URI without disturbing the stream position.
def _to_data_uri(file_stream: BinaryIO, stream_info: Optional[StreamInfo]) -> str:
    content_type = stream_info.mimetype if stream_info else None
    if not content_type:
        extension = stream_info.extension if stream_info else None
        content_type, _ = mimetypes.guess_type("_dummy" + (extension or ""))
    if not content_type:
        content_type = "image/png"

    cur_pos = file_stream.tell()
    try:
        encoded = base64.b64encode(file_stream.read()).decode("utf-8")
    finally:
        file_stream.seek(cur_pos)
    return f"data:{content_type};base64,{encoded}"


# Return a compact response body that is safe to show in CLI errors.
def _compact_body(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    value = " ".join(value.split())
    if len(value) > 500:
        return value[:497] + "..."
    return value


# Extract the most useful error message from an OpenAI-compatible error body.
def _response_error_message(response: "requests.Response") -> str:
    try:
        body = response.json()
    except ValueError:
        body = response.text

    message = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("code")
        elif isinstance(error, str):
            message = error
        message = message or body.get("message")

    compact = _compact_body(message or body)
    if compact:
        return (
            f"Cloud VLM API returned HTTP {response.status_code} "
            f"{response.reason}: {compact}"
        )
    return f"Cloud VLM API returned HTTP {response.status_code} {response.reason}"


# Build a detailed image-description prompt for the requested output language.
def description_prompt(language: str = "en") -> str:
    language_name = _DESCRIPTION_LANGUAGE_NAMES.get(language, "English")
    return (
        "You are a careful image analyst. Describe the image with high factual "
        "precision for a user who cannot see it. Mention the main subject, "
        "important objects, visible text, layout, colors, scene context, and any "
        "notable relationships or actions. Do not invent details that are not "
        f"visible. Write the answer in {language_name}. Output only the "
        "description as clean Markdown."
    )


# Send one image + prompt to the OpenAI-compatible endpoint and return the text
# content of the reply. Raises on network/HTTP/parse errors so CLI users can
# see the configured cloud backend failure instead of a silent fallback.
def _run_vision(
    prompt: str, file_stream: BinaryIO, stream_info: Optional[StreamInfo]
) -> Optional[str]:
    config = _config()
    t_start = time.perf_counter()
    with log_timing("image.vlm.data_uri", enabled=False):
        data_uri = _to_data_uri(file_stream, stream_info)

    url = config["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": config["model"],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            }
        ],
        "max_tokens": 4096,
    }
    headers = {
        "Authorization": f"Bearer {config['api_key']}",
        "Content-Type": "application/json",
    }

    timeout = request_timeout_seconds()
    logger.debug(
        "[vlm] Requesting %s model=%s (image_uri_len=%d, timeout=%ds)",
        url,
        config["model"],
        len(data_uri),
        timeout,
    )
    try:
        t_req_start = time.perf_counter()
        with log_timing("image.vlm.request", enabled=False):
            response = _get_session().post(
                url, json=payload, headers=headers, timeout=timeout
            )
        elapsed = time.perf_counter() - t_req_start
        logger.debug(
            "[vlm] Cloud VLM responded HTTP %d in %.3fs (model=%s)",
            response.status_code,
            elapsed,
            config["model"],
        )
    except requests.Timeout as exc:
        elapsed = time.perf_counter() - t_start
        logger.warning(
            "[vlm] Cloud VLM timed out after %.3fs (timeout=%ds, model=%s)",
            elapsed,
            timeout,
            config["model"],
        )
        raise VisionApiError(
            f"Cloud VLM API request timed out after {timeout}s."
        ) from exc
    except requests.RequestException as exc:
        elapsed = time.perf_counter() - t_start
        logger.warning(
            "[vlm] Cloud VLM request failed after %.3fs: %s", elapsed, exc
        )
        raise VisionApiError(f"Cloud VLM API request failed: {exc}") from exc

    if response.status_code >= 400:
        err_msg = _response_error_message(response)
        logger.warning("[vlm] %s", err_msg)
        raise VisionApiError(err_msg)

    try:
        return _parse_response(response)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise VisionApiError(
            f"Cloud VLM API returned an unexpected response: {exc}"
        ) from exc


# Extract the assistant text from either a plain JSON chat-completion body or a
# Server-Sent-Events (streaming) body — this endpoint streams `data:` chunks
# even for non-streaming requests.
def _parse_response(response: "requests.Response") -> Optional[str]:
    # `requests` defaults to ISO-8859-1 for bodies without an explicit charset
    # (common for text/event-stream), which mojibakes UTF-8 text — force UTF-8.
    response.encoding = "utf-8"
    content_type = response.headers.get("content-type", "")
    body = response.text
    if "text/event-stream" in content_type or body.lstrip().startswith("data:"):
        content = _parse_sse(body)
        return content or None
    content = response.json()["choices"][0]["message"]["content"]
    return content.strip() if isinstance(content, str) else None


# Concatenate the `delta.content` pieces from an SSE stream into one string.
def _parse_sse(body: str) -> str:
    parts = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[len("data:") :].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            event = json.loads(chunk)
        except ValueError:
            continue
        for choice in event.get("choices", []):
            delta = choice.get("delta") or {}
            piece = delta.get("content")
            if piece:
                parts.append(piece)
            else:
                message = choice.get("message") or {}
                if isinstance(message.get("content"), str):
                    parts.append(message["content"])
    return "".join(parts).strip()


# Build the prompt that turns a transfer receipt into the `--bank` JSON object.
# The model is pinned to the beneficiary side because most receipts show the
# sender's bank in the app chrome (an MB Bank app confirming a transfer into a
# Vietcombank account), and is told to answer "" rather than guess.
def bank_transfer_prompt() -> str:
    return (
        "You are reading a screenshot or photo of a bank money-transfer "
        "receipt, usually from a Vietnamese banking app (Vietcombank, MB Bank, "
        "Techcombank, ACB, BIDV, VietinBank, MoMo, ...).\n\n"
        "Extract these fields about the BENEFICIARY — the account that RECEIVES "
        "the money — and reply with exactly one JSON object:\n"
        '{"amount": "", "account_number": "", "account_name": "", '
        '"bank": "", "content": ""}\n\n'
        "Rules:\n"
        "- amount: the transferred amount, digits only, no separators and no "
        'currency (write "3,518,000 VND" as "3518000").\n'
        "- account_number: the beneficiary account number, digits only.\n"
        "- account_name: the beneficiary name exactly as printed.\n"
        "- bank: the bank holding the RECEIVING account, not the app that "
        "issued the receipt. Use the bank's common name, e.g. "
        '"Vietcombank".\n'
        "- content: the transfer message (Nội dung, Lời nhắn, Diễn giải), "
        "joining a message that wraps over several lines.\n"
        "- Never copy the sender's account, the fee, the transaction id or the "
        "timestamp into any field.\n"
        "- A paper payment order (Ủy nhiệm chi) repeats the same bilingual "
        'labels ("Số tài khoản/ Account No.") for two blocks: the payer under '
        '"Đơn vị trả tiền/ Applicant" and the beneficiary under "Đơn vị thụ '
        'hưởng/ Beneficiary". Read only the beneficiary block; its amount is '
        '"Số tiền bằng số/ Amount in figures" and its message "Nội dung/ '
        'Details".\n'
        '- Use an empty string "" for anything not visible or uncertain.\n'
        "- Output raw JSON only: no markdown fences, no explanation."
    )


# Read a bank-transfer receipt via the cloud VLM. Returns the raw reply; the
# caller parses and normalizes it.
def extract_bank_transfer(
    file_stream: BinaryIO, stream_info: Optional[StreamInfo] = None
) -> Optional[str]:
    return _run_vision(bank_transfer_prompt(), file_stream, stream_info)


# Extract text from an image via the cloud VLM, preserving structure.
def extract_text(
    file_stream: BinaryIO, stream_info: Optional[StreamInfo] = None
) -> Optional[str]:
    return _run_vision(_OCR_PROMPT, file_stream, stream_info)


# Describe an image via the cloud VLM.
def describe(
    file_stream: BinaryIO,
    stream_info: Optional[StreamInfo] = None,
    *,
    language: str = "en",
) -> Optional[str]:
    return _run_vision(description_prompt(language), file_stream, stream_info)
