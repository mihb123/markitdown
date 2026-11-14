"""
Load shedding: how many conversions may run at once, and what a caller is told
when the server is full.

Every conversion either burns CPU (offline OCR) or holds a worker thread for the
length of a cloud VLM call — tens of seconds either way. Without a bound, a
queue of a hundred images turns into a hundred concurrent conversions, each with
its own decoded image in memory, and the process is killed by the kernel long
before the last one finishes. Nothing about that failure is visible in a log:
the server simply disappears, and every request after it fails at the proxy.

So the server admits `max_concurrency` conversions and makes the rest wait.
A caller that waits longer than `queue_timeout_seconds` gets a `503
server_busy` with `Retry-After` — an answer it can act on, and one that keeps
the queue from growing without limit. Clients that process a batch (a worker
draining a message queue) should treat it as "come back shortly", not as a
failed image.

Light endpoints — `/health`, the docs, download links — are never queued, so a
monitoring check still answers while the server is saturated.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, Sequence

import anyio
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from ._responses import ErrorCode, failure

logger = logging.getLogger("markitdown_api")

# The endpoints that actually convert something. Everything else is cheap.
HEAVY_PATH_PREFIXES = ("/v1/bank", "/v1/text", "/v1/describe", "/v1/documents")


class ConcurrencyLimiter:
    """Counts what is running, waiting and rejected; the numbers /health shows."""

    def __init__(self, limit: int, queue_timeout_seconds: int):
        self.limit = max(1, limit)
        self.queue_timeout_seconds = max(1, queue_timeout_seconds)
        self._semaphore = anyio.Semaphore(self.limit)
        self._lock = threading.Lock()
        self._in_flight = 0
        self._waiting = 0
        self._peak_in_flight = 0
        self._rejected = 0

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def stats(self) -> Dict[str, int]:
        return {
            "max_concurrency": self.limit,
            "in_flight": self._in_flight,
            "waiting": self._waiting,
            "peak_in_flight": self._peak_in_flight,
            "rejected_busy": self._rejected,
        }

    def _enter(self) -> None:
        with self._lock:
            self._in_flight += 1
            self._waiting -= 1
            self._peak_in_flight = max(self._peak_in_flight, self._in_flight)

    def _leave(self) -> None:
        with self._lock:
            self._in_flight -= 1

    def _queued(self) -> None:
        with self._lock:
            self._waiting += 1

    def _dequeued_busy(self) -> None:
        with self._lock:
            self._waiting -= 1
            self._rejected += 1


class ConcurrencyLimitMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        limiter: ConcurrencyLimiter,
        heavy_prefixes: Sequence[str] = HEAVY_PATH_PREFIXES,
    ):
        super().__init__(app)
        self._limiter = limiter
        self._heavy_prefixes = tuple(heavy_prefixes)

    def _is_heavy(self, request: Request) -> bool:
        if request.method not in ("POST", "PUT"):
            return False
        path = request.url.path
        return any(path.startswith(prefix) for prefix in self._heavy_prefixes)

    async def dispatch(self, request: Request, call_next):
        if not self._is_heavy(request):
            return await call_next(request)

        limiter = self._limiter
        limiter._queued()
        try:
            with anyio.fail_after(limiter.queue_timeout_seconds):
                await limiter._semaphore.acquire()
        except TimeoutError:
            limiter._dequeued_busy()
            logger.warning(
                "Busy: refused %s %s after waiting %ds (limit %d, in flight %d)",
                request.method,
                request.url.path,
                limiter.queue_timeout_seconds,
                limiter.limit,
                limiter.in_flight,
            )
            return JSONResponse(
                status_code=503,
                content=failure(
                    "The server is at capacity: too many conversions are already "
                    "running. Retry in a few seconds.",
                    ErrorCode.SERVER_BUSY,
                ),
                headers={"Retry-After": str(limiter.queue_timeout_seconds)},
            )

        limiter._enter()
        try:
            return await call_next(request)
        finally:
            limiter._leave()
            limiter._semaphore.release()
