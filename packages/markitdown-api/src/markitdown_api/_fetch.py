"""
Download the source file when a request names a `url` instead of uploading one.

Fetching on the caller's behalf makes the server an SSRF surface: without checks
anyone able to reach the API could use it to read the host's private network
(`http://169.254.169.254/`, internal admin pages, ...). So every URL — and every
redirect hop — is resolved and refused when it points at a non-public address,
unless the operator opts in via MARKITDOWN_API_ALLOW_PRIVATE_URLS. The download
is also capped at the same limit as an upload.
"""

from __future__ import annotations

import io
import ipaddress
import os
import re
import socket
from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote, urlparse

import requests

from ._responses import ErrorCode, quote_value, sanitize_message

# The link the caller gave could not be downloaded — its file is gone, private, or
# the host is down. That is a failed dependency of the request, not a failure of
# this server, and the distinction is not academic: a 5xx gets its body rewritten
# by proxies in front of this API (Cloudflare replaces it with "error code: 502"),
# so the client loses the `error_code` and the reason, and cannot tell a dead
# image link from a dead server. 424 passes through untouched.
UPSTREAM_FETCH_STATUS = 424

_CONNECT_TIMEOUT_SECONDS = 10
_READ_TIMEOUT_SECONDS = 30
_MAXIMUM_REDIRECTS = 5
_CHUNK_SIZE = 1024 * 1024
_USER_AGENT = "markitdown-api"
_MAXIMUM_URL_LENGTH = 2048
_MAXIMUM_FILENAME_LENGTH = 128
# A URL is pasted into log lines and error messages, and handed to an HTTP
# client; whitespace and control characters have no legitimate place in one.
_FORBIDDEN_URL_CHARACTERS = re.compile(r"[\s\x00-\x1f\x7f]")

# Magic bytes of the image formats the converters accept. Object storage often
# serves images as `application/octet-stream`, so the header alone cannot decide
# whether a link holds an image.
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SIGNATURE_LENGTH = 16


