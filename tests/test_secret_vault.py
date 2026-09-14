"""Server-side password vault: scoped autofill + migrate autopass, no /api/info leak."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.main import app as fastapi_app
from app.services import auth as auth_mod
from app.services import secret_vault
from app.services.auth import ensure_token, get_token
from app.services.env_migration import (
    extract_env_password_candidates,
    public_password_candidates,
)


ENV_TEXT = """
SQLALCHEMY_DATABASE_URL=postgresql+asyncpg://pguser:VaultSecret99@127.0.0.1:5432/pasarguard
POSTGRES_PASSWORD=VaultSecret99
"""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    token_file = tmp_path / ".access_token"
    monkeypatch.setattr(auth_mod, "TOKEN_FILE", token_file)
    auth_mod._cached_token = None
    auth_mod._LOGIN_HITS.clear()
    auth_mod._LOGIN_LOCK_UNTIL.clear()
    ensure_token()
    secret_vault.clear_scope(secret_vault.LIVE_PASARGUARD)
    secret_vault.clear_scope(secret_vault.LIVE_MARZBAN)
    secret_vault.clear_scope("upload:testupload1")
    with TestClient(fastapi_app) as c:
        yield c
    auth_mod._cached_token = None
    secret_vault.clear_scope(secret_vault.LIVE_PASARGUARD)
    secret_vault.clear_scope(secret_vault.LIVE_MARZBAN)
    secret_vault.clear_scope("upload:testupload1")


def test_vault_stores_and_returns_primary():
    cands = extract_env_password_candidates(ENV_TEXT, "postgresql")
    secret_vault.put_candidates(secret_vault.LIVE_PASARGUARD, cands, db_type="postgresql")
    assert secret_vault.get_primary(secret_vault.LIVE_PASARGUARD) == "VaultSecret99"
    stored = secret_vault.get_candidates(secret_vault.LIVE_PASARGUARD)
    assert any(c.get("value") == "VaultSecret99" for c in stored)


def test_apply_vault_passwords_fills_missing():
    cands = extract_env_password_candidates(ENV_TEXT, "postgresql")
    secret_vault.put_candidates(secret_vault.LIVE_PASARGUARD, cands, db_type="postgresql")
    secret_vault.put_candidates(
        "upload:testupload1",
        [{"key": "MYSQL_ROOT_PASSWORD", "value": "SrcPass", "used_for_migration": True}],
        db_type="mysql",
    )
    out = secret_vault.apply_vault_passwords({
        "upload_id": "testupload1",
        "source_panel": "marzban",
        "source_db_password": "",
        "target_db_password": None,
    })
    assert out["source_db_password"] == "SrcPass"
    assert out["target_db_password"] == "VaultSecret99"


def test_apply_vault_does_not_overwrite_explicit():
    cands = extract_env_password_candidates(ENV_TEXT, "postgresql")
    secret_vault.put_candidates(secret_vault.LIVE_PASARGUARD, cands, db_type="postgresql")
    out = secret_vault.apply_vault_passwords({
        "target_db_password": "UserTyped",
    })
    assert out["target_db_password"] == "UserTyped"


def test_credentials_endpoint_scoped_and_auth(client):
    headers = {"X-Auth-Token": get_token()}
    cands = extract_env_password_candidates(ENV_TEXT, "postgresql")
    secret_vault.put_candidates(secret_vault.LIVE_PASARGUARD, cands, db_type="postgresql")

    assert client.get("/api/credentials/candidates", params={"scope": "live:pasarguard"}).status_code == 401

    r = client.get(
        "/api/credentials/candidates",
        params={"scope": "live:pasarguard"},
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["server_held"] is True
    assert body["primary"] == "VaultSecret99"
    assert any(c.get("value") == "VaultSecret99" for c in body["candidates"])

    bad = client.get(
        "/api/credentials/candidates",
        params={"scope": "live:other"},
        headers=headers,
    )
    assert bad.status_code == 400


def test_info_still_scrubbed_while_vault_holds_secret(client):
    headers = {"X-Auth-Token": get_token()}
    cands = extract_env_password_candidates(ENV_TEXT, "postgresql")
    secret_vault.put_candidates(secret_vault.LIVE_PASARGUARD, cands, db_type="postgresql")
    public = public_password_candidates(cands)
    assert all("value" not in c for c in public)
    assert all(c.get("server_held") for c in public)

    r = client.get("/api/info", headers=headers)
    assert r.status_code == 200
    blob = r.text
    assert "VaultSecret99" not in blob
