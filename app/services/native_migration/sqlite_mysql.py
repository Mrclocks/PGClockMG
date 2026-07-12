"""Copy Marzban-compatible SQLite tables into PasarGuard MySQL/MariaDB."""

from __future__ import annotations

import sqlite3
from typing import Callable

from app.services.native_migration.copy_core import copy_sqlite_tables


def copy_sqlite_to_mysql(
    sqlite_path: str,
    mysql_dsn: dict,
    log: Callable[[str], None],
    source_version: str | None = None,
) -> dict:
    import pymysql

    conn_sqlite = sqlite3.connect(sqlite_path)
    conn_sqlite.row_factory = sqlite3.Row
    mysql_conn = pymysql.connect(
        host=mysql_dsn.get("host") or "127.0.0.1",
        port=int(mysql_dsn.get("port") or 3306),
        user=mysql_dsn.get("user") or "root",
        password=mysql_dsn.get("password") or "",
        database=mysql_dsn.get("database") or "pasarguard",
        charset="utf8mb4",
    )

    try:
        cur = mysql_conn.cursor()

        def target_columns(table: str) -> list[str]:
            cur.execute(
                """
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                ORDER BY ORDINAL_POSITION
                """,
                (mysql_dsn.get("database") or "pasarguard", table),
            )
            return [r[0] for r in cur.fetchall()]

        def truncate(table: str) -> None:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            cur.execute(f"TRUNCATE TABLE `{table}`")
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")

        def insert(table: str, columns: list[str], values: tuple) -> None:
            cols = ", ".join(f"`{c}`" for c in columns)
            ph = ", ".join(["%s"] * len(columns))
            cur.execute(f"INSERT INTO `{table}` ({cols}) VALUES ({ph})", values)

        def reset_seq(table: str) -> None:
            cur.execute(f"SELECT COALESCE(MAX(id), 0) + 1 FROM `{table}`")
            nxt = cur.fetchone()[0]
            cur.execute(f"ALTER TABLE `{table}` AUTO_INCREMENT = {int(nxt)}")

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

        mysql_conn.commit()
        return stats
    finally:
        conn_sqlite.close()
        mysql_conn.close()
