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


def detect_db_type_from_env(text: str) -> str | None:
    """Detect database engine from .env content."""
    url = read_env_var(text, "SQLALCHEMY_DATABASE_URL") or ""
    low = (url + text).lower()
    if "sqlite" in low:
        return "sqlite"
    if "mariadb" in low:
        return "mariadb"
    if "mysql" in low or "pymysql" in low or "asyncmy" in low:
        return "mysql"
    if "postgres" in low or "asyncpg" in low or "timescale" in low:
        return "timescaledb" if "timescale" in low else "postgresql"
    return None


def _parse_db_user_from_url(url: str) -> str | None:
    m = re.search(r"://([^:@/]+)", url or "")
    return m.group(1) if m else None


def extract_env_summary(text: str) -> dict:
    """Extract DB credentials and panel port from a panel .env file."""
    db_type = detect_db_type_from_env(text)
    url = read_env_var(text, "SQLALCHEMY_DATABASE_URL") or ""
    mysql_password = read_env_var(text, "MYSQL_ROOT_PASSWORD") or read_env_var(text, "MYSQL_PASSWORD")
    postgres_password = read_env_var(text, "POSTGRES_PASSWORD")
    db_password = None
    db_user = None
    if db_type in ("mysql", "mariadb"):
        db_user = _parse_db_user_from_url(url) or "root"
        db_password = mysql_password
    elif db_type in ("postgresql", "timescaledb"):
        db_user = _parse_db_user_from_url(url) or "postgres"
        db_password = postgres_password
    panel_port = read_env_var(text, "UVICORN_PORT") or "8000"
    panel_host = read_env_var(text, "UVICORN_HOST") or "0.0.0.0"
    return {
        "db_type": db_type,
        "db_user": db_user,
        "db_password": db_password,
        "mysql_password": mysql_password,
        "postgres_password": postgres_password,
        "panel_port": panel_port,
        "panel_host": panel_host,
        "has_password": bool(db_password),
    }


def get_panel_url_from_env(env_text: str | None = None, ip: str | None = None) -> str:
    """Build PasarGuard dashboard URL using UVICORN_PORT from .env."""
    import socket

    port = "8000"
    if env_text:
        port = read_env_var(env_text, "UVICORN_PORT") or "8000"
    if not ip:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            ip = "127.0.0.1"
    return f"https://{ip}:{port}/dashboard/"


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


MIGRATE_ENV_KEYS = {
    "XRAY_SUBSCRIPTION_TEMPLATE",
    "V2RAY_SUBSCRIPTION_TEMPLATE",
    "SUBSCRIPTION_URL_PREFIX",
    "UVICORN_SSL_CERTFILE",
    "UVICORN_SSL_KEYFILE",
    "UVICORN_SSL_CA_TYPE",
    "UVICORN_HOST",
    "UVICORN_PORT",
    "XRAY_EXECUTABLE_PATH",
    "XRAY_JSON",
    "SUDO_USERNAME",
    "JWT_ACCESS_TOKEN_EXPIRE_MINUTES",
    "DOCS",
    "DEBUG",
    "WEBHOOK_ADDRESS",
    "TELEGRAM_API_TOKEN",
    "TELEGRAM_ADMIN_ID",
    "DISCORD_WEBHOOK_URL",
}


def _parse_env_lines(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        out[key.strip()] = val.strip()
    return out


def merge_marzban_env_into_pasarguard(
    pg_env: str,
    marzban_env: str,
    target_db: str,
    password_override: str | None = None,
) -> str:
    """Apply Marzban settings onto existing PasarGuard .env (fresh migration from backup)."""
    transformed = transform_marzban_env(marzban_env, target_db, password_override)
    pg_keys = _parse_env_lines(pg_env)
    mz_keys = _parse_env_lines(transformed)

    for key, val in mz_keys.items():
        if key in MIGRATE_ENV_KEYS or key.startswith(("UVICORN_SSL_", "TELEGRAM_", "WEBHOOK_")):
            pg_keys[key] = val

    if "SQLALCHEMY_DATABASE_URL" in mz_keys:
        pg_keys["SQLALCHEMY_DATABASE_URL"] = mz_keys["SQLALCHEMY_DATABASE_URL"]

    lines = pg_env.splitlines()
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in pg_keys:
                result.append(f"{key} = {pg_keys[key]}")
                seen.add(key)
                continue
        result.append(line)

    for key, val in pg_keys.items():
        if key not in seen:
            result.append(f"{key} = {val}")

    return "\n".join(result) + "\n"

