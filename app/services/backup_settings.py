"""Persistent settings for the backup panel (Telegram, schedule, notify)."""

from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import BACKUP_SETTINGS_FILE, TELEGRAM_BOT_MAX_BYTES

_LOCK = threading.RLock()

SCHEDULE_INTERVALS = (1, 3, 6, 12, 24)

# Common IANA zones for the panel dropdown (plus UTC).
SCHEDULE_TIMEZONES = (
    "UTC",
    "Asia/Tehran",
    "Asia/Dubai",
    "Asia/Istanbul",
    "Europe/Moscow",
    "Europe/London",
    "Europe/Berlin",
    "America/New_York",
    "Asia/Shanghai",
    "Asia/Tokyo",
)

DEFAULT_TELEGRAM_CAPTION = (
    "PGClockMG backup\n"
    "Date: {date}\n"
    "Size: {size}\n"
    "DB: {db_type}\n"
    "Users: {users} · Nodes: {nodes}\n"
    "Status: {status}"
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "version": 2,
    "retention_count": 10,
    "retention_days": 0,  # 0 = disabled (count-only)
    "schedule": {
        "enabled": False,
        "interval_hours": 24,
        "timezone": "UTC",
        # Legacy daily clock fields (ignored by the interval scheduler).
        "hour": 3,
        "minute": 0,
        "send_telegram": False,
        "notify_on_failure": True,
        "last_success_at": None,
        "last_attempt_at": None,
        # Compat alias — scheduler prefers last_success_at.
        "last_run_at": None,
    },
    "telegram": {
        "enabled": False,
        "bot_token": "",
        "chat_id": "",
        "message_thread_id": None,
        "destinations": [],  # [{chat_id, message_thread_id, label}]
        "proxy_enabled": False,
        "proxy_type": "socks5",  # socks5 | http
        "proxy_host": "",
        "proxy_port": 1080,
        "proxy_user": "",
        "proxy_password": "",
        "caption_template": DEFAULT_TELEGRAM_CAPTION,
        "max_part_bytes": TELEGRAM_BOT_MAX_BYTES,
    },
    "notify": {
        "webhook_enabled": False,
        "webhook_url": "",
    },
    "integrity": {
        "verify_after_create": True,
    },
    "stream": {
        "default_dest_url": "",
    },
    "last_backup": None,
    "last_error": None,
}


def normalize_interval_hours(value: object) -> int:
    try:
        iv = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 24
    return iv if iv in SCHEDULE_INTERVALS else 24


def normalize_timezone(value: object) -> str:
    text = (str(value).strip() if value is not None else "") or "UTC"
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return "UTC"
    return text


def get_zoneinfo(name: object) -> ZoneInfo:
    return ZoneInfo(normalize_timezone(name))


def parse_last_run_at(raw: object) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_in_timezone(dt: datetime | None, tz_name: object) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(get_zoneinfo(tz_name))
    return local.strftime("%Y-%m-%d %H:%M:%S %Z")


