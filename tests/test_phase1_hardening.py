"""Phase-1 low-risk hardening: secret scrubbing, docs off, login throttle, headers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.main import app as fastapi_app
from app.services import auth as auth_mod
from app.services.auth import COOKIE_NAME, ensure_token, get_token
from app.services.env_migration import (
    extract_env_password_candidates,
    extract_env_summary,
    public_env_summary,
    public_password_candidates,
)


ENV_TEXT = """
SQLALCHEMY_DATABASE_URL=postgresql+asyncpg://pguser:SuperSecret@127.0.0.1:5432/pasarguard
POSTGRES_PASSWORD=SuperSecret
MYSQL_ROOT_PASSWORD=MysqlSecret
UVICORN_PORT=8000
"""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    token_file = tmp_path / ".access_token"
    monkeypatch.setattr(auth_mod, "TOKEN_FILE", token_file)
    auth_mod._cached_token = None
    auth_mod._LOGIN_HITS.clear()
    auth_mod._LOGIN_LOCK_UNTIL.clear()
    ensure_token()
    with TestClient(fastapi_app) as c:
        yield c
    auth_mod._cached_token = None
    auth_mod._LOGIN_HITS.clear()
    auth_mod._LOGIN_LOCK_UNTIL.clear()


def test_public_env_summary_strips_secrets():
    summary = extract_env_summary(ENV_TEXT)
    assert summary["db_password"] == "SuperSecret"
    assert summary["mysql_password"] == "MysqlSecret"
    assert summary["postgres_password"] == "SuperSecret"
    public = public_env_summary(summary)
    assert "db_password" not in public
    assert "mysql_password" not in public
    assert "postgres_password" not in public
    assert public["has_password"] is True
    assert public["db_user"] == "pguser"


def test_public_password_candidates_strip_values():
    cands = extract_env_password_candidates(ENV_TEXT, "postgresql")
    assert cands and cands[0]["value"] == "SuperSecret"
    public = public_password_candidates(cands)
    assert public and "value" not in public[0]
    assert public[0]["masked"]
    assert public[0]["key"]
    assert public[0]["server_held"] is True


def test_openapi_docs_disabled(client):
    headers = {"X-Auth-Token": get_token()}
    assert client.get("/docs", headers=headers).status_code == 404
    assert client.get("/redoc", headers=headers).status_code == 404
    assert client.get("/openapi.json", headers=headers).status_code == 404


def test_security_headers_and_no_secret_leak_in_info(client):
    r = client.get("/api/info", headers={"X-Auth-Token": get_token()})
    assert r.status_code == 200
    assert r.headers.get("referrer-policy") == "no-referrer"
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    system = r.json().get("system") or {}
    env = system.get("pasarguard_env")
    if env:
        assert "db_password" not in env
        assert "mysql_password" not in env
        assert "postgres_password" not in env
    for key in ("pasarguard_password_candidates", "marzban_password_candidates"):
        for item in system.get(key) or []:
            assert "value" not in item


def test_login_throttle_helpers():
    auth_mod._LOGIN_HITS.clear()
    auth_mod._LOGIN_LOCK_UNTIL.clear()
    for _ in range(3):
        auth_mod.record_login_failure("phase1-ip", max_hits=3, window_sec=60, lock_sec=60)
    assert auth_mod.login_is_throttled("phase1-ip", max_hits=3, window_sec=60, lock_sec=60)
    auth_mod.clear_login_failures("phase1-ip")
    assert not auth_mod.login_is_throttled("phase1-ip", max_hits=3, window_sec=60, lock_sec=60)


def test_login_endpoint_returns_429_when_throttled(client):
    auth_mod._LOGIN_HITS.clear()
    auth_mod._LOGIN_LOCK_UNTIL.clear()
    for _ in range(8):
        auth_mod.record_login_failure("testclient")
    r = client.get("/login", params={"token": "0" * 48})
    assert r.status_code == 429
