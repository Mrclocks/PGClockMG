"""Unit tests for smart PasarGuard restore helpers (all DB families)."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.pg_restore import (
    soft_db_family,
    filter_timescaledb_extension_sql,
    parse_timescale_wanted,
    detect_ts_mismatch_from_text,
    is_auth_failure_text,
    _sql_literal,
    _set_env_var,
    _parse_manifest_ts_versions,
    analyze_pasarguard_backup,
)


def test_soft_db_family_matrix():
    assert soft_db_family("mysql", "mariadb")
    assert soft_db_family("mariadb", "mysql")
    assert soft_db_family("postgresql", "timescaledb")
    assert soft_db_family("timescaledb", "postgresql")
    assert soft_db_family("sqlite", "sqlite")
    assert not soft_db_family("sqlite", "mysql")
    assert not soft_db_family("mysql", "timescaledb")
    assert not soft_db_family("postgresql", "mysql")
    assert not soft_db_family(None, "mysql")
    print("OK: soft_db_family matrix")


def test_filter_timescaledb_extension_sql():
    sql = "\n".join([
        "CREATE TABLE t(id int);",
        "CREATE EXTENSION timescaledb CASCADE;",
        "CREATE EXTENSION IF NOT EXISTS timescaledb;",
        "DROP EXTENSION IF EXISTS timescaledb;",
        "INSERT INTO t VALUES (1);",
    ])
    out = filter_timescaledb_extension_sql(sql)
    assert "CREATE TABLE" in out
    assert "INSERT INTO" in out
    assert "timescaledb" not in out.lower()
    print("OK: filter timescaledb extension sql")


def test_filter_timescaledb_strip_all_for_plain_pg():
    sql = "\n".join([
        "CREATE TABLE users (id int);",
        "CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;",
        "SELECT create_hypertable('metrics', 'time');",
        "SELECT timescaledb_pre_restore();",
        "COMMENT ON EXTENSION timescaledb IS 'x';",
        "INSERT INTO users VALUES (1);",
    ])
    out = filter_timescaledb_extension_sql(sql, strip_all=True)
    assert "CREATE TABLE users" in out
    assert "INSERT INTO users" in out
    assert "timescaledb" not in out.lower()
    assert "create_hypertable" not in out.lower()
    print("OK: strip_all timescaledb for plain PostgreSQL")


def test_parse_timescale_wanted():
    assert parse_timescale_wanted(["2.28.1", "2.28.1"]) == "2.28.1"
    assert parse_timescale_wanted(["latest", "2.17.2"]) == "2.17.2"
    assert parse_timescale_wanted([]) is None
    print("OK: parse_timescale_wanted")


def test_detect_ts_mismatch_from_official_error():
    text = """
ERROR: TimescaleDB version mismatch for database "pasarguard"
  Backup version: 2.28.1
  Target server version: 2.28.2
