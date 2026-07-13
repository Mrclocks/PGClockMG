"""Tests for native cross-DB migration helpers."""

import sys
import json
import sqlite3
import tempfile
import os
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_build_local_alembic_url():
    from app.services.pasarguard_ops import build_local_alembic_url

    params = {
        "target_db": "postgresql",
        "target_db_user": "pasarguard",
        "target_db_password": "secret",
        "target_db_name": "pasarguard",
        "target_db_host": "127.0.0.1",
        "target_db_port": "6432",
    }
    url = build_local_alembic_url(params)
    assert ":5432/" in url
    assert "pasarguard:secret@127.0.0.1:5432/pasarguard" in url
    print("OK: build_local_alembic_url")


def test_sqlite_column_intersection():
    from app.services.native_migration.copy_core import (
        sqlite_columns, SKIP_TABLES, TABLE_ORDER,
    )

    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, status TEXT)")
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        conn.execute("INSERT INTO alembic_version VALUES ('2b231de97dc3')")
        conn.commit()
        cols = sqlite_columns(conn, "users")
        assert "username" in cols
        assert "alembic_version" in SKIP_TABLES
        assert "exclude_inbounds_association" in TABLE_ORDER
        assert "exclude_inbounds_association" not in SKIP_TABLES
        assert "users" in TABLE_ORDER
        conn.close()
        print("OK: sqlite column helpers")
    finally:
        os.unlink(path)


def test_migration_strategy_matrix():
    from app.services.native_migration import migration_strategy

    engines = ("sqlite", "mysql", "mariadb", "postgresql", "timescaledb")
    for src in engines:
        for tgt in engines:
            s = migration_strategy(src, tgt)
            if src == tgt:
                assert s == "same_db"
            else:
                assert s == "two_phase", f"{src}→{tgt}"
    assert migration_strategy("sqlite", "sqlite") == "same_db"
    assert migration_strategy("unknown", "postgresql") == "unsupported"
    print("OK: migration strategy matrix (all 25 pairs)")


def test_read_alembic_from_sql_dump():
    from app.services.native_migration.source_version import read_alembic_version_from_sql_dump

    sql = "INSERT INTO `alembic_version` VALUES ('2b231de97dc3');"
    assert read_alembic_version_from_sql_dump(sql) == "2b231de97dc3"
    print("OK: alembic from sql dump")


def test_convert_bool_values():
    from app.services.native_migration.copy_core import convert_value

    assert convert_value("admins", "is_sudo", 1) is True
    assert convert_value("admins", "is_sudo", 0) is False
    assert convert_value("hosts", "allowinsecure", 0) is False
    assert convert_value("hosts", "random_user_agent", 1) is True
    assert convert_value("hosts", "use_sni_as_host", 0) is False
    assert convert_value("hosts", "fingerprint", "") is None
    assert convert_value("hosts", "fingerprint", None) is None
    assert convert_value("hosts", "alpn", "none") is None
    assert convert_value("hosts", "alpn", None) is None
    assert convert_value("hosts", "alpn", "h2") == "h2"
    print("OK: convert_bool_values")


def test_users_status_not_bool():
    from app.services.native_migration.copy_core import (
        convert_value, normalize_user_status, BOOL_COLUMNS,
    )

    assert "status" not in BOOL_COLUMNS
    assert convert_value("users", "status", "active") == "active"
    assert convert_value("users", "status", "limited") == "limited"
    assert convert_value("users", "status", "on_hold") == "on_hold"
    assert convert_value("users", "status", 1) == "active"
    assert convert_value("users", "status", 0) == "disabled"
    assert normalize_user_status("onhold") == "on_hold"
    assert normalize_user_status("ACTIVE") == "active"
    print("OK: users_status_not_bool")


def test_hosts_json_and_column_plan():
    from app.services.native_migration.copy_core import (
        build_table_column_plan,
        coerce_json_value,
        coerce_mux_settings,
        convert_value,
    )

    insert_cols, select_cols = build_table_column_plan(
        "hosts",
        ["id", "remark", "fragment_setting", "noise_setting", "mux_enable", "inbound_tag"],
        ["id", "remark", "fragment_settings", "noise_settings", "mux_settings", "priority", "inbound_tag"],
    )
    assert "fragment_settings" in insert_cols
    assert "noise_settings" in insert_cols
    assert "mux_settings" in insert_cols
    assert "priority" in insert_cols
    assert select_cols[insert_cols.index("fragment_settings")] == "fragment_setting"
    assert select_cols[insert_cols.index("priority")] is None

    parsed = coerce_json_value('{"a":1}')
    assert parsed is not None
    assert json.loads(parsed) == {"a": 1}
    assert coerce_json_value("not-json") is None
    assert coerce_json_value("") is None
    assert coerce_mux_settings(1) == '{"enabled": true}'
    assert coerce_mux_settings("1") == '{"enabled": true}'
    assert convert_value("hosts", "fragment_settings", "legacy") is None
    print("OK: hosts_json_and_column_plan")


