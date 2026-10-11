from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.main import MAX_PROMPT_CHARS, create_app
from app.providers import Aspect, GeneratedImage, ProviderError
from tests.conftest import BASE_URL

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _generate(client, auth, **body):
    body.setdefault("prompt", "a lighthouse at dawn")
    return client.post("/generate_image", json=body, headers=auth)


def test_generate_returns_markdown_and_signed_url(client, auth):
    response = _generate(client, auth)
    assert response.status_code == 200
    data = response.json()

    assert data["image_url"].startswith(f"{BASE_URL}/images/")
    assert data["markdown"] == f"![a lighthouse at dawn]({data['image_url']})"
    assert data["provider"] == "mock"
    assert "markdown" in data["note"]
    query = parse_qs(urlparse(data["image_url"]).query)
    assert set(query) == {"exp", "sig"}


def test_generated_image_can_be_downloaded_without_bearer_token(client, auth):
    url = _generate(client, auth).json()["image_url"]
    # Browsers load <img> without an Authorization header; the signature is the credential.
    image = client.get(url.removeprefix(BASE_URL))
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.headers["x-content-type-options"] == "nosniff"
    assert image.content.startswith(PNG_MAGIC)


@pytest.mark.parametrize("aspect", [a.value for a in Aspect])
def test_all_aspects_are_supported(client, auth, aspect):
    assert _generate(client, auth, aspect=aspect).status_code == 200


def test_invalid_aspect_is_rejected(client, auth):
    assert _generate(client, auth, aspect="panorama").status_code == 422


@pytest.mark.parametrize("prompt", ["", "   "])
def test_blank_prompt_is_rejected(client, auth, prompt):
    assert _generate(client, auth, prompt=prompt).status_code == 422


def test_prompt_longer_than_limit_is_rejected(client, auth):
    response = _generate(client, auth, prompt="x" * (MAX_PROMPT_CHARS + 1))
    assert response.status_code == 422


@pytest.mark.parametrize(
    "prompt",
    ["evil](http://attacker.example/x) ![pwn\nnewline", "ends with a backslash \\", "`code` *bold* _it_ | & !"],
)
def test_markdown_alt_text_cannot_break_out_of_the_image_syntax(client, auth, prompt):
    data = _generate(client, auth, prompt=prompt).json()
    markdown = data["markdown"]
    assert markdown.endswith(f"]({data['image_url']})")  # exactly one image, intact
    alt = markdown[2 : markdown.rindex("](")]
    assert not any(ch in alt for ch in "[]()<>\\`*_|&!\n")


class _StubProvider:
    name = "stub"

    def __init__(self, result):
        self._result = result

    async def generate(self, prompt, aspect):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _client_with(provider, settings, clock):
    return TestClient(create_app(settings, provider=provider, clock=clock))


def test_provider_error_is_surfaced_with_its_status(settings, clock, auth):
    provider = _StubProvider(ProviderError(429, "slow down"))
    with _client_with(provider, settings, clock) as client:
        response = _generate(client, auth)
    assert response.status_code == 429
    assert response.json()["detail"] == "slow down"


def test_non_image_provider_output_is_refused(settings, clock, auth):
    provider = _StubProvider(GeneratedImage(data=b"<html>not an image</html>", model="x"))
    with _client_with(provider, settings, clock) as client:
        response = _generate(client, auth)
    assert response.status_code == 502
