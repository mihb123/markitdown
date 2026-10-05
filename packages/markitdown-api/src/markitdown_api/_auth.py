"""
API-key authentication middleware.

Every `/v1/*` request must present a key from MARKITDOWN_API_KEYS, sent either as
`X-API-Key: <key>` or `Authorization: Bearer <key>`. Keys are compared in
constant time so a wrong key leaks nothing about how much of it was right.

Two paths are deliberately open:

* `/health`, `/docs`, `/redoc`, `/openapi.json` — monitoring and documentation;
* `GET /v1/files/{id}` — the download links, whose 128-bit random id is itself
  the credential, so a link stays usable in a browser or an <img> tag. Operators
  who would rather have the key required there too can set
  MARKITDOWN_API_PROTECT_DOWNLOADS=true.

With no keys configured the middleware lets everything through, which keeps
local development frictionless; the server prints a warning at startup so this
never happens unnoticed in production.
"""

from __future__ import annotations

import secrets
from typing import Sequence, Tuple

from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from ._responses import ErrorCode, failure

API_KEY_HEADER = "X-API-Key"
_BEARER_PREFIX = "bearer "
_PUBLIC_PATHS = frozenset(
    ("/health", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect")
)
_DOWNLOAD_PREFIX = "/v1/files/"
_MAXIMUM_KEY_LENGTH = 512

# Declared for the OpenAPI schema only — it gives /docs and /redoc a single
# "Authorize" box whose key is then sent with every request. Enforcement stays in
# the middleware, so `auto_error=False` keeps this from rejecting anything itself.
api_key_scheme = APIKeyHeader(
    name=API_KEY_HEADER,
    scheme_name="ApiKeyHeader",
    auto_error=False,
    description=(
        f"An API key from MARKITDOWN_API_KEYS, sent as '{API_KEY_HEADER}: <key>'. "
        "'Authorization: Bearer <key>' works too."
    ),
)


# Read the key from either supported header, ignoring an oversized value so a
# huge header cannot be used to make the comparison expensive.
def _presented_key(request: Request) -> str:
    key = request.headers.get(API_KEY_HEADER, "")
    if not key:
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith(_BEARER_PREFIX):
            key = authorization[len(_BEARER_PREFIX) :]

    key = key.strip()
    return "" if len(key) > _MAXIMUM_KEY_LENGTH else key


def _is_valid(key: str, allowed: Sequence[str]) -> bool:
    # Compare against every key even after a match, so timing does not reveal
    # which key was used or how many are configured.
    valid = False
    for candidate in allowed:
        if secrets.compare_digest(key, candidate):
            valid = True
    return valid


class ApiKeyMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        api_keys: Tuple[str, ...],
        protect_downloads: bool,
    ):
        super().__init__(app)
        self._api_keys = api_keys
        self._protect_downloads = protect_downloads

    def _requires_key(self, request: Request) -> bool:
        if not self._api_keys:
            return False

        path = request.url.path.rstrip("/") or "/"
        if path in _PUBLIC_PATHS:
            return False
        if (
            not self._protect_downloads
            and request.method == "GET"
            and request.url.path.startswith(_DOWNLOAD_PREFIX)
        ):
            return False
        return True

    async def dispatch(self, request: Request, call_next):
        if self._requires_key(request) and not _is_valid(
            _presented_key(request), self._api_keys
        ):
            return JSONResponse(
                status_code=401,
                content=failure(
                    f"Missing or invalid API key. Send it as '{API_KEY_HEADER}: <key>' "
                    "or 'Authorization: Bearer <key>'.",
                    ErrorCode.UNAUTHORIZED,
                ),
                headers={"WWW-Authenticate": API_KEY_HEADER},
            )

        response = await call_next(request)
        # Generated files are served from this origin; stop a browser from
        # sniffing one into an executable type.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response
