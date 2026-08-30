"""Failure notifications for the backup panel (Telegram text + optional webhook)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.services.backup_net import UnsafeDestinationError, normalize_public_http_url
from app.services.backup_settings import load_settings

log = logging.getLogger("pgclockmg.backup_notify")


def _telegram_send_text(settings: dict, text: str) -> dict:
    from app.services.backup_telegram import resolve_destinations, send_telegram_message, telegram_config

    tg = telegram_config(settings)
    token = (tg.get("bot_token") or "").strip()
    dests = resolve_destinations(tg)
    if not token or not dests:
        return {"ok": False, "error": "telegram_not_configured", "sent": 0}
    sent = 0
    errors: list[str] = []
    for dest in dests:
        result = send_telegram_message(
            text,
            chat_id=dest["chat_id"],
            message_thread_id=dest.get("message_thread_id"),
            settings=settings,
        )
        if result.get("ok"):
            sent += 1
        else:
            errors.append(str(result.get("error") or "send_failed"))
    return {"ok": sent > 0, "sent": sent, "errors": errors}


def _webhook_post(settings: dict, payload: dict) -> dict:
    notify = (settings or {}).get("notify") or {}
    if not notify.get("webhook_enabled"):
        return {"ok": False, "skipped": True, "error": "webhook_disabled"}
    raw_url = (notify.get("webhook_url") or "").strip()
    if not raw_url:
        return {"ok": False, "skipped": True, "error": "webhook_url_missing"}
    try:
        url = normalize_public_http_url(raw_url)
    except UnsafeDestinationError as exc:
        return {"ok": False, "error": f"webhook_url_unsafe:{exc}"}
    try:
        with httpx.Client(timeout=15.0, follow_redirects=False) as client:
            resp = client.post(url, json=payload)
            if resp.status_code >= 400:
                return {"ok": False, "error": f"http_{resp.status_code}", "status": resp.status_code}
            return {"ok": True, "status": resp.status_code}
    except Exception as exc:
        log.warning("webhook notify failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def notify_backup_failure(
    *,
    message: str,
    trigger: str = "schedule",
    at: str | None = None,
    settings: dict | None = None,
    extra: dict | None = None,
) -> dict[str, Any]:
    """
    Notify operators about a backup/schedule failure.

    Telegram text goes to all configured destinations when schedule.notify_on_failure
    is enabled (default). Webhook posts a small JSON payload when enabled.
    """
    cfg = settings or load_settings()
    sched = cfg.get("schedule") or {}
    notify_tg = bool(sched.get("notify_on_failure", True))
    text = (
        "PGClockMG backup failure\n"
        f"Trigger: {trigger}\n"
        f"Time: {at or '-'}\n"
        f"Error: {(message or 'unknown')[:500]}"
    )
    out: dict[str, Any] = {"telegram": None, "webhook": None}
    if notify_tg:
        try:
            out["telegram"] = _telegram_send_text(cfg, text)
        except Exception as exc:
            log.exception("telegram failure notify failed")
            out["telegram"] = {"ok": False, "error": str(exc)}
    payload = {
        "event": "backup_failure",
        "trigger": trigger,
        "at": at,
        "message": (message or "")[:2000],
        "extra": extra or {},
    }
    try:
        out["webhook"] = _webhook_post(cfg, payload)
    except Exception as exc:
        log.exception("webhook failure notify failed")
        out["webhook"] = {"ok": False, "error": str(exc)}
    out["ok"] = bool(
        (out.get("telegram") or {}).get("ok")
        or (out.get("webhook") or {}).get("ok")
    )
    return out