class FetchError(Exception):
    """A URL could not be used, carrying the status and code to answer with."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class FetchedFile:
    stream: io.BytesIO
    url: str
    filename: str
    extension: Optional[str]
    mimetype: Optional[str]


# Report the image extension the leading bytes belong to, or None.
def sniff_image_extension(head: bytes) -> Optional[str]:
    if head.startswith(_JPEG_SIGNATURE):
        return ".jpg"
    if head.startswith(_PNG_SIGNATURE):
        return ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return None


# Refuse hosts that resolve to anything but a routable public address.
def _ensure_public_host(host: str) -> None:
    try:
        addresses = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise FetchError(
            400,
            ErrorCode.INVALID_URL,
            f"Could not resolve the host {quote_value(host)} in the url.",
        ) from exc

    for entry in addresses:
        address = ipaddress.ip_address(entry[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise FetchError(
                400,
                ErrorCode.BLOCKED_URL,
                f"The url resolves to the non-public address {address}, which this "
                "server refuses to fetch. Set MARKITDOWN_API_ALLOW_PRIVATE_URLS=true "
                "to allow private destinations.",
            )


# Validate one URL — the submitted one or a redirect target — and return it
# normalized. Everything rejected here is rejected before any connection is made.
def _validate_url(url: str, *, allow_private_hosts: bool) -> str:
    candidate = url.strip()

    if len(candidate) > _MAXIMUM_URL_LENGTH:
        raise FetchError(
            400,
            ErrorCode.INVALID_URL,
            f"The url is longer than {_MAXIMUM_URL_LENGTH} characters.",
        )
    if _FORBIDDEN_URL_CHARACTERS.search(candidate):
        raise FetchError(
            400,
            ErrorCode.INVALID_URL,
            "The url contains whitespace or control characters.",
        )

    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https"):
        raise FetchError(
            400,
            ErrorCode.INVALID_URL,
            f"Unsupported url scheme {quote_value(parsed.scheme or 'none')}. "
            "Only http:// and https:// are accepted.",
        )
    if not parsed.hostname:
        raise FetchError(400, ErrorCode.INVALID_URL, "The url has no host part.")
    # user:password@host both smuggles credentials through the API and makes the
    # real host hard to read; a public file link never needs it.
    if parsed.username or parsed.password:
        raise FetchError(
            400,
            ErrorCode.INVALID_URL,
            "The url must not embed credentials (user:password@host).",
        )
    if not allow_private_hosts:
        _ensure_public_host(parsed.hostname)
    return parsed.geturl()


# Read the file name from Content-Disposition, else from the URL path. The name
# comes from a remote server, so only its basename is kept and the length is
# capped; :mod:`._storage` strips whatever characters remain unsafe.
def _response_filename(response: requests.Response, url: str) -> str:
    disposition = response.headers.get("content-disposition", "")
    match = re.search(r"filename\*?=([^;]+)", disposition)
    if match:
        name = unquote(match.group(1).strip().strip("\"'").removeprefix("UTF-8''"))
        if name:
            return os.path.basename(name)[:_MAXIMUM_FILENAME_LENGTH]

    path = urlparse(url).path
    name = os.path.basename(unquote(path))[:_MAXIMUM_FILENAME_LENGTH]
    return name or "download"


def _content_length(response: requests.Response) -> Optional[int]:
    raw = response.headers.get("content-length")
    if not raw or not raw.strip().isdigit():
        return None
    return int(raw.strip())


# Follow the redirect chain by hand so each hop goes through _validate_url; the
# `requests` built-in redirect handling would only check the first URL.
def _get_with_checked_redirects(
    url: str, *, allow_private_hosts: bool
) -> requests.Response:
    current = _validate_url(url, allow_private_hosts=allow_private_hosts)

    for _ in range(_MAXIMUM_REDIRECTS + 1):
        response = requests.get(
            current,
            stream=True,
            allow_redirects=False,
            timeout=(_CONNECT_TIMEOUT_SECONDS, _READ_TIMEOUT_SECONDS),
            headers={"User-Agent": _USER_AGENT},
        )

        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("location", "")
            response.close()
            if not location:
                raise FetchError(
                    UPSTREAM_FETCH_STATUS,
                    ErrorCode.UPSTREAM_FETCH_FAILED,
                    f"The url answered HTTP {response.status_code} without a redirect target.",
                )
            current = _validate_url(
                requests.compat.urljoin(current, location),
                allow_private_hosts=allow_private_hosts,
            )
            continue

        if response.status_code >= 400:
            response.close()
            raise FetchError(
                UPSTREAM_FETCH_STATUS,
                ErrorCode.UPSTREAM_FETCH_FAILED,
                f"The url could not be downloaded: the server answered HTTP "
                f"{response.status_code}. Check that the link is public and has not expired.",
            )
        return response

    raise FetchError(
        UPSTREAM_FETCH_STATUS,
        ErrorCode.UPSTREAM_FETCH_FAILED,
        f"The url redirected more than {_MAXIMUM_REDIRECTS} times.",
    )


# Download a URL into memory, or raise FetchError explaining why it was refused.
def fetch(
    url: str, *, max_bytes: int, allow_private_hosts: bool = False
) -> FetchedFile:
    try:
        response = _get_with_checked_redirects(
            url, allow_private_hosts=allow_private_hosts
        )
    except requests.Timeout as exc:
        raise FetchError(
            UPSTREAM_FETCH_STATUS,
            ErrorCode.UPSTREAM_FETCH_FAILED,
            "The url did not respond in time "
            f"({_CONNECT_TIMEOUT_SECONDS}s connect / {_READ_TIMEOUT_SECONDS}s read).",
        ) from exc
    except requests.RequestException as exc:
        raise FetchError(
            UPSTREAM_FETCH_STATUS,
            ErrorCode.UPSTREAM_FETCH_FAILED,
            f"The url could not be reached: {sanitize_message(exc, limit=160)}",
        ) from exc

    limit_message = (
        f"The linked file is larger than the {max_bytes // (1024 * 1024)} MB limit."
    )
    with response:
        declared_length = _content_length(response)
        if declared_length is not None and declared_length > max_bytes:
            raise FetchError(413, ErrorCode.FILE_TOO_LARGE, limit_message)

        buffer = io.BytesIO()
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise FetchError(413, ErrorCode.FILE_TOO_LARGE, limit_message)
                buffer.write(chunk)
        except requests.RequestException as exc:
            raise FetchError(
                UPSTREAM_FETCH_STATUS,
                ErrorCode.UPSTREAM_FETCH_FAILED,
                f"The download was interrupted: {sanitize_message(exc, limit=160)}",
            ) from exc

        if total == 0:
            raise FetchError(
                400, ErrorCode.EMPTY_FILE, "The url returned an empty file."
            )

        mimetype = response.headers.get("content-type", "").split(";")[0].strip()
        filename = _response_filename(response, response.url)

    buffer.seek(0)
    extension = os.path.splitext(filename)[1].lower() or None
    return FetchedFile(
        stream=buffer,
        url=response.url,
        filename=filename,
        extension=extension,
        mimetype=mimetype or None,
    )
