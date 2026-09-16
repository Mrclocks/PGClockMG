"""Regression tests for migration diagnostics hardening."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_sqlite_reader_quotes_identifiers():
    from app.services.native_migration.adapters import SqliteReader

    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.execute('CREATE TABLE "users" (id INTEGER, "group" TEXT)')
        conn.execute('INSERT INTO "users" VALUES (1, \'a\')')
        conn.commit()
        conn.close()

        reader = SqliteReader(path)
        try:
            rows = list(reader.fetch_rows("users", ["id", "group"]))
            assert rows == [(1, "a")]
        finally:
            reader.close()
    finally:
        Path(path).unlink(missing_ok=True)


def test_sniff_mariadb_sandbox_and_uca1400(tmp_path):
    from app.services.pg_restore import _sniff_sql_dump

    path = tmp_path / "db_backup.sql"
    path.write_text(
        "-- MySQL dump 10.19  Distrib 10.11.6-MariaDB\n"
        "/*!999999\\- enable the sandbox mode */\n"
        "CREATE TABLE t (c varchar(10) COLLATE utf8mb4_uca1400_ai_ci);\n",
        encoding="utf-8",
    )
    score, engine = _sniff_sql_dump(path)
    assert score > 0
    assert engine == "mariadb"


@pytest.mark.asyncio
async def test_reset_target_schema_fails_hard_when_wipe_fails(monkeypatch):
    from app.services.native_migration import cross_db as mod

    class Job:
        def log(self, *_a, **_k):
            pass

    class Mini:
        def __init__(self):
            self.job = Job()
            self.params = {}

    async def fake_exec(*_a, **_k):
        proc = MagicMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"FATAL: wipe failed", None))
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        mod, "get_target_connection",
        lambda _p: {"user": "postgres", "password": "x", "database": "pasarguard"},
    )
    monkeypatch.setattr(mod, "resolve_db_service", lambda _t: "timescaledb")

    with pytest.raises(RuntimeError, match="schema reset failed"):
        await mod._reset_target_schema(Mini(), "timescaledb")


@pytest.mark.asyncio
async def test_marzban_mysql_to_sqlite_rejected_early():
    from app.services.migrators.marzban import MarzbanMigrator
    from app.services.migrators.base import MigrationJob

    job = MigrationJob(job_id="t")
    mig = MarzbanMigrator(job, {"source_db": "mysql", "target_db": "sqlite"})

    with pytest.raises(RuntimeError, match="sqlite"):
        await mig._migrate_mysql_like_restore(
            Path("/tmp/nope.sql"), "mysql", "sqlite", None, "",
        )


def test_filter_timescaledb_keeps_catalog_preamble_but_drops_extension_ddl():
    from app.services.native_migration.sql_staging import _filter_timescaledb_extension_sql

    sql = "\n".join([
        "CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;",
        "CREATE TABLE users (id int);",
        "DROP EXTENSION IF EXISTS timescaledb;",
        "INSERT INTO users VALUES (1);",
    ])
    out = _filter_timescaledb_extension_sql(sql)
    assert "CREATE EXTENSION" not in out.upper()
    assert "DROP EXTENSION" not in out.upper()
    assert "CREATE TABLE users" in out
    assert "pgclockmg_ts_catalog" in out.lower() or "_timescaledb" in out.lower()
