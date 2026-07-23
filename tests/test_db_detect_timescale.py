"""Tests for Timescale vs PostgreSQL detection + SQL dump count estimates."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.env_migration import detect_db_type_from_env
from app.services.pg_restore import (
    _estimate_sql_table_counts,
    soft_db_family,
)


def test_detect_timescale_from_compose_not_url():
    """Real Timescale installs use postgresql+asyncpg URL — compose decides."""
    env = 'SQLALCHEMY_DATABASE_URL="postgresql+asyncpg://pasarguard:x@127.0.0.1:6432/pasarguard"\n'
    with tempfile.TemporaryDirectory() as td:
        compose = Path(td) / "docker-compose.yml"
        compose.write_text(
            "services:\n  timescaledb:\n    image: timescale/timescaledb:latest-pg17\n  pasarguard:\n    image: x\n",
            encoding="utf-8",
        )
        with patch("app.config.PASARGUARD_DIR", Path(td)):
            assert detect_db_type_from_env(env) == "timescaledb"
    print("OK: timescale detected via compose")


def test_detect_postgresql_from_compose():
    env = 'SQLALCHEMY_DATABASE_URL="postgresql+asyncpg://pasarguard:x@127.0.0.1:6432/pasarguard"\n'
    with tempfile.TemporaryDirectory() as td:
        compose = Path(td) / "docker-compose.yml"
        compose.write_text(
            "services:\n  postgresql:\n    image: postgres:17\n  pasarguard:\n    image: x\n",
            encoding="utf-8",
        )
        with patch("app.config.PASARGUARD_DIR", Path(td)):
            assert detect_db_type_from_env(env) == "postgresql"
    print("OK: postgresql detected via compose")


def test_backup_env_ignores_live_compose():
    """Backup analysis must not inherit live compose service name."""
    env = 'SQLALCHEMY_DATABASE_URL="postgresql+asyncpg://u:p@127.0.0.1:6432/pasarguard"\n'
    with tempfile.TemporaryDirectory() as td:
        compose = Path(td) / "docker-compose.yml"
        compose.write_text("services:\n  timescaledb:\n    image: x\n", encoding="utf-8")
        with patch("app.config.PASARGUARD_DIR", Path(td)):
            assert detect_db_type_from_env(env, prefer_compose=False) == "postgresql"
            assert detect_db_type_from_env(env, prefer_compose=True) == "timescaledb"
    print("OK: prefer_compose=False ignores live compose")


def test_stamp_overrides_url():
    env = '\n'.join([
        'PASARGUARD_DB_ENGINE="timescaledb"',
        'SQLALCHEMY_DATABASE_URL="postgresql+asyncpg://u:p@127.0.0.1:6432/pasarguard"',
    ])
    assert detect_db_type_from_env(env, prefer_compose=False) == "timescaledb"
    print("OK: PASARGUARD_DB_ENGINE stamp wins")


def test_estimate_copy_and_insert_counts():
    sql = """
COPY public.users (id, username) FROM stdin;
1	alice
2	bob
\\.
INSERT INTO hosts (id, remark) VALUES (1, 'h1'), (2, 'h2');
INSERT INTO `groups` (id) VALUES (1);
"""
    counts = _estimate_sql_table_counts(sql)
    assert counts.get("users") == 2
    assert counts.get("hosts", 0) >= 1
    assert counts.get("groups", 0) >= 1
    print("OK: SQL dump row estimates", counts)


def test_soft_family_pg_timescale():
    assert soft_db_family("postgresql", "timescaledb")
    assert not soft_db_family("sqlite", "timescaledb")
    print("OK: soft family")


def test_detect_mariadb_from_compose_not_mysql_url():
    """Real MariaDB installs often use mysql+asyncmy URL — compose decides."""
    env = 'SQLALCHEMY_DATABASE_URL="mysql+asyncmy://pasarguard:x@127.0.0.1:3306/pasarguard"\n'
    with tempfile.TemporaryDirectory() as td:
        compose = Path(td) / "docker-compose.yml"
        compose.write_text(
            "services:\n  mariadb:\n    image: mariadb:11\n  pasarguard:\n    image: x\n",
            encoding="utf-8",
        )
        with patch("app.config.PASARGUARD_DIR", Path(td)):
            assert detect_db_type_from_env(env) == "mariadb"
    print("OK: mariadb detected via compose service")


def test_detect_mariadb_from_mysql_service_mariadb_image():
    """Service named mysql: with image mariadb → mariadb."""
    env = 'SQLALCHEMY_DATABASE_URL="mysql+asyncmy://root:x@127.0.0.1:3306/pasarguard"\n'
    with tempfile.TemporaryDirectory() as td:
        compose = Path(td) / "docker-compose.yml"
        compose.write_text(
            "services:\n  mysql:\n    image: mariadb:lts\n  pasarguard:\n    image: x\n",
            encoding="utf-8",
        )
        with patch("app.config.PASARGUARD_DIR", Path(td)):
            assert detect_db_type_from_env(env) == "mariadb"
    print("OK: mariadb detected via mysql service + mariadb image")


def test_detect_mysql_from_compose():
    env = 'SQLALCHEMY_DATABASE_URL="mysql+asyncmy://root:x@127.0.0.1:3306/pasarguard"\n'
    with tempfile.TemporaryDirectory() as td:
        compose = Path(td) / "docker-compose.yml"
        compose.write_text(
            "services:\n  mysql:\n    image: mysql:8\n  pasarguard:\n    image: x\n",
            encoding="utf-8",
        )
        with patch("app.config.PASARGUARD_DIR", Path(td)):
            assert detect_db_type_from_env(env) == "mysql"
    print("OK: mysql detected via compose")


def test_backup_mysql_url_ignores_live_mariadb_compose():
    env = 'SQLALCHEMY_DATABASE_URL="mysql+asyncmy://u:p@127.0.0.1:3306/pasarguard"\n'
    with tempfile.TemporaryDirectory() as td:
        compose = Path(td) / "docker-compose.yml"
        compose.write_text("services:\n  mariadb:\n    image: mariadb:11\n", encoding="utf-8")
        with patch("app.config.PASARGUARD_DIR", Path(td)):
            assert detect_db_type_from_env(env, prefer_compose=False) == "mysql"
            assert detect_db_type_from_env(env, prefer_compose=True) == "mariadb"
    print("OK: prefer_compose=False ignores live mariadb compose")


def test_explain_auth_mysql_target_not_postgres_tips():
    from app.services.pg_restore import explain_restore_error

    info = explain_restore_error(
        RuntimeError("password authentication failed for user"),
        "timescaledb",
        "mysql",
    )
    blob = " ".join(info.get("causes_fa") or [])
    assert "MYSQL" in blob or "MySQL" in blob or "mysql" in blob.lower()
    assert "POSTGRES_PASSWORD" not in blob or "POSTGRES_*" in blob
    assert "PgBouncer" not in blob
    print("OK: auth tips are MySQL-aware for timescale→mysql")


if __name__ == "__main__":
    test_detect_timescale_from_compose_not_url()
    test_detect_postgresql_from_compose()
    test_backup_env_ignores_live_compose()
    test_stamp_overrides_url()
    test_estimate_copy_and_insert_counts()
    test_soft_family_pg_timescale()
    test_detect_mariadb_from_compose_not_mysql_url()
    test_detect_mariadb_from_mysql_service_mariadb_image()
    test_detect_mysql_from_compose()
    test_backup_mysql_url_ignores_live_mariadb_compose()
    test_explain_auth_mysql_target_not_postgres_tips()
    print("\nAll detect/estimate tests passed")
