"""Orchestrate native cross-DB migration: host alembic + direct data copy."""

from __future__ import annotations

import asyncio

from app.services.db_credentials import get_target_connection, migration_port
from app.services.pasarguard_ops import (
    read_sqlite_alembic_version,
    docker_compose_up,
    resolve_db_service,
    _wait_db_service,
    _is_duplicate_schema_error,
    _heal_alembic_duplicate_schema,
    read_target_alembic_version,
)
from app.config import PASARGUARD_DIR
from app.services.native_migration.host_alembic import run_host_alembic
from app.services.native_migration.sqlite_pg import copy_sqlite_to_postgres


async def _ensure_target_db_running(migrator, target_db: str) -> None:
    service = resolve_db_service(target_db)
    if service:
        migrator.job.log(f"Starting database service {service}...")
        await docker_compose_up(migrator, [service])
        await _wait_db_service(migrator, target_db, service)


async def _stop_panel(migrator) -> None:
    await migrator._run_cmd(
        ["docker", "compose", "stop", "pasarguard"],
        cwd=str(PASARGUARD_DIR),
        timeout=120,
    )


async def _bootstrap_schema(migrator, revision: str, target_db: str) -> None:
    migrator.job.log(f"Creating target schema (alembic {revision})...")
    ok, out = await run_host_alembic(migrator, "upgrade", revision)
    if ok:
        return
    if _is_duplicate_schema_error(out or ""):
        migrator.job.log("Schema partially exists — healing alembic_version...")
        if await _heal_alembic_duplicate_schema(migrator, target_db, out or ""):
            return
    raise RuntimeError(
        f"Failed to create target schema at revision {revision}:\n{(out or '')[-3000:]}"
    )


async def _upgrade_to_head(migrator, target_db: str) -> None:
    migrator.job.log("Upgrading schema to head...")
    ok, out = await run_host_alembic(migrator, "upgrade", "head")
    if ok:
        return
    if _is_duplicate_schema_error(out or ""):
        if await _heal_alembic_duplicate_schema(migrator, target_db, out or ""):
            ok2, out2 = await run_host_alembic(migrator, "upgrade", "head")
            if ok2:
                return
            out = out2 or out
    raise RuntimeError(
        f"Failed to upgrade target schema to head:\n{(out or '')[-3000:]}"
    )


def _pg_dsn(params: dict) -> dict:
    conn = get_target_connection(params)
    target_db = params["target_db"]
    return {
        "host": conn.get("host") or "127.0.0.1",
        "port": migration_port(conn, target_db),
        "database": conn.get("database") or "pasarguard",
        "user": conn.get("user") or "postgres",
        "password": conn.get("password") or "",
    }


async def run_native_cross_db_migration(
    migrator,
    source_path: str,
    source_db: str,
    target_db: str,
) -> None:
    """
    Native Marzban/PasarGuard cross-DB migration without compose-run alembic
    or the external db-migrations shell tool.
    """
    if source_db != "sqlite":
        raise RuntimeError(
            f"Native migration currently supports SQLite sources only (got {source_db}). "
            "Use same-DB migration or contact support for MySQL sources."
        )
    if target_db not in ("postgresql", "timescaledb"):
        raise RuntimeError(
            f"Native migration target must be PostgreSQL/TimescaleDB (got {target_db})"
        )

    source_version = read_sqlite_alembic_version(source_path)
    if not source_version:
        raise RuntimeError(
            "Source SQLite has no alembic_version — cannot verify schema compatibility"
        )
    migrator.job.log(f"Source alembic version: {source_version}")

    await _ensure_target_db_running(migrator, target_db)
    await _stop_panel(migrator)
    await _bootstrap_schema(migrator, source_version, target_db)

    migrator.job.log("Copying data (native SQLite → PostgreSQL)...")
    dsn = _pg_dsn(migrator.params)
    migrator.job.log(
        f"PostgreSQL: {dsn['user']}@{dsn['host']}:{dsn['port']}/{dsn['database']}"
    )
    stats = await asyncio.to_thread(
        copy_sqlite_to_postgres,
        source_path,
        dsn,
        migrator.job.log,
        source_version,
    )
    total = sum(stats.values())
    migrator.job.log(f"Native data copy complete: {total} rows across {len(stats)} tables")

    await _upgrade_to_head(migrator, target_db)
    final = await read_target_alembic_version(migrator, target_db)
    migrator.job.log(f"Target alembic after migration: {final or '(unknown)'}")
