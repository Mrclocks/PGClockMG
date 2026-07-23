"""Tests for live DB credential resolution."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.db_auth import (
    migration_params_from_connection,
    mysql_password_candidates,
    postgres_password_candidates,
    postgres_admin_users,
    target_database_name,
)
from app.services.db_credentials import get_target_connection
from unittest.mock import MagicMock, patch


ENV_PG = """
DB_USER=pasarguard
DB_PASSWORD=app_secret
DB_NAME=pasarguard
POSTGRES_USER=postgres
POSTGRES_PASSWORD=super_secret
POSTGRES_DB=pasarguard
SQLALCHEMY_DATABASE_URL=postgresql+asyncpg://pasarguard:app_secret@127.0.0.1:6432/pasarguard
"""


def test_postgres_password_candidates_order():
    cands = postgres_password_candidates(ENV_PG)
    assert cands[0] == "super_secret"
    assert "app_secret" in cands
    print("OK: postgres password candidate order")


def test_postgres_admin_users():
    users = postgres_admin_users(ENV_PG)
    assert users[0] == "pasarguard"
    assert "postgres" in users
    print("OK: postgres admin users")


def test_target_database_name_pg():
    assert target_database_name(ENV_PG, "timescaledb") == "pasarguard"
    print("OK: target database name")


def test_migration_params_from_connection():
    admin = {
        "user": "postgres",
        "password": "super_secret",
        "database": "pasarguard",
        "host": "127.0.0.1",
        "port": "5432",
        "db_type": "timescaledb",
    }
    p = migration_params_from_connection("sqlite", "timescaledb", admin)
    assert p["_resolved_target_conn"]["user"] == "postgres"
    assert p["_resolved_target_conn"]["password"] == "super_secret"
    assert p["target_db"] == "timescaledb"
    print("OK: migration params from connection")


def test_get_target_uses_resolved_conn():
    params = {
        "target_db": "timescaledb",
        "_resolved_target_conn": {
            "user": "postgres",
            "password": "live_probe_ok",
            "database": "pasarguard",
            "host": "127.0.0.1",
            "port": "5432",
            "db_type": "timescaledb",
        },
        "target_db_password": "wrong",
    }
    conn = get_target_connection(params)
    assert conn["password"] == "live_probe_ok"
    assert conn["user"] == "postgres"
    print("OK: resolved conn bypasses wizard password")


def test_get_target_wizard_password_when_manual():
    fake_env = MagicMock()
    fake_env.exists.return_value = True

    def fake_admin(target_db, password_override=None, env_text=None):
        return {
            "user": "root",
            "password": "fromenv",
            "database": "pasarguard",
            "host": "127.0.0.1",
            "port": "3306",
            "db_type": target_db,
        }

    params = {
        "target_db": "mysql",
        "target_db_user": "pasarguard",
        "target_db_name": "pasarguard",
        "target_db_password": "wizardpwd",
    }
    with patch("app.services.db_credentials.PASARGUARD_ENV", fake_env), patch(
        "app.services.env_migration.get_pasarguard_admin_connection",
        fake_admin,
    ):
        conn = get_target_connection(params)
    assert conn["password"] == "wizardpwd"
    print("OK: manual wizard password preserved")


def test_mysql_password_candidates():
    env = "MYSQL_ROOT_PASSWORD=rootpw\nDB_PASSWORD=apppw\n"
    c = mysql_password_candidates(env)
    assert c[0] == "rootpw"
    assert "apppw" in c
    print("OK: mysql password candidates")


def test_mysql_password_candidates_from_sqlalchemy_url():
    env = (
        'SQLALCHEMY_DATABASE_URL="mysql+asyncmy://pasarguard:urlsecret@127.0.0.1:3306/pasarguard"\n'
        "DB_PASSWORD=apppw\n"
    )
    c = mysql_password_candidates(env)
    assert "urlsecret" in c
    assert "apppw" in c
    print("OK: mysql password from SQLAlchemy URL")


def test_explain_auth_mariadb_target_from_timescale():
    from app.services.pg_restore import explain_restore_error

    info = explain_restore_error(
        RuntimeError("MySQL/MariaDB authentication failed — check MYSQL_ROOT_PASSWORD"),
        "timescaledb",
        "mariadb",
    )
    blob = "\n".join(info.get("causes_fa") or [])
    assert "MYSQL" in blob or "MariaDB" in blob or "mariadb" in blob.lower()
    assert "PgBouncer" not in blob
    assert "POSTGRES_PASSWORD" not in blob
    print("OK: timescale→mariadb auth tips are MySQL-aware")


if __name__ == "__main__":
    test_postgres_password_candidates_order()
    test_postgres_admin_users()
    test_target_database_name_pg()
    test_migration_params_from_connection()
    test_get_target_uses_resolved_conn()
    test_get_target_wizard_password_when_manual()
    test_mysql_password_candidates()
    test_mysql_password_candidates_from_sqlalchemy_url()
    test_explain_auth_mariadb_target_from_timescale()
    print("\nAll db_auth tests passed")
