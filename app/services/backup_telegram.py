"""Telegram delivery for backup zips (optional proxy, chunked when > ~50MB)."""

from __future__ import annotations

import logging
import math
import mimetypes
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.config import TELEGRAM_BOT_MAX_BYTES
from app.services.backup_net import UnsafeDestinationError, assert_proxy_host
from app.services.backup_settings import (
    DEFAULT_TELEGRAM_CAPTION,
    load_settings,
    normalize_destinations,
    normalize_thread_id,
    update_settings,
)

log = logging.getLogger("pgclockmg.backup_telegram")

TELEGRAM_CAPTION_MAX = 1024
TELEGRAM_TEXT_MAX = 4000


def _proxy_url(tg: dict) -> str | None:
    if not tg.get("proxy_enabled"):
        return None
    host = (tg.get("proxy_host") or "").strip()
    port = int(tg.get("proxy_port") or 0)
    if not host or not (1 <= port <= 65535):
        return None
    try:
        assert_proxy_host(host)
    except UnsafeDestinationError as exc:
        raise ValueError("proxy_host_invalid") from exc
    kind = (tg.get("proxy_type") or "socks5").lower()
    if kind not in ("socks5", "socks5h", "http", "https"):
        kind = "socks5"
    user = (tg.get("proxy_user") or "").strip()
    password = tg.get("proxy_password") or ""
    if user:
        auth = f"{quote(user, safe='')}:{quote(password, safe='')}@"
    else:
        auth = ""
    scheme = "socks5" if kind.startswith("socks") else kind
    return f"{scheme}://{auth}{host}:{port}"


def _client(tg: dict, timeout: float = 120.0) -> httpx.Client:
    proxy = _proxy_url(tg)
    kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": False}
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.Client(**kwargs)


def format_caption(template: str | None, ctx: dict[str, Any]) -> str:
    text = template or DEFAULT_TELEGRAM_CAPTION
    safe = {k: ("" if v is None else str(v)) for k, v in ctx.items()}

    class _Safe(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    try:
        return text.format_map(_Safe(safe))
    except Exception:
        return text


def clip_caption(text: str, limit: int = TELEGRAM_CAPTION_MAX) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1] + "…"


