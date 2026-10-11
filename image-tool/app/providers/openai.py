"""OpenAI Images API provider (also works with OpenAI-compatible servers).

The API key is only ever sent in the ``Authorization`` header of the upstream
request. It is never logged, returned to a caller, or included in an error.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Awaitable, Callable

import httpx

from .base import Aspect, GeneratedImage, ProviderError

# Pixel sizes valid on every GPT image model.
_SIZES_GPT_IMAGE = {
    Aspect.SQUARE: "1024x1024",
    Aspect.LANDSCAPE: "1536x1024",
    Aspect.PORTRAIT: "1024x1536",
}
# Kept for OpenAI-compatible servers that still advertise a dall-e-3 model.
_SIZES_DALLE3 = {
    Aspect.SQUARE: "1024x1024",
    Aspect.LANDSCAPE: "1792x1024",
    Aspect.PORTRAIT: "1024x1792",
}

_MAX_UPSTREAM_MESSAGE = 300
_MAX_IMAGE_BYTES = 20 * 1024 * 1024  # refuse absurdly large upstream payloads
_MAX_RETRIES = 2
_MAX_BACKOFF_SECONDS = 10.0
# Quota / billing errors are reported as 429 but retrying them cannot succeed.
_NON_RETRYABLE_429_CODES = frozenset(
    {
        "insufficient_quota",
        "billing_hard_limit_reached",
        "credit_balance_exhausted",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
    }
)


def _sizes_for(model: str) -> dict[Aspect, str]:
    return _SIZES_DALLE3 if model.startswith("dall-e") else _SIZES_GPT_IMAGE


def _error_fields(response: httpx.Response) -> tuple[str, str]:
    """Return (message, code) from an OpenAI-style error body, or empty strings."""
    try:
        error = response.json().get("error") or {}
        return str(error.get("message") or ""), str(error.get("code") or "")
    except (ValueError, AttributeError):
        return "", ""


class OpenAIImageProvider:
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        quality: str,
        client: httpx.AsyncClient,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._api_key = api_key
        self._endpoint = f"{base_url.rstrip('/')}/images/generations"
        self._model = model
        self._quality = quality
        self._client = client
        self._sleep = sleep

    def _payload(self, prompt: str, aspect: Aspect) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self._model,
            "prompt": prompt,
            "size": _sizes_for(self._model)[aspect],
            "n": 1,
        }
        if self._quality:
            payload["quality"] = self._quality
        if self._model.startswith("dall-e"):
            # GPT image models always return base64 and reject ``response_format``;
            # legacy dall-e style servers default to URLs, so ask for base64 explicitly.
            payload["response_format"] = "b64_json"
        return payload

    async def generate(self, prompt: str, aspect: Aspect) -> GeneratedImage:
        response = await self._post_with_retries(self._payload(prompt, aspect))
        if response.status_code != 200:
            raise self._error_for(response)

        try:
            item = response.json()["data"][0]
            b64 = item.get("b64_json")
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise ProviderError(502, "The image provider returned an unexpected response.") from exc
        if not b64:
            # We deliberately never fetch provider-supplied URLs (SSRF risk).
            raise ProviderError(
                502,
                "The image provider returned no base64 image data (URL responses are unsupported).",
            )
        if len(b64) > _MAX_IMAGE_BYTES * 4 // 3 + 4:
            raise ProviderError(502, "The image provider returned an oversized image.")
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ProviderError(502, "The image provider returned invalid image data.") from exc
        return GeneratedImage(data=data, model=self._model, revised_prompt=item.get("revised_prompt"))

    async def _post_with_retries(self, payload: dict[str, object]) -> httpx.Response:
        """POST, retrying transient failures (429 rate limits, 5xx) with capped backoff."""
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = await self._client.post(
                    self._endpoint,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
            except httpx.TimeoutException as exc:
                raise ProviderError(504, "The image provider timed out. Try again.") from exc
            except httpx.HTTPError as exc:
                raise ProviderError(502, "Could not reach the image provider.") from exc

            if attempt == _MAX_RETRIES or not self._is_retryable(response):
                return response
            await self._sleep(self._backoff(response, attempt))
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _is_retryable(response: httpx.Response) -> bool:
        if response.status_code in (500, 502, 503, 504):
            return True
        if response.status_code == 429:
            return _error_fields(response)[1] not in _NON_RETRYABLE_429_CODES
        return False

    @staticmethod
    def _backoff(response: httpx.Response, attempt: int) -> float:
        try:
            requested = float(response.headers.get("retry-after", ""))
        except ValueError:
            requested = 0.0
        return min(max(requested, 2.0**attempt), _MAX_BACKOFF_SECONDS)

    @staticmethod
    def _error_for(response: httpx.Response) -> ProviderError:
        status = response.status_code
        message, _ = _error_fields(response)
        message = message[:_MAX_UPSTREAM_MESSAGE]

        if status == 400:
            detail = "The image provider rejected the request."
            if message:
                detail = f"{detail} {message}"
            return ProviderError(422, detail)
        if status in (401, 403):
            # Never echo upstream auth errors; they are a server-side configuration problem.
            return ProviderError(
                502, "The image provider refused our credentials (server configuration error)."
            )
        if status == 429:
            return ProviderError(
                429, "The image provider is rate limiting requests. Try again shortly."
            )
        return ProviderError(502, f"The image provider failed with status {status}.")
