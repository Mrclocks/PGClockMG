"""Read alembic_version from migration sources."""

from __future__ import annotations

import re
from pathlib import Path

from app.services.pasarguard_ops import read_sqlite_alembic_version


def read_alembic_version_from_sql_dump(sql_text: str) -> str | None:
    patterns = (
        r"INSERT\s+INTO\s+[`\"]?alembic_version[`\"]?\s+.*?VALUES\s*\(\s*['\"]?([0-9a-f]{12,})",
        r"INSERT\s+INTO\s+[`\"]?alembic_version[`\"]?\s*\([^)]*\)\s*VALUES\s*\(\s*['\"]?([0-9a-f]{12,})",
    )
    for pattern in patterns:
        m = re.search(pattern, sql_text, re.I | re.S)
        if m:
            return m.group(1)
    return None


async def resolve_source_alembic_version(
    migrator, source_db: str, source_path: str,
) -> str | None:
    path = Path(source_path) if source_path else None

    if source_db == "sqlite":
        if not path or not path.exists():
            return None
        return read_sqlite_alembic_version(path)

    if path and path.exists() and path.suffix.lower() == ".sql":
        text = path.read_text(encoding="utf-8", errors="ignore")
        return read_alembic_version_from_sql_dump(text)

    if source_db in ("postgresql", "timescaledb", "mysql", "mariadb"):
        from app.services.db_credentials import get_source_connection
        return _read_live_alembic_version(get_source_connection(migrator.params), source_db)

    return None


def _read_live_alembic_version(conn: dict, db_type: str) -> str | None:
    """Read alembic_version from a live source DB using wizard credentials."""
    from app.services.db_credentials import migration_port

    host = conn.get("host") or "127.0.0.1"
    port = int(migration_port(conn, db_type))
    user = conn.get("user") or (
        "postgres" if db_type in ("postgresql", "timescaledb") else "root"
    )
    password = conn.get("password") or ""
    database = conn.get("database") or "pasarguard"

    try:
        if db_type in ("postgresql", "timescaledb"):
            import psycopg2

            with psycopg2.connect(
                host=host, port=port, dbname=database, user=user, password=password,
            ) as pg:
                with pg.cursor() as cur:
                    cur.execute("SELECT version_num FROM alembic_version LIMIT 1")
                    row = cur.fetchone()
                    return str(row[0]).strip() if row and row[0] else None

        import pymysql

        with pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
            charset="utf8mb4",
        ) as mysql:
            with mysql.cursor() as cur:
                cur.execute("SELECT version_num FROM alembic_version LIMIT 1")
                row = cur.fetchone()
                return str(row[0]).strip() if row and row[0] else None
    except Exception:
        return None
