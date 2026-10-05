"""
Storage for the text/markdown files the API hands back as download links.

Endpoints whose CLI equivalent prints plain text write their result here and
return a link instead of the body. Everything the download route needs is
encoded in the file name (`<token>__<original name>`), so no database or
background worker is involved: expired files are swept whenever a new one is
written.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_UNSAFE_CHARACTERS = re.compile(r"[^A-Za-z0-9._-]+")
_FILE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}__[A-Za-z0-9._-]+$")
_MAXIMUM_STEM_LENGTH = 64
_TOKEN_SEPARATOR = "__"


@dataclass(frozen=True)
class StoredFile:
    file_id: str
    download_name: str
    path: Path
    size_bytes: int
    expires_at: float


# Reduce an uploaded file name to a safe stem for the generated output file.
def _safe_stem(source_name: str) -> str:
    stem = Path(source_name or "").stem
    cleaned = _UNSAFE_CHARACTERS.sub("-", stem).strip("-._")
    return (cleaned[:_MAXIMUM_STEM_LENGTH] or "output").lower()


class OutputStore:
    """Writes generated output files and resolves them back for download."""

    def __init__(self, directory: Path, ttl_seconds: int):
        self._directory = directory
        self._ttl_seconds = ttl_seconds
        self._directory.mkdir(parents=True, exist_ok=True)

    # Persist `content` and return the record the response links to.
    def save(self, content: str, *, source_name: str, suffix: str) -> StoredFile:
        self.purge_expired()

        download_name = f"{_safe_stem(source_name)}{suffix}"
        file_id = f"{uuid.uuid4().hex}{_TOKEN_SEPARATOR}{download_name}"
        path = self._directory / file_id
        path.write_text(content, encoding="utf-8")
        return self._describe(file_id, path)

    # Look up a stored file, rejecting ids that are malformed or past their TTL.
    def resolve(self, file_id: str) -> Optional[StoredFile]:
        if not _FILE_ID_PATTERN.match(file_id):
            return None

        path = self._directory / file_id
        if not path.is_file():
            return None

        stored = self._describe(file_id, path)
        if stored.expires_at <= time.time():
            path.unlink(missing_ok=True)
            return None
        return stored

    # Delete every file whose TTL has passed. Sweep failures are ignored so a
    # concurrent request removing the same file cannot fail this one.
    def purge_expired(self) -> None:
        deadline = time.time() - self._ttl_seconds
        for path in self._directory.glob(f"*{_TOKEN_SEPARATOR}*"):
            try:
                if path.is_file() and path.stat().st_mtime <= deadline:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    def _describe(self, file_id: str, path: Path) -> StoredFile:
        stat = path.stat()
        return StoredFile(
            file_id=file_id,
            download_name=file_id.split(_TOKEN_SEPARATOR, 1)[1],
            path=path,
            size_bytes=stat.st_size,
            expires_at=stat.st_mtime + self._ttl_seconds,
        )
