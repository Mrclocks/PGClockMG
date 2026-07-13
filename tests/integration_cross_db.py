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
    test_savepoint_recovers_bad_row()
    test_sqlite_to_mysql("mysql:8", "mysql")
    test_sqlite_to_mysql("mariadb:11", "mariadb")
    print("\nAll integration tests passed.")
