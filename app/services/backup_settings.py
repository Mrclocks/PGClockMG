"""Persistent settings for the backup panel (Telegram, schedule, stream defaults)."""

from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from typing import Any

from app.config import BACKUP_SETTINGS_FILE, TELEGRAM_BOT_MAX_BYTES

_LOCK = threading.RLock()

DEFAULT_TELEGRAM_CAPTION = (
    "PGClockMG backup\n"
    "Date: {date}\n"
    "Size: {size}\n"
    "DB: {db_type}\n"
    "Users: {users} · Nodes: {nodes}\n"
    "Status: {status}"
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "version": 1,
    "retention_count": 10,
    "schedule": {
        "enabled": False,
        "hour": 3,
        "minute": 0,
        "send_telegram": False,
    },
    "telegram": {
        "enabled": False,
        "bot_token": "",
        "chat_id": "",
        "proxy_enabled": False,
        "proxy_type": "socks5",  # socks5 | http
        "proxy_host": "",
        "proxy_port": 1080,
        "proxy_user": "",
        "proxy_password": "",
        "caption_template": DEFAULT_TELEGRAM_CAPTION,
        "max_part_bytes": TELEGRAM_BOT_MAX_BYTES,
    },
    "stream": {
        "default_dest_url": "",
    },
    "last_backup": None,
    "last_error": None,
}


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
    # Expose admin_id as the preferred UI field (same value as Telegram chat_id).
    chat = (tg.get("chat_id") or tg.get("admin_id") or "").strip()
    tg["chat_id"] = chat
    tg["admin_id"] = chat
    cfg["telegram"] = tg
    return cfg