def next_run_after(
    *,
    last_success_at: object,
    interval_hours: object,
    timezone_name: object,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute next due instant for UI (UTC + local labels)."""
    interval = normalize_interval_hours(interval_hours)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    last = parse_last_run_at(last_success_at)
    if last is None:
        due = current
    else:
        due = last + timedelta(hours=interval)
        if due < current:
            due = current
    return {
        "at_utc": due.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "at_local": format_in_timezone(due, timezone_name),
        "timezone": normalize_timezone(timezone_name),
        "interval_hours": interval,
    }


def normalize_thread_id(value: object) -> int | None:
    if value in (None, "", False):
        return None
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def normalize_retention_days(value: object) -> int:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, min(3650, n))


def normalize_retention_count(value: object) -> int:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 10
    return max(1, min(100, n))


def normalize_destinations(
    raw: object,
    *,
    primary_chat: str = "",
    primary_thread: object = None,
) -> list[dict]:
    """Build a clean destinations list (max 5)."""
    out: list[dict] = []
    seen: set[tuple[str, int | None]] = set()

    def _add(chat: object, thread: object, label: object = "") -> None:
        cid = (str(chat).strip() if chat is not None else "")
        if not cid:
            return
        tid = normalize_thread_id(thread)
        key = (cid, tid)
        if key in seen:
            return
        seen.add(key)
        item: dict[str, Any] = {"chat_id": cid, "message_thread_id": tid}
        lab = (str(label).strip() if label is not None else "")[:64]
        if lab:
            item["label"] = lab
        out.append(item)

    if primary_chat:
        _add(primary_chat, primary_thread, "primary")

    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            _add(
                item.get("chat_id") or item.get("admin_id"),
                item.get("message_thread_id"),
                item.get("label"),
            )
            if len(out) >= 5:
                break
    return out


def normalize_schedule(patch: dict | None) -> dict:
    """Sanitize a schedule patch from the API/UI (does not wipe runtime stamps)."""
    src = dict(patch or {})
    out: dict[str, Any] = {
        "enabled": bool(src.get("enabled")),
        "interval_hours": normalize_interval_hours(src.get("interval_hours")),
        "timezone": normalize_timezone(src.get("timezone")),
        "send_telegram": bool(src.get("send_telegram")),
        "notify_on_failure": bool(src.get("notify_on_failure", True)),
    }
    for key in ("last_run_at", "last_success_at", "last_attempt_at"):
        if key in src:
            out[key] = src.get(key)
    return out


def normalize_notify(patch: dict | None) -> dict:
    src = dict(patch or {})
    url = (src.get("webhook_url") or "").strip()
    if len(url) > 2048:
        url = url[:2048]
    return {
        "webhook_enabled": bool(src.get("webhook_enabled")),
        "webhook_url": url,
    }


def normalize_integrity(patch: dict | None) -> dict:
    src = dict(patch or {})
    return {"verify_after_create": bool(src.get("verify_after_create", True))}


def _atomic_write(path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _merge(base: dict, overlay: dict) -> dict:
    out = deepcopy(base)
    for key, val in (overlay or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], val)
        else:
            out[key] = val
    return out


def load_settings() -> dict:
    with _LOCK:
        if not BACKUP_SETTINGS_FILE.is_file():
            data = deepcopy(DEFAULT_SETTINGS)
            _atomic_write(BACKUP_SETTINGS_FILE, data)
            return data
        try:
            raw = json.loads(BACKUP_SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        return _merge(DEFAULT_SETTINGS, raw if isinstance(raw, dict) else {})


def save_settings(data: dict) -> dict:
    with _LOCK:
        merged = _merge(DEFAULT_SETTINGS, data)
        _atomic_write(BACKUP_SETTINGS_FILE, merged)
        return merged


def update_settings(patch: dict) -> dict:
    with _LOCK:
        current = load_settings()
        merged = _merge(current, patch)
        _atomic_write(BACKUP_SETTINGS_FILE, merged)
        return merged


def public_settings(data: dict | None = None) -> dict:
    """Settings safe to return to the browser (secrets masked)."""
    cfg = deepcopy(data or load_settings())
    tg = cfg.get("telegram") or {}
    token = tg.get("bot_token") or ""
    if token:
        tg["bot_token_set"] = True
        tg["bot_token"] = ""
        tg["bot_token_hint"] = (token[:4] + "…" + token[-4:]) if len(token) > 10 else "••••"
    else:
        tg["bot_token_set"] = False
        tg["bot_token_hint"] = ""
    proxy_pass = tg.get("proxy_password") or ""
    if proxy_pass:
        tg["proxy_password_set"] = True
        tg["proxy_password"] = ""
    else:
        tg["proxy_password_set"] = False
    chat = (tg.get("chat_id") or tg.get("admin_id") or "").strip()
    tg["chat_id"] = chat
    tg["admin_id"] = chat
    tg["message_thread_id"] = normalize_thread_id(tg.get("message_thread_id"))
    stored_dests = tg.get("destinations") or []
    tg["destinations"] = normalize_destinations(stored_dests)
    primary_key = (chat, tg["message_thread_id"])
    extras = []
    for d in tg["destinations"]:
        key = (d.get("chat_id"), d.get("message_thread_id"))
        if chat and key == primary_key:
            continue
        extras.append(d)
    tg["extra_destinations"] = extras[:4]
    cfg["telegram"] = tg
    sched = cfg.get("schedule") or {}
    sched["timezone"] = normalize_timezone(sched.get("timezone"))
    sched["interval_hours"] = normalize_interval_hours(sched.get("interval_hours"))
    success = sched.get("last_success_at") or sched.get("last_run_at")
    if sched.get("enabled"):
        sched["next_run"] = next_run_after(
            last_success_at=success,
            interval_hours=sched.get("interval_hours"),
            timezone_name=sched.get("timezone"),
        )
    else:
        sched["next_run"] = None
    sched["last_success_local"] = format_in_timezone(
        parse_last_run_at(success),
        sched.get("timezone"),
    )
    cfg["schedule"] = sched
    cfg["retention_count"] = normalize_retention_count(cfg.get("retention_count"))
    cfg["retention_days"] = normalize_retention_days(cfg.get("retention_days"))
    return cfg
