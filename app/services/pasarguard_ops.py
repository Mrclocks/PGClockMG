"""Non-interactive PasarGuard Docker operations (no hanging CLI)."""

from __future__ import annotations

import asyncio

from app.config import PASARGUARD_DIR

STARTUP_MARKERS = (
    "Application startup complete",
    "Uvicorn running",
)


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
            ["docker", "compose", "logs", "--no-color", "--tail", "50", "pasarguard"],
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


async def start_pasarguard(migrator, wait: bool = True) -> None:
    await docker_compose_up(migrator)
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


async def ensure_schema_initialized(migrator) -> None:
    """Boot PasarGuard once so Alembic creates schema on an empty target DB."""
    await start_pasarguard(migrator, wait=True)
