"""Telegram delivery for backup zips (optional proxy, chunked when > ~50MB)."""

from __future__ import annotations

import math
import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.config import TELEGRAM_BOT_MAX_BYTES
from app.services.backup_settings import DEFAULT_TELEGRAM_CAPTION, load_settings


def _proxy_url(tg: dict) -> str | None:
    if not tg.get("proxy_enabled"):
        return None
    host = (tg.get("proxy_host") or "").strip()
    port = int(tg.get("proxy_port") or 0)
    if not host or not (1 <= port <= 65535):
        return None
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
    kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": True}
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


def human_size(n: int | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def telegram_config(settings: dict | None = None) -> dict:
    cfg = settings or load_settings()
    return dict(cfg.get("telegram") or {})


def test_telegram_connection(settings: dict | None = None) -> dict:
    tg = telegram_config(settings)
    token = (tg.get("bot_token") or "").strip()
    chat_id = (tg.get("chat_id") or "").strip()
    if not token:
        return {"ok": False, "error": "bot_token_missing"}
    if not chat_id:
        return {"ok": False, "error": "chat_id_missing"}
    try:
        with _client(tg, timeout=30.0) as client:
            me = client.get(f"https://api.telegram.org/bot{token}/getMe")
            me.raise_for_status()
            me_data = me.json()
            if not me_data.get("ok"):
                return {"ok": False, "error": me_data.get("description") or "getMe_failed"}
            caption = format_caption(
                tg.get("caption_template"),
                {
                    "date": "test",
                    "size": "0 B",
                    "db_type": "n/a",
                    "users": "-",
                    "nodes": "-",
                    "status": "connection test",
                    "filename": "-",
                    "parts": "0/0",
                },
            )
            send = client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": caption},
            )
            send.raise_for_status()
            send_data = send.json()
            if not send_data.get("ok"):
                return {"ok": False, "error": send_data.get("description") or "sendMessage_failed", "bot": me_data.get("result")}
            return {"ok": True, "bot": me_data.get("result"), "message_id": (send_data.get("result") or {}).get("message_id")}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def send_backup_to_telegram(
    path: Path,
    *,
    manifest: dict | None = None,
    settings: dict | None = None,
) -> dict:
    """Send a single on-disk zip; split into parts only for Telegram upload."""
    tg = telegram_config(settings)
    if not tg.get("enabled") and not (tg.get("bot_token") and tg.get("chat_id")):
        return {"ok": False, "error": "telegram_disabled"}
    token = (tg.get("bot_token") or "").strip()
    chat_id = (tg.get("chat_id") or "").strip()
    if not token or not chat_id:
        return {"ok": False, "error": "telegram_not_configured"}
    if not path.is_file():
        return {"ok": False, "error": "file_missing"}

    max_part = int(tg.get("max_part_bytes") or TELEGRAM_BOT_MAX_BYTES)
    max_part = max(1024 * 1024, min(max_part, TELEGRAM_BOT_MAX_BYTES))
    size = path.stat().st_size
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

    sent: list[dict] = []
    try:
        with _client(tg, timeout=300.0) as client:
            # intro caption
            intro = format_caption(tg.get("caption_template"), base_ctx)
            intro_resp = client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": intro},
            )
            if intro_resp.status_code >= 400:
                return {"ok": False, "error": intro_resp.text[:500]}

            with path.open("rb") as fh:
                for idx in range(parts):
                    chunk = fh.read(max_part)
                    if not chunk:
                        break
                    part_name = path.name if parts == 1 else f"{path.name}.{idx + 1:03d}-of-{parts:03d}"
                    caption = f"{path.name} ({idx + 1}/{parts})" if parts > 1 else path.name
                    files = {
                        "document": (part_name, chunk, mimetypes.guess_type(part_name)[0] or "application/zip"),
                    }
                    data = {"chat_id": chat_id, "caption": caption}
                    resp = client.post(
                        f"https://api.telegram.org/bot{token}/sendDocument",
                        data=data,
                        files=files,
                    )
                    if resp.status_code >= 400:
                        return {
                            "ok": False,
                            "error": resp.text[:800],
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
                    sent.append({"part": idx + 1, "name": part_name, "bytes": len(chunk)})
        return {
            "ok": True,
            "parts": parts,
            "sent": sent,
            "kept_as_single_file": True,
            "path": str(path),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "sent_parts": len(sent), "total_parts": parts}
