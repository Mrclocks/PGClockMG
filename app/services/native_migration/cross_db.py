"""Unified cross-DB migration — any source engine to any target engine."""

from __future__ import annotations

from pathlib import Path

from app.config import PASARGUARD_DIR, PASARGUARD_DATA
from app.services.db_credentials import get_source_connection, get_target_connection
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
from app.services.native_migration.sql_staging import import_sql_dump_to_live_db
from app.services.native_migration.universal_copy import copy_database_universal

SUPPORTED_ENGINES = frozenset({
    "sqlite", "mysql", "mariadb", "postgresql", "timescaledb",
})


def migration_strategy(source_db: str, target_db: str) -> str:
    if source_db == target_db:
        return "same_db"
    if source_db not in SUPPORTED_ENGINES or target_db not in SUPPORTED_ENGINES:
        return "unsupported"
    return "universal"


async def _ensure_target_db_running(migrator, target_db: str) -> None:
    if target_db == "sqlite":
        PASARGUARD_DATA.mkdir(parents=True, exist_ok=True)
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


async def _prepare_source_connection(
    migrator, source_db: str, source_path: str,
) -> dict | None:
    """If source is a .sql dump, import into live staging DB and return its DSN."""
    path = Path(source_path)
    if path.suffix.lower() != ".sql":
        return None
    if source_db == "sqlite":
        raise RuntimeError("SQLite source cannot be a .sql dump — upload db.sqlite3")
    conn = get_source_connection(migrator.params)
    if not conn.get("password"):
        tgt = get_target_connection(migrator.params)
        conn["password"] = tgt.get("password")
        conn["user"] = conn.get("user") or tgt.get("user")
        conn["database"] = conn.get("database") or tgt.get("database")
    return await import_sql_dump_to_live_db(migrator, source_path, source_db, conn)


async def run_cross_db_migration(
    migrator,
    source_path: str,
    source_db: str,
    target_db: str,
) -> None:
    """Migrate data from any supported source DB to any supported target DB."""
    strategy = migration_strategy(source_db, target_db)
    if strategy == "unsupported":
        raise RuntimeError(
            f"Unsupported cross-DB migration: {source_db} → {target_db}"
        )
    if strategy == "same_db":
        raise RuntimeError("same_db should not use cross-DB migrator")

    migrator.job.log(f"Universal cross-DB: {source_db} → {target_db}")

    source_version = await resolve_source_alembic_version(migrator, source_db, source_path)
    if not source_version:
        raise RuntimeError(
            "Could not read alembic_version from source — "
            "backup may be corrupt or from an unsupported panel version."
        )
    migrator.job.log(f"Source alembic version: {source_version}")

    staging_conn = await _prepare_source_connection(migrator, source_db, source_path)

    try:
        await _ensure_target_db_running(migrator, target_db)
        await _stop_panel(migrator)
        await _bootstrap_schema(migrator, source_version, target_db)

        stats = await copy_database_universal(
            migrator,
            source_path,
            source_db,
            target_db,
            source_version,
            staging_conn=staging_conn,
        )

        await _upgrade_to_head(migrator, target_db)
        final = await read_target_alembic_version(migrator, target_db)
        migrator.job.log(f"Target alembic after migration: {final or '(unknown)'}")

        users = stats.get("users", 0)
        admins = stats.get("admins", 0)
        if users == 0 and admins == 0:
            migrator.job.log(
                "Warning: no users/admins copied — check source backup and credentials"
            )
    finally:
        container = (staging_conn or {}).get("_ephemeral_container")
        if container:
            migrator.job.log(f"Removing staging container {container}...")
            await migrator._run_cmd(["docker", "rm", "-f", container], timeout=30)


run_native_cross_db_migration = run_cross_db_migration
