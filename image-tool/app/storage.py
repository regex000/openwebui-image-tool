"""On-disk storage for generated images, with magic-byte sniffing, TTL purge and a quota."""

from __future__ import annotations

import os
import re
import secrets
import time
from pathlib import Path

# 128 random bits + a known extension: no user-controlled path component ever reaches disk.
_FILENAME_RE = re.compile(r"^[0-9a-f]{32}\.(png|jpg|webp)$")

MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}


def sniff_extension(data: bytes) -> str | None:
    """Identify an image by its magic bytes. Returns None for anything else."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


class ImageStore:
    def __init__(self, root: Path, ttl_seconds: int, max_files: int = 1000) -> None:
        self.root = root
        self.ttl_seconds = ttl_seconds
        self.max_files = max_files
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, data: bytes, extension: str) -> str:
        """Atomically write ``data`` and return the generated filename.

        When the quota is exceeded the oldest images are evicted, so the volume cannot
        fill up no matter how many images are requested within the TTL.
        """
        filename = f"{secrets.token_hex(16)}.{extension}"
        target = self.root / filename
        tmp = self.root / f".{filename}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, target)
        self._evict_over_quota()
        return filename

    def path_for(self, filename: str) -> Path | None:
        """Resolve ``filename`` to an existing file, or None if invalid/missing."""
        if not _FILENAME_RE.fullmatch(filename):
            return None
        path = self.root / filename
        return path if path.is_file() else None

    def purge_expired(self, now: float | None = None) -> int:
        """Delete images older than the TTL (and stray temp files). Returns the count."""
        cutoff = (now if now is not None else time.time()) - self.ttl_seconds
        removed = 0
        for entry in self.root.iterdir():
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def _evict_over_quota(self) -> None:
        images = [p for p in self.root.iterdir() if _FILENAME_RE.fullmatch(p.name)]
        excess = len(images) - self.max_files
        if excess <= 0:
            return
        images.sort(key=lambda p: p.stat().st_mtime)
        for path in images[:excess]:
            path.unlink(missing_ok=True)
