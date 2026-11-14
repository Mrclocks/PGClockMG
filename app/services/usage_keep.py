"""Keep large usage/history tables aside during the panel schema upgrade, then restore.

Large ``node_user_usages`` style tables used to be TRUNCATEd before the PasarGuard
alembic upgrade (the ``bigint id`` rebuild can take hours on millions of rows),
which dropped the whole usage history. Now the rows are copied to a side table
first (``_pgkeep_<table>``, no FKs / indexes so alembic ignores it) and copied
back with ``INSERT IGNORE`` once the upgrade finished.

Everything here is best-effort and fail-open: if stashing fails the caller falls
back to the old behaviour (truncate), if restoring fails the side table is left
in place and a warning is logged — the migration itself never fails because of it.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

KEEP_PREFIX = "_pgkeep_"
_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# child column -> parent table (rows pointing at a missing parent are skipped)
_FK_PARENTS = {"user_id": "users", "node_id": "nodes"}


def _ident(name: str) -> str:
    if not _SAFE_IDENT.match(name or ""):
        raise ValueError(f"unsafe identifier {name!r}")
    return name


def keep_name(table: str) -> str:
    return f"{KEEP_PREFIX}{_ident(table)}"


# ── SQLite ────────────────────────────────────────────────────────────────

def _sqlite_has(db: sqlite3.Connection, table: str) -> bool:
    return bool(db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def _sqlite_cols(db: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in db.execute(f'PRAGMA table_info("{_ident(table)}")').fetchall()]


def stash_sqlite(db: sqlite3.Connection, table: str) -> bool:
    try:
        keep = keep_name(table)
        db.execute(f'DROP TABLE IF EXISTS "{keep}"')
        db.execute(f'CREATE TABLE "{keep}" AS SELECT * FROM "{_ident(table)}"')
        db.commit()
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return False


def restore_sqlite(sqlite_path: str | Path) -> list[tuple[str, int]]:
    path = Path(sqlite_path)
    restored: list[tuple[str, int]] = []
    if not path.exists():
        return restored
    db = sqlite3.connect(str(path), timeout=60)
    try:
        names = [
            r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ? ESCAPE '\\'",
                (KEEP_PREFIX.replace("_", "\\_") + "%",),
            ).fetchall()
        ]
        names = [n for n in names if n.startswith(KEEP_PREFIX)]
        for keep in names:
            table = keep[len(KEEP_PREFIX):]
            try:
                if not _SAFE_IDENT.match(table) or not _sqlite_has(db, table):
                    continue
                cols = [c for c in _sqlite_cols(db, keep) if c in set(_sqlite_cols(db, table))]
                if not cols:
                    continue
                col_sql = ", ".join(f'"{_ident(c)}"' for c in cols)
                where: list[str] = []
                for col, parent in _FK_PARENTS.items():
                    if col in cols and _sqlite_has(db, parent):
                        where.append(f'("{col}" IS NULL OR "{col}" IN (SELECT id FROM "{parent}"))')
                sql = (
                    f'INSERT OR IGNORE INTO "{table}" ({col_sql}) '
                    f'SELECT {col_sql} FROM "{keep}"'
                    + (" WHERE " + " AND ".join(where) if where else "")
                )
                cur = db.execute(sql)
                n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                db.execute(f'DROP TABLE "{keep}"')
                db.commit()
                restored.append((table, n))
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
    finally:
        db.close()
    return restored


# ── MySQL / MariaDB ───────────────────────────────────────────────────────

def _mysql_cols(cur, database: str, table: str) -> list[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
        (database, table),
    )
    return [r[0] for r in cur.fetchall()]


def _mysql_has(cur, database: str, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s LIMIT 1",
        (database, table),
    )
    return bool(cur.fetchone())


def stash_mysql(cur, table: str) -> bool:
    try:
        keep = keep_name(table)
        cur.execute(f"DROP TABLE IF EXISTS `{keep}`")
        cur.execute(f"CREATE TABLE `{keep}` AS SELECT * FROM `{_ident(table)}`")
        return True
    except Exception:
        return False


def restore_mysql(
    *, host: str, port: int | str, user: str, password: str, database: str,
) -> list[tuple[str, int]]:
    import pymysql

    restored: list[tuple[str, int]] = []
    with pymysql.connect(
        host=host, port=int(port), user=user, password=password or "",
        database=database, charset="utf8mb4", autocommit=True,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=%s AND table_name LIKE %s",
                (database, KEEP_PREFIX.replace("_", "\\_") + "%"),
            )
            keeps = [r[0] for r in cur.fetchall() if str(r[0]).startswith(KEEP_PREFIX)]
            for keep in keeps:
                table = keep[len(KEEP_PREFIX):]
                try:
                    if not _SAFE_IDENT.match(table) or not _mysql_has(cur, database, table):
                        continue
                    target_cols = set(_mysql_cols(cur, database, table))
                    cols = [c for c in _mysql_cols(cur, database, keep) if c in target_cols]
                    if not cols:
                        continue
                    col_sql = ", ".join(f"`{_ident(c)}`" for c in cols)
                    where: list[str] = []
                    for col, parent in _FK_PARENTS.items():
                        if col in cols and _mysql_has(cur, database, parent):
                            where.append(f"(`{col}` IS NULL OR `{col}` IN (SELECT id FROM `{parent}`))")
                    sql = (
                        f"INSERT IGNORE INTO `{table}` ({col_sql}) "
                        f"SELECT {col_sql} FROM `{keep}`"
                        + (" WHERE " + " AND ".join(where) if where else "")
                    )
                    cur.execute(sql)
                    n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                    cur.execute(f"DROP TABLE `{keep}`")
                    restored.append((table, n))
                except Exception:
                    continue
    return restored
