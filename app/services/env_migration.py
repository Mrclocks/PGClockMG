"""Marzban → PasarGuard .env transformation (official docs)."""

import re
from pathlib import Path

PATH_REPLACEMENTS = [
    ("/opt/marzban", "/opt/pasarguard"),
    ("/var/lib/marzban", "/var/lib/pasarguard"),
    ("/var/lib/mysql/marzban", "/var/lib/mysql/pasarguard"),
]


def read_env_var(text: str, key: str) -> str | None:
    pattern = rf'^\s*{re.escape(key)}\s*=\s*["\']?([^"\'#\n]+)["\']?'
    m = re.search(pattern, text, re.MULTILINE | re.IGNORECASE)
    if not m:
        return None
    return m.group(1).strip().strip('"')


def transform_marzban_env(
    text: str,
    target_db: str,
    password_override: str | None = None,
) -> str:
    """
    Convert Marzban .env to PasarGuard .env per:
    https://docs.pasarguard.org/en/migration/marzban/
    """
    for old, new in PATH_REPLACEMENTS:
        text = text.replace(old, new)

    text = re.sub(r"V2RAY_SUBSCRIPTION_TEMPLATE", "XRAY_SUBSCRIPTION_TEMPLATE", text, flags=re.I)
    text = text.replace("v2ray/", "xray/")
    text = text.replace("V2ray/", "xray/")

    text = re.sub(r"(?m)^(\s*MYSQL_DATABASE\s*=\s*)marzban\s*$", r"\1pasarguard", text, flags=re.I)

    mysql_pwd = (
        password_override
        or read_env_var(text, "MYSQL_ROOT_PASSWORD")
        or read_env_var(text, "MYSQL_PASSWORD")
        or "password"
    )
    pg_pwd = password_override or read_env_var(text, "POSTGRES_PASSWORD") or "password"

    if target_db == "sqlite":
        db_url = 'SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite:////var/lib/pasarguard/db.sqlite3"'
    elif target_db in ("mysql", "mariadb"):
        db_url = f'SQLALCHEMY_DATABASE_URL = "mysql+asyncmy://root:{mysql_pwd}@127.0.0.1/pasarguard"'
    else:
        db_url = f'SQLALCHEMY_DATABASE_URL = "postgresql+asyncpg://postgres:{pg_pwd}@localhost:5432/pasarguard"'

    if re.search(r"SQLALCHEMY_DATABASE_URL", text, re.I):
        text = re.sub(
            r'#\s*SQLALCHEMY_DATABASE_URL\s*=\s*"[^"]*"|SQLALCHEMY_DATABASE_URL\s*=\s*"[^"]*"',
            db_url,
            text,
            count=1,
        )
    else:
        text = text.rstrip() + f"\n{db_url}\n"

    return text


def transform_pasarguard_env_for_target(text: str, target_db: str, password: str | None = None) -> str:
    """Update existing PasarGuard .env when only DB backend changes."""
    pwd = password or read_env_var(text, "MYSQL_ROOT_PASSWORD") or read_env_var(text, "POSTGRES_PASSWORD") or "password"
    if target_db == "sqlite":
        url = 'SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite:////var/lib/pasarguard/db.sqlite3"'
    elif target_db in ("mysql", "mariadb"):
        url = f'SQLALCHEMY_DATABASE_URL = "mysql+asyncmy://root:{pwd}@127.0.0.1/pasarguard"'
    else:
        url = f'SQLALCHEMY_DATABASE_URL = "postgresql+asyncpg://postgres:{pwd}@localhost:5432/pasarguard"'
    if re.search(r"SQLALCHEMY_DATABASE_URL", text, re.I):
        return re.sub(
            r'#\s*SQLALCHEMY_DATABASE_URL\s*=\s*"[^"]*"|SQLALCHEMY_DATABASE_URL\s*=\s*"[^"]*"',
            url,
            text,
            count=1,
        )
    return text.rstrip() + f"\n{url}\n"


def transform_compose_marzban_to_pasarguard(text: str) -> str:
    """Update docker-compose.yml per official migration table."""
    replacements = [
        ("gozargah/marzban", "pasarguard/panel"),
        ("image: marzban", "image: pasarguard"),
        ("/var/lib/marzban", "/var/lib/pasarguard"),
        ("/var/lib/mysql/marzban", "/var/lib/mysql/pasarguard"),
        ("MYSQL_DATABASE: marzban", "MYSQL_DATABASE: pasarguard"),
        ("MYSQL_DATABASE=marzban", "MYSQL_DATABASE=pasarguard"),
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    text = re.sub(r"(?m)^(\s*)marzban(\s*:)", r"\1pasarguard\2", text)
    text = re.sub(r"container_name:\s*marzban", "container_name: pasarguard", text, flags=re.I)
    return text


def transform_xray_config(text: str) -> str:
    return text.replace("/var/lib/marzban", "/var/lib/pasarguard").replace("/opt/marzban", "/opt/pasarguard")


def fix_mysql_dump_for_pasarguard(sql_text: str) -> str:
    """Official sed: CREATE DATABASE and USE lines only, then safe rename."""
    sql_text = re.sub(r"(?m)^(CREATE DATABASE.*)\bmarzban\b", r"\1pasarguard", sql_text, flags=re.I)
    sql_text = re.sub(r"(?m)^(USE )\bmarzban\b", r"\1pasarguard", sql_text, flags=re.I)
    return sql_text.replace("marzban", "pasarguard")
