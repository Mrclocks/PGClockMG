"""Tests for post-restore .env finalization (SSL, DB URL, pgAdmin)."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.env_migration import (
    detect_db_type_from_env,
    env_points_to_db,
    finalize_pasarguard_env_after_restore,
    sanitize_ssl_env,
    ssl_cert_files_exist,
    _resolve_ssl_cert_path,
)


def test_detect_db_type_sqlalchemy_beats_pgadmin():
    env = '\n'.join([
        'PGADMIN_EMAIL="admin@test.com"',
        'PGADMIN_PASSWORD="secret"',
        'SQLALCHEMY_DATABASE_URL="sqlite+aiosqlite:////var/lib/pasarguard/db.sqlite3"',
    ])
    assert detect_db_type_from_env(env) == "sqlite"
    print("OK: SQLALCHEMY beats pgAdmin for db type detection")


def test_finalize_sqlite_to_timescaledb_url():
    backup = '\n'.join([
        'SQLALCHEMY_DATABASE_URL="sqlite+aiosqlite:////var/lib/pasarguard/db.sqlite3"',
        'UVICORN_SSL_CERTFILE="/var/lib/pasarguard/certs/fullchain.pem"',
        'UVICORN_SSL_KEYFILE="/var/lib/pasarguard/certs/privkey.pem"',
        'TELEGRAM_API_TOKEN="tok"',
    ])
    install = '\n'.join([
        'SQLALCHEMY_DATABASE_URL="postgresql+asyncpg://pasarguard:live@127.0.0.1:6432/pasarguard"',
        'DB_USER="pasarguard"',
        'DB_NAME="pasarguard"',
        'DB_PASSWORD="live"',
        'UVICORN_PORT="8000"',
        'UVICORN_HOST="0.0.0.0"',
        'PGADMIN_EMAIL="admin@local"',
        'PGADMIN_PASSWORD="pgpass"',
    ])
    out = finalize_pasarguard_env_after_restore(
        backup, "timescaledb", "live", install,
        db_user="pasarguard", db_name="pasarguard",
    )
    assert env_points_to_db(out, "timescaledb")
    assert "sqlite+aiosqlite" not in out.lower()
    # Install URL host/port/user must be preserved (not rebuilt from sqlite .env)
    assert "pasarguard:live@127.0.0.1:6432/pasarguard" in out
    assert "TELEGRAM_API_TOKEN" in out
    assert 'DB_PASSWORD="live"' in out
    assert 'POSTGRES_PASSWORD="live"' in out
    # SSL paths from backup but files missing → stripped
    assert "UVICORN_SSL_CERTFILE" not in out
    print("OK: finalize timescaledb URL + strip missing SSL")


def test_finalize_collapses_duplicate_sqlite_urls():
    """Duplicate SQLALCHEMY lines (common in some panel .env files) must collapse to one.
    Replacing only the first left the panel on sqlite (last-wins) after convert."""
    import re
    from app.services.env_migration import _sqlalchemy_url_line_pattern

    # Synthetic fixture — never load a personal backup into the repo/tests.
    text = "\n".join([
        'SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite://///var/lib/pasarguard/db.sqlite3"',
        'SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite://///var/lib/pasarguard/db.sqlite3"',
        'SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite://///var/lib/pasarguard/db.sqlite3"',
        'UVICORN_PORT = 8880',
    ])
    before = len(re.findall(_sqlalchemy_url_line_pattern(), text))
    assert before >= 2, f"expected duplicate URLs in fixture, got {before}"

    for final_db, install_url in (
        ("timescaledb", "postgresql+asyncpg://pasarguard:Secret123@127.0.0.1:6432/pasarguard"),
        ("postgresql", "postgresql+asyncpg://pasarguard:Secret123@127.0.0.1:5432/pasarguard"),
        ("mysql", "mysql+asyncmy://pasarguard:Secret123@127.0.0.1:3306/pasarguard"),
        ("mariadb", "mariadb+asyncmy://pasarguard:Secret123@127.0.0.1:3306/pasarguard"),
    ):
        install = "\n".join([
            f'SQLALCHEMY_DATABASE_URL="{install_url}"',
            'DB_USER="pasarguard"',
            'DB_NAME="pasarguard"',
            'DB_PASSWORD="Secret123"',
        ])
        out = finalize_pasarguard_env_after_restore(
            text, final_db, "Secret123", install,
            db_user="pasarguard", db_name="pasarguard",
        )
        after = re.findall(_sqlalchemy_url_line_pattern(), out)
        assert len(after) == 1, f"{final_db}: must be 1 URL line, got {len(after)}: {after}"
        assert "sqlite" not in after[0].lower(), f"{final_db}: leftover sqlite URL"
        assert env_points_to_db(out, final_db), f"{final_db}: URL does not match engine"
    print(f"OK: collapsed {before} duplicate sqlite URLs for all server engines")


def test_finalize_prefers_install_url_over_sqlite_merge():
    backup = 'SQLALCHEMY_DATABASE_URL="sqlite+aiosqlite:////var/lib/pasarguard/db.sqlite3"\nFOO="1"\n'
    install = (
        'SQLALCHEMY_DATABASE_URL="postgresql+asyncpg://pasarguard:secret@127.0.0.1:6432/pasarguard"\n'
        'DB_USER="pasarguard"\nDB_PASSWORD="secret"\n'
    )
    out = finalize_pasarguard_env_after_restore(backup, "postgresql", "secret", install)
    assert "sqlite+aiosqlite" not in out.lower()
    assert "postgresql+asyncpg://pasarguard:secret@127.0.0.1:6432/pasarguard" in out
    print("OK: finalize keeps install postgresql URL")


def test_sanitize_ssl_keeps_valid_files():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        cert = base / "fullchain.pem"
        key = base / "privkey.pem"
        cert.write_text("cert", encoding="utf-8")
        key.write_text("key", encoding="utf-8")
        env = f'UVICORN_SSL_CERTFILE="{cert}"\nUVICORN_SSL_KEYFILE="{key}"\n'
        assert ssl_cert_files_exist(str(cert), str(key))
        cleaned = sanitize_ssl_env(env)
        assert "UVICORN_SSL_CERTFILE" in cleaned
        assert "UVICORN_SSL_KEYFILE" in cleaned
    print("OK: sanitize keeps valid SSL files")


def test_resolve_container_cert_path():
    with tempfile.TemporaryDirectory() as td:
        import app.services.env_migration as em

        old = em.PASARGUARD_DATA
        em.PASARGUARD_DATA = Path(td)
        try:
            certs = em.PASARGUARD_DATA / "certs"
            certs.mkdir()
            (certs / "fullchain.pem").write_text("x", encoding="utf-8")
            p = _resolve_ssl_cert_path("/var/lib/pasarguard/certs/fullchain.pem")
            assert p and p.is_file()
        finally:
            em.PASARGUARD_DATA = old
    print("OK: resolve container SSL path to host")


def test_strict_complete_hosts_fails_hard():
    """Partial hosts copy must abort when fail_hard (change-DB completeness)."""
    import tempfile
    import sqlite3
    from app.services.native_migration.adapters import SqliteReader, SqliteWriter, copy_tables_universal

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src.db"
        dst = Path(td) / "dst.db"
        for path in (src, dst):
            conn = sqlite3.connect(str(path))
            conn.executescript("""
                CREATE TABLE hosts (
                    id INTEGER PRIMARY KEY,
                    remark TEXT NOT NULL,
                    address TEXT NOT NULL,
                    security TEXT,
                    fingerprint TEXT,
                    priority INTEGER,
                    random_user_agent INTEGER,
                    use_sni_as_host INTEGER,
                    is_disabled INTEGER
                );
                CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
            """)
            if path == src:
                conn.execute(
                    "INSERT INTO hosts (remark, address, security, fingerprint, priority, random_user_agent, use_sni_as_host, is_disabled) "
                    "VALUES ('a','1.1.1.1','inbound_default','none',0,0,0,0)"
                )
                conn.execute(
                    "INSERT INTO hosts (remark, address, security, fingerprint, priority, random_user_agent, use_sni_as_host, is_disabled) "
                    "VALUES ('b','2.2.2.2','inbound_default','none',0,0,0,0)"
                )
                conn.execute("INSERT INTO users (username) VALUES ('u1')")
            conn.commit()
            conn.close()

        # Writer that rejects second host insert
        class FlakyWriter(SqliteWriter):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self._n = 0

            def insert(self, table, columns, values):
                if table == "hosts":
                    self._n += 1
                    if self._n > 1:
                        raise RuntimeError("forced host skip")
                return super().insert(table, columns, values)

        reader = SqliteReader(str(src))
        writer = FlakyWriter(str(dst))
        logs = []
        try:
            raised = False
            try:
                copy_tables_universal(reader, writer, logs.append, fail_hard=True)
            except RuntimeError as e:
                raised = True
                assert "hosts" in str(e).lower() or "incomplete" in str(e).lower()
            assert raised, "expected fail_hard on incomplete hosts"
        finally:
            reader.close()
            writer.close()
    print("OK: strict complete hosts fails hard")


if __name__ == "__main__":
    test_detect_db_type_sqlalchemy_beats_pgadmin()
    test_finalize_sqlite_to_timescaledb_url()
    test_finalize_prefers_install_url_over_sqlite_merge()
    test_sanitize_ssl_keeps_valid_files()
    test_resolve_container_cert_path()
    test_strict_complete_hosts_fails_hard()
    print("\nAll env_finalize tests passed")
