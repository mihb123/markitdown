"""
The response envelope every endpoint returns, and the error vocabulary.

Success and failure share one shape — `status`, `message`, `data` — plus an
`error_code` on failures. The code is the part a client should branch on: it is
stable and machine-readable, while `message` is prose meant for a human and may
be reworded at any time.

Error messages routinely quote things the caller controls (a URL, a remote
Content-Type, a file name). :func:`sanitize_message` is what keeps that from
turning into an injection vector — a message never carries control characters,
never spans lines, and never grows unbounded.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from fastapi import HTTPException

STATUS_SUCCESS = "success"
STATUS_ERROR = "error"

_MAXIMUM_MESSAGE_LENGTH = 400
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class ErrorCode:
    """Stable error identifiers. Clients branch on these, not on `message`."""

    # Request shape
    MISSING_SOURCE = "missing_source"
    AMBIGUOUS_SOURCE = "ambiguous_source"
    INVALID_PARAMETER = "invalid_parameter"
    INVALID_REQUEST = "invalid_request"

    # Authentication
    UNAUTHORIZED = "unauthorized"

    # The submitted file or link
    EMPTY_FILE = "empty_file"
    FILE_TOO_LARGE = "file_too_large"
    UNSUPPORTED_FORMAT = "unsupported_format"
    INVALID_URL = "invalid_url"
    BLOCKED_URL = "blocked_url"
    UPSTREAM_FETCH_FAILED = "upstream_fetch_failed"

    # Conversion
    CONVERSION_FAILED = "conversion_failed"
    CLOUD_VLM_FAILED = "cloud_vlm_failed"
    MISSING_DEPENDENCY = "missing_dependency"

    # Results
    FILE_NOT_FOUND = "file_not_found"

    # Capacity: the request was not even attempted, so retrying it is correct.
    SERVER_BUSY = "server_busy"

    INTERNAL_ERROR = "internal_error"


# Fallbacks for HTTPExceptions raised outside our own code (e.g. by FastAPI).
_STATUS_FALLBACK_CODES = {
    401: ErrorCode.UNAUTHORIZED,
    404: ErrorCode.FILE_NOT_FOUND,
    405: ErrorCode.INVALID_REQUEST,
    413: ErrorCode.FILE_TOO_LARGE,
    415: ErrorCode.UNSUPPORTED_FORMAT,
    422: ErrorCode.INVALID_REQUEST,
    424: ErrorCode.UPSTREAM_FETCH_FAILED,
}


# Reduce any text to a single safe line: no control characters, no runaway length.
def sanitize_message(text: object, *, limit: int = _MAXIMUM_MESSAGE_LENGTH) -> str:
    collapsed = " ".join(_CONTROL_CHARACTERS.sub(" ", str(text)).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


# Quote a caller-supplied value inside a message, kept short so a long URL or
# header cannot push the useful part of the message out of view.
def quote_value(value: object, *, limit: int = 120) -> str:
    return f"'{sanitize_message(value, limit=limit)}'"


class ApiError(HTTPException):
    """An HTTP error carrying the machine-readable code sent to the client."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(status_code=status_code, detail=sanitize_message(message))
        self.code = code


def success(data: Any, message: str = "OK") -> Dict[str, Any]:
    return {"status": STATUS_SUCCESS, "message": message, "data": data}


def failure(message: object, code: str, data: Optional[Any] = None) -> Dict[str, Any]:
    return {
        "status": STATUS_ERROR,
        "error_code": code,
        "message": sanitize_message(message),
        "data": data,
    }


# Pick the code for an exception raised outside our own helpers.
def fallback_code(status_code: int) -> str:
    if status_code == 503:
        return ErrorCode.SERVER_BUSY
    if status_code >= 500:
        return ErrorCode.INTERNAL_ERROR
    return _STATUS_FALLBACK_CODES.get(status_code, ErrorCode.INVALID_REQUEST)
