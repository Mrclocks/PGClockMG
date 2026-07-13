"""Database readers/writers for universal cross-DB copy."""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Iterable

from app.services.native_migration.copy_core import (
    TABLE_ORDER,
    SKIP_TABLES,
    convert_value,
    sqlite_columns,
    sqlite_table_names,
)


def _dsn_from_conn(conn: dict, db_type: str) -> dict:
    from app.services.db_credentials import migration_port

    default_user = "postgres" if db_type in ("postgresql", "timescaledb") else "root"
    return {
        "host": conn.get("host") or "127.0.0.1",
        "port": migration_port(conn, db_type),
        "database": conn.get("database") or "pasarguard",
        "user": conn.get("user") or default_user,
        "password": conn.get("password") or "",
    }


class TableReader(ABC):
    @abstractmethod
    def source_tables(self) -> set[str]:
        pass

    @abstractmethod
    def source_columns(self, table: str) -> list[str]:
        pass

    @abstractmethod
    def fetch_rows(self, table: str, columns: list[str]) -> Iterable[tuple]:
        pass

    @abstractmethod
    def close(self) -> None:
        pass


class TableWriter(ABC):
    @abstractmethod
    def target_columns(self, table: str) -> list[str]:
        pass

    @abstractmethod
    def truncate(self, table: str) -> None:
        pass

    @abstractmethod
    def insert(self, table: str, columns: list[str], values: tuple) -> None:
        pass

    @abstractmethod
    def reset_sequence(self, table: str) -> None:
        pass

    @abstractmethod
    def set_alembic_version(self, version: str) -> None:
        pass

    @abstractmethod
    def commit(self) -> None:
        pass

    @abstractmethod
    def close(self) -> None:
        pass

    def recover(self) -> None:
        """Recover writer after a failed statement (no-op by default)."""
        pass


class SqliteReader(TableReader):
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row

    def source_tables(self) -> set[str]:
        return sqlite_table_names(self._conn)

    def source_columns(self, table: str) -> list[str]:
        return sqlite_columns(self._conn, table)

    def fetch_rows(self, table: str, columns: list[str]) -> Iterable[tuple]:
        cols = ", ".join(columns)
        for row in self._conn.execute(f"SELECT {cols} FROM {table}"):
            yield tuple(row[c] for c in columns)

    def close(self) -> None:
        self._conn.close()


