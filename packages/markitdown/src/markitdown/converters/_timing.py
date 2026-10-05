from __future__ import annotations

import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from typing import Dict, Iterator, Optional

logger = logging.getLogger("markitdown.timing")
_active_tracker = threading.local()


# Resolve whether timing diagnostics should be emitted for a conversion step.
def is_debug_timing_enabled(value: Optional[bool] = None) -> bool:
    if value is not None:
        return bool(value)

    return os.getenv("MARKITDOWN_DEBUG_TIMING", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


# Context manager that collects elapsed seconds for all labeled timing steps.
@contextmanager
def timing_context() -> Iterator[Dict[str, float]]:
    timings: Dict[str, float] = {}
    old_tracker = getattr(_active_tracker, "timings", None)
    _active_tracker.timings = timings
    try:
        yield timings
    finally:
        _active_tracker.timings = old_tracker


# Return the active timing dictionary if a timing_context is currently open.
def get_current_timings() -> Optional[Dict[str, float]]:
    return getattr(_active_tracker, "timings", None)


# Measure elapsed time and record to active tracker and logs.
@contextmanager
def log_timing(label: str, enabled: bool) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        active = getattr(_active_tracker, "timings", None)
        if active is not None:
            active[label] = round(elapsed, 4)

        if enabled:
            msg = f"[markitdown timing] {label}: {elapsed:.3f}s"
            logger.debug(msg)
            if not logger.handlers and not logging.getLogger().handlers:
                print(msg, file=sys.stderr)
