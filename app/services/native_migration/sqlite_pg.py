"""Copy Marzban-compatible SQLite tables into PasarGuard PostgreSQL."""

from __future__ import annotations

import sqlite3
from typing import Callable

from app.services.native_migration.copy_core import copy_sqlite_tables


def copy_sqlite_to_postgres(
    sqlite_path: str,
    pg_dsn: dict,
    log: Callable[[str], None],
    source_version: str | None = None,
) -> dict:
    import psycopg2
    from psycopg2 import sql

    conn_sqlite = sqlite3.connect(sqlite_path)
    conn_sqlite.row_factory = sqlite3.Row
    pg_conn = psycopg2.connect(
        host=pg_dsn.get("host") or "127.0.0.1",
        port=int(pg_dsn.get("port") or 5432),
        dbname=pg_dsn.get("database") or "pasarguard",
        user=pg_dsn.get("user") or "postgres",
        password=pg_dsn.get("password") or "",
    )
    pg_conn.autocommit = False
    cur = pg_conn.cursor()

    try:
        def target_columns(table: str) -> list[str]:
            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                ORDER BY ordinal_position
                """,
                (table,),
            )
            return [r[0] for r in cur.fetchall()]

        def truncate(table: str) -> None:
            cur.execute(
                sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(
                    sql.Identifier(table)
                )
            )

        def insert(table: str, columns: list[str], values: tuple) -> None:
            col_list = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
            placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in columns)
            q = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                sql.Identifier(table), col_list, placeholders
            )
            cur.execute(q, values)

        def reset_seq(table: str) -> None:
            cur.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table}), 1), true)"
            )

        stats = copy_sqlite_tables(
            conn_sqlite, log,
            target_columns_fn=target_columns,
            truncate_fn=truncate,
            insert_fn=insert,
            reset_sequence_fn=reset_seq,
        )

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
        pg_conn.close()