class MysqlReader(TableReader):
    def __init__(self, dsn: dict):
        import pymysql

        self._conn = pymysql.connect(
            host=dsn["host"],
            port=int(dsn["port"]),
            user=dsn["user"],
            password=dsn["password"],
            database=dsn["database"],
            charset="utf8mb4",
        )
        self._db = dsn["database"]

    def source_tables(self) -> set[str]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s",
            (self._db,),
        )
        return {r[0] for r in cur.fetchall()}

    def source_columns(self, table: str) -> list[str]:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT COLUMN_NAME FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
            """,
            (self._db, table),
        )
        return [r[0] for r in cur.fetchall()]

    def fetch_rows(self, table: str, columns: list[str]) -> Iterable[tuple]:
        cur = self._conn.cursor()
        cols = ", ".join(f"`{c}`" for c in columns)
        cur.execute(f"SELECT {cols} FROM `{table}`")
        for row in cur.fetchall():
            yield row

    def close(self) -> None:
        self._conn.close()


class PostgresReader(TableReader):
    def __init__(self, dsn: dict):
        import psycopg2

        self._conn = psycopg2.connect(
            host=dsn["host"],
            port=int(dsn["port"]),
            dbname=dsn["database"],
            user=dsn["user"],
            password=dsn["password"],
        )

    def source_tables(self) -> set[str]:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            """
        )
        return {r[0] for r in cur.fetchall()}

    def source_columns(self, table: str) -> list[str]:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
            """,
            (table,),
        )
        return [r[0] for r in cur.fetchall()]

    def fetch_rows(self, table: str, columns: list[str]) -> Iterable[tuple]:
        cur = self._conn.cursor()
        cols = ", ".join(f'"{c}"' for c in columns)
        cur.execute(f'SELECT {cols} FROM "{table}"')
        for row in cur.fetchall():
            yield row

    def close(self) -> None:
        self._conn.close()


class SqliteWriter(TableWriter):
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)

    def target_columns(self, table: str) -> list[str]:
        return sqlite_columns(self._conn, table)

    def truncate(self, table: str) -> None:
        self._conn.execute(f"DELETE FROM {table}")

    def insert(self, table: str, columns: list[str], values: tuple) -> None:
        cols = ", ".join(columns)
        ph = ", ".join(["?"] * len(columns))
        self._conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({ph})", values)

    def reset_sequence(self, table: str) -> None:
        pass

    def set_alembic_version(self, version: str) -> None:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32))"
        )
        self._conn.execute("DELETE FROM alembic_version")
        self._conn.execute(
            "INSERT INTO alembic_version (version_num) VALUES (?)", (version,)
        )

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


class MysqlWriter(TableWriter):
    def __init__(self, dsn: dict):
        import pymysql

        self._db = dsn["database"]
        self._conn = pymysql.connect(
            host=dsn["host"],
            port=int(dsn["port"]),
            user=dsn["user"],
            password=dsn["password"],
            database=self._db,
            charset="utf8mb4",
        )

    def target_columns(self, table: str) -> list[str]:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT COLUMN_NAME FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
            """,
            (self._db, table),
        )
        return [r[0] for r in cur.fetchall()]

    def truncate(self, table: str) -> None:
        cur = self._conn.cursor()
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        cur.execute(f"TRUNCATE TABLE `{table}`")
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")

    def insert(self, table: str, columns: list[str], values: tuple) -> None:
        cur = self._conn.cursor()
        cols = ", ".join(f"`{c}`" for c in columns)
        ph = ", ".join(["%s"] * len(columns))
        cur.execute(f"INSERT INTO `{table}` ({cols}) VALUES ({ph})", values)

    def reset_sequence(self, table: str) -> None:
        cur = self._conn.cursor()
        cur.execute(f"SELECT COALESCE(MAX(id), 0) + 1 FROM `{table}`")
        nxt = cur.fetchone()[0]
        cur.execute(f"ALTER TABLE `{table}` AUTO_INCREMENT = {int(nxt)}")

    def set_alembic_version(self, version: str) -> None:
        cur = self._conn.cursor()
        cur.execute("DELETE FROM alembic_version")
        cur.execute(
            "INSERT INTO alembic_version (version_num) VALUES (%s)", (version,)
        )

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


