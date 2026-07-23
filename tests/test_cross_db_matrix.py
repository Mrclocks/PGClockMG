"""Cross-DB migration matrix: all source→target engine combinations.

Run: python tests/test_cross_db_matrix.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ENGINES = ("sqlite", "mysql", "mariadb", "postgresql", "timescaledb")


def test_all_migration_strategies():
    from app.services.native_migration import migration_strategy

    for src in ENGINES:
        for tgt in ENGINES:
            strategy = migration_strategy(src, tgt)
            if src == tgt:
                assert strategy == "same_db", f"{src}→{tgt}"
            elif tgt == "sqlite" and src != "sqlite":
                assert strategy == "unsupported", f"{src}→{tgt} got {strategy}"
            else:
                assert strategy == "two_phase", f"{src}→{tgt} got {strategy}"
    print("OK: all engine pairs use same_db / two_phase / unsupported(*→sqlite)")


def test_engine_families():
    from app.services.native_migration.copy_core import engine_family

    assert engine_family("mysql") == "mysql"
    assert engine_family("mariadb") == "mysql"
    assert engine_family("postgresql") == "postgresql"
    assert engine_family("timescaledb") == "postgresql"
    assert engine_family("sqlite") == "sqlite"
    print("OK: engine families")


def test_normalize_raw_value():
    from datetime import datetime, timezone
    from app.services.native_migration.copy_core import (
        normalize_datetime_for_sql,
        normalize_raw_value,
    )

    assert normalize_raw_value(Decimal("10")) == 10
    assert normalize_raw_value(Decimal("10.5")) == 10.5
    dt = datetime(2024, 1, 2, 3, 4, 5)
    assert normalize_raw_value(dt) == "2024-01-02 03:04:05"
    assert normalize_raw_value(b"hello") == "hello"

    aware = datetime(2026, 7, 23, 8, 37, 41, tzinfo=timezone.utc)
    assert normalize_raw_value(aware) == "2026-07-23 08:37:41"
    assert "+00:00" not in normalize_raw_value(aware)
    assert normalize_raw_value("2026-07-23 08:37:41+00:00") == "2026-07-23 08:37:41"
    assert normalize_raw_value("2026-07-23T08:37:41+00:00") == "2026-07-23 08:37:41"
    assert normalize_raw_value("2026-07-23 08:37:41Z") == "2026-07-23 08:37:41"
    assert normalize_datetime_for_sql("2026-07-23 08:37:41.123456+00:00") == (
        "2026-07-23 08:37:41.123456"
    )
    print("OK: normalize_raw_value (incl. timestamptz → MySQL-safe)")


def test_parse_not_null_column():
    from app.services.native_migration.copy_core import parse_not_null_column

    assert parse_not_null_column(
        'null value in column "server_ca" violates not-null constraint'
    ) == "server_ca"
    assert parse_not_null_column(
        "Column 'priority' cannot be null"
    ) == "priority"
    print("OK: parse_not_null_column")


def _make_full_source_sqlite(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE admins (id INTEGER PRIMARY KEY, username TEXT, is_sudo INTEGER);
        INSERT INTO admins VALUES (1, 'admin', 1);

        CREATE TABLE core_configs (id INTEGER PRIMARY KEY, name TEXT);
        INSERT INTO core_configs VALUES (1, 'xray');

        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, name TEXT, address TEXT, core_config_id INTEGER
        );
        INSERT INTO nodes VALUES (1, 'node1', '1.2.3.4', 1);

        CREATE TABLE inbounds (id INTEGER PRIMARY KEY, tag TEXT, protocol TEXT, is_disabled INTEGER);
        INSERT INTO inbounds VALUES (1, 'vless-tcp', 'vless', 0);

        CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT, is_disabled INTEGER);
        INSERT INTO groups VALUES (1, 'default', 0);

        CREATE TABLE hosts (
            id INTEGER PRIMARY KEY,
            remark TEXT,
            inbound_tag TEXT,
            fragment_setting TEXT,
            noise_setting TEXT,
            mux_enable INTEGER,
            security TEXT,
            fingerprint TEXT
        );
        INSERT INTO hosts VALUES (
            1, 'host1', 'vless-tcp', 'not-json', 'also-bad', 1, 'none', 'none'
        );

        CREATE TABLE users (
            id INTEGER PRIMARY KEY, username TEXT, status TEXT, enable INTEGER, admin_id INTEGER
        );
        INSERT INTO users VALUES (1, 'u1', 'active', 1, 1);

        CREATE TABLE settings (id INTEGER PRIMARY KEY, key TEXT, value TEXT);
        INSERT INTO settings VALUES (1, 'sub', 'not-json-either');
        """
    )
    conn.commit()
    conn.close()


def _sqlite_target_schema() -> str:
    return """
        CREATE TABLE admins (id INTEGER PRIMARY KEY, username TEXT, is_sudo INTEGER);
        CREATE TABLE core_configs (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, name TEXT, address TEXT,
            core_config_id INTEGER, server_ca TEXT NOT NULL DEFAULT '', api_key TEXT,
            status TEXT
        );
        CREATE TABLE inbounds (
            id INTEGER PRIMARY KEY, tag TEXT, protocol TEXT, is_disabled INTEGER
        );
        CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT, is_disabled INTEGER);
        CREATE TABLE hosts (
            id INTEGER PRIMARY KEY, remark TEXT, inbound_tag TEXT,
            fragment_settings TEXT, noise_settings TEXT, mux_settings TEXT,
            priority INTEGER NOT NULL DEFAULT 0, security TEXT, fingerprint TEXT
        );
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, username TEXT, status TEXT, enable INTEGER, admin_id INTEGER
        );
        CREATE TABLE settings (id INTEGER PRIMARY KEY, key TEXT, value TEXT);
    """


def test_sqlite_to_sqlite_full_schema():
    from app.services.native_migration.adapters import (
        create_reader, create_writer, copy_tables_universal,
    )

    fd1, src = tempfile.mkstemp(suffix=".sqlite3")
    fd2, dst = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd1)
    os.close(fd2)
    try:
        _make_full_source_sqlite(src)
        dc = sqlite3.connect(dst)
        dc.executescript(_sqlite_target_schema())
        dc.commit()
        dc.close()

        reader = create_reader("sqlite", src, {})
        writer = create_writer("sqlite", {"sqlite_path": dst}, dst)
        try:
            stats, _ = copy_tables_universal(
                reader, writer, lambda _m: None, fail_hard=True,
            )
        finally:
            reader.close()
            writer.close()

        for table in ("users", "hosts", "nodes", "inbounds", "groups"):
            assert stats.get(table, 0) >= 1, f"{table}: {stats}"
        print("OK: sqlite->sqlite full schema")
    finally:
        os.unlink(src)
        os.unlink(dst)


def test_reader_writer_pairs_instantiate():
    """Every engine can create reader/writer classes (sqlite offline)."""
    from app.services.native_migration.adapters import create_reader, create_writer

    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE users (id INTEGER, username TEXT)")
        conn.commit()
        conn.close()

        r = create_reader("sqlite", path, {})
        r.close()
        w = create_writer("sqlite", {"sqlite_path": path}, path)
        w.close()
        print("OK: reader/writer instantiate")
    finally:
        os.unlink(path)


if __name__ == "__main__":
    test_all_migration_strategies()
    test_engine_families()
    test_normalize_raw_value()
    test_parse_not_null_column()
    test_reader_writer_pairs_instantiate()
    test_sqlite_to_sqlite_full_schema()
    print("\nAll cross-DB matrix tests passed.")
