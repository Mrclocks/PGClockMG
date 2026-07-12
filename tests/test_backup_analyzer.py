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


if __name__ == "__main__":
    test_nested_marzban_zip_sqlite()
    test_mysql_sql_dump()
    test_incomplete_zip()
    print("\nAll backup analyzer tests passed.")
