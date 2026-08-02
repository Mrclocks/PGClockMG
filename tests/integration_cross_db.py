"""Integration tests: SQLite → Postgres / MySQL / MariaDB via copy_tables_universal.

Requires Docker. Skips cleanly when Docker is unavailable.
Run: python tests/integration_cross_db.py
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _docker_available() -> bool:
    try:
        r = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=20,
        )
        return r.returncode == 0
    except Exception:
        return False


def _make_full_source_sqlite(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);
        INSERT INTO alembic_version VALUES ('deadbeefcafe');

        CREATE TABLE admins (id INTEGER PRIMARY KEY, username TEXT, is_sudo INTEGER);
        INSERT INTO admins VALUES (1, 'admin', 1);

        CREATE TABLE core_configs (id INTEGER PRIMARY KEY, name TEXT);
        INSERT INTO core_configs VALUES (1, 'xray');

        CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, address TEXT, core_config_id INTEGER);
        INSERT INTO nodes VALUES (1, 'n1', '1.2.3.4', 1);

        CREATE TABLE inbounds (id INTEGER PRIMARY KEY, tag TEXT, protocol TEXT, is_disabled INTEGER);
        INSERT INTO inbounds VALUES (1, 'vless-tcp', 'vless', 0);

        CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT, is_disabled INTEGER);
        INSERT INTO groups VALUES (1, 'default', 0);

        CREATE TABLE hosts (
            id INTEGER PRIMARY KEY, remark TEXT, inbound_tag TEXT,
            fragment_setting TEXT, noise_setting TEXT, mux_enable INTEGER,
            security TEXT, fingerprint TEXT
        );
        INSERT INTO hosts VALUES (1, 'h1', 'vless-tcp', 'bad', 'bad', 1, 'none', 'none');

        CREATE TABLE users (
            id INTEGER PRIMARY KEY, username TEXT, status TEXT, enable INTEGER, admin_id INTEGER
        );
        INSERT INTO users VALUES (1, 'u1', 'active', 1, 1);
        INSERT INTO users VALUES (2, 'u2', 'active', 1, 1);
        """
    )
    conn.commit()
    conn.close()


def _create_pg_full_schema(cur) -> None:
    cur.execute(
        """
        CREATE TABLE admins (id SERIAL PRIMARY KEY, username TEXT, is_sudo BOOLEAN);
        CREATE TABLE core_configs (id SERIAL PRIMARY KEY, name TEXT);
        CREATE TABLE nodes (
            id SERIAL PRIMARY KEY, name TEXT, address TEXT,
            core_config_id INT, server_ca TEXT NOT NULL DEFAULT '', api_key TEXT, status TEXT
        );
        CREATE TABLE inbounds (
            id SERIAL PRIMARY KEY, tag TEXT, protocol TEXT, is_disabled BOOLEAN DEFAULT FALSE
        );
        CREATE TABLE groups (
            id SERIAL PRIMARY KEY, name TEXT, is_disabled BOOLEAN DEFAULT FALSE
        );
        CREATE TABLE hosts (
            id SERIAL PRIMARY KEY, remark TEXT, inbound_tag TEXT,
            fragment_settings JSONB, noise_settings JSONB, mux_settings JSONB,
            priority INT NOT NULL DEFAULT 0, security TEXT, fingerprint TEXT
        );
        CREATE TABLE users (
            id SERIAL PRIMARY KEY, username TEXT, status TEXT, enable BOOLEAN, admin_id INT
        );
        CREATE TABLE alembic_version (version_num VARCHAR(32));
        """
    )


def _create_mysql_full_schema(cur) -> None:
    cur.execute(
        "CREATE TABLE admins (id INT PRIMARY KEY AUTO_INCREMENT, username VARCHAR(64), is_sudo TINYINT(1))"
    )
    cur.execute("CREATE TABLE core_configs (id INT PRIMARY KEY AUTO_INCREMENT, name VARCHAR(64))")
    cur.execute(
        """
        CREATE TABLE nodes (
            id INT PRIMARY KEY AUTO_INCREMENT, name VARCHAR(64), address VARCHAR(255),
            core_config_id INT, server_ca TEXT NOT NULL, api_key VARCHAR(255), status VARCHAR(32)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE inbounds (
            id INT PRIMARY KEY AUTO_INCREMENT, tag VARCHAR(64),
            protocol VARCHAR(32), is_disabled TINYINT(1) DEFAULT 0
        )
        """
    )
    cur.execute(
        "CREATE TABLE groups (id INT PRIMARY KEY AUTO_INCREMENT, name VARCHAR(64), is_disabled TINYINT(1) DEFAULT 0)"
    )
    cur.execute(
        """
        CREATE TABLE hosts (
            id INT PRIMARY KEY AUTO_INCREMENT, remark VARCHAR(255), inbound_tag VARCHAR(64),
            fragment_settings JSON, noise_settings JSON, mux_settings JSON,
            priority INT NOT NULL DEFAULT 0, security VARCHAR(32), fingerprint VARCHAR(32)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE users (
            id INT PRIMARY KEY AUTO_INCREMENT, username VARCHAR(64),
            status VARCHAR(32), enable TINYINT(1), admin_id INT
        )
        """
    )
    cur.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")