class PostgresWriter(TableWriter):
    def __init__(self, dsn: dict):
        import psycopg2
        from psycopg2 import sql as psql

        self._psql = psql
        self._conn = psycopg2.connect(
            host=dsn["host"],
            port=int(dsn["port"]),
            dbname=dsn["database"],
            user=dsn["user"],
            password=dsn["password"],
        )

    def recover(self) -> None:
        """Clear aborted transaction state without losing prior successful work."""
        from psycopg2 import extensions

        if self._conn.get_transaction_status() != extensions.TRANSACTION_STATUS_INERROR:
            return
        try:
            cur = self._conn.cursor()
            cur.execute("ROLLBACK TO SAVEPOINT pgmig_row")
        except Exception:
            self._conn.rollback()

    def target_columns(self, table: str) -> list[str]:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ORDINAL_POSITION
            """,
            (table,),
        )
        return [r[0] for r in cur.fetchall()]

    def truncate(self, table: str) -> None:
        cur = self._conn.cursor()
        cur.execute(
            self._psql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(
                self._psql.Identifier(table)
            )
        )

    def insert(self, table: str, columns: list[str], values: tuple) -> None:
        cur = self._conn.cursor()
        cur.execute("SAVEPOINT pgmig_row")
        try:
            col_list = self._psql.SQL(", ").join(self._psql.Identifier(c) for c in columns)
            placeholders = self._psql.SQL(", ").join(self._psql.Placeholder() for _ in columns)
            q = self._psql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                self._psql.Identifier(table), col_list, placeholders
            )
            cur.execute(q, values)
            cur.execute("RELEASE SAVEPOINT pgmig_row")
        except Exception:
            try:
                cur.execute("ROLLBACK TO SAVEPOINT pgmig_row")
            except Exception:
                self._conn.rollback()
            raise

    def reset_sequence(self, table: str) -> None:
        cur = self._conn.cursor()
        # table names come from our whitelist; quote as identifier for MAX()
        cur.execute(
            f'SELECT setval(pg_get_serial_sequence(%s, \'id\'), '
            f'COALESCE((SELECT MAX(id) FROM "{table}"), 1), true)',
            (table,),
        )

    def set_alembic_version(self, version: str) -> None:
        cur = self._conn.cursor()
        cur.execute("DELETE FROM alembic_version")
        cur.execute(
            "INSERT INTO alembic_version (version_num) VALUES (%s)", (version,)
        )

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def create_reader(db_type: str, source_path: str | None, conn: dict) -> TableReader:
    if db_type == "sqlite":
        if not source_path:
            raise ValueError("SQLite source path required")
        return SqliteReader(source_path)
    dsn = _dsn_from_conn(conn, db_type)
    if db_type in ("mysql", "mariadb"):
        return MysqlReader(dsn)
    if db_type in ("postgresql", "timescaledb"):
        return PostgresReader(dsn)
    raise ValueError(f"Unsupported source database: {db_type}")


def create_writer(db_type: str, conn: dict, target_path: str | None = None) -> TableWriter:
    if db_type == "sqlite":
        path = target_path or conn.get("sqlite_path")
        if not path:
            raise ValueError("SQLite target path required")
        return SqliteWriter(path)
    dsn = _dsn_from_conn(conn, db_type)
    if db_type in ("mysql", "mariadb"):
        return MysqlWriter(dsn)
    if db_type in ("postgresql", "timescaledb"):
        return PostgresWriter(dsn)
    raise ValueError(f"Unsupported target database: {db_type}")


def copy_tables_universal(
    reader: TableReader,
    writer: TableWriter,
    log: Callable[[str], None],
    source_version: str | None = None,
) -> dict[str, int]:
    """Copy shared PasarGuard/Marzban tables from any reader to any writer."""
    stats: dict[str, int] = {}
    source_tables = reader.source_tables()

    for table in TABLE_ORDER:
        if table in SKIP_TABLES or table not in source_tables:
            continue

        src_cols = reader.source_columns(table)
        if not src_cols:
            continue
        tgt_cols = writer.target_columns(table)
        if not tgt_cols:
            log(f"Skip {table}: not in target schema")
            continue

        common = [c for c in src_cols if c in tgt_cols]
        if not common:
            log(f"Skip {table}: no matching columns")
            continue

        writer.truncate(table)
        count = 0
        errors = 0
        first_error = None
        for row in reader.fetch_rows(table, common):
            values = tuple(
                convert_value(table, col, row[i]) for i, col in enumerate(common)
            )
            try:
                writer.insert(table, common, values)
                count += 1
            except Exception as exc:
                writer.recover()
                errors += 1
                if first_error is None:
                    first_error = str(exc)
                if errors <= 3:
                    log(f"Row skip {table}: {str(exc)[:200]}")
        stats[table] = count
        if errors:
            log(f"Imported {table}: {count} rows, {errors} skipped — first error: {(first_error or '')[:200]}")
        else:
            log(f"Imported {table}: {count} rows ({len(common)} columns)")

        if "id" in tgt_cols:
            try:
                writer.reset_sequence(table)
            except Exception as exc:
                writer.recover()
                log(f"Sequence reset skip {table}: {str(exc)[:120]}")

        # Commit per table so one bad table cannot abort the whole copy
        try:
            writer.commit()
        except Exception as exc:
            writer.recover()
            log(f"Commit warning {table}: {str(exc)[:120]}")

    if source_version:
        writer.set_alembic_version(source_version)
    writer.commit()
    return stats
