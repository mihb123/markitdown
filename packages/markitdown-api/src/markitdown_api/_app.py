"""
The MarkItDown HTTP API: one endpoint per CLI feature.

    POST /v1/bank        image bank-transfer receipt  (CLI: --bank)  -> JSON fields
                         local-only; never calls the cloud VLM
    POST /v1/text        image text extraction        (CLI: --text)  -> download link
    POST /v1/describe    image description/caption    (CLI: --des)   -> download link
    POST /v1/documents   pdf/docx/xlsx/pptx/...       (CLI: no flag) -> download link
    GET  /v1/files/{id}  download a generated file

Endpoints that mirror a CLI flag printing JSON return that same JSON under
`data`; the ones whose CLI equivalent prints plain text store the output through
:class:`OutputStore` and return a link to it, so a client never has to handle a
multi-megabyte response body.

Handlers are deliberately synchronous: conversion is blocking (OCR models, PDF
parsing, cloud calls), and Starlette runs sync handlers in its worker threadpool
instead of stalling the event loop. How many may run at once is bounded — see
:mod:`._limits` — so a batch of queued images degrades into waiting rather than
into an out-of-memory kill.
"""

from __future__ import annotations

import io
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Dict, Optional, Tuple

import anyio
from fastapi import FastAPI, File, Form, HTTPException, Request, Security, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from markitdown import (
    FileConversionException,
    MarkItDown,
    MarkItDownException,
    MissingDependencyException,
    StreamInfo,
    UnsupportedFormatException,
)
from markitdown.converters._bank_fields import empty_info
from markitdown.converters._image_converter import (
    ACCEPTED_FILE_EXTENSIONS,
    ACCEPTED_MIME_TYPE_PREFIXES,
)
from markitdown.converters._local_ocr import is_available as local_ocr_available
from markitdown.converters._timing import (
    is_debug_timing_enabled,
    timing_context,
)
from markitdown.converters._vlm_client import (
    VisionApiError,
    is_configured as vlm_configured,
)

from .__about__ import __version__
from ._auth import API_KEY_HEADER, ApiKeyMiddleware, api_key_scheme
from ._config import ApiConfig, load_config
from ._fetch import FetchError, fetch, sniff_image_extension
from ._limits import ConcurrencyLimiter, ConcurrencyLimitMiddleware
from ._observability import (
    RequestLogMiddleware,
    configure_logging,
    crash_dump_path,
    current_rss_bytes,
    install_crash_diagnostics,
    log_startup_summary,
    start_memory_watchdog,
)
from ._responses import (
    ApiError,
    ErrorCode,
    fallback_code,
    failure,
    quote_value,
    success,
)
from ._storage import OutputStore, StoredFile

DESCRIPTION_LANGUAGES = ("en", "vi")
_UPLOAD_CHUNK_SIZE = 1024 * 1024

logger = logging.getLogger("markitdown_api")


