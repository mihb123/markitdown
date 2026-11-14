"""
Server settings, read from the environment or the shared `.env` file.

Every key is resolved through :mod:`markitdown._env`, so the API reads the same
`.env` the CLI already uses for its cloud VLM credentials.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from markitdown import _env

DEFAULT_PORT = 9890
DEFAULT_HOST = "0.0.0.0"
DEFAULT_MAX_UPLOAD_MB = 25
DEFAULT_FILE_TTL_MINUTES = 120
DEFAULT_QUEUE_TIMEOUT_SECONDS = 30
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_MAX_MB = 20
DEFAULT_LOG_BACKUPS = 5
DEFAULT_MEMORY_WARN_MB = 1024
DEFAULT_MEMORY_CHECK_SECONDS = 60
_TRUE_VALUES = ("true", "1", "yes", "on")


# Read an int setting, falling back to the default when unset or unparsable.
# `allow_zero` is what lets a setting be switched off with 0 (memory watchdog,
# log rotation) instead of silently falling back to its default.
def _int_setting(key: str, default: int, *, allow_zero: bool = False) -> int:
    raw = _env.resolve(key)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    if value > 0 or (allow_zero and value == 0):
        return value
    return default


# How many conversions may run at once. Conversions are CPU-heavy (OCR) or hold
# a thread waiting on the cloud VLM, so the default follows the core count: on a
# small box a burst of requests then queues instead of all fighting for the same
# two cores and the same memory.
def _default_concurrency() -> int:
    return max(2, os.cpu_count() or 2)


def _bool_setting(key: str, default: bool = False) -> bool:
    raw = _env.resolve(key)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_VALUES


# Read the accepted API keys. Several may be listed so a key can be rotated
# without a window where clients are locked out.
def _api_keys() -> Tuple[str, ...]:
    raw = (
        _env.resolve("MARKITDOWN_API_KEYS") or _env.resolve("MARKITDOWN_API_KEY") or ""
    )
    return tuple(key.strip() for key in raw.split(",") if key.strip())


@dataclass(frozen=True)
class ApiConfig:
    host: str
    port: int
    max_upload_bytes: int
    file_ttl_seconds: int
    output_dir: Path
    public_base_url: Optional[str]
    enable_plugins: bool
    cors_origins: List[str]
    allow_private_urls: bool
    api_keys: Tuple[str, ...]
    protect_downloads: bool
    # --- load shedding ---
    max_concurrency: int
    queue_timeout_seconds: int
    # --- observability ---
    log_level: str
    log_file: Optional[Path]
    log_max_bytes: int
    log_backup_count: int
    memory_warn_bytes: int
    memory_check_seconds: int


# Build the configuration used by the app and the `markitdown-api` command.
def load_config() -> ApiConfig:
    # --- 1. NETWORK ---
    # PORT is the documented key; MARKITDOWN_API_PORT overrides it when the host
    # already uses PORT for another service.
    port = _int_setting("MARKITDOWN_API_PORT", _int_setting("PORT", DEFAULT_PORT))
    host = (_env.resolve("MARKITDOWN_API_HOST") or DEFAULT_HOST).strip()

    origins = _env.resolve("MARKITDOWN_API_CORS_ORIGINS") or "*"
    cors_origins = [origin.strip() for origin in origins.split(",") if origin.strip()]

    # --- 2. UPLOAD AND OUTPUT STORAGE ---
    output_dir = _env.resolve("MARKITDOWN_API_OUTPUT_DIR")
    directory = (
        Path(os.path.expanduser(output_dir))
        if output_dir
        else Path(tempfile.gettempdir()) / "markitdown-api-files"
    )

    # --- 3. LOGGING ---
    # Logging to a file is opt-in: under systemd or `make start` the process
    # already has its output captured, and a second copy would only rotate on
    # its own schedule.
    log_file = (_env.resolve("MARKITDOWN_API_LOG_FILE") or "").strip()
    log_path = Path(os.path.expanduser(log_file)) if log_file else None

    return ApiConfig(
        host=host,
        port=port,
        max_upload_bytes=_int_setting(
            "MARKITDOWN_API_MAX_UPLOAD_MB", DEFAULT_MAX_UPLOAD_MB
        )
        * 1024
        * 1024,
        file_ttl_seconds=_int_setting(
            "MARKITDOWN_API_FILE_TTL_MINUTES", DEFAULT_FILE_TTL_MINUTES
        )
        * 60,
        output_dir=directory,
        public_base_url=(_env.resolve("MARKITDOWN_API_PUBLIC_URL") or "").rstrip("/")
        or None,
        enable_plugins=_bool_setting("MARKITDOWN_ENABLE_PLUGINS"),
        cors_origins=cors_origins,
        allow_private_urls=_bool_setting("MARKITDOWN_API_ALLOW_PRIVATE_URLS"),
        api_keys=_api_keys(),
        protect_downloads=_bool_setting("MARKITDOWN_API_PROTECT_DOWNLOADS"),
        max_concurrency=_int_setting(
            "MARKITDOWN_API_MAX_CONCURRENCY", _default_concurrency()
        ),
        queue_timeout_seconds=_int_setting(
            "MARKITDOWN_API_QUEUE_TIMEOUT_SECONDS", DEFAULT_QUEUE_TIMEOUT_SECONDS
        ),
        log_level=(
            _env.resolve("MARKITDOWN_API_LOG_LEVEL") or DEFAULT_LOG_LEVEL
        ).strip().upper(),
        log_file=log_path,
        log_max_bytes=_int_setting("MARKITDOWN_API_LOG_MAX_MB", DEFAULT_LOG_MAX_MB)
        * 1024
        * 1024,
        log_backup_count=_int_setting(
            "MARKITDOWN_API_LOG_BACKUPS", DEFAULT_LOG_BACKUPS, allow_zero=True
        ),
        memory_warn_bytes=_int_setting(
            "MARKITDOWN_API_MEMORY_WARN_MB", DEFAULT_MEMORY_WARN_MB, allow_zero=True
        )
        * 1024
        * 1024,
        memory_check_seconds=_int_setting(
            "MARKITDOWN_API_MEMORY_CHECK_SECONDS",
            DEFAULT_MEMORY_CHECK_SECONDS,
            allow_zero=True,
        ),
    )