def test_sqlite_to_postgres_full_schema():
    import psycopg2

    name = f"pgmig-full-pg-{uuid.uuid4().hex[:8]}"
    port = 55434
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "-e", "POSTGRES_PASSWORD=test",
            "-e", "POSTGRES_USER=test",
            "-e", "POSTGRES_DB=pasarguard",
            "-p", f"{port}:5432",
            "postgres:16-alpine",
        ],
        check=True,
        capture_output=True,
    )
    try:
        _wait_port("127.0.0.1", port)
        time.sleep(3)
        conn = psycopg2.connect(
            host="127.0.0.1", port=port, dbname="pasarguard",
            user="test", password="test",
        )
        cur = conn.cursor()
        _create_pg_full_schema(cur)
        conn.commit()
        conn.close()

        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        sqlite_path = Path(path)
        try:
            _make_full_source_sqlite(sqlite_path)
            dsn = {
                "host": "127.0.0.1", "port": str(port),
                "database": "pasarguard", "user": "test", "password": "test",
            }
            stats = _run_copy(sqlite_path, "postgresql", dsn)
            for tbl in ("users", "hosts", "nodes", "inbounds", "groups"):
                assert stats.get(tbl, 0) >= 1, f"{tbl}: {stats}"
            print("OK: sqlite → postgresql (full schema)")
        finally:
            sqlite_path.unlink(missing_ok=True)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_sqlite_to_mysql_full_schema(label: str = "mysql", image: str = "mysql:8", port: int = 33072):
    import pymysql

    name = f"pgmig-full-{label}-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "-e", "MYSQL_ROOT_PASSWORD=test",
            "-e", "MYSQL_DATABASE=pasarguard",
            "-p", f"{port}:3306",
            image,
        ],
        check=True,
        capture_output=True,
    )
    try:
        _wait_port("127.0.0.1", port, timeout=90)
        time.sleep(8)
        conn = pymysql.connect(
            host="127.0.0.1", port=port, user="root",
            password="test", database="pasarguard",
        )
        cur = conn.cursor()
        _create_mysql_full_schema(cur)
        conn.commit()
        conn.close()

        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        sqlite_path = Path(path)
        try:
            _make_full_source_sqlite(sqlite_path)
            dsn = {
                "host": "127.0.0.1", "port": str(port),
                "database": "pasarguard", "user": "root", "password": "test",
            }
            engine = "mariadb" if "maria" in label else "mysql"
            stats = _run_copy(sqlite_path, engine, dsn)
            for tbl in ("users", "hosts", "nodes", "inbounds", "groups"):
                assert stats.get(tbl, 0) >= 1, f"{tbl}: {stats}"
            print(f"OK: sqlite → {label} (full schema)")
        finally:
            sqlite_path.unlink(missing_ok=True)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_mysql_to_postgres_full_schema():
    """Cross-family: MySQL reader → PostgreSQL writer."""
    import psycopg2
    import pymysql

    pg_name = f"pgmig-m2p-pg-{uuid.uuid4().hex[:8]}"
    my_name = f"pgmig-m2p-my-{uuid.uuid4().hex[:8]}"
    pg_port, my_port = 55435, 33073
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", pg_name,
            "-e", "POSTGRES_PASSWORD=test", "-e", "POSTGRES_USER=test",
            "-e", "POSTGRES_DB=pasarguard", "-p", f"{pg_port}:5432",
            "postgres:16-alpine",
        ],
        check=True, capture_output=True,
    )
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", my_name,
            "-e", "MYSQL_ROOT_PASSWORD=test", "-e", "MYSQL_DATABASE=pasarguard",
            "-p", f"{my_port}:3306", "mysql:8",
        ],
        check=True, capture_output=True,
    )
    try:
        _wait_port("127.0.0.1", pg_port)
        _wait_port("127.0.0.1", my_port, timeout=90)
        time.sleep(10)

        my_conn = pymysql.connect(
            host="127.0.0.1", port=my_port, user="root",
            password="test", database="pasarguard",
        )
        cur = my_conn.cursor()
        _create_mysql_full_schema(cur)
        cur.execute("INSERT INTO admins VALUES (1, 'admin', 1)")
        cur.execute("INSERT INTO core_configs VALUES (1, 'xray')")
        cur.execute(
            "INSERT INTO nodes VALUES (1, 'n1', '1.2.3.4', 1, '', '', 'healthy')"
        )
        cur.execute("INSERT INTO inbounds VALUES (1, 'vless-tcp', 'vless', 0)")
        cur.execute("INSERT INTO groups VALUES (1, 'default', 0)")
        cur.execute(
            "INSERT INTO hosts VALUES (1, 'h1', 'vless-tcp', NULL, NULL, "
            "'{\"enabled\": true}', 0, 'none', 'none')"
        )
        cur.execute("INSERT INTO users VALUES (1, 'u1', 'active', 1, 1)")
        my_conn.commit()
        my_conn.close()

        pg_conn = psycopg2.connect(
            host="127.0.0.1", port=pg_port, dbname="pasarguard",
            user="test", password="test",
        )
        cur = pg_conn.cursor()
        _create_pg_full_schema(cur)
        pg_conn.commit()
        pg_conn.close()

        from app.services.native_migration.adapters import (
            create_reader, create_writer, copy_tables_universal,
        )
        my_dsn = {
            "host": "127.0.0.1", "port": str(my_port),
            "database": "pasarguard", "user": "root", "password": "test",
        }
        pg_dsn = {
            "host": "127.0.0.1", "port": str(pg_port),
            "database": "pasarguard", "user": "test", "password": "test",
        }
        reader = create_reader("mysql", None, my_dsn)
        writer = create_writer("postgresql", pg_dsn)
        try:
            stats, _ = copy_tables_universal(
                reader, writer, lambda _m: None, fail_hard=True,
            )
        finally:
            reader.close()
            writer.close()

        for tbl in ("users", "hosts", "nodes", "inbounds", "groups"):
            assert stats.get(tbl, 0) >= 1, f"{tbl}: {stats}"
        print("OK: mysql → postgresql (full schema)")
    finally:
        subprocess.run(["docker", "rm", "-f", pg_name], capture_output=True)
        subprocess.run(["docker", "rm", "-f", my_name], capture_output=True)


