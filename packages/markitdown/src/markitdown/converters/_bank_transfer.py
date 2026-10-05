"""
Read the beneficiary details off a bank-transfer receipt image (CLI: ``--bank``).

Extraction runs entirely on the local machine: :mod:`._local_ocr` reads the text
off the receipt and :mod:`._bank_text_parser` recovers the six fields from it.

The cloud VLM this module used to prefer has been removed from the ``--bank``
path. It read a receipt well, but a round trip cost tens of seconds against a
couple for local OCR, and it put a network dependency in front of every
conversion — a provider outage turned each request into a timeout followed by
the OCR run that would have answered it on its own. The receipt shapes the cloud
handled and OCR did not, notably a photo of a phone held sideways, are handled
locally now (see ``_read_page`` in :mod:`._local_ocr`).

Unrecognized fields come back as empty strings.
"""

from __future__ import annotations

import logging
from typing import Any, BinaryIO, Dict, Optional

from . import _local_ocr
from ._bank_fields import empty_info, normalize_info
from ._bank_text_parser import parse_bank_text
from ._timing import log_timing
from .._stream_info import StreamInfo

logger = logging.getLogger("markitdown.bank")


# Read the receipt with local OCR, returning nothing when the OCR extra is not
# installed or the model fails.
def _extract_via_ocr(
    file_stream: BinaryIO,
    stream_info: Optional[StreamInfo],
    *,
    debug_timing: bool = False,
) -> Dict[str, Any]:
    if not _local_ocr.is_available():
        logger.warning("[bank] Offline OCR is not available (check dependencies)")
        return {}

    try:
        with log_timing("image.bank.ocr", debug_timing):
            lines = _local_ocr.extract_line_details_offline(
                file_stream, stream_info, debug_timing=debug_timing
            )
    except Exception as exc:
        logger.warning("[bank] Offline OCR extraction failed: %s", exc)
        return {}

    if not lines:
        logger.debug("[bank] Offline OCR found 0 lines of text")
        return {}

    logger.debug("[bank] Offline OCR extracted %d lines", len(lines))
    with log_timing("image.bank.parse", debug_timing):
        return parse_bank_text(
            "\n".join(line["text"] for line in lines),
            line_confidences=[line["confidence"] for line in lines],
        )


# Extract the beneficiary details of a transfer receipt. Every field is a
# string; anything that could not be recognized is an empty string.
def extract_bank_info(
    file_stream: BinaryIO,
    stream_info: Optional[StreamInfo] = None,
    *,
    debug_timing: bool = False,
) -> Dict[str, str]:
    raw = _extract_via_ocr(file_stream, stream_info, debug_timing=debug_timing)
    result = normalize_info(raw) if raw else empty_info()
    logger.debug(
        "[bank] Extraction finished via backend=%s: %s",
        "ocr" if raw else "none",
        result,
    )
    return result
