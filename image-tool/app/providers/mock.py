"""Offline provider that renders a placeholder PNG.

It needs no API key and no network, so the whole stack (and the end-to-end test)
works out of the box. Output is deterministic for a given prompt.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import textwrap

from PIL import Image, ImageDraw, ImageFont

from .base import Aspect, GeneratedImage

_SIZES = {
    Aspect.SQUARE: (768, 768),
    Aspect.LANDSCAPE: (1024, 683),
    Aspect.PORTRAIT: (683, 1024),
}


def _render(prompt: str, aspect: Aspect) -> bytes:
    width, height = _SIZES[aspect]
    digest = hashlib.sha256(prompt.encode()).digest()
    top = (digest[0], digest[1], digest[2])
    bottom = (digest[3], digest[4], digest[5])

    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    for y in range(height):  # vertical gradient derived from the prompt hash
        t = y / (height - 1)
        draw.line(
            [(0, y), (width, y)],
            fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)),
        )

    title_font = ImageFont.load_default(size=max(18, width // 28))
    body_font = ImageFont.load_default(size=max(22, width // 22))
    margin = width // 12
    draw.text((margin, margin), "MOCK IMAGE PROVIDER", font=title_font, fill=(255, 255, 255))
    wrapped = textwrap.fill(prompt, width=max(16, width // 24))
    draw.multiline_text(
        (margin, margin * 2), wrapped[:600], font=body_font, fill=(255, 255, 255), spacing=8
    )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


class MockImageProvider:
    name = "mock"

    async def generate(self, prompt: str, aspect: Aspect) -> GeneratedImage:
        data = await asyncio.to_thread(_render, prompt, aspect)
        return GeneratedImage(data=data, model="mock-placeholder-v1")
