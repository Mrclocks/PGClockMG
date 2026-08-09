"""Marzban migrate matrix: sqlite/mysql/mariadb → all PasarGuard targets.

Exercises MarzbanMigrator path selection + pre-boot heal wiring with mocks
(no live Docker / PasarGuard required).

Run: python tests/test_marzban_migrate_matrix.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MARZBAN_SOURCES = ("sqlite", "mysql", "mariadb")
PASARGUARD_TARGETS = ("sqlite", "mysql", "mariadb", "postgresql", "timescaledb")


def _expected_path(source_db: str, target_db: str) -> str:
    """Mirror MarzbanMigrator routing rules for assertions."""
    from app.services.pg_restore import soft_db_family

    if source_db == "sqlite":
        if target_db == "sqlite":
            return "sqlite_same"
        return "sqlite_then_convert"
    if source_db in ("mysql", "mariadb"):
        if soft_db_family(source_db, target_db) or source_db == target_db:
            return "mysql_same_family"
        return "mysql_two_phase"
    raise AssertionError(f"unexpected source {source_db}")


def test_routing_matrix_matches_soft_family():
    """Document expected path for every Marzban source → PasarGuard target."""
    expected = {
        ("sqlite", "sqlite"): "sqlite_same",
        ("sqlite", "mysql"): "sqlite_then_convert",
        ("sqlite", "mariadb"): "sqlite_then_convert",
        ("sqlite", "postgresql"): "sqlite_then_convert",
        ("sqlite", "timescaledb"): "sqlite_then_convert",
        ("mysql", "sqlite"): "mysql_two_phase",  # soft_family false; two_phase (may be unsupported downstream)
        ("mysql", "mysql"): "mysql_same_family",
        ("mysql", "mariadb"): "mysql_same_family",
        ("mysql", "postgresql"): "mysql_two_phase",
        ("mysql", "timescaledb"): "mysql_two_phase",
        ("mariadb", "sqlite"): "mysql_two_phase",
        ("mariadb", "mysql"): "mysql_same_family",
        ("mariadb", "mariadb"): "mysql_same_family",
        ("mariadb", "postgresql"): "mysql_two_phase",
        ("mariadb", "timescaledb"): "mysql_two_phase",
    }
    for src in MARZBAN_SOURCES:
        for tgt in PASARGUARD_TARGETS:
            got = _expected_path(src, tgt)
            assert got == expected[(src, tgt)], f"{src}→{tgt}: {got} != {expected[(src, tgt)]}"
    print(f"OK: routing matrix ({len(expected)} pairs)")


def _job():
    from app.services.migrators.base import MigrationJob

    return MigrationJob(job_id="mtx-test")


def _run(coro):
    return asyncio.run(coro)


async def _exercise_sqlite_land(target_db: str) -> dict:
    """Land a tiny Marzban-shaped sqlite and drive _migrate_sqlite_like_restore with mocks."""
    from app.services.migrators.marzban import MarzbanMigrator
    from app.services.migrators.base import MigrationJob

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "db.sqlite3"
        import sqlite3

        db = sqlite3.connect(str(src))
        db.executescript(
            """
            CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT);
            INSERT INTO nodes VALUES (1, 'usa-reality');
            INSERT INTO nodes VALUES (2, 'USA-Reality');
            CREATE TABLE node_usages (id INTEGER PRIMARY KEY, node_id INTEGER);
            INSERT INTO node_usages VALUES (1, 1);
            INSERT INTO node_usages VALUES (2, 999);
            CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);
            INSERT INTO alembic_version VALUES ('deadbeef');
            """
        )
        db.commit()
        db.close()

        job = MigrationJob(job_id="sqlite-land")
        params = {
            "source_db": "sqlite",
            "target_db": target_db,
            "source_db_password": "x",
            "target_db_password": "x",
        }
        m = MarzbanMigrator(job, params)
        calls = {"preboot": 0, "starts": [], "convert": 0}

        import app.services.marzban_preboot_heal as preboot_mod

        real_preboot = preboot_mod.heal_marzban_preboot

        async def fake_preboot(migrator):
            calls["preboot"] += 1
            return await real_preboot(migrator)

        async def fake_start(migrator, *, health_max_wait=None):
            calls["starts"].append(health_max_wait)
            return None

        async def fake_stop():
            return None

        def fake_assert(_path):
            return None

        async def fake_convert(*_a, **_k):
            calls["convert"] += 1
            return None

        with (
            patch("app.services.migrators.marzban.PASARGUARD_DIR", Path(td) / "pg"),
            patch("app.services.migrators.marzban.PASARGUARD_DATA", Path(td) / "pgdata"),
            patch("app.services.migrators.marzban.PASARGUARD_ENV", Path(td) / "pg" / ".env"),
            patch("app.services.migrators.marzban.BACKUP_DIR", Path(td) / "bak"),
            patch("app.services.migrators.marzban.safe_start_pasarguard", fake_start),
            patch.object(preboot_mod, "heal_marzban_preboot", side_effect=fake_preboot),
            patch.object(m, "_stop_panel", fake_stop),
            patch.object(m, "_assert_sqlite_pasarguard_ready", fake_assert),
            patch.object(m, "_convert_pg_sqlite_to_target", fake_convert),
            patch.object(m, "_force_env_sqlite", AsyncMock()),
            patch.object(preboot_mod, "get_target_connection") as gtc,
            patch("app.services.unique_name_heal.get_target_connection") as gtc2,
        ):
            (Path(td) / "pg").mkdir(parents=True, exist_ok=True)
            (Path(td) / "pgdata").mkdir(parents=True, exist_ok=True)
            (Path(td) / "bak").mkdir(parents=True, exist_ok=True)
            (Path(td) / "pg" / ".env").write_text(
                'SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite:////x"\n',
                encoding="utf-8",
            )
            landed = Path(td) / "pgdata" / "db.sqlite3"
            gtc.return_value = {
                "db_type": "sqlite",
                "sqlite_path": str(landed),
            }
            gtc2.return_value = gtc.return_value

            await m._migrate_sqlite_like_restore(src, target_db, None, "")

        return calls


async def _exercise_mysql_same_or_two_phase(source_db: str, target_db: str) -> dict:
    from app.services.migrators.marzban import MarzbanMigrator
    from app.services.migrators.base import MigrationJob
    from app.services.pg_restore import soft_db_family

    with tempfile.TemporaryDirectory() as td:
        dump = Path(td) / "marzban.sql"
        dump.write_text(
            "CREATE DATABASE marzban;\nUSE marzban;\n"
            "CREATE TABLE nodes (id INT PRIMARY KEY, name VARCHAR(64));\n"
            "INSERT INTO nodes VALUES (1,'usa-reality');\n",
            encoding="utf-8",
        )
        job = MigrationJob(job_id="mysql-path")
        params = {
            "source_db": source_db,
            "target_db": target_db,
            "source_db_password": "x",
            "target_db_password": "x",
        }
        m = MarzbanMigrator(job, params)
        calls = {
            "preboot": 0,
            "starts": [],
            "import": 0,
            "cross": 0,
            "same_family": soft_db_family(source_db, target_db) or source_db == target_db,
        }

        async def fake_preboot(_m):
            calls["preboot"] += 1
            return {"renamed": 0, "orphans_deleted": 0, "orphans_nulled": 0}

        async def fake_start(_m, *, health_max_wait=None):
            calls["starts"].append(health_max_wait)
            return None

        async def fake_import(_p):
            calls["import"] += 1
            return None

        async def fake_cross(*_a, **_k):
            calls["cross"] += 1
            return {"users": 1}

        with (
            patch("app.services.migrators.marzban.PASARGUARD_DIR", Path(td) / "pg"),
            patch("app.services.migrators.marzban.PASARGUARD_DATA", Path(td) / "pgdata"),
            patch("app.services.migrators.marzban.PASARGUARD_ENV", Path(td) / "pg" / ".env"),
            patch("app.services.migrators.marzban.BACKUP_DIR", Path(td) / "bak"),
            patch("app.services.migrators.marzban.safe_start_pasarguard", fake_start),
            patch(
                "app.services.marzban_preboot_heal.heal_marzban_preboot",
                side_effect=fake_preboot,
            ),
            patch.object(m, "_update_env_paths", AsyncMock()),
            patch.object(m, "_ensure_target_database_stack", AsyncMock()),
            patch.object(m, "_import_mysql_dump", fake_import),
            patch(
                "app.services.migrators.marzban.run_cross_db_migration",
                fake_cross,
            ),
            patch.object(m, "_finalize_env_after_convert", AsyncMock()),
            patch.object(m, "_abort_if_copy_gaps", MagicMock()),
            patch(
                "app.services.native_migration.cross_db._heal_staging_alembic_if_unknown",
                AsyncMock(),
            ),
            patch(
                "app.services.migrators.marzban.get_target_connection",
                return_value={"user": "root", "password": "x", "database": "pasarguard"},
            ),
        ):
            (Path(td) / "pg").mkdir(parents=True, exist_ok=True)
            await m._migrate_mysql_like_restore(dump, source_db, target_db, None, "")

        return calls


def test_sqlite_paths_call_preboot_and_long_wait():
    for tgt in PASARGUARD_TARGETS:
        calls = _run(_exercise_sqlite_land(tgt))
        assert calls["preboot"] == 1, f"sqlite→{tgt} missing preboot"
        # Schema-upgrade boot must use long wait
        assert 1800 in calls["starts"], f"sqlite→{tgt} starts={calls['starts']}"
        if tgt == "sqlite":
            assert calls["convert"] == 0
            assert calls["starts"].count(1800) >= 1
        else:
            assert calls["convert"] == 1
        print(f"OK: sqlite→{tgt} preboot+wait (starts={calls['starts']})")


def test_mysql_mariadb_paths_matrix():
    for src in ("mysql", "mariadb"):
        for tgt in PASARGUARD_TARGETS:
            calls = _run(_exercise_mysql_same_or_two_phase(src, tgt))
            path = _expected_path(src, tgt)
            if path == "mysql_same_family":
                assert calls["import"] == 1, f"{src}→{tgt}"
                assert calls["cross"] == 0, f"{src}→{tgt}"
                assert calls["preboot"] == 1, f"{src}→{tgt} preboot"
                assert 1800 in calls["starts"], f"{src}→{tgt} starts={calls['starts']}"
            else:
                assert calls["import"] == 0, f"{src}→{tgt}"
                assert calls["cross"] == 1, f"{src}→{tgt}"
                # two-phase does final safe_start without forced 1800 here
                # (panel-boot inside cross_db uses 1800 separately)
            print(f"OK: {src}→{tgt} path={path} calls={ {k: calls[k] for k in ('import','cross','preboot','starts')} }")


def test_preboot_heal_noop_and_dirty_sqlite():
    """Clean dump → no mutations; dirty dump → rename + orphan delete."""
    import sqlite3
    from app.services.marzban_preboot_heal import (
        cleanup_orphans_sqlite,
    )
    from app.services.unique_name_heal import dedupe_unique_names_sqlite

    with tempfile.TemporaryDirectory() as td:
        clean = Path(td) / "clean.sqlite3"
        dirty = Path(td) / "dirty.sqlite3"
        for path, script in (
            (
                clean,
                """
                CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT);
                INSERT INTO nodes VALUES (1, 'only');
                CREATE TABLE node_usages (id INTEGER PRIMARY KEY, node_id INTEGER);
                INSERT INTO node_usages VALUES (1, 1);
                """,
            ),
            (
                dirty,
                """
                CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT);
                INSERT INTO nodes VALUES (1, 'usa-reality');
                INSERT INTO nodes VALUES (2, 'usa-reality');
                CREATE TABLE node_usages (id INTEGER PRIMARY KEY, node_id INTEGER);
                INSERT INTO node_usages VALUES (1, 1);
                INSERT INTO node_usages VALUES (2, 999);
                CREATE TABLE user_templates (id INTEGER PRIMARY KEY, name TEXT);
                """,
            ),
        ):
            db = sqlite3.connect(str(path))
            db.executescript(script)
            db.commit()
            db.close()

        assert dedupe_unique_names_sqlite(clean) == 0
        assert cleanup_orphans_sqlite(clean) == (0, 0)

        from app.services.unique_name_heal import plan_duplicate_name_renames

        # MySQL path groups case-insensitively (Marzban case-sensitive names).
        case_renames = plan_duplicate_name_renames(
            [(1, "usa-reality"), (2, "USA-Reality")],
            case_insensitive=True,
        )
        assert len(case_renames) == 1

        renamed = dedupe_unique_names_sqlite(dirty)
        deleted, nulled = cleanup_orphans_sqlite(dirty)
        assert renamed >= 1
        assert deleted == 1
        assert nulled == 0
        db = sqlite3.connect(str(dirty))
        names = sorted(r[0] for r in db.execute("SELECT name FROM nodes").fetchall())
        usage_count = db.execute("SELECT COUNT(*) FROM node_usages").fetchone()[0]
        db.close()
        assert len(names) == len(set(names))
        assert usage_count == 1
    print("OK: preboot heal noop on clean + fixes dirty")


def test_dump_rewrite_safe_for_mysql_source():
    from app.services.env_migration import (
        fix_mysql_dump_for_pasarguard,
        rewrite_mysql_dump_file_for_pasarguard,
    )

    raw = (
        "CREATE DATABASE marzban;\n"
        "USE marzban;\n"
        "INSERT INTO nodes VALUES (1, 'x');\n"
    )
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "m.sql"
        dst = Path(td) / "out.sql"
        src.write_text(raw, encoding="utf-8")
        n = rewrite_mysql_dump_file_for_pasarguard(src, dst)
        assert n >= 2
        assert dst.read_text(encoding="utf-8") == fix_mysql_dump_for_pasarguard(raw)
    print("OK: mysql dump rewrite")


if __name__ == "__main__":
    # Python 3.10+ has get_event_loop quirks; ensure loop exists
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    test_routing_matrix_matches_soft_family()
    test_preboot_heal_noop_and_dirty_sqlite()
    test_dump_rewrite_safe_for_mysql_source()
    test_sqlite_paths_call_preboot_and_long_wait()
    test_mysql_mariadb_paths_matrix()
    print("\nAll Marzban migrate matrix tests passed.")
