"""Marzban MySQL dump must land in the client-selected PasarGuard DB.

User report: Marzban(MySQL) → PasarGuard(MySQL) finished without error, but
only core config arrived — no users/hosts. The dump's ``CREATE DATABASE``/
``USE`` lines diverted rows away from the database the panel boots on.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.env_migration import (  # noqa: E402
    pick_marzban_db_from_mysql_dump,
    rewrite_marzban_mysql_dump_for_target_db,
)
from app.services.migrators.base import MigrationJob  # noqa: E402
from app.services.migrators.marzban import MarzbanMigrator  # noqa: E402

MARZBAN_DUMP = """-- MySQL dump 10.13
/*!40101 SET NAMES utf8mb4 */;
SET @@GLOBAL.GTID_PURGED=/*!80000 '+'*/ '';
--
-- Current Database: `marzban`
--
/*!40000 DROP DATABASE IF EXISTS `marzban`*/;
CREATE DATABASE /*!32312 IF NOT EXISTS*/ `marzban` /*!40100 DEFAULT CHARACTER SET utf8mb4 */ /*!80016 DEFAULT ENCRYPTION='N' */;
USE `marzban`;
CREATE TABLE `admins` (`id` int NOT NULL);
CREATE TABLE `users` (`id` int NOT NULL, `username` varchar(34));
INSERT INTO `users` VALUES (1,'a'),(2,'b'),(3,'c');
INSERT INTO `admins` VALUES (1);
"""

ALL_DBS_DUMP = """-- dump --all-databases
CREATE DATABASE /*!32312 IF NOT EXISTS*/ `mysql`;
USE `mysql`;
CREATE TABLE `user` (`Host` char(255));
INSERT INTO `user` VALUES ('localhost');
CREATE DATABASE /*!32312 IF NOT EXISTS*/ `marzban_db`;
USE `marzban_db`;
CREATE TABLE `users` (`id` int);
CREATE TABLE `admins` (`id` int);
INSERT INTO `users` VALUES (1),(2);
CREATE DATABASE /*!32312 IF NOT EXISTS*/ `sys`;
USE `sys`;
CREATE TABLE `sys_config` (`variable` varchar(128));
"""


def _exec_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("--")]


def test_strips_db_switch_statements(tmp_path):
    src = tmp_path / "marzban.sql"
    dest = tmp_path / "fixed_import.sql"
    src.write_text(MARZBAN_DUMP, encoding="utf-8")
    stats = rewrite_marzban_mysql_dump_for_target_db(src, dest)
    out = dest.read_text(encoding="utf-8")
    for ln in _exec_lines(out):
        up = ln.upper()
        assert not up.lstrip().startswith("USE "), ln
        assert "CREATE DATABASE" not in up, ln
        assert "DROP DATABASE" not in up, ln
        assert "GTID_PURGED" not in up, ln
    assert "INSERT INTO `users` VALUES (1,'a'),(2,'b'),(3,'c');" in out
    assert stats["db_statements_stripped"] == 3
    assert stats["kept_db"] == "marzban"


def test_multi_db_dump_keeps_only_marzban_section(tmp_path):
    src = tmp_path / "all.sql"
    dest = tmp_path / "fixed_import.sql"
    src.write_text(ALL_DBS_DUMP, encoding="utf-8")
    keep, seen = pick_marzban_db_from_mysql_dump(src)
    assert keep == "marzban_db"
    assert seen == ["mysql", "marzban_db", "sys"]
    stats = rewrite_marzban_mysql_dump_for_target_db(src, dest)
    out = dest.read_text(encoding="utf-8")
    assert "INSERT INTO `users` VALUES (1),(2);" in out
    assert "`user`" not in out
    assert "sys_config" not in out
    assert stats["skipped_lines"] >= 3


def test_plain_dump_without_use_is_kept_whole(tmp_path):
    src = tmp_path / "marzban.sql"
    dest = tmp_path / "out.sql"
    src.write_text("CREATE TABLE `users` (`id` int);\nINSERT INTO `users` VALUES (1);\n")
    stats = rewrite_marzban_mysql_dump_for_target_db(src, dest)
    assert stats["kept_db"] is None
    assert "INSERT INTO `users` VALUES (1);" in dest.read_text()


def test_pick_marzban_sql_prefers_file_with_users(tmp_path):
    (tmp_path / "a_mysql.sql").write_text(
        "CREATE TABLE `user` (`Host` char(255));\nINSERT INTO `user` VALUES ('x');\n"
    )
    sub = tmp_path / "db-backup"
    sub.mkdir()
    (sub / "marzban.sql").write_text(MARZBAN_DUMP)
    m = MarzbanMigrator(MigrationJob(job_id="pick"), {"target_db": "mysql"})
    assert m._pick_marzban_sql(tmp_path) == sub / "marzban.sql"


def test_post_boot_refuses_core_only_result():
    m = MarzbanMigrator(MigrationJob(job_id="shape"), {"target_db": "mysql"})
    m._expected_dump_counts = {"users": 3}
    with pytest.raises(RuntimeError, match="3 users"):
        m._assert_pasarguard_shape_ready(
            {"inbounds": 4, "core_configs": 1},
            tables_present={"users", "inbounds", "core_configs"},
            engine="mysql",
        )


def test_verify_import_counts_raises_on_zero_users(monkeypatch):
    m = MarzbanMigrator(MigrationJob(job_id="verify"), {"target_db": "mysql"})

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"0\t1\n", None

    async def fake_exec(*_a, **_k):
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(RuntimeError, match="0 landed"):
        asyncio.run(m._verify_mysql_import_counts(
            "mysql", "mysql", "root", "pw", "127.0.0.1", "pasarguard", {"users": 5},
        ))

    class _Ok(_Proc):
        async def communicate(self):
            return b"5\t1\n", None

    async def ok_exec(*_a, **_k):
        return _Ok()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", ok_exec)
    asyncio.run(m._verify_mysql_import_counts(
        "mysql", "mysql", "root", "pw", "127.0.0.1", "pasarguard", {"users": 5},
    ))


def test_usernames_and_hosts_containing_marzban_are_preserved():
    """PasarGuard still resolves legacy Marzban sub links by exact username."""
    from app.services.env_migration import fix_mysql_dump_for_pasarguard

    sql = (
        "USE `marzban`;\n"
        "INSERT INTO `users` VALUES (1,'marzban_ali'),(2,'Marzban.vip');\n"
        "INSERT INTO `admins` VALUES (1,'marzban');\n"
        "INSERT INTO `hosts` VALUES (1,'marzban.example.com','/var/lib/marzban/certs/a.pem');\n"
    )
    out = fix_mysql_dump_for_pasarguard(sql)
    assert "USE `pasarguard`;" in out
    assert "'marzban_ali'" in out and "'Marzban.vip'" in out
    assert "(1,'marzban')" in out
    assert "'marzban.example.com'" in out
    assert "/var/lib/pasarguard/certs/a.pem" in out