def _timescale_run_args(name: str, port: int) -> list[str]:
    """Start Timescale; skip timescaledb-tune (panics without cgroup memory)."""
    return [
        "docker", "run", "-d", "--name", name,
        "-e", "POSTGRES_PASSWORD=test",
        "-e", "POSTGRES_USER=postgres",
        "-e", "POSTGRES_DB=pasarguard",
        "-p", f"127.0.0.1:{port}:5432",
        "-v", "/dev/null:/docker-entrypoint-initdb.d/001_timescaledb_tune.sh:ro",
        "timescale/timescaledb:latest-pg16",
    ]


def _wait_mysql_ready(port: int, password: str = "test", database: str = "pasarguard", timeout: float = 120):
    import pymysql

    _wait_port("127.0.0.1", port, timeout=timeout)
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            conn = pymysql.connect(
                host="127.0.0.1", port=port, user="root",
                password=password, database=database,
            )
            conn.close()
            return
        except Exception as exc:
            last = exc
            time.sleep(2)
    raise RuntimeError(f"MySQL/MariaDB not ready on {port}: {last}")


def _wait_pg_ready(port: int, user: str = "postgres", password: str = "test",
                   database: str = "pasarguard", timeout: float = 120):
    import psycopg2

    _wait_port("127.0.0.1", port, timeout=timeout)
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            conn = psycopg2.connect(
                host="127.0.0.1", port=port, dbname=database,
                user=user, password=password,
            )
            conn.close()
            return
        except Exception as exc:
            last = exc
            time.sleep(2)
    raise RuntimeError(f"Postgres/Timescale not ready on {port}: {last}")


def _mariadb_seed_full(port: int) -> None:
    import pymysql

    conn = pymysql.connect(
        host="127.0.0.1", port=port, user="root",
        password="test", database="pasarguard",
    )
    cur = conn.cursor()
    _create_mysql_full_schema(cur)
    cur.execute("INSERT INTO admins VALUES (1, 'admin', 1)")
    cur.execute("INSERT INTO core_configs VALUES (1, 'xray')")
    cur.execute(
        "INSERT INTO nodes VALUES (1, 'n1', '1.2.3.4', 1, '', '', 'healthy')"
    )
    cur.execute("INSERT INTO inbounds VALUES (1, 'vless-tcp', 'vless', 0)")
    cur.execute("INSERT INTO groups VALUES (1, 'default', 0)")
    cur.execute(
        "INSERT INTO hosts VALUES (1, 'h1', 'vless-tcp', NULL, NULL, "
        "'{\"enabled\": true}', 0, 'none', 'none')"
    )
    cur.execute("INSERT INTO users VALUES (1, 'u1', 'active', 1, 1)")
    cur.execute("INSERT INTO users VALUES (2, 'u2', 'active', 1, 1)")
    cur.execute("INSERT INTO alembic_version VALUES ('deadbeefcafe')")
    conn.commit()
    conn.close()


