"""
What the server records about itself: logs, per-request traces, crash dumps and
memory pressure.

The default uvicorn setup logs one unstamped line per request and nothing else,
which is exactly the state an operator finds after the process is gone: a log
that cannot say when anything happened, why a request failed, or how the process
ended. Everything here exists to answer those three questions.

* :func:`configure_logging` puts a timestamp and a level on every line — the
  application's, uvicorn's and the access log's — and optionally mirrors them to
  a rotating file.
* :class:`RequestLogMiddleware` logs the outcome of each request (id, status,
  duration, client) and stamps ``X-Request-ID`` on the reply, so a client-side
  error and a server-side line can be matched up afterwards.
* :func:`install_crash_diagnostics` arms :mod:`faulthandler`, which is the only
  thing that leaves a trace when a native extension (ONNX Runtime, OpenCV)
  segfaults — the failure mode that otherwise ends the process with an empty
  log. ``SIGUSR1`` dumps the stack of every thread on demand, for a server that
  is stuck rather than dead.
* :func:`start_memory_watchdog` reports resident memory, so an approaching
  out-of-memory kill leaves a warning behind: the kernel's SIGKILL cannot be
  logged from inside the process it kills.

A process that is killed outright still logs nothing here, by construction —
that case is what the systemd unit in ``deploy/`` is for, since journald records
the exit signal from the outside.
"""

from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from ._config import ApiConfig

LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S%z"
REQUEST_ID_HEADER = "X-Request-ID"

logger = logging.getLogger("markitdown_api")
_access_logger = logging.getLogger("markitdown_api.access")

# Guards against re-configuring when create_app() is called more than once (the
# test suite builds several apps in one process).
_configure_lock = threading.Lock()
_configured = False
_crash_file = None  # kept open for the process lifetime: faulthandler writes to the fd
_watchdog: Optional[threading.Thread] = None


# Resident set size of this process in bytes, or 0 where /proc is unavailable.
def current_rss_bytes() -> int:
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as handle:
            pages = int(handle.read().split()[1])
    except (OSError, IndexError, ValueError):
        return 0
    return pages * os.sysconf("SC_PAGE_SIZE")


def _megabytes(value: int) -> int:
    return value // (1024 * 1024)


class _StderrHandler(logging.StreamHandler):
    """Writes to whatever ``sys.stderr`` is *now*.

    ``logging.StreamHandler()`` captures the stream object at construction. That
    breaks whenever stderr is replaced afterwards — under pytest's capture, or a
    process that reopens its output — and every later log line raises
    "I/O operation on closed file" instead of being written.
    """

    def __init__(self) -> None:
        super().__init__()

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, value) -> None:  # set by StreamHandler.__init__
        pass


# Attach the handlers every logger in the process writes through: stderr always,
# plus a rotating file when one is configured.
def configure_logging(config: ApiConfig) -> None:
    global _configured

    with _configure_lock:
        if _configured:
            return

        formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
        handlers: list[logging.Handler] = [_StderrHandler()]

        if config.log_file is not None:
            try:
                config.log_file.parent.mkdir(parents=True, exist_ok=True)
                handlers.append(
                    logging.handlers.RotatingFileHandler(
                        config.log_file,
                        maxBytes=config.log_max_bytes,
                        backupCount=config.log_backup_count,
                        encoding="utf-8",
                    )
                )
            except OSError as exc:
                # A log file that cannot be opened must not stop the server.
                print(
                    f"WARNING: cannot write to {config.log_file}: {exc}",
                    file=sys.stderr,
                )

        level = getattr(logging, config.log_level, logging.INFO)
        root = logging.getLogger()
        root.setLevel(level)
        for handler in handlers:
            handler.setFormatter(formatter)
            root.addHandler(handler)

        # uvicorn installs its own unstamped handlers; drop them so its lines go
        # through the ones above instead of being printed twice in two formats.
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            uvicorn_logger = logging.getLogger(name)
            uvicorn_logger.handlers.clear()
            uvicorn_logger.propagate = True
            uvicorn_logger.setLevel(level)

        # This server logs its own access lines, with timing and a request id.
        logging.getLogger("uvicorn.access").disabled = True

        _configured = True


