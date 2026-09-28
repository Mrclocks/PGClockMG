#!/usr/bin/env python3
"""Live engine E2E (no Docker): sqlite→PG, sqlite→MySQL, MySQL→PG, large COPY path.

Uses locally installed PostgreSQL :5432 and MariaDB :3306.
Exit 0 only if all scenarios pass.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.integration_cross_db import (  # noqa: E402
    _create_mysql_full_schema,
    _create_pg_full_schema,
    _make_full_source_sqlite,
    _run_copy,
)


PG = {
    "host": "127.0.0.1",
    "port": "5432",
    "database": "pasarguard",
    "user": "test",
    "password": "test",
}
MY = {
    "host": "127.0.0.1",
    "port": "3306",
    "database": "pasarguard",
    "user": "root",
    "password": "test",
}


def _reset_pg() -> None:
    import psycopg2

    conn = psycopg2.connect(
        host=PG["host"], port=int(PG["port"]), dbname=PG["database"],
        user=PG["user"], password=PG["password"],
    )
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname='public'"
    )
    for (name,) in cur.fetchall():
        cur.execute(f'DROP TABLE IF EXISTS "{name}" CASCADE')
    _create_pg_full_schema(cur)
    conn.close()


def _reset_mysql() -> None:
    import pymysql

    conn = pymysql.connect(
        host=MY["host"], port=int(MY["port"]), user=MY["user"],
        password=MY["password"], database=MY["database"], autocommit=True,
    )
    cur = conn.cursor()
    cur.execute("SET FOREIGN_KEY_CHECKS=0")
    cur.execute("SHOW TABLES")
    for (name,) in cur.fetchall():
        cur.execute(f"DROP TABLE IF EXISTS `{name}`")
    cur.execute("SET FOREIGN_KEY_CHECKS=1")
    _create_mysql_full_schema(cur)
    conn.close()


def _sqlite_source() -> Path:
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    p = Path(path)
    _make_full_source_sqlite(p)
    return p


def _pg_counts() -> dict[str, int]:
    import psycopg2

    conn = psycopg2.connect(
        host=PG["host"], port=int(PG["port"]), dbname=PG["database"],
        user=PG["user"], password=PG["password"],
    )
    cur = conn.cursor()
    out = {}
    for t in ("users", "hosts", "nodes", "inbounds", "groups", "admins", "core_configs"):
        cur.execute(f'SELECT COUNT(*) FROM "{t}"')
        out[t] = int(cur.fetchone()[0])
    conn.close()
    return out


def _mysql_counts() -> dict[str, int]:
    import pymysql

    conn = pymysql.connect(
        host=MY["host"], port=int(MY["port"]), user=MY["user"],
        password=MY["password"], database=MY["database"],
    )
    cur = conn.cursor()
    out = {}
    for t in ("users", "hosts", "nodes", "inbounds", "groups", "admins", "core_configs"):
        cur.execute(f"SELECT COUNT(*) FROM `{t}`")
        out[t] = int(cur.fetchone()[0])
    conn.close()
    return out


def test_sqlite_to_postgres() -> None:
    _reset_pg()
    src = _sqlite_source()
    try:
        stats = _run_copy(src, "postgresql", PG)
        counts = _pg_counts()
        for t in ("users", "hosts", "nodes", "inbounds", "groups", "admins", "core_configs"):
            assert counts[t] >= 1, f"pg {t}={counts[t]} stats={stats}"
            assert stats.get(t, 0) == counts[t], (t, stats, counts)
        # enum 'none' must survive
        import psycopg2

        conn = psycopg2.connect(
            host=PG["host"], port=int(PG["port"]), dbname=PG["database"],
            user=PG["user"], password=PG["password"],
        )
        cur = conn.cursor()
        cur.execute("SELECT security, fingerprint FROM hosts WHERE id=1")
        sec, fp = cur.fetchone()
        conn.close()
        assert sec == "none", sec
        assert fp == "none", fp
        print(f"OK: live sqlite→postgresql counts={counts}")
    finally:
        src.unlink(missing_ok=True)


def test_sqlite_to_mysql() -> None:
    _reset_mysql()
    src = _sqlite_source()
    try:
        stats = _run_copy(src, "mysql", MY)
        counts = _mysql_counts()
        for t in ("users", "hosts", "nodes", "inbounds", "groups", "admins", "core_configs"):
            assert counts[t] >= 1, f"mysql {t}={counts[t]} stats={stats}"
            assert stats.get(t, 0) == counts[t], (t, stats, counts)
        print(f"OK: live sqlite→mysql counts={counts}")
    finally:
        src.unlink(missing_ok=True)


def test_mysql_to_postgres() -> None:
    import pymysql
    from app.services.native_migration.adapters import (
        create_reader, create_writer, copy_tables_universal,
    )

    _reset_mysql()
    _reset_pg()
    conn = pymysql.connect(
        host=MY["host"], port=int(MY["port"]), user=MY["user"],
        password=MY["password"], database=MY["database"], autocommit=True,
    )
    cur = conn.cursor()
    cur.execute("INSERT INTO admins VALUES (1, 'admin', 1)")
    cur.execute("INSERT INTO core_configs VALUES (1, 'xray')")
    cur.execute(
        "INSERT INTO nodes VALUES (1, 'n1', '1.2.3.4', 1, '', '', 'healthy')"
    )
    cur.execute("INSERT INTO inbounds VALUES (1, 'vless-tcp', 'vless', 0)")
    cur.execute("INSERT INTO groups VALUES (1, 'default', 0)")
    cur.execute(
        "INSERT INTO hosts VALUES (1, 'h1', 'vless-tcp', NULL, NULL, "
        "'{\\\"enabled\\\": true}', 0, 'none', 'none')"
    )
    cur.execute("INSERT INTO users VALUES (1, 'u1', 'active', 1, 1)")
    cur.execute("INSERT INTO users VALUES (2, 'u2', 'active', 1, 1)")
    conn.close()

    reader = create_reader("mysql", None, MY)
    writer = create_writer("postgresql", PG)
    try:
        stats, report = copy_tables_universal(
            reader, writer, lambda _m: None, fail_hard=True,
        )
    finally:
        reader.close()
        writer.close()

    assert not report.get("has_gaps"), report
    counts = _pg_counts()
    assert counts["users"] == 2, counts
    assert counts["inbounds"] == 1 and counts["core_configs"] == 1, counts
    print(f"OK: live mysql→postgresql counts={counts} stats={stats}")


def test_postgres_large_copy_path() -> None:
    """Prove COPY FROM path lands thousands of history rows accurately."""
    import psycopg2
    from app.services.native_migration.adapters import (
        SqliteReader, PostgresWriter, copy_tables_universal,
    )

    _reset_pg()
    # Add heavy history table on PG
    conn = psycopg2.connect(
        host=PG["host"], port=int(PG["port"]), dbname=PG["database"],
        user=PG["user"], password=PG["password"],
    )
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS node_user_usages ("
        "id SERIAL PRIMARY KEY, node_id INT, user_id INT, used_traffic BIGINT)"
    )
    conn.commit()
    conn.close()

    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    src = Path(path)
    try:
        _make_full_source_sqlite(src)
        sc = sqlite3.connect(src)
        sc.execute(
            "CREATE TABLE node_user_usages ("
            "id INTEGER PRIMARY KEY, node_id INT, user_id INT, used_traffic INT)"
        )
        sc.executemany(
            "INSERT INTO node_user_usages VALUES (?,?,?,?)",
            [(i, 1, 1, i * 10) for i in range(1, 1201)],
        )
        sc.commit()
        sc.close()

        reader = SqliteReader(str(src))
        writer = PostgresWriter(dict(PG))
        writer._COPY_MIN = 64
        writer._BATCH_FLUSH = 200
        logs: list[str] = []
        try:
            writer.begin_bulk_load()
            stats, report = copy_tables_universal(
                reader, writer, logs.append, fail_hard=True,
            )
            writer.end_bulk_load()
            writer.commit()
        finally:
            reader.close()
            writer.close()

        assert stats.get("node_user_usages") == 1200, stats
        assert not report.get("has_gaps"), report
        conn = psycopg2.connect(
            host=PG["host"], port=int(PG["port"]), dbname=PG["database"],
            user=PG["user"], password=PG["password"],
        )
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), SUM(used_traffic) FROM node_user_usages")
        n, total = cur.fetchone()
        conn.close()
        assert int(n) == 1200, n
        assert int(total) == sum(i * 10 for i in range(1, 1201)), total
        print(f"OK: live postgres COPY path node_user_usages={n} sum={total}")
    finally:
        src.unlink(missing_ok=True)


def test_change_db_gates_and_soft_family() -> None:
    from app.services.migrators.base import MigrationJob
    from app.services.migrators.pasarguard_db import PasarguardDbMigrator
    from app.services.pg_restore import soft_db_family
    from app.services.native_migration.sql_staging import _filter_timescaledb_extension_sql

    assert soft_db_family("postgresql", "timescaledb")
    assert soft_db_family("timescaledb", "postgresql")
    assert soft_db_family("mysql", "mariadb")
    assert not soft_db_family("sqlite", "postgresql")

    sql = "\n".join([
        "CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;",
        "CREATE TABLE users (id int);",
        "INSERT INTO users VALUES (1);",
    ])
    out = _filter_timescaledb_extension_sql(sql)
    assert "CREATE EXTENSION" not in out.upper()
    assert "CREATE TABLE users" in out

    m = PasarguardDbMigrator(MigrationJob(job_id="live-gate"), {"target_db": "postgresql"})
    m.copy_stats = {"users": 5, "admins": 1, "hosts": 0, "inbounds": 1, "nodes": 1, "groups": 1}
    m.copy_report = {
        "source_counts": {
            "users": 5, "admins": 1, "hosts": 3, "inbounds": 1, "nodes": 1, "groups": 1,
        },
        "has_gaps": False,
    }
    try:
        m._assert_convert_counts("sqlite", "postgresql")
        raise AssertionError("expected abort")
    except RuntimeError as e:
        assert "hosts" in str(e)
    print("OK: soft family + TS strip + Change-DB empty critical gate")


def test_marzban_orphan_and_usage_shrink() -> None:
    from app.services.marzban_preboot_heal import (
        cleanup_orphans_sqlite,
        shrink_heavy_usage_tables_sqlite,
    )

    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    p = Path(path)
    try:
        db = sqlite3.connect(p)
        db.executescript(
            """
            CREATE TABLE users (id INTEGER PRIMARY KEY);
            CREATE TABLE notification_reminders (
                id INTEGER PRIMARY KEY, user_id INTEGER
            );
            INSERT INTO users VALUES (1);
            INSERT INTO notification_reminders VALUES (1, 1);
            INSERT INTO notification_reminders VALUES (2, 99);
            CREATE TABLE node_user_usages (
                id INTEGER PRIMARY KEY, node_id INT, user_id INT
            );
            """
        )
        db.executemany(
            "INSERT INTO node_user_usages VALUES (?,?,?)",
            [(i, 1, 1) for i in range(1, 120)],
        )
        db.commit()
        db.close()
        deleted, nulled = cleanup_orphans_sqlite(p)
        assert deleted >= 1
        got = shrink_heavy_usage_tables_sqlite(p, row_threshold=50)
        assert any(t == "node_user_usages" for t, _n in got)
        print(f"OK: orphan cleanup deleted={deleted} shrink={got}")
    finally:
        p.unlink(missing_ok=True)


def main() -> int:
    tests = [
        test_change_db_gates_and_soft_family,
        test_marzban_orphan_and_usage_shrink,
        test_sqlite_to_postgres,
        test_sqlite_to_mysql,
        test_mysql_to_postgres,
        test_postgres_large_copy_path,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"FAIL: {fn.__name__}: {e}")
            import traceback
            traceback.print_exc()
    if failed:
        print(f"\n{failed}/{len(tests)} live E2E tests FAILED")
        return 1
    print(f"\nAll {len(tests)} live engine E2E tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
