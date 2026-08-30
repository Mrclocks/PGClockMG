"""Interval scheduler for automatic backups (timezone-aware next-run UX)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.services.backup_settings import (
    load_settings,
    normalize_interval_hours,
    normalize_timezone,
    parse_last_run_at,
    update_settings,
    utc_now_iso,
)

log = logging.getLogger("pgclockmg.backup_scheduler")

# Retry sooner after a failed schedule without waiting the full interval.
FAILURE_RETRY_SECONDS = 15 * 60

_task: asyncio.Task | None = None
_running = False


def schedule_anchor(sched: dict | None) -> datetime | None:
    """Prefer last_success_at; fall back to legacy last_run_at."""
    sched = sched or {}
    return parse_last_run_at(sched.get("last_success_at")) or parse_last_run_at(sched.get("last_run_at"))


def due_for_scheduled_run(
    *,
    enabled: bool,
    interval_hours: object,
    last_success_at: object = None,
    last_run_at: object = None,
    last_attempt_at: object = None,
    now: datetime | None = None,
) -> bool:
    """
    Return True when a scheduled backup should start.

    Interval is measured from the last *successful* run (elapsed UTC seconds).
    After a failure, wait FAILURE_RETRY_SECONDS before retrying — do not wait
    the full interval, and do not hammer every 30s tick.
    """
    if not enabled:
        return False
    interval = normalize_interval_hours(interval_hours)
    interval_sec = float(interval) * 3600.0
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    last_success = parse_last_run_at(last_success_at) or parse_last_run_at(last_run_at)
    last_attempt = parse_last_run_at(last_attempt_at)

    def _in_failure_backoff() -> bool:
        if last_attempt is None:
            return False
        if last_success is not None and last_attempt <= last_success:
            return False
        return (current - last_attempt).total_seconds() < FAILURE_RETRY_SECONDS

    if last_success is None:
        # Never succeeded: run soon, but respect failure backoff.
        return not _in_failure_backoff()

    elapsed = (current - last_success).total_seconds()
    if elapsed < interval_sec:
        return False
    return not _in_failure_backoff()


def should_send_scheduled_telegram(cfg: dict | None, sched: dict | None = None) -> bool:
    """
    Decide whether a successful scheduled backup should be uploaded to Telegram.

    Rules (robust for real-world configs):
    - Bot must be ready (token + destination).
    - Send when schedule.send_telegram is on, OR when Telegram itself is enabled
      (many users turn on Telegram for manual sends and expect schedule to follow).
    - Explicit send_telegram=False with telegram.enabled=False → no send.
    """
    from app.services.backup_telegram import telegram_config, telegram_ready

    settings = cfg or {}
    schedule = sched if sched is not None else (settings.get("schedule") or {})
    tg = telegram_config(settings)
    if not telegram_ready(tg):
        return False
    if bool(schedule.get("send_telegram")):
        return True
    return bool(tg.get("enabled"))


async def _deliver_scheduled_telegram(result: dict, cfg: dict, *, interval: float, tz_name: str) -> dict:
    """Send scheduled backup to Telegram with one retry; never raise to caller."""
    from app.services.backup_engine import resolve_backup_path
    from app.services.backup_telegram import send_backup_to_telegram

    path = resolve_backup_path(str(result.get("backup_id") or ""))
    if not path:
        out = {"ok": False, "error": "backup_path_missing"}
        log.error("scheduled telegram skipped: backup path missing for %s", result.get("backup_id"))
        return out

    last: dict = {"ok": False, "error": "not_attempted"}
    for attempt in range(1, 3):
        try:
            last = await asyncio.to_thread(
                send_backup_to_telegram,
                path,
                manifest=result.get("manifest") or {},
                settings=cfg,
            )
        except Exception as exc:
            log.exception("scheduled telegram send raised (attempt %s)", attempt)
            last = {"ok": False, "error": str(exc)}
        if last.get("ok"):
            log.info(
                "scheduled telegram send ok (attempt %s): %s",
                attempt,
                path.name,
            )
            return last
        log.warning(
            "scheduled telegram send failed (attempt %s): %s",
            attempt,
            last.get("error") or "unknown",
        )
        if attempt < 2:
            await asyncio.sleep(2)

    # Soft error only — backup itself already succeeded.
    try:
        update_settings({
            "last_error": {
                "at": utc_now_iso(),
                "message": f"telegram_send_failed: {last.get('error') or 'unknown'}",
                "trigger": "schedule_telegram",
            },
        })
    except Exception:
        log.exception("failed to record telegram send error")
    try:
        from app.services.backup_notify import notify_backup_failure

        await asyncio.to_thread(
            notify_backup_failure,
            message=f"Telegram send failed after scheduled backup: {last.get('error') or 'unknown'}",
            trigger="schedule_telegram",
            at=utc_now_iso(),
            settings=cfg,
            extra={"interval_hours": interval, "timezone": tz_name, "filename": path.name},
        )
    except Exception:
        log.exception("telegram failure notify crashed")
    return last


async def _maybe_run_scheduled() -> None:
    global _running
    if _running:
        return
    cfg = load_settings()
    sched = cfg.get("schedule") or {}
    now = datetime.now(timezone.utc)
    if not due_for_scheduled_run(
        enabled=bool(sched.get("enabled")),
        interval_hours=sched.get("interval_hours"),
        last_success_at=sched.get("last_success_at"),
        last_run_at=sched.get("last_run_at"),
        last_attempt_at=sched.get("last_attempt_at"),
        now=now,
    ):
        return

    _running = True
    stamp = utc_now_iso()
    tz_name = normalize_timezone(sched.get("timezone"))
    interval = normalize_interval_hours(sched.get("interval_hours"))
    update_settings({"schedule": {"last_attempt_at": stamp}})
    log.info("Starting scheduled backup (every %sh, tz=%s) at %s", interval, tz_name, stamp)

    from app.services.backup_engine import create_backup_bundle
    from app.services.backup_notify import notify_backup_failure

    try:
        result = await asyncio.to_thread(create_backup_bundle, trigger="schedule")
        if result.get("status") != "success":
            message = result.get("error") or "schedule_failed"
            update_settings({"last_error": {"at": stamp, "message": message, "trigger": "schedule"}})
            await asyncio.to_thread(
                notify_backup_failure,
                message=message,
                trigger="schedule",
                at=stamp,
                settings=cfg,
                extra={"interval_hours": interval, "timezone": tz_name},
            )
            return

        success_stamp = utc_now_iso()
        update_settings({
            "schedule": {
                "last_success_at": success_stamp,
                "last_run_at": success_stamp,
                "last_attempt_at": success_stamp,
            },
            "last_error": None,
        })
        # Retention already runs inside create_backup_bundle on success.
        # Telegram: awaited here (not via maybe_auto_send) so delivery is reliable.
        cfg2 = load_settings()
        sched2 = cfg2.get("schedule") or {}
        if should_send_scheduled_telegram(cfg2, sched2):
            await _deliver_scheduled_telegram(
                result,
                cfg2,
                interval=interval,
                tz_name=tz_name,
            )
        else:
            log.info("scheduled telegram skipped (not enabled / not configured)")
    except Exception as exc:
        log.exception("scheduled backup crashed")
        message = str(exc) or "schedule_exception"
        update_settings({"last_error": {"at": stamp, "message": message, "trigger": "schedule"}})
        try:
            await asyncio.to_thread(
                notify_backup_failure,
                message=message,
                trigger="schedule",
                at=stamp,
                settings=cfg,
                extra={"interval_hours": interval, "timezone": tz_name},
            )
        except Exception:
            log.exception("failure notify crashed")
    finally:
        _running = False


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
    global _task, _running
    if _task and not _task.done():
        _task.cancel()
    _task = None
    _running = False
