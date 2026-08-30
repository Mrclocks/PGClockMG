"""Tests for backup panel auth, settings, telegram caption, and stream listener."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest


def test_password_policy_and_session(tmp_path, monkeypatch):
    import app.services.backup_auth as auth

    monkeypatch.setattr(auth, "BACKUP_PASSWORD_FILE", tmp_path / ".password")
    monkeypatch.setattr(auth, "BACKUP_SECRET_FILE", tmp_path / ".session_secret")

    with pytest.raises(auth.PasswordPolicyError):
        auth.set_password("short")
    with pytest.raises(auth.PasswordPolicyError):
        auth.set_password("alllowercase1!")
    with pytest.raises(auth.PasswordPolicyError):
        auth.set_password("ALLUPPERCASE1!")
    with pytest.raises(auth.PasswordPolicyError):
        auth.set_password("NoSpecial1234")

    auth.set_password("StrongPass123!")
    assert auth.password_is_set()
    assert auth.check_password("StrongPass123!")
    assert not auth.check_password("WrongPass123!")

    cookie = auth.create_session_cookie()
    assert auth.session_cookie_valid(cookie)
    assert not auth.session_cookie_valid(cookie + "x")
    assert not auth.session_cookie_valid(None)
    print("OK: backup password policy + session")


def test_settings_mask_secrets(tmp_path, monkeypatch):
    import app.services.backup_settings as settings

    monkeypatch.setattr(settings, "BACKUP_SETTINGS_FILE", tmp_path / "settings.json")
    data = settings.update_settings({
        "telegram": {
            "enabled": True,
            "bot_token": "123456:ABC-DEF",
            "chat_id": "-1001",
            "proxy_password": "proxypass",
        }
    })
    pub = settings.public_settings(data)
    assert pub["telegram"]["bot_token"] == ""
    assert pub["telegram"]["bot_token_set"] is True
    assert pub["telegram"]["proxy_password"] == ""
    assert pub["telegram"]["chat_id"] == "-1001"
    # reload keeps secrets on disk
    loaded = settings.load_settings()
    assert loaded["telegram"]["bot_token"] == "123456:ABC-DEF"
    print("OK: settings mask secrets")


def test_telegram_caption_and_chunk_math():
    from app.services.backup_telegram import format_caption, human_size
    import math
    from app.config import TELEGRAM_BOT_MAX_BYTES

    text = format_caption(
        "U={users} N={nodes} S={size} {missing}",
        {"users": 10, "nodes": 2, "size": human_size(2048)},
    )
    assert "U=10" in text
    assert "N=2" in text
    assert "{missing}" in text
    size = 120 * 1024 * 1024
    parts = math.ceil(size / TELEGRAM_BOT_MAX_BYTES)
    assert parts >= 3
    print("OK: telegram caption + chunk sizing")


def test_stream_listener_lifecycle():
    from app.services import backup_stream as stream

    stream._LISTENERS.clear()
    info = stream.create_listener(label="test")
    token = info["token"]
    got = stream.get_listener(token)
    assert got and got["status"] == "listening"
    stream.mark_listener_consumed(token)
    assert stream.get_listener(token)["status"] == "consumed"
    print("OK: stream listener lifecycle")


def test_stream_receive_writes_zip(tmp_path, monkeypatch):
    import asyncio
    from app.services import backup_stream as stream

    monkeypatch.setattr(stream, "UPLOAD_DIR", tmp_path / "uploads")
    (tmp_path / "uploads").mkdir()
    stream._LISTENERS.clear()
    info = stream.create_listener()
    token = info["token"]

    payload = b"PK\x03\x04" + b"0" * 200  # not a real zip; size check only needs >= 64

    async def gen():
        yield payload[:50]
        yield payload[50:]

    async def _run():
        return await stream.receive_stream(
            token,
            gen(),
            filename="demo.zip",
            expected_size=len(payload),
        )

    result = asyncio.run(_run())
    assert result["ok"]
    upload_id = result["upload_id"]
    zip_path = tmp_path / "uploads" / upload_id / "backup.zip"
    assert zip_path.is_file()
    assert zip_path.stat().st_size == len(payload)
    st = stream.get_listener(token)
    assert st["status"] == "ready"
    print("OK: stream receive writes zip")


def test_backup_bundle_sqlite_layout(tmp_path, monkeypatch):
    """Build a sqlite full-bundle zip without a live PasarGuard install."""
    import sqlite3
    import app.services.backup_engine as eng

    pg_dir = tmp_path / "opt" / "pasarguard"
    pg_data = tmp_path / "var" / "lib" / "pasarguard"
    pg_dir.mkdir(parents=True)
    pg_data.mkdir(parents=True)
    (pg_dir / ".env").write_text(
        'SQLALCHEMY_DATABASE_URL="sqlite+aiosqlite:////var/lib/pasarguard/db.sqlite3"\n',
        encoding="utf-8",
    )
    (pg_dir / "docker-compose.yml").write_text("services:\n  pasarguard:\n    image: x\n", encoding="utf-8")
    db = pg_data / "db.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
    conn.execute("INSERT INTO users(username) VALUES ('a'), ('b')")
    conn.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE admins (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE inbounds (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE hosts (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE groups (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    (pg_data / "certs").mkdir()
    (pg_data / "certs" / "fullchain.pem").write_text("CERT", encoding="utf-8")
    (pg_data / "xray_config.json").write_text('{"inbounds":[]}', encoding="utf-8")
    (pg_data / "templates").mkdir()
    (pg_data / "templates" / "user.html").write_text("hi", encoding="utf-8")

    monkeypatch.setattr(eng, "PASARGUARD_DIR", pg_dir)
    monkeypatch.setattr(eng, "PASARGUARD_ENV", pg_dir / ".env")
    monkeypatch.setattr(eng, "PASARGUARD_DATA", pg_data)
    monkeypatch.setattr(eng, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(eng, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(eng, "BACKUP_JOBS_DIR", tmp_path / "jobs")
    (tmp_path / "backups").mkdir()
    (tmp_path / "work").mkdir()
    (tmp_path / "jobs").mkdir()

    monkeypatch.setattr(eng, "is_pasarguard_installed", lambda: True)
    monkeypatch.setattr(eng, "get_pasarguard_db_type", lambda: "sqlite")

    # settings write path
    import app.services.backup_settings as settings
    monkeypatch.setattr(settings, "BACKUP_SETTINGS_FILE", tmp_path / "settings.json")

    job = eng.create_backup_bundle(trigger="test")
    assert job["status"] == "success", job.get("error")
    path = eng.resolve_backup_path(job["backup_id"])
    assert path and path.is_file()
    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())
    assert ".env" in names
    assert "db.sqlite3" in names
    assert "pgclockmg-manifest.json" in names
    assert "certs/fullchain.pem" in names
    assert "templates/user.html" in names
    assert "xray_config.json" in names
    assert "var/lib/pasarguard/xray_config.json" in names
    assert "docker-compose.yml" in names
    assert job["manifest"]["counts"]["users"] == 2
    # Restore discoverer must find sqlite + env in this layout
    from app.services.pg_restore import discover_backup_artifacts, _find_env
    import tempfile, shutil
    extract = Path(tempfile.mkdtemp(prefix="pg-bak-check-"))
    try:
        with zipfile.ZipFile(path, "r") as zf:
            zf.extractall(extract)
        assert _find_env(extract) is not None
        art = discover_backup_artifacts(extract, env_db="sqlite")
        assert art.get("layout") == "sqlite_file"
        assert art.get("sqlite_path")
    finally:
        shutil.rmtree(extract, ignore_errors=True)
    print("OK: sqlite full-bundle layout + restore-compatible paths")


def test_resolve_sqlite_path_from_env(tmp_path, monkeypatch):
    import app.services.backup_engine as eng
    custom = tmp_path / "custom.sqlite3"
    custom.write_bytes(b"SQLite format 3\x00" + b"\x00" * 80)
    env = f'SQLALCHEMY_DATABASE_URL="sqlite+aiosqlite:///{custom.as_posix()}"\n'
    monkeypatch.setattr(eng, "PASARGUARD_DATA", tmp_path / "missing")
    assert eng._resolve_sqlite_path(env) == custom
    print("OK: sqlite path from .env")


def test_sqlite_url_path_variants_no_uri_authority(tmp_path, monkeypatch):
    """All SQLite URL slash forms resolve to a normal path and open read-only."""
    import sqlite3
    from app.services.env_migration import sqlite_fs_path_from_url, parse_sqlalchemy_url
    import app.services.backup_engine as eng

    db = tmp_path / "db.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE users (id INTEGER)")
    conn.execute("INSERT INTO users VALUES (1)")
    conn.commit()
    conn.close()

    abs_posix = db.as_posix()
    assert abs_posix.startswith("/")
    urls = [
        f"sqlite+aiosqlite:////{abs_posix.lstrip('/')}",
        f"sqlite:////{abs_posix.lstrip('/')}",
        f"sqlite+aiosqlite://///{abs_posix.lstrip('/')}",  # 5-slash rebuild bug
        f"sqlite:///{abs_posix.lstrip('/')}",  # 3-slash absolute-looking
        f"sqlite+aiosqlite://{abs_posix.lstrip('/')}",  # host-style //var…
        f"sqlite+aiosqlite:////{abs_posix.lstrip('/')}?check_same_thread=false",
    ]
    for url in urls:
        parsed = sqlite_fs_path_from_url(url)
        assert parsed == abs_posix, (url, parsed)
        assert not parsed.startswith("//")
        assert parse_sqlalchemy_url(url)["sqlite_path"] == abs_posix

        monkeypatch.setattr(eng, "PASARGUARD_DATA", tmp_path / "missing-data")
        monkeypatch.setattr(eng, "PASARGUARD_DIR", tmp_path / "missing-dir")
        env = f'SQLALCHEMY_DATABASE_URL="{url}"\n'
        resolved = eng._resolve_sqlite_path(env)
        assert resolved == db
        assert not str(resolved).startswith("//")

        ro = eng._sqlite_connect_ro(resolved)
        try:
            assert ro.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        finally:
            ro.close()

        dest = tmp_path / f"out-{abs(hash(url)) % 10_000}.sqlite3"
        job = {"logs": []}
        eng._dump_sqlite(dest, job, env_text=env)
        assert dest.is_file() and dest.stat().st_size >= 64
        assert "invalid uri authority" not in " ".join(job.get("logs") or []).lower()

    # Double-slash Path must still open (regression for screenshot error)
    weird = eng._normalize_sqlite_fs_path(Path("//" + abs_posix.lstrip("/")))
    assert str(weird) == abs_posix
    ro = eng._sqlite_connect_ro(Path("//" + abs_posix.lstrip("/")))
    ro.close()
    print("OK: sqlite URL variants + URI authority regression")


def test_safe_db_ident_rejects_path_like_values():
    from app.services.backup_engine import _safe_db_ident
    assert _safe_db_ident("pasarguard", fallback="x") == "pasarguard"
    assert _safe_db_ident(None, fallback="pasarguard") == "pasarguard"
    for bad in ("/var/lib/pasarguard/db.sqlite3", "file:///tmp/x", "//var/lib/x", "a b"):
        try:
            _safe_db_ident(bad)
            raise AssertionError(f"expected reject for {bad!r}")
        except RuntimeError:
            pass
    print("OK: safe db ident rejects path-like values")


def test_stream_push_receive_roundtrip(tmp_path, monkeypatch):
    """Chunked receive reconstructs the exact zip bytes."""
    import asyncio
    from app.services import backup_stream as stream

    monkeypatch.setattr(stream, "UPLOAD_DIR", tmp_path / "uploads")
    (tmp_path / "uploads").mkdir()
    stream._LISTENERS.clear()

    src = tmp_path / "pgclockmg-test.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr(".env", 'SQLALCHEMY_DATABASE_URL="sqlite+aiosqlite:////var/lib/pasarguard/db.sqlite3"\n')
        zf.writestr("db.sqlite3", b"SQLite format 3\x00" + b"\x00" * 200)

    payload = src.read_bytes()
    digest = stream._file_sha256(src)

    async def gen():
        step = 32 * 1024
        for i in range(0, len(payload), step):
            yield payload[i:i + step]

    token = stream.create_listener()["token"]
    received = asyncio.run(stream.receive_stream(
        token, gen(), filename=src.name, expected_size=len(payload),
        expected_sha256=digest,
    ))
    assert received["ok"]
    out = tmp_path / "uploads" / received["upload_id"] / "backup.zip"
    assert out.is_file()
    assert out.read_bytes() == payload
    assert received["sha256"] == digest
    print("OK: stream receive roundtrip chunked")


def test_backup_api_setup_login(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app.services.backup_auth as auth
    import app.services.backup_settings as settings
    import app.backup_main as bm

    monkeypatch.setattr(auth, "BACKUP_PASSWORD_FILE", tmp_path / ".password")
    monkeypatch.setattr(auth, "BACKUP_SECRET_FILE", tmp_path / ".session_secret")
    monkeypatch.setattr(settings, "BACKUP_SETTINGS_FILE", tmp_path / "settings.json")

    # Avoid scheduler/docker side effects
    monkeypatch.setattr(bm, "start_scheduler", lambda: None)
    monkeypatch.setattr(bm, "stop_scheduler", lambda: None)
    monkeypatch.setattr(bm, "is_pasarguard_installed", lambda: False)
    monkeypatch.setattr(bm, "get_system_status", lambda: {
        "pasarguard": False, "pasarguard_db": None, "docker": False, "resources": {},
    })
    monkeypatch.setattr(bm, "list_backup_files", lambda: [])

    client = TestClient(bm.app)
    st = client.get("/api/setup/status")
    assert st.status_code == 200
    assert st.json()["password_set"] is False

    bad = client.post("/api/setup/password", json={
        "password": "alllowercase1!", "password_confirm": "alllowercase1!",
    })
    assert bad.status_code == 400

    mismatch = client.post("/api/setup/password", json={
        "password": "StrongPass123!", "password_confirm": "StrongPass123?",
    })
    assert mismatch.status_code == 400

    ok = client.post("/api/setup/password", json={
        "password": "StrongPass123!", "password_confirm": "StrongPass123!",
    })
    assert ok.status_code == 200
    assert "pgclockmg_backup_session" in ok.cookies

    dash = client.get("/api/dashboard")
    assert dash.status_code == 200

    # new client without cookie
    client2 = TestClient(bm.app)
    assert client2.get("/api/dashboard").status_code == 401
    login = client2.post("/api/login", json={"password": "StrongPass123!"})
    assert login.status_code == 200
    print("OK: backup API setup + login")


if __name__ == "__main__":
    test_password_policy_and_session.__wrapped__ if False else None
    # simple runner without pytest fixtures for a couple tests
    import tempfile
    from unittest.mock import patch
    # prefer pytest
    raise SystemExit("run with pytest")


def test_setup_token_length_mismatch_no_500(tmp_path, monkeypatch):
    """Unequal-length tokens must not crash (Python 3.10 compare_digest)."""
    monkeypatch.setenv("PG_BACKUP_HOME", str(tmp_path))
    monkeypatch.setenv("PG_MIGRATOR_HOME", str(tmp_path))
    import importlib
    import app.config as cfg
    importlib.reload(cfg)
    from app.services import backup_auth
    importlib.reload(backup_auth)
    from app import backup_main
    importlib.reload(backup_main)
    from fastapi.testclient import TestClient

    tok = backup_auth.issue_setup_token()
    client = TestClient(backup_main.app)
    r = client.post(
        "/api/setup/password",
        json={
            "password": "BackupTest1!ab",
            "password_confirm": "BackupTest1!ab",
            "setup_token": "SHORT",
        },
    )
    assert r.status_code == 403, r.text
    # Empty leftover password file + valid token must succeed
    cfg.BACKUP_PASSWORD_FILE.write_text("", encoding="utf-8")
    r2 = client.post(
        "/api/setup/password",
        json={
            "password": "BackupTest1!ab",
            "password_confirm": "BackupTest1!ab",
            "setup_token": tok,
        },
    )
    assert r2.status_code == 200, r2.text
    print("OK: setup token mismatch + empty password file")


def _prep_pg_tree(tmp_path, env_url: str):
    pg_dir = tmp_path / "opt" / "pasarguard"
    pg_data = tmp_path / "var" / "lib" / "pasarguard"
    pg_dir.mkdir(parents=True)
    pg_data.mkdir(parents=True)
    (pg_dir / ".env").write_text(f'SQLALCHEMY_DATABASE_URL="{env_url}"\nAPP_VERSION="1.2.3"\n', encoding="utf-8")
    (pg_dir / "docker-compose.yml").write_text("services:\n  pasarguard:\n    image: x\n", encoding="utf-8")
    (pg_data / "certs").mkdir()
    (pg_data / "certs" / "fullchain.pem").write_text("CERT", encoding="utf-8")
    (pg_data / "xray_config.json").write_text('{"inbounds":[]}', encoding="utf-8")
    (pg_data / "templates").mkdir()
    (pg_data / "templates" / "user.html").write_text("hi", encoding="utf-8")
    return pg_dir, pg_data


def _assert_sql_bundle(path, db_type: str, *, expect_globals: bool = False):
    import json
    import zipfile
    from app.services.pg_restore import discover_backup_artifacts, _find_env
    import tempfile, shutil

    assert path and path.is_file()
    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("pgclockmg-manifest.json"))
    assert ".env" in names
    assert "db_backup.sql" in names
    assert "pgclockmg-manifest.json" in names
    assert "certs/fullchain.pem" in names
    assert "docker-compose.yml" in names
    assert "xray_config.json" in names
    assert manifest["db_type"] == db_type
    assert manifest["format"] == "pgclockmg-full-bundle"
    if expect_globals:
        assert "globals.sql" in names

    extract = Path(tempfile.mkdtemp(prefix="pg-bak-sql-"))
    try:
        with zipfile.ZipFile(path, "r") as zf:
            zf.extractall(extract)
        assert _find_env(extract) is not None
        art = discover_backup_artifacts(extract, env_db=db_type)
        assert art.get("layout") == "single"
        assert art.get("dump_path")
        assert Path(art["dump_path"]).name == "db_backup.sql"
    finally:
        shutil.rmtree(extract, ignore_errors=True)


def test_backup_bundle_all_sql_engines(tmp_path, monkeypatch):
    """Postgres/Timescale/MySQL/MariaDB dumps produce restore-compatible full bundles."""
    import app.services.backup_engine as eng

    cases = [
        ("postgresql", "postgresql+asyncpg://pasarguard:x@db:5432/pasarguard", True),
        ("timescaledb", "postgresql+asyncpg://pasarguard:x@db:5432/pasarguard", True),
        ("mysql", "mysql+aiomysql://pasarguard:x@db:3306/pasarguard", False),
        ("mariadb", "mysql+aiomysql://pasarguard:x@db:3306/pasarguard", False),
    ]

    for db_type, url, with_globals in cases:
        case_root = tmp_path / db_type
        case_root.mkdir()
        pg_dir, pg_data = _prep_pg_tree(case_root, url)

        monkeypatch.setattr(eng, "PASARGUARD_DIR", pg_dir)
        monkeypatch.setattr(eng, "PASARGUARD_ENV", pg_dir / ".env")
        monkeypatch.setattr(eng, "PASARGUARD_DATA", pg_data)
        monkeypatch.setattr(eng, "BACKUP_DIR", case_root / "backups")
        monkeypatch.setattr(eng, "WORK_DIR", case_root / "work")
        monkeypatch.setattr(eng, "BACKUP_JOBS_DIR", case_root / "jobs")
        (case_root / "backups").mkdir()
        (case_root / "work").mkdir()
        (case_root / "jobs").mkdir()
        monkeypatch.setattr(eng, "is_pasarguard_installed", lambda: True)
        monkeypatch.setattr(eng, "get_pasarguard_db_type", lambda dt=db_type: dt)
        monkeypatch.setattr(eng, "live_panel_stats", lambda: {"counts": {"users": 3, "nodes": 1, "admins": 1, "inbounds": 2, "hosts": 0, "groups": 0}})

        import app.services.backup_settings as settings
        monkeypatch.setattr(settings, "BACKUP_SETTINGS_FILE", case_root / "settings.json")

        def _fake_pg(dt, dest, job, *, expect_g=with_globals):
            dest.write_text(
                "--\n-- PostgreSQL database dump\n--\n"
                "SET statement_timeout = 0;\n"
                "CREATE TABLE users(id int);\n"
                "INSERT INTO users VALUES (1),(2),(3);\n",
                encoding="utf-8",
            )
            if expect_g:
                (dest.parent / "globals.sql").write_text(
                    "--\n-- PostgreSQL database cluster dump\n--\n"
                    "CREATE ROLE pasarguard WITH LOGIN PASSWORD 'x';\n",
                    encoding="utf-8",
                )
            job.setdefault("logs", []).append(f"fake dump {dt}")

        def _fake_mysql(dt, dest, job):
            dest.write_text(
                "-- MySQL dump 10.13\n"
                "/*!40101 SET NAMES utf8mb4 */;\n"
                "CREATE TABLE users(id int);\n"
                "INSERT INTO users VALUES (1),(2),(3);\n",
                encoding="utf-8",
            )
            job.setdefault("logs", []).append(f"fake dump {dt}")

        if db_type in ("postgresql", "timescaledb"):
            monkeypatch.setattr(eng, "_dump_postgres", _fake_pg)
        else:
            monkeypatch.setattr(eng, "_dump_mysql", _fake_mysql)

        job = eng.create_backup_bundle(trigger="test")
        assert job["status"] == "success", (db_type, job.get("error"), job.get("logs"))
        path = eng.resolve_backup_path(job["backup_id"])
        _assert_sql_bundle(path, db_type, expect_globals=with_globals)
        assert job["manifest"]["counts"]["users"] == 3
        assert f"pgclockmg-{db_type}-" in (job.get("filename") or "")

    print("OK: postgresql/timescaledb/mysql/mariadb full-bundle layouts")


def test_list_backups_api_includes_path(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app.services.backup_auth as auth
    import app.services.backup_settings as settings
    import app.backup_main as bm
    from app.config import BACKUP_DIR

    monkeypatch.setattr(auth, "BACKUP_PASSWORD_FILE", tmp_path / ".password")
    monkeypatch.setattr(auth, "BACKUP_SECRET_FILE", tmp_path / ".session_secret")
    monkeypatch.setattr(settings, "BACKUP_SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(bm, "start_scheduler", lambda: None)
    monkeypatch.setattr(bm, "stop_scheduler", lambda: None)
    monkeypatch.setattr(bm, "is_pasarguard_installed", lambda: False)
    monkeypatch.setattr(bm, "list_backup_files", lambda: [{"id": "x", "filename": "x.zip"}])

    auth.set_password("StrongPass123!")
    client = TestClient(bm.app)
    login = client.post("/api/login", json={"password": "StrongPass123!"})
    assert login.status_code == 200
    r = client.get("/api/backups")
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert data.get("backups_path") == str(BACKUP_DIR)
    print("OK: /api/backups returns backups_path")
