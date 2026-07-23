"""Conversion matrix and install-guide helpers."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_can_convert_databases():
    from app.panels import can_convert_databases

    assert can_convert_databases("sqlite", "mysql")
    assert can_convert_databases("sqlite", "timescaledb")
    assert can_convert_databases("sqlite", "sqlite")
    assert can_convert_databases("mysql", "mariadb")
    assert can_convert_databases("mariadb", "mysql")
    assert can_convert_databases("postgresql", "timescaledb")
    assert can_convert_databases("timescaledb", "postgresql")
    assert can_convert_databases("mysql", "postgresql")
    assert can_convert_databases("timescaledb", "mysql")
    assert not can_convert_databases("mysql", "sqlite")
    assert not can_convert_databases("postgresql", "sqlite")
    assert not can_convert_databases("timescaledb", "sqlite")
    assert not can_convert_databases("mariadb", "sqlite")
    print("OK: can_convert_databases matrix")


def test_migration_strategy_blocks_to_sqlite():
    from app.services.native_migration.cross_db import migration_strategy

    assert migration_strategy("sqlite", "mysql") == "two_phase"
    assert migration_strategy("mysql", "postgresql") == "two_phase"
    assert migration_strategy("mysql", "sqlite") == "unsupported"
    assert migration_strategy("timescaledb", "sqlite") == "unsupported"
    assert migration_strategy("mysql", "mysql") == "same_db"
    print("OK: migration_strategy blocks *→sqlite")


def test_install_commands_present():
    from app.panels import PASARGUARD_INSTALL_COMMANDS, PASARGUARD_INSTALL_DBS, OWNER_TEMP_KEY_CMD

    assert PASARGUARD_INSTALL_DBS[0] == "timescaledb"
    for db in PASARGUARD_INSTALL_DBS:
        assert db in PASARGUARD_INSTALL_COMMANDS
        assert "pasarguard.sh" in PASARGUARD_INSTALL_COMMANDS[db]["cmd"]
    assert "generate-temp-key" in OWNER_TEMP_KEY_CMD
    print("OK: install guide commands")


if __name__ == "__main__":
    test_can_convert_databases()
    test_migration_strategy_blocks_to_sqlite()
    test_install_commands_present()
    print("\nAll convert/install-guide tests passed.")
