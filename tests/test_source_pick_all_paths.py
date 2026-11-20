"""Same bug class as the Marzban stale-sqlite report, on every migration path:
pick the backup file that actually holds users, never report a config-only
"success", and keep multi-DB dumps from leaking into staging."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.migrators.base import MigrationJob  # noqa: E402
from app.services.migrators.marzban import MarzbanMigrator  # noqa: E402


def _sqlite_users(path: Path, n: int, extra_tables: tuple[str, ...] = ()) -> Path:
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
    for t in extra_tables:
        c.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY)")
    c.executemany("INSERT INTO users (username) VALUES (?)", [(f"u{i}",) for i in range(n)])
    c.commit()
    c.close()
    return path


def _age(path: Path, seconds: int) -> None:
    st = path.stat()
    os.utime(path, (st.st_atime - seconds, st.st_mtime - seconds))


# --- Marzban -----------------------------------------------------------------

def test_marzban_multiple_sqlite_prefers_one_with_users(tmp_path):
    (tmp_path / "old").mkdir()
    _sqlite_users(tmp_path / "old" / "db.sqlite3", 0)
    good = _sqlite_users(tmp_path / "marzban.db", 4)
    m = MarzbanMigrator(MigrationJob(job_id="sq-multi"), {"target_db": "mysql"})
    assert m._pick_marzban_sqlite(tmp_path) == good


def test_marzban_multiple_sqlite_with_users_prefers_newest(tmp_path):
    old = _sqlite_users(tmp_path / "backup_db.sqlite3", 9)  # older copy, more users
    new_dir = tmp_path / "var"
    new_dir.mkdir()
    new = _sqlite_users(new_dir / "db.sqlite3", 5)
    _age(old, 86400)
    m = MarzbanMigrator(MigrationJob(job_id="sq-new"), {"target_db": "mysql"})
    assert m._pick_marzban_sqlite(tmp_path) == new


def test_marzban_declared_engine_kept_when_both_have_users(tmp_path):
    sq = _sqlite_users(tmp_path / "db.sqlite3", 2)
    sql = tmp_path / "marzban.sql"
    sql.write_text("CREATE TABLE `users` (`id` int);\nINSERT INTO `users` VALUES (1),(2),(3);\n")
    m = MarzbanMigrator(MigrationJob(job_id="keep"), {"target_db": "mysql"})
    assert m._auto_pick_source("sqlite", "mysql", sq, sql) == "sqlite"
    assert m._auto_pick_source("mysql", "mysql", sq, sql) == "mysql"


def test_marzban_live_sqlite_declared_but_env_is_mysql(tmp_path, monkeypatch):
    from app.services.migrators import marzban as mz

    mdir = tmp_path / "opt"
    mdata = tmp_path / "data"
    mdir.mkdir()
    mdata.mkdir()
    (mdir / ".env").write_text('SQLALCHEMY_DATABASE_URL = "mysql+pymysql://root:pw@127.0.0.1/marzban"\n')
    _sqlite_users(mdata / "db.sqlite3", 0)
    monkeypatch.setattr(mz, "MARZBAN_DIR", mdir)
    monkeypatch.setattr(mz, "MARZBAN_DATA", mdata)
    m = mz.MarzbanMigrator(MigrationJob(job_id="live"), {"target_db": "mysql", "source_db": "sqlite"})
    assert m._resolve_live_source("sqlite") == "mysql"
    assert m.params["source_db"] == "mysql"
    db, pwds, _svcs = m._live_marzban_dump_targets()
    assert db == "marzban" and "pw" in pwds


def test_marzban_live_custom_db_name(tmp_path, monkeypatch):
    from app.services.migrators import marzban as mz

    mdir = tmp_path / "opt"
    mdir.mkdir()
    (mdir / ".env").write_text(
        'SQLALCHEMY_DATABASE_URL = "mysql+pymysql://root:s3c@127.0.0.1:3306/marz_prod"\n'
        'MYSQL_ROOT_PASSWORD = "rootpw"\n'
    )
    (mdir / "docker-compose.yml").write_text(
        "services:\n  marzban:\n    image: x\n  mariadb:\n    image: mariadb\n"
    )
    monkeypatch.setattr(mz, "MARZBAN_DIR", mdir)
    m = mz.MarzbanMigrator(MigrationJob(job_id="live2"), {"target_db": "mysql"})
    db, pwds, svcs = m._live_marzban_dump_targets()
    assert db == "marz_prod"
    assert pwds[:2] == ["rootpw", "s3c"]
    assert svcs[0] == "mariadb"


# --- MySQL staging (Marzban/PasarGuard → PostgreSQL) ---------------------------

def test_staging_keeps_only_panel_section(tmp_path):
    from app.services.native_migration.sql_staging import write_mysql_dump_for_staging

    dump = tmp_path / "all.sql"
    dump.write_text(
        "/*!40000 DROP DATABASE IF EXISTS `mysql`*/;\n"
        "CREATE DATABASE /*!32312 IF NOT EXISTS*/ `mysql`;\nUSE `mysql`;\n"
        "CREATE TABLE `user` (`Host` char(60));\nINSERT INTO `user` VALUES ('%');\n"
        "CREATE DATABASE /*!32312 IF NOT EXISTS*/ `marzban`;\nUSE `marzban`;\n"
        "CREATE TABLE `admins` (`id` int);\nCREATE TABLE `users` (`id` int);\n"
        "INSERT INTO `users` VALUES (1),(2);\n"
    )
    out, stripped = write_mysql_dump_for_staging(dump, "pgmig_x")
    text = out.read_text()
    assert "INSERT INTO `users` VALUES (1),(2);" in text
    assert "`user`" not in text
    assert "DROP DATABASE" not in text and "USE `" not in text
    assert stripped >= 5


# --- 3x-ui -----------------------------------------------------------------------

def _xui_db(path: Path, clients: int, inbounds: int = 1) -> Path:
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE inbounds (id INTEGER PRIMARY KEY, settings TEXT)")
    c.execute("CREATE TABLE client_traffics (id INTEGER PRIMARY KEY, email TEXT)")
    c.executemany("INSERT INTO inbounds (settings) VALUES ('{}')", [()] * inbounds)
    c.executemany("INSERT INTO client_traffics (email) VALUES (?)", [(f"e{i}",) for i in range(clients)])
    c.commit()
    c.close()
    return path


def test_xui_picks_db_with_clients(tmp_path):
    from app.services.migrators.xui import find_xui_db_in_dir

    (tmp_path / "a").mkdir()
    _xui_db(tmp_path / "a" / "x-ui.db", 0)
    good = _xui_db(tmp_path / "x-ui-backup.db", 12)
    _sqlite_users(tmp_path / "db.sqlite3", 3)  # PasarGuard/Marzban sqlite, not 3x-ui
    assert find_xui_db_in_dir(tmp_path) == good


def test_xui_output_without_users_is_refused(tmp_path):
    from app.services.migrators.xui import assert_migrated_pg_has_data

    out = tmp_path / "out.sqlite3"
    c = sqlite3.connect(str(out))
    c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    c.execute("CREATE TABLE inbounds (id INTEGER PRIMARY KEY)")
    c.execute("INSERT INTO inbounds DEFAULT VALUES")
    c.commit()
    c.close()
    with pytest.raises(RuntimeError):
        assert_migrated_pg_has_data(out, source_clients=7)
    assert assert_migrated_pg_has_data(out, source_clients=0)["inbounds"] == 1


def test_xui_convert_without_users_is_refused():
    from app.services.migrators.xui import XuiMigrator

    m = XuiMigrator(MigrationJob(job_id="xui-conv"), {"target_db": "mysql"})
    m._xui_source_clients = 5
    with pytest.raises(RuntimeError, match="0 users"):
        m._abort_if_post_convert_gaps({"inbounds": 3, "core_configs": 1})


# --- Hiddify ---------------------------------------------------------------------

def _hiddify_json(path: Path, users: int) -> Path:
    path.write_text(json.dumps({
        "users": [{"uuid": f"00000000-0000-0000-0000-{i:012d}", "name": f"u{i}"} for i in range(users)],
        "hconfigs": [{"key": "proxy_path_client", "value": "abc"}],
    }))
    return path


def test_hiddify_picks_newest_export_with_users(tmp_path):
    from app.services.migrators.hiddify_lib import find_hiddify_json_in_dir

    old = _hiddify_json(tmp_path / "backup-old.json", 3)
    new = _hiddify_json(tmp_path / "backup-new.json", 50)
    empty = _hiddify_json(tmp_path / "backup-newest-empty.json", 0)
    _age(old, 3 * 86400)
    _age(new, 86400)
    assert empty.exists()
    assert find_hiddify_json_in_dir(tmp_path) == new


# --- PasarGuard restore -------------------------------------------------------------

def test_restore_prefers_dump_with_users(tmp_path):
    from app.services.pg_restore import discover_backup_artifacts

    schema = (
        "CREATE TABLE `users` (`id` int);\nCREATE TABLE `admins` (`id` int);\n"
        "CREATE TABLE `hosts` (`id` int);\nCREATE TABLE `inbounds` (`id` int);\n"
        "CREATE TABLE `alembic_version` (`version_num` varchar(32));\n"
    )
    (tmp_path / "a").mkdir()
    empty = tmp_path / "a" / "x.sql"
    empty.write_text("-- MySQL dump\n" + schema)
    full = tmp_path / "a" / "y_longer_name.sql"
    full.write_text("-- MySQL dump\n" + schema + "INSERT INTO `users` VALUES (1),(2);\n")
    art = discover_backup_artifacts(tmp_path)
    assert Path(art["dump_path"]) == full


# --- Interactive choice when the backup holds two DBs with different counts ---

def _ambiguous(tmp_path):
    sq = _sqlite_users(tmp_path / "db.sqlite3", 290)
    sql = tmp_path / "marzban.sql"
    sql.write_text("CREATE TABLE `users` (`id` int);\nINSERT INTO `users` VALUES (1),(2);\n")
    return sq, sql


@pytest.mark.parametrize("answer", ["sqlite", "mysql"])
def test_ambiguous_backup_asks_user_and_honours_answer(tmp_path, answer):
    import asyncio

    sq, sql = _ambiguous(tmp_path)
    job = MigrationJob(job_id="ask")
    m = MarzbanMigrator(job, {"target_db": "mysql"})

    async def scenario():
        task = asyncio.create_task(m._ask_source_if_ambiguous("mysql", sq, sql))
        for _ in range(50):
            if job.pending_choice:
                break
            await asyncio.sleep(0.01)
        pc = job.pending_choice
        assert pc and pc["key"] == "backup_source"
        assert {o["value"]: o["users"] for o in pc["options"]} == {"sqlite": 290, "mysql": 2}
        assert not job.answer_choice("backup_source", "bogus")
        assert job.answer_choice("backup_source", answer)
        return await task

    assert asyncio.run(scenario()) == answer
    assert job.pending_choice is None


def test_no_question_when_counts_equal_or_single_source(tmp_path):
    import asyncio

    sq = _sqlite_users(tmp_path / "db.sqlite3", 2)
    sql = tmp_path / "m.sql"
    sql.write_text("CREATE TABLE `users` (`id` int);\nINSERT INTO `users` VALUES (1),(2);\n")
    job = MigrationJob(job_id="noask")
    m = MarzbanMigrator(job, {"target_db": "mysql"})
    assert asyncio.run(m._ask_source_if_ambiguous("mysql", sq, sql)) == "mysql"
    assert asyncio.run(m._ask_source_if_ambiguous("sqlite", sq, None)) == "sqlite"
    assert job.pending_choice is None


def test_choice_times_out_to_default():
    import asyncio

    job = MigrationJob(job_id="to")
    r = asyncio.run(job.ask_choice(
        "k", {"en": "q"}, [{"value": "a", "label": {}}, {"value": "b", "label": {}}],
        "b", timeout=0.05,
    ))
    assert r == "b"


# --- PasarGuard restore: same question --------------------------------------

@pytest.mark.parametrize("answer,layout,db", [("sqlite", "sqlite_file", "sqlite"), ("mysql", "single", "mysql")])
def test_restore_asks_which_db_and_prunes_other(tmp_path, answer, layout, db):
    import asyncio

    from app.services import pg_restore

    _sqlite_users(tmp_path / "db.sqlite3", 290, extra_tables=("hosts", "nodes"))
    (tmp_path / "db_backup.sql").write_text(
        "-- MySQL dump 10.13\nCREATE TABLE `users` (`id` int) ENGINE=InnoDB;\n"
        "INSERT INTO `users` VALUES (1),(2);\n"
    )
    job = MigrationJob(job_id="rs-ask")
    analysis: dict = {"table_counts": {"users": 2}}

    async def scenario():
        task = asyncio.create_task(
            pg_restore._ask_restore_source_if_ambiguous(job, tmp_path, analysis, "mysql")
        )
        for _ in range(100):
            if job.pending_choice:
                break
            await asyncio.sleep(0.01)
        assert {o["value"]: o["users"] for o in job.pending_choice["options"]} == {"sqlite": 290, "mysql": 2}
        assert job.answer_choice("backup_source", answer)
        return await task

    assert asyncio.run(scenario()) == db
    assert analysis["layout"] == layout and analysis["table_counts"] == {}
    assert (tmp_path / "db.sqlite3").exists() == (answer == "sqlite")
    assert (tmp_path / "db_backup.sql").exists() == (answer != "sqlite")


def test_restore_no_question_with_single_source(tmp_path):
    import asyncio

    from app.services import pg_restore

    _sqlite_users(tmp_path / "db.sqlite3", 5, extra_tables=("hosts", "nodes"))
    job = MigrationJob(job_id="rs-none")
    assert asyncio.run(pg_restore._ask_restore_source_if_ambiguous(job, tmp_path, {}, "sqlite")) is None
