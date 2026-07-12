"""Tests for native cross-DB migration helpers."""

import sys
import sqlite3
import tempfile
import os
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_build_local_alembic_url():
    from app.services.pasarguard_ops import build_local_alembic_url

    params = {
        "target_db": "postgresql",
        "target_db_user": "pasarguard",
        "target_db_password": "secret",
        "target_db_name": "pasarguard",
        "target_db_host": "127.0.0.1",
        "target_db_port": "6432",
    }
    url = build_local_alembic_url(params)
    assert ":5432/" in url
    assert "pasarguard:secret@127.0.0.1:5432/pasarguard" in url
    print("OK: build_local_alembic_url")


def test_sqlite_column_intersection():
    from app.services.native_migration.sqlite_pg import _sqlite_columns, SKIP_TABLES, TABLE_ORDER

    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, status TEXT)")
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        conn.execute("INSERT INTO alembic_version VALUES ('2b231de97dc3')")
        conn.commit()
        cols = _sqlite_columns(conn, "users")
        assert "username" in cols
        assert "alembic_version" in SKIP_TABLES
        assert "users" in TABLE_ORDER
        conn.close()
        print("OK: sqlite column helpers")
    finally:
        os.unlink(path)


def test_native_migration_import():
    from app.services.native_migration import run_native_cross_db_migration
    from app.services.migrators.marzban import MarzbanMigrator
    assert run_native_cross_db_migration and MarzbanMigrator
    print("OK: native migration imports")


if __name__ == "__main__":
    test_build_local_alembic_url()
    test_sqlite_column_intersection()
    test_native_migration_import()
    print("\nAll native migration tests passed.")
