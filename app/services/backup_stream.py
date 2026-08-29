"""Server-to-server backup streaming (source push → destination wizard receive)."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from app.config import UPLOAD_DIR, WORK_DIR

_LOCK = threading.RLock()
_LISTENERS: dict[str, dict[str, Any]] = {}

LISTENER_TTL_SEC = 30 * 60


def _purge_expired() -> None:
    now = time.time()
    dead = [k for k, v in _LISTENERS.items() if float(v.get("expires_at") or 0) < now]
    for k in dead:
        info = _LISTENERS.pop(k, None)
        partial = (info or {}).get("partial_path")
        if partial:
            try:
                Path(partial).unlink(missing_ok=True)
            except OSError:
                pass


def create_listener(*, label: str | None = None) -> dict:
    """Destination side: create a one-time receive token."""
    with _LOCK:
        _purge_expired()
        token = secrets.token_urlsafe(24)
        info = {
            "token": token,
            "label": label or "",
            "created_at": time.time(),
            "expires_at": time.time() + LISTENER_TTL_SEC,
            "status": "listening",  # listening | receiving | ready | error | consumed
            "bytes_received": 0,
            "expected_sha256": None,
            "filename": None,
            "upload_id": None,
            "error": None,
            "partial_path": None,
        }
        _LISTENERS[token] = info
        return dict(info)


def get_listener(token: str) -> dict | None:
    with _LOCK:
        _purge_expired()
        info = _LISTENERS.get(token)
        return dict(info) if info else None


def mark_listener_consumed(token: str) -> None:
    with _LOCK:
        info = _LISTENERS.get(token)
        if info:
            info["status"] = "consumed"


async def receive_stream(
    token: str,
    request_stream,
    *,
    filename: str | None = None,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
) -> dict:
    """Consume an ASGI/Starlette request body stream into an upload zip."""
    with _LOCK:
        _purge_expired()
        info = _LISTENERS.get(token)
        if not info:
            raise FileNotFoundError("listener_not_found")
        if float(info["expires_at"]) < time.time():
            raise TimeoutError("listener_expired")
        if info["status"] not in ("listening", "receiving"):
            raise RuntimeError(f"listener_status_{info['status']}")
        info["status"] = "receiving"
        info["expected_sha256"] = expected_sha256
        info["filename"] = filename or "streamed-backup.zip"

    upload_id = secrets.token_hex(8)
    dest_dir = UPLOAD_DIR / upload_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "backup.zip"
    dest = dest_dir / safe_name
    partial = dest.with_suffix(".partial")
    hasher = hashlib.sha256()
    received = 0

    with _LOCK:
        if token in _LISTENERS:
            _LISTENERS[token]["partial_path"] = str(partial)
            _LISTENERS[token]["upload_id"] = upload_id

    try:
        with partial.open("wb") as fh:
            async for chunk in request_stream:
                if not chunk:
                    continue
                fh.write(chunk)
                hasher.update(chunk)
                received += len(chunk)
                with _LOCK:
                    if token in _LISTENERS:
                        _LISTENERS[token]["bytes_received"] = received
                if expected_size and received > expected_size + 1024:
                    raise RuntimeError("stream_too_large")

        digest = hasher.hexdigest()
        if expected_sha256 and digest.lower() != expected_sha256.lower():
            raise RuntimeError("checksum_mismatch")
        if expected_size is not None and received != expected_size:
            raise RuntimeError(f"size_mismatch:{received}:{expected_size}")
        if received < 64:
            raise RuntimeError("empty_stream")

        os.replace(partial, dest)
        meta = {
            "upload_id": upload_id,
            "filename": info.get("filename") or safe_name,
            "size_bytes": received,
            "sha256": digest,
            "source": "stream",
            "received_at": time.time(),
        }
        (dest_dir / "stream_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

        with _LOCK:
            if token in _LISTENERS:
                _LISTENERS[token].update({
                    "status": "ready",
                    "bytes_received": received,
                    "upload_id": upload_id,
                    "error": None,
                    "partial_path": None,
                    "sha256": digest,
                })
        return {"ok": True, "upload_id": upload_id, "size_bytes": received, "sha256": digest}
    except Exception as exc:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        with _LOCK:
            if token in _LISTENERS:
                _LISTENERS[token]["status"] = "error"
                _LISTENERS[token]["error"] = str(exc)
        raise


def push_backup_file(
    path: Path,
    *,
    dest_base_url: str,
    token: str,
    sha256: str | None = None,
    timeout: float = 3600.0,
) -> dict:
    """Source side: stream a local zip to destination /api/stream/receive/{token}."""
    if not path.is_file():
        return {"ok": False, "error": "file_missing"}
    base = dest_base_url.rstrip("/")
    url = f"{base}/api/stream/receive/{token}"
    size = path.stat().st_size
    if not sha256:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        sha256 = h.hexdigest()

    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Length": str(size),
        "X-Backup-Filename": path.name,
        "X-Backup-Sha256": sha256,
        "X-Backup-Size": str(size),
    }
    try:
        with path.open("rb") as fh, httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.put(url, content=fh, headers=headers)
        if resp.status_code >= 400:
            return {"ok": False, "error": resp.text[:800], "status_code": resp.status_code}
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text[:400]}
        return {"ok": True, "response": body, "sha256": sha256, "size_bytes": size}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
