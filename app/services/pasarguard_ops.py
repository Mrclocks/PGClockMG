"""Non-interactive PasarGuard Docker operations (no hanging CLI)."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from pathlib import Path

from app.config import PASARGUARD_DIR, PASARGUARD_ENV
from app.services.env_migration import read_env_var

STARTUP_MARKERS = (
    "Application startup complete",
    "Uvicorn running",
)

DB_SERVICES = {
    "timescaledb": ("timescaledb", "postgresql"),
    "postgresql": ("postgresql", "timescaledb"),
    "mysql": ("mysql",),
    "mariadb": ("mariadb", "mysql"),
}


def _compose_text() -> str:
    compose = PASARGUARD_DIR / "docker-compose.yml"
    return compose.read_text(encoding="utf-8", errors="ignore") if compose.exists() else ""


def resolve_db_service(target_db: str) -> str | None:
    text = _compose_text()
    for name in DB_SERVICES.get(target_db, (target_db,)):
        if re.search(rf"^\s*{re.escape(name)}\s*:", text, re.MULTILINE):
            return name
    return DB_SERVICES.get(target_db, (None,))[0]


def read_sqlite_alembic_version(sqlite_path: str | Path) -> str | None:
    path = Path(sqlite_path)
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(str(path))
        try:
            cur = conn.execute("SELECT version_num FROM alembic_version LIMIT 1")
            row = cur.fetchone()
            return str(row[0]).strip() if row and row[0] else None
        finally:
            conn.close()
    except Exception:
        return None


async def docker_compose_up(migrator, services: list[str] | None = None) -> bool:
    cwd = str(PASARGUARD_DIR)
    cmd = ["docker", "compose", "up", "-d"]
    if services:
        cmd.extend(services)
    ok, _ = await migrator._run_cmd(cmd, cwd=cwd, timeout=180)
    return ok


async def wait_pasarguard_ready(migrator, max_wait: int = 90) -> bool:
    """Poll container logs until startup completes — never blocks on pasarguard CLI."""
    cwd = str(PASARGUARD_DIR)
    migrator.job.log("Waiting for PasarGuard to become ready...")

    for attempt in range(max(1, max_wait // 3)):
        ok, out = await migrator._run_cmd(
            ["docker", "compose", "logs", "--no-color", "--tail", "80", "pasarguard"],
            cwd=cwd,
            timeout=25,
        )
        combined = out or ""
        if any(marker in combined for marker in STARTUP_MARKERS):
            migrator.job.log("PasarGuard ready")
            return True

        ok_run, running = await migrator._run_cmd(
            ["docker", "compose", "ps", "--status", "running", "-q", "pasarguard"],
            cwd=cwd,
            timeout=15,
        )
        if ok_run and running.strip() and attempt >= 4:
            migrator.job.log("PasarGuard container running — proceeding")
            return True

        await asyncio.sleep(3)

    migrator.job.log("PasarGuard readiness timeout — continuing migration")
    return True


async def start_pasarguard(migrator, wait: bool = True, recreate: bool = False) -> None:
    cwd = str(PASARGUARD_DIR)
    cmd = ["docker", "compose", "up", "-d"]
    if recreate:
        cmd.extend(["--force-recreate", "pasarguard"])
    else:
        cmd.append("pasarguard")
    await migrator._run_cmd(cmd, cwd=cwd, timeout=180)
    if wait:
        await wait_pasarguard_ready(migrator)


async def restart_pasarguard(migrator, wait: bool = True) -> None:
    """Restart via docker compose — avoids `pasarguard restart` streaming logs forever."""
    cwd = str(PASARGUARD_DIR)
    migrator.job.log("Restarting PasarGuard (docker compose)...")
    ok, _ = await migrator._run_cmd(
        ["docker", "compose", "restart", "pasarguard"],
        cwd=cwd,
        timeout=120,
    )
    if not ok:
        await migrator._run_cmd(
            ["docker", "compose", "up", "-d", "--force-recreate", "pasarguard"],
            cwd=cwd,
            timeout=180,
        )
    if wait:
        await wait_pasarguard_ready(migrator, max_wait=60)


async def _wait_db_service(migrator, service: str, password: str | None, attempts: int = 20) -> None:
    cwd = str(PASARGUARD_DIR)
    pwd = password or read_env_var(
        PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore") if PASARGUARD_ENV.exists() else "",
        "POSTGRES_PASSWORD",
    ) or "password"

    for i in range(attempts):
        if service in ("postgresql", "timescaledb"):
            cmd = (
                f'cd "{cwd}" && docker compose exec -T {service} '
                f'env PGPASSWORD="{pwd}" psql -U postgres -d pasarguard -c "SELECT 1"'
            )
        elif service in ("mysql", "mariadb"):
            cmd = (
                f'cd "{cwd}" && docker compose exec -T {service} '
                f'mysqladmin ping -h 127.0.0.1 -u root -p"{pwd}"'
            )
        else:
            return

        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        await proc.wait()
        if proc.returncode == 0:
            migrator.job.log(f"Database service {service} is ready")
            return
        await asyncio.sleep(3)

    migrator.job.log(f"Warning: {service} readiness check timed out — continuing")


async def read_target_alembic_version(migrator, target_db: str, password: str | None) -> str | None:
    if target_db == "sqlite":
        from app.config import PASARGUARD_DATA
        return read_sqlite_alembic_version(PASARGUARD_DATA / "db.sqlite3")

    service = resolve_db_service(target_db)
    if not service:
        return None

    cwd = str(PASARGUARD_DIR)
    pwd = password or read_env_var(
        PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore") if PASARGUARD_ENV.exists() else "",
        "POSTGRES_PASSWORD",
    ) or "password"

    if service in ("postgresql", "timescaledb"):
        cmd = (
            f'cd "{cwd}" && docker compose exec -T {service} '
            f'env PGPASSWORD="{pwd}" psql -U postgres -d pasarguard -tAc '
            f'"SELECT version_num FROM alembic_version LIMIT 1"'
        )
    elif service in ("mysql", "mariadb"):
        cmd = (
            f'cd "{cwd}" && docker compose exec -T {service} '
            f'mysql -u root -p"{pwd}" -h 127.0.0.1 -N -e '
            f'"SELECT version_num FROM pasarguard.alembic_version LIMIT 1"'
        )
    else:
        return None

    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    version = (stdout or b"").decode("utf-8", errors="ignore").strip()
    return version or None


async def run_alembic_upgrade(migrator) -> bool:
    """Try common in-container Alembic commands."""
    cwd = str(PASARGUARD_DIR)
    commands = [
        ["docker", "compose", "exec", "-T", "pasarguard", "alembic", "upgrade", "head"],
        ["docker", "compose", "exec", "-T", "pasarguard", "uv", "run", "alembic", "upgrade", "head"],
        ["docker", "compose", "exec", "-T", "pasarguard", "bash", "-lc", "uv run alembic upgrade head"],
        ["docker", "compose", "exec", "-T", "pasarguard", "bash", "-lc", "cd /code && uv run alembic upgrade head"],
    ]
    for cmd in commands:
        ok, out = await migrator._run_cmd(cmd, cwd=cwd, timeout=300)
        if ok:
            migrator.job.log("Alembic upgrade head completed")
            return True
        if out and "already at head" in out.lower():
            return True
    return False


async def set_target_alembic_version(
    migrator, target_db: str, version: str, password: str | None,
) -> bool:
    """Align target alembic_version with source (required by db-migrations tool)."""
    if not version:
        return False

    service = resolve_db_service(target_db)
    if not service:
        return False

    cwd = str(PASARGUARD_DIR)
    pwd = password or read_env_var(
        PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore") if PASARGUARD_ENV.exists() else "",
        "POSTGRES_PASSWORD",
    ) or "password"

    if service in ("postgresql", "timescaledb"):
        sql = (
            f"DELETE FROM alembic_version; "
            f"INSERT INTO alembic_version (version_num) VALUES ('{version}');"
        )
        cmd = (
            f'cd "{cwd}" && docker compose exec -T {service} '
            f'env PGPASSWORD="{pwd}" psql -U postgres -d pasarguard -c "{sql}"'
        )
    elif service in ("mysql", "mariadb"):
        sql = (
            f"DELETE FROM alembic_version; "
            f"INSERT INTO alembic_version (version_num) VALUES ('{version}');"
        )
        cmd = (
            f'cd "{cwd}" && docker compose exec -T {service} '
            f'mysql -u root -p"{pwd}" -h 127.0.0.1 pasarguard -e "{sql}"'
        )
    else:
        return False

    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    await proc.wait()
    if proc.returncode == 0:
        migrator.job.log(f"Target alembic_version set to {version}")
        return True
    return False


async def ensure_schema_initialized(
    migrator,
    target_db: str,
    password: str | None = None,
    source_sqlite: str | Path | None = None,
) -> str | None:
    """
    Boot PasarGuard on the target DB so Alembic creates schema, then align
    alembic_version with the Marzban source when needed for db-migrations.
    """
    cwd = str(PASARGUARD_DIR)
    source_version = read_sqlite_alembic_version(source_sqlite) if source_sqlite else None
    if source_version:
        migrator.job.log(f"Source Alembic version: {source_version}")

    service = resolve_db_service(target_db) if target_db != "sqlite" else None
    if service:
        migrator.job.log(f"Ensuring DB service {service} is running...")
        await docker_compose_up(migrator, [service])
        await _wait_db_service(migrator, service, password)

    migrator.job.log("Stopping PasarGuard before schema init...")
    await migrator._run_cmd(["docker", "compose", "stop", "pasarguard"], cwd=cwd, timeout=120)

    migrator.job.log("Starting PasarGuard on target database (force recreate)...")
    await start_pasarguard(migrator, wait=True, recreate=True)
    await asyncio.sleep(8)

    target_version = await read_target_alembic_version(migrator, target_db, password)
    if not target_version:
        migrator.job.log("Target schema empty — running Alembic upgrade...")
        await run_alembic_upgrade(migrator)
        await asyncio.sleep(5)
        await start_pasarguard(migrator, wait=True, recreate=True)
        target_version = await read_target_alembic_version(migrator, target_db, password)

    if not target_version:
        raise RuntimeError(
            "Target database has no Alembic schema. "
            "Start PasarGuard manually once on the target DB (PostgreSQL/MySQL), "
            "wait until it finishes migrations, then retry."
        )

    migrator.job.log(f"Target Alembic version after init: {target_version}")

    if source_version and source_version != target_version:
        migrator.job.log(
            f"Aligning target alembic_version {target_version} → {source_version} for db-migrations"
        )
        if await set_target_alembic_version(migrator, target_db, source_version, password):
            target_version = source_version

    return target_version
