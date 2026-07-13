"""Database readers/writers for universal cross-DB copy."""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Iterable

from app.services.native_migration.copy_core import (
    TABLE_ORDER,
    SKIP_TABLES,
    SUBSCRIPTION_TABLES,
    MIGRATION_ABORT_IF_ZERO,
    OPTIONAL_FK_COLUMNS,
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

    def row_count(self, table: str) -> int:
        """Rows in table after last commit (best effort)."""
        return -1

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

    def row_count(self, table: str) -> int:
        cur = self._conn.execute(f"SELECT COUNT(*) FROM {table}")
        return int(cur.fetchone()[0])

    def recover(self) -> None:
        self._conn.rollback()

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

    def commit(self) -> None:
        self._conn.commit()

    def row_count(self, table: str) -> int:
        cur = self._conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM `{table}`")
        return int(cur.fetchone()[0])

    def recover(self) -> None:
        self._conn.rollback()

    def insert(self, table: str, columns: list[str], values: tuple) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute("SAVEPOINT pgmig_row")
            cols = ", ".join(f"`{c}`" for c in columns)
            ph = ", ".join(["%s"] * len(columns))
            cur.execute(f"INSERT INTO `{table}` ({cols}) VALUES ({ph})", values)
            cur.execute("RELEASE SAVEPOINT pgmig_row")
        except Exception:
            try:
                cur.execute("ROLLBACK TO SAVEPOINT pgmig_row")
            except Exception:
                self._conn.rollback()
            raise

    def _insert_plain(self, table: str, columns: list[str], values: tuple) -> None:
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
        self._col_nullable: dict[str, dict[str, bool]] = {}
        self._enum_labels: dict[str, frozenset[str]] = {}

    def _enum_labels_for(self, udt_name: str) -> frozenset[str]:
        if udt_name not in self._enum_labels:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT e.enumlabel
                FROM pg_enum e
                JOIN pg_type t ON e.enumtypid = t.oid
                WHERE t.typname = %s
                """,
                (udt_name,),
            )
            self._enum_labels[udt_name] = frozenset(r[0] for r in cur.fetchall())
        return self._enum_labels[udt_name]

    def _types_for(self, table: str) -> dict[str, str]:
        if table in self._col_types:
            return self._col_types[table]
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT column_name, data_type, udt_name, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            """,
            (table,),
        )
        types: dict[str, str] = {}
        nullable: dict[str, bool] = {}
        for name, data_type, udt_name, is_nullable in cur.fetchall():
            nullable[name] = is_nullable == "YES"
            if data_type == "boolean" or udt_name == "bool":
                types[name] = "boolean"
            elif data_type == "USER-DEFINED":
                types[name] = f"enum:{udt_name}"
            else:
                types[name] = data_type or udt_name or ""
        self._col_types[table] = types
        self._col_nullable[table] = nullable
        return types

    def _nullable_for(self, table: str, column: str) -> bool:
        if table not in self._col_nullable:
            self._types_for(table)
        return self._col_nullable.get(table, {}).get(column, True)

    def _fit_enum(self, udt_name: str, val, nullable: bool):
        labels = self._enum_labels_for(udt_name)
        if val is None:
            return None
        if not isinstance(val, str):
            return val
        s = val.strip()
        if not s or s.lower() in ("none", "null", "default"):
            if nullable:
                return None
            return sorted(labels)[0] if labels else None
        if s in labels:
            return s
        low = s.lower()
        if low in labels:
            return low
        for lbl in labels:
            if lbl.lower() == low:
                return lbl
        if nullable:
            return None
        return sorted(labels)[0] if labels else None

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
                udt = kind.split(":", 1)[1]
                out.append(self._fit_enum(udt, val, self._nullable_for(table, col)))
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

    def row_count(self, table: str) -> int:
        cur = self._conn.cursor()
        cur.execute(f'SELECT COUNT(*) FROM "{table}"')
        return int(cur.fetchone()[0])

    def enum_columns(self, table: str) -> list[str]:
        types = self._types_for(table)
        return [c for c, k in types.items() if k.startswith("enum:")]

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


def _attempt_insert(
    writer: TableWriter,
    table: str,
    columns: list[str],
    values: tuple,
) -> tuple[bool, str | None]:
    try:
        writer.insert(table, columns, values)
        return True, None
    except Exception as exc:
        writer.recover()
        return False, str(exc)


def _retry_null_columns(
    writer: TableWriter,
    table: str,
    columns: list[str],
    values: tuple,
    cols_to_null: list[str],
) -> tuple[bool, str | None]:
    if not cols_to_null:
        return False, "no columns to null"
    cols = list(columns)
    vals = list(values)
    for col in cols_to_null:
        if col in cols:
            vals[cols.index(col)] = None
    return _attempt_insert(writer, table, cols, tuple(vals))


