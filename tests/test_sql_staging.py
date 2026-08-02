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


def test_timescale_never_stages_into_plain_postgres_compose():
    """Regression: resolve_db_service(timescaledb) may return postgresql — must NOT use it."""
    import asyncio
    from app.services.native_migration import sql_staging as mod

    calls = {"ephemeral": 0, "compose": 0}

    async def fake_ephemeral(migrator, dump_path, source_db, conn, staging_db, container):
        calls["ephemeral"] += 1
        assert source_db == "timescaledb"
        return {
            "host": "127.0.0.1",
            "port": "54331",
            "database": staging_db,
            "user": "postgres",
            "password": "pgmigrator",
            "_ephemeral_container": container,
        }

    async def fake_compose(*_a, **_k):
        calls["compose"] += 1
        raise AssertionError("must not stage Timescale dump into compose postgresql")

    class Job:
        def log(self, *_a, **_k):
            pass

    class Mini:
        def __init__(self):
            self.job = Job()

        async def _run_cmd(self, *a, **k):
            return True, ""

    orig_compose = mod._compose_text
    orig_resolve = mod.resolve_db_service
    orig_ephemeral = mod._import_via_ephemeral_postgres
    orig_via = mod._import_via_compose_service
    # Live panel is plain PostgreSQL — the buggy fallback path
    mod._compose_text = lambda: "services:\n  postgresql:\n    image: postgres:17\n  pgbouncer:\n    image: x\n"
    mod.resolve_db_service = lambda _db: "postgresql"
    mod._import_via_ephemeral_postgres = fake_ephemeral
    mod._import_via_compose_service = fake_compose

    tmp = Path("/tmp/pgmig_stage_ts_pg.sql")
    tmp.write_text(
        "CREATE EXTENSION timescaledb;\nCREATE TABLE users (id int);\n",
        encoding="utf-8",
    )
    try:
        result = asyncio.run(
            mod.import_sql_dump_to_live_db(
                Mini(), str(tmp), "timescaledb", {"password": "secret", "user": "pasarguard"},
            )
        )
        assert calls["ephemeral"] == 1
        assert calls["compose"] == 0
        assert result.get("_ephemeral_container")
        print("OK: Timescale dump never stages into plain postgresql compose")
    finally:
        mod._compose_text = orig_compose
        mod.resolve_db_service = orig_resolve
        mod._import_via_ephemeral_postgres = orig_ephemeral
        mod._import_via_compose_service = orig_via
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


def test_mysql_shell_e_arg_preserves_backticks():
    """Regression: double-quoted -e ate backticks → CREATE DATABASE ;"""
    import subprocess
    from app.services.native_migration.sql_staging import (
        mysql_create_db_sql,
        mysql_shell_e_arg,
    )

    staging_db = "pgmig_89b78bab"
    sql = mysql_create_db_sql(staging_db)
    assert f"`{staging_db}`" in sql

    # Buggy historical pattern (double quotes) expands backticks away
    buggy = subprocess.run(
        f'echo "CREATE DATABASE `{staging_db}`;"',
        shell=True, capture_output=True, text=True,
    )
    assert "CREATE DATABASE ;" in buggy.stdout, buggy.stdout

    # Fixed pattern (single quotes via helper) must keep identifier
    fixed = subprocess.run(
        f"echo {mysql_shell_e_arg(sql)}",
        shell=True, capture_output=True, text=True,
    )
    assert f"`{staging_db}`" in fixed.stdout, fixed.stdout
    assert "CREATE DATABASE ;" not in fixed.stdout
    print("OK: mysql_shell_e_arg preserves backticks under shell")


def test_mysql_create_db_sql_drop_first():
    from app.services.native_migration.sql_staging import mysql_create_db_sql

    s = mysql_create_db_sql("pgmig_abc123", drop_first=True)
    assert "DROP DATABASE IF EXISTS `pgmig_abc123`" in s
    assert "CREATE DATABASE `pgmig_abc123`" in s
    try:
        mysql_create_db_sql("evil;drop")
        assert False, "expected invalid name"
    except RuntimeError:
        pass
    print("OK: mysql_create_db_sql validates names")


def test_prepare_mysql_dump_strips_use_and_create_database():
    """Regression: USE pasarguard diverts data away from pgmig_* staging DB."""
    from app.services.native_migration.sql_staging import prepare_mysql_dump_for_staging

    raw = "\n".join([
        "-- MySQL dump",
        "CREATE DATABASE /*!32312 IF NOT EXISTS*/ `pasarguard` /*!40100 DEFAULT CHARACTER SET utf8mb4 */;",
        "USE `pasarguard`;",
        "DROP DATABASE IF EXISTS `old`;",
        "CREATE TABLE users (id INT);",
        "INSERT INTO users VALUES (1);",
        "USE marzban;",
        "",
    ])
    out, stripped = prepare_mysql_dump_for_staging(raw, "pgmig_deadbeef")
    assert stripped >= 3, stripped
    assert "USE `pasarguard`" not in out
    assert "USE marzban" not in out
    assert "CREATE DATABASE" not in out
    assert "DROP DATABASE" not in out
    assert "CREATE TABLE users" in out
    assert "INSERT INTO users VALUES (1);" in out
    print("OK: prepare_mysql_dump strips USE/CREATE/DROP DATABASE")