The restore was stopped BEFORE changing anything
"""
    pair = detect_ts_mismatch_from_text(text)
    assert pair == ("2.28.1", "2.28.2")
    print("OK: detect timescale mismatch text")


def test_is_auth_failure_text():
    assert is_auth_failure_text("asyncpg.exceptions.ProtocolViolationError: SASL authentication failed")
    assert is_auth_failure_text("password authentication failed for user pasarguard")
    assert is_auth_failure_text("Access denied for user 'root'@'%'")
    assert not is_auth_failure_text("Application startup complete")
    print("OK: auth failure detection")


def test_sql_literal_escapes_quotes():
    assert _sql_literal("a'b") == "'a''b'"
    print("OK: sql literal")


def test_merge_env_preserves_password():
    backup = 'DB_PASSWORD="old"\nSQLALCHEMY_DATABASE_URL="x"\n'
    text = backup
    text = _set_env_var(text, "DB_PASSWORD", "live-secret")
    text = _set_env_var(text, "POSTGRES_PASSWORD", "live-secret")
    assert 'DB_PASSWORD="live-secret"' in text
    assert 'POSTGRES_PASSWORD="live-secret"' in text
    print("OK: env password preserve")


def test_parse_manifest_ts_versions():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pg = root / "pg_dump"
        pg.mkdir()
        (pg / "manifest.tsv").write_text(
            "pasarguard\tpasarguard\t1\tpasarguard.sql\t2.28.1\n",
            encoding="utf-8",
        )
        assert _parse_manifest_ts_versions(root) == ["2.28.1"]
    print("OK: manifest timescale versions")


def _make_backup_zip(dest: Path, db_url: str, layout: str = "single") -> Path:
    work = dest / "content"
    work.mkdir(parents=True, exist_ok=True)
    (work / ".env").write_text(f'SQLALCHEMY_DATABASE_URL="{db_url}"\nDB_PASSWORD="x"\n', encoding="utf-8")
    if layout == "sqlite":
        (work / "db.sqlite3").write_bytes(b"SQLite format 3\x00")
    elif layout == "multi":
        pg = work / "pg_dump"
        pg.mkdir()
        (pg / "manifest.tsv").write_text(
            "pasarguard\tpasarguard\t1\tdump.sql\t2.28.1\n", encoding="utf-8"
        )
        (pg / "dump.sql").write_text("-- dump\n", encoding="utf-8")
    else:
        (work / "db_backup.sql").write_text("-- dump\n", encoding="utf-8")
    zpath = dest / "backup.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for p in work.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(work).as_posix())
    return zpath


def test_analyze_all_db_types():
    """Analyze each DB family zip without requiring PasarGuard installed."""
    import tempfile
    import shutil
    import app.services.pg_restore as mod

    base = Path(tempfile.mkdtemp(prefix="pg-restore-zips-"))
    cases = [
        ("sqlite", "sqlite+aiosqlite:////var/lib/pasarguard/db.sqlite3", "sqlite"),
        ("mysql", "mysql+asyncmy://u:p@127.0.0.1/pasarguard", "single"),
        ("mariadb", "mariadb+asyncmy://u:p@127.0.0.1/pasarguard", "single"),
        ("postgresql", "postgresql+asyncpg://u:p@127.0.0.1/pasarguard", "single"),
        ("timescaledb", "postgresql+asyncpg://u:p@timescaledb:5432/pasarguard", "multi"),
    ]

    orig_installed = mod.is_pasarguard_installed
    orig_db = mod.get_pasarguard_db_type
    mod.is_pasarguard_installed = lambda: True  # type: ignore
    try:
        for name, url, layout in cases:
            mod.get_pasarguard_db_type = lambda n=name: n  # type: ignore
            z = _make_backup_zip(base / name, url, layout=layout)
            a = analyze_pasarguard_backup(path=z)
            assert a["backup_db"]
            assert a["layout"] in ("sqlite_file", "single", "multi")
            assert a["ok"] is True
            print(f"OK: analyze {name} layout={a['layout']} backup_db={a['backup_db']}")
    finally:
        mod.is_pasarguard_installed = orig_installed  # type: ignore
        mod.get_pasarguard_db_type = orig_db  # type: ignore
        shutil.rmtree(base, ignore_errors=True)


def test_analyze_experimental_hard_mismatch():
    import tempfile
    import shutil
    import app.services.pg_restore as mod

    base = Path(tempfile.mkdtemp(prefix="pg-restore-mismatch-"))
    z = _make_backup_zip(base, "mysql+asyncmy://u:p@127.0.0.1/pasarguard", layout="single")
    orig_installed = mod.is_pasarguard_installed
    orig_db = mod.get_pasarguard_db_type
    mod.is_pasarguard_installed = lambda: True  # type: ignore
    mod.get_pasarguard_db_type = lambda: "timescaledb"  # type: ignore
    try:
        a = analyze_pasarguard_backup(path=z)
        assert a["ok"] is True
        assert a["experimental_db_change"] is True
        assert a["soft_match"] is False
        print("OK: experimental hard mismatch flagged")
    finally:
        mod.is_pasarguard_installed = orig_installed  # type: ignore
        mod.get_pasarguard_db_type = orig_db  # type: ignore
        shutil.rmtree(base, ignore_errors=True)

if __name__ == "__main__":
    test_soft_db_family_matrix()
    test_filter_timescaledb_extension_sql()
    test_filter_timescaledb_strip_all_for_plain_pg()
    test_parse_timescale_wanted()
    test_detect_ts_mismatch_from_official_error()
    test_is_auth_failure_text()
    test_sql_literal_escapes_quotes()
    test_merge_env_preserves_password()
    test_parse_manifest_ts_versions()
    test_analyze_all_db_types()
    test_analyze_experimental_hard_mismatch()
    print("\nAll pg_restore tests passed")
