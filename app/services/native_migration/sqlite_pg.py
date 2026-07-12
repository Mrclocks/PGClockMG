"""Copy Marzban-compatible SQLite tables into PasarGuard PostgreSQL."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Callable

# Order respects foreign keys (Marzban / shared PasarGuard tables).
TABLE_ORDER = [
    "jwt",
    "system",
    "admins",
    "nodes",
    "inbounds",
    "hosts",
    "user_templates",
    "users",
    "next_plans",
    "notification_reminders",
    "node_user_usages",
    "node_usages",
]

SKIP_TABLES = {
    "alembic_version",
    "admin_usage_logs",
    "user_usage_logs",
    "node_stats",
    "proxies",
    "tls",
    "exclude_inbounds_association",
    "template_inbounds_association",
}

ENUM_DEFAULTS = {
    ("hosts", "fingerprint"): "none",
    ("hosts", "security"): "inbound_default",
    ("hosts", "alpn"): "none",
}


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]


def _pg_columns(cur, table: str) -> list[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


def _convert_value(table: str, column: str, value):
    if value is None:
        key = (table, column)
        if key in ENUM_DEFAULTS:
            return ENUM_DEFAULTS[key]
        return None
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except Exception:
            return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                json.loads(stripped)
                return stripped
            except Exception:
                pass
    return value


def _reset_pg_sequences(cur, table: str, pk: str = "id") -> None:
    cur.execute(
        f"SELECT setval(pg_get_serial_sequence('{table}', '{pk}'), "
        f"COALESCE((SELECT MAX({pk}) FROM {table}), 1), true)"
    )


def copy_sqlite_to_postgres(
    sqlite_path: str,
    pg_dsn: dict,
    log: Callable[[str], None],
    source_version: str | None = None,
) -> dict:
    """
    Copy shared tables from Marzban SQLite into PasarGuard PostgreSQL.
    Returns stats dict {table: row_count}.
    """
    import psycopg2
    from psycopg2 import sql

    stats: dict[str, int] = {}
    conn_sqlite = sqlite3.connect(sqlite_path)
    conn_sqlite.row_factory = sqlite3.Row

    try:
        pg_conn = psycopg2.connect(
            host=pg_dsn.get("host") or "127.0.0.1",
            port=int(pg_dsn.get("port") or 5432),
            dbname=pg_dsn.get("database") or "pasarguard",
            user=pg_dsn.get("user") or "postgres",
            password=pg_dsn.get("password") or "",
        )
        pg_conn.autocommit = False
        cur = pg_conn.cursor()

        sqlite_tables = {
            r[0]
            for r in conn_sqlite.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        for table in TABLE_ORDER:
            if table in SKIP_TABLES or table not in sqlite_tables:
                continue

            src_cols = _sqlite_columns(conn_sqlite, table)
            if not src_cols:
                continue
            tgt_cols = _pg_columns(cur, table)
            if not tgt_cols:
                log(f"Skip {table}: not in target schema")
                continue

            common = [c for c in src_cols if c in tgt_cols]
            if not common:
                log(f"Skip {table}: no matching columns")
                continue

            col_list = sql.SQL(", ").join(sql.Identifier(c) for c in common)
            placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in common)
            select_sql = sql.SQL("SELECT {} FROM {}").format(
                col_list, sql.Identifier(table)
            )

            cur.execute(
                sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(
                    sql.Identifier(table)
                )
            )

            rows = conn_sqlite.execute(
                f"SELECT {', '.join(common)} FROM {table}"
            ).fetchall()
            insert_sql = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                sql.Identifier(table), col_list, placeholders
            )

            count = 0
            for row in rows:
                values = tuple(
                    _convert_value(table, col, row[col]) for col in common
                )
                try:
                    cur.execute(insert_sql, values)
                    count += 1
                except Exception as exc:
                    log(f"Row skip {table}: {str(exc)[:120]}")
            stats[table] = count
            log(f"Imported {table}: {count} rows ({len(common)} columns)")

            if "id" in tgt_cols:
                try:
                    _reset_pg_sequences(cur, table, "id")
                except Exception:
                    pass

        if source_version:
            cur.execute("DELETE FROM alembic_version")
            cur.execute(
                "INSERT INTO alembic_version (version_num) VALUES (%s)",
                (source_version,),
            )
            log(f"alembic_version set to {source_version}")

        pg_conn.commit()
        return stats
    finally:
        conn_sqlite.close()
        try:
            pg_conn.close()
        except Exception:
            pass