def human_size(n: int | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def resolve_admin_chat_id(tg: dict) -> str:
    """Prefer chat_id; accept admin_id as UI/API alias."""
    return (tg.get("chat_id") or tg.get("admin_id") or "").strip()


def resolve_destinations(tg: dict | None = None) -> list[dict]:
    """
    Resolve send targets: primary chat (+ optional topic) plus extra destinations.
    """
    cfg = tg if tg is not None else telegram_config()
    primary = resolve_admin_chat_id(cfg)
    return normalize_destinations(
        cfg.get("destinations") or [],
        primary_chat=primary,
        primary_thread=cfg.get("message_thread_id"),
    )


def telegram_config(settings: dict | None = None) -> dict:
    cfg = settings or load_settings()
    tg = dict(cfg.get("telegram") or {})
    chat = resolve_admin_chat_id(tg)
    if chat:
        tg["chat_id"] = chat
    tg["message_thread_id"] = normalize_thread_id(tg.get("message_thread_id"))
    return tg


def telegram_ready(tg: dict | None = None) -> bool:
    cfg = tg if tg is not None else telegram_config()
    token = (cfg.get("bot_token") or "").strip()
    return bool(cfg.get("enabled") and token and resolve_destinations(cfg))


def probe_telegram_connection(settings: dict | None = None) -> dict:
    """Lightweight connectivity check (getMe only — does not send a chat message)."""
    tg = telegram_config(settings)
    token = (tg.get("bot_token") or "").strip()
    if not token:
        return {"ok": False, "connected": False, "error": "bot_token_missing"}
    if not resolve_destinations(tg):
        return {"ok": False, "connected": False, "error": "admin_id_missing"}
    try:
        with _client(tg, timeout=20.0) as client:
            me = client.get(f"https://api.telegram.org/bot{token}/getMe")
            me.raise_for_status()
            me_data = me.json()
            if not me_data.get("ok"):
                return {
                    "ok": False,
                    "connected": False,
                    "error": me_data.get("description") or "getMe_failed",
                }
            return {"ok": True, "connected": True, "bot": me_data.get("result")}
    except httpx.HTTPError as exc:
        return {"ok": False, "connected": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "connected": False, "error": str(exc)}


def test_telegram_connection(settings: dict | None = None) -> dict:
    """
    Validate bot credentials, enable Telegram delivery, create a real backup,
    and send the zip document to the admin chat.
    """
    probe = probe_telegram_connection(settings)
    if not probe.get("ok"):
        return {"ok": False, "error": probe.get("error") or "telegram_probe_failed", "bot": probe.get("bot")}

    # Persist enabled so future manual backups auto-send.
    update_settings({"telegram": {"enabled": True}})
    cfg = load_settings()

    from app.services.backup_engine import create_backup_bundle, resolve_backup_path

    backup = create_backup_bundle(trigger="telegram_connect")
    if backup.get("status") != "success":
        return {
            "ok": False,
            "connected": True,
            "bot": probe.get("bot"),
            "error": backup.get("error") or "backup_failed",
            "backup": backup,
        }

    path = resolve_backup_path(str(backup.get("backup_id") or ""))
    if not path or not path.is_file():
        return {
            "ok": False,
            "connected": True,
            "bot": probe.get("bot"),
            "error": "backup_path_missing",
            "backup": backup,
        }

    send = send_backup_to_telegram(path, manifest=backup.get("manifest") or {}, settings=cfg)
    if not send.get("ok"):
        return {
            "ok": False,
            "connected": True,
            "bot": probe.get("bot"),
            "error": send.get("error") or "telegram_send_failed",
            "backup": {
                "backup_id": backup.get("backup_id"),
                "filename": backup.get("filename"),
                "size_bytes": backup.get("size_bytes"),
            },
            "send": send,
        }

    return {
        "ok": True,
        "connected": True,
        "bot": probe.get("bot"),
        "backup": {
            "backup_id": backup.get("backup_id"),
            "filename": backup.get("filename"),
            "size_bytes": backup.get("size_bytes"),
        },
        "send": send,
        "message_id": (send.get("sent") or [{}])[0].get("message_id") if send.get("sent") else None,
    }


def _api_error(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            return str(body.get("description") or body.get("error") or resp.text[:800])
    except Exception:
        pass
    return (resp.text or f"http_{resp.status_code}")[:800]


def send_telegram_message(
    text: str,
    *,
    chat_id: str,
    message_thread_id: int | None = None,
    settings: dict | None = None,
) -> dict:
    """Send a plain text message to one chat (optional forum topic)."""
    tg = telegram_config(settings)
    token = (tg.get("bot_token") or "").strip()
    cid = (chat_id or "").strip()
    if not token or not cid:
        return {"ok": False, "error": "telegram_not_configured"}
    body_text = clip_caption(text, TELEGRAM_TEXT_MAX)
    data: dict[str, Any] = {"chat_id": str(cid), "text": body_text, "disable_web_page_preview": "true"}
    tid = normalize_thread_id(message_thread_id)
    if tid is not None:
        data["message_thread_id"] = str(tid)
    try:
        with _client(tg, timeout=30.0) as client:
            resp = client.post(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
            if resp.status_code >= 400:
                return {"ok": False, "error": _api_error(resp)}
            body = resp.json()
            if not body.get("ok"):
                return {"ok": False, "error": body.get("description") or "sendMessage_failed"}
            return {"ok": True, "message_id": (body.get("result") or {}).get("message_id")}
    except Exception as exc:
        log.exception("telegram sendMessage failed")
        return {"ok": False, "error": str(exc)}


def _send_document_parts(
    *,
    client: httpx.Client,
    token: str,
    chat_id: str,
    message_thread_id: int | None,
    path: Path,
    parts: int,
    max_part: int,
    full_caption: str,
) -> dict:
    sent: list[dict] = []
    with path.open("rb") as fh:
        for idx in range(parts):
            chunk = fh.read(max_part)
            if not chunk:
                break
            part_name = path.name if parts == 1 else f"{path.name}.{idx + 1:03d}-of-{parts:03d}"
            if parts == 1:
                caption = full_caption
            elif idx == 0:
                caption = clip_caption(f"{full_caption}\n\n({idx + 1}/{parts})")
            else:
                caption = f"{path.name} ({idx + 1}/{parts})"
            mime = mimetypes.guess_type(part_name)[0] or "application/zip"
            files = {"document": (part_name, BytesIO(chunk), mime)}
            data: dict[str, Any] = {
                "chat_id": str(chat_id),
                "caption": caption,
                "disable_content_type_detection": "true",
            }
            tid = normalize_thread_id(message_thread_id)
            if tid is not None:
                data["message_thread_id"] = str(tid)
            resp = client.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                data=data,
                files=files,
            )
            if resp.status_code >= 400:
                return {
                    "ok": False,
                    "error": _api_error(resp),
                    "sent_parts": len(sent),
                    "total_parts": parts,
                }
            body = resp.json()
            if not body.get("ok"):
                return {
                    "ok": False,
                    "error": body.get("description") or "sendDocument_failed",
                    "sent_parts": len(sent),
                    "total_parts": parts,
                }
            msg = body.get("result") or {}
            sent.append(
                {
                    "part": idx + 1,
                    "name": part_name,
                    "bytes": len(chunk),
                    "message_id": msg.get("message_id"),
                    "chat_id": str(chat_id),
                    "message_thread_id": tid,
                }
            )
    return {"ok": True, "sent": sent, "parts": parts}


def send_backup_to_telegram(
    path: Path,
    *,
    manifest: dict | None = None,
    settings: dict | None = None,
) -> dict:
    """Send a single on-disk zip as a Telegram document to all destinations."""
    tg = telegram_config(settings)
    token = (tg.get("bot_token") or "").strip()
    destinations = resolve_destinations(tg)
    if not token or not destinations:
        return {"ok": False, "error": "telegram_not_configured"}
    if not path.is_file():
        return {"ok": False, "error": "file_missing"}

    max_part = int(tg.get("max_part_bytes") or TELEGRAM_BOT_MAX_BYTES)
    max_part = max(1024 * 1024, min(max_part, TELEGRAM_BOT_MAX_BYTES))
    size = path.stat().st_size
    if size <= 0:
        return {"ok": False, "error": "file_empty"}
    parts = max(1, math.ceil(size / max_part))
    counts = (manifest or {}).get("counts") or {}
    base_ctx = {
        "date": (manifest or {}).get("created_at") or "",
        "size": human_size(size),
        "db_type": (manifest or {}).get("db_type") or "",
        "users": counts.get("users") if counts.get("users") is not None else "-",
        "nodes": counts.get("nodes") if counts.get("nodes") is not None else "-",
        "status": "ok",
        "filename": path.name,
        "parts": f"{parts} part(s)" if parts > 1 else "1 part",
    }
    full_caption = clip_caption(format_caption(tg.get("caption_template"), base_ctx))

    all_sent: list[dict] = []
    dest_results: list[dict] = []
    try:
        with _client(tg, timeout=300.0) as client:
            for dest in destinations:
                # Re-open file for each destination (parts read is sequential).
                result = _send_document_parts(
                    client=client,
                    token=token,
                    chat_id=str(dest["chat_id"]),
                    message_thread_id=dest.get("message_thread_id"),
                    path=path,
                    parts=parts,
                    max_part=max_part,
                    full_caption=full_caption,
                )
                dest_results.append({
                    "chat_id": dest["chat_id"],
                    "message_thread_id": dest.get("message_thread_id"),
                    "ok": bool(result.get("ok")),
                    "error": result.get("error"),
                })
                if result.get("ok"):
                    all_sent.extend(result.get("sent") or [])
                else:
                    # Continue other destinations; report partial failure.
                    log.warning(
                        "telegram send to %s failed: %s",
                        dest.get("chat_id"),
                        result.get("error"),
                    )
        ok_any = any(d.get("ok") for d in dest_results)
        return {
            "ok": ok_any,
            "parts": parts,
            "sent": all_sent,
            "destinations": dest_results,
            "kept_as_single_file": True,
            "path": str(path),
            "error": None if ok_any else (dest_results[0].get("error") if dest_results else "send_failed"),
        }
    except Exception as exc:
        log.exception("telegram sendDocument failed")
        return {
            "ok": False,
            "error": str(exc),
            "sent": all_sent,
            "destinations": dest_results,
            "total_parts": parts,
        }


def maybe_auto_send_backup(
    backup_result: dict,
    *,
    settings: dict | None = None,
) -> dict | None:
    """
    After a successful web-panel (manual) backup, send to Telegram when configured.
    Returns send result dict, or None when skipped.
    """
    if (backup_result or {}).get("status") != "success":
        return None
    trigger = (backup_result or {}).get("trigger") or ""
    if trigger != "manual":
        return None
    cfg = settings or load_settings()
    tg = telegram_config(cfg)
    if not telegram_ready(tg):
        return None
    from app.services.backup_engine import resolve_backup_path

    path = resolve_backup_path(str(backup_result.get("backup_id") or ""))
    if not path:
        return {"ok": False, "error": "backup_path_missing", "skipped": False}
    result = send_backup_to_telegram(
        path,
        manifest=backup_result.get("manifest") or {},
        settings=cfg,
    )
    if not result.get("ok"):
        log.warning("auto telegram send failed: %s", result.get("error"))
    return result
