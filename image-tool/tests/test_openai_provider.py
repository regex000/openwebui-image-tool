from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from app.providers import Aspect, ProviderError
from app.providers.openai import OpenAIImageProvider

SECRET = "sk-super-secret-key"
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _provider(handler, *, model="gpt-image-2", quality="", sleeps=None) -> OpenAIImageProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def fake_sleep(seconds: float) -> None:  # keep tests instant
        if sleeps is not None:
            sleeps.append(seconds)

    return OpenAIImageProvider(
        api_key=SECRET,
        base_url="https://api.test/v1/",
        model=model,
        quality=quality,
        client=client,
        sleep=fake_sleep,
    )


def _ok(b64: str | None = None, **extra):
    item = {"b64_json": b64 or base64.b64encode(PNG_1X1).decode(), **extra}
    return httpx.Response(200, json={"data": [item]})


def _generate(provider, prompt="a cat", aspect=Aspect.SQUARE):
    return asyncio.run(provider.generate(prompt, aspect))


def test_request_shape_for_gpt_image_models():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return _ok()

    image = _generate(_provider(handler, quality="low"), "a cat", Aspect.LANDSCAPE)

    assert seen["url"] == "https://api.test/v1/images/generations"
    assert seen["auth"] == f"Bearer {SECRET}"
    assert seen["body"] == {
        "model": "gpt-image-2",
        "prompt": "a cat",
        "size": "1536x1024",
        "n": 1,
        "quality": "low",
    }
    assert image.data == PNG_1X1
    assert image.model == "gpt-image-2"


def test_dalle_models_use_their_own_sizes_and_request_base64():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _ok(revised_prompt="a fluffy cat")

    image = _generate(_provider(handler, model="dall-e-3"), "a cat", Aspect.PORTRAIT)
    assert seen["body"]["size"] == "1024x1792"
    assert seen["body"]["response_format"] == "b64_json"
    assert image.revised_prompt == "a fluffy cat"


@pytest.mark.parametrize(
    ("upstream_status", "expected_status"),
    [(400, 422), (401, 502), (403, 502), (429, 429), (500, 502)],
)
def test_upstream_errors_are_mapped(upstream_status, expected_status):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(upstream_status, json={"error": {"message": f"bad {SECRET}"}})

    with pytest.raises(ProviderError) as info:
        _generate(_provider(handler))
    assert info.value.status_code == expected_status
    if upstream_status != 400:  # only 400 messages are relayed, and they are truncated
        assert SECRET not in info.value.public_message


def test_timeout_maps_to_504():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderError) as info:
        _generate(_provider(handler))
    assert info.value.status_code == 504


def test_url_only_responses_are_refused_instead_of_fetched():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"url": "http://169.254.169.254/latest"}]})

    with pytest.raises(ProviderError) as info:
        _generate(_provider(handler))
    assert info.value.status_code == 502


def test_garbage_base64_is_a_502():
    with pytest.raises(ProviderError) as info:
        _generate(_provider(lambda r: _ok("%%%not-base64%%%")))
    assert info.value.status_code == 502


def test_transient_errors_are_retried_then_succeed():
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"retry-after": "4"}, json={"error": {"code": "rate_limit_exceeded"}})
        return _ok()

    image = _generate(_provider(handler, sleeps=sleeps))
    assert image.data == PNG_1X1
    assert calls["n"] == 3
    assert sleeps == [4.0, 4.0]  # honours Retry-After


def test_quota_errors_are_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={"error": {"code": "insufficient_quota", "message": "no credit"}})

    with pytest.raises(ProviderError) as info:
        _generate(_provider(handler))
    assert calls["n"] == 1
    assert info.value.status_code == 429


def test_retries_are_bounded():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={"error": {"message": "overloaded"}})

    with pytest.raises(ProviderError):
        _generate(_provider(handler))
    assert calls["n"] == 3  # 1 attempt + 2 retries
