"""Authentication primitives.

* ``api_key_guard`` protects the tool endpoints with a static Bearer API key.
* ``sign_image_path`` / ``is_valid_signature`` implement expiring, HMAC-signed
  image URLs. Browsers load ``<img>`` tags without an Authorization header, so the
  image endpoint is protected by an unguessable, time-limited signature instead.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Callable

from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer(
    auto_error=False,
    description="Static API key shared between Open WebUI and this tool server.",
)


def api_key_guard(valid_keys: tuple[str, ...]) -> Callable[..., None]:
    """Build a FastAPI dependency that accepts any of ``valid_keys`` as a Bearer token."""
    encoded = tuple(key.encode() for key in valid_keys)

    def guard(
        credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    ) -> None:
        presented = credentials.credentials.encode() if credentials else b""
        # Compare against every key without short-circuiting, in constant time.
        matched = False
        for key in encoded:
            matched |= hmac.compare_digest(presented, key)
        if not credentials or not matched:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid API key.",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return guard


def sign_image_path(key: bytes, filename: str, expires_at: int) -> str:
    """Return a URL-safe HMAC-SHA256 signature binding ``filename`` to ``expires_at``."""
    message = f"{filename}:{expires_at}".encode()
    digest = hmac.new(key, message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def is_valid_signature(
    key: bytes, filename: str, expires_at: int, signature: str, now: float
) -> bool:
    """True when ``signature`` matches and ``expires_at`` is still in the future."""
    if expires_at <= now:
        return False
    expected = sign_image_path(key, filename, expires_at)
    return hmac.compare_digest(expected.encode(), signature.encode())
