"""Two-phase cross-DB migration — intermediate@head → target@head.

Never bootstraps alembic to a source revision (that path was the main failure mode).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app.config import PASARGUARD_DIR, PASARGUARD_DATA, BACKUP_DIR
from app.services.db_credentials import get_source_connection, get_target_connection
from app.services.pasarguard_ops import (
    docker_compose_up,
    resolve_db_service,
    _wait_db_service,
    build_sqlite_alembic_url,
    build_alembic_url_from_conn,
    build_local_alembic_url,
    run_alembic_upgrade_head,
    read_target_alembic_version,
)
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
    # Never convert non-SQLite → SQLite (destination must already be a server DB)
    if target_db == "sqlite" and source_db != "sqlite":
        return "unsupported"
    return "two_phase"


async def _ensure_db_running(migrator, db_type: str) -> None:
    if db_type == "sqlite":
        PASARGUARD_DATA.mkdir(parents=True, exist_ok=True)
        return
    service = resolve_db_service(db_type)
    if service:
        migrator.job.log(f"Starting database service {service}...")
        await docker_compose_up(migrator, [service])
        await _wait_db_service(migrator, db_type, service)


async def _stop_panel(migrator) -> None:
    await migrator._run_cmd(
        ["docker", "compose", "stop", "pasarguard"],
        cwd=str(PASARGUARD_DIR),
        timeout=120,
    )


async def _flush_pg_type_caches(migrator, target_db: str) -> None:
    """After DROP SCHEMA CASCADE, PgBouncer keeps stale enum OIDs.

    Only restart pgbouncer — restarting TimescaleDB/PostgreSQL is unnecessary
    and produces scary benign FATAL lines in logs.
    """
    import asyncio
    import re

    if target_db not in ("postgresql", "timescaledb"):
        return
    cwd = str(PASARGUARD_DIR)
    from app.services.pasarguard_ops import _compose_text

    text = _compose_text()
    if re.search(r"^\s*pgbouncer\s*:", text, re.M):
        migrator.job.log("Restarting pgbouncer to clear PG type/OID cache...")
        await migrator._run_cmd(
            ["docker", "compose", "restart", "pgbouncer"],
            cwd=cwd,
            timeout=120,
        )
        await asyncio.sleep(5)
    service = resolve_db_service(target_db)
    if service:
        await _wait_db_service(migrator, target_db, service)


async def _reset_target_schema(migrator, target_db: str) -> None:
    """Wipe target so alembic upgrade head creates a clean head schema."""
    import asyncio

    if target_db == "sqlite":
        path = PASARGUARD_DATA / "db.sqlite3"
        # Keep intermediate data on a side copy if it is the current file
        if path.exists():
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            side = BACKUP_DIR / "phase2-intermediate.sqlite3"
            shutil.copy2(path, side)
            path.unlink()
            migrator.job.log(f"Moved intermediate SQLite aside → {side}")
            migrator._phase2_sqlite_intermediate = str(side)
        return

    conn = get_target_connection(migrator.params)
    service = resolve_db_service(target_db)
    if not service:
        migrator.job.log("No compose DB service — skipping schema wipe")
        return

    user = conn.get("user") or (
        "postgres" if target_db in ("postgresql", "timescaledb") else "root"
    )
    pwd = conn.get("password") or ""
    db = conn.get("database") or "pasarguard"
    cwd = str(PASARGUARD_DIR)
    migrator.job.log(f"Resetting target schema on {service}/{db}...")

    if target_db in ("postgresql", "timescaledb"):
        sql = (
            "DROP SCHEMA IF EXISTS public CASCADE; "
            "CREATE SCHEMA public; "
            f"GRANT ALL ON SCHEMA public TO \"{user}\"; "
            "GRANT ALL ON SCHEMA public TO public;"
        )
        cmd = (
            f'cd "{cwd}" && docker compose exec -T {service} '
            f'env PGPASSWORD="{pwd}" psql -U {user} -d {db} -c "{sql}"'
        )
    else:
        cmd = (
            f'cd "{cwd}" && docker compose exec -T {service} '
            f'mysql -u {user} -p"{pwd}" -e '
            f'"DROP DATABASE IF EXISTS `{db}`; CREATE DATABASE `{db}`;"'
        )

    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    await proc.wait()
    if proc.returncode != 0:
        migrator.job.log("Warning: target schema reset returned non-zero — continuing")


async def _phase1_land_intermediate(
    migrator,
    source_path: str,
    source_db: str,
) -> tuple[str, str, dict | None]:
    """Land source data on an intermediate DB and upgrade it to alembic head.

    Returns (intermediate_path, intermediate_db, staging_conn).
    """
    path = Path(source_path)
    staging_conn: dict | None = None

    if source_db == "sqlite":
        if not path.exists():
            raise RuntimeError(f"SQLite source not found: {source_path}")
        dest = PASARGUARD_DATA / "db.sqlite3"
        PASARGUARD_DATA.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.resolve() != path.resolve():
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, BACKUP_DIR / "pre-phase1-db.sqlite3")
        if dest.resolve() != path.resolve():
            shutil.copy2(path, dest)
        migrator.job.log(f"Phase1: intermediate SQLite at {dest}")
        url = build_sqlite_alembic_url(dest)
        await run_alembic_upgrade_head(migrator, url_override=url, heal_db="sqlite")
        return str(dest), "sqlite", None

    # MySQL/MariaDB/PG source: .sql dump → staging, or live connection
    if path.suffix.lower() == ".sql":
        conn = get_source_connection(migrator.params)
        if not conn.get("password"):
            tgt = get_target_connection(migrator.params)
            conn["password"] = tgt.get("password")
            conn["user"] = conn.get("user") or tgt.get("user")
            conn["database"] = conn.get("database") or tgt.get("database")
        staging_conn = await import_sql_dump_to_live_db(
            migrator, source_path, source_db, conn,
        )
        url = build_alembic_url_from_conn(source_db, staging_conn)
        await run_alembic_upgrade_head(
            migrator, url_override=url, heal_db=source_db,
        )
        return source_path, source_db, staging_conn

    # Live non-sqlite source (credentials from wizard)
    conn = get_source_connection(migrator.params)
    url = build_alembic_url_from_conn(source_db, conn)
    await run_alembic_upgrade_head(migrator, url_override=url, heal_db=source_db)
    return source_path, source_db, None


async def _prepare_target_for_migration(migrator, target_db: str) -> None:
    """Ensure target DB is up, credentials verified, and role passwords aligned."""
    from app.services.db_auth import (
        migration_params_from_connection,
        resolve_live_admin_connection,
        sync_postgres_roles_to_app_password,
    )

    await _ensure_db_running(migrator, target_db)
    admin = await resolve_live_admin_connection(migrator, target_db)
    migrator.params = migration_params_from_connection(
        migrator.params.get("source_db") or migrator.params.get("source_db_type") or "sqlite",
        target_db,
        admin,
    )
    migrator.params["_auto_db_credentials"] = True
    if target_db in ("postgresql", "timescaledb"):
        await sync_postgres_roles_to_app_password(migrator, target_db, admin)


async def run_two_phase_migration(
    migrator,
    source_path: str,
    source_db: str,
    target_db: str,
) -> dict[str, int]:
    """Migrate any supported source → any supported target via head→head copy."""
    strategy = migration_strategy(source_db, target_db)
    if strategy == "unsupported":
        raise RuntimeError(f"Unsupported cross-DB migration: {source_db} → {target_db}")
    if strategy == "same_db":
        raise RuntimeError("same_db should not use two-phase migrator")

    migrator.job.log(f"Two-phase migration: {source_db} → {target_db}")
    await _stop_panel(migrator)

    staging_conn: dict | None = None
    try:
        # Phase 1 — land + upgrade intermediate to head
        migrator.job.set_progress(45, "Phase 1: intermediate DB → alembic head...")
        inter_path, inter_db, staging_conn = await _phase1_land_intermediate(
            migrator, source_path, source_db,
        )

        if inter_db == target_db and target_db == "sqlite":
            migrator.job.log("Phase1 complete — target is SQLite intermediate; skip Phase2")
            return {"users": -1, "admins": -1}  # counts unknown; same-file path

        # Sanity: refuse to wipe target if intermediate source looks empty for key tables.
        # This prevents the classic "DROP SCHEMA then copy nothing" empty-panel outcome.
        try:
            from app.services.native_migration.adapters import (
                create_reader,
                _count_source_rows,
            )

            pre_reader = create_reader(
                inter_db,
                inter_path if inter_db == "sqlite" else None,
                staging_conn or get_source_connection(migrator.params),
            )
            try:
                critical = ("users", "admins", "hosts", "inbounds", "nodes", "groups")
                pre_counts = {}
                for t in critical:
                    n = _count_source_rows(pre_reader, t)
                    if n > 0:
                        pre_counts[t] = n
                if pre_counts:
                    migrator.job.log(
                        "Phase1 source ready: "
                        + ", ".join(f"{k}={v}" for k, v in pre_counts.items())
                    )
                else:
                    migrator.job.log(
                        "WARNING: Phase1 intermediate has 0 rows in users/admins/hosts/"
                        "inbounds/nodes/groups — convert may produce an empty panel"
                    )
            finally:
                pre_reader.close()
        except Exception as e:
            migrator.job.log(f"Phase1 pre-count note: {e}")

        # Phase 2 — empty target at head, then copy head→head
        migrator.job.set_progress(60, f"Phase 2: create {target_db} schema at head...")
        await _prepare_target_for_migration(migrator, target_db)
        await _reset_target_schema(migrator, target_db)
        if target_db in ("postgresql", "timescaledb"):
            from app.services.db_auth import sync_postgres_roles_to_app_password

            await sync_postgres_roles_to_app_password(
                migrator, target_db, get_target_connection(migrator.params),
            )
        await run_alembic_upgrade_head(
            migrator,
            url_override=build_local_alembic_url(migrator.params),
            heal_db=target_db,
        )

        migrator.job.set_progress(75, f"Phase 2: copy {inter_db} → {target_db}...")
        stats = await copy_database_universal(
            migrator,
            inter_path,
            inter_db,
            target_db,
            "head",
            staging_conn=staging_conn,
            fail_hard=True,
            stamp_alembic=False,
        )
        # Pin alembic_version to head so panel does not re-run old migrations over data
        try:
            from app.services.pasarguard_ops import get_alembic_head_revision, set_target_alembic_version

            head = await get_alembic_head_revision(migrator)
            if head:
                if await set_target_alembic_version(migrator, target_db, head):
                    migrator.job.log(f"Pinned alembic_version to head ({head}) after data copy")
                else:
                    migrator.job.log("Warning: could not pin alembic_version after copy")
        except Exception as e:
            migrator.job.log(f"Alembic pin note: {e}")
        migrator.job.log(
            f"Two-phase done: users={stats.get('users', 0)} admins={stats.get('admins', 0)} "
            f"hosts={stats.get('hosts', 0)} groups={stats.get('groups', 0)} nodes={stats.get('nodes', 0)}"
        )
        await _flush_pg_type_caches(migrator, target_db)
        migrator.copy_stats = stats
        migrator.copy_report = getattr(migrator, "copy_report", None) or {}
        return stats
    finally:
        container = (staging_conn or {}).get("_ephemeral_container")
        if container:
            migrator.job.log(f"Removing staging container {container}...")
            await migrator._run_cmd(["docker", "rm", "-f", container], timeout=30)


async def run_cross_db_migration(
    migrator,
    source_path: str,
    source_db: str,
    target_db: str,
) -> None:
    """Public entry — always uses two-phase engine."""
    await run_two_phase_migration(migrator, source_path, source_db, target_db)


run_native_cross_db_migration = run_cross_db_migration