def _timescale_prepare_schema(port: int) -> None:
    import psycopg2

    conn = psycopg2.connect(
        host="127.0.0.1", port=port, dbname="pasarguard",
        user="postgres", password="test",
    )
    cur = conn.cursor()
    cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;")
    _create_pg_full_schema(cur)
    conn.commit()
    conn.close()


def test_ephemeral_mysql_create_database_real_docker():
    """Regression for reported failure: CREATE DATABASE on ephemeral mysql:8.

    Exact user path: MariaDB dump + Timescale-only compose → ephemeral MySQL staging.
    """
    import asyncio
    import pymysql
    from app.services.native_migration import sql_staging as mod

    dump = Path(tempfile.mkstemp(suffix=".sql")[1])
    dump.write_text(
        "CREATE TABLE users (id INT PRIMARY KEY, username VARCHAR(64));\n"
        "INSERT INTO users VALUES (1, 'from_mariadb_dump');\n"
        "CREATE TABLE admins (id INT PRIMARY KEY, username VARCHAR(64), is_sudo TINYINT);\n"
        "INSERT INTO admins VALUES (1, 'admin', 1);\n",
        encoding="utf-8",
    )
    container = None  # filled after import
    logs: list[str] = []
    result = None

    class Job:
        def log(self, msg, *_a, **_k):
            logs.append(str(msg))

    class Mini:
        def __init__(self):
            self.job = Job()

        async def _run_cmd(self, cmd, timeout=60, cwd=None):
            try:
                r = subprocess.run(
                    cmd, capture_output=True, timeout=timeout, cwd=cwd,
                )
                out = (r.stdout or b"").decode() + (r.stderr or b"").decode()
                return r.returncode == 0, out
            except Exception as exc:
                return False, str(exc)

    # Simulate Timescale-only compose (no mysql/mariadb service)
    orig_compose = mod._compose_text
    orig_resolve = mod.resolve_db_service
    mod._compose_text = lambda: (
        "services:\n  timescaledb:\n    image: timescale/timescaledb:latest-pg16\n"
    )
    mod.resolve_db_service = lambda db: "timescaledb" if db == "timescaledb" else None

    try:
        result = asyncio.run(
            mod.import_sql_dump_to_live_db(
                Mini(), str(dump), "mariadb", {"password": "x"},
            )
        )
        assert result.get("_ephemeral_container"), result
        assert result["database"].startswith("pgmig_"), result
        container = result["_ephemeral_container"]
        # Prove CREATE DATABASE + dump import actually landed
        conn = pymysql.connect(
            host=result["host"],
            port=int(result["port"]),
            user=result["user"],
            password=result["password"],
            database=result["database"],
        )
        cur = conn.cursor()
        cur.execute("SELECT username FROM users WHERE id=1")
        assert cur.fetchone()[0] == "from_mariadb_dump"
        cur.execute("SELECT COUNT(*) FROM admins")
        assert cur.fetchone()[0] == 1
        conn.close()
        print("OK: ephemeral MySQL CREATE DATABASE + dump import (MariaDB→Timescale path)")
    finally:
        mod._compose_text = orig_compose
        mod.resolve_db_service = orig_resolve
        dump.unlink(missing_ok=True)
        if container:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        out = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "name=pgmig-mysql-"],
            capture_output=True, text=True,
        )
        for cid in (out.stdout or "").split():
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True)