# Arm the handlers that turn a silent death into a logged one.
def install_crash_diagnostics(config: ApiConfig) -> None:
    global _crash_file

    if _crash_file is not None:
        return

    target = sys.stderr
    if config.log_file is not None:
        try:
            config.log_file.parent.mkdir(parents=True, exist_ok=True)
            # Line buffered and append-only: a crash dump must survive the
            # process that wrote it, and must not clobber the rotating log.
            _crash_file = open(
                config.log_file.with_suffix(config.log_file.suffix + ".crash"),
                "a",
                buffering=1,
                encoding="utf-8",
            )
            target = _crash_file
        except OSError:
            target = sys.stderr

    try:
        faulthandler.enable(file=target, all_threads=True)
        # `kill -USR1 <pid>` prints every thread's stack: the answer to "the
        # server answers /health but this request has been running for minutes".
        if hasattr(signal, "SIGUSR1"):
            faulthandler.register(signal.SIGUSR1, file=target, all_threads=True)
    except (RuntimeError, ValueError, AttributeError):
        # Non-standard stderr (pytest capture) or a platform without the signal.
        pass

    # An exception escaping a worker thread is otherwise printed raw to stderr,
    # with no timestamp and no context.
    def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
        logger.error(
            "Unhandled exception in thread %s",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _thread_excepthook


# Log resident memory in the background, loudly once it crosses the warning
# mark. An OOM kill leaves nothing behind, so the warning before it is the only
# record that the process was growing.
def start_memory_watchdog(config: ApiConfig) -> None:
    global _watchdog

    if _watchdog is not None or config.memory_check_seconds <= 0:
        return
    if current_rss_bytes() == 0:
        return

    def _run() -> None:
        warned = False
        while True:
            time.sleep(config.memory_check_seconds)
            rss = current_rss_bytes()
            if config.memory_warn_bytes and rss >= config.memory_warn_bytes:
                logger.warning(
                    "Memory high: rss=%dMB (warn at %dMB). The kernel kills the "
                    "process without a log line if this keeps growing.",
                    _megabytes(rss),
                    _megabytes(config.memory_warn_bytes),
                )
                warned = True
            elif warned:
                logger.info("Memory back to normal: rss=%dMB", _megabytes(rss))
                warned = False
            else:
                logger.debug("Memory: rss=%dMB", _megabytes(rss))

    _watchdog = threading.Thread(
        target=_run, name="markitdown-memory-watchdog", daemon=True
    )
    _watchdog.start()


# One line per request, after the response is known: what was asked, what came
# back, how long it took, and how loaded the server was while doing it.
class RequestLogMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, in_flight_source=None):
        super().__init__(app)
        self._in_flight_source = in_flight_source

    async def dispatch(self, request: Request, call_next):
        request_id = uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        started = time.monotonic()

        try:
            response = await call_next(request)
        except Exception:
            # The exception handlers turn this into a 500 for the client; here it
            # is recorded with the request it belongs to.
            logger.exception(
                "request=%s %s %s failed after %dms",
                request_id,
                request.method,
                request.url.path,
                int((time.monotonic() - started) * 1000),
            )
            raise

        duration_ms = int((time.monotonic() - started) * 1000)
        response.headers[REQUEST_ID_HEADER] = request_id

        status = response.status_code
        level = logging.INFO
        if status >= 500:
            level = logging.ERROR
        elif status >= 400:
            level = logging.WARNING

        in_flight = ""
        if self._in_flight_source is not None:
            in_flight = f" in_flight={self._in_flight_source()}"

        _access_logger.log(
            level,
            "request=%s %s %s -> %d %dms client=%s%s",
            request_id,
            request.method,
            request.url.path,
            status,
            duration_ms,
            _client_address(request),
            in_flight,
        )
        return response


# Prefer the address a reverse proxy reports, since every request otherwise
# comes from the tunnel or proxy on localhost.
def _client_address(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64] or "-"
    client = request.client
    return client.host if client else "-"


# Summarize the settings that decide how the server behaves under load, so the
# log says what it was running with rather than what the file said at some point.
def log_startup_summary(config: ApiConfig, extra: Optional[dict] = None) -> None:
    logger.info(
        "Config: concurrency=%d queue_timeout=%ds max_upload=%dMB "
        "log_level=%s log_file=%s memory_warn=%dMB",
        config.max_concurrency,
        config.queue_timeout_seconds,
        _megabytes(config.max_upload_bytes),
        config.log_level,
        config.log_file or "-",
        _megabytes(config.memory_warn_bytes),
    )
    for key, value in (extra or {}).items():
        logger.info("Config: %s=%s", key, value)


# Resolve the path a crash dump would be written to, for the startup banner.
def crash_dump_path(config: ApiConfig) -> Optional[Path]:
    if config.log_file is None:
        return None
    return config.log_file.with_suffix(config.log_file.suffix + ".crash")
