"""Provider registry."""

from __future__ import annotations

import httpx

from ..config import ConfigError, Settings
from .base import Aspect, GeneratedImage, ImageProvider, ProviderError
from .mock import MockImageProvider
from .openai import OpenAIImageProvider

__all__ = [
    "Aspect",
    "GeneratedImage",
    "ImageProvider",
    "ProviderError",
    "build_provider",
]


def build_provider(settings: Settings, client: httpx.AsyncClient) -> ImageProvider:
    """Instantiate the configured provider (``auto`` = OpenAI when a key is present)."""
    kind = settings.provider
    if kind == "auto":
        kind = "openai" if settings.image_api_key else "mock"

    if kind == "openai":
        if not settings.image_api_key:
            raise ConfigError("IMAGE_PROVIDER=openai requires IMAGE_API_KEY.")
        return OpenAIImageProvider(
            api_key=settings.image_api_key,
            base_url=settings.image_api_base_url,
            model=settings.image_model,
            quality=settings.image_quality,
            client=client,
        )
    return MockImageProvider()