def _try_insert_row(
    writer: TableWriter,
    table: str,
    columns: list[str],
    values: tuple,
    log: Callable[[str], None],
) -> tuple[bool, str | None]:
    """Insert one row with FK / enum retries for maximum copy fidelity."""
    ok, err = _attempt_insert(writer, table, columns, values)
    if ok:
        return True, None

    err_low = (err or "").lower()

    # Optional FK columns (nodes, hosts)
    for fk_col in OPTIONAL_FK_COLUMNS.get(table, ()):
        if fk_col in columns and (
            "foreign key" in err_low
            or "violates foreign key" in err_low
            or fk_col.replace("_", "") in err_low.replace("_", "")
        ):
            ok2, err2 = _retry_null_columns(writer, table, columns, values, [fk_col])
            if ok2:
                log(f"{table}: copied with {fk_col}=NULL (FK retry)")
                return True, None
            err = err2 or err

    # PostgreSQL enum: null enum fields and retry
    if isinstance(writer, PostgresWriter) and (
        "invalid input value for enum" in err_low or "enum" in err_low
    ):
        enum_cols = [c for c in writer.enum_columns(table) if c in columns]
        ok3, err3 = _retry_null_columns(writer, table, columns, values, enum_cols)
        if ok3:
            log(f"{table}: copied after clearing invalid enum values")
            return True, None
        err = err3 or err

    return False, err


def _count_source_rows(reader: TableReader, table: str) -> int:
    if table not in reader.source_tables():
        return 0
    try:
        cols = reader.source_columns(table)
        if not cols:
            return 0
        key_col = "id" if "id" in cols else cols[0]
        return sum(1 for _ in reader.fetch_rows(table, [key_col]))
    except Exception:
        return -1


def build_copy_report(
    source_counts: dict[str, int],
    stats: dict[str, int],
) -> dict:
    """Summarize tables that were not fully copied (for post-migration UI)."""
    incomplete: list[dict] = []
    for table in TABLE_ORDER:
        src = source_counts.get(table, 0)
        if src <= 0:
            continue
        copied = stats.get(table, 0)
        if copied < src:
            incomplete.append({
                "table": table,
                "source": src,
                "copied": copied,
                "missing": src - copied,
            })
    return {"incomplete": incomplete, "has_gaps": bool(incomplete)}


def copy_tables_universal(
    reader: TableReader,
    writer: TableWriter,
    log: Callable[[str], None],
    source_version: str | None = None,
    fail_hard: bool = True,
) -> tuple[dict[str, int], dict]:
    """Copy shared PasarGuard/Marzban tables from any reader to any writer."""
    stats: dict[str, int] = {}
    source_tables = reader.source_tables()
    source_counts: dict[str, int] = {}
    for t in TABLE_ORDER:
        n = _count_source_rows(reader, t)
        if n > 0:
            source_counts[t] = n
        elif n < 0:
            log(f"Warning: could not count source rows for {t}")

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
                log_limit = errors <= 20 or table in SUBSCRIPTION_TABLES
                if log_limit:
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
            verified = writer.row_count(table)
            if verified >= 0 and verified != count:
                log(f"Count verify {table}: inserted {count}, committed {verified}")
                count = verified
                stats[table] = count
        except Exception as exc:
            writer.recover()
            log(f"Commit warning {table}: {str(exc)[:120]}")
            verified = writer.row_count(table)
            if verified >= 0:
                stats[table] = verified

    if source_version and source_version != "head":
        writer.set_alembic_version(source_version)
        writer.commit()
    else:
        writer.commit()

    if fail_hard:
        for table in MIGRATION_ABORT_IF_ZERO:
            src_n = source_counts.get(table, 0)
            dst_n = stats.get(table, 0)
            if src_n > 0 and dst_n == 0:
                raise RuntimeError(
                    f"Migration failed: source has {src_n} {table} but "
                    f"0 were copied to target. Subscription data missing."
                )

    report = build_copy_report(source_counts, stats)
    for item in report.get("incomplete", []):
        tbl = item["table"]
        if tbl in SUBSCRIPTION_TABLES:
            log(
                f"INCOMPLETE {tbl}: {item['copied']}/{item['source']} copied "
                f"({item['missing']} missing) — check row-skip errors above"
            )
        else:
            log(
                f"Partial {tbl}: {item['copied']}/{item['source']} copied "
                f"({item['missing']} not transferred)"
            )

    return stats, report
