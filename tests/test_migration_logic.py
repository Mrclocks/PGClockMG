"""Tests for migration config and panel logic (no Docker required)."""

import sys
from pathlib import Path

# Add project root
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.panels import PANELS, TARGET_DB_RECOMMENDATIONS
from app.services.db_migration import write_migration_config, build_target_url, map_db_type


def test_marzban_panel_exists():
    assert "marzban" in PANELS
    p = PANELS["marzban"]
    assert p.subscription_mode == "native"
    assert "sqlite" in p.supported_source_dbs


def test_sqlite_to_timescaledb_config():
    config = write_migration_config(
        "test01",
        "/tmp/marzban.db.sqlite3",
        "sqlite",
        "timescaledb",
        "postgresql+asyncpg://postgres:secret@localhost:5432/pasarguard",
    )
    text = config.read_text()
    assert 'type: "sqlite"' in text
    assert "/tmp/marzban.db.sqlite3" in text
    assert 'type: "postgres"' in text
    assert "asyncpg" in text
    print("OK: sqlite -> timescaledb config")


def test_map_db_types():
    assert map_db_type("timescaledb") == "postgres"
    assert map_db_type("postgresql") == "postgres"
    assert map_db_type("sqlite") == "sqlite"


def test_target_recommendations():
    recs = TARGET_DB_RECOMMENDATIONS["sqlite"]
    assert "timescaledb" in recs


def test_build_target_urls():
    assert "sqlite" in build_target_url("sqlite", None)
    assert "asyncpg" in build_target_url("timescaledb", "pass")
    assert "pymysql" in build_target_url("mysql", "pass")


def test_suggest_marzban_mode():
    from app.services.prerequisites import _suggest_marzban_mode
    assert _suggest_marzban_mode(True, False) == "fresh"
    assert _suggest_marzban_mode(True, True) == "fresh"
    assert _suggest_marzban_mode(False, False) == "fresh"
    print("OK: marzban mode suggestion")


def test_migration_request_marzban_mode():
    from app.models import MigrationRequest
    req = MigrationRequest(source_panel="marzban", source_db="sqlite", target_db="timescaledb")
    assert req.marzban_mode == "fresh"
    print("OK: MigrationRequest marzban_mode")


def test_read_sqlite_alembic_version(tmp_path=None):
    from app.services.pasarguard_ops import read_sqlite_alembic_version
    import sqlite3
    import tempfile
    import os
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        conn.execute("INSERT INTO alembic_version VALUES ('2b231de97dc3')")
        conn.commit()
        conn.close()
        assert read_sqlite_alembic_version(path) == "2b231de97dc3"
        print("OK: read_sqlite_alembic_version")
    finally:
        os.unlink(path)


def test_extract_env_summary():
    from app.services.env_migration import extract_env_summary
    text = '''
SQLALCHEMY_DATABASE_URL = "mysql+asyncmy://root:secret123@127.0.0.1/pasarguard"
MYSQL_ROOT_PASSWORD = secret123
UVICORN_PORT = 8443
'''
    s = extract_env_summary(text)
    assert s["db_type"] == "mysql"
    assert s["db_password"] == "secret123"
    assert s["db_name"] == "pasarguard"
    assert s["db_user"] == "root"
    assert s["panel_port"] == "8443"
    print("OK: extract_env_summary")


def test_parse_sqlalchemy_urls():
    from app.services.env_migration import parse_sqlalchemy_url, build_db_migration_target_url
    pg = parse_sqlalchemy_url("postgresql+asyncpg://pguser:pgpass@dbhost:5433/mydb")
    assert pg["user"] == "pguser"
    assert pg["password"] == "pgpass"
    assert pg["database"] == "mydb"
    assert pg["port"] == "5433"
    print("OK: parse_sqlalchemy_url")


def test_pasarguard_install_dbs():
    from app.panels import PASARGUARD_INSTALL_DBS
    assert "sqlite" in PASARGUARD_INSTALL_DBS
    assert "timescaledb" in PASARGUARD_INSTALL_DBS
    print("OK: PASARGUARD_INSTALL_DBS")


def test_import_migrators():
    from app.services.migrators.marzban import MarzbanMigrator
    from app.services.migrators.pasarguard_db import PasarguardDbMigrator
    from app.services.db_migration import run_db_migration
    assert MarzbanMigrator and PasarguardDbMigrator and run_db_migration
    print("OK: migrator imports")


def test_system_status():
    from app.services.prerequisites import get_system_status
    status = get_system_status()
    assert "pasarguard" in status
    assert "marzban" in status
    assert "docker" in status
    print("OK: system status")


if __name__ == "__main__":
    test_marzban_panel_exists()
    test_sqlite_to_timescaledb_config()
    test_map_db_types()
    test_target_recommendations()
    test_build_target_urls()
    test_suggest_marzban_mode()
    test_migration_request_marzban_mode()
    test_extract_env_summary()
    test_parse_sqlalchemy_urls()
    test_read_sqlite_alembic_version()
    test_pasarguard_install_dbs()
    test_import_migrators()
    test_system_status()
    print("\nAll validation tests passed.")
