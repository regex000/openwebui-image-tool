from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from app.providers.mock import MockImageProvider

BODY = {"prompt": "a red bicycle"}


def test_generate_requires_api_key(client):
    response = client.post("/generate_image", json=BODY)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_generate_rejects_wrong_key(client):
    response = client.post(
        "/generate_image", json=BODY, headers={"Authorization": "Bearer not-the-key"}
    )
    assert response.status_code == 401


def test_generate_rejects_non_bearer_scheme(client):
    response = client.post(
        "/generate_image", json=BODY, headers={"Authorization": "Basic dXNlcjpwYXNz"}
    )
    assert response.status_code == 401


def test_generate_accepts_valid_key(client, auth):
    response = client.post("/generate_image", json=BODY, headers=auth)
    assert response.status_code == 200


def test_any_configured_key_is_accepted_for_rotation(settings, clock):
    from dataclasses import replace

    rotating = replace(settings, api_keys=("old-key-0123456789abcdefghij", "new-key-0123456789abcdefghij"))
    app = create_app(rotating, provider=MockImageProvider(), clock=clock)
    with TestClient(app) as client:
        for key in rotating.api_keys:
            ok = client.post("/generate_image", json=BODY, headers={"Authorization": f"Bearer {key}"})
            assert ok.status_code == 200


def test_healthz_is_public_and_leaks_nothing_sensitive(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "provider": "mock"}


def test_docs_are_disabled_by_default(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