def test_mysql_ephemeral_create_uses_exec_not_shell():
    """CREATE DATABASE must go through docker exec argv, never shell -e."""
    import asyncio
    from app.services.native_migration import sql_staging as mod

    calls: list[tuple] = []

    async def fake_mysql(container, pwd, *, sql=None, database=None, stdin_path=None, client="mysql"):
        calls.append({"sql": sql, "database": database, "stdin": stdin_path, "client": client})
        return 0, "ok"

    async def fake_ready(container, pwd, attempts=90, *, client="mysql", admin_client="mysqladmin"):
        return None

    async def fake_verify(*_a, **_k):
        return None

    class Job:
        def log(self, *_a, **_k):
            pass

    class Mini:
        def __init__(self):
            self.job = Job()

        async def _run_cmd(self, *a, **k):
            return True, "cid"

    tmp = Path("/tmp/pgmig_mysql_ephemeral_test.sql")
    tmp.write_text("CREATE TABLE t (id int);\n", encoding="utf-8")

    orig_mysql = mod._mysql_ephemeral
    orig_ready = mod._wait_ephemeral_mysql_ready
    orig_verify = mod._verify_mysql_staging_has_data
    mod._mysql_ephemeral = fake_mysql
    mod._wait_ephemeral_mysql_ready = fake_ready
    mod._verify_mysql_staging_has_data = fake_verify
    try:
        result = asyncio.run(
            mod._import_via_ephemeral_mysql(
                Mini(), tmp, {}, "pgmig_89b78bab", "pgmig-mysql-test", "mysql",
            )
        )
        assert result["database"] == "pgmig_89b78bab"
        assert result.get("_ephemeral_container") == "pgmig-mysql-test"
        assert result.get("_ephemeral_image") == "mysql:8"
        assert any(
            c["sql"] and "CREATE DATABASE `pgmig_89b78bab`" in c["sql"]
            for c in calls
        ), calls
        assert any(c["stdin"] == tmp for c in calls), calls
        # Must not use shell-eaten empty CREATE DATABASE
        assert not any(c["sql"] == "CREATE DATABASE ;" for c in calls)
        print("OK: ephemeral MySQL create uses exec helper with intact backticks")
    finally:
        mod._mysql_ephemeral = orig_mysql
        mod._wait_ephemeral_mysql_ready = orig_ready
        mod._verify_mysql_staging_has_data = orig_verify
        tmp.unlink(missing_ok=True)


def test_ephemeral_mariadb_uses_mariadb_image():
    """MariaDB dumps must stage into mariadb:11 (not mysql:8) for collations."""
    import asyncio
    from app.services.native_migration import sql_staging as mod

    assert mod._ephemeral_mysql_runtime("mariadb") == (
        "mariadb:11", "mariadb", "mariadb-admin",
    )
    assert mod._ephemeral_mysql_runtime("mysql") == (
        "mysql:8", "mysql", "mysqladmin",
    )

    run_cmds: list[list] = []

    async def fake_mysql(container, pwd, *, sql=None, database=None, stdin_path=None, client="mysql"):
        assert client == "mariadb"
        return 0, "ok"

    async def fake_ready(container, pwd, attempts=90, *, client="mysql", admin_client="mysqladmin"):
        assert client == "mariadb"
        assert admin_client == "mariadb-admin"

    async def fake_verify(*_a, **_k):
        return None

    class Job:
        def log(self, *_a, **_k):
            pass

    class Mini:
        def __init__(self):
            self.job = Job()

        async def _run_cmd(self, cmd, timeout=60, cwd=None):
            run_cmds.append(list(cmd))
            return True, "ok"

    tmp = Path("/tmp/pgmig_maria_ephemeral_test.sql")
    tmp.write_text("CREATE TABLE t (id int);\n", encoding="utf-8")
    orig_mysql = mod._mysql_ephemeral
    orig_ready = mod._wait_ephemeral_mysql_ready
    orig_verify = mod._verify_mysql_staging_has_data
    mod._mysql_ephemeral = fake_mysql
    mod._wait_ephemeral_mysql_ready = fake_ready
    mod._verify_mysql_staging_has_data = fake_verify
    try:
        result = asyncio.run(
            mod._import_via_ephemeral_mysql(
                Mini(), tmp, {}, "pgmig_deadbeef", "pgmig-mysql-maria", "mariadb",
            )
        )
        assert result["_ephemeral_image"] == "mariadb:11"
        docker_run = next(c for c in run_cmds if c[:2] == ["docker", "run"])
        assert "mariadb:11" in docker_run
        print("OK: ephemeral MariaDB staging uses mariadb:11 + mariadb client")
    finally:
        mod._mysql_ephemeral = orig_mysql
        mod._wait_ephemeral_mysql_ready = orig_ready
        mod._verify_mysql_staging_has_data = orig_verify
        tmp.unlink(missing_ok=True)


