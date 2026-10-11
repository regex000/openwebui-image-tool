"""Abuse resistance: size limits, deadlines, quotas, resilience and provider selection."""

from __future__ import annotations

import asyncio
import io
import os
import time
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import ConfigError, Settings
from app.main import _purge_loop, create_app
from app.providers import Aspect, GeneratedImage, build_provider
from app.storage import ImageStore
from tests.conftest import API_KEY

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


# ---- request size ------------------------------------------------------------
def test_oversized_body_is_rejected_before_auth_or_parsing(client):
    response = client.post("/generate_image", content=b"{" + b"a" * 100_000 + b"}")
    assert response.status_code == 413


def test_oversized_chunked_body_is_rejected(client, auth):
    def chunks():
        for _ in range(50):
            yield b"x" * 1024  # no Content-Length: streamed

    response = client.post("/generate_image", content=chunks(), headers=auth)
    assert response.status_code == 413


# ---- deadline ----------------------------------------------------------------
class _SlowProvider:
    name = "slow"

    async def generate(self, prompt, aspect):
        await asyncio.sleep(30)
        return GeneratedImage(data=PNG, model="x")


def test_generation_is_bounded_by_the_overall_deadline(settings, clock, auth):
    fast_deadline = replace(settings, request_timeout_seconds=1)
    app = create_app(fast_deadline, provider=_SlowProvider(), clock=clock)
    started = time.monotonic()
    with TestClient(app) as client:
        response = client.post("/generate_image", json={"prompt": "slow"}, headers=auth)
    assert response.status_code == 504
    assert time.monotonic() - started < 10


# ---- aspect ratios actually change the output ------------------------------------
@pytest.mark.parametrize(
    ("aspect", "size"),
    [("square", (768, 768)), ("landscape", (1024, 683)), ("portrait", (683, 1024))],
)
def test_mock_provider_honours_the_aspect(client, auth, aspect, size):
    url = client.post("/generate_image", json={"prompt": "a", "aspect": aspect}, headers=auth).json()[
        "image_url"
    ]
    image = Image.open(io.BytesIO(client.get(url.split("tool.test", 1)[1]).content))
    assert image.size == size


# ---- storage -------------------------------------------------------------------
def test_non_image_output_is_not_persisted(settings, clock, auth):
    class Html:
        name = "html"

        async def generate(self, prompt, aspect):
            return GeneratedImage(data=b"<html></html>", model="x")

    with TestClient(create_app(settings, provider=Html(), clock=clock)) as client:
        assert client.post("/generate_image", json={"prompt": "x y"}, headers=auth).status_code == 502
    assert list(settings.data_dir.iterdir()) == []


def test_oldest_images_are_evicted_over_quota(tmp_path):
    store = ImageStore(tmp_path, ttl_seconds=3600, max_files=3)
    names = []
    for i in range(5):
        name = store.save(PNG, "png")
        os.utime(tmp_path / name, (1000 + i, 1000 + i))  # strictly increasing age
        names.append(name)
    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert len(remaining) == 3
    assert names[0] not in remaining  # the oldest went first


def test_purge_also_removes_stale_temp_files(tmp_path):
    store = ImageStore(tmp_path, ttl_seconds=100)
    stray = tmp_path / ".abc.png.tmp"
    stray.write_bytes(b"partial")
    now = time.time()
    os.utime(stray, (now - 500, now - 500))
    assert store.purge_expired(now) == 1
    assert not stray.exists()


def test_purge_loop_survives_a_failing_pass():
    class Flaky:
        calls = 0

        def purge_expired(self):
            self.calls += 1
            if self.calls == 1:
                raise OSError("volume briefly unavailable")
            return 0

    store = Flaky()

    async def run():
        task = asyncio.create_task(_purge_loop(store, interval=0.01))
        await asyncio.sleep(0.2)
        assert not task.done()  # the first failure did not kill the loop
        task.cancel()

    asyncio.run(run())
    assert store.calls >= 2


# ---- provider selection & config guard rails --------------------------------------
def test_auto_provider_selection(settings):
    client = httpx.AsyncClient()
    assert build_provider(replace(settings, provider="auto", image_api_key=""), client).name == "mock"
    assert build_provider(replace(settings, provider="auto", image_api_key="k" * 24), client).name == "openai"
    with pytest.raises(ConfigError):
        build_provider(replace(settings, provider="openai", image_api_key=""), client)


def _env(monkeypatch, **values):
    for name in list(os.environ):
        if name.startswith(("TOOL_API_KEY", "URL_SIGNING_KEY", "IMAGE_", "OPENAI_", "PUBLIC_BASE_URL")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TOOL_API_KEY", API_KEY)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_api_key_is_never_sent_over_plain_http(monkeypatch):
    _env(monkeypatch, IMAGE_API_KEY="sk-" + "x" * 30, IMAGE_API_BASE_URL="http://images.internal/v1")
    with pytest.raises(ConfigError, match="https"):
        Settings.from_env()


def test_plain_http_requires_explicit_opt_in(monkeypatch):
    _env(
        monkeypatch,
        IMAGE_API_KEY="sk-" + "x" * 30,
        IMAGE_API_BASE_URL="http://sd.internal/v1",
        IMAGE_API_ALLOW_INSECURE="true",
    )
    assert Settings.from_env().image_api_base_url == "http://sd.internal/v1"


def test_chat_model_key_is_never_reused_for_images(monkeypatch):
    _env(monkeypatch, OPENAI_API_KEY="sk-chat-vendor-key-" + "x" * 20)
    assert Settings.from_env().image_api_key == ""


@pytest.mark.parametrize(
    ("name", "value"),
    [("IMAGE_TTL_SECONDS", "5"), ("MAX_CONCURRENCY", "zero"), ("PUBLIC_BASE_URL", "localhost:8001")],
)
def test_invalid_numeric_and_url_settings_are_rejected(monkeypatch, name, value):
    _env(monkeypatch, **{name: value})
    with pytest.raises(ConfigError):
        Settings.from_env()
