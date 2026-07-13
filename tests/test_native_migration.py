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
    from app.services.native_migration.copy_core import (
        sqlite_columns, SKIP_TABLES, TABLE_ORDER,
    )

    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, status TEXT)")
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        conn.execute("INSERT INTO alembic_version VALUES ('2b231de97dc3')")
        conn.commit()
        cols = sqlite_columns(conn, "users")
        assert "username" in cols
        assert "alembic_version" in SKIP_TABLES
        assert "exclude_inbounds_association" in TABLE_ORDER
        assert "exclude_inbounds_association" not in SKIP_TABLES
        assert "users" in TABLE_ORDER
        conn.close()
        print("OK: sqlite column helpers")
    finally:
        os.unlink(path)


def test_migration_strategy_matrix():
    from app.services.native_migration import migration_strategy

    assert migration_strategy("sqlite", "postgresql") == "two_phase"
    assert migration_strategy("sqlite", "timescaledb") == "two_phase"
    assert migration_strategy("sqlite", "mysql") == "two_phase"
    assert migration_strategy("mysql", "postgresql") == "two_phase"
    assert migration_strategy("postgresql", "timescaledb") == "two_phase"
    assert migration_strategy("postgresql", "sqlite") == "two_phase"
    assert migration_strategy("sqlite", "sqlite") == "same_db"
    assert migration_strategy("unknown", "postgresql") == "unsupported"
    print("OK: migration strategy matrix")


def test_read_alembic_from_sql_dump():
    from app.services.native_migration.source_version import read_alembic_version_from_sql_dump

    sql = "INSERT INTO `alembic_version` VALUES ('2b231de97dc3');"
    assert read_alembic_version_from_sql_dump(sql) == "2b231de97dc3"
    print("OK: alembic from sql dump")


def test_convert_bool_values():
    from app.services.native_migration.copy_core import convert_value

    assert convert_value("admins", "is_sudo", 1) is True
    assert convert_value("admins", "is_sudo", 0) is False
    assert convert_value("hosts", "fingerprint", None) == "none"
    print("OK: convert_bool_values")


def test_copy_sqlite_to_sqlite_associations(tmp_path=None):
    """Offline head→head style copy including association tables + fail_hard."""
    import tempfile
    from app.services.native_migration.adapters import (
        create_reader, create_writer, copy_tables_universal,
    )
    from app.services.native_migration.copy_core import TABLE_ORDER

    fd1, src = tempfile.mkstemp(suffix=".sqlite3")
    fd2, dst = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd1)
    os.close(fd2)
    try:
        sc = sqlite3.connect(src)
        sc.executescript(
            """
            CREATE TABLE admins (id INTEGER PRIMARY KEY, username TEXT, is_sudo INTEGER);
            INSERT INTO admins VALUES (1, 'admin', 1);
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, enable INTEGER);
            INSERT INTO users VALUES (1, 'u1', 1);
            CREATE TABLE inbounds (id INTEGER PRIMARY KEY, tag TEXT);
            INSERT INTO inbounds VALUES (1, 'in1');
            CREATE TABLE exclude_inbounds_association (user_id INTEGER, inbound_id INTEGER);
            INSERT INTO exclude_inbounds_association VALUES (1, 1);
            CREATE TABLE template_inbounds_association (user_template_id INTEGER, inbound_id INTEGER);
            CREATE TABLE alembic_version (version_num VARCHAR(32));
            INSERT INTO alembic_version VALUES ('abc');
            """
        )
        sc.commit()
        sc.close()

        dc = sqlite3.connect(dst)
        dc.executescript(
            """
            CREATE TABLE admins (id INTEGER PRIMARY KEY, username TEXT, is_sudo INTEGER);
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, enable INTEGER);
            CREATE TABLE inbounds (id INTEGER PRIMARY KEY, tag TEXT);
            CREATE TABLE exclude_inbounds_association (user_id INTEGER, inbound_id INTEGER);
            CREATE TABLE template_inbounds_association (user_template_id INTEGER, inbound_id INTEGER);
            CREATE TABLE alembic_version (version_num VARCHAR(32));
            """
        )
        dc.commit()
        dc.close()

        assert "exclude_inbounds_association" in TABLE_ORDER
        reader = create_reader("sqlite", src, {})
        writer = create_writer("sqlite", {"sqlite_path": dst}, dst)
        logs = []
        try:
            stats = copy_tables_universal(reader, writer, logs.append, fail_hard=True)
        finally:
            reader.close()
            writer.close()

        assert stats.get("users") == 1
        assert stats.get("admins") == 1
        assert stats.get("exclude_inbounds_association") == 1

        # fail_hard: empty users copy must raise
        fd3, empty_src = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd3)
        try:
            esc = sqlite3.connect(empty_src)
            esc.execute("CREATE TABLE users (id INTEGER, username TEXT)")
            esc.execute("CREATE TABLE admins (id INTEGER, username TEXT)")
            esc.execute("INSERT INTO users VALUES (1, 'x')")
            esc.commit()
            esc.close()
            # destination has schema but we will fail because... actually source has users
            # Create dest with schema, copy should work. For fail: source has users, dest write fails all.
            # Simpler: source has users, dest missing users table → skip → fail_hard
            fd4, bad_dst = tempfile.mkstemp(suffix=".sqlite3")
            os.close(fd4)
            bdc = sqlite3.connect(bad_dst)
            bdc.execute("CREATE TABLE admins (id INTEGER, username TEXT)")
            bdc.commit()
            bdc.close()
            r2 = create_reader("sqlite", empty_src, {})
            w2 = create_writer("sqlite", {"sqlite_path": bad_dst}, bad_dst)
            try:
                raised = False
                try:
                    copy_tables_universal(r2, w2, logs.append, fail_hard=True)
                except RuntimeError as exc:
                    raised = True
                    assert "users" in str(exc)
                assert raised, "expected fail_hard RuntimeError"
            finally:
                r2.close()
                w2.close()
            os.unlink(bad_dst)
        finally:
            os.unlink(empty_src)

        print("OK: sqlite to sqlite associations + fail_hard")
    finally:
        os.unlink(src)
        os.unlink(dst)


def test_sanitize_env_text_for_docker():
    from app.services.pasarguard_ops import sanitize_env_text_for_docker

    raw = 'UVICORN_HOST = "0.0.0.0"\nDB_PASSWORD = secret\n# comment\n'
    out = sanitize_env_text_for_docker(raw)
    assert "UVICORN_HOST=0.0.0.0" in out
    assert "DB_PASSWORD=secret" in out
    assert "UVICORN_HOST " not in out
    print("OK: sanitize_env_text_for_docker")


def test_native_migration_import():
    from app.services.native_migration import run_native_cross_db_migration
    from app.services.migrators.marzban import MarzbanMigrator
    assert run_native_cross_db_migration and MarzbanMigrator
    print("OK: native migration imports")


if __name__ == "__main__":
    test_build_local_alembic_url()
    test_sqlite_column_intersection()
    test_migration_strategy_matrix()
    test_read_alembic_from_sql_dump()
    test_convert_bool_values()
    test_copy_sqlite_to_sqlite_associations()
    test_sanitize_env_text_for_docker()
    test_native_migration_import()
    print("\nAll native migration tests passed.")
