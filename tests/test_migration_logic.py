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
    assert _suggest_marzban_mode(True, False) == "inplace"
    assert _suggest_marzban_mode(True, True) == "fresh"
    assert _suggest_marzban_mode(False, False) == "fresh"
    print("OK: marzban mode suggestion")


def test_migration_request_marzban_mode():
    from app.models import MigrationRequest
    req = MigrationRequest(source_panel="marzban", source_db="sqlite", target_db="timescaledb", marzban_mode="inplace")
    assert req.marzban_mode == "inplace"
    print("OK: MigrationRequest marzban_mode")


def test_import_migrators():
    from app.services.migrators.marzban import MarzbanMigrator
    from app.services.migrators.pasarguard_db import PasarguardDbMigrator
    from app.services.db_migration import run_db_migration
    assert MarzbanMigrator and PasarguardDbMigrator and run_db_migration
    print("OK: migrator imports")


if __name__ == "__main__":
    test_marzban_panel_exists()
    test_sqlite_to_timescaledb_config()
    test_map_db_types()
    test_target_recommendations()
    test_build_target_urls()
    test_suggest_marzban_mode()
    test_migration_request_marzban_mode()
    test_import_migrators()
    print("\nAll validation tests passed.")
