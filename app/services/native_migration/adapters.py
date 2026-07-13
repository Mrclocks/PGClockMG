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
        self._col_types: dict[str, dict[str, str]] = {}

    def _types_for(self, table: str) -> dict[str, str]:
        if table in self._col_types:
            return self._col_types[table]
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT column_name, data_type, udt_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            """,
            (table,),
        )
        types: dict[str, str] = {}
        for name, data_type, udt_name in cur.fetchall():
            # Normalize: boolean / USER-DEFINED enums
            if data_type == "boolean" or udt_name == "bool":
                types[name] = "boolean"
            elif data_type == "USER-DEFINED":
                types[name] = f"enum:{udt_name}"
            else:
                types[name] = data_type or udt_name or ""
        self._col_types[table] = types
        return types

    def _coerce_row(self, table: str, columns: list[str], values: tuple) -> tuple:
        from app.services.native_migration.copy_core import to_bool, convert_value

        types = self._types_for(table)
        out = []
        for col, val in zip(columns, values):
            kind = types.get(col, "")
            val = convert_value(table, col, val)
            if kind == "boolean":
                out.append(to_bool(val) if val is not None else None)
            elif kind.startswith("enum:"):
                # Empty / obsolete → NULL; never invent a PasarGuard default
                if val is None or (isinstance(val, str) and val.strip() == ""):
                    out.append(None)
                else:
                    out.append(val)
            else:
                out.append(val)
        return tuple(out)

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
            ORDER BY ordinal_position
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
            values = self._coerce_row(table, columns, values)
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


def _try_insert_row(
    writer: TableWriter,
    table: str,
    columns: list[str],
    values: tuple,
    log: Callable[[str], None],
) -> tuple[bool, str | None]:
    """Insert one row; for nodes retry without core_config_id on FK errors."""
    try:
        writer.insert(table, columns, values)
        return True, None
    except Exception as exc:
        writer.recover()
        err = str(exc)
        err_low = err.lower()
        if table == "nodes" and "core_config" in err_low and "core_config_id" in columns:
            cols = list(columns)
            vals = list(values)
            idx = cols.index("core_config_id")
            vals[idx] = None
            try:
                writer.insert(table, cols, tuple(vals))
                log("Node row copied with core_config_id=NULL (optional FK skipped)")
                return True, None
            except Exception as exc2:
                writer.recover()
                err = str(exc2)
        return False, err


# Tables that must copy for subscription links / panel login
CRITICAL_TABLES = ("users", "hosts", "admins")

# Optional — skip rows / warn if none copied (nodes need separate pg-node in PG v5)
OPTIONAL_TABLES = ("nodes", "core_configs", "groups")


def copy_tables_universal(
    reader: TableReader,
    writer: TableWriter,
    log: Callable[[str], None],
    source_version: str | None = None,
    fail_hard: bool = True,
) -> dict[str, int]:
    """Copy shared PasarGuard/Marzban tables from any reader to any writer."""
    stats: dict[str, int] = {}
    source_tables = reader.source_tables()
    source_counts: dict[str, int] = {}

    for table in (
        "users", "admins", "hosts", "nodes", "core_configs", "groups",
    ):
        if table in source_tables:
            try:
                source_counts[table] = sum(1 for _ in reader.fetch_rows(table, ["id"]))
            except Exception:
                # Fallback: try without id column
                try:
                    cols = reader.source_columns(table)
                    if cols:
                        source_counts[table] = sum(1 for _ in reader.fetch_rows(table, [cols[0]]))
                    else:
                        source_counts[table] = 0
                except Exception:
                    source_counts[table] = -1

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
            ok, row_err = _try_insert_row(writer, table, common, values, log)
            if ok:
                count += 1
            else:
                errors += 1
                if first_error is None:
                    first_error = row_err
                if errors <= 5:
                    log(f"Row skip {table}: {(row_err or '')[:200]}")
        stats[table] = count
        if errors:
            log(
                f"Imported {table}: {count} rows, {errors} skipped — "
                f"first error: {(first_error or '')[:200]}"
            )
        else:
            log(f"Imported {table}: {count} rows ({len(common)} columns)")

        if "id" in tgt_cols:
            try:
                writer.reset_sequence(table)
            except Exception as exc:
                writer.recover()
                log(f"Sequence reset skip {table}: {str(exc)[:120]}")

        try:
            writer.commit()
        except Exception as exc:
            writer.recover()
            log(f"Commit warning {table}: {str(exc)[:120]}")

    if source_version and source_version != "head":
        writer.set_alembic_version(source_version)
        writer.commit()
    else:
        writer.commit()

    if fail_hard:
        for critical in CRITICAL_TABLES:
            src_n = source_counts.get(critical, 0)
            dst_n = stats.get(critical, 0)
            if src_n > 0 and dst_n == 0:
                raise RuntimeError(
                    f"Migration failed: source has {src_n} {critical} but "
                    f"0 were copied to target. Check row-skip errors above."
                )
        for optional in OPTIONAL_TABLES:
            src_n = source_counts.get(optional, 0)
            dst_n = stats.get(optional, 0)
            if src_n > 0 and dst_n == 0:
                log(
                    f"Warning: {src_n} source {optional} not copied — "
                    "optional; subscription links are unchanged. "
                    "Configure manually in PasarGuard/pg-node if needed."
                )

    return stats
