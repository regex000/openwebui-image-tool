"""Image-generation tool server for Open WebUI.

Exposes one OpenAPI operation, ``generate_image``, that Open WebUI registers as an
LLM tool. Generated images are stored on disk and served through short-lived,
HMAC-signed URLs so they render inline in the chat.

Run with:  uvicorn app.main:create_app --factory
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .config import Settings
from .limits import BodySizeLimitMiddleware
from .providers import Aspect, ImageProvider, ProviderError, build_provider
from .security import api_key_guard, is_valid_signature, sign_image_path
from .storage import MEDIA_TYPES, ImageStore, sniff_extension

log = logging.getLogger("image_tool")

MAX_PROMPT_CHARS = 1000
MAX_REQUEST_BYTES = 16 * 1024
MAX_REVISED_PROMPT_CHARS = 1000
PURGE_INTERVAL_SECONDS = 3600

# Characters that could break out of, or inject into, the markdown image syntax.
_ALT_TEXT_UNSAFE = re.compile(r"[\[\]()<>\\`*_|&!\r\n]+")

TOOL_DESCRIPTION = (
    "Create a brand-new image from a text description and return a link to it. "
    "Use this whenever the user asks you to draw, paint, illustrate, render, design or "
    "generate a picture, image, logo, poster, icon or photo. Write a rich, specific prompt "
    "(subject, style, colours, lighting, composition) based on the user's request. "
    "After the call, show the image to the user by including the `markdown` value from the "
    "result verbatim in your reply."
)
PRESENTATION_NOTE = (
    "Show this image to the user by including the `markdown` value verbatim in your reply."
)


class GenerateImageRequest(BaseModel):
    prompt: str = Field(
        ...,
        min_length=1,
        max_length=MAX_PROMPT_CHARS,
        description=(
            "Detailed visual description of the image to create: subject, style, colours, "
            f"lighting and composition (at most {MAX_PROMPT_CHARS} characters). "
            "English gives the best results."
        ),
        examples=["A watercolor lighthouse at dawn, soft pastel sky, gentle waves"],
    )
    aspect: Aspect = Field(
        Aspect.SQUARE,
        description="Image shape: 'square' (1:1), 'landscape' (wide) or 'portrait' (tall).",
    )


class GenerateImageResponse(BaseModel):
    image_url: str = Field(..., description="Signed, time-limited URL of the generated image.")
    markdown: str = Field(..., description="Markdown that renders the image inline in chat.")
    note: str = Field(..., description="Instruction for the assistant on how to present the result.")
    prompt: str
    revised_prompt: str | None = Field(
        None, description="Prompt as rewritten by the provider, if any."
    )
    model: str
    provider: str
    expires_at: datetime = Field(
        ..., description="UTC time after which the image URL stops working."
    )


def _alt_text(prompt: str) -> str:
    cleaned = _ALT_TEXT_UNSAFE.sub(" ", prompt).strip()
    return (cleaned[:80] + "…") if len(cleaned) > 80 else cleaned


async def _purge_loop(store: ImageStore, interval: float = PURGE_INTERVAL_SECONDS) -> None:
    """Delete expired images forever; a failed pass is logged and retried next interval."""
    while True:
        try:
            removed = await asyncio.to_thread(store.purge_expired)
            if removed:
                log.info("purged %d expired image(s)", removed)
        except Exception:  # noqa: BLE001 - the loop must survive any filesystem hiccup
            log.exception("image purge failed; retrying in %ss", interval)
        await asyncio.sleep(interval)


def create_app(
    settings: Settings | None = None,
    *,
    provider: ImageProvider | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    """Application factory. ``provider`` and ``clock`` are injectable for tests."""
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
        )
    settings = settings or Settings.from_env()
    store = ImageStore(settings.data_dir, settings.image_ttl_seconds, settings.max_stored_images)
    semaphore = asyncio.Semaphore(settings.max_concurrency)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as stack:
            active = provider
            if active is None:
                client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        timeout=settings.request_timeout_seconds, follow_redirects=False
                    )
                )
                active = build_provider(settings, client)
            app.state.provider = active
            log.info("image provider: %s", active.name)

            purger = asyncio.create_task(_purge_loop(store))
            try:
                yield
            finally:
                purger.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await purger

    app = FastAPI(
        title="Image Generation Tool",
        version="1.0.0",
        description="OpenAPI tool server that lets an Open WebUI model generate images.",
        lifespan=lifespan,
        docs_url="/docs" if settings.enable_docs else None,
        redoc_url="/redoc" if settings.enable_docs else None,
    )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_REQUEST_BYTES)
    guard = api_key_guard(settings.api_keys)

    @app.middleware("http")
    async def request_log(request: Request, call_next):
        # Log the path only: query strings carry signed-URL credentials and must not be logged.
        started = time.perf_counter()
        response = await call_next(request)
        if request.url.path != "/healthz":
            log.info(
                "%s %s -> %d (%.0f ms)",
                request.method,
                request.url.path,
                response.status_code,
                (time.perf_counter() - started) * 1000,
            )
        return response

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "provider": app.state.provider.name}

    @app.post(
        "/generate_image",
        operation_id="generate_image",
        summary="Generate an image from a text prompt",
        description=TOOL_DESCRIPTION,
        response_model=GenerateImageResponse,
        dependencies=[Depends(guard)],
        responses={
            401: {"description": "Missing or invalid API key."},
            413: {"description": "Request body too large."},
            422: {"description": "Invalid prompt, or prompt rejected by the provider."},
            429: {"description": "Provider rate limit reached."},
            502: {"description": "Image provider failure."},
            504: {"description": "Image provider timed out."},
        },
        tags=["images"],
    )
    async def generate_image(body: GenerateImageRequest) -> GenerateImageResponse:
        prompt = body.prompt.strip()
        if not prompt:
            raise HTTPException(status_code=422, detail="prompt must not be blank.")

        started = time.perf_counter()
        try:
            # One deadline for queueing + provider calls + retries, kept below Open WebUI's
            # own tool timeout so a slow provider never leaves work running unattended.
            async with asyncio.timeout(settings.request_timeout_seconds):
                async with semaphore:
                    image = await app.state.provider.generate(prompt, body.aspect)
        except TimeoutError:
            log.warning("generation exceeded the %ss deadline", settings.request_timeout_seconds)
            raise HTTPException(504, "Image generation timed out. Try again.") from None
        except ProviderError as exc:
            log.warning("provider error status=%s", exc.status_code)
            raise HTTPException(exc.status_code, exc.public_message) from exc

        extension = sniff_extension(image.data)
        if extension is None:
            log.error("provider %s returned data that is not an image", app.state.provider.name)
            raise HTTPException(502, "The image provider returned data that is not an image.")

        filename = await asyncio.to_thread(store.save, image.data, extension)
        expires_at = int(clock()) + settings.image_ttl_seconds
        signature = sign_image_path(settings.url_signing_key, filename, expires_at)
        url = f"{settings.public_base_url}/images/{filename}?exp={expires_at}&sig={signature}"

        log.info(
            "generated image provider=%s model=%s aspect=%s prompt_chars=%d bytes=%d ms=%.0f",
            app.state.provider.name,
            image.model,
            body.aspect.value,
            len(prompt),
            len(image.data),
            (time.perf_counter() - started) * 1000,
        )
        revised = image.revised_prompt[:MAX_REVISED_PROMPT_CHARS] if image.revised_prompt else None
        return GenerateImageResponse(
            image_url=url,
            markdown=f"![{_alt_text(prompt)}]({url})",
            note=PRESENTATION_NOTE,
            prompt=prompt,
            revised_prompt=revised,
            model=image.model,
            provider=app.state.provider.name,
            expires_at=datetime.fromtimestamp(expires_at, tz=timezone.utc),
        )

    @app.get("/images/{filename}", include_in_schema=False)
    async def get_image(
        filename: str,
        exp: int = Query(...),
        sig: str = Query(..., max_length=128),
    ) -> FileResponse:
        # Browsers cannot send a Bearer header from an <img> tag, so access is granted by
        # an expiring HMAC signature over (filename, expiry) instead.
        if not is_valid_signature(settings.url_signing_key, filename, exp, sig, clock()):
            raise HTTPException(status_code=403, detail="Invalid or expired image link.")
        path = store.path_for(filename)
        if path is None:
            raise HTTPException(status_code=404, detail="Image not found.")
        return FileResponse(
            path,
            media_type=MEDIA_TYPES[path.suffix.lstrip(".")],
            headers={
                "Cache-Control": "private, max-age=3600",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "Content-Disposition": "inline",
            },
        )

    return app
