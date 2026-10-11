"""Runtime configuration for the image tool server.

Everything comes from environment variables. Secrets additionally support the
Docker/Kubernetes ``<NAME>_FILE`` convention: when ``<NAME>_FILE`` is set, the
value is read from that file, so a secret never has to live in the environment
or in an image layer.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass, field
from pathlib import Path

MIN_API_KEY_LENGTH = 24
PROVIDERS = ("auto", "openai", "mock")
DEFAULT_IMAGE_MODEL = "gpt-image-2"
_SIGNING_KEY_LABEL = b"owui-image-tool/url-signing/v1"


class ConfigError(RuntimeError):
    """Raised at start-up when the configuration is missing or unsafe."""


def _read(name: str, default: str = "") -> str:
    """Return ``$NAME`` or, if ``$NAME_FILE`` is set, the contents of that file."""
    file_var = os.environ.get(f"{name}_FILE")
    if file_var:
        try:
            return Path(file_var).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"{name}_FILE points to an unreadable file: {file_var}") from exc
    return os.environ.get(name, default).strip()


def _read_int(name: str, default: int, minimum: int = 1) -> int:
    raw = _read(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _read_bool(name: str, default: bool = False) -> bool:
    raw = _read(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Validated, immutable settings. Secret fields are excluded from ``repr``."""

    api_keys: tuple[str, ...] = field(repr=False)
    url_signing_key: bytes = field(repr=False)
    public_base_url: str = "http://localhost:8001"
    provider: str = "auto"
    image_api_key: str = field(default="", repr=False)
    image_api_base_url: str = "https://api.openai.com/v1"
    image_model: str = DEFAULT_IMAGE_MODEL
    image_quality: str = ""
    data_dir: Path = Path("/data/images")
    image_ttl_seconds: int = 7 * 24 * 3600
    max_stored_images: int = 1000
    max_concurrency: int = 4
    request_timeout_seconds: int = 180
    enable_docs: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        primary_key = _read("TOOL_API_KEY")
        if not primary_key:
            raise ConfigError("TOOL_API_KEY is not set. Run ./scripts/init-env.sh to generate one.")
        # The current key, plus an optional previous key that stays valid while rotating.
        api_keys = tuple(k for k in (primary_key, _read("TOOL_API_KEY_PREVIOUS")) if k)
        for key in api_keys:
            if len(key) < MIN_API_KEY_LENGTH:
                raise ConfigError(
                    f"TOOL_API_KEY(_PREVIOUS) must be at least {MIN_API_KEY_LENGTH} characters."
                )

        signing = _read("URL_SIGNING_KEY")
        if signing and len(signing) < MIN_API_KEY_LENGTH:
            raise ConfigError(f"URL_SIGNING_KEY must be at least {MIN_API_KEY_LENGTH} characters.")
        # Derive a separate key from the primary API key when none is configured, so the
        # bearer token itself is never used as the MAC key. Set URL_SIGNING_KEY explicitly
        # if image links must survive a TOOL_API_KEY rotation.
        signing_key = (
            signing.encode()
            if signing
            else hmac.new(primary_key.encode(), _SIGNING_KEY_LABEL, hashlib.sha256).digest()
        )

        provider = _read("IMAGE_PROVIDER", "auto").lower()
        if provider not in PROVIDERS:
            raise ConfigError(f"IMAGE_PROVIDER must be one of {', '.join(PROVIDERS)}")

        public_base_url = _read("PUBLIC_BASE_URL", "http://localhost:8001").rstrip("/")
        if not public_base_url.startswith(("http://", "https://")):
            raise ConfigError("PUBLIC_BASE_URL must start with http:// or https://")

        # Deliberately NOT falling back to the chat model's OPENAI_API_KEY: that key may
        # belong to a different vendor and must never be forwarded to the image API.
        image_api_key = _read("IMAGE_API_KEY")
        image_api_base_url = _read("IMAGE_API_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        if (
            image_api_key
            and not image_api_base_url.startswith("https://")
            and not _read_bool("IMAGE_API_ALLOW_INSECURE")
        ):
            raise ConfigError(
                "IMAGE_API_BASE_URL must use https:// when an API key is configured "
                "(set IMAGE_API_ALLOW_INSECURE=true only for a trusted local server)."
            )

        return cls(
            api_keys=api_keys,
            url_signing_key=signing_key,
            public_base_url=public_base_url,
            provider=provider,
            image_api_key=image_api_key,
            image_api_base_url=image_api_base_url,
            image_model=_read("IMAGE_MODEL", DEFAULT_IMAGE_MODEL),
            image_quality=_read("IMAGE_QUALITY"),
            data_dir=Path(_read("DATA_DIR", "/data/images")),
            image_ttl_seconds=_read_int("IMAGE_TTL_SECONDS", 7 * 24 * 3600, minimum=60),
            max_stored_images=_read_int("MAX_STORED_IMAGES", 1000, minimum=1),
            max_concurrency=_read_int("MAX_CONCURRENCY", 4),
            request_timeout_seconds=_read_int("REQUEST_TIMEOUT_SECONDS", 180, minimum=5),
            enable_docs=_read_bool("ENABLE_DOCS", False),
        )