def test_nodes_defaults_copy_sqlite():
    """Nodes copy when target requires server_ca but source lacks it."""
    import tempfile
    from app.services.native_migration.adapters import (
        create_reader, create_writer, copy_tables_universal,
    )

    fd1, src = tempfile.mkstemp(suffix=".sqlite3")
    fd2, dst = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd1)
    os.close(fd2)
    try:
        sc = sqlite3.connect(src)
        sc.executescript(
            """
            CREATE TABLE admins (id INTEGER PRIMARY KEY, username TEXT);
            INSERT INTO admins VALUES (1, 'admin');
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
            INSERT INTO users VALUES (1, 'u1');
            CREATE TABLE hosts (id INTEGER PRIMARY KEY, remark TEXT);
            INSERT INTO hosts VALUES (1, 'h1');
            CREATE TABLE inbounds (id INTEGER PRIMARY KEY, tag TEXT);
            INSERT INTO inbounds VALUES (1, 'vless-tcp');
            CREATE TABLE core_configs (id INTEGER PRIMARY KEY, name TEXT);
            INSERT INTO core_configs VALUES (1, 'xray');
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY, name TEXT, address TEXT, core_config_id INTEGER
            );
            INSERT INTO nodes VALUES (1, 'n1', '1.2.3.4', 1);
            """
        )
        sc.commit()
        sc.close()

        dc = sqlite3.connect(dst)
        dc.executescript(
            """
            CREATE TABLE admins (id INTEGER PRIMARY KEY, username TEXT);
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
            CREATE TABLE hosts (id INTEGER PRIMARY KEY, remark TEXT);
            CREATE TABLE inbounds (id INTEGER PRIMARY KEY, tag TEXT);
            CREATE TABLE core_configs (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY,
                name TEXT,
                address TEXT,
                core_config_id INTEGER,
                server_ca TEXT NOT NULL DEFAULT '',
                api_key TEXT
            );
            """
        )
        dc.commit()
        dc.close()

        reader = create_reader("sqlite", src, {})
        writer = create_writer("sqlite", {"sqlite_path": dst}, dst)
        logs = []
        try:
            stats, _report = copy_tables_universal(reader, writer, logs.append, fail_hard=True)
        finally:
            reader.close()
            writer.close()

        assert stats.get("users") == 1
        assert stats.get("hosts") == 1
        assert stats.get("nodes") == 1
        verify = sqlite3.connect(dst)
        row = verify.execute("SELECT server_ca FROM nodes WHERE id=1").fetchone()
        verify.close()
        assert row[0] == ""
        print("OK: nodes_defaults_copy_sqlite")
    finally:
        os.unlink(src)
        os.unlink(dst)


