"""Tests for streaming SQL dump row counters and empty-dump refuse policy."""

from __future__ import annotations

from pathlib import Path


def test_estimate_pg_and_mysql_text():
    from app.services.sql_dump_counts import estimate_sql_dump_counts_from_text

    pg = """
COPY public.users (id, username) FROM stdin;
1	alice
2	bob
3	carol
\\.
COPY public.nodes (id) FROM stdin;
1
\\.
INSERT INTO admins (id) VALUES (1), (2);
"""
    c = estimate_sql_dump_counts_from_text(pg)
    assert c["users"] == 3
    assert c["nodes"] == 1
    assert c["admins"] == 2

    my = """
INSERT INTO `users` VALUES (1,'a'),(2,'b');
INSERT INTO `inbounds` (`id`) VALUES (10);
INSERT INTO `inbounds` (`id`) VALUES (11);
"""
    c2 = estimate_sql_dump_counts_from_text(my)
    assert c2["users"] == 2
    assert c2["inbounds"] == 2
    print("OK: text estimates")


def test_late_users_after_large_ddl_prefix(tmp_path: Path):
    """Timescale-style: CREATE TABLE users early, COPY data many MB later."""
    from app.services.sql_dump_counts import (
        assert_dump_compatible_with_live_users,
        scan_sql_dump_file,
    )

    p = tmp_path / "db_backup.sql"
    prefix = (
        "-- TimescaleDB dump\n"
        "CREATE EXTENSION IF NOT EXISTS timescaledb;\n"
        + ("SELECT 1;\n" * 40_000)
        + "CREATE TABLE public.users (\n    id integer PRIMARY KEY\n);\n"
        + ("-- more ddl\n" * 5_000)
    )
    body = (
        "COPY public.users (id) FROM stdin;\n"
        + "\n".join(str(i) for i in range(417))
        + "\n\\.\n"
    )
    p.write_text(prefix + body, encoding="utf-8")
    # Ensure users COPY sits past the old 2MB-sample / early-read bug window conceptually:
    # prefix alone is hundreds of KB of DDL before any COPY users.
    assert "COPY public.users" not in prefix
    assert p.stat().st_size > 200_000

    meta = scan_sql_dump_file(p)
    assert meta["counts"]["users"] == 417
    assert meta["ddl_seen"]["users"] is True
    assert meta["data_section_seen"]["users"] is True

    err = assert_dump_compatible_with_live_users(
        db_type="timescaledb",
        dump_path=p,
        live_users=417,
        dump_meta=meta,
    )
    assert err is None
    print("OK: late users after DDL prefix")


def test_refuse_confirmed_empty_copy(tmp_path: Path):
    from app.services.sql_dump_counts import (
        assert_dump_compatible_with_live_users,
        scan_sql_dump_file,
    )

    p = tmp_path / "empty.sql"
    p.write_text(
        "CREATE TABLE public.users (id int);\n"
        "COPY public.users (id) FROM stdin;\n"
        "\\.\n",
        encoding="utf-8",
    )
    meta = scan_sql_dump_file(p)
    assert meta["counts"]["users"] == 0
    err = assert_dump_compatible_with_live_users(
        db_type="postgresql",
        dump_path=p,
        live_users=10,
        dump_meta=meta,
    )
    assert err and "0 users" in err
    print("OK: refuse empty COPY")


def test_refuse_ddl_without_data_section(tmp_path: Path):
    from app.services.sql_dump_counts import (
        assert_dump_compatible_with_live_users,
        scan_sql_dump_file,
    )

    p = tmp_path / "ddl_only.sql"
    p.write_text(
        "CREATE TABLE public.users (id int);\nCREATE TABLE public.nodes (id int);\n",
        encoding="utf-8",
    )
    meta = scan_sql_dump_file(p)
    assert meta["counts"]["users"] is None
    err = assert_dump_compatible_with_live_users(
        db_type="mysql",
        dump_path=p,
        live_users=5,
        dump_meta=meta,
    )
    assert err and "no users data" in err
    print("OK: refuse DDL-only")


def test_unknown_format_does_not_refuse(tmp_path: Path):
    """No users DDL and no data — unknown; do not false-positive refuse."""
    from app.services.sql_dump_counts import (
        assert_dump_compatible_with_live_users,
        scan_sql_dump_file,
    )

    p = tmp_path / "other.sql"
    p.write_text("-- custom binary-ish placeholder\nSELECT 1;\n" * 100, encoding="utf-8")
    meta = scan_sql_dump_file(p)
    assert meta["counts"]["users"] is None
    err = assert_dump_compatible_with_live_users(
        db_type="postgresql",
        dump_path=p,
        live_users=100,
        dump_meta=meta,
    )
    assert err is None
    print("OK: unknown format proceeds")


def test_sqlite_refuse_and_ok(tmp_path: Path):
    from app.services.sql_dump_counts import assert_dump_compatible_with_live_users

    p = tmp_path / "db.sqlite3"
    p.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    err = assert_dump_compatible_with_live_users(
        db_type="sqlite",
        dump_path=p,
        live_users=3,
        sqlite_user_count=0,
    )
    assert err and "0 users" in err
    err2 = assert_dump_compatible_with_live_users(
        db_type="sqlite",
        dump_path=p,
        live_users=3,
        sqlite_user_count=3,
    )
    assert err2 is None
    print("OK: sqlite policy")


def test_backup_engine_counts_from_artifact_streams(tmp_path: Path):
    from app.services.backup_engine import _counts_from_dump_artifact

    p = tmp_path / "db_backup.sql"
    pad = "-- pad\n" * 100_000
    p.write_text(
        pad
        + "COPY public.users (id) FROM stdin;\n1\n2\n\\.\n",
        encoding="utf-8",
    )
    c = _counts_from_dump_artifact(p, "timescaledb")
    assert c["users"] == 2
    print("OK: backup_engine streams full file")
