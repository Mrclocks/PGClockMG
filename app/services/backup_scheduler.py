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

# Legacy constant — first-step backoff; kept for import compatibility with tests.
FAILURE_RETRY_SECONDS = 15 * 60
FAILURE_BACKOFF_CAP_SECONDS = 6 * 60 * 60

_task: asyncio.Task | None = None
_running = False


def failure_backoff_seconds(consecutive_failures: object) -> int:
    """Exponential backoff after consecutive failures: 15m, 30m, 60m … capped at 6h."""
    try:
        n = int(consecutive_failures)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return 0
    # After 1st failure wait 15m; 2nd → 30m; 3rd → 60m; …
    exp = max(0, n - 1)
    delay = FAILURE_RETRY_SECONDS * (2 ** exp)
    return int(min(delay, FAILURE_BACKOFF_CAP_SECONDS))


def should_notify_consecutive_failure(consecutive_failures: object) -> bool:
    """Notify on the first failure and every 3rd consecutive failure thereafter."""
    try:
        n = int(consecutive_failures)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return n == 1 or (n > 0 and n % 3 == 0)


def due_for_scheduled_run(
    *,
    enabled: bool,
    interval_hours: object,
    last_success_at: object = None,
    last_run_at: object = None,
    last_attempt_at: object = None,
    consecutive_failures: object = 0,
    now: datetime | None = None,
    run_at: object = None,
    timezone_name: object = None,
) -> bool:
    """
    Return True when a scheduled backup should start.

    Interval is measured from the last *successful* run (elapsed UTC seconds).
    After failures, wait exponential backoff (15m → 30m → 60m … ≤ 6h) before
    retrying — do not wait the full interval, and do not hammer every 30s tick.
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
    backoff = failure_backoff_seconds(consecutive_failures)

    def _in_failure_backoff() -> bool:
        if last_attempt is None:
            return False
        if last_success is not None and last_attempt <= last_success:
            return False
        wait = backoff if backoff > 0 else FAILURE_RETRY_SECONDS
        return (current - last_attempt).total_seconds() < wait

    slot = latest_run_slot(run_at, timezone_name, interval, current)
    if slot is not None:
        # Fixed time of day (24h interval): due once per day after the slot passed.
        if last_success is not None and last_success >= slot:
            return False
        return not _in_failure_backoff()

    if last_success is None:
        # Never succeeded: run soon, but respect failure backoff.
        return not _in_failure_backoff()

    elapsed = (current - last_success).total_seconds()
    if elapsed < interval_sec:
        return False
    return not _in_failure_backoff()


def latest_run_slot(run_at: object, timezone_name: object, interval_hours: float, now: datetime) -> datetime | None:
    """Most recent fixed daily slot (UTC) for ``run_at`` HH:MM, or None when not configured.

    Only applies to the 24h interval; shorter intervals keep the rolling behaviour.
    """
    from datetime import timedelta

    from app.services.backup_settings import get_zoneinfo, normalize_run_at

    hhmm = normalize_run_at(run_at)
    if not hhmm or float(interval_hours) < 24.0:
        return None
    hour, minute = int(hhmm[:2]), int(hhmm[3:])
    local_now = now.astimezone(get_zoneinfo(timezone_name))
    slot = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if slot > local_now:
        slot -= timedelta(days=1)
    return slot.astimezone(timezone.utc)


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
            from app.services.backup_telegram import redact_telegram_text

            log.error("scheduled telegram send raised (attempt %s): %s", attempt, redact_telegram_text(exc))
            last = {"ok": False, "error": redact_telegram_text(exc)}
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

    # Keep trying in the background (Telegram is often blocked for hours).
    try:
        from app.services.backup_telegram_queue import enqueue

        enqueue(str(result.get("backup_id") or ""), str(last.get("error") or ""))
    except Exception:
        log.exception("could not queue telegram retry")
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
    # Skip quietly while restore/migrate holds the shared panel lock.
    try:
        from app.services.panel_job_lock import is_panel_busy_locked

        if is_panel_busy_locked():
            return
    except Exception:
        pass

    cfg = load_settings()
    sched = cfg.get("schedule") or {}
    now = datetime.now(timezone.utc)
    consecutive = int(sched.get("consecutive_failures") or 0)
    if not due_for_scheduled_run(
        enabled=bool(sched.get("enabled")),
        interval_hours=sched.get("interval_hours"),
        last_success_at=sched.get("last_success_at"),
        last_run_at=sched.get("last_run_at"),
        last_attempt_at=sched.get("last_attempt_at"),
        consecutive_failures=consecutive,
        now=now,
        run_at=sched.get("run_at"),
        timezone_name=sched.get("timezone"),
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

    async def _record_failure(message: str) -> None:
        new_count = consecutive + 1
        update_settings({
            "schedule": {"consecutive_failures": new_count},
            "last_error": {"at": stamp, "message": message, "trigger": "schedule"},
        })
        if should_notify_consecutive_failure(new_count):
            await asyncio.to_thread(
                notify_backup_failure,
                message=message,
                trigger="schedule",
                at=stamp,
                settings=cfg,
                extra={
                    "interval_hours": interval,
                    "timezone": tz_name,
                    "consecutive_failures": new_count,
                },
            )

    try:
        result = await asyncio.to_thread(create_backup_bundle, trigger="schedule")
        if result.get("status") != "success":
            message = result.get("error") or "schedule_failed"
            await _record_failure(message)
            return

        success_stamp = utc_now_iso()
        update_settings({
            "schedule": {
                "last_success_at": success_stamp,
                "last_run_at": success_stamp,
                "last_attempt_at": success_stamp,
                "consecutive_failures": 0,
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
        try:
            await _record_failure(message)
        except Exception:
            log.exception("failure notify crashed")
    finally:
        _running = False


STALE_ALERT_COOLDOWN_SEC = 24 * 3600


def stale_backup_hours(sched: dict | None, now: datetime | None = None) -> float | None:
    """Hours since the last successful scheduled backup when it is clearly overdue, else None."""
    sched = sched or {}
    if not sched.get("enabled"):
        return None
    interval = normalize_interval_hours(sched.get("interval_hours"))
    last = parse_last_run_at(sched.get("last_success_at")) or parse_last_run_at(sched.get("last_run_at"))
    if last is None:
        return None
    current = now or datetime.now(timezone.utc)
    age_h = (current - last).total_seconds() / 3600.0
    limit = max(3.0 * interval, interval + 6.0)
    return age_h if age_h > limit else None


async def _check_stale_backup() -> None:
    """Dead-man alert: tell the operator when no backup succeeded for far too long."""
    cfg = load_settings()
    sched = cfg.get("schedule") or {}
    hours = stale_backup_hours(sched)
    if hours is None:
        return
    last_alert = parse_last_run_at(sched.get("last_stale_alert_at"))
    if last_alert and (datetime.now(timezone.utc) - last_alert).total_seconds() < STALE_ALERT_COOLDOWN_SEC:
        return
    update_settings({"schedule": {"last_stale_alert_at": utc_now_iso()}})
    from app.services.backup_notify import notify_backup_failure

    await asyncio.to_thread(
        notify_backup_failure,
        message=f"No successful backup for {hours:.0f} hours (expected every {sched.get('interval_hours')}h)",
        trigger="stale_backup",
        at=utc_now_iso(),
        settings=cfg,
        extra={"hours_since_success": round(hours, 1)},
    )


async def _retry_pending_telegram() -> None:
    from app.services import backup_telegram_queue as q

    if not q.pending():
        return
    cfg = load_settings()
    from app.services.backup_engine import resolve_backup_manifest, resolve_backup_path
    from app.services.backup_telegram import send_backup_to_telegram, telegram_ready, telegram_config

    if not telegram_ready(telegram_config(cfg)):
        return

    def _send(path, item):
        return send_backup_to_telegram(path, manifest=resolve_backup_manifest(path), settings=cfg)

    result = await asyncio.to_thread(q.process_due, _send, resolve_backup_path)
    if result.get("sent"):
        log.info("telegram retry queue: delivered %s pending backup(s)", result["sent"])
    if result.get("expired"):
        log.warning("telegram retry queue: dropped %s expired item(s)", len(result["expired"]))


async def _loop() -> None:
    tick = 0
    while True:
        try:
            await _maybe_run_scheduled()
        except Exception:
            log.exception("scheduler tick failed")
        tick += 1
        if tick % 2 == 0:  # ~every minute
            try:
                await _retry_pending_telegram()
            except Exception:
                log.exception("telegram retry queue failed")
            try:
                await _check_stale_backup()
            except Exception:
                log.exception("stale backup check failed")
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
