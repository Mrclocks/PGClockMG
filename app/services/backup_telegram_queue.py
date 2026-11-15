"""Persistent retry queue for Telegram backup uploads.

Telegram is often unreachable for hours. When an upload fails, the backup file is
queued here and retried with backoff until it is delivered (or it expires), so a
temporary block no longer leaves the newest backup only on local disk. The queue
never affects backup creation; failures here are logged and swallowed.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from app.config import BACKUP_PANEL_DIR

log = logging.getLogger("pgclockmg.backup_telegram_queue")

QUEUE_FILE = BACKUP_PANEL_DIR / "telegram_pending.json"
_LOCK = threading.RLock()

BACKOFF_SEC = (10 * 60, 30 * 60, 60 * 60, 2 * 3600, 4 * 3600, 6 * 3600)
MAX_AGE_SEC = 48 * 3600
MAX_KEEP = 1  # only the newest backup matters: it supersedes older undelivered ones


def _read() -> list[dict]:
    try:
        data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write(items: list[dict]) -> None:
    try:
        QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = QUEUE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, QUEUE_FILE)
    except OSError as exc:
        log.warning("could not persist telegram queue: %s", exc)


def next_delay(attempts: int) -> int:
    return BACKOFF_SEC[min(max(attempts, 0), len(BACKOFF_SEC) - 1)]


def enqueue(backup_id: str, error: str | None = None, destinations: list | None = None) -> None:
    """Queue ``backup_id``; ``destinations`` (when given) limits retries to those chats."""
    if not backup_id:
        return
    with _LOCK:
        items = [i for i in _read() if i.get("backup_id") != backup_id]
        now = time.time()
        item = {
            "backup_id": backup_id,
            "queued_at": now,
            "attempts": 0,
            "next_try_at": now + next_delay(0),
            "last_error": (error or "")[:300],
        }
        if destinations:
            item["destinations"] = list(destinations)
        items.append(item)
        _write(items[-MAX_KEEP:])


def dequeue(backup_id: str) -> None:
    with _LOCK:
        items = _read()
        kept = [i for i in items if i.get("backup_id") != backup_id]
        if len(kept) != len(items):
            _write(kept)


def record_delivery(backup_id: str, result: dict | None) -> None:
    """Sync the queue with a send result: delivered everywhere → drop; otherwise queue
    only what is still missing (all chats on a total failure, the failed ones on a partial)."""
    if not backup_id or result is None:
        return
    try:
        failed = list(result.get("failed_destinations") or [])
        if result.get("ok") and not failed:
            dequeue(backup_id)
        else:
            enqueue(backup_id, str(result.get("error") or "partial_delivery"), destinations=failed or None)
    except Exception:  # noqa: BLE001 — never let queue bookkeeping break a backup
        log.exception("telegram queue bookkeeping failed")


def pending() -> list[dict]:
    with _LOCK:
        return _read()


def process_due(send_fn, resolve_path_fn, *, now: float | None = None) -> dict:
    """Try every due item once. ``send_fn(path, manifest) -> dict`` returns ``{"ok": bool}``."""
    current = time.time() if now is None else now
    out = {"sent": 0, "failed": 0, "expired": [], "attempted": 0}
    with _LOCK:
        items = _read()
    if not items:
        return out
    keep: list[dict] = []
    for item in items:
        backup_id = str(item.get("backup_id") or "")
        path = resolve_path_fn(backup_id)
        if not path or not Path(path).is_file():
            out["expired"].append(backup_id)  # file gone (retention) — nothing to send
            continue
        if current - float(item.get("queued_at") or current) > MAX_AGE_SEC:
            out["expired"].append(backup_id)
            continue
        if float(item.get("next_try_at") or 0) > current:
            keep.append(item)
            continue
        out["attempted"] += 1
        try:
            result = send_fn(Path(path), item)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": str(exc)}
        failed_dests = list(result.get("failed_destinations") or [])
        if result.get("ok") and not failed_dests:
            out["sent"] += 1
            continue
        if result.get("ok"):
            # delivered to some chats — keep retrying only the missing ones
            out["sent"] += 1
            item = {**item, "destinations": failed_dests}
        out["failed"] += 1
        attempts = int(item.get("attempts") or 0) + 1
        item = {
            **item,
            "attempts": attempts,
            "next_try_at": current + next_delay(attempts),
            "last_error": str(result.get("error") or "")[:300],
        }
        keep.append(item)
    with _LOCK:
        # keep anything enqueued while we were sending
        orig_ids = {i.get("backup_id") for i in items}
        extra = [i for i in _read() if i.get("backup_id") not in orig_ids]
        _write((keep + extra)[-MAX_KEEP:])
    return out
