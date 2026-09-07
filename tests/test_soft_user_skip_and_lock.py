"""Tests for shared panel job lock + soft user-row skip policy."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def setup_function():
    from app.services.orchestrator import _active_jobs
    from app.services.pg_restore import _restore_jobs

    _active_jobs.clear()
    _restore_jobs.clear()


def teardown_function():
    setup_function()


def test_shared_lock_blocks_migrate_while_restore_running():
    from app.services.migrators.base import MigrationJob
    from app.services.orchestrator import start_migration, MigrationAlreadyRunning
    from app.services.panel_job_lock import PanelJobAlreadyRunning
    from app.services import pg_restore

    setup_function()
    restore_job = MigrationJob(job_id="rest1")
    restore_job.status = "running"
    pg_restore._restore_jobs[restore_job.job_id] = restore_job

    async def _run():
        class FakeMigrator:
            def __init__(self, job, params):
                self.job = job

            async def run(self, params):
                return {"ok": True}

        with patch.dict(
            "app.services.orchestrator.MIGRATORS",
            {"marzban": FakeMigrator},
            clear=False,
        ):
            try:
                await start_migration({"source_panel": "marzban"})
                raise AssertionError("expected PanelJobAlreadyRunning")
            except (PanelJobAlreadyRunning, MigrationAlreadyRunning) as e:
                assert e.job.job_id == "rest1"
                assert getattr(e, "kind", "restore") == "restore"

    asyncio.run(_run())
    teardown_function()
    print("OK: migrate blocked by running restore")


def test_soft_user_rows_do_not_abort_partial_users():
    from app.services.native_migration.adapters import copy_tables_universal
    from app.services.native_migration.copy_core import SOFT_USER_RELATED_TABLES

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src.sqlite3"
        dst = Path(td) / "dst.sqlite3"
        sconn = sqlite3.connect(str(src))
        sconn.executescript(
            """
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
            INSERT INTO users VALUES (1, 'ok');
            INSERT INTO users VALUES (2, NULL);
            CREATE TABLE inbounds (id INTEGER PRIMARY KEY, tag TEXT);
            INSERT INTO inbounds VALUES (1, 'vless');
            """
        )
        sconn.commit()
        sconn.close()

        dconn = sqlite3.connect(str(dst))
        dconn.executescript(
            """
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL);
            CREATE TABLE inbounds (id INTEGER PRIMARY KEY, tag TEXT);
            """
        )
        dconn.commit()
        dconn.close()

        from app.services.native_migration.adapters import SqliteReader, SqliteWriter

        reader = SqliteReader(str(src))
        writer = SqliteWriter(str(dst))
        logs = []
        try:
            stats, report = copy_tables_universal(
                reader,
                writer,
                logs.append,
                fail_hard=True,
                soft_incomplete_tables=SOFT_USER_RELATED_TABLES,
            )
        finally:
            reader.close()
            writer.close()

        assert stats.get("users") == 1
        assert report.get("has_gaps") is False
        assert "users" in (report.get("row_skips") or {})
        assert report["row_skips"]["users"]["skipped"] >= 1
    print("OK: soft user skip continues with report")


def test_strict_user_rows_still_abort_without_soft_policy():
    from app.services.native_migration.adapters import copy_tables_universal, SqliteReader, SqliteWriter

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src.sqlite3"
        dst = Path(td) / "dst.sqlite3"
        sconn = sqlite3.connect(str(src))
        sconn.executescript(
            """
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
            INSERT INTO users VALUES (1, 'ok');
            INSERT INTO users VALUES (2, NULL);
            """
        )
        sconn.commit()
        sconn.close()
        dconn = sqlite3.connect(str(dst))
        dconn.executescript(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL);"
        )
        dconn.commit()
        dconn.close()

        reader = SqliteReader(str(src))
        writer = SqliteWriter(str(dst))
        try:
            try:
                copy_tables_universal(
                    reader, writer, lambda *_: None, fail_hard=True,
                    soft_incomplete_tables=frozenset(),
                )
                raise AssertionError("expected abort on partial users")
            except RuntimeError as e:
                assert "users" in str(e).lower()
        finally:
            reader.close()
            writer.close()
    print("OK: strict policy still aborts partial users")


def test_migration_request_defaults():
    from app.models import MigrationRequest

    req = MigrationRequest(source_panel="marzban", source_db="sqlite", target_db="sqlite")
    assert req.skip_bad_user_rows is True
    assert req.relocate_inbound_certs is False
    print("OK: migration request defaults")


if __name__ == "__main__":
    test_shared_lock_blocks_migrate_while_restore_running()
    test_soft_user_rows_do_not_abort_partial_users()
    test_strict_user_rows_still_abort_without_soft_policy()
    test_migration_request_defaults()
    print("All soft-skip / lock tests passed")
