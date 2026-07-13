"""Tests for manual DB credential parsing."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.db_credentials import (
    get_target_connection,
    build_migration_url,
    validate_db_credentials,
)


def test_target_merges_env_identity():
    """Wizard password + .env user (e.g. root for MySQL), not hardcoded pasarguard."""
    fake_env = MagicMock()
    fake_env.exists.return_value = True

    def fake_pg_conn(target_db, password_override=None, env_text=None):
        assert password_override == "wizardpwd"
        return {
            "user": "root",
            "database": "pasarguard",
            "host": "127.0.0.1",
            "port": "3306",
            "password": "fromenv",
            "db_type": target_db,
        }

    params = {
        "target_db": "mysql",
        "target_db_user": "pasarguard",
        "target_db_name": "pasarguard",
        "target_db_password": "wizardpwd",
        "target_db_host": "127.0.0.1",
        "target_db_port": "3306",
    }
    with patch("app.services.db_credentials.PASARGUARD_ENV", fake_env), patch(
        "app.services.env_migration.get_pasarguard_target_connection",
        fake_pg_conn,
    ):
        conn = get_target_connection(params)
    assert conn["user"] == "root"
    assert conn["password"] == "wizardpwd"
    assert conn["database"] == "pasarguard"
    with patch("app.services.db_credentials.PASARGUARD_ENV", fake_env), patch(
        "app.services.env_migration.get_pasarguard_target_connection",
        fake_pg_conn,
    ):
        url = build_migration_url(params)
    assert "root:wizardpwd@" in url
    print("OK: target merges env identity")


def test_manual_target_credentials():
    fake_env = MagicMock()
    fake_env.exists.return_value = False

    params = {
        "target_db": "postgresql",
        "target_db_user": "pasarguard",
        "target_db_name": "pasarguard",
        "target_db_password": "secret123",
        "target_db_host": "127.0.0.1",
        "target_db_port": 6432,
    }
    with patch("app.services.db_credentials.PASARGUARD_ENV", fake_env):
        conn = get_target_connection(params)
    assert conn["user"] == "pasarguard"
    assert conn["database"] == "pasarguard"
    assert conn["password"] == "secret123"
    with patch("app.services.db_credentials.PASARGUARD_ENV", fake_env):
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
    test_target_merges_env_identity()
    test_manual_target_credentials()
    test_validate_requires_fields()
    print("\nAll db credential tests passed.")
