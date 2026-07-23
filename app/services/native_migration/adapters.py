"""Database readers/writers for universal cross-DB copy."""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Iterable

from app.services.native_migration.copy_core import (
    TABLE_ORDER,
    SKIP_TABLES,
    SUBSCRIPTION_TABLES,
    MIGRATION_ABORT_IF_ZERO,
    STRICT_COMPLETE_TABLES,
    OPTIONAL_FK_COLUMNS,
    TARGET_INSERT_DEFAULTS,
    JSON_COLUMNS,
    BOOL_COLUMNS,
    convert_value,
    build_table_column_plan,
    build_inbound_tag_lookup,
    apply_host_defaults,
    parse_not_null_column,
    to_bool,
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

    def prepare_replace(self, tables: list[str]) -> None:
        """Optional: wipe all listed tables once before inserts (avoids mid-copy CASCADE)."""
        return None

    def begin_bulk_load(self) -> None:
        """Optional: disable FK checks for the duration of inserts."""
        return None

    def end_bulk_load(self) -> None:
        """Optional: restore FK checks after inserts."""
        return None

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
        self._bulk_prepared = False
        self._bulk_load_active = False
        # Keep FK off for the lifetime of bulk replace — SQLite "foreign key mismatch"
        # also fires when unique indexes are missing/rebuilt mid-copy.
        self._conn.execute("PRAGMA foreign_keys = OFF")

    def _coerce_row(self, table: str, columns: list[str], values: tuple) -> tuple:
        out = []
        for col, val in zip(columns, values):
            val = convert_value(table, col, val)
            if col in BOOL_COLUMNS and val is not None:
                val = 1 if to_bool(val) else 0
            out.append(val)
        return tuple(out)

    def target_columns(self, table: str) -> list[str]:
        return sqlite_columns(self._conn, table)

    def prepare_replace(self, tables: list[str]) -> None:
        self._conn.execute("PRAGMA foreign_keys = OFF")
        for t in tables:
            safe = "".join(c for c in t if c.isalnum() or c == "_")
            if safe != t:
                continue
            try:
                self._conn.execute(f'DELETE FROM "{safe}"')
            except Exception:
                pass
        self._conn.commit()
        self._bulk_prepared = True
        self._bulk_load_active = True

    def begin_bulk_load(self) -> None:
        self._conn.execute("PRAGMA foreign_keys = OFF")
        self._bulk_load_active = True

    def end_bulk_load(self) -> None:
        if not getattr(self, "_bulk_load_active", False):
            return
        self._conn.commit()
        # Re-enable for any post-copy verification the caller may do on this connection.
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._bulk_load_active = False

    def truncate(self, table: str) -> None:
        if self._bulk_prepared:
            return
        self._conn.execute(f'DELETE FROM "{table}"')

    def insert(self, table: str, columns: list[str], values: tuple) -> None:
        values = self._coerce_row(table, columns, values)
        cols = ", ".join(f'"{c}"' for c in columns)
        ph = ", ".join(["?"] * len(columns))
        self._conn.execute(f'INSERT INTO "{table}" ({cols}) VALUES ({ph})', values)

    def reset_sequence(self, table: str) -> None:
        pass

    def set_alembic_version(self, version: str) -> None:
        from app.services.native_migration.source_version import alembic_revisions_for_stamp

        revisions = alembic_revisions_for_stamp(version)
        if not revisions:
            return
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32))"
        )
        self._conn.execute("DELETE FROM alembic_version")
        for rev in revisions:
            self._conn.execute(
                "INSERT INTO alembic_version (version_num) VALUES (?)", (rev,)
            )

    def commit(self) -> None:
        self._conn.commit()

    def row_count(self, table: str) -> int:
        cur = self._conn.execute(f'SELECT COUNT(*) FROM "{table}"')
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
            autocommit=False,
        )
        self._col_meta: dict[str, dict[str, dict]] = {}
        self._enum_labels: dict[str, frozenset[str]] = {}
        self._bulk_prepared = False
        self._bulk_load_active = False

    def _load_meta(self, table: str) -> dict[str, dict]:
        if table in self._col_meta:
            return self._col_meta[table]
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
            """,
            (self._db, table),
        )
        meta: dict[str, dict] = {}
        for name, data_type, column_type, is_nullable in cur.fetchall():
            entry = {
                "data_type": data_type,
                "column_type": column_type or "",
                "nullable": is_nullable == "YES",
            }
            if data_type == "enum" and column_type:
                labels: list[str] = []
                inner = column_type[column_type.find("(") + 1 : column_type.rfind(")")]
                for part in inner.split(","):
                    part = part.strip().strip("'\"")
                    if part:
                        labels.append(part)
                entry["enum_labels"] = frozenset(labels)
            meta[name] = entry
        self._col_meta[table] = meta
        return meta

    def _fit_mysql_enum(self, labels: frozenset[str], val, nullable: bool):
        if val is None:
            return None
        if not isinstance(val, str):
            val = str(val)
        s = val.strip()
        if not s:
            return None if nullable else (sorted(labels)[0] if labels else None)
        if s in labels:
            return s
        low = s.lower()
        if low in labels:
            return low
        for lbl in labels:
            if lbl.lower() == low:
                return lbl
        # PasarGuard fingerprint.none may store as empty string in PG enum
        if low == "none" and "" in labels:
            return ""
        if nullable:
            return None
        if "none" in labels:
            return "none"
        return sorted(labels)[0] if labels else s

    def _coerce_row(self, table: str, columns: list[str], values: tuple) -> tuple:
        from app.services.native_migration.copy_core import convert_value, to_bool

        meta = self._load_meta(table)
        out = []
        for col, val in zip(columns, values):
            val = convert_value(table, col, val)
            col_meta = meta.get(col, {})
            data_type = col_meta.get("data_type", "")
            column_type = (col_meta.get("column_type") or "").lower()
            if data_type == "enum":
                labels = col_meta.get("enum_labels") or frozenset()
                val = self._fit_mysql_enum(
                    labels, val, col_meta.get("nullable", True),
                )
            elif data_type == "json":
                from app.services.native_migration.copy_core import coerce_json_value
                val = coerce_json_value(val)
            elif data_type in ("datetime", "timestamp", "date", "time"):
                from app.services.native_migration.copy_core import normalize_datetime_for_sql
                if val is not None:
                    val = normalize_datetime_for_sql(val)
            elif (
                col in BOOL_COLUMNS
                or data_type == "bit"
                or (data_type == "tinyint" and "tinyint(1)" in column_type)
            ):
                if val is not None:
                    val = 1 if to_bool(val) else 0
            out.append(val)
        return tuple(out)

    def enum_columns(self, table: str) -> list[str]:
        meta = self._load_meta(table)
        return [c for c, m in meta.items() if m.get("data_type") == "enum"]

    def json_columns(self, table: str, columns: list[str]) -> list[str]:
        meta = self._load_meta(table)
        return [
            c for c in columns
            if meta.get(c, {}).get("data_type") == "json" or c in JSON_COLUMNS
        ]

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
        if getattr(self, "_bulk_prepared", False):
            return
        cur = self._conn.cursor()
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        cur.execute(f"TRUNCATE TABLE `{table}`")
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")

    def prepare_replace(self, tables: list[str]) -> None:
        cur = self._conn.cursor()
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        for t in tables:
            safe = "".join(c for c in t if c.isalnum() or c == "_")
            if safe != t:
                continue
            try:
                cur.execute(f"TRUNCATE TABLE `{safe}`")
            except Exception:
                pass
        # Keep FK checks off until end_bulk_load
        self._conn.commit()
        self._bulk_prepared = True
        self._bulk_load_active = True

    def begin_bulk_load(self) -> None:
        cur = self._conn.cursor()
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        self._conn.commit()
        self._bulk_load_active = True

    def end_bulk_load(self) -> None:
        if not getattr(self, "_bulk_load_active", False):
            return
        cur = self._conn.cursor()
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        self._conn.commit()
        self._bulk_load_active = False

    def commit(self) -> None:
        self._conn.commit()

    def row_count(self, table: str) -> int:
        cur = self._conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM `{table}`")
        return int(cur.fetchone()[0])

    def recover(self) -> None:
        """Rollback failed row only — never wipe successful inserts in this table."""
        try:
            cur = self._conn.cursor()
            cur.execute("ROLLBACK TO SAVEPOINT pgmig_row")
        except Exception:
            pass

    def insert(self, table: str, columns: list[str], values: tuple) -> None:
        cur = self._conn.cursor()
        values = self._coerce_row(table, columns, values)
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
                pass
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
        from app.services.native_migration.source_version import alembic_revisions_for_stamp

        revisions = alembic_revisions_for_stamp(version)
        if not revisions:
            return
        cur = self._conn.cursor()
        cur.execute("DELETE FROM alembic_version")
        for rev in revisions:
            cur.execute(
                "INSERT INTO alembic_version (version_num) VALUES (%s)", (rev,)
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
        self._bulk_prepared = False
        self._bulk_load_active = False
        self._fk_disabled = False
        self._fk_mode: str | None = None  # "replica" | "triggers" | None
        self._trigger_tables: list[str] = []

    def _note(self, msg: str) -> None:
        fn = getattr(self, "_log", None)
        if callable(fn):
            try:
                fn(msg)
            except Exception:
                pass

    def _set_replication_role(self, role: str) -> bool:
        """Best-effort SET session_replication_role (needs superuser)."""
        try:
            cur = self._conn.cursor()
            cur.execute(
                self._psql.SQL("SET session_replication_role = {}").format(
                    self._psql.Literal(role)
                )
            )
            return True
        except Exception as exc:  # noqa: BLE001
            try:
                self._conn.rollback()
            except Exception:
                pass
            self._note(f"session_replication_role={role} not applied ({exc})")
            return False

    def _disable_table_triggers(self, tables: list[str]) -> bool:
        """Fallback for non-superuser: table owner can DISABLE TRIGGER ALL.

        This is what actually makes admins/users copy succeed when the DB role is
        `pasarguard` (not superuser) — session_replication_role is denied there.
        """
        cur = self._conn.cursor()
        disabled: list[str] = []
        for t in tables:
            try:
                cur.execute(
                    self._psql.SQL("ALTER TABLE {} DISABLE TRIGGER ALL").format(
                        self._psql.Identifier(t)
                    )
                )
                disabled.append(t)
            except Exception as exc:  # noqa: BLE001
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                # Re-open a clean txn and continue — partial disable still helps.
                cur = self._conn.cursor()
                self._note(f"DISABLE TRIGGER ALL failed on {t}: {exc}")
        if not disabled:
            return False
        self._conn.commit()
        self._trigger_tables = disabled
        return True

    def _enable_table_triggers(self) -> None:
        tables = list(getattr(self, "_trigger_tables", []) or [])
        if not tables:
            return
        cur = self._conn.cursor()
        for t in tables:
            try:
                cur.execute(
                    self._psql.SQL("ALTER TABLE {} ENABLE TRIGGER ALL").format(
                        self._psql.Identifier(t)
                    )
                )
            except Exception as exc:  # noqa: BLE001
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                cur = self._conn.cursor()
                self._note(f"ENABLE TRIGGER ALL failed on {t}: {exc}")
        try:
            self._conn.commit()
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
        self._trigger_tables = []

    def _disable_fk(self, tables: list[str]) -> bool:
        if self._set_replication_role("replica"):
            self._fk_mode = "replica"
            self._fk_disabled = True
            self._note("FK enforcement during copy: disabled (session_replication_role=replica)")
            return True
        if self._disable_table_triggers(tables):
            self._fk_mode = "triggers"
            self._fk_disabled = True
            self._note(
                f"FK enforcement during copy: disabled via ALTER TABLE DISABLE TRIGGER ALL "
                f"({len(self._trigger_tables)} tables)"
            )
            return True
        self._fk_mode = None
        self._fk_disabled = False
        self._note(
            "FK enforcement during copy: ENABLED — relying on parent-first table order "
            "(admin_roles→admins→users). Non-superuser + no trigger rights."
        )
        return False

    def prepare_replace(self, tables: list[str]) -> None:
        """Wipe all copy targets once, up front. Per-table TRUNCATE CASCADE was wiping
        users/admins when a later parent table (e.g. admin_roles) was truncated."""
        existing = []
        for t in tables:
            safe = "".join(c for c in t if c.isalnum() or c == "_")
            if safe != t:
                continue
            if self.target_columns(t):
                existing.append(t)
        if not existing:
            return
        # Disable FK before TRUNCATE+insert. Prefer replica role; fall back to
        # DISABLE TRIGGER ALL (works as table owner without superuser).
        self._disable_fk(existing)
        cur = self._conn.cursor()
        idents = self._psql.SQL(", ").join(self._psql.Identifier(t) for t in existing)
        cur.execute(
            self._psql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(idents)
        )
        self._conn.commit()
        self._bulk_prepared = True
        self._bulk_load_active = True

    def begin_bulk_load(self) -> None:
        # prepare_replace already disabled FK; only retry if that never ran.
        if not getattr(self, "_fk_disabled", False):
            # Without a table list we can still try replica mode.
            if self._set_replication_role("replica"):
                self._fk_mode = "replica"
                self._fk_disabled = True
        self._bulk_load_active = True

    def end_bulk_load(self) -> None:
        if not getattr(self, "_bulk_load_active", False):
            return
        mode = getattr(self, "_fk_mode", None)
        if mode == "replica":
            self._set_replication_role("origin")
        elif mode == "triggers":
            self._enable_table_triggers()
        try:
            self._conn.commit()
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
        self._bulk_load_active = False
        self._fk_disabled = False
        self._fk_mode = None

    def truncate(self, table: str) -> None:
        if self._bulk_prepared:
            return
        # Never CASCADE here — that deletes already-copied child tables (users via admins).
        cur = self._conn.cursor()
        cur.execute(
            self._psql.SQL("TRUNCATE TABLE {} RESTART IDENTITY").format(
                self._psql.Identifier(table)
            )
        )

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
            if "none" in labels:
                return "none"
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
        from psycopg2.extras import Json

        from app.services.native_migration.copy_core import (
            coerce_json_value, convert_value, to_bool,
        )

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
            elif kind in ("json", "jsonb"):
                parsed = coerce_json_value(val)
                out.append(Json(json.loads(parsed)) if parsed else None)
            elif kind in (
                "integer", "bigint", "smallint", "numeric", "double precision", "real",
            ) and val is not None and isinstance(val, str) and val.strip().isdigit():
                out.append(int(val.strip()))
            else:
                out.append(val)
        return tuple(out)

    def json_columns(self, table: str, columns: list[str]) -> list[str]:
        types = self._types_for(table)
        return [
            c for c in columns
            if types.get(c, "") in ("json", "jsonb") or c in JSON_COLUMNS
        ]

    def recover(self) -> None:
        """Rollback failed row only — never wipe successful inserts in this table."""
        from psycopg2 import extensions

        if self._conn.get_transaction_status() != extensions.TRANSACTION_STATUS_INERROR:
            return
        try:
            cur = self._conn.cursor()
            cur.execute("ROLLBACK TO SAVEPOINT pgmig_row")
        except Exception:
            pass

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
                pass
            raise

    def reset_sequence(self, table: str) -> None:
        cur = self._conn.cursor()
        cur.execute(
            f'SELECT setval(pg_get_serial_sequence(%s, \'id\'), '
            f'COALESCE((SELECT MAX(id) FROM "{table}"), 1), true)',
            (table,),
        )

    def set_alembic_version(self, version: str) -> None:
        from app.services.native_migration.source_version import alembic_revisions_for_stamp

        revisions = alembic_revisions_for_stamp(version)
        if not revisions:
            return
        cur = self._conn.cursor()
        cur.execute("DELETE FROM alembic_version")
        for rev in revisions:
            cur.execute(
                "INSERT INTO alembic_version (version_num) VALUES (%s)", (rev,)
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

    def boolean_columns(self, table: str, columns: list[str]) -> list[str]:
        types = self._types_for(table)
        return [c for c in columns if types.get(c) == "boolean"]

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


def _retry_with_defaults(
    writer: TableWriter,
    table: str,
    columns: list[str],
    values: tuple,
    overrides: dict[str, object],
) -> tuple[bool, str | None]:
    if not overrides:
        return False, "no defaults"
    cols = list(columns)
    vals = list(values)
    for col, default in overrides.items():
        if col in cols:
            vals[cols.index(col)] = default
    return _attempt_insert(writer, table, cols, tuple(vals))


def _try_insert_host_row(
    writer: TableWriter,
    columns: list[str],
    values: tuple,
    log: Callable[[str], None],
) -> tuple[bool, str | None]:
    """Hosts: multi-tier fallback so subscription hosts always copy."""
    table = "hosts"
    values = apply_host_defaults(columns, values)

    ok, err = _try_insert_row(writer, table, columns, values, log)
    if ok:
        return True, None

    host_defaults = {
        k: v for k, v in TARGET_INSERT_DEFAULTS.get("hosts", {}).items() if k in columns
    }
    ok2, err2 = _retry_with_defaults(writer, table, columns, values, host_defaults)
    if ok2:
        log("hosts: copied with PasarGuard defaults fallback")
        return True, None

    relax = [c for c in columns if c in JSON_COLUMNS or c in ("alpn", "inbound_tag")]
    if relax:
        ok3, err3 = _retry_null_columns(writer, table, columns, values, relax)
        if ok3:
            log("hosts: copied with relaxed JSON/ALPN/inbound_tag")
            return True, None
        err = err3 or err

    minimal_order = (
        "id", "remark", "address", "priority", "security", "fingerprint",
        "random_user_agent", "use_sni_as_host", "inbound_tag",
    )
    minimal = [c for c in minimal_order if c in columns]
    if minimal and len(minimal) < len(columns):
        min_vals = []
        for col in minimal:
            idx = columns.index(col)
            v = values[idx]
            if v is None and col in host_defaults:
                v = host_defaults[col]
            min_vals.append(v)
        min_vals = apply_host_defaults(minimal, tuple(min_vals))
        ok4, err4 = _attempt_insert(writer, table, minimal, min_vals)
        if ok4:
            log(f"hosts: copied minimal row ({len(minimal)} columns)")
            return True, None
        err = err4 or err

    return False, err


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

    # JSON syntax errors — null JSON columns and retry
    if (
        "invalid input syntax for type json" in err_low
        or "json" in err_low and ("syntax" in err_low or "truncated" in err_low)
    ):
        json_cols = []
        if isinstance(writer, PostgresWriter):
            json_cols = writer.json_columns(table, columns)
        elif isinstance(writer, MysqlWriter):
            json_cols = writer.json_columns(table, columns)
        else:
            json_cols = [c for c in columns if c in JSON_COLUMNS]
        if json_cols:
            ok4, err4 = _retry_null_columns(writer, table, columns, values, json_cols)
            if ok4:
                log(f"{table}: copied after clearing invalid JSON values")
                return True, None
            err = err4 or err

    # MySQL enum truncation
    if isinstance(writer, MysqlWriter) and (
        "data truncated" in err_low or "enum" in err_low
    ):
        enum_cols = [c for c in writer.enum_columns(table) if c in columns]
        ok5, err5 = _retry_null_columns(writer, table, columns, values, enum_cols)
        if ok5:
            log(f"{table}: copied after clearing invalid MySQL enum values")
            return True, None
        err = err5 or err

    # NOT NULL violations — apply known target defaults
    if "not null" in err_low or "cannot be null" in err_low:
        col = parse_not_null_column(err or "")
        defaults = TARGET_INSERT_DEFAULTS.get(table, {})
        if col and col in defaults and col in columns:
            ok6, err6 = _retry_with_defaults(
                writer, table, columns, values, {col: defaults[col]},
            )
            if ok6:
                log(f"{table}: copied with default {col}={defaults[col]!r}")
                return True, None
            err = err6 or err

    # PostgreSQL boolean coercion retry
    if isinstance(writer, PostgresWriter) and (
        "invalid input syntax for type boolean" in err_low
        or "type boolean" in err_low
    ):
        bool_cols = writer.boolean_columns(table, columns)
        if bool_cols:
            cols = list(columns)
            vals = list(values)
            for col in bool_cols:
                if col in cols:
                    idx = cols.index(col)
                    vals[idx] = to_bool(vals[idx]) if vals[idx] is not None else None
            ok7, err7 = _attempt_insert(writer, table, cols, tuple(vals))
            if ok7:
                log(f"{table}: copied after boolean coercion")
                return True, None
            err = err7 or err

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
    """Summarize tables that were not fully copied (for post-migration UI).

    ``has_gaps`` is True only when a *critical* table is incomplete.
    Best-effort history tables (usages, subscription updates, hwids, …) may
    appear in ``incomplete`` / ``soft_incomplete`` without failing the job —
    SQLite often keeps orphan FK rows that PostgreSQL correctly rejects.
    """
    incomplete: list[dict] = []
    seen: set[str] = set()
    critical = STRICT_COMPLETE_TABLES | SUBSCRIPTION_TABLES | MIGRATION_ABORT_IF_ZERO
    for table in list(TABLE_ORDER) + sorted(source_counts.keys()):
        if table in seen:
            continue
        seen.add(table)
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
    critical_incomplete = [i for i in incomplete if i["table"] in critical]
    soft_only = [i for i in incomplete if i["table"] not in critical]
    return {
        "incomplete": incomplete,
        "critical_incomplete": critical_incomplete,
        "soft_incomplete": soft_only,
        "has_gaps": bool(critical_incomplete),
    }


def copy_tables_universal(
    reader: TableReader,
    writer: TableWriter,
    log: Callable[[str], None],
    source_version: str | None = None,
    fail_hard: bool = True,
    stamp_alembic: bool = True,
) -> tuple[dict[str, int], dict]:
    """Copy shared PasarGuard/Marzban tables from any reader to any writer."""
    stats: dict[str, int] = {}
    # Let writers emit diagnostics (e.g. whether FK enforcement was disabled).
    try:
        setattr(writer, "_log", log)
    except Exception:
        pass
    source_tables = reader.source_tables()
    source_counts: dict[str, int] = {}
    attempted_tables: set[str] = set()
    table_first_errors: dict[str, str] = {}
    for t in TABLE_ORDER:
        n = _count_source_rows(reader, t)
        if n > 0:
            source_counts[t] = n
        elif n < 0:
            log(f"Warning: could not count source rows for {t}")

    # Copy whitelist first, then any extra tables present in both schemas
    ordered = list(TABLE_ORDER)
    for t in sorted(source_tables):
        if (
            t in SKIP_TABLES
            or t in ordered
            or t.startswith("sqlite_")
            or t.startswith("pg_")
        ):
            continue
        if writer.target_columns(t):
            ordered.append(t)
            n = _count_source_rows(reader, t)
            if n > 0:
                source_counts[t] = n
            log(f"Extra table queued for copy: {t}" + (f" ({n} rows)" if n > 0 else ""))

    # Build wipe list first — one bulk TRUNCATE avoids CASCADE wiping earlier tables
    wipe_tables: list[str] = []
    for table in ordered:
        if table in SKIP_TABLES or table not in source_tables:
            continue
        if not reader.source_columns(table):
            continue
        if not writer.target_columns(table):
            continue
        wipe_tables.append(table)
    if wipe_tables:
        try:
            writer.prepare_replace(wipe_tables)
            log(f"Pre-wiped {len(wipe_tables)} target tables before copy (safe bulk replace)")
        except Exception as exc:
            log(f"Bulk prepare_replace note: {str(exc)[:200]} — falling back to per-table truncate")
            # Never leave the connection in an aborted-transaction state.
            for meth in ("rollback",):
                fn = getattr(getattr(writer, "_conn", None), meth, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        pass

    try:
        writer.begin_bulk_load()
        return _copy_tables_universal_body(
            reader,
            writer,
            log,
            source_version=source_version,
            fail_hard=fail_hard,
            stamp_alembic=stamp_alembic,
            ordered=ordered,
            source_tables=source_tables,
            source_counts=source_counts,
            attempted_tables=attempted_tables,
            table_first_errors=table_first_errors,
            stats=stats,
        )
    finally:
        try:
            writer.end_bulk_load()
        except Exception as exc:
            log(f"end_bulk_load note: {str(exc)[:160]}")


def _copy_tables_universal_body(
    reader: TableReader,
    writer: TableWriter,
    log: Callable[[str], None],
    *,
    source_version: str | None,
    fail_hard: bool,
    stamp_alembic: bool,
    ordered: list[str],
    source_tables: set[str],
    source_counts: dict[str, int],
    attempted_tables: set[str],
    table_first_errors: dict[str, str],
    stats: dict[str, int],
) -> tuple[dict[str, int], dict]:
    for table in ordered:
        if table in SKIP_TABLES or table not in source_tables:
            continue

        src_cols = reader.source_columns(table)
        if not src_cols:
            continue
        tgt_cols = writer.target_columns(table)
        if not tgt_cols:
            log(f"Skip {table}: not in target schema")
            continue

        insert_cols, select_cols = build_table_column_plan(table, src_cols, tgt_cols)
        if not insert_cols:
            log(f"Skip {table}: no matching columns")
            continue

        attempted_tables.add(table)

        fetch_src = list(dict.fromkeys(c for c in select_cols if c is not None))
        defaults = TARGET_INSERT_DEFAULTS.get(table, {})
        src_index = {col: i for i, col in enumerate(fetch_src)}

        inbound_lookup: dict[int, str] = {}
        if table == "hosts" and "inbounds" in source_tables:
            ic = reader.source_columns("inbounds")
            if "id" in ic and "tag" in ic:
                inbound_lookup = build_inbound_tag_lookup(
                    reader.fetch_rows("inbounds", ["id", "tag"])
                )
            if (
                "inbound_tag" in tgt_cols
                and "inbound_tag" not in insert_cols
                and "inbound_id" in src_cols
            ):
                insert_cols.append("inbound_tag")
                select_cols.append("__inbound_id__")
                if "inbound_id" not in fetch_src:
                    fetch_src.append("inbound_id")
                    src_index["inbound_id"] = len(fetch_src) - 1

        writer.truncate(table)
        count = 0
        errors = 0
        first_error = None
        for row in reader.fetch_rows(table, fetch_src):
            values = []
            for ins_col, sel_col in zip(insert_cols, select_cols):
                if sel_col == "__inbound_id__":
                    iid = row[src_index["inbound_id"]]
                    raw = None
                    if iid is not None:
                        try:
                            raw = inbound_lookup.get(int(iid))
                        except (TypeError, ValueError):
                            raw = None
                elif sel_col is None:
                    raw = defaults.get(ins_col)
                else:
                    raw = row[src_index[sel_col]]
                values.append(convert_value(table, ins_col, raw))
            if table == "hosts":
                ok, row_err = _try_insert_host_row(
                    writer, insert_cols, tuple(values), log,
                )
            else:
                ok, row_err = _try_insert_row(
                    writer, table, insert_cols, tuple(values), log,
                )
            if ok:
                count += 1
            else:
                errors += 1
                if first_error is None:
                    first_error = row_err
                log_limit = errors <= 3 or table in SUBSCRIPTION_TABLES or table in STRICT_COMPLETE_TABLES
                if log_limit:
                    log(f"Row skip {table}: {(row_err or '')[:200]}")
                elif errors == 4:
                    log(f"Row skip {table}: … further skips suppressed (non-critical orphans)")
        stats[table] = count
        if first_error:
            table_first_errors[table] = first_error
        if errors:
            log(
                f"Imported {table}: {count} rows, {errors} skipped — "
                f"first error: {(first_error or '')[:200]}"
            )
        else:
            log(f"Imported {table}: {count} rows ({len(insert_cols)} columns)")

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

    if stamp_alembic and source_version and source_version != "head":
        from app.services.native_migration.source_version import alembic_revisions_for_stamp

        revisions = alembic_revisions_for_stamp(source_version)
        if revisions:
            writer.set_alembic_version(source_version)
            writer.commit()
            log(f"alembic_version stamped: {', '.join(revisions)}")
        else:
            log(
                "Skipping alembic_version stamp — target schema already at head "
                f"(unparsed stamp {source_version[:64]!r})"
            )
    else:
        writer.commit()

    # Final truth check — catch mid-copy CASCADE wipes that inflated earlier stats
    for table in MIGRATION_ABORT_IF_ZERO:
        src_n = source_counts.get(table, 0)
        if src_n <= 0 or table not in attempted_tables:
            continue
        try:
            live = writer.row_count(table)
        except Exception:
            live = -1
        if live >= 0:
            stats[table] = live
            if live == 0 and src_n > 0:
                hint = (table_first_errors.get(table) or "")[:300]
                raise RuntimeError(
                    f"Migration failed: {table} was {src_n} in source but 0 after full copy"
                    + (f". First error: {hint}" if hint else "")
                    + ". Check FK order / parent tables (e.g. admin_roles before admins)."
                )
            if live < src_n:
                log(f"Post-copy recount {table}: {live}/{src_n}")

    if fail_hard:
        for table in MIGRATION_ABORT_IF_ZERO:
            src_n = source_counts.get(table, 0)
            dst_n = stats.get(table, 0)
            if src_n > 0 and dst_n == 0 and table in attempted_tables:
                hint = (table_first_errors.get(table) or "unknown")[:400]
                raise RuntimeError(
                    f"Migration failed: source has {src_n} {table} but "
                    f"0 were copied to target. First error: {hint}"
                )

        for table in STRICT_COMPLETE_TABLES:
            src_n = source_counts.get(table, 0)
            dst_n = stats.get(table, 0)
            if src_n > 0 and dst_n < src_n and table in attempted_tables:
                hint = (table_first_errors.get(table) or "")[:400]
                raise RuntimeError(
                    f"Migration incomplete: {table} copied {dst_n}/{src_n} rows"
                    + (f". First error: {hint}" if hint else "")
                    + ". Nothing should be lost — fix and retry."
                )

    report = build_copy_report(source_counts, stats)
    report["source_counts"] = dict(source_counts)
    report["copied_counts"] = dict(stats)
    for item in report.get("incomplete", []):
        tbl = item["table"]
        if tbl in SUBSCRIPTION_TABLES or tbl in STRICT_COMPLETE_TABLES:
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
