"""Marzban → PasarGuard .env transformation (official docs)."""

import re
from pathlib import Path
from urllib.parse import unquote

from app.config import PASARGUARD_DATA, PASARGUARD_ENV

PATH_REPLACEMENTS = [
    ("/opt/marzban", "/opt/pasarguard"),
    ("/var/lib/marzban", "/var/lib/pasarguard"),
    ("/var/lib/mysql/marzban", "/var/lib/mysql/pasarguard"),
]


def read_env_var(text: str, key: str) -> str | None:
    pattern = rf'^\s*{re.escape(key)}\s*=\s*(.+?)\s*$'
    m = re.search(pattern, text, re.MULTILINE | re.IGNORECASE)
    if not m:
        return None
    raw = m.group(1).strip()
    if not raw or raw.startswith("#"):
        return None
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        return raw[1:-1]
    if "#" in raw:
        raw = raw.split("#", 1)[0].strip()
    return raw.strip().strip('"').strip("'") or None


def mask_password(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 2:
        return "•" * len(value)
    return value[0] + ("•" * (len(value) - 2)) + value[-1]


def migration_primary_key(candidates: list[dict], db_type: str | None) -> str | None:
    if not candidates:
        return None
    if db_type in ("mysql", "mariadb"):
        order = ["MYSQL_ROOT_PASSWORD", "MYSQL_PASSWORD", "DB_PASSWORD"]
    elif db_type in ("postgresql", "timescaledb"):
        order = ["POSTGRES_PASSWORD", "DB_PASSWORD"]
    else:
        order = ["DB_PASSWORD", "MYSQL_ROOT_PASSWORD", "MYSQL_PASSWORD", "POSTGRES_PASSWORD"]
    keys = {c["key"] for c in candidates}
    for key in order:
        if key in keys:
            return key
    return candidates[0]["key"]


def extract_env_password_candidates(text: str, db_type: str | None = None) -> list[dict]:
    """Return distinct password keys/values found in a panel .env file."""
    if not text:
        return []

    keys: list[str] = []
    if db_type in ("mysql", "mariadb"):
        keys = ["MYSQL_ROOT_PASSWORD", "MYSQL_PASSWORD", "DB_PASSWORD"]
    elif db_type in ("postgresql", "timescaledb"):
        keys = ["POSTGRES_PASSWORD", "DB_PASSWORD"]
    else:
        keys = ["DB_PASSWORD", "MYSQL_ROOT_PASSWORD", "MYSQL_PASSWORD", "POSTGRES_PASSWORD"]

    seen_vals: set[str] = set()
    candidates: list[dict] = []
    for key in keys:
        val = read_env_var(text, key)
        if not val:
            continue
        dup = val in seen_vals
        seen_vals.add(val)
        candidates.append({
            "key": key,
            "value": val,
            "masked": mask_password(val),
            "quoted_preview": f'"{mask_password(val)}"',
            "duplicate_value": dup,
        })

    primary = migration_primary_key(candidates, db_type)
    for c in candidates:
        c["used_for_migration"] = c["key"] == primary
    return candidates


def pick_primary_env_password(candidates: list[dict], db_type: str | None) -> str | None:
    if not candidates:
        return None
    order: list[str]
    if db_type in ("mysql", "mariadb"):
        order = ["MYSQL_ROOT_PASSWORD", "MYSQL_PASSWORD", "DB_PASSWORD"]
    elif db_type in ("postgresql", "timescaledb"):
        order = ["POSTGRES_PASSWORD", "DB_PASSWORD"]
    else:
        order = ["DB_PASSWORD", "MYSQL_ROOT_PASSWORD", "MYSQL_PASSWORD", "POSTGRES_PASSWORD"]
    by_key = {c["key"]: c["value"] for c in candidates}
    for key in order:
        if key in by_key:
            return by_key[key]
    return candidates[0]["value"]


def read_compose_db_credentials(text: str) -> dict:
    """Read PasarGuard installer DB_* block used by docker-compose database stack."""
    creds: dict = {}
    for env_key, field in (
        ("DB_USER", "user"),
        ("DB_PASSWORD", "password"),
        ("DB_NAME", "database"),
        ("DB_HOST", "host"),
        ("DB_PORT", "port"),
    ):
        val = read_env_var(text, env_key)
        if val:
            creds[field] = val
    return creds


def _compose_db_service() -> str | None:
    from app.config import PASARGUARD_DIR

    compose = PASARGUARD_DIR / "docker-compose.yml"
    if not compose.exists():
        return None
    body = compose.read_text(encoding="utf-8", errors="ignore")
    for svc in ("timescaledb", "postgresql", "mariadb", "mysql"):
        if re.search(rf"^\s*{re.escape(svc)}\s*:", body, re.MULTILINE):
            return svc
    return None


def _compose_has_pgbouncer() -> bool:
    from app.config import PASARGUARD_DIR

    compose = PASARGUARD_DIR / "docker-compose.yml"
    if not compose.exists():
        return False
    body = compose.read_text(encoding="utf-8", errors="ignore")
    return bool(re.search(r"^\s*pgbouncer\s*:", body, re.MULTILINE))


def detect_db_type_from_env(text: str) -> str | None:
    """Detect database engine from .env content."""
    if read_env_var(text, "PGADMIN_EMAIL") or read_env_var(text, "PGADMIN_PASSWORD"):
        svc = _compose_db_service()
        return "timescaledb" if svc == "timescaledb" else "postgresql"

    db_creds = read_compose_db_credentials(text)
    if db_creds.get("database") and db_creds.get("user"):
        svc = _compose_db_service()
        if svc in ("timescaledb", "postgresql"):
            return svc
        if svc in ("mysql", "mariadb"):
            return svc
        if read_env_var(text, "MYSQL_ROOT_PASSWORD"):
            return "mariadb" if "mariadb" in text.lower() else "mysql"
        if "postgres" in text.lower() or "pgadmin" in text.lower():
            return "timescaledb" if "timescale" in text.lower() else "postgresql"
        if "mysql" in text.lower():
            return "mysql"

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


def parse_sqlalchemy_url(url: str, env_text: str | None = None) -> dict:
    """Parse SQLALCHEMY_DATABASE_URL into user, password, host, port, database."""
    result: dict = {
        "user": None,
        "password": None,
        "host": None,
        "port": None,
        "database": None,
        "sqlite_path": None,
    }
    if not url:
        return result

    low = url.lower()
    if "sqlite" in low:
        if "///" in url:
            result["sqlite_path"] = url.split("///", 1)[1].split("?")[0]
        else:
            result["sqlite_path"] = url.split("//", 1)[-1].split("?")[0]
        result["database"] = Path(result["sqlite_path"]).stem or "pasarguard"
        return result

    m = re.match(
        r"^(?:[\w+]+://)"
        r"(?:([^:@/]*)(?::([^@/]*))?@)?"
        r"([^:/]+)"
        r"(?::(\d+))?"
        r"/([^?]*)",
        url,
    )
    if m:
        user = m.group(1) or ""
        result["user"] = unquote(user) if user else None
        result["password"] = unquote(m.group(2)) if m.group(2) is not None else None
        result["host"] = m.group(3)
        result["port"] = m.group(4)
        result["database"] = m.group(5) or None

    if env_text:
        compose_db = read_compose_db_credentials(env_text)
        if not result["user"]:
            result["user"] = (
                compose_db.get("user")
                or read_env_var(env_text, "MYSQL_USER")
                or read_env_var(env_text, "POSTGRES_USER")
            )
        if not result["password"]:
            result["password"] = (
                compose_db.get("password")
                or read_env_var(env_text, "MYSQL_ROOT_PASSWORD")
                or read_env_var(env_text, "MYSQL_PASSWORD")
                or read_env_var(env_text, "POSTGRES_PASSWORD")
                or read_env_var(env_text, "DB_PASSWORD")
            )
        if not result["database"]:
            result["database"] = (
                compose_db.get("database")
                or read_env_var(env_text, "MYSQL_DATABASE")
                or read_env_var(env_text, "POSTGRES_DB")
            )
        if not result["port"]:
            result["port"] = (
                compose_db.get("port")
                or read_env_var(env_text, "MYSQL_PORT")
                or read_env_var(env_text, "POSTGRES_PORT")
            )
        if not result["host"]:
            result["host"] = (
                compose_db.get("host")
                or read_env_var(env_text, "MYSQL_HOST")
                or read_env_var(env_text, "POSTGRES_HOST")
            )

    return result


_DEFAULTS = {
    "sqlite": {"user": None, "password": None, "host": "127.0.0.1", "port": None, "database": "pasarguard"},
    "mysql": {"user": "root", "password": "password", "host": "127.0.0.1", "port": "3306", "database": "pasarguard"},
    "mariadb": {"user": "root", "password": "password", "host": "127.0.0.1", "port": "3306", "database": "pasarguard"},
    "postgresql": {"user": "postgres", "password": "password", "host": "127.0.0.1", "port": "5432", "database": "pasarguard"},
    "timescaledb": {"user": "postgres", "password": "password", "host": "127.0.0.1", "port": "5432", "database": "pasarguard"},
}


def get_pasarguard_target_connection(
    target_db: str,
    password_override: str | None = None,
    env_text: str | None = None,
) -> dict:
    """Read target DB user/name/host/port/password from installed PasarGuard .env."""
    defaults = _DEFAULTS.get(target_db, _DEFAULTS["postgresql"]).copy()
    defaults["sqlite_path"] = str(PASARGUARD_DATA / "db.sqlite3")
    defaults["db_type"] = target_db

    text = env_text
    if text is None and PASARGUARD_ENV.exists():
        text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")

    if not text:
        if password_override:
            defaults["password"] = password_override
        return defaults

    url = read_env_var(text, "SQLALCHEMY_DATABASE_URL") or ""
    parsed = parse_sqlalchemy_url(url, text)
    compose_db = read_compose_db_credentials(text)
    conn = {
        "user": None,
        "password": None,
        "host": None,
        "port": None,
        "database": None,
        "sqlite_path": parsed.get("sqlite_path") or defaults["sqlite_path"],
        "db_type": target_db,
    }

    if target_db in ("mysql", "mariadb"):
        conn["user"] = (
            compose_db.get("user")
            or parsed.get("user")
            or read_env_var(text, "MYSQL_USER")
            or defaults["user"]
        )
        conn["password"] = (
            password_override
            or compose_db.get("password")
            or parsed.get("password")
            or read_env_var(text, "MYSQL_ROOT_PASSWORD")
            or read_env_var(text, "MYSQL_PASSWORD")
            or defaults["password"]
        )
        conn["database"] = (
            compose_db.get("database")
            or parsed.get("database")
            or read_env_var(text, "MYSQL_DATABASE")
            or defaults["database"]
        )
        conn["host"] = (
            compose_db.get("host")
            or parsed.get("host")
            or read_env_var(text, "MYSQL_HOST")
            or defaults["host"]
        )
        conn["port"] = (
            compose_db.get("port")
            or parsed.get("port")
            or read_env_var(text, "MYSQL_PORT")
            or defaults["port"]
        )
    elif target_db in ("postgresql", "timescaledb"):
        conn["user"] = (
            compose_db.get("user")
            or parsed.get("user")
            or read_env_var(text, "POSTGRES_USER")
            or defaults["user"]
        )
        conn["password"] = (
            password_override
            or compose_db.get("password")
            or parsed.get("password")
            or read_env_var(text, "POSTGRES_PASSWORD")
            or defaults["password"]
        )
        conn["database"] = (
            compose_db.get("database")
            or parsed.get("database")
            or read_env_var(text, "POSTGRES_DB")
            or defaults["database"]
        )
        conn["host"] = (
            compose_db.get("host")
            or parsed.get("host")
            or read_env_var(text, "POSTGRES_HOST")
            or defaults["host"]
        )
        conn["port"] = (
            compose_db.get("port")
            or parsed.get("port")
            or read_env_var(text, "POSTGRES_PORT")
            or defaults["port"]
        )
    elif target_db == "sqlite":
        conn["sqlite_path"] = parsed.get("sqlite_path") or defaults["sqlite_path"]
        conn["database"] = Path(conn["sqlite_path"]).name

    conn["db_type"] = target_db
    return conn


def _direct_db_port(target_db: str, conn: dict) -> str:
    """db-migrations and docker exec need direct DB port, not PgBouncer (6432)."""
    port = conn.get("port") or ("3306" if target_db in ("mysql", "mariadb") else "5432")
    if target_db in ("postgresql", "timescaledb") and port == "6432":
        return "5432"
    return port


def _app_db_port(target_db: str, conn: dict) -> str:
    """SQLALCHEMY URL for PasarGuard app — uses PgBouncer when installed."""
    port = conn.get("port") or ("3306" if target_db in ("mysql", "mariadb") else "5432")
    if target_db in ("postgresql", "timescaledb") and _compose_has_pgbouncer() and port in (None, "5432"):
        return "6432"
    return port


def build_db_migration_target_url(
    target_db: str,
    password: str | None = None,
    env_text: str | None = None,
) -> str:
    """Build connection URL for official db-migrations tool (pymysql/asyncpg)."""
    conn = get_pasarguard_target_connection(target_db, password, env_text)
    pwd = conn.get("password") or "password"
    if target_db == "sqlite":
        path = conn.get("sqlite_path") or str(PASARGUARD_DATA / "db.sqlite3")
        return f"sqlite:///{path}"
    if target_db in ("mysql", "mariadb"):
        user = conn.get("user") or "root"
        host = conn.get("host") or "127.0.0.1"
        port = _direct_db_port(target_db, conn)
        db = conn.get("database") or "pasarguard"
        return f"mysql+pymysql://{user}:{pwd}@{host}:{port}/{db}"
    user = conn.get("user") or "postgres"
    host = conn.get("host") or "127.0.0.1"
    port = _direct_db_port(target_db, conn)
    db = conn.get("database") or "pasarguard"
    return f"postgresql+asyncpg://{user}:{pwd}@{host}:{port}/{db}"


def build_sqlalchemy_url_for_target(target_db: str, password_override: str | None = None) -> str:
    """Build SQLALCHEMY_DATABASE_URL line for PasarGuard .env (async drivers)."""
    conn = get_pasarguard_target_connection(target_db, password_override)
    pwd = conn.get("password") or "password"
    if target_db == "sqlite":
        path = conn.get("sqlite_path") or "/var/lib/pasarguard/db.sqlite3"
        return f"sqlite+aiosqlite:///{path}"
    if target_db in ("mysql", "mariadb"):
        user = conn.get("user") or "root"
        host = conn.get("host") or "127.0.0.1"
        port = _app_db_port(target_db, conn)
        db = conn.get("database") or "pasarguard"
        return f"mysql+asyncmy://{user}:{pwd}@{host}:{port}/{db}"
    user = conn.get("user") or "postgres"
    host = conn.get("host") or "127.0.0.1"
    port = _app_db_port(target_db, conn)
    db = conn.get("database") or "pasarguard"
    return f"postgresql+asyncpg://{user}:{pwd}@{host}:{port}/{db}"


def extract_env_summary(text: str) -> dict:
    """Extract DB credentials and panel port from a panel .env file."""
    db_type = detect_db_type_from_env(text)
    url = read_env_var(text, "SQLALCHEMY_DATABASE_URL") or ""
    parsed = parse_sqlalchemy_url(url, text)
    compose_db = read_compose_db_credentials(text)
    mysql_password = read_env_var(text, "MYSQL_ROOT_PASSWORD") or read_env_var(text, "MYSQL_PASSWORD")
    postgres_password = read_env_var(text, "POSTGRES_PASSWORD")
    db_password = parsed.get("password") or compose_db.get("password")
    db_user = parsed.get("user") or compose_db.get("user")
    db_name = parsed.get("database") or compose_db.get("database")
    db_host = parsed.get("host") or compose_db.get("host")
    db_port = parsed.get("port") or compose_db.get("port")
    if db_type in ("mysql", "mariadb"):
        db_user = db_user or read_env_var(text, "MYSQL_USER") or "root"
        db_password = db_password or mysql_password
        db_name = db_name or read_env_var(text, "MYSQL_DATABASE") or "pasarguard"
        db_host = db_host or read_env_var(text, "MYSQL_HOST") or "127.0.0.1"
        db_port = db_port or read_env_var(text, "MYSQL_PORT") or "3306"
    elif db_type in ("postgresql", "timescaledb"):
        db_user = db_user or read_env_var(text, "POSTGRES_USER") or "postgres"
        db_password = db_password or postgres_password
        db_name = db_name or read_env_var(text, "POSTGRES_DB") or "pasarguard"
        db_host = db_host or read_env_var(text, "POSTGRES_HOST") or "127.0.0.1"
        db_port = db_port or read_env_var(text, "POSTGRES_PORT") or ("6432" if _compose_has_pgbouncer() else "5432")
    elif db_type == "sqlite":
        db_name = Path(parsed.get("sqlite_path") or "db.sqlite3").name
    panel_port = read_env_var(text, "UVICORN_PORT") or "8000"
    panel_host = read_env_var(text, "UVICORN_HOST") or "0.0.0.0"
    return {
        "db_type": db_type,
        "db_user": db_user,
        "db_name": db_name,
        "db_host": db_host,
        "db_port": db_port,
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
    Preserves user/database/host from installed PasarGuard .env when available.
    """
    for old, new in PATH_REPLACEMENTS:
        text = text.replace(old, new)

    text = re.sub(r"V2RAY_SUBSCRIPTION_TEMPLATE", "XRAY_SUBSCRIPTION_TEMPLATE", text, flags=re.I)
    text = text.replace("v2ray/", "xray/")
    text = text.replace("V2ray/", "xray/")

    text = re.sub(r"(?m)^(\s*MYSQL_DATABASE\s*=\s*)marzban\s*$", r"\1pasarguard", text, flags=re.I)

    pg_env = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore") if PASARGUARD_ENV.exists() else None
    sqlalchemy_url = build_sqlalchemy_url_for_target(target_db, password_override)
    db_url = f'SQLALCHEMY_DATABASE_URL = "{sqlalchemy_url}"'

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
    url_line = f'SQLALCHEMY_DATABASE_URL = "{build_sqlalchemy_url_for_target(target_db, password)}"'
    if re.search(r"SQLALCHEMY_DATABASE_URL", text, re.I):
        return re.sub(
            r'#\s*SQLALCHEMY_DATABASE_URL\s*=\s*"[^"]*"|SQLALCHEMY_DATABASE_URL\s*=\s*"[^"]*"',
            url_line,
            text,
            count=1,
        )
    return text.rstrip() + f"\n{url_line}\n"


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
    "SUBSCRIPTION_PATH",
    "SUBSCRIPTION_PAGE_TEMPLATE",
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

