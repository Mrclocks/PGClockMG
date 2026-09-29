"""Tests for backup zip analysis."""

import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.backup_analyzer import analyze_upload_directory


def test_nested_marzban_zip_sqlite():
    with tempfile.TemporaryDirectory() as tmp:
        upload_dir = Path(tmp)
        extracted = upload_dir / "extracted"
        data = extracted / "var" / "lib" / "marzban"
        data.mkdir(parents=True)
        (data / "db.sqlite3").write_bytes(b"sqlite-data")
        (data / ".env").write_text(
            'SQLALCHEMY_DATABASE_URL = "sqlite:////var/lib/marzban/db.sqlite3"\n'
            'V2RAY_SUBSCRIPTION_TEMPLATE = "v2ray/default.json"\n',
            encoding="utf-8",
        )
        (data / "xray_config.json").write_text(
            '{"certificates":[{"certificateFile":"/var/lib/marzban/certs/x/fullchain.pem"}]}',
            encoding="utf-8",
        )

        result = analyze_upload_directory(upload_dir)
        assert result["panel_hint"] == "marzban"
        assert result["detected_source_db"] == "sqlite"
        assert result["backup_ok"] is True
        assert result["password_candidates"] == []
        assert result["mysql_password_found"] is False
        assert result["categories"].get("database_sqlite", 0) >= 1
        assert result["has_xray_config"] is True
        assert any(m["from"] == "V2RAY_SUBSCRIPTION_TEMPLATE" for m in result["env_mapping"])
        print("OK: nested sqlite zip")


def test_mysql_sql_dump():
    with tempfile.TemporaryDirectory() as tmp:
        upload_dir = Path(tmp)
        ext = upload_dir / "extracted"
        ext.mkdir()
        (ext / "marzban.sql").write_text("CREATE DATABASE marzban;\nUSE marzban;\n", encoding="utf-8")
        (ext / ".env").write_text(
            'MYSQL_ROOT_PASSWORD = "sec"\n'
            'SQLALCHEMY_DATABASE_URL = "mysql+pymysql://root:sec@127.0.0.1/marzban"\n',
            encoding="utf-8",
        )

        result = analyze_upload_directory(upload_dir)
        assert result["detected_source_db"] == "mysql"
        assert result["backup_ok"] is True
        assert result["mysql_password_found"] is True
        print("OK: mysql sql zip")


def test_incomplete_zip():
    with tempfile.TemporaryDirectory() as tmp:
        upload_dir = Path(tmp)
        ext = upload_dir / "extracted"
        ext.mkdir()
        (ext / "readme.txt").write_text("no db here", encoding="utf-8")

        result = analyze_upload_directory(upload_dir)
        assert result["backup_ok"] is False
        print("OK: incomplete zip")


def test_xui_db_in_extracted_zip():
    with tempfile.TemporaryDirectory() as tmp:
        upload_dir = Path(tmp)
        ext = upload_dir / "extracted"
        ext.mkdir()
        (ext / "x-ui.db").write_bytes(b"xui-sqlite")

        result = analyze_upload_directory(upload_dir)
        assert result["panel_hint"] == "3x-ui"
        assert result["detected_source_db"] == "sqlite"
        assert result["backup_ok"] is True
        assert result["categories"].get("database_sqlite", 0) >= 1
        print("OK: x-ui zip")


def test_marzban_not_misdetected_as_xui():
    """Mentions of x-ui in unrelated filenames must not flip Marzban backups."""
    with tempfile.TemporaryDirectory() as tmp:
        upload_dir = Path(tmp)
        data = upload_dir / "extracted" / "var" / "lib" / "marzban"
        data.mkdir(parents=True)
        (data / "db.sqlite3").write_bytes(b"sqlite-data")
        (data / "notes-about-x-ui.txt").write_text("migration notes", encoding="utf-8")
        (data / ".env").write_text(
            'SQLALCHEMY_DATABASE_URL = "sqlite:////var/lib/marzban/db.sqlite3"\n',
            encoding="utf-8",
        )

        result = analyze_upload_directory(upload_dir)
        assert result["panel_hint"] == "marzban"
        assert result["backup_ok"] is True
        print("OK: marzban not misdetected")


def test_plain_sqlite_still_marzban_hint():
    with tempfile.TemporaryDirectory() as tmp:
        upload_dir = Path(tmp)
        ext = upload_dir / "extracted"
        ext.mkdir()
        (ext / "db.sqlite3").write_bytes(b"m")
        result = analyze_upload_directory(upload_dir)
        assert result["panel_hint"] == "marzban"
        assert result["backup_ok"] is True
        print("OK: plain sqlite marzban")