# Buffer the upload in memory, refusing anything above the configured limit.
def _read_upload(upload: UploadFile, max_bytes: int) -> io.BytesIO:
    buffer = io.BytesIO()
    total = 0
    while True:
        chunk = upload.file.read(_UPLOAD_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ApiError(
                413,
                ErrorCode.FILE_TOO_LARGE,
                f"The uploaded file is larger than the {max_bytes // (1024 * 1024)} MB limit.",
            )
        buffer.write(chunk)

    if total == 0:
        raise ApiError(400, ErrorCode.EMPTY_FILE, "The uploaded file is empty.")

    buffer.seek(0)
    return buffer


# Pass the client's file name and content type through as conversion hints; the
# converters still sniff the stream, so a wrong hint is only a hint.
def _stream_info(upload: UploadFile) -> StreamInfo:
    filename = upload.filename or None
    extension = Path(filename).suffix.lower() if filename else None
    mimetype = (upload.content_type or "").strip() or None
    return StreamInfo(
        extension=extension or None,
        mimetype=mimetype,
        filename=filename,
    )


# Confirm the source really is an image before an image-only endpoint converts
# it, reusing the format list ImageConverter itself accepts. A file name or
# Content-Type saying "image" is enough; when both are uninformative — as with a
# CDN link served as application/octet-stream — the magic bytes decide, and the
# format they reveal is returned as a corrected hint.
def _ensure_image(stream: io.BytesIO, info: StreamInfo) -> StreamInfo:
    if (info.extension or "").lower() in ACCEPTED_FILE_EXTENSIONS:
        return info

    mimetype = (info.mimetype or "").lower()
    if any(mimetype.startswith(prefix) for prefix in ACCEPTED_MIME_TYPE_PREFIXES):
        return info

    sniffed = sniff_image_extension(stream.getvalue()[:16])
    if sniffed is not None:
        return info.copy_and_update(extension=sniffed)

    # Name what actually arrived: "the link returns plain text" is the difference
    # between a client retrying blindly and fixing the link it sent.
    observed = []
    if info.mimetype:
        observed.append(f"content type {quote_value(info.mimetype, limit=80)}")
    if info.filename:
        observed.append(f"file name {quote_value(info.filename, limit=80)}")
    description = ", ".join(observed) or "no usable type information"

    raise ApiError(
        415,
        ErrorCode.UNSUPPORTED_FORMAT,
        f"This endpoint only accepts images ({', '.join(ACCEPTED_FILE_EXTENSIONS)}), "
        f"but the source is not one: {description}, and its content does not start "
        "with JPEG, PNG or WebP data.",
    )


# Turn the request's source — an uploaded file or a URL — into a stream, the
# conversion hints, and the name the generated output file is derived from.
def _load_source(
    upload: Optional[UploadFile],
    url: Optional[str],
    *,
    config: ApiConfig,
    require_image: bool,
) -> Tuple[io.BytesIO, StreamInfo, str]:
    # A client that leaves the file field empty still sends an unnamed part.
    if upload is not None and not (upload.filename or "").strip():
        upload = None
    url = (url or "").strip() or None

    if upload is None and url is None:
        raise ApiError(
            400,
            ErrorCode.MISSING_SOURCE,
            "No source given: send a file upload in the 'file' field or a link in the 'url' field.",
        )
    if upload is not None and url is not None:
        raise ApiError(
            400,
            ErrorCode.AMBIGUOUS_SOURCE,
            "Both 'file' and 'url' were given; send exactly one of the two.",
        )

    if upload is not None:
        stream = _read_upload(upload, config.max_upload_bytes)
        info = _stream_info(upload)
        name = upload.filename or "output"
    else:
        try:
            fetched = fetch(
                url,
                max_bytes=config.max_upload_bytes,
                allow_private_hosts=config.allow_private_urls,
            )
        except FetchError as exc:
            raise ApiError(exc.status_code, exc.code, exc.message) from exc

        stream = fetched.stream
        info = StreamInfo(
            extension=fetched.extension,
            mimetype=fetched.mimetype,
            filename=fetched.filename,
            url=fetched.url,
        )
        name = fetched.filename

    if require_image:
        info = _ensure_image(stream, info)
    return stream, info, name


# Return the cloud VLM error a failed conversion attempt was caused by, if any.
def _vision_error(exc: FileConversionException) -> Optional[BaseException]:
    for attempt in exc.attempts or []:
        if attempt.exc_info and isinstance(attempt.exc_info[1], VisionApiError):
            return attempt.exc_info[1]
    return None


# Translate a conversion failure into the matching HTTP status.
def _convert(
    markitdown: MarkItDown, stream: BinaryIO, info: StreamInfo, **kwargs: Any
) -> str:
    try:
        result = markitdown.convert_stream(stream, stream_info=info, **kwargs)
    except MissingDependencyException as exc:
        raise ApiError(501, ErrorCode.MISSING_DEPENDENCY, str(exc)) from exc
    except UnsupportedFormatException as exc:
        raise ApiError(415, ErrorCode.UNSUPPORTED_FORMAT, str(exc)) from exc
    except VisionApiError as exc:
        raise ApiError(502, ErrorCode.CLOUD_VLM_FAILED, str(exc)) from exc
    except FileConversionException as exc:
        # A failing cloud VLM is an upstream problem, not a bad upload, so it
        # keeps its own status instead of being reported as an invalid file.
        vision_error = _vision_error(exc)
        if vision_error is not None:
            raise ApiError(502, ErrorCode.CLOUD_VLM_FAILED, str(vision_error)) from exc
        raise ApiError(422, ErrorCode.CONVERSION_FAILED, str(exc)) from exc
    except MarkItDownException as exc:
        raise ApiError(422, ErrorCode.CONVERSION_FAILED, str(exc)) from exc
    return result.markdown


def _iso_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def create_app(config: Optional[ApiConfig] = None) -> FastAPI:
    config = config or load_config()
    configure_logging(config)
    install_crash_diagnostics(config)

    store = OutputStore(config.output_dir, config.file_ttl_seconds)
    markitdown = MarkItDown(enable_plugins=config.enable_plugins)
    limiter = ConcurrencyLimiter(config.max_concurrency, config.queue_timeout_seconds)
    started_at = time.monotonic()

    # Everything that must be true while the server is serving, and only then:
    # the threadpool bound, the recorded configuration, the memory watchdog.
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Starlette hands sync handlers to a threadpool that defaults to 40
        # threads. Leaving it there would let 40 conversions run at once if the
        # limiter were ever bypassed; keep the pool just above what is admitted.
        try:
            anyio.to_thread.current_default_thread_limiter().total_tokens = (
                limiter.limit + 4
            )
        except (RuntimeError, ValueError):  # pragma: no cover - defensive
            pass

        log_startup_summary(
            config,
            extra={
                "cloud_vlm": vlm_configured(),
                "local_ocr": local_ocr_available(),
                "crash_dump": crash_dump_path(config) or "stderr",
            },
        )
        start_memory_watchdog(config)
        yield
        stats = limiter.stats()
        logger.info(
            "Shutting down: peak_in_flight=%d rejected_busy=%d rss=%dMB",
            stats["peak_in_flight"],
            stats["rejected_busy"],
            current_rss_bytes() // (1024 * 1024),
        )

    app = FastAPI(
        lifespan=lifespan,
        title="MarkItDown API",
        version=__version__,
        description="Convert images and documents to text, markdown or structured JSON.",
        # An app-wide dependency, so the key entered once in /docs is sent with
        # every request instead of being asked for per endpoint. persistAuthorization
        # keeps it across page reloads.
        dependencies=[Security(api_key_scheme)],
        swagger_ui_parameters={"persistAuthorization": True},
    )
    # Middleware runs outermost-last. The order below is deliberate:
    #   request log   — outermost, so even a rejected request is recorded
    #   API key       — an unauthenticated request must not occupy a slot
    #   concurrency   — the gate in front of the conversion itself
    #   CORS          — innermost of the four, closest to the handler
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["*", API_KEY_HEADER],
    )
    app.add_middleware(ConcurrencyLimitMiddleware, limiter=limiter)
    app.add_middleware(
        ApiKeyMiddleware,
        api_keys=config.api_keys,
        protect_downloads=config.protect_downloads,
    )
    app.add_middleware(RequestLogMiddleware, in_flight_source=lambda: limiter.in_flight)

    # Build the payload describing a stored result: the download link is what the
    # client is expected to use, the rest is metadata about it.
    def file_payload(
        request: Request, stored: StoredFile, characters: int
    ) -> Dict[str, Any]:
        base_url = config.public_base_url or str(request.base_url).rstrip("/")
        return {
            "file_id": stored.file_id,
            "file_name": stored.download_name,
            "download_url": f"{base_url}/v1/files/{stored.file_id}",
            "size_bytes": stored.size_bytes,
            "characters": characters,
            "expires_at": _iso_utc(stored.expires_at),
        }

    # Run a conversion whose output is plain text, then store it behind a link.
    def text_result(
        request: Request,
        source: Tuple[io.BytesIO, StreamInfo, str],
        suffix: str,
        message: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        stream, info, name = source
        markdown = _convert(markitdown, stream, info, **kwargs)
        stored = store.save(markdown, source_name=name, suffix=suffix)
        return success(file_payload(request, stored, len(markdown)), message)

    @app.get("/health")
    def health() -> Dict[str, Any]:
        # Beyond "is it up": the load and memory figures are what tell an
        # operator whether the server is saturated or on its way to being
        # killed, without shelling into the box.
        data: Dict[str, Any] = {
            "version": __version__,
            "cloud_vlm_configured": vlm_configured(),
            "local_ocr_available": local_ocr_available(),
            "plugins_enabled": config.enable_plugins,
            "auth_required": bool(config.api_keys),
            "uptime_seconds": int(time.monotonic() - started_at),
        }
        data.update(limiter.stats())
        rss = current_rss_bytes()
        if rss:
            data["rss_mb"] = rss // (1024 * 1024)
        return success(data, "Service is healthy.")

    @app.post("/v1/bank")
    def read_bank_receipt(
        request: Request,
        file: Optional[UploadFile] = File(
            None, description="Receipt image: jpg, png or webp."
        ),
        url: Optional[str] = Form(
            None, description="Public http(s) link to the receipt image."
        ),
        debug: bool = Form(
            False,
            description="Include execution timing breakdown and diagnostics.",
        ),
    ) -> Dict[str, Any]:
        """Extract beneficiary fields and confident transaction time (CLI: --bank).

        Local-only: this endpoint never calls the cloud VLM. See
        :mod:`markitdown.converters._bank_transfer` for why.
        """
        request_id = getattr(request.state, "request_id", "local")
        t_start = time.perf_counter()

        with timing_context() as timings:
            t0 = time.perf_counter()
            stream, info, _name = _load_source(
                file, url, config=config, require_image=True
            )
            timings["source_load"] = round(time.perf_counter() - t0, 4)

            enable_timing = debug or is_debug_timing_enabled()
            markdown = _convert(
                markitdown,
                stream,
                info,
                image_bank=True,
                debug_timing=enable_timing,
            )

            # The converter emits the same JSON document the CLI prints; a reply that
            # is not parsable means nothing was recognized.
            try:
                fields = json.loads(markdown)
            except ValueError:
                fields = empty_info()

            recognized = [field for field, value in fields.items() if value]
            message = (
                "Receipt fields extracted."
                if recognized
                else "No receipt fields could be recognized."
            )
            total_elapsed = round(time.perf_counter() - t_start, 4)
            timings["total"] = total_elapsed

            logger.debug(
                "request=%s /v1/bank completed in %.3fs (source_load=%.3fs, timings=%s)",
                request_id,
                total_elapsed,
                timings.get("source_load", 0),
                json.dumps(timings),
            )

            res = success(fields, message)
            if debug:
                res["debug"] = {
                    "request_id": request_id,
                    "timings": timings,
                    "local_ocr_available": local_ocr_available(),
                }
            return res

    @app.post("/v1/text")
    def extract_image_text(
        request: Request,
        file: Optional[UploadFile] = File(None, description="Image: jpg, png or webp."),
        url: Optional[str] = Form(
            None, description="Public http(s) link to the image."
        ),
        cloud: bool = Form(
            False, description="Use the cloud VLM instead of local OCR."
        ),
    ) -> Dict[str, Any]:
        """Extract the text of an image and return a link to the result (CLI: --text)."""
        return text_result(
            request,
            _load_source(file, url, config=config, require_image=True),
            ".txt",
            "Text extracted.",
            image_ocr=True,
            image_cloud=cloud,
        )

    @app.post("/v1/describe")
    def describe_image(
        request: Request,
        file: Optional[UploadFile] = File(None, description="Image: jpg, png or webp."),
        url: Optional[str] = Form(
            None, description="Public http(s) link to the image."
        ),
        language: str = Form("en", description="Description language: en or vi."),
    ) -> Dict[str, Any]:
        """Describe an image and return a link to the result (CLI: --des)."""
        if language not in DESCRIPTION_LANGUAGES:
            raise ApiError(
                400,
                ErrorCode.INVALID_PARAMETER,
                f"Unsupported language {quote_value(language, limit=40)}. "
                f"Use one of: {', '.join(DESCRIPTION_LANGUAGES)}.",
            )

        return text_result(
            request,
            _load_source(file, url, config=config, require_image=True),
            ".txt",
            "Image described.",
            image_description=True,
            description_language=language,
            image_description_cloud=True,
        )

    @app.post("/v1/documents")
    def convert_document(
        request: Request,
        file: Optional[UploadFile] = File(
            None, description="pdf, docx, xlsx, pptx, csv, html, epub, zip, ..."
        ),
        url: Optional[str] = Form(
            None, description="Public http(s) link to the document."
        ),
        keep_data_uris: bool = Form(
            False, description="Keep base64 data URIs instead of truncating them."
        ),
    ) -> Dict[str, Any]:
        """Convert a document to markdown and return a link to the result."""
        return text_result(
            request,
            _load_source(file, url, config=config, require_image=False),
            ".md",
            "Document converted.",
            keep_data_uris=keep_data_uris,
        )

    @app.get("/v1/files/{file_id}", name="download_file")
    def download_file(file_id: str) -> FileResponse:
        """Download a generated result file. Links expire after the configured TTL."""
        stored = store.resolve(file_id)
        if stored is None:
            raise ApiError(
                404,
                ErrorCode.FILE_NOT_FOUND,
                "No such result file: the id is unknown or the link has expired.",
            )

        # The media type is decided here, never taken from the converted content,
        # and the file is always sent as an attachment — a converted document can
        # contain anything, so it must never be rendered in the browser.
        media_type = "text/markdown" if stored.path.suffix == ".md" else "text/plain"
        return FileResponse(
            stored.path,
            media_type=f"{media_type}; charset=utf-8",
            filename=stored.download_name,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )

    # Keep failures in the same envelope as successes so clients parse one shape.
    @app.exception_handler(HTTPException)
    def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        code = getattr(exc, "code", None) or fallback_code(exc.status_code)
        # The client is told what went wrong; until now the server kept no
        # record of it, which left "the API returns 502" with nothing to read.
        logger.log(
            logging.ERROR if exc.status_code >= 500 else logging.WARNING,
            "request=%s %s -> %d %s: %s",
            getattr(request.state, "request_id", "-"),
            request.url.path,
            exc.status_code,
            code,
            exc.detail,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=failure(exc.detail, code),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields = ", ".join(
            quote_value(error["loc"][-1], limit=40)
            for error in exc.errors()
            if error.get("loc")
        )
        return JSONResponse(
            status_code=422,
            content=failure(
                f"The request could not be parsed. Check these fields: {fields or 'body'}.",
                ErrorCode.INVALID_REQUEST,
            ),
        )

    # Never leak an internal traceback or message to the caller: the details go
    # to the server log, the client gets the code to report.
    @app.exception_handler(Exception)
    def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "request=%s Unhandled error while serving %s",
            getattr(request.state, "request_id", "-"),
            request.url.path,
        )
        return JSONResponse(
            status_code=500,
            content=failure(
                "The server hit an unexpected error. Check the server log for details.",
                ErrorCode.INTERNAL_ERROR,
            ),
        )

    return app