def test_mariadb_to_timescaledb_full_schema():
    """Live MariaDB → TimescaleDB universal copy with row verification."""
    import psycopg2
    import pymysql

    maria_name = f"pgmig-maria-{uuid.uuid4().hex[:8]}"
    ts_name = f"pgmig-ts-{uuid.uuid4().hex[:8]}"
    maria_port, ts_port = 33190, 55490

    subprocess.run(
        [
            "docker", "run", "-d", "--name", maria_name,
            "-e", "MYSQL_ROOT_PASSWORD=test",
            "-e", "MYSQL_DATABASE=pasarguard",
            "-p", f"127.0.0.1:{maria_port}:3306",
            "mariadb:11",
        ],
        check=True, capture_output=True,
    )
    subprocess.run(_timescale_run_args(ts_name, ts_port), check=True, capture_output=True)

    try:
        _wait_mysql_ready(maria_port)
        _wait_pg_ready(ts_port)
        _mariadb_seed_full(maria_port)
        _timescale_prepare_schema(ts_port)

        from app.services.native_migration.adapters import (
            create_reader, create_writer, copy_tables_universal,
        )
        src = {
            "host": "127.0.0.1", "port": str(maria_port),
            "database": "pasarguard", "user": "root", "password": "test",
        }
        dst = {
            "host": "127.0.0.1", "port": str(ts_port),
            "database": "pasarguard", "user": "postgres", "password": "test",
        }
        reader = create_reader("mariadb", None, src)
        writer = create_writer("timescaledb", dst)
        try:
            stats, _ = copy_tables_universal(
                reader, writer, lambda _m: None, fail_hard=True,
            )
        finally:
            reader.close()
            writer.close()

        for tbl in ("users", "hosts", "nodes", "inbounds", "groups", "admins"):
            assert stats.get(tbl, 0) >= 1, f"{tbl}: {stats}"
        assert stats.get("users") == 2, stats

        # Verify actual Timescale rows + extension
        conn = psycopg2.connect(
            host="127.0.0.1", port=ts_port, dbname="pasarguard",
            user="postgres", password="test",
        )
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT username FROM users ORDER BY id")
        assert [r[0] for r in cur.fetchall()] == ["u1", "u2"]
        cur.execute("SELECT is_sudo FROM admins WHERE id=1")
        assert cur.fetchone()[0] is True
        cur.execute("SELECT extname FROM pg_extension WHERE extname='timescaledb'")
        assert cur.fetchone()[0] == "timescaledb"
        conn.close()
        print("OK: mariadb → timescaledb (full schema + row verify)")
    finally:
        subprocess.run(["docker", "rm", "-f", maria_name], capture_output=True)
        subprocess.run(["docker", "rm", "-f", ts_name], capture_output=True)


def test_mariadb_dump_ephemeral_then_timescaledb():
    """End-to-end like the panel convert: MariaDB .sql → ephemeral MySQL → Timescale."""
    import asyncio
    import pymysql
    import psycopg2
    from app.services.native_migration import sql_staging as mod
    from app.services.native_migration.adapters import (
        create_reader, create_writer, copy_tables_universal,
    )

    maria_name = f"pgmig-mdump-{uuid.uuid4().hex[:8]}"
    ts_name = f"pgmig-tsdump-{uuid.uuid4().hex[:8]}"
    maria_port, ts_port = 33191, 55491

    subprocess.run(
        [
            "docker", "run", "-d", "--name", maria_name,
            "-e", "MYSQL_ROOT_PASSWORD=test",
            "-e", "MYSQL_DATABASE=pasarguard",
            "-p", f"127.0.0.1:{maria_port}:3306",
            "mariadb:11",
        ],
        check=True, capture_output=True,
    )
    subprocess.run(_timescale_run_args(ts_name, ts_port), check=True, capture_output=True)

    dump = Path(tempfile.mkstemp(suffix="-mariadb.sql")[1])
    staging_container = None
    try:
        _wait_mysql_ready(maria_port)
        _wait_pg_ready(ts_port)
        _mariadb_seed_full(maria_port)
        _timescale_prepare_schema(ts_port)

        # mysqldump-style dump from live MariaDB (what backup restore uses)
        with open(dump, "wb") as fh:
            r = subprocess.run(
                [
                    "docker", "exec", maria_name,
                    "mariadb-dump", "-uroot", "-ptest", "pasarguard",
                ],
                stdout=fh, stderr=subprocess.PIPE, check=True,
            )
        assert dump.stat().st_size > 100, "empty dump"

        class Job:
            def log(self, msg, *_a, **_k):
                print(f"  [staging] {msg}")

        class Mini:
            def __init__(self):
                self.job = Job()

            async def _run_cmd(self, cmd, timeout=180, cwd=None):
                try:
                    rr = subprocess.run(
                        cmd, capture_output=True, timeout=timeout, cwd=cwd,
                    )
                    out = (rr.stdout or b"").decode() + (rr.stderr or b"").decode()
                    return rr.returncode == 0, out
                except Exception as exc:
                    return False, str(exc)

        orig_compose = mod._compose_text
        orig_resolve = mod.resolve_db_service
        mod._compose_text = lambda: (
            "services:\n  timescaledb:\n    image: timescale/timescaledb:latest-pg16\n"
        )
        mod.resolve_db_service = lambda db: (
            "timescaledb" if db == "timescaledb" else None
        )
        try:
            staging = asyncio.run(
                mod.import_sql_dump_to_live_db(
                    Mini(), str(dump), "mariadb", {"password": "x"},
                )
            )
        finally:
            mod._compose_text = orig_compose
            mod.resolve_db_service = orig_resolve

        staging_container = staging.get("_ephemeral_container")
        assert staging_container, staging

        # Prove staging has MariaDB rows
        sconn = pymysql.connect(
            host=staging["host"], port=int(staging["port"]),
            user=staging["user"], password=staging["password"],
            database=staging["database"],
        )
        scur = sconn.cursor()
        scur.execute("SELECT COUNT(*) FROM users")
        assert scur.fetchone()[0] == 2
        sconn.close()

        dst = {
            "host": "127.0.0.1", "port": str(ts_port),
            "database": "pasarguard", "user": "postgres", "password": "test",
        }
        reader = create_reader("mariadb", None, staging)
        writer = create_writer("timescaledb", dst)
        try:
            stats, _ = copy_tables_universal(
                reader, writer, lambda _m: None, fail_hard=True,
            )
        finally:
            reader.close()
            writer.close()

        assert stats.get("users") == 2, stats
        assert stats.get("admins") == 1, stats

        conn = psycopg2.connect(
            host="127.0.0.1", port=ts_port, dbname="pasarguard",
            user="postgres", password="test",
        )
        cur = conn.cursor()
        cur.execute("SELECT username FROM users ORDER BY id")
        assert [r[0] for r in cur.fetchall()] == ["u1", "u2"]
        cur.execute("SELECT remark FROM hosts WHERE id=1")
        assert cur.fetchone()[0] == "h1"
        cur.execute("SELECT extname FROM pg_extension WHERE extname='timescaledb'")
        assert cur.fetchone() is not None
        conn.close()
        print("OK: mariadb dump → ephemeral MySQL → timescaledb (E2E)")
    finally:
        dump.unlink(missing_ok=True)
        if staging_container:
            subprocess.run(["docker", "rm", "-f", staging_container], capture_output=True)
        subprocess.run(["docker", "rm", "-f", maria_name], capture_output=True)
        subprocess.run(["docker", "rm", "-f", ts_name], capture_output=True)
        # Sweep any leftover ephemeral mysql from failed runs
        out = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "name=pgmig-mysql-"],
            capture_output=True, text=True,
        )
        for cid in (out.stdout or "").split():
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True)


