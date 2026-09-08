"""Regression: Marzban migrate must not succeed with empty inbounds.

Covers the reported class of bugs where users land but inbounds/core_configs
do not (soft readiness OR-gate, live MySQL without assets, convert sum-check).
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _run(coro):
    return asyncio.run(coro)


def _migrator(params=None):
    from app.services.migrators.base import MigrationJob
    from app.services.migrators.marzban import MarzbanMigrator

    return MarzbanMigrator(MigrationJob(job_id="inbound-guard"), params or {})


def _sqlite_with(*, users=0, inbounds=0, hosts=0, core_configs=0, proxies=0) -> Path:
    td = tempfile.mkdtemp()
    path = Path(td) / "db.sqlite3"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
        CREATE TABLE inbounds (id INTEGER PRIMARY KEY, tag TEXT);
        CREATE TABLE hosts (id INTEGER PRIMARY KEY, remark TEXT);
        CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE admins (id INTEGER PRIMARY KEY);
        CREATE TABLE nodes (id INTEGER PRIMARY KEY);
        CREATE TABLE core_configs (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE proxies (id INTEGER PRIMARY KEY, type TEXT);
        """
    )
    for i in range(users):
        conn.execute("INSERT INTO users VALUES (?, ?)", (i + 1, f"u{i}"))
    for i in range(inbounds):
        conn.execute("INSERT INTO inbounds VALUES (?, ?)", (i + 1, f"in{i}"))
    for i in range(hosts):
        conn.execute("INSERT INTO hosts VALUES (?, ?)", (i + 1, f"h{i}"))
    for i in range(core_configs):
        conn.execute("INSERT INTO core_configs VALUES (?, ?)", (i + 1, f"core{i}"))
    for i in range(proxies):
        conn.execute("INSERT INTO proxies VALUES (?, ?)", (i + 1, "vless"))
    conn.commit()
    conn.close()
    return path


def test_assert_sqlite_rejects_users_without_inbounds():
    m = _migrator()
    path = _sqlite_with(users=2, inbounds=0, core_configs=1)
    try:
        m._assert_sqlite_pasarguard_ready(path)
        raise AssertionError("expected abort when users>0 and inbounds=0")
    except RuntimeError as e:
        assert "inbounds empty" in str(e)


def test_assert_sqlite_rejects_users_without_core_configs():
    m = _migrator()
    path = _sqlite_with(users=1, inbounds=1, core_configs=0)
    try:
        m._assert_sqlite_pasarguard_ready(path)
        raise AssertionError("expected abort when core_configs empty")
    except RuntimeError as e:
        assert "core_configs empty" in str(e)


def test_assert_sqlite_accepts_users_with_inbounds_and_core():
    m = _migrator()
    path = _sqlite_with(users=1, inbounds=2, core_configs=1)
    m._assert_sqlite_pasarguard_ready(path)


def test_assert_sqlite_still_rejects_totally_empty():
    m = _migrator()
    path = _sqlite_with()
    try:
        m._assert_sqlite_pasarguard_ready(path)
        raise AssertionError("expected abort on empty critical tables")
    except RuntimeError as e:
        assert "critical tables empty" in str(e)


def test_abort_if_empty_convert_rejects_users_without_inbounds():
    m = _migrator()
    path = _sqlite_with(users=3, inbounds=0)
    try:
        m._abort_if_empty_convert(path, {"users": 3, "inbounds": 0, "hosts": 0})
        raise AssertionError("expected convert abort")
    except RuntimeError as e:
        assert "inbounds=0" in str(e)


def test_abort_if_inbounds_missing_from_stats():
    m = _migrator()
    try:
        m._abort_if_inbounds_missing_from_stats({"users": 5, "inbounds": 0})
        raise AssertionError("expected stats abort")
    except RuntimeError as e:
        assert "inbounds=0" in str(e)
    m._abort_if_inbounds_missing_from_stats({"users": 5, "inbounds": 2})


def test_live_mysql_sets_extra_data_dir_and_merges_env():
    """Live MySQL must copy Marzban data dir assets (previously left None)."""

    async def _case():
        m = _migrator(
            {
                "source_db": "mysql",
                "target_db": "mysql",
                "source_db_password": "x",
                "target_db_password": "x",
            }
        )
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            mz_data = td_path / "mzdata"
            mz_dir = td_path / "mz"
            pg_dir = td_path / "pg"
            mz_data.mkdir()
            mz_dir.mkdir()
            pg_dir.mkdir()
            (mz_data / "xray_config.json").write_text('{"inbounds":[]}', encoding="utf-8")
            (mz_data / "certs").mkdir()
            (mz_data / "certs" / "a.pem").write_text("CERT", encoding="utf-8")
            (mz_dir / ".env").write_text(
                'XRAY_JSON = "./xray_config.json"\nMYSQL_ROOT_PASSWORD = "x"\n',
                encoding="utf-8",
            )
            (pg_dir / ".env").write_text(
                'SQLALCHEMY_DATABASE_URL = "mysql+pymysql://root:x@127.0.0.1/pasarguard"\n',
                encoding="utf-8",
            )
            dump = td_path / "work" / "marzban.sql"
            dump.parent.mkdir()
            dump.write_text("-- dump\n", encoding="utf-8")

            captured: dict = {}

            async def fake_dump(work_dir):
                return dump

            async def fake_mysql_restore(source_sql, source_db, target_db, extra_data_dir, install_env_snapshot):
                captured["extra_data_dir"] = extra_data_dir
                captured["env"] = Path(pg_dir / ".env").read_text(encoding="utf-8")

            with (
                patch("app.services.migrators.marzban.MARZBAN_DIR", mz_dir),
                patch("app.services.migrators.marzban.MARZBAN_DATA", mz_data),
                patch("app.services.migrators.marzban.PASARGUARD_DIR", pg_dir),
                patch("app.services.migrators.marzban.PASARGUARD_ENV", pg_dir / ".env"),
                patch("app.services.migrators.marzban.PASARGUARD_DATA", td_path / "pgdata"),
                patch("app.services.migrators.marzban.BACKUP_DIR", td_path / "bak"),
                patch.object(m, "_dump_marzban_mysql", fake_dump),
                patch.object(m, "_migrate_mysql_like_restore", fake_mysql_restore),
            ):
                (td_path / "bak").mkdir()
                await m._migrate("mysql", "mysql", None, True, None)

            assert captured["extra_data_dir"] == mz_data
            assert "XRAY_JSON" in captured["env"]

    _run(_case())
    print("OK: live mysql sets extra_data_dir + merges env")


