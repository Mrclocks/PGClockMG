"""Unified cross-DB migration router for all source/target database pairs."""

from __future__ import annotations

import asyncio

from app.config import PASARGUARD_DIR
from app.services.db_credentials import get_target_connection, migration_port
from app.services.pasarguard_ops import (
    docker_compose_up,
    resolve_db_service,
    _wait_db_service,
    _is_duplicate_schema_error,
    _heal_alembic_duplicate_schema,
    read_target_alembic_version,
)
from app.services.native_migration.host_alembic import run_host_alembic
from app.services.native_migration.source_version import resolve_source_alembic_version
from app.services.native_migration.sqlite_pg import copy_sqlite_to_postgres
from app.services.native_migration.sqlite_mysql import copy_sqlite_to_mysql

# Native engine: direct Python copy (no db-migrations subprocess).
NATIVE_SQLITE_TARGETS = frozenset({"postgresql", "timescaledb", "mysql", "mariadb"})

# Hybrid: host-network schema + db-migrations data import.
HYBRID_SUPPORTED = frozenset({
    ("mysql", "postgresql"),
    ("mysql", "timescaledb"),
    ("mariadb", "postgresql"),
    ("mariadb", "timescaledb"),
    ("mysql", "mysql"),
    ("mysql", "mariadb"),
    ("mariadb", "mysql"),
    ("mariadb", "mariadb"),
    ("postgresql", "postgresql"),
    ("postgresql", "timescaledb"),
    ("postgresql", "mysql"),
    ("postgresql", "mariadb"),
    ("timescaledb", "postgresql"),
    ("timescaledb", "timescaledb"),
    ("timescaledb", "mysql"),
    ("timescaledb", "mariadb"),
    ("sqlite", "sqlite"),
})


def migration_strategy(source_db: str, target_db: str) -> str:
    if source_db == target_db:
        return "same_db"
    if source_db == "sqlite" and target_db in NATIVE_SQLITE_TARGETS:
        return "native_sqlite"
    if (source_db, target_db) in HYBRID_SUPPORTED:
        return "hybrid"
    return "unsupported"


def _target_dsn(params: dict) -> dict:
    conn = get_target_connection(params)
    target_db = params["target_db"]
    default_user = "postgres" if target_db in ("postgresql", "timescaledb") else "root"
    return {
        "host": conn.get("host") or "127.0.0.1",
        "port": migration_port(conn, target_db),
        "database": conn.get("database") or "pasarguard",
        "user": conn.get("user") or default_user,
        "password": conn.get("password") or "",
    }


async def _ensure_target_db_running(migrator, target_db: str) -> None:
    if target_db == "sqlite":
        return
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


async def _copy_data_native(
    migrator, source_path: str, source_db: str, target_db: str, source_version: str,
) -> dict:
    dsn = _target_dsn(migrator.params)
    migrator.job.log(
        f"Native copy: {source_db} → {target_db} "
        f"({dsn['user']}@{dsn['host']}:{dsn['port']}/{dsn['database']})"
    )
    if target_db in ("postgresql", "timescaledb"):
        return await asyncio.to_thread(
            copy_sqlite_to_postgres, source_path, dsn, migrator.job.log, source_version,
        )
    if target_db in ("mysql", "mariadb"):
        return await asyncio.to_thread(
            copy_sqlite_to_mysql, source_path, dsn, migrator.job.log, source_version,
        )
    raise RuntimeError(f"Native SQLite copy not implemented for target {target_db}")


async def _copy_data_hybrid(
    migrator, source_path: str, source_db: str, target_db: str,
) -> None:
    from app.services.db_migration import run_db_migration
    migrator.job.log(
        f"Hybrid migration: schema ready — importing data via db-migrations "
        f"({source_db} → {target_db})"
    )
    await run_db_migration(migrator, source_path, source_db, target_db)


async def run_cross_db_migration(
    migrator,
    source_path: str,
    source_db: str,
    target_db: str,
) -> None:
    """Route cross-DB migration to native or hybrid engine."""
    strategy = migration_strategy(source_db, target_db)
    if strategy == "unsupported":
        raise RuntimeError(
            f"Unsupported cross-DB migration: {source_db} → {target_db}. "
            "Choose a supported target database in the wizard."
        )
    if strategy == "same_db":
        raise RuntimeError("same_db should not use cross-DB migrator")

    migrator.job.log(f"Cross-DB strategy: {strategy} ({source_db} → {target_db})")

    source_version = await resolve_source_alembic_version(migrator, source_db, source_path)
    if not source_version:
        raise RuntimeError(
            "Could not read alembic_version from source — "
            "backup may be corrupt or from an unsupported panel version."
        )
    migrator.job.log(f"Source alembic version: {source_version}")

    await _ensure_target_db_running(migrator, target_db)
    await _stop_panel(migrator)
    await _bootstrap_schema(migrator, source_version, target_db)

    if strategy == "native_sqlite":
        stats = await _copy_data_native(
            migrator, source_path, source_db, target_db, source_version,
        )
        total = sum(stats.values())
        migrator.job.log(f"Native data copy: {total} rows / {len(stats)} tables")
    else:
        await _copy_data_hybrid(migrator, source_path, source_db, target_db)

    await _upgrade_to_head(migrator, target_db)
    final = await read_target_alembic_version(migrator, target_db)
    migrator.job.log(f"Target alembic after migration: {final or '(unknown)'}")


# Backward-compatible alias
run_native_cross_db_migration = run_cross_db_migration
