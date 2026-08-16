"""Pre-panel-boot hygiene for Marzban → PasarGuard dumps.

Small/clean dumps are a no-op (0 rows touched). Dirty/large Marzban dumps may
need:
  - case-insensitive unique-name renames (nodes / user_templates)
  - orphan FK cleanup (node_usages → nodes, etc.) mirroring PasarGuard's own
    alembic orphan cleanup so FK creation / table rebuilds do not fail with 1452.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from app.config import PASARGUARD_DATA
from app.services.db_credentials import get_target_connection, migration_port
from app.services.unique_name_heal import (
    heal_duplicate_unique_names,
    logs_indicate_duplicate_unique_name,
)

_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ident(name: str) -> str:
    if not _SAFE_IDENT.match(name or ""):
        raise RuntimeError(f"Invalid SQL identifier: {name!r}")
    return name


# Matches PasarGuard panel migration orphan cleanup (delete side).
ORPHAN_DELETE_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("node_usages", "node_id", "nodes", "id"),
    ("node_user_usages", "node_id", "nodes", "id"),
    ("node_user_usages", "user_id", "users", "id"),
    ("node_usage_reset_logs", "node_id", "nodes", "id"),
    ("next_plans", "user_id", "users", "id"),
)

# SET NULL style refs (parent missing → null child column).
# If the column is NOT NULL (common for hosts.inbound_tag on MySQL), we DELETE
# the orphan row instead — SET NULL raises 1048 and used to abort the whole heal.
ORPHAN_NULL_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("hosts", "inbound_tag", "inbounds", "tag"),
    ("next_plans", "user_template_id", "user_templates", "id"),
)

# High-churn usage/history tables. PasarGuard's "use bigint for id" alembic rebuilds
# these with copy-to-tmp + re-add FK — on large Marzban MySQL dumps that can run for
# hours and look "stuck" at 70%. Truncating large ones before panel boot is safe:
# the panel refills usage from live traffic; users/nodes/inbounds are untouched.
HEAVY_USAGE_TABLES: tuple[str, ...] = (
    "node_user_usages",
    "node_usages",
    "node_usage_reset_logs",
)
# Only truncate when clearly large — small/clean dumps stay untouched.
HEAVY_USAGE_ROW_THRESHOLD = 50_000
HEAVY_USAGE_SIZE_THRESHOLD = 16 * 1024 * 1024  # 16 MiB data+index


def orphan_delete_sql(child: str, child_col: str, parent: str, parent_col: str = "id") -> str:
    child, child_col, parent, parent_col = map(_ident, (child, child_col, parent, parent_col))
    return (
        f"DELETE FROM {child} WHERE {child}.{child_col} IS NOT NULL "
        f"AND NOT EXISTS (SELECT 1 FROM {parent} "
        f"WHERE {parent}.{parent_col} = {child}.{child_col})"
    )


def orphan_null_sql(child: str, child_col: str, parent: str, parent_col: str = "id") -> str:
    child, child_col, parent, parent_col = map(_ident, (child, child_col, parent, parent_col))
    return (
        f"UPDATE {child} SET {child_col} = NULL "
        f"WHERE {child}.{child_col} IS NOT NULL "
        f"AND NOT EXISTS (SELECT 1 FROM {parent} "
        f"WHERE {parent}.{parent_col} = {child}.{child_col})"
    )


def logs_indicate_orphan_fk(logs: str) -> bool:
    """True when panel/alembic logs show an orphan FK failure we can heal."""
    low = (logs or "").lower()
    if not any(
        s in low
        for s in (
            "foreign key constraint fails",
            "foreign key violation",
            "violates foreign key",
            "1452",
            "integrityerror",
        )
    ):
        return False
    return any(
        s in low
        for s in (
            "node_usages",
            "node_user_usages",
            "node_usage_reset",
            "next_plans",
            "node_id",
            "inbound_tag",
            "references nodes",
            "references users",
        )
    )


def _sqlite_table_exists(db: sqlite3.Connection, table: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return bool(row)


def _sqlite_column_exists(db: sqlite3.Connection, table: str, column: str) -> bool:
    rows = db.execute(f'PRAGMA table_info("{_ident(table)}")').fetchall()
    cols = {r[1] for r in rows}
    return column in cols


def _sqlite_column_nullable(db: sqlite3.Connection, table: str, column: str) -> bool:
    rows = db.execute(f'PRAGMA table_info("{_ident(table)}")').fetchall()
    for r in rows:
        if r[1] == column:
            return int(r[3] or 0) == 0  # notnull flag
    return True


def cleanup_orphans_sqlite(sqlite_path: str | Path) -> tuple[int, int]:
    """Delete/null orphan FK rows in SQLite. Returns (deleted, nulled)."""
    path = Path(sqlite_path)
    if not path.exists():
        return 0, 0
    deleted = 0
    nulled = 0
    db = sqlite3.connect(str(path))
    try:
        for child, child_col, parent, parent_col in ORPHAN_DELETE_SPECS:
            if not (_sqlite_table_exists(db, child) and _sqlite_table_exists(db, parent)):
                continue
            if not _sqlite_column_exists(db, child, child_col):
                continue
            cur = db.execute(orphan_delete_sql(child, child_col, parent, parent_col))
            deleted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        for child, child_col, parent, parent_col in ORPHAN_NULL_SPECS:
            if not (_sqlite_table_exists(db, child) and _sqlite_table_exists(db, parent)):
                continue
            if not (
                _sqlite_column_exists(db, child, child_col)
                and _sqlite_column_exists(db, parent, parent_col)
            ):
                continue
            if _sqlite_column_nullable(db, child, child_col):
                cur = db.execute(orphan_null_sql(child, child_col, parent, parent_col))
                nulled += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            else:
                cur = db.execute(orphan_delete_sql(child, child_col, parent, parent_col))
                deleted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        if deleted or nulled:
            db.commit()
    finally:
        db.close()
    return deleted, nulled


def _mysql_table_exists(cur, database: str, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema=%s AND table_name=%s LIMIT 1",
        (database, table),
    )
    return bool(cur.fetchone())


def _mysql_column_exists(cur, database: str, table: str, column: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name=%s AND column_name=%s LIMIT 1",
        (database, table, column),
    )
    return bool(cur.fetchone())


def _mysql_column_nullable(cur, database: str, table: str, column: str) -> bool:
    cur.execute(
        "SELECT IS_NULLABLE FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name=%s AND column_name=%s LIMIT 1",
        (database, table, column),
    )
    row = cur.fetchone()
    if not row:
        return True
    return str(row[0] or "").upper() == "YES"


def cleanup_orphans_mysql_conn(
    *,
    host: str,
    port: int | str,
    user: str,
    password: str,
    database: str,
) -> tuple[int, int]:
    import pymysql

    deleted = 0
    nulled = 0
    with pymysql.connect(
        host=host,
        port=int(port),
        user=user,
        password=password or "",
        database=database,
        charset="utf8mb4",
        autocommit=True,
    ) as conn:
        with conn.cursor() as cur:
            for child, child_col, parent, parent_col in ORPHAN_DELETE_SPECS:
                if not (
                    _mysql_table_exists(cur, database, child)
                    and _mysql_table_exists(cur, database, parent)
                    and _mysql_column_exists(cur, database, child, child_col)
                ):
                    continue
                cur.execute(orphan_delete_sql(child, child_col, parent, parent_col))
                deleted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            for child, child_col, parent, parent_col in ORPHAN_NULL_SPECS:
                if not (
                    _mysql_table_exists(cur, database, child)
                    and _mysql_table_exists(cur, database, parent)
                    and _mysql_column_exists(cur, database, child, child_col)
                    and _mysql_column_exists(cur, database, parent, parent_col)
                ):
                    continue
                if _mysql_column_nullable(cur, database, child, child_col):
                    cur.execute(orphan_null_sql(child, child_col, parent, parent_col))
                    nulled += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                else:
                    # NOT NULL (e.g. hosts.inbound_tag) — delete orphan rows instead.
                    cur.execute(orphan_delete_sql(child, child_col, parent, parent_col))
                    deleted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return deleted, nulled


def _mysql_table_bulk_stats(cur, database: str, table: str) -> tuple[int, int]:
    """Return (approx_rows, data+index bytes) from information_schema."""
    cur.execute(
        "SELECT TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH FROM information_schema.tables "
        "WHERE table_schema=%s AND table_name=%s LIMIT 1",
        (database, table),
    )
    row = cur.fetchone()
    if not row:
        return 0, 0
    try:
        approx = int(row[0] or 0)
    except (TypeError, ValueError):
        approx = 0
    try:
        size = int(row[1] or 0) + int(row[2] or 0)
    except (TypeError, ValueError):
        size = 0
    return approx, size


def shrink_heavy_usage_tables_mysql_conn(
    *,
    host: str,
    port: int | str,
    user: str,
    password: str,
    database: str,
    row_threshold: int = HEAVY_USAGE_ROW_THRESHOLD,
    size_threshold: int = HEAVY_USAGE_SIZE_THRESHOLD,
) -> list[tuple[str, int]]:
    """TRUNCATE large usage/history tables so bigint alembic finishes quickly.

    Returns list of (table, approx_rows_before). Empty when nothing was large.
    """
    import pymysql

    truncated: list[tuple[str, int]] = []
    with pymysql.connect(
        host=host,
        port=int(port),
        user=user,
        password=password or "",
        database=database,
        charset="utf8mb4",
        autocommit=True,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            try:
                for table in HEAVY_USAGE_TABLES:
                    if not _mysql_table_exists(cur, database, table):
                        continue
                    approx, size = _mysql_table_bulk_stats(cur, database, table)
                    if approx < int(row_threshold) and size < int(size_threshold):
                        continue
                    cur.execute(f"TRUNCATE TABLE `{_ident(table)}`")
                    truncated.append((table, approx if approx > 0 else size))
            finally:
                cur.execute("SET FOREIGN_KEY_CHECKS=1")
    return truncated


def shrink_heavy_usage_tables_sqlite(
    sqlite_path: str | Path,
    row_threshold: int = HEAVY_USAGE_ROW_THRESHOLD,
) -> list[tuple[str, int]]:
    path = Path(sqlite_path)
    if not path.exists():
        return []
    truncated: list[tuple[str, int]] = []
    db = sqlite3.connect(str(path))
    try:
        for table in HEAVY_USAGE_TABLES:
            if not _sqlite_table_exists(db, table):
                continue
            row = db.execute(f"SELECT COUNT(*) FROM {_ident(table)}").fetchone()
            n = int(row[0] or 0) if row else 0
            if n < int(row_threshold):
                continue
            db.execute(f"DELETE FROM {_ident(table)}")
            truncated.append((table, n))
        if truncated:
            db.commit()
    finally:
        db.close()
    return truncated


def shrink_heavy_usage_tables_on_conn(
    db_type: str,
    conn: dict,
    row_threshold: int = HEAVY_USAGE_ROW_THRESHOLD,
) -> list[tuple[str, int]]:
    db_type = (db_type or "").lower()
    if db_type == "sqlite":
        path = conn.get("sqlite_path") or str(PASARGUARD_DATA / "db.sqlite3")
        return shrink_heavy_usage_tables_sqlite(path, row_threshold=row_threshold)
    if db_type not in ("mysql", "mariadb"):
        return []
    host = conn.get("host") or "127.0.0.1"
    port = migration_port(conn, db_type)
    return shrink_heavy_usage_tables_mysql_conn(
        host=host,
        port=port,
        user=conn.get("user") or "root",
        password=conn.get("password") or "",
        database=conn.get("database") or "pasarguard",
        row_threshold=row_threshold,
    )


def cleanup_orphans_postgres_conn(
    *,
    host: str,
    port: int | str,
    user: str,
    password: str,
    database: str,
) -> tuple[int, int]:
    import psycopg2

    deleted = 0
    nulled = 0
    with psycopg2.connect(
        host=host,
        port=int(port),
        dbname=database,
        user=user,
        password=password or "",
    ) as conn:
        with conn.cursor() as cur:
            for child, child_col, parent, parent_col in ORPHAN_DELETE_SPECS:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name=%s LIMIT 1",
                    (child,),
                )
                if not cur.fetchone():
                    continue
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name=%s LIMIT 1",
                    (parent,),
                )
                if not cur.fetchone():
                    continue
                cur.execute(orphan_delete_sql(child, child_col, parent, parent_col))
                deleted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            for child, child_col, parent, parent_col in ORPHAN_NULL_SPECS:
                cur.execute(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=%s AND column_name=%s LIMIT 1",
                    (child, child_col),
                )
                if not cur.fetchone():
                    continue
                cur.execute(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=%s AND column_name=%s LIMIT 1",
                    (parent, parent_col),
                )
                if not cur.fetchone():
                    continue
                cur.execute(orphan_null_sql(child, child_col, parent, parent_col))
                nulled += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
    return deleted, nulled


def cleanup_orphans_on_conn(db_type: str, conn: dict) -> tuple[int, int]:
    db_type = (db_type or "").lower()
    if db_type == "sqlite":
        path = conn.get("sqlite_path") or str(PASARGUARD_DATA / "db.sqlite3")
        return cleanup_orphans_sqlite(path)
    host = conn.get("host") or "127.0.0.1"
    port = migration_port(conn, db_type)
    password = conn.get("password") or ""
    database = conn.get("database") or "pasarguard"
    if db_type in ("mysql", "mariadb"):
        return cleanup_orphans_mysql_conn(
            host=host,
            port=port,
            user=conn.get("user") or "root",
            password=password,
            database=database,
        )
    if db_type in ("postgresql", "timescaledb"):
        return cleanup_orphans_postgres_conn(
            host=host,
            port=port,
            user=conn.get("user") or "postgres",
            password=password,
            database=database,
        )
    return 0, 0


async def _cleanup_orphans_mysql_via_compose(migrator, conn: dict) -> tuple[int, int]:
    from app.config import PASARGUARD_DIR
    from app.services.pasarguard_ops import resolve_db_service
    from app.services.unique_name_heal import _mysql_compose_query

    svc = resolve_db_service("mysql") or resolve_db_service("mariadb") or "mysql"
    user = conn.get("user") or "root"
    pwd = conn.get("password") or ""
    db = conn.get("database") or "pasarguard"
    host = conn.get("host") or "127.0.0.1"
    cwd = str(PASARGUARD_DIR)

    deleted = 0
    nulled = 0

    async def _table_ok(table: str) -> bool:
        rc, out = await _mysql_compose_query(
            svc=svc, user=user, pwd=pwd, host=host, db=db, cwd=cwd,
            sql=(
                "SELECT COUNT(*) FROM information_schema.tables "
                f"WHERE table_schema='{db}' AND table_name='{table}'"
            ),
            tabular=True,
        )
        last = ""
        for line in out.splitlines():
            if line.strip() and not line.lower().startswith("mysql:"):
                last = line.strip()
        return rc == 0 and last == "1"

    async def _count_orphans(
        child: str, child_col: str, parent: str, parent_col: str,
    ) -> int:
        count_sql = (
            f"SELECT COUNT(*) FROM `{child}` WHERE `{child_col}` IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM `{parent}` "
            f"WHERE `{parent}`.`{parent_col}` = `{child}`.`{child_col}`)"
        )
        rc, cout = await _mysql_compose_query(
            svc=svc, user=user, pwd=pwd, host=host, db=db, cwd=cwd,
            sql=count_sql, tabular=True,
        )
        if rc != 0:
            return 0
        for line in cout.splitlines():
            if line.strip().isdigit():
                return int(line.strip())
        return 0

    for child, child_col, parent, parent_col in ORPHAN_DELETE_SPECS:
        child_i, child_col_i = _ident(child), _ident(child_col)
        parent_i, parent_col_i = _ident(parent), _ident(parent_col)
        if not (await _table_ok(child_i) and await _table_ok(parent_i)):
            continue
        n = await _count_orphans(child_i, child_col_i, parent_i, parent_col_i)
        if n == 0:
            continue
        rc, _ = await _mysql_compose_query(
            svc=svc, user=user, pwd=pwd, host=host, db=db, cwd=cwd,
            sql=orphan_delete_sql(child_i, child_col_i, parent_i, parent_col_i),
        )
        if rc == 0:
            deleted += n

    for child, child_col, parent, parent_col in ORPHAN_NULL_SPECS:
        child_i, child_col_i = _ident(child), _ident(child_col)
        parent_i, parent_col_i = _ident(parent), _ident(parent_col)
        if not (await _table_ok(child_i) and await _table_ok(parent_i)):
            continue
        n = await _count_orphans(child_i, child_col_i, parent_i, parent_col_i)
        if n == 0:
            continue
        # Prefer SET NULL when column is nullable; otherwise DELETE (avoids 1048).
        rc_null, out_null = await _mysql_compose_query(
            svc=svc, user=user, pwd=pwd, host=host, db=db, cwd=cwd,
            sql=(
                "SELECT IS_NULLABLE FROM information_schema.columns "
                f"WHERE table_schema='{db}' AND table_name='{child_i}' "
                f"AND column_name='{child_col_i}' LIMIT 1"
            ),
            tabular=True,
        )
        nullable = True
        if rc_null == 0:
            for line in out_null.splitlines():
                if line.strip().upper() in ("YES", "NO"):
                    nullable = line.strip().upper() == "YES"
                    break
        if nullable:
            rc, _ = await _mysql_compose_query(
                svc=svc, user=user, pwd=pwd, host=host, db=db, cwd=cwd,
                sql=orphan_null_sql(child_i, child_col_i, parent_i, parent_col_i),
            )
            if rc == 0:
                nulled += n
        else:
            rc, _ = await _mysql_compose_query(
                svc=svc, user=user, pwd=pwd, host=host, db=db, cwd=cwd,
                sql=orphan_delete_sql(child_i, child_col_i, parent_i, parent_col_i),
            )
            if rc == 0:
                deleted += n

    return deleted, nulled


async def heal_orphan_fk_refs(migrator) -> tuple[int, int]:
    """Remove/null orphan FK rows on the migration target. Clean DBs → (0, 0)."""
    params = migrator.params or {}
    target_db = (params.get("target_db") or "").lower()
    if target_db not in ("mysql", "mariadb", "postgresql", "timescaledb", "sqlite"):
        return 0, 0

    conn = dict(get_target_connection(params))
    if target_db == "sqlite":
        conn["sqlite_path"] = conn.get("sqlite_path") or str(PASARGUARD_DATA / "db.sqlite3")

    try:
        deleted, nulled = cleanup_orphans_on_conn(target_db, conn)
    except Exception as e:
        migrator.job.log(f"Orphan-FK heal via direct DB failed ({e}); trying compose fallback…")
        deleted, nulled = 0, 0
        if target_db in ("mysql", "mariadb"):
            try:
                deleted, nulled = await _cleanup_orphans_mysql_via_compose(migrator, conn)
            except Exception as e2:
                migrator.job.log(f"Orphan-FK compose heal failed: {e2}")
                return 0, 0
        else:
            return 0, 0

    if deleted or nulled:
        migrator.job.log(
            f"Orphan FK heal: deleted {deleted} row(s), nulled {nulled} ref(s) "
            f"(node_usages/node_user_usages/…)"
        )
    else:
        migrator.job.log("Orphan FK heal: no orphan references found")
    return deleted, nulled


async def heal_heavy_usage_tables(migrator) -> list[tuple[str, int]]:
    """Truncate large usage tables before panel alembic (no-op when small/clean)."""
    params = migrator.params or {}
    target_db = (params.get("target_db") or "").lower()
    if target_db not in ("mysql", "mariadb", "sqlite"):
        return []
    conn = dict(get_target_connection(params))
    if target_db == "sqlite":
        conn["sqlite_path"] = conn.get("sqlite_path") or str(PASARGUARD_DATA / "db.sqlite3")
    try:
        truncated = shrink_heavy_usage_tables_on_conn(target_db, conn)
    except Exception as e:
        migrator.job.log(f"Heavy-usage shrink note: {e}")
        return []
    for table, rows in truncated:
        migrator.job.log(
            f"Truncated large usage table `{table}` (~{rows} rows) before panel "
            "alembic — avoids multi-hour MySQL copy-to-tmp on bigint/FK rebuild; "
            "usage history refills from live traffic"
        )
    if truncated:
        migrator.job.set_progress(
            max(getattr(migrator.job, "progress", 0) or 0, 68),
            "Cleared large usage tables for faster schema upgrade…",
        )
    return truncated


async def heal_marzban_preboot(migrator) -> dict[str, int]:
    """Run all safe Marzban pre-boot heals. No-op on clean small dumps.

    Order matters for large installs: truncate heavy usage tables *before*
    orphan FK cleanup. Otherwise NOT EXISTS deletes scan millions of
    node_user_usages rows that we were about to drop anyway.
    """
    truncated = await heal_heavy_usage_tables(migrator)
    renamed = await heal_duplicate_unique_names(migrator)
    deleted, nulled = await heal_orphan_fk_refs(migrator)
    return {
        "renamed": int(renamed or 0),
        "orphans_deleted": int(deleted or 0),
        "orphans_nulled": int(nulled or 0),
        "usage_tables_truncated": int(len(truncated or [])),
        "usage_rows_cleared": int(sum(n for _, n in (truncated or []))),
    }


def logs_indicate_marzban_preboot_issue(logs: str) -> bool:
    return logs_indicate_duplicate_unique_name(logs) or logs_indicate_orphan_fk(logs)
