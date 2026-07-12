"""Tests for manual DB credential parsing."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.db_credentials import (
    get_target_connection,
    build_migration_url,
    validate_db_credentials,
)


def test_manual_target_credentials():
    params = {
        "target_db": "postgresql",
        "target_db_user": "pasarguard",
        "target_db_name": "pasarguard",
        "target_db_password": "secret123",
        "target_db_host": "127.0.0.1",
        "target_db_port": 6432,
    }
    conn = get_target_connection(params)
    assert conn["user"] == "pasarguard"
    assert conn["database"] == "pasarguard"
    assert conn["password"] == "secret123"
    url = build_migration_url(params)
    assert ":5432/" in url
    assert "pasarguard:secret123" in url
    print("OK: manual target credentials")


def test_validate_requires_fields():
    params = {"target_db": "postgresql", "source_db": "sqlite"}
    assert validate_db_credentials(params, "target")
    missing = validate_db_credentials(
        {
            "target_db": "postgresql",
            "target_db_user": "u",
            "target_db_name": "d",
            "target_db_password": "p",
            "source_db": "sqlite",
        },
        "target",
    )
    assert missing == []
    print("OK: validate db credentials")


if __name__ == "__main__":
    test_manual_target_credentials()
    test_validate_requires_fields()
    print("\nAll db credential tests passed.")
