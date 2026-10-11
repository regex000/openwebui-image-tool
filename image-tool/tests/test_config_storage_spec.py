from __future__ import annotations

import os
import time

import pytest

from app.config import ConfigError, Settings
from app.storage import ImageStore, sniff_extension

GOOD_KEY = "k" * 32


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("TOOL_API_KEY", "URL_SIGNING_KEY", "IMAGE_", "OPENAI_", "PUBLIC_BASE_URL")):
            monkeypatch.delenv(name, raising=False)


# ---- configuration ---------------------------------------------------------
def test_missing_api_key_fails_fast():
    with pytest.raises(ConfigError, match="TOOL_API_KEY"):
        Settings.from_env()


def test_short_api_key_is_rejected(monkeypatch):
    monkeypatch.setenv("TOOL_API_KEY", "short")
    with pytest.raises(ConfigError, match="at least"):
        Settings.from_env()


def test_previous_key_is_accepted_during_rotation(monkeypatch):
    monkeypatch.setenv("TOOL_API_KEY", GOOD_KEY)
    monkeypatch.setenv("TOOL_API_KEY_PREVIOUS", "p" * 32)
    assert Settings.from_env().api_keys == (GOOD_KEY, "p" * 32)


def test_secrets_can_be_read_from_files(monkeypatch, tmp_path):
    key_file = tmp_path / "tool_key"
    key_file.write_text(GOOD_KEY + "\n")
    upstream = tmp_path / "upstream"
    upstream.write_text("sk-from-file\n")
    monkeypatch.setenv("TOOL_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("IMAGE_API_KEY_FILE", str(upstream))

    settings = Settings.from_env()
    assert settings.api_keys == (GOOD_KEY,)
    assert settings.image_api_key == "sk-from-file"


def test_signing_key_is_derived_and_differs_from_the_api_key(monkeypatch):
    monkeypatch.setenv("TOOL_API_KEY", GOOD_KEY)
    settings = Settings.from_env()
    assert settings.url_signing_key != GOOD_KEY.encode()
    assert len(settings.url_signing_key) == 32


def test_secrets_never_appear_in_repr(monkeypatch):
    monkeypatch.setenv("TOOL_API_KEY", GOOD_KEY)
    monkeypatch.setenv("IMAGE_API_KEY", "sk-should-not-leak")
    text = repr(Settings.from_env())
    assert GOOD_KEY not in text
    assert "sk-should-not-leak" not in text


def test_unknown_provider_is_rejected(monkeypatch):
    monkeypatch.setenv("TOOL_API_KEY", GOOD_KEY)
    monkeypatch.setenv("IMAGE_PROVIDER", "midjourney")
    with pytest.raises(ConfigError, match="IMAGE_PROVIDER"):
        Settings.from_env()


# ---- storage ---------------------------------------------------------------
def test_sniff_extension():
    assert sniff_extension(b"\x89PNG\r\n\x1a\n....") == "png"
    assert sniff_extension(b"\xff\xd8\xff\xe0....") == "jpg"
    assert sniff_extension(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert sniff_extension(b"<svg onload=alert(1)>") is None


def test_purge_removes_only_expired_files(tmp_path):
    store = ImageStore(tmp_path, ttl_seconds=100)
    old = store.save(b"\x89PNG\r\n\x1a\n-old", "png")
    new = store.save(b"\x89PNG\r\n\x1a\n-new", "png")
    now = time.time()
    os.utime(tmp_path / old, (now - 500, now - 500))

    assert store.purge_expired(now) == 1
    assert store.path_for(old) is None
    assert store.path_for(new) is not None


# ---- OpenAPI contract (what Open WebUI imports) ------------------------------
def test_openapi_exposes_exactly_one_tool(client):
    spec = client.get("/openapi.json").json()
    assert list(spec["paths"]) == ["/generate_image"]
    operation = spec["paths"]["/generate_image"]["post"]
    assert operation["operationId"] == "generate_image"
    assert "markdown" in operation["description"]
    assert operation["security"] == [{"HTTPBearer": []}]

    schema = spec["components"]["schemas"]["GenerateImageRequest"]
    assert schema["required"] == ["prompt"]
    assert set(schema["properties"]) == {"prompt", "aspect"}
