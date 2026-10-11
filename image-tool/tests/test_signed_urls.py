from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from app.security import is_valid_signature, sign_image_path
from tests.conftest import BASE_URL, SIGNING_KEY


def _make_image(client, auth) -> tuple[str, str, str]:
    url = client.post(
        "/generate_image", json={"prompt": "a quiet forest"}, headers=auth
    ).json()["image_url"]
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    return parsed.path, query["exp"][0], query["sig"][0]


def test_valid_link_works(client, auth):
    path, exp, sig = _make_image(client, auth)
    assert client.get(path, params={"exp": exp, "sig": sig}).status_code == 200


def test_tampered_signature_is_forbidden(client, auth):
    path, exp, sig = _make_image(client, auth)
    forged = ("A" if sig[0] != "A" else "B") + sig[1:]
    assert client.get(path, params={"exp": exp, "sig": forged}).status_code == 403


def test_signature_is_bound_to_the_filename(client, auth):
    path, exp, sig = _make_image(client, auth)
    other_path, _, _ = _make_image(client, auth)
    assert path != other_path
    assert client.get(other_path, params={"exp": exp, "sig": sig}).status_code == 403


def test_signature_is_bound_to_the_expiry(client, auth):
    path, exp, sig = _make_image(client, auth)
    assert client.get(path, params={"exp": int(exp) + 10_000, "sig": sig}).status_code == 403


def test_link_expires(client, auth, clock):
    path, exp, sig = _make_image(client, auth)
    clock.now = int(exp) + 1
    assert client.get(path, params={"exp": exp, "sig": sig}).status_code == 403


def test_missing_query_parameters_are_rejected(client, auth):
    path, _, _ = _make_image(client, auth)
    assert client.get(path).status_code == 422


@pytest.mark.parametrize(
    "filename",
    ["../etc/passwd", "..%2f..%2fetc%2fpasswd", "abc.png", "0" * 32 + ".svg", "0" * 32 + ".png"],
)
def test_arbitrary_filenames_never_reach_the_filesystem(client, filename):
    exp = 1_700_000_000 + 600
    sig = sign_image_path(SIGNING_KEY, filename, exp)  # even with a *valid* signature
    response = client.get(f"/images/{filename}", params={"exp": exp, "sig": sig})
    assert response.status_code in (404, 422)


def test_signature_helpers_roundtrip():
    sig = sign_image_path(SIGNING_KEY, "f.png", 2000)
    assert is_valid_signature(SIGNING_KEY, "f.png", 2000, sig, now=1000)
    assert not is_valid_signature(SIGNING_KEY, "f.png", 2000, sig, now=2000)
    assert not is_valid_signature(b"x" * 32, "f.png", 2000, sig, now=1000)


def test_public_base_url_is_used_in_links(client, auth):
    url = client.post("/generate_image", json={"prompt": "x y z"}, headers=auth).json()["image_url"]
    assert url.startswith(BASE_URL + "/images/")


def test_signed_links_never_reach_the_logs(client, auth, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="image_tool"):
        path, exp, sig = _make_image(client, auth)
        client.get(path, params={"exp": exp, "sig": sig})
    assert "/images/" in caplog.text  # the request is logged ...
    assert sig not in caplog.text  # ... but the credential in its query string is not
    assert "sig=" not in caplog.text
