"""The wizard must never answer without a valid access token."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.main import app as fastapi_app
from app.services import auth as auth_mod
from app.services.auth import COOKIE_NAME, ensure_token, get_token, token_matches, token_path


PROTECTED_GETS = (
    "/",
    "/api/info",
    "/api/panels",
    "/api/system-check",
    "/api/pasarguard/status",
    "/api/upload/whatever/analysis",
    "/api/self-uninstall",
    "/static/js/app.js",
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    token_file = tmp_path / ".access_token"
    monkeypatch.setattr(auth_mod, "TOKEN_FILE", token_file)
    auth_mod._cached_token = None
    ensure_token()
    with TestClient(fastapi_app) as c:
        yield c
    auth_mod._cached_token = None


@pytest.mark.parametrize("path", PROTECTED_GETS)
def test_get_requires_token(client, path):
    assert client.get(path).status_code == 401


def test_post_requires_token(client):
    assert client.post("/api/self-uninstall").status_code == 401
    assert client.post("/api/pasarguard/cleanup", json={"upload_id": "x"}).status_code == 401
    assert client.post("/api/migrate", json={"source_panel": "marzban"}).status_code == 401


def test_wrong_token_is_rejected(client):
    bad = "0" * 48
    assert client.get("/api/info", params={"token": bad}).status_code == 401
    assert client.get("/api/info", headers={"X-Auth-Token": bad}).status_code == 401
    client.cookies.set(COOKIE_NAME, bad)
    assert client.get("/api/info").status_code == 401


def test_valid_token_via_header_query_and_cookie(client):
    token = get_token()
    assert client.get("/api/panels", headers={"X-Auth-Token": token}).status_code == 200

    r = client.get("/api/panels", params={"token": token})
    assert r.status_code == 200
    assert COOKIE_NAME in r.cookies

    client.cookies.set(COOKIE_NAME, token)
    assert client.get("/api/panels").status_code == 200


def test_api_responses_are_not_cached(client):
    r = client.get("/api/info", headers={"X-Auth-Token": get_token()})
    assert r.headers.get("cache-control") == "no-store"


def test_login_sets_cookie_and_redirects(client):
    r = client.get("/login", params={"token": get_token()}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert COOKIE_NAME in r.cookies
    assert client.get("/login", params={"token": "nope"}).status_code == 401


def test_websocket_requires_token(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises((WebSocketDisconnect, Exception)):
        with client.websocket_connect("/ws/migrate/anything"):
            pass


def test_token_comparison_rejects_prefixes():
    token = get_token()
    assert token_matches(token)
    assert not token_matches(token[:-1])
    assert not token_matches(token + "x")
    assert not token_matches("")
    assert not token_matches(None)


def test_ensure_token_writes_readable_0600_file(tmp_path, monkeypatch):
    token_file = tmp_path / ".access_token"
    monkeypatch.setattr(auth_mod, "TOKEN_FILE", token_file)
    auth_mod._cached_token = None
    monkeypatch.delenv("PG_MIGRATOR_TOKEN", raising=False)

    token = ensure_token()
    assert token_file.is_file()
    assert token_file.read_text(encoding="utf-8").strip() == token
    mode = stat.S_IMODE(token_file.stat().st_mode)
    assert mode == 0o600
    assert token_path() == token_file

    # Unauthenticated login page must still leave a recoverable file behind.
    auth_mod._cached_token = None
    with TestClient(fastapi_app) as c:
        page = c.get("/")
        assert page.status_code == 401
        assert "cat " in page.text
        assert str(token_file) in page.text
        assert "?token=" in page.text
    assert token_file.read_text(encoding="utf-8").strip() == token


def test_env_token_is_persisted_to_disk(tmp_path, monkeypatch):
    token_file = tmp_path / ".access_token"
    monkeypatch.setattr(auth_mod, "TOKEN_FILE", token_file)
    auth_mod._cached_token = None
    monkeypatch.setenv("PG_MIGRATOR_TOKEN", "abcdef0123456789abcdef0123456789abcdef0123456789")

    token = ensure_token()
    assert token == "abcdef0123456789abcdef0123456789abcdef0123456789"
    assert token_file.read_text(encoding="utf-8").strip() == token
