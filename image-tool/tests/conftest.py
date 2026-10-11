from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.providers.mock import MockImageProvider

API_KEY = "test-api-key-0123456789abcdef"
SIGNING_KEY = b"signing-key-0123456789abcdef-xyz"
BASE_URL = "http://tool.test"


class FakeClock:
    """Mutable clock so expiry can be tested without sleeping."""

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        api_keys=(API_KEY,),
        url_signing_key=SIGNING_KEY,
        public_base_url=BASE_URL,
        provider="mock",
        data_dir=tmp_path / "images",
        image_ttl_seconds=3600,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(settings, clock):
    app = create_app(settings, provider=MockImageProvider(), clock=clock)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_KEY}"}
