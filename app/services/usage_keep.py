"""Keep large usage/history tables aside during the panel schema upgrade, then restore.

Large ``node_user_usages`` style tables used to be TRUNCATEd before the PasarGuard
alembic upgrade (the ``bigint id`` rebuild can take hours on millions of rows),
which dropped the whole usage history. Now the rows are copied to a side table
first (``_pgkeep_<table>``, no FKs so alembic ignores it) and copied back in small
committed chunks after the upgrade.

Safety rules:
- The live table is only emptied when the side copy succeeded (callers check the
  return value of ``stash_*``); otherwise it is left untouched.
- Rows are restored WITHOUT their old ``id`` so they can never collide with rows the
  running panel already wrote (MySQL TRUNCATE resets AUTO_INCREMENT to 1). Chunks are
  deleted from the side table as they are restored, so a re-run never duplicates.
- A leftover side table from an earlier run is never dropped: new rows are appended.
- Copy-aside and emptying are paired (``stash_and_clear_*``): on SQLite in one
  transaction; on MySQL the side rows are removed again when the TRUNCATE fails.
  Rows are therefore never in both places, which would duplicate them on restore.
- Rows the restore cannot insert (user/node deleted meanwhile, constraint) are counted
  in ``skipped`` so the caller can log them.
- Failures never raise out of this module; they are returned for the caller to log.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
from pathlib import Path

KEEP_PREFIX = "_pgkeep_"
MYSQL_SEQ_COL = "pgk_seq"
CHUNK_ROWS = 20_000
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


def _sqlite_has_room(db: sqlite3.Connection) -> bool:
    """The side copy needs about one extra database file of free disk."""
    try:
        file = next((r[2] for r in db.execute("PRAGMA database_list") if r[1] == "main"), "")
        if not file:
            return True
        size = os.path.getsize(file)
        free = shutil.disk_usage(os.path.dirname(file) or ".").free
        return free > int(size * 1.2) + 64 * 1024 * 1024
    except Exception:
        return True  # cannot tell — let the copy itself decide


def stash_sqlite(db: sqlite3.Connection, table: str) -> bool:
    """Copy ``table`` aside. True only when the copy is complete."""
    try:
        keep = keep_name(table)
        if not _sqlite_has_room(db):
            return False
        if _sqlite_has(db, keep):
            # leftover from an interrupted run: keep it, append what is still live
            cols = [c for c in _sqlite_cols(db, table) if c in set(_sqlite_cols(db, keep))]
            if not cols:
                return False  # nothing could be copied — the table must not be emptied
            col_sql = ", ".join(f'"{c}"' for c in cols)
            db.execute(f'INSERT INTO "{keep}" ({col_sql}) SELECT {col_sql} FROM "{_ident(table)}"')
        else:
            db.execute(f'CREATE TABLE "{keep}" AS SELECT * FROM "{_ident(table)}"')
        db.commit()
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return False


def stash_and_clear_sqlite(db: sqlite3.Connection, table: str) -> bool:
    """Copy ``table`` aside and empty it in ONE transaction.

    Either both happen or neither: a failure (or crash) can never leave the rows both
    in the live table and in the side table, which would duplicate them on restore.
    """
    try:
        keep = keep_name(table)
        if not _sqlite_has_room(db):
            return False
    except Exception:
        return False
    prev_isolation = db.isolation_level
    try:
        db.commit()  # finish anything pending; the explicit transaction below is ours
        db.isolation_level = None
        db.execute("BEGIN IMMEDIATE")
        try:
            if _sqlite_has(db, keep):
                keep_cols = set(_sqlite_cols(db, keep))
                cols = [c for c in _sqlite_cols(db, table) if c in keep_cols]
                if not cols:
                    raise RuntimeError("side table has no common columns")
                col_sql = ", ".join(f'"{c}"' for c in cols)
                db.execute(f'INSERT INTO "{keep}" ({col_sql}) SELECT {col_sql} FROM "{_ident(table)}"')
            else:
                db.execute(f'CREATE TABLE "{keep}" AS SELECT * FROM "{_ident(table)}"')
            db.execute(f'DELETE FROM "{_ident(table)}"')
            db.execute("COMMIT")
            return True
        except Exception:
            try:
                db.execute("ROLLBACK")
            except Exception:
                pass
            return False
    except Exception:
        return False
    finally:
        db.isolation_level = prev_isolation


def restore_sqlite(
    sqlite_path: str | Path, failures: list | None = None, skipped: dict | None = None,
) -> list[tuple[str, int]]:
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
                    if failures is not None:
                        failures.append((table, "target table missing"))
                    continue
                target_cols = set(_sqlite_cols(db, table))
                cols = [c for c in _sqlite_cols(db, keep) if c in target_cols and c != "id"]
                if not cols:
                    if failures is not None:
                        failures.append((table, "no common columns"))
                    continue
                col_sql = ", ".join(f'"{_ident(c)}"' for c in cols)
                where = [
                    f'("{col}" IS NULL OR "{col}" IN (SELECT id FROM "{parent}"))'
                    for col, parent in _FK_PARENTS.items()
                    if col in cols and _sqlite_has(db, parent)
                ]
                cond = (" AND " + " AND ".join(where)) if where else ""
                inserted = 0
                while True:
                    lo_hi = db.execute(
                        f'SELECT MIN(rowid), MAX(rowid) FROM '
                        f'(SELECT rowid FROM "{keep}" ORDER BY rowid LIMIT {CHUNK_ROWS})'
                    ).fetchone()
                    if not lo_hi or lo_hi[0] is None:
                        break
                    lo, hi = lo_hi
                    chunk = db.execute(
                        f'SELECT COUNT(*) FROM "{keep}" WHERE rowid BETWEEN ? AND ?', (lo, hi),
                    ).fetchone()[0]
                    cur = db.execute(
                        f'INSERT OR IGNORE INTO "{table}" ({col_sql}) '
                        f'SELECT {col_sql} FROM "{keep}" WHERE rowid BETWEEN ? AND ?{cond}',
                        (lo, hi),
                    )
                    n_in = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                    inserted += n_in
                    if skipped is not None and chunk > n_in:
                        skipped[table] = skipped.get(table, 0) + int(chunk - n_in)
                    db.execute(f'DELETE FROM "{keep}" WHERE rowid BETWEEN ? AND ?', (lo, hi))
                    db.commit()  # short transactions: a running panel is never locked out
                db.execute(f'DROP TABLE "{keep}"')
                db.commit()
                restored.append((table, inserted))
            except Exception as exc:  # noqa: BLE001
                try:
                    db.rollback()
                except Exception:
                    pass
                if failures is not None:
                    failures.append((table, str(exc)[:200]))
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


def stash_mysql(cur, table: str, database: str | None = None) -> bool:
    """Copy ``table`` aside. True only when the copy is complete."""
    keep = keep_name(table)
    existed_before = False
    try:
        existed = database is not None and _mysql_has(cur, database, keep)
        existed_before = bool(existed)
        if existed:
            cols = [c for c in _mysql_cols(cur, database, table)
                    if c in set(_mysql_cols(cur, database, keep))]
            if not cols:
                return False  # nothing could be copied — the table must not be emptied
            col_sql = ", ".join(f"`{c}`" for c in cols)
            cur.execute(f"INSERT INTO `{keep}` ({col_sql}) SELECT {col_sql} FROM `{_ident(table)}`")
            return True
        cur.execute(
            f"CREATE TABLE `{keep}` (`{MYSQL_SEQ_COL}` BIGINT NOT NULL AUTO_INCREMENT, "
            f"PRIMARY KEY (`{MYSQL_SEQ_COL}`)) AS SELECT * FROM `{_ident(table)}`"
        )
        return True
    except Exception:
        # a half-built NEW side table is useless — drop it (a pre-existing one is never
        # dropped: it may hold history from an interrupted earlier run)
        try:
            if not existed_before:
                cur.execute(f"DROP TABLE IF EXISTS `{keep}`")
        except Exception:
            pass
        return False


def stash_and_clear_mysql(cur, table: str, database: str) -> bool:
    """Copy ``table`` aside, then TRUNCATE it. True only when both succeeded.

    MySQL DDL commits implicitly, so the two steps cannot share a transaction. When the
    TRUNCATE fails, the rows just added to the side table are removed again (the live
    table still has them) so a later restore cannot insert them a second time.
    """
    keep = keep_name(table)
    try:
        existed = _mysql_has(cur, database, keep)
        mark = 0
        if existed:
            cur.execute(f"SELECT COALESCE(MAX(`{MYSQL_SEQ_COL}`), 0) FROM `{keep}`")
            row = cur.fetchone()
            mark = int((row or [0])[0] or 0)
    except Exception:
        return False
    if not stash_mysql(cur, table, database):
        return False
    try:
        cur.execute(f"TRUNCATE TABLE `{_ident(table)}`")
        return True
    except Exception:
        try:
            if existed:
                cur.execute(f"DELETE FROM `{keep}` WHERE `{MYSQL_SEQ_COL}` > %s", (mark,))
            else:
                cur.execute(f"DROP TABLE IF EXISTS `{keep}`")
            try:
                cur.connection.commit()
            except Exception:
                pass
        except Exception:
            pass
        return False


def restore_mysql(
    *, host: str, port: int | str, user: str, password: str, database: str,
    failures: list | None = None, skipped: dict | None = None,
) -> list[tuple[str, int]]:
    import pymysql

    restored: list[tuple[str, int]] = []
    with pymysql.connect(
        host=host, port=int(port), user=user, password=password or "",
        database=database, charset="utf8mb4", autocommit=False,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=%s AND table_name LIKE %s",
                (database, KEEP_PREFIX.replace("_", "\\_") + "%"),
            )
            keeps = [r[0] for r in cur.fetchall() if str(r[0]).startswith(KEEP_PREFIX)]
            conn.commit()
            for keep in keeps:
                table = keep[len(KEEP_PREFIX):]
                try:
                    if not _SAFE_IDENT.match(table) or not _mysql_has(cur, database, table):
                        if failures is not None:
                            failures.append((table, "target table missing"))
                        continue
                    target_cols = set(_mysql_cols(cur, database, table))
                    keep_cols = _mysql_cols(cur, database, keep)
                    cols = [c for c in keep_cols if c in target_cols and c != "id" and c != MYSQL_SEQ_COL]
                    if not cols or MYSQL_SEQ_COL not in keep_cols:
                        if failures is not None:
                            failures.append((table, "unexpected side-table layout"))
                        continue
                    col_sql = ", ".join(f"`{_ident(c)}`" for c in cols)
                    where = [
                        f"(`{col}` IS NULL OR `{col}` IN (SELECT id FROM `{parent}`))"
                        for col, parent in _FK_PARENTS.items()
                        if col in cols and _mysql_has(cur, database, parent)
                    ]
                    cond = (" AND " + " AND ".join(where)) if where else ""
                    inserted = 0
                    while True:
                        cur.execute(
                            f"SELECT MIN(`{MYSQL_SEQ_COL}`), MAX(`{MYSQL_SEQ_COL}`) FROM "
                            f"(SELECT `{MYSQL_SEQ_COL}` FROM `{keep}` ORDER BY `{MYSQL_SEQ_COL}` "
                            f"LIMIT {CHUNK_ROWS}) x"
                        )
                        lo_hi = cur.fetchone()
                        if not lo_hi or lo_hi[0] is None:
                            break
                        lo, hi = lo_hi
                        cur.execute(
                            f"SELECT COUNT(*) FROM `{keep}` WHERE `{MYSQL_SEQ_COL}` BETWEEN %s AND %s",
                            (lo, hi),
                        )
                        chunk = int((cur.fetchone() or [0])[0] or 0)
                        cur.execute(
                            f"INSERT IGNORE INTO `{table}` ({col_sql}) SELECT {col_sql} FROM `{keep}` "
                            f"WHERE `{MYSQL_SEQ_COL}` BETWEEN %s AND %s{cond}",
                            (lo, hi),
                        )
                        n_in = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                        inserted += n_in
                        if skipped is not None and chunk > n_in:
                            skipped[table] = skipped.get(table, 0) + int(chunk - n_in)
                        cur.execute(
                            f"DELETE FROM `{keep}` WHERE `{MYSQL_SEQ_COL}` BETWEEN %s AND %s", (lo, hi),
                        )
                        conn.commit()
                    cur.execute(f"DROP TABLE `{keep}`")
                    conn.commit()
                    restored.append((table, inserted))
                except Exception as exc:  # noqa: BLE001
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    if failures is not None:
                        failures.append((table, str(exc)[:200]))
    return restored
