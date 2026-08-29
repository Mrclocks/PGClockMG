"""Lightweight daily scheduler for automatic backups."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.services.backup_settings import load_settings, update_settings

log = logging.getLogger("pgclockmg.backup_scheduler")

_task: asyncio.Task | None = None
_last_run_key: str | None = None


async def _maybe_run_scheduled() -> None:
    global _last_run_key
    cfg = load_settings()
    sched = cfg.get("schedule") or {}
    if not sched.get("enabled"):
        return
    hour = int(sched.get("hour") or 3)
    minute = int(sched.get("minute") or 0)
    now = datetime.now(timezone.utc)
    if now.hour != hour or now.minute != minute:
        return
    key = now.strftime("%Y-%m-%d-%H-%M")
    if key == _last_run_key:
        return
    _last_run_key = key
    log.info("Starting scheduled backup at %s", key)
    from app.services.backup_engine import create_backup_bundle, apply_retention, resolve_backup_path
    from app.services.backup_telegram import send_backup_to_telegram

    # Run blocking work off the event loop
    result = await asyncio.to_thread(create_backup_bundle, trigger="schedule")
    if result.get("status") != "success":
        update_settings({"last_error": {"at": key, "message": result.get("error") or "schedule_failed"}})
        return
    apply_retention(int((cfg.get("retention_count") or 10)))
    if sched.get("send_telegram"):
        path = resolve_backup_path(result.get("backup_id") or "")
        if path:
            await asyncio.to_thread(
                send_backup_to_telegram,
                path,
                manifest=result.get("manifest") or {},
                settings=cfg,
            )


async def _loop() -> None:
    while True:
        try:
            await _maybe_run_scheduled()
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(30)


def start_scheduler() -> None:
    global _task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _task and not _task.done():
        return
    _task = loop.create_task(_loop(), name="backup-scheduler")


def stop_scheduler() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
    _task = None