def test_mysql_same_family_calls_post_boot_assert():
    async def _case():
        from app.services.migrators.base import MigrationJob
        from app.services.migrators.marzban import MarzbanMigrator

        with tempfile.TemporaryDirectory() as td:
            dump = Path(td) / "marzban.sql"
            dump.write_text("SELECT 1;\n", encoding="utf-8")
            m = MarzbanMigrator(
                MigrationJob(job_id="same-fam"),
                {"source_db": "mysql", "target_db": "mysql", "target_db_password": "x"},
            )
            calls = {"assert": 0}

            async def fake_assert(db):
                calls["assert"] += 1
                assert db == "mysql"

            with (
                patch("app.services.migrators.marzban.PASARGUARD_DIR", Path(td) / "pg"),
                patch("app.services.migrators.marzban.PASARGUARD_DATA", Path(td) / "pgdata"),
                patch("app.services.migrators.marzban.PASARGUARD_ENV", Path(td) / "pg" / ".env"),
                patch("app.services.migrators.marzban.BACKUP_DIR", Path(td) / "bak"),
                patch("app.services.migrators.marzban.safe_start_pasarguard", AsyncMock()),
                patch(
                    "app.services.marzban_preboot_heal.heal_marzban_preboot",
                    AsyncMock(return_value={}),
                ),
                patch.object(m, "_update_env_paths", AsyncMock()),
                patch.object(m, "_ensure_target_database_stack", AsyncMock()),
                patch.object(m, "_import_mysql_dump", AsyncMock()),
                patch.object(m, "_assert_target_pasarguard_ready", fake_assert),
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
                await m._migrate_mysql_like_restore(dump, "mysql", "mysql", None, "")

            assert calls["assert"] == 1

    _run(_case())
    print("OK: mysql same-family calls post-boot assert")


def test_copy_assets_pins_xray_json_and_skips_empty_certs():
    async def _case():
        m = _migrator()
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            pgdata = Path(td) / "pgdata"
            pg = Path(td) / "pg"
            src.mkdir()
            pgdata.mkdir()
            pg.mkdir()
            (src / "xray_config.json").write_text(
                '{"path":"/var/lib/marzban/certs/a.pem"}', encoding="utf-8"
            )
            (src / "certs").mkdir()  # empty — must not wipe good dst certs
            good = pgdata / "certs"
            good.mkdir()
            (good / "keep.pem").write_text("KEEP", encoding="utf-8")
            (pg / ".env").write_text("SQLALCHEMY_DATABASE_URL=sqlite:///x\n", encoding="utf-8")

            with (
                patch("app.services.migrators.marzban.PASARGUARD_DATA", pgdata),
                patch("app.services.migrators.marzban.PASARGUARD_ENV", pg / ".env"),
            ):
                await m._copy_marzban_assets(src)

            assert (pgdata / "xray_config.json").exists()
            text = (pgdata / "xray_config.json").read_text(encoding="utf-8")
            assert "/var/lib/pasarguard/certs/a.pem" in text
            assert (good / "keep.pem").read_text(encoding="utf-8") == "KEEP"
            env = (pg / ".env").read_text(encoding="utf-8")
            assert "/var/lib/pasarguard/xray_config.json" in env
            assert "XRAY_JSON" in env

    _run(_case())
    print("OK: asset copy pins XRAY_JSON and skips empty certs wipe")


def test_assert_shape_ready_unit():
    m = _migrator()
    m._assert_pasarguard_shape_ready(
        {"users": 1, "inbounds": 1, "core_configs": 1},
        tables_present={"users", "inbounds", "core_configs"},
        engine="mysql",
    )
    try:
        m._assert_pasarguard_shape_ready(
            {"users": 1, "hosts": 2},
            tables_present={"users", "hosts", "inbounds", "core_configs"},
            engine="mariadb",
        )
        raise AssertionError("expected failure")
    except RuntimeError as e:
        assert "inbounds empty" in str(e)


if __name__ == "__main__":
    test_assert_sqlite_rejects_users_without_inbounds()
    print("OK: rejects users without inbounds")
    test_assert_sqlite_rejects_users_without_core_configs()
    print("OK: rejects users without core_configs")
    test_assert_sqlite_accepts_users_with_inbounds_and_core()
    print("OK: accepts healthy shape")
    test_assert_sqlite_still_rejects_totally_empty()
    print("OK: rejects totally empty")
    test_abort_if_empty_convert_rejects_users_without_inbounds()
    print("OK: convert abort")
    test_abort_if_inbounds_missing_from_stats()
    print("OK: stats abort")
    test_live_mysql_sets_extra_data_dir_and_merges_env()
    test_mysql_same_family_calls_post_boot_assert()
    test_copy_assets_pins_xray_json_and_skips_empty_certs()
    test_assert_shape_ready_unit()
    print("OK: shape ready unit")
    print("All inbound-guard tests passed")