def test_import_mysql_dump_routes_to_ephemeral_when_target_is_timescale():
    """MariaDB/MySQL → Timescale: no mysql compose service → ephemeral MySQL."""
    import asyncio
    from app.services.native_migration import sql_staging as mod

    calls = {"ephemeral": 0, "sources": []}

    async def fake_ephemeral(migrator, dump_path, conn, staging_db, container, source_db="mysql"):
        calls["ephemeral"] += 1
        calls["sources"].append(source_db)
        assert staging_db.startswith("pgmig_")
        assert container.startswith("pgmig-mysql-")
        return {
            "host": "127.0.0.1",
            "port": "33060",
            "database": staging_db,
            "user": "root",
            "password": "pgmigrator",
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

    orig_compose = mod._compose_text
    orig_resolve = mod.resolve_db_service
    orig_ephemeral = mod._import_via_ephemeral_mysql
    # Live panel is Timescale only — matches reported MariaDB→Timescale migration
    mod._compose_text = lambda: (
        "services:\n  timescaledb:\n    image: timescale/timescaledb:latest-pg17\n"
    )
    mod.resolve_db_service = lambda db: "timescaledb" if db == "timescaledb" else None
    mod._import_via_ephemeral_mysql = fake_ephemeral

    tmp = Path("/tmp/pgmig_mariadb_to_ts.sql")
    tmp.write_text("CREATE TABLE users (id int);\nINSERT INTO users VALUES (1);\n", encoding="utf-8")
    try:
        for source in ("mysql", "mariadb"):
            calls["ephemeral"] = 0
            result = asyncio.run(
                mod.import_sql_dump_to_live_db(
                    Mini(), str(tmp), source, {"password": "x"},
                )
            )
            assert calls["ephemeral"] == 1, source
            assert calls["sources"][-1] == source
            assert result.get("_ephemeral_container")
        print("OK: mysql/mariadb dump routes to ephemeral MySQL when compose has timescale only")
    finally:
        mod._compose_text = orig_compose
        mod.resolve_db_service = orig_resolve
        mod._import_via_ephemeral_mysql = orig_ephemeral
        tmp.unlink(missing_ok=True)


def test_mysql_ephemeral_passes_create_sql_as_exec_argv():
    """_mysql_ephemeral must invoke create_subprocess_exec with -e SQL intact."""
    import asyncio
    from app.services.native_migration import sql_staging as mod

    captured: list[list[str]] = []

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", None

    async def fake_exec(*argv, **kwargs):
        captured.append(list(argv))
        return FakeProc()

    orig = asyncio.create_subprocess_exec
    asyncio.create_subprocess_exec = fake_exec
    try:
        sql = "CREATE DATABASE `pgmig_89b78bab`;"
        rc, _ = asyncio.run(mod._mysql_ephemeral("c", "pgmigrator", sql=sql))
        assert rc == 0
        assert captured, "expected docker exec invocation"
        argv = captured[0]
        assert "docker" in argv[0]
        assert "exec" in argv
        assert "mysql" in argv
        assert "-e" in argv
        e_idx = argv.index("-e")
        assert argv[e_idx + 1] == sql
        assert "`pgmig_89b78bab`" in argv[e_idx + 1]
        # Must not go through a shell string
        assert not any("CREATE DATABASE ;" in a for a in argv)
        print("OK: _mysql_ephemeral passes CREATE DATABASE via exec argv")
    finally:
        asyncio.create_subprocess_exec = orig


if __name__ == "__main__":
    test_filter_timescaledb_extension_in_staging()
    test_compose_has_service_helper()
    test_explain_cannot_stage_timescale()
    test_import_sql_dump_routes_to_ephemeral_pg()
    test_timescale_never_stages_into_plain_postgres_compose()
    test_create_staging_db_runs_drop_and_create_separately()
    test_transient_pg_error_detection()
    test_mysql_shell_e_arg_preserves_backticks()
    test_mysql_create_db_sql_drop_first()
    test_prepare_mysql_dump_strips_use_and_create_database()
    test_mysql_ephemeral_create_uses_exec_not_shell()
    test_ephemeral_mariadb_uses_mariadb_image()
    test_import_mysql_dump_routes_to_ephemeral_when_target_is_timescale()
    test_mysql_ephemeral_passes_create_sql_as_exec_argv()
    print("\nAll sql staging tests passed.")
