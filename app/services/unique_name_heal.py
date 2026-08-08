"""Heal duplicate unique-name rows before PasarGuard alembic/panel boot.

Marzban dumps can contain duplicate ``nodes.name`` (and similar) values that
PasarGuard later rejects with MySQL 1062 / unique violations during upgrade.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from app.config import PASARGUARD_DATA
from app.services.db_credentials import get_target_connection, migration_port

# Tables/columns where PasarGuard (or its alembic chain) may enforce uniqueness.
UNIQUE_NAME_TARGETS: tuple[tuple[str, str], ...] = (
    ("nodes", "name"),
    ("user_templates", "name"),
)

_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _require_ident(name: str) -> str:
    if not _SAFE_IDENT.match(name or ""):
        raise RuntimeError(f"Invalid SQL identifier: {name!r}")
    return name


def plan_duplicate_name_renames(
    rows: Iterable[tuple[Any, str | None]],
    *,
    case_insensitive: bool = False,
    empty_prefix: str = "unnamed",
) -> list[tuple[Any, str, str]]:
    """Keep lowest id per name group; rename the rest to ``{base}-{id}``.

    Returns list of ``(id, old_name, new_name)``. Collision-safe against both
    existing names and names produced in this pass.
    """
    normalized: list[tuple[Any, str]] = []
    for row_id, raw_name in rows:
        name = "" if raw_name is None else str(raw_name)
        normalized.append((row_id, name))

    def key_for(name: str) -> str:
        return name.casefold() if case_insensitive else name

    taken: set[str] = set()
    for _, name in normalized:
        taken.add(key_for(name))

    groups: dict[str, list[tuple[Any, str]]] = {}
    for row_id, name in normalized:
        groups.setdefault(key_for(name), []).append((row_id, name))

    renames: list[tuple[Any, str, str]] = []
    for _, members in groups.items():
        if len(members) < 2:
            continue
        members_sorted = sorted(members, key=lambda m: (m[0] is None, m[0]))
        # Keep the first (lowest id); rename the rest.
        for row_id, old_name in members_sorted[1:]:
            base = old_name.strip() if old_name.strip() else empty_prefix
            candidate = f"{base}-{row_id}"
            n = 2
            while key_for(candidate) in taken:
                candidate = f"{base}-{row_id}-{n}"
                n += 1
            taken.add(key_for(candidate))
            renames.append((row_id, old_name, candidate))
    return renames


def logs_indicate_duplicate_unique_name(logs: str) -> bool:
    """True when panel/alembic logs show a unique-name collision we can heal."""
    text = logs or ""
    low = text.lower()
    if not any(
        s in low
        for s in (
            "duplicate entry",
            "uniqueconstraint",
            "unique violation",
            "duplicate key",
            "integrityerror",
        )
    ):
        return False
    return any(
        s in low
        for s in (
            "nodes.name",
            "uq_nodes_name",
            "user_templates.name",
            "uq_user_templates_name",
            "for key 'nodes.",
            'for key "nodes',
            "for key 'user_templates.",
            'for key "user_templates',
            "nodes_name",
            "user_templates_name",
        )
    )


def _table_exists_sqlite(db: sqlite3.Connection, table: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return bool(row)


def dedupe_unique_names_sqlite(sqlite_path: str | Path) -> int:
    """Dedupe UNIQUE_NAME_TARGETS in a SQLite file. Returns rename count."""
    path = Path(sqlite_path)
    if not path.exists():
        return 0
    total = 0
    db = sqlite3.connect(str(path))
    try:
        for table, column in UNIQUE_NAME_TARGETS:
            table = _require_ident(table)
            column = _require_ident(column)
            if not _table_exists_sqlite(db, table):
                continue
            rows = db.execute(f'SELECT id, "{column}" FROM "{table}"').fetchall()
            renames = plan_duplicate_name_renames(rows, case_insensitive=False)
            for row_id, _old, new_name in renames:
                db.execute(
                    f'UPDATE "{table}" SET "{column}" = ? WHERE id = ?',
                    (new_name, row_id),
                )
                total += 1
        if total:
            db.commit()
    finally:
        db.close()
    return total


def dedupe_unique_names_mysql_conn(
    *,
    host: str,
    port: int | str,
    user: str,
    password: str,
    database: str,
) -> int:
    import pymysql

    total = 0
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
            for table, column in UNIQUE_NAME_TARGETS:
                table = _require_ident(table)
                column = _require_ident(column)
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema=%s AND table_name=%s LIMIT 1",
                    (database, table),
                )
                if not cur.fetchone():
                    continue
                cur.execute(f"SELECT `id`, `{column}` FROM `{table}`")
                rows = cur.fetchall()
                # MySQL unique indexes are usually case-insensitive (utf8mb4_*_ci).
                renames = plan_duplicate_name_renames(rows, case_insensitive=True)
                for row_id, _old, new_name in renames:
                    cur.execute(
                        f"UPDATE `{table}` SET `{column}`=%s WHERE `id`=%s",
                        (new_name, row_id),
                    )
                    total += 1
    return total


def dedupe_unique_names_postgres_conn(
    *,
    host: str,
    port: int | str,
    user: str,
    password: str,
    database: str,
) -> int:
    import psycopg2

    total = 0
    with psycopg2.connect(
        host=host,
        port=int(port),
        dbname=database,
        user=user,
        password=password or "",
    ) as conn:
        with conn.cursor() as cur:
            for table, column in UNIQUE_NAME_TARGETS:
                table = _require_ident(table)
                column = _require_ident(column)
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name=%s LIMIT 1",
                    (table,),
                )
                if not cur.fetchone():
                    continue
                cur.execute(f'SELECT id, "{column}" FROM "{table}"')
                rows = cur.fetchall()
                renames = plan_duplicate_name_renames(rows, case_insensitive=False)
                for row_id, _old, new_name in renames:
                    cur.execute(
                        f'UPDATE "{table}" SET "{column}"=%s WHERE id=%s',
                        (new_name, row_id),
                    )
                    total += 1
        conn.commit()
    return total


def dedupe_unique_names_on_conn(db_type: str, conn: dict) -> int:
    """Dedupe on a concrete connection dict. Returns number of renamed rows."""
    db_type = (db_type or "").lower()
    if db_type == "sqlite":
        path = conn.get("sqlite_path") or str(PASARGUARD_DATA / "db.sqlite3")
        return dedupe_unique_names_sqlite(path)

    host = conn.get("host") or "127.0.0.1"
    port = migration_port(conn, db_type)
    password = conn.get("password") or ""
    database = conn.get("database") or "pasarguard"

    if db_type in ("mysql", "mariadb"):
        user = conn.get("user") or "root"
        return dedupe_unique_names_mysql_conn(
            host=host, port=port, user=user, password=password, database=database,
        )
    if db_type in ("postgresql", "timescaledb"):
        user = conn.get("user") or "postgres"
        return dedupe_unique_names_postgres_conn(
            host=host, port=port, user=user, password=password, database=database,
        )
    return 0


async def _mysql_compose_query(
    *,
    svc: str,
    user: str,
    pwd: str,
    host: str,
    db: str,
    cwd: str,
    sql: str,
    tabular: bool = False,
) -> tuple[int, str]:
    import asyncio

    cmd = [
        "docker", "compose", "exec", "-T", svc,
        "mysql", "-u", user, f"-p{pwd}", "-h", host, db,
    ]
    if tabular:
        cmd.append("-N")
    cmd.extend(["-e", sql])
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out_b, _ = await proc.communicate()
    return proc.returncode or 0, (out_b or b"").decode("utf-8", errors="replace")


async def _dedupe_mysql_via_compose(migrator, conn: dict) -> int:
    """Fallback when host:port pymysql cannot reach the compose MySQL."""
    from app.config import PASARGUARD_DIR
    from app.services.pasarguard_ops import resolve_db_service

    svc = resolve_db_service("mysql") or resolve_db_service("mariadb") or "mysql"
    user = conn.get("user") or "root"
    pwd = conn.get("password") or ""
    db = conn.get("database") or "pasarguard"
    host = conn.get("host") or "127.0.0.1"
    cwd = str(PASARGUARD_DIR)

    total = 0
    for table, column in UNIQUE_NAME_TARGETS:
        table = _require_ident(table)
        column = _require_ident(column)
        rc, probe_out = await _mysql_compose_query(
            svc=svc, user=user, pwd=pwd, host=host, db=db, cwd=cwd,
            sql=(
                "SELECT COUNT(*) FROM information_schema.tables "
                f"WHERE table_schema='{db}' AND table_name='{table}'"
            ),
            tabular=True,
        )
        last = ""
        for line in probe_out.splitlines():
            if line.strip() and not line.lower().startswith("mysql:"):
                last = line.strip()
        if rc != 0 or last != "1":
            continue

        rc, text = await _mysql_compose_query(
            svc=svc, user=user, pwd=pwd, host=host, db=db, cwd=cwd,
            sql=f"SELECT id, `{column}` FROM `{table}`",
            tabular=True,
        )
        if rc != 0:
            continue
        rows: list[tuple[Any, str | None]] = []
        for line in text.splitlines():
            if not line.strip() or line.lower().startswith("mysql:"):
                continue
            if "\t" not in line:
                continue
            rid_s, name = line.split("\t", 1)
            try:
                rid: Any = int(rid_s)
            except ValueError:
                rid = rid_s
            rows.append((rid, None if name == "NULL" else name))
        renames = plan_duplicate_name_renames(rows, case_insensitive=True)
        for row_id, _old, new_name in renames:
            lit = new_name.replace("\\", "\\\\").replace("'", "''")
            rc, _ = await _mysql_compose_query(
                svc=svc, user=user, pwd=pwd, host=host, db=db, cwd=cwd,
                sql=f"UPDATE `{table}` SET `{column}`='{lit}' WHERE id={int(row_id)}",
            )
            if rc == 0:
                total += 1
    return total


async def heal_duplicate_unique_names(migrator) -> int:
    """Dedupe unique-name collisions on the migration target. Returns rename count."""
    params = migrator.params or {}
    target_db = (params.get("target_db") or "").lower()
    if target_db not in ("mysql", "mariadb", "postgresql", "timescaledb", "sqlite"):
        return 0

    conn = dict(get_target_connection(params))
    if target_db == "sqlite":
        conn["sqlite_path"] = conn.get("sqlite_path") or str(PASARGUARD_DATA / "db.sqlite3")

    try:
        renamed = dedupe_unique_names_on_conn(target_db, conn)
    except Exception as e:
        migrator.job.log(f"Unique-name heal via direct DB failed ({e}); trying compose fallback…")
        renamed = 0
        if target_db in ("mysql", "mariadb"):
            try:
                renamed = await _dedupe_mysql_via_compose(migrator, conn)
            except Exception as e2:
                migrator.job.log(f"Unique-name compose heal failed: {e2}")
                return 0
        else:
            return 0

    if renamed:
        migrator.job.log(
            f"Renamed {renamed} duplicate unique-name row(s) "
            f"(nodes/user_templates) so PasarGuard alembic can proceed"
        )
    else:
        migrator.job.log("Unique-name heal: no duplicate name rows found")
    return renamed