def test_hosts_legacy_columns_copy_sqlite():
    """Legacy Marzban host columns map to PasarGuard JSON column names."""
    import tempfile
    from app.services.native_migration.adapters import (
        create_reader, create_writer, copy_tables_universal,
    )

    fd1, src = tempfile.mkstemp(suffix=".sqlite3")
    fd2, dst = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd1)
    os.close(fd2)
    try:
        sc = sqlite3.connect(src)
        sc.executescript(
            """
            CREATE TABLE admins (id INTEGER PRIMARY KEY, username TEXT);
            INSERT INTO admins VALUES (1, 'admin');
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
            INSERT INTO users VALUES (1, 'u1');
            CREATE TABLE inbounds (id INTEGER PRIMARY KEY, tag TEXT);
            INSERT INTO inbounds VALUES (1, 'vless-tcp');
            CREATE TABLE hosts (
                id INTEGER PRIMARY KEY,
                remark TEXT,
                inbound_tag TEXT,
                fragment_setting TEXT,
                noise_setting TEXT,
                mux_enable INTEGER
            );
            INSERT INTO hosts VALUES (1, 'h1', 'vless-tcp', 'bad', 'bad', 1);
            """
        )
        sc.commit()
        sc.close()

        dc = sqlite3.connect(dst)
        dc.executescript(
            """
            CREATE TABLE admins (id INTEGER PRIMARY KEY, username TEXT);
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
            CREATE TABLE inbounds (id INTEGER PRIMARY KEY, tag TEXT);
            CREATE TABLE hosts (
                id INTEGER PRIMARY KEY,
                remark TEXT,
                inbound_tag TEXT,
                fragment_settings TEXT,
                noise_settings TEXT,
                mux_settings TEXT,
                priority INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        dc.commit()
        dc.close()

        reader = create_reader("sqlite", src, {})
        writer = create_writer("sqlite", {"sqlite_path": dst}, dst)
        try:
            stats, _report = copy_tables_universal(reader, writer, lambda _m: None, fail_hard=True)
        finally:
            reader.close()
            writer.close()

        assert stats.get("hosts") == 1
        verify = sqlite3.connect(dst)
        row = verify.execute(
            "SELECT fragment_settings, noise_settings, mux_settings, priority FROM hosts WHERE id=1"
        ).fetchone()
        verify.close()
        assert row[0] is None
        assert row[1] is None
        assert row[2] == '{"enabled": true}'
        assert row[3] == 0
        print("OK: hosts_legacy_columns_copy_sqlite")
    finally:
        os.unlink(src)
        os.unlink(dst)


def test_copy_sqlite_to_sqlite_associations(tmp_path=None):
    """Offline head→head style copy including association tables + fail_hard."""
    import tempfile
    from app.services.native_migration.adapters import (
        create_reader, create_writer, copy_tables_universal,
    )
    from app.services.native_migration.copy_core import TABLE_ORDER

    fd1, src = tempfile.mkstemp(suffix=".sqlite3")
    fd2, dst = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd1)
    os.close(fd2)
    try:
        sc = sqlite3.connect(src)
        sc.executescript(
            """
            CREATE TABLE admins (id INTEGER PRIMARY KEY, username TEXT, is_sudo INTEGER);
            INSERT INTO admins VALUES (1, 'admin', 1);
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, enable INTEGER);
            INSERT INTO users VALUES (1, 'u1', 1);
            CREATE TABLE inbounds (id INTEGER PRIMARY KEY, tag TEXT);
            INSERT INTO inbounds VALUES (1, 'in1');
            CREATE TABLE exclude_inbounds_association (user_id INTEGER, inbound_id INTEGER);
            INSERT INTO exclude_inbounds_association VALUES (1, 1);
            CREATE TABLE template_inbounds_association (user_template_id INTEGER, inbound_id INTEGER);
            CREATE TABLE alembic_version (version_num VARCHAR(32));
            INSERT INTO alembic_version VALUES ('abc');
            """
        )
        sc.commit()
        sc.close()

        dc = sqlite3.connect(dst)
        dc.executescript(
            """
            CREATE TABLE admins (id INTEGER PRIMARY KEY, username TEXT, is_sudo INTEGER);
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, enable INTEGER);
            CREATE TABLE inbounds (id INTEGER PRIMARY KEY, tag TEXT);
            CREATE TABLE exclude_inbounds_association (user_id INTEGER, inbound_id INTEGER);
            CREATE TABLE template_inbounds_association (user_template_id INTEGER, inbound_id INTEGER);
            CREATE TABLE alembic_version (version_num VARCHAR(32));
            """
        )
        dc.commit()
        dc.close()

        assert "exclude_inbounds_association" in TABLE_ORDER
        reader = create_reader("sqlite", src, {})
        writer = create_writer("sqlite", {"sqlite_path": dst}, dst)
        logs = []
        try:
            stats, _report = copy_tables_universal(reader, writer, logs.append, fail_hard=True)
        finally:
            reader.close()
            writer.close()

        assert stats.get("users") == 1
        assert stats.get("admins") == 1
        assert stats.get("exclude_inbounds_association") == 1

        # fail_hard: users table exists but every row fails insert
        fd3, empty_src = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd3)
        try:
            esc = sqlite3.connect(empty_src)
            esc.execute("CREATE TABLE users (id INTEGER, username TEXT)")
            esc.execute("CREATE TABLE admins (id INTEGER, username TEXT)")
            esc.execute("INSERT INTO users VALUES (1, 'x')")
            esc.commit()
            esc.close()
            fd4, bad_dst = tempfile.mkstemp(suffix=".sqlite3")
            os.close(fd4)
            bdc = sqlite3.connect(bad_dst)
            bdc.execute("CREATE TABLE admins (id INTEGER, username TEXT)")
            bdc.execute(
                "CREATE TABLE users (id INTEGER, username TEXT, email TEXT NOT NULL)"
            )
            bdc.commit()
            bdc.close()
            r2 = create_reader("sqlite", empty_src, {})
            w2 = create_writer("sqlite", {"sqlite_path": bad_dst}, bad_dst)
            try:
                raised = False
                try:
                    copy_tables_universal(r2, w2, logs.append, fail_hard=True)[0]
                except RuntimeError as exc:
                    raised = True
                    assert "users" in str(exc)
                assert raised, "expected fail_hard RuntimeError"
            finally:
                r2.close()
                w2.close()
            os.unlink(bad_dst)
        finally:
            os.unlink(empty_src)

        print("OK: sqlite to sqlite associations + fail_hard")
    finally:
        os.unlink(src)
        os.unlink(dst)


def test_nodes_missing_target_table_skips():
    """When target schema has no nodes table, migration continues for users/hosts."""
    import tempfile
    from app.services.native_migration.adapters import (
        create_reader, create_writer, copy_tables_universal,
    )

    fd1, src = tempfile.mkstemp(suffix=".sqlite3")
    fd2, dst = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd1)
    os.close(fd2)
    try:
        sc = sqlite3.connect(src)
        sc.executescript(
            """
            CREATE TABLE admins (id INTEGER PRIMARY KEY, username TEXT);
            INSERT INTO admins VALUES (1, 'admin');
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
            INSERT INTO users VALUES (1, 'u1');
            CREATE TABLE hosts (id INTEGER PRIMARY KEY, remark TEXT);
            INSERT INTO hosts VALUES (1, 'h1');
            CREATE TABLE core_configs (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, core_config_id INTEGER);
            INSERT INTO nodes VALUES (1, 'n1', 99);
            INSERT INTO nodes VALUES (2, 'n2', 99);
            """
        )
        sc.commit()
        sc.close()

        dc = sqlite3.connect(dst)
        dc.executescript(
            """
            CREATE TABLE admins (id INTEGER PRIMARY KEY, username TEXT);
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
            CREATE TABLE hosts (id INTEGER PRIMARY KEY, remark TEXT);
            """
        )
        dc.commit()
        dc.close()

        logs = []
        reader = create_reader("sqlite", src, {})
        writer = create_writer("sqlite", {"sqlite_path": dst}, dst)
        try:
            stats, _report = copy_tables_universal(reader, writer, logs.append, fail_hard=True)
        finally:
            reader.close()
            writer.close()

        assert stats.get("users") == 1
        assert stats.get("hosts") == 1
        assert stats.get("nodes", 0) == 0
        assert any("Skip nodes" in ln for ln in logs), logs
        print("OK: nodes_missing_target_table_skips")
    finally:
        os.unlink(src)
        os.unlink(dst)


def test_build_copy_report():
    from app.services.native_migration.adapters import build_copy_report

    report = build_copy_report(
        {"nodes": 5, "users": 10, "hosts": 3},
        {"nodes": 0, "users": 10, "hosts": 2},
    )
    assert report["has_gaps"] is True
    tables = {x["table"] for x in report["incomplete"]}
    assert tables == {"nodes", "hosts"}
    assert report["incomplete"][0]["missing"] == 5 or any(
        i["table"] == "nodes" and i["missing"] == 5 for i in report["incomplete"]
    )
    print("OK: build_copy_report")


def test_sanitize_env_text_for_docker():
    from app.services.pasarguard_ops import sanitize_env_text_for_docker

    raw = 'UVICORN_HOST = "0.0.0.0"\nDB_PASSWORD = secret\n# comment\n'
    out = sanitize_env_text_for_docker(raw)
    assert "UVICORN_HOST=0.0.0.0" in out
    assert "DB_PASSWORD=secret" in out
    assert "UVICORN_HOST " not in out
    print("OK: sanitize_env_text_for_docker")


def test_native_migration_import():
    from app.services.native_migration import run_native_cross_db_migration
    from app.services.migrators.marzban import MarzbanMigrator
    assert run_native_cross_db_migration and MarzbanMigrator
    print("OK: native migration imports")


if __name__ == "__main__":
    test_build_local_alembic_url()
    test_sqlite_column_intersection()
    test_migration_strategy_matrix()
    test_read_alembic_from_sql_dump()
    test_convert_bool_values()
    test_users_status_not_bool()
    test_hosts_json_and_column_plan()
    test_hosts_legacy_columns_copy_sqlite()
    test_nodes_defaults_copy_sqlite()
    test_nodes_missing_target_table_skips()
    test_copy_sqlite_to_sqlite_associations()
    test_build_copy_report()
    test_sanitize_env_text_for_docker()
    test_native_migration_import()
    print("\nAll native migration tests passed.")
