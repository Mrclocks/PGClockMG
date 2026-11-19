"""marzban.sh ``backup`` archives: tar.gz (Telegram ``backup_part_aa.tar.gz``),
often renamed or re-zipped as ``.zip``, holding a MariaDB dump plus a stale
pre-switch ``marzban_data/db.sqlite3``. The wizard must detect MariaDB."""

from __future__ import annotations

import io
import sqlite3
import tarfile
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ENV = (
    '# SQLALCHEMY_DATABASE_URL = "sqlite:////var/lib/marzban/db.sqlite3"\n'
    'SQLALCHEMY_DATABASE_URL = "mysql+pymysql://root:secretpw@127.0.0.1:3306/marzban"\n'
    "MYSQL_ROOT_PASSWORD = secretpw\n"
)
DUMP = (
    "CREATE DATABASE marzban;\nUSE `marzban`;\n"
    "CREATE TABLE `admins` (`id` int);\nINSERT INTO `admins` VALUES (1);\n"
    "CREATE TABLE `users` (`id` int);\nINSERT INTO `users` VALUES (1),(2),(3);\n"
)


def _stale_sqlite(tmp_path: Path) -> bytes:
    p = tmp_path / "stale.sqlite3"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT)")
    con.execute("INSERT INTO users(username) VALUES ('old')")
    con.commit()
    con.close()
    return p.read_bytes()


def _tar_gz(files: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, raw in files.items():
            data = raw if isinstance(raw, bytes) else raw.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _marzban_sh_backup(tmp_path: Path) -> bytes:
    return _tar_gz({
        "./db_backup.sql": DUMP,
        "./.env": ENV,
        "./docker-compose.yml": "services:\n  marzban:\n    image: gozargah/marzban\n",
        "./marzban_data/db.sqlite3": _stale_sqlite(tmp_path),
        "./marzban_data/xray_config.json": '{"inbounds": []}',
    })


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("PG_MIGRATOR_HOME", str(tmp_path))
    import importlib
    import app.config as cfg
    from app.services import auth as auth_mod

    importlib.reload(cfg)
    monkeypatch.setattr(auth_mod, "TOKEN_FILE", tmp_path / ".access_token")
    auth_mod._cached_token = None
    auth_mod.ensure_token()
    token = auth_mod.get_token()
    import app.main as main_mod

    importlib.reload(main_mod)
    return TestClient(main_mod.app), token


FORM = {"slot": "bundle_zip", "panel_id": "marzban", "marzban_mode": "fresh"}


def _assert_mariadb_bundle(bs: dict):
    assert bs["complete"] is True, bs
    a = bs["analysis"]
    assert a["detected_source_db"] == "mysql"
    assert a["paths"]["sql"].endswith("db_backup.sql")
    assert a["mysql_password_found"] is True


def _wrap_zip(name: str, payload: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, payload)
    return buf.getvalue()


@pytest.mark.parametrize("filename,wrap", [
    ("backup_part_aa.tar.gz", None),          # as sent by marzban.sh
    ("backup_part_aa.zip", None),             # renamed so the picker accepts it
    ("backup_part_aa.zip", "backup_part_aa"),  # re-zipped by the user / a bot
    ("backup.zip", "backup_20260101.tar.gz"),
])
def test_single_upload_detects_mariadb_over_stale_sqlite(tmp_path, monkeypatch, filename, wrap):
    payload = _marzban_sh_backup(tmp_path)
    if wrap:
        payload = _wrap_zip(wrap, payload)
    client, token = _client(tmp_path, monkeypatch)
    with client:
        r = client.post(
            "/api/upload",
            headers={"X-Auth-Token": token},
            data=FORM,
            files={"file": (filename, payload, "application/octet-stream")},
        )
        assert r.status_code == 200, r.text
        _assert_mariadb_bundle(r.json()["bundle_status"])


def test_parts_endpoint_single_and_split_tar(tmp_path, monkeypatch):
    payload = _marzban_sh_backup(tmp_path)
    half = len(payload) // 2
    client, token = _client(tmp_path, monkeypatch)
    with client:
        # Frontend routes part-looking names through /api/upload-parts.
        one = client.post(
            "/api/upload-parts",
            headers={"X-Auth-Token": token},
            data=FORM,
            files=[("files", ("backup_part_aa.zip", payload, "application/zip"))],
        )
        assert one.status_code == 200, one.text
        _assert_mariadb_bundle(one.json()["bundle_status"])

        two = client.post(
            "/api/upload-parts",
            headers={"X-Auth-Token": token},
            data=FORM,
            files=[
                ("files", ("backup_part_ab.tar.gz", payload[half:], "application/gzip")),
                ("files", ("backup_part_aa.tar.gz", payload[:half], "application/gzip")),
            ],
        )
        assert two.status_code == 200, two.text
        body = two.json()
        assert body["assembled_from_parts"] is True
        _assert_mariadb_bundle(body["bundle_status"])


def test_tar_rejects_path_traversal_and_skips_links(tmp_path):
    from app.services.archive_guard import safe_extract_archive

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        link = tarfile.TarInfo("evil-link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)
        ok = tarfile.TarInfo("ok.txt")
        ok.size = 2
        tf.addfile(ok, io.BytesIO(b"hi"))
    good = tmp_path / "good.tar.gz"
    good.write_bytes(buf.getvalue())
    safe_extract_archive(good, tmp_path / "out")
    assert (tmp_path / "out" / "ok.txt").read_bytes() == b"hi"
    assert not (tmp_path / "out" / "evil-link").exists()

    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(_tar_gz({"../escape.txt": "x"}))
    with pytest.raises(ValueError, match="Unsafe tar entry"):
        safe_extract_archive(bad, tmp_path / "out2")
    assert not (tmp_path / "escape.txt").exists()

