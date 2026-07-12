"""Tests for env migration helpers."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.env_migration import (
    transform_marzban_env,
    transform_compose_marzban_to_pasarguard,
    fix_mysql_dump_for_pasarguard,
    read_env_var,
)


def test_sqlite_env_transform():
    old = '''
SQLALCHEMY_DATABASE_URL = "sqlite:////var/lib/marzban/db.sqlite3"
V2RAY_SUBSCRIPTION_TEMPLATE = "v2ray/default.json"
'''
    out = transform_marzban_env(old, "sqlite")
    assert "/var/lib/pasarguard" in out
    assert "sqlite+aiosqlite" in out
    assert "XRAY_SUBSCRIPTION_TEMPLATE" in out
    assert "xray/default.json" in out
    print("OK: sqlite env transform")


def test_mysql_env_uses_root_password():
    old = '''
MYSQL_ROOT_PASSWORD = "secret123"
SQLALCHEMY_DATABASE_URL = "mysql+pymysql://root:secret123@127.0.0.1/marzban"
'''
    out = transform_marzban_env(old, "mysql")
    assert "mysql+asyncmy" in out
    assert "pasarguard" in out
    assert "secret123" in out
    print("OK: mysql env transform")


def test_compose_transform():
    text = "gozargah/marzban:latest\n/var/lib/marzban:/var/lib/marzban\nMYSQL_DATABASE: marzban"
    out = transform_compose_marzban_to_pasarguard(text)
    assert "pasarguard/panel" in out
    assert "/var/lib/pasarguard" in out
    assert "MYSQL_DATABASE: pasarguard" in out
    print("OK: compose transform")


def test_mysql_dump_fix():
    sql = "CREATE DATABASE marzban;\nUSE marzban;\nINSERT INTO users VALUES (1);"
    out = fix_mysql_dump_for_pasarguard(sql)
    assert "CREATE DATABASE pasarguard" in out or "pasarguard" in out
    print("OK: mysql dump fix")


def test_read_env_var():
    text = 'MYSQL_ROOT_PASSWORD = "abc"\n'
    assert read_env_var(text, "MYSQL_ROOT_PASSWORD") == "abc"
    print("OK: read env var")


if __name__ == "__main__":
    test_sqlite_env_transform()
    test_mysql_env_uses_root_password()
    test_compose_transform()
    test_mysql_dump_fix()
    test_read_env_var()
    print("\nAll env migration tests passed.")
