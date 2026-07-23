"""Tests for SQL dump staging (Timescale→MySQL convert path)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_filter_timescaledb_extension_in_staging():
    from app.services.native_migration.sql_staging import _filter_timescaledb_extension_sql

    sql = "\n".join([
        "CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;",
        "CREATE TABLE users (id int);",
        "DROP EXTENSION IF EXISTS timescaledb;",
        "INSERT INTO users VALUES (1);",
    ])
    out = _filter_timescaledb_extension_sql(sql)
    assert "timescaledb" not in out.lower()
    assert "CREATE TABLE users" in out
    assert "INSERT INTO users" in out
    print("OK: staging filter timescaledb extension")


def test_compose_has_service_helper():
    from app.services.pg_restore import _compose_has_service
    # Without a live compose file this should be False, not crash
    assert _compose_has_service("") is False
    assert _compose_has_service("pgbouncer") in (True, False)
    print("OK: compose_has_service safe")


def test_explain_cannot_stage_timescale():
    from app.services.pg_restore import explain_restore_error

    info = explain_restore_error(
        RuntimeError("Cannot stage timescaledb SQL dump — start the timescaledb service"),
        "timescaledb",
        "mysql",
    )
    assert "stage" in info["en"].lower() or "timescale" in info["en"].lower()
    assert "mysql" in info["en"].lower() or "installed=mysql" in info["en"]
    assert info.get("causes_fa")
    print("OK: explain cannot stage timescale→mysql")


def test_import_sql_dump_routes_to_ephemeral_pg(monkeypatch=None):
    """When compose has no timescaledb, import_sql_dump must use ephemeral PG — not raise."""
    import asyncio
    from app.services.native_migration import sql_staging as mod

    calls = {"ephemeral": 0}

    async def fake_ephemeral(migrator, dump_path, source_db, conn, staging_db, container):
        calls["ephemeral"] += 1
        return {
            "host": "127.0.0.1",
            "port": "54330",
            "database": staging_db,
            "user": "postgres",
            "password": "x",
            "_ephemeral_container": container,
        }

    class Job:
        def log(self, *_a, **_k):
            pass

    class Mini:
        def __init__(self):
            self.job = Job()

        async def _run_cmd(self, *a, **k):
            return True, ""

    # Patch helpers
    orig_compose = mod._compose_text
    orig_resolve = mod.resolve_db_service
    orig_ephemeral = mod._import_via_ephemeral_postgres
    mod._compose_text = lambda: "services:\n  mysql:\n    image: mysql:8\n"
    mod.resolve_db_service = lambda _db: "timescaledb"
    mod._import_via_ephemeral_postgres = fake_ephemeral

    tmp = Path("/tmp/pgmig_stage_test.sql")
    tmp.write_text("CREATE TABLE users (id int);\n", encoding="utf-8")
    try:
        result = asyncio.run(
            mod.import_sql_dump_to_live_db(Mini(), str(tmp), "timescaledb", {"password": "x"})
        )
        assert calls["ephemeral"] == 1
        assert result["port"] == "54330"
        assert result.get("_ephemeral_container")
        print("OK: timescaledb dump routes to ephemeral PG when compose has mysql only")
    finally:
        mod._compose_text = orig_compose
        mod.resolve_db_service = orig_resolve
        mod._import_via_ephemeral_postgres = orig_ephemeral
        tmp.unlink(missing_ok=True)


def test_create_staging_db_runs_drop_and_create_separately():
    """Regression: DROP+CREATE in one psql -c fails (transaction block)."""
    import asyncio
    from app.services.native_migration import sql_staging as mod

    calls: list[str] = []

    async def fake_psql(container, pwd, db, sql=None, *, stdin_path=None, on_error_stop=True):
        calls.append(sql or "")
        return 0, "ok"

    async def fake_running(_name):
        return True

    orig = mod._psql_ephemeral
    orig_run = mod._container_running
    mod._psql_ephemeral = fake_psql
    mod._container_running = fake_running
    try:
        asyncio.run(mod._create_pg_staging_db("c", "p", "pgmig_abc123"))
        # DROP, CREATE, then SELECT 1 on new DB
        assert len(calls) >= 2
        assert "DROP DATABASE" in calls[0]
        assert "CREATE DATABASE" in calls[1]
        assert "DROP" not in calls[1]
        print("OK: staging CREATE DATABASE is a separate psql -c")
    finally:
        mod._psql_ephemeral = orig
        mod._container_running = orig_run


def test_transient_pg_error_detection():
    from app.services.native_migration.sql_staging import _is_transient_pg_error

    assert _is_transient_pg_error("FATAL: the database system is shutting down")
    assert _is_transient_pg_error("the database system is starting up")
    assert not _is_transient_pg_error("syntax error at or near")
    print("OK: transient pg error detection")


if __name__ == "__main__":
    test_filter_timescaledb_extension_in_staging()
    test_compose_has_service_helper()
    test_explain_cannot_stage_timescale()
    test_import_sql_dump_routes_to_ephemeral_pg()
    test_create_staging_db_runs_drop_and_create_separately()
    test_transient_pg_error_detection()
    print("\nAll sql staging tests passed.")
