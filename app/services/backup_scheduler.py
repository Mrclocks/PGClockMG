"""Lightweight interval scheduler for automatic backups."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.services.backup_settings import (
    normalize_interval_hours,
    load_settings,
    parse_last_run_at,
    update_settings,
)

log = logging.getLogger("pgclockmg.backup_scheduler")

_task: asyncio.Task | None = None
_last_run_key: str | None = None


def due_for_scheduled_run(
    *,
    enabled: bool,
    interval_hours: object,
    last_run_at: object,
    now: datetime | None = None,
) -> bool:
    """Return True when a scheduled backup should start."""
    if not enabled:
        return False
    interval = normalize_interval_hours(interval_hours)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    last = parse_last_run_at(last_run_at)
    if last is None:
        return True
    return (current - last).total_seconds() >= interval * 3600


async def _maybe_run_scheduled() -> None:
    global _last_run_key
    cfg = load_settings()
    sched = cfg.get("schedule") or {}
    now = datetime.now(timezone.utc)
    if not due_for_scheduled_run(
        enabled=bool(sched.get("enabled")),
        interval_hours=sched.get("interval_hours"),
        last_run_at=sched.get("last_run_at"),
        now=now,
    ):
        return
    # Debounce within the same UTC minute so a slow tick cannot double-fire.
    key = now.strftime("%Y-%m-%d-%H-%M")
    if key == _last_run_key:
        return
    _last_run_key = key
    stamp = now.isoformat().replace("+00:00", "Z")
    # Persist start time first so failures do not retry every 30s.
    update_settings({"schedule": {"last_run_at": stamp}})
    log.info(
        "Starting scheduled backup (every %sh) at %s",
        normalize_interval_hours(sched.get("interval_hours")),
        stamp,
    )
    from app.services.backup_engine import create_backup_bundle, apply_retention, resolve_backup_path
    from app.services.backup_telegram import send_backup_to_telegram

    result = await asyncio.to_thread(create_backup_bundle, trigger="schedule")
    if result.get("status") != "success":
        update_settings({"last_error": {"at": stamp, "message": result.get("error") or "schedule_failed"}})
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
