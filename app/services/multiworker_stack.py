"""PasarGuard multi-worker / NATS docker-compose stack helpers (restore & panel boot)."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from app.config import PASARGUARD_DIR, PASARGUARD_ENV
from app.services.env_migration import _set_env_var_simple, read_env_var
from app.services.pasarguard_ops import (
    PANEL_BOOT_MARKERS,
    _compose_text,
    compose_file_prefix,
    panel_compose_service,
    resolve_pasarguard_service,
)

if TYPE_CHECKING:
    from app.services.migrators.base import MigrationJob

NATS_SERVICE = "nats"
SATELLITE_SERVICES = ("node-worker", "scheduler")
NATS_READY_MARKERS = (
    "Server is ready",
    "Listening for client connections on",
)


def compose_has_service(name: str) -> bool:
    if not name:
        return False
    text = _compose_text()
    if not text.strip():
        return False
    return bool(re.search(rf"^\s*{re.escape(name)}\s*:", text, re.MULTILINE))


def parse_env_bool(val: str | None) -> bool:
    if not val:
        return False
    return val.strip().lower() in ("1", "true", "yes", "on")


def _parse_uvicorn_workers(env_text: str) -> int:
    raw = read_env_var(env_text, "UVICORN_WORKERS")
    if not raw:
        return 1
    try:
        return max(1, int(str(raw).strip()))
    except ValueError:
        return 1


def detect_multiworker_stack(env_text: str | None = None) -> dict[str, Any]:
    """Inspect compose + .env to decide if NATS/multi-service orchestration is needed."""
    env = env_text
    if env is None:
        env = (
            PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
            if PASARGUARD_ENV.exists()
            else ""
        )
    workers = _parse_uvicorn_workers(env)
    nats_enabled = parse_env_bool(read_env_var(env, "NATS_ENABLED"))
    has_nats = compose_has_service(NATS_SERVICE)
    satellites = [s for s in SATELLITE_SERVICES if compose_has_service(s)]
    panel_service = resolve_pasarguard_service()

    uses_nats = bool(has_nats and (nats_enabled or workers > 1))
    orchestrate = bool(uses_nats or satellites)

    return {
        "orchestrate": orchestrate,
        "uses_nats": uses_nats,
        "uvicorn_workers": workers,
        "nats_enabled": nats_enabled,
        "has_nats_service": has_nats,
        "satellite_services": satellites,
        "panel_service": panel_service,
    }


def align_nats_env_for_compose(text: str) -> str:
    """Point NATS_URL at the in-compose service name when multi-worker needs NATS."""
    if not compose_has_service(NATS_SERVICE):
        return text

    workers = _parse_uvicorn_workers(text)
    nats_url = (read_env_var(text, "NATS_URL") or "").strip()
    bad_host = (
        not nats_url
        or "localhost" in nats_url.lower()
        or "127.0.0.1" in nats_url
        or "0.0.0.0" in nats_url
    )
    if bad_host:
        text = _set_env_var_simple(text, "NATS_URL", f"nats://{NATS_SERVICE}:4222")

    if workers > 1 and not parse_env_bool(read_env_var(text, "NATS_ENABLED")):
        text = _set_env_var_simple(text, "NATS_ENABLED", "1")

    return text


def panel_stack_stop_services() -> list[str]:
    """Satellite workers first, then the panel/backend service."""
    panel = panel_compose_service()
    ordered: list[str] = []
    for svc in SATELLITE_SERVICES:
        if compose_has_service(svc) and svc not in ordered:
            ordered.append(svc)
    if compose_has_service(panel) and panel not in ordered:
        ordered.append(panel)
    return ordered


async def _compose_job(
    job: MigrationJob,
    *args: str,
    timeout: int = 300,
    quiet: bool = False,
) -> tuple[bool, str]:
    from app.services.pg_restore import _run

    prefix = compose_file_prefix()
    return await _run(
        job,
        ["docker", "compose", *prefix, *args],
        cwd=str(PASARGUARD_DIR),
        timeout=timeout,
        quiet=quiet,
    )


async def stop_panel_stack(job: MigrationJob) -> None:
    """Stop panel + node-worker/scheduler before alembic or DB restore."""
    info = detect_multiworker_stack()
    if not info["orchestrate"]:
        panel = info["panel_service"]
        if compose_has_service(panel):
            await _compose_job(job, "stop", panel, timeout=120)
        elif compose_has_service("pasarguard"):
            await _compose_job(job, "stop", "pasarguard", timeout=120)
        return

    services = panel_stack_stop_services()
    if services:
        job.log(f"Stopping multi-worker stack: {', '.join(services)}")
        await _compose_job(job, "stop", *services, timeout=120)


async def ensure_nats_ready(
    job: MigrationJob,
    timeout: int = 90,
    *,
    force_recreate: bool = False,
) -> None:
    """Bring NATS up and wait until it accepts connections."""
    if not compose_has_service(NATS_SERVICE):
        return

    job.log("Starting NATS for multi-worker panel…")
    up_args: list[str] = ["up", "-d"]
    if force_recreate:
        up_args.append("--force-recreate")
    up_args.append(NATS_SERVICE)
    ok, out = await _compose_job(job, *up_args, timeout=120)
    if not ok:
        job.log(f"NATS compose up warning: {(out or '')[-500:]}")

    deadline = max(15, int(timeout))
    for waited in range(0, deadline, 3):
        _ok, logs = await _compose_job(
            job,
            "logs",
            "--no-color",
            "--tail",
            "40",
            NATS_SERVICE,
            timeout=25,
            quiet=True,
        )
        text = logs or ""
        if any(marker in text for marker in NATS_READY_MARKERS):
            job.log("NATS is ready")
            await asyncio.sleep(2)
            return
        if waited == 0:
            job.log("Waiting for NATS to become ready…")
        await asyncio.sleep(3)

    job.log("NATS readiness timeout — continuing (panel will retry NATS connection)")


async def wait_for_panel_boot(job: MigrationJob, timeout: int = 120) -> bool:
    """Wait until panel logs show Uvicorn/startup markers (multi-worker needs longer)."""
    panel = panel_compose_service()
    deadline = max(30, int(timeout))
    for waited in range(0, deadline, 4):
        _ok, logs = await _compose_job(
            job,
            "logs",
            "--no-color",
            "--tail",
            "80",
            panel,
            timeout=25,
            quiet=True,
        )
        text = logs or ""
        if any(marker in text for marker in PANEL_BOOT_MARKERS):
            job.log("Panel application startup detected")
            return True
        if waited == 0:
            job.log("Waiting for panel workers to finish boot…")
        await asyncio.sleep(4)
    job.log("Panel boot wait timed out — continuing to health check")
    return False


async def start_panel_stack(
    job: MigrationJob,
    *,
    force_recreate: bool = True,
) -> tuple[bool, str]:
    """Start panel with correct ordering for multi-worker / NATS compose layouts."""
    info = detect_multiworker_stack()
    panel = info["panel_service"]

    if not info["orchestrate"]:
        args: list[str] = ["up", "-d"]
        if force_recreate:
            args.extend(["--force-recreate", panel])
        else:
            args.append(panel)
        return await _compose_job(job, *args, timeout=300)

    if info["uses_nats"]:
        await ensure_nats_ready(job, force_recreate=force_recreate)

    job.log(
        f"Starting multi-worker panel ({panel}, workers={info['uvicorn_workers']}, "
        f"nats={'yes' if info['uses_nats'] else 'no'})…"
    )
    up_args: list[str] = ["up", "-d"]
    if force_recreate:
        up_args.append("--force-recreate")
    up_args.append(panel)
    ok, out = await _compose_job(job, *up_args, timeout=300)
    if not ok:
        return ok, out

    await wait_for_panel_boot(job)

    satellites = info["satellite_services"]
    if satellites:
        job.log(f"Starting stack workers: {', '.join(satellites)}")
        sat_args: list[str] = ["up", "-d"]
        if force_recreate:
            sat_args.append("--force-recreate")
        sat_args.extend(satellites)
        ok2, out2 = await _compose_job(job, *sat_args, timeout=180)
        if not ok2:
            return ok2, (out or "") + "\n" + (out2 or "")
        out = (out or "") + "\n" + (out2 or "")

    return ok, out
