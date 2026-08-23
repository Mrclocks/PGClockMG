"""Cleanup HTTP endpoints: additive, fail-open, and never blocking a restore."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ENV_TEXT = (
    "SQLALCHEMY_DATABASE_URL=mysql+asyncmy://pg:pw@127.0.0.1:3306/pasarguard\n"
    "DB_PASSWORD=pw\n"
)

DUMP = """DROP TABLE IF EXISTS `users`;
CREATE TABLE `users` (`id` int NOT NULL, `username` varchar(64));
INSERT INTO `users` VALUES (1,'alice'),(2,'bob');
CREATE TABLE `node_user_usages` (`id` bigint NOT NULL, `used_traffic` bigint);
INSERT INTO `node_user_usages` VALUES (1,100),(2,200),(3,300);
"""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import app.config as config
    import app.services.upload as upload_mod
    from app.main import app as fastapi_app

    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(config, "UPLOAD_DIR", uploads, raising=False)
    monkeypatch.setattr(upload_mod, "UPLOAD_DIR", uploads, raising=False)
    monkeypatch.delenv("PG_CLEANUP_ENABLED", raising=False)
    from app.services.auth import get_token

    with TestClient(fastapi_app) as c:
        c.headers.update({"X-Auth-Token": get_token()})
        c.uploads = uploads  # type: ignore[attr-defined]
        yield c


def _stage(uploads: Path, upload_id: str = "up0000000001") -> str:
    d = uploads / upload_id
    d.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(d / "backup.zip", "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(".env", ENV_TEXT)
        zf.writestr("db_backup.sql", DUMP)
    return upload_id


def test_analyze_reports_rules(client):
    upload_id = _stage(client.uploads)
    r = client.get(f"/api/pasarguard/cleanup/analyze/{upload_id}")
    assert r.status_code == 200
    body = r.json()

    assert body["available"] is True
    assert body["removable_rows"] == 3
    by_id = {x["id"]: x for x in body["rules"]}
    assert by_id["node_traffic_history"]["rows"] == 3
    # every rule carries all three languages for the UI
    for rule in body["rules"]:
        for lang in ("en", "fa", "ru"):
            assert rule["label"][lang] and rule["description"][lang]
    print("OK: analyze endpoint reports per-rule numbers")


def test_analyze_unknown_upload_is_404(client):
    r = client.get("/api/pasarguard/cleanup/analyze/doesnotexist")
    assert r.status_code == 404
    print("OK: analyze 404s on unknown upload")


def test_analyze_never_500s_on_a_broken_archive(client, tmp_path):
    """A junk upload must degrade to 'no cleanup offered', not an error page."""
    d = client.uploads / "broken00001"
    d.mkdir(parents=True)
    (d / "backup.zip").write_bytes(b"this is not a zip file")

    r = client.get("/api/pasarguard/cleanup/analyze/broken00001")
    assert r.status_code == 200
    assert r.json()["available"] is False
    print("OK: broken archive degrades instead of erroring")


def test_apply_returns_new_upload_id(client):
    upload_id = _stage(client.uploads)
    r = client.post(
        "/api/pasarguard/cleanup",
        json={"upload_id": upload_id, "rule_ids": ["node_traffic_history"]},
    )
    assert r.status_code == 200
    body = r.json()

    assert body["applied"] is True
    assert body["upload_id"] != upload_id
    assert body["removed_rows"] == 3
    assert body["size_after"] <= body["size_before"]

    # the original upload is still there and still restorable
    assert (client.uploads / upload_id / "backup.zip").is_file()

    cleaned = client.uploads / body["upload_id"] / "backup.zip"
    assert cleaned.is_file()
    with zipfile.ZipFile(cleaned) as zf:
        sql = zf.read("db_backup.sql").decode()
    assert "INSERT INTO `node_user_usages`" not in sql
    assert "INSERT INTO `users` VALUES (1,'alice'),(2,'bob');" in sql
    print("OK: apply endpoint returns a usable new upload id")


def test_apply_with_no_rules_returns_original(client):
    upload_id = _stage(client.uploads)
    r = client.post("/api/pasarguard/cleanup", json={"upload_id": upload_id, "rule_ids": []})
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] is False
    assert body["upload_id"] == upload_id
    print("OK: apply with no rules hands back the original id")


def test_apply_unknown_upload_returns_original_not_error(client):
    r = client.post(
        "/api/pasarguard/cleanup",
        json={"upload_id": "missing00001", "rule_ids": ["node_traffic_history"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] is False
    assert body["upload_id"] == "missing00001"
    print("OK: apply on unknown upload degrades instead of erroring")


def test_kill_switch_disables_both_endpoints(client, monkeypatch):
    upload_id = _stage(client.uploads)
    monkeypatch.setenv("PG_CLEANUP_ENABLED", "0")

    analyze = client.get(f"/api/pasarguard/cleanup/analyze/{upload_id}").json()
    assert analyze["available"] is False and analyze["reason"] == "disabled"

    applied = client.post(
        "/api/pasarguard/cleanup",
        json={"upload_id": upload_id, "rule_ids": ["node_traffic_history"]},
    ).json()
    assert applied["applied"] is False
    assert applied["upload_id"] == upload_id
    print("OK: PG_CLEANUP_ENABLED=0 disables cleanup entirely")


def test_existing_restore_contract_unchanged(client):
    """The restore endpoint still takes only upload_id — cleanup added no coupling."""
    from app.models import PasarguardRestoreRequest

    fields = set(PasarguardRestoreRequest.model_fields)
    assert fields == {
        "upload_id",
        "force",
        "confirmed",
        "target_db",
        "accept_experimental",
        "disable_nodes_after_restore",
    }, fields
    print("OK: restore request contract unchanged")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