def _make_source_sqlite(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);
        INSERT INTO alembic_version VALUES ('deadbeefcafe');

        CREATE TABLE admins (
            id INTEGER PRIMARY KEY,
            username TEXT,
            hashed_password TEXT,
            is_sudo INTEGER,
            enabled INTEGER
        );
        INSERT INTO admins VALUES (1, 'admin', 'hash', 1, 1);

        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            status TEXT,
            used_traffic INTEGER,
            data_limit INTEGER,
            admin_id INTEGER,
            enable INTEGER
        );
        INSERT INTO users VALUES (1, 'user1', 'active', 0, 0, 1, 1);
        INSERT INTO users VALUES (2, 'user2', 'active', 100, 0, 1, 1);

        CREATE TABLE inbounds (
            id INTEGER PRIMARY KEY,
            tag TEXT,
            protocol TEXT
        );
        INSERT INTO inbounds VALUES (1, 'vless-tcp', 'vless');

        CREATE TABLE exclude_inbounds_association (
            user_id INTEGER,
            inbound_id INTEGER
        );
        INSERT INTO exclude_inbounds_association VALUES (1, 1);

        CREATE TABLE template_inbounds_association (
            user_template_id INTEGER,
            inbound_id INTEGER
        );
        """
    )
    conn.commit()
    conn.close()


def _create_pg_schema(cur) -> None:
    cur.execute(
        """
        CREATE TABLE admins (
            id SERIAL PRIMARY KEY,
            username TEXT,
            hashed_password TEXT,
            is_sudo BOOLEAN,
            enabled BOOLEAN
        );
        CREATE TABLE users (
            id SERIAL PRIMARY KEY,
            username TEXT,
            status TEXT,
            used_traffic BIGINT,
            data_limit BIGINT,
            admin_id INT,
            enable BOOLEAN
        );
        CREATE TABLE inbounds (
            id SERIAL PRIMARY KEY,
            tag TEXT,
            protocol TEXT
        );
        CREATE TABLE exclude_inbounds_association (
            user_id INT,
            inbound_id INT
        );
        CREATE TABLE template_inbounds_association (
            user_template_id INT,
            inbound_id INT
        );
        CREATE TABLE alembic_version (version_num VARCHAR(32));
        """
    )


def _create_mysql_schema(cur) -> None:
    cur.execute(
        """
        CREATE TABLE admins (
            id INT PRIMARY KEY AUTO_INCREMENT,
            username VARCHAR(64),
            hashed_password VARCHAR(255),
            is_sudo TINYINT(1),
            enabled TINYINT(1)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE users (
            id INT PRIMARY KEY AUTO_INCREMENT,
            username VARCHAR(64),
            status VARCHAR(32),
            used_traffic BIGINT,
            data_limit BIGINT,
            admin_id INT,
            enable TINYINT(1)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE inbounds (
            id INT PRIMARY KEY AUTO_INCREMENT,
            tag VARCHAR(64),
            protocol VARCHAR(32)
        )
        """
    )
    cur.execute(
        "CREATE TABLE exclude_inbounds_association (user_id INT, inbound_id INT)"
    )
    cur.execute(
        "CREATE TABLE template_inbounds_association (user_template_id INT, inbound_id INT)"
    )
    cur.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")


def _wait_port(host: str, port: int, timeout: float = 60) -> None:
    import socket

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return
        except OSError:
            time.sleep(1)
    raise RuntimeError(f"Timeout waiting for {host}:{port}")


def _run_copy(sqlite_path: Path, target_db: str, dsn: dict) -> dict:
    from app.services.native_migration.adapters import (
        create_reader,
        create_writer,
        copy_tables_universal,
    )

    reader = create_reader("sqlite", str(sqlite_path), {})
    writer = create_writer(target_db, dsn)
    logs: list[str] = []
    try:
        return copy_tables_universal(
            reader, writer, logs.append, source_version="deadbeefcafe", fail_hard=True,
        )[0]
    finally:
        reader.close()
        writer.close()


def test_sqlite_to_postgres():
    import psycopg2

    name = f"pgmig-pg-{uuid.uuid4().hex[:8]}"
    port = 55432
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "-e", "POSTGRES_PASSWORD=test",
            "-e", "POSTGRES_USER=test",
            "-e", "POSTGRES_DB=pasarguard",
            "-p", f"{port}:5432",
            "postgres:16-alpine",
        ],
        check=True,
        capture_output=True,
    )
    try:
        _wait_port("127.0.0.1", port)
        time.sleep(3)
        for _ in range(30):
            try:
                conn = psycopg2.connect(
                    host="127.0.0.1", port=port, dbname="pasarguard",
                    user="test", password="test",
                )
                break
            except Exception:
                time.sleep(1)
        else:
            raise RuntimeError("Postgres not ready")

        cur = conn.cursor()
        _create_pg_schema(cur)
        conn.commit()
        conn.close()

        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        sqlite_path = Path(path)
        try:
            _make_source_sqlite(sqlite_path)
            dsn = {
                "host": "127.0.0.1",
                "port": str(port),
                "database": "pasarguard",
                "user": "test",
                "password": "test",
            }
            stats = _run_copy(sqlite_path, "postgresql", dsn)
            assert stats.get("users") == 2, stats
            assert stats.get("admins") == 1, stats
            assert stats.get("exclude_inbounds_association") == 1, stats

            conn = psycopg2.connect(
                host="127.0.0.1", port=port, dbname="pasarguard",
                user="test", password="test",
            )
            cur = conn.cursor()
            cur.execute("SELECT is_sudo FROM admins WHERE id=1")
            assert cur.fetchone()[0] is True
            cur.execute("SELECT enable FROM users WHERE username='user1'")
            assert cur.fetchone()[0] is True
            conn.close()
            print("OK: sqlite → postgresql")
        finally:
            sqlite_path.unlink(missing_ok=True)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_sqlite_to_mysql(image: str = "mysql:8", label: str = "mysql"):
    import pymysql

    name = f"pgmig-{label}-{uuid.uuid4().hex[:8]}"
    port = 33070 if label == "mysql" else 33071
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "-e", "MYSQL_ROOT_PASSWORD=test",
            "-e", "MYSQL_DATABASE=pasarguard",
            "-p", f"{port}:3306",
            image,
        ],
        check=True,
        capture_output=True,
    )
    try:
        _wait_port("127.0.0.1", port, timeout=90)
        time.sleep(8)
        for _ in range(40):
            try:
                conn = pymysql.connect(
                    host="127.0.0.1", port=port, user="root",
                    password="test", database="pasarguard",
                )
                break
            except Exception:
                time.sleep(2)
        else:
            raise RuntimeError(f"{label} not ready")

        cur = conn.cursor()
        _create_mysql_schema(cur)
        conn.commit()
        conn.close()

        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        sqlite_path = Path(path)
        try:
            _make_source_sqlite(sqlite_path)
            dsn = {
                "host": "127.0.0.1",
                "port": str(port),
                "database": "pasarguard",
                "user": "root",
                "password": "test",
            }
            engine = "mariadb" if "maria" in label else "mysql"
            stats = _run_copy(sqlite_path, engine, dsn)
            assert stats.get("users") == 2, stats
            assert stats.get("admins") == 1, stats
            assert stats.get("exclude_inbounds_association") == 1, stats
            print(f"OK: sqlite → {label}")
        finally:
            sqlite_path.unlink(missing_ok=True)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_savepoint_recovers_bad_row():
    """Bad row must not abort the whole Postgres transaction."""
    import psycopg2

    name = f"pgmig-sp-{uuid.uuid4().hex[:8]}"
    port = 55433
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "-e", "POSTGRES_PASSWORD=test",
            "-e", "POSTGRES_USER=test",
            "-e", "POSTGRES_DB=pasarguard",
            "-p", f"{port}:5432",
            "postgres:16-alpine",
        ],
        check=True,
        capture_output=True,
    )
    try:
        _wait_port("127.0.0.1", port)
        time.sleep(3)
        for _ in range(30):
            try:
                conn = psycopg2.connect(
                    host="127.0.0.1", port=port, dbname="pasarguard",
                    user="test", password="test",
                )
                break
            except Exception:
                time.sleep(1)
        else:
            raise RuntimeError("Postgres not ready")

        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE users (id SERIAL PRIMARY KEY, username TEXT UNIQUE, enable BOOLEAN)"
        )
        cur.execute("CREATE TABLE admins (id SERIAL PRIMARY KEY, username TEXT, is_sudo BOOLEAN)")
        cur.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        conn.commit()
        conn.close()

        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        sqlite_path = Path(path)
        try:
            sc = sqlite3.connect(sqlite_path)
            sc.execute("CREATE TABLE users (id INTEGER, username TEXT, enable INTEGER)")
            sc.execute("CREATE TABLE admins (id INTEGER, username TEXT, is_sudo INTEGER)")
            sc.execute("INSERT INTO admins VALUES (1, 'a', 1)")
            sc.execute("INSERT INTO users VALUES (1, 'dup', 1)")
            sc.execute("INSERT INTO users VALUES (2, 'dup', 1)")  # unique violation on 2nd
            sc.execute("INSERT INTO users VALUES (3, 'ok', 1)")
            sc.commit()
            sc.close()

            from app.services.native_migration.adapters import (
                create_reader, create_writer, copy_tables_universal,
            )
            dsn = {
                "host": "127.0.0.1", "port": str(port),
                "database": "pasarguard", "user": "test", "password": "test",
            }
            reader = create_reader("sqlite", str(sqlite_path), {})
            writer = create_writer("postgresql", dsn)
            logs: list[str] = []
            try:
                stats, _report = copy_tables_universal(
                    reader, writer, logs.append, fail_hard=False,
                )
            finally:
                reader.close()
                writer.close()

            assert stats.get("users", 0) >= 2, (stats, logs)
            assert stats.get("admins") == 1, stats
            print("OK: postgres savepoint recovers bad rows")
        finally:
            sqlite_path.unlink(missing_ok=True)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_strategy_two_phase():
    from app.services.native_migration import migration_strategy

    assert migration_strategy("sqlite", "postgresql") == "two_phase"
    assert migration_strategy("sqlite", "mysql") == "two_phase"
    assert migration_strategy("sqlite", "sqlite") == "same_db"
    print("OK: migration strategy two_phase")


def test_env_sanitize_smoke():
    from app.services.pasarguard_ops import sanitize_env_text_for_docker

    out = sanitize_env_text_for_docker('UVICORN_HOST = "0.0.0.0"\n')
    assert out.strip() == "UVICORN_HOST=0.0.0.0"
    print("OK: env sanitize smoke")


if __name__ == "__main__":
    test_strategy_two_phase()
    test_env_sanitize_smoke()

    if not _docker_available():
        print("SKIP: Docker not available — integration DB tests skipped")
        sys.exit(0)

    test_sqlite_to_postgres()
    test_sqlite_to_postgres_full_schema()
    test_savepoint_recovers_bad_row()
    test_sqlite_to_mysql("mysql:8", "mysql")
    test_sqlite_to_mysql_full_schema("mysql", "mysql:8", 33072)
    test_sqlite_to_mysql("mariadb:11", "mariadb")
    test_sqlite_to_mysql_full_schema("mariadb", "mariadb:11", 33074)
    test_mysql_to_postgres_full_schema()
    # MariaDB → TimescaleDB (reported failure path)
    test_ephemeral_mysql_create_database_real_docker()
    test_mariadb_to_timescaledb_full_schema()
    test_mariadb_dump_ephemeral_then_timescaledb()
    print("\nAll integration tests passed.")
