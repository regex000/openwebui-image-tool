"""Provider interface shared by all image back-ends."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class Aspect(str, Enum):
    """Provider-agnostic image shape; each provider maps it to concrete pixel sizes."""

    SQUARE = "square"
    LANDSCAPE = "landscape"
    PORTRAIT = "portrait"


@dataclass(frozen=True)
class GeneratedImage:
    data: bytes
    model: str
    revised_prompt: str | None = None


class ProviderError(Exception):
    """A failure that is safe to show to the caller (never contains secrets)."""

    def __init__(self, status_code: int, public_message: str) -> None:
        super().__init__(public_message)
        self.status_code = status_code
        self.public_message = public_message


class ImageProvider(Protocol):
    name: str

    async def generate(self, prompt: str, aspect: Aspect) -> GeneratedImage:
        """Return image bytes for ``prompt`` or raise :class:`ProviderError`."""
        ...
