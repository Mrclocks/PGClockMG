"""Database connection details supplied by the user in the wizard."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from app.config import PASARGUARD_DATA, PASARGUARD_ENV

Role = Literal["source", "target"]

_DEFAULT_PORTS = {
    "mysql": "3306",
    "mariadb": "3306",
    "postgresql": "5432",
    "timescaledb": "5432",
}


def db_needs_credentials(db: str | None) -> bool:
    return db in ("mysql", "mariadb", "postgresql", "timescaledb")


def _field(params: dict, role: Role, name: str) -> str | None:
    val = params.get(f"{role}_db_{name}")
    if val is None:
        return None
    text = str(val).strip()
    return text or None


def connection_from_params(params: dict, role: Role) -> dict:
    """Build connection dict from wizard fields only (no .env auto-read)."""
    db_type = params[f"{role}_db"]
    conn: dict = {
        "db_type": db_type,
        "user": _field(params, role, "user"),
        "password": _field(params, role, "password"),
        "database": _field(params, role, "name"),
        "host": _field(params, role, "host") or "127.0.0.1",
        "port": _field(params, role, "port") or _DEFAULT_PORTS.get(db_type),
        "sqlite_path": str(PASARGUARD_DATA / "db.sqlite3"),
    }
    if db_type == "sqlite":
        conn["database"] = Path(conn["sqlite_path"]).name
    return conn


def get_source_connection(params: dict) -> dict:
    return connection_from_params(params, "source")


def get_target_connection(params: dict) -> dict:
    """Target identity from installed PasarGuard .env; password from wizard."""
    wizard = connection_from_params(params, "target")
    target_db = params.get("target_db") or wizard.get("db_type")
    if not target_db or not PASARGUARD_ENV.exists():
        return wizard

    from app.services.env_migration import get_pasarguard_target_connection

    env_conn = get_pasarguard_target_connection(
        target_db,
        password_override=wizard.get("password"),
    )
    return {
        "db_type": target_db,
        "user": env_conn.get("user") or wizard.get("user"),
        "password": wizard.get("password") or env_conn.get("password"),
        "database": env_conn.get("database") or wizard.get("database"),
        "host": env_conn.get("host") or wizard.get("host") or "127.0.0.1",
        "port": env_conn.get("port") or wizard.get("port") or _DEFAULT_PORTS.get(target_db),
        "sqlite_path": env_conn.get("sqlite_path") or wizard.get("sqlite_path"),
    }


def migration_port(conn: dict, db_type: str) -> str:
    port = conn.get("port") or _DEFAULT_PORTS.get(db_type, "5432")
    if db_type in ("postgresql", "timescaledb") and port == "6432":
        return "5432"
    return port


def build_migration_url(params: dict) -> str:
    """URL for db-migrations tool using user-provided target credentials."""
    target_db = params["target_db"]
    conn = get_target_connection(params)
    pwd = conn.get("password") or ""
    if target_db == "sqlite":
        path = conn.get("sqlite_path") or str(PASARGUARD_DATA / "db.sqlite3")
        return f"sqlite:///{path}"
    if target_db in ("mysql", "mariadb"):
        user = conn.get("user") or "root"
        host = conn.get("host") or "127.0.0.1"
        port = migration_port(conn, target_db)
        db = conn.get("database") or "pasarguard"
        return f"mysql+pymysql://{user}:{pwd}@{host}:{port}/{db}"
    user = conn.get("user") or "postgres"
    host = conn.get("host") or "127.0.0.1"
    port = migration_port(conn, target_db)
    db = conn.get("database") or "pasarguard"
    return f"postgresql+asyncpg://{user}:{pwd}@{host}:{port}/{db}"


def build_app_sqlalchemy_url(params: dict) -> str:
    """SQLALCHEMY_DATABASE_URL for PasarGuard .env from user-provided target credentials."""
    target_db = params["target_db"]
    conn = get_target_connection(params)
    pwd = conn.get("password") or ""
    if target_db == "sqlite":
        path = conn.get("sqlite_path") or str(PASARGUARD_DATA / "db.sqlite3")
        return f"sqlite+aiosqlite:///{path}"
    if target_db in ("mysql", "mariadb"):
        user = conn.get("user") or "root"
        host = conn.get("host") or "127.0.0.1"
        port = conn.get("port") or "3306"
        db = conn.get("database") or "pasarguard"
        return f"mysql+asyncmy://{user}:{pwd}@{host}:{port}/{db}"
    user = conn.get("user") or "postgres"
    host = conn.get("host") or "127.0.0.1"
    port = conn.get("port") or "5432"
    db = conn.get("database") or "pasarguard"
    return f"postgresql+asyncpg://{user}:{pwd}@{host}:{port}/{db}"


def validate_db_credentials(params: dict, role: Role) -> list[dict]:
    """Return localized error dicts if required DB fields are missing."""
    db_type = params.get(f"{role}_db")
    if not db_needs_credentials(db_type):
        return []

    if role == "source":
        source_db = params.get("source_db")
        target_db = params.get("target_db")
        if source_db == target_db:
            return []
        if source_db == "sqlite":
            return []

    conn = connection_from_params(params, role)
    label = "Source" if role == "source" else "Target"
    label_fa = "مبدأ" if role == "source" else "مقصد"
    label_ru = "источника" if role == "source" else "цели"
    errors: list[dict] = []

    if not conn.get("user"):
        errors.append({
            "en": f"{label} database username required",
            "fa": f"نام کاربری دیتابیس {label_fa} لازم است",
            "ru": f"Укажите пользователя БД {label_ru}",
        })
    if not conn.get("database"):
        errors.append({
            "en": f"{label} database name required",
            "fa": f"نام دیتابیس {label_fa} لازم است",
            "ru": f"Укажите имя БД {label_ru}",
        })
    if not conn.get("password"):
        errors.append({
            "en": f"{label} database password required",
            "fa": f"رمز دیتابیس {label_fa} لازم است",
            "ru": f"Укажите пароль БД {label_ru}",
        })
    return errors