def test_sqlite_dump_ignores_stale_mysql_env_passwords():
    """SQLite file + stale MySQL .env must detect sqlite and never ask for passwords."""
    with tempfile.TemporaryDirectory() as tmp:
        upload_dir = Path(tmp)
        data = upload_dir / "extracted" / "var" / "lib" / "marzban"
        data.mkdir(parents=True)
        (data / "db.sqlite3").write_bytes(b"sqlite-data")
        (data / ".env").write_text(
            'SQLALCHEMY_DATABASE_URL = "mysql+pymysql://root:sec@127.0.0.1/marzban"\n'
            'MYSQL_ROOT_PASSWORD = "sec"\n',
            encoding="utf-8",
        )
        result = analyze_upload_directory(upload_dir)
        assert result["detected_source_db"] == "sqlite"
        assert result["backup_ok"] is True
        assert result["password_candidates"] == []
        assert result["mysql_password_found"] is False
        print("OK: sqlite dump wins over stale mysql .env")


def test_marzban_sqlite_not_contaminated_by_live_timescale_compose():
    """Regression: PasarGuard Timescale install must NOT become Marzban source.

    User installs PasarGuard+Timescale on a new server, uploads Marzban sqlite
    backup — wizard must show source=sqlite with no password, not timescaledb.
    """
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # Fake live PasarGuard Timescale compose (the contamination source)
        live_pg = tmp_path / "pasarguard"
        live_pg.mkdir()
        (live_pg / "docker-compose.yml").write_text(
            "services:\n  timescaledb:\n    image: timescale/timescaledb:latest-pg17\n",
            encoding="utf-8",
        )
        (live_pg / ".env").write_text(
            'PASARGUARD_DB_ENGINE="timescaledb"\n'
            'SQLALCHEMY_DATABASE_URL="postgresql+asyncpg://postgres:x@timescaledb:5432/pasarguard"\n'
            'POSTGRES_PASSWORD="x"\n',
            encoding="utf-8",
        )

        upload_dir = tmp_path / "upload"
        data = upload_dir / "extracted" / "var" / "lib" / "marzban"
        data.mkdir(parents=True)
        (data / "db.sqlite3").write_bytes(b"sqlite-data")
        (data / ".env").write_text(
            'SQLALCHEMY_DATABASE_URL = "sqlite:////var/lib/marzban/db.sqlite3"\n',
            encoding="utf-8",
        )

        with patch("app.config.PASARGUARD_DIR", live_pg):
            # Even if prefer_compose=True would return timescaledb, upload analysis must not.
            from app.services.env_migration import detect_db_type_from_env

            env = (data / ".env").read_text(encoding="utf-8")
            assert detect_db_type_from_env(env, prefer_compose=True) == "timescaledb"
            assert detect_db_type_from_env(env, prefer_compose=False) == "sqlite"

            result = analyze_upload_directory(upload_dir)
            assert result["detected_source_db"] == "sqlite", result
            assert result["panel_hint"] == "marzban"
            assert result["backup_ok"] is True
            assert result["password_candidates"] == []
            assert result["mysql_password_found"] is False
        print("OK: marzban sqlite not contaminated by live Timescale compose")


def test_marzban_sqlite_env_only_not_contaminated_by_live_compose():
    """Even without finding db.sqlite3 path quirks — env sqlite must stay sqlite."""
    from unittest.mock import patch
    from app.services.backup_analyzer import detect_db_from_env

    with tempfile.TemporaryDirectory() as tmp:
        live_pg = Path(tmp) / "pasarguard"
        live_pg.mkdir()
        (live_pg / "docker-compose.yml").write_text(
            "services:\n  timescaledb:\n    image: timescale/timescaledb:latest-pg17\n",
            encoding="utf-8",
        )
        env = 'SQLALCHEMY_DATABASE_URL = "sqlite:////var/lib/marzban/db.sqlite3"\n'
        with patch("app.config.PASARGUARD_DIR", live_pg):
            assert detect_db_from_env(env) == "sqlite"
        print("OK: detect_db_from_env ignores live compose")


if __name__ == "__main__":
    test_nested_marzban_zip_sqlite()
    test_mysql_sql_dump()
    test_incomplete_zip()
    test_xui_db_in_extracted_zip()
    test_marzban_not_misdetected_as_xui()
    test_plain_sqlite_still_marzban_hint()
    test_sqlite_dump_ignores_stale_mysql_env_passwords()
    test_marzban_sqlite_not_contaminated_by_live_timescale_compose()
    test_marzban_sqlite_env_only_not_contaminated_by_live_compose()
    print("\nAll backup analyzer tests passed.")
