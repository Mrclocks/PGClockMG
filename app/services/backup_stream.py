"""Server-to-server backup streaming (source push → destination wizard receive)."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from app.config import UPLOAD_DIR
from app.services.backup_net import UnsafeDestinationError, normalize_public_http_url

_LOCK = threading.RLock()
_LISTENERS: dict[str, dict[str, Any]] = {}

LISTENER_TTL_SEC = 30 * 60
STREAM_CHUNK = 8 * 1024 * 1024  # 8 MiB — fewer syscalls for large zips
PROGRESS_EVERY = 1 * 1024 * 1024  # update listener progress every 1 MiB (smoother UI)
MAX_STREAM_BYTES = 20 * 1024 * 1024 * 1024  # 20 GiB hard cap even without expected_size

_PUSH_JOBS: dict[str, dict[str, Any]] = {}
_PUSH_LOCK = threading.RLock()


def get_push_job(job_id: str) -> dict | None:
    with _PUSH_LOCK:
        job = _PUSH_JOBS.get(job_id)
        return dict(job) if job else None


def start_push_async(
    path: Path,
    *,
    dest_base_url: str,
    token: str,
    sha256: str | None = None,
) -> dict:
    """Background push so the backup UI can poll progress."""
    job_id = secrets.token_hex(6)
    size = path.stat().st_size if path.is_file() else 0
    job = {
        "job_id": job_id,
        "status": "queued",  # queued | connecting | sending | success | error
        "bytes_sent": 0,
        "bytes_total": size,
        "error": None,
        "result": None,
        "started_at": time.time(),
    }
    with _PUSH_LOCK:
        _PUSH_JOBS[job_id] = job

    def _run() -> None:
        def on_progress(sent: int, total: int, *, phase: str = "sending") -> None:
            with _PUSH_LOCK:
                j = _PUSH_JOBS.get(job_id)
                if not j:
                    return
                j["status"] = phase
                j["bytes_sent"] = int(sent)
                j["bytes_total"] = int(total)

        try:
            with _PUSH_LOCK:
                if job_id in _PUSH_JOBS:
                    _PUSH_JOBS[job_id]["status"] = "connecting"
            result = push_backup_file(
                path,
                dest_base_url=dest_base_url,
                token=token,
                sha256=sha256,
                progress_cb=on_progress,
            )
            with _PUSH_LOCK:
                j = _PUSH_JOBS.get(job_id) or job
                if result.get("ok"):
                    j["status"] = "success"
                    j["bytes_sent"] = int(result.get("size_bytes") or j.get("bytes_total") or 0)
                    j["result"] = result
                    j["error"] = None
                else:
                    j["status"] = "error"
                    j["error"] = result.get("error") or "stream_failed"
                    j["result"] = result
                _PUSH_JOBS[job_id] = j
        except UnsafeDestinationError as exc:
            with _PUSH_LOCK:
                j = _PUSH_JOBS.get(job_id) or job
                j["status"] = "error"
                j["error"] = str(exc)
                _PUSH_JOBS[job_id] = j
        except Exception as exc:
            with _PUSH_LOCK:
                j = _PUSH_JOBS.get(job_id) or job
                j["status"] = "error"
                j["error"] = str(exc)
                _PUSH_JOBS[job_id] = j

    threading.Thread(target=_run, daemon=True, name=f"stream-push-{job_id}").start()
    return {"job_id": job_id, "bytes_total": size, "status": "queued"}


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
            "expected_size": None,
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


def _normalize_dest_url(dest_base_url: str) -> str:
    return normalize_public_http_url(dest_base_url)


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
        info["expected_size"] = expected_size
        info["filename"] = filename or "streamed-backup.zip"

    upload_id = secrets.token_hex(8)
    dest_dir = UPLOAD_DIR / upload_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "backup.zip"
    dest = dest_dir / safe_name
    partial = dest.with_suffix(".partial")
    hasher = hashlib.sha256()
    received = 0
    last_progress = 0

    with _LOCK:
        if token in _LISTENERS:
            _LISTENERS[token]["partial_path"] = str(partial)
            _LISTENERS[token]["upload_id"] = upload_id

    try:
        # Larger buffer reduces write syscalls on big archives
        with partial.open("wb", buffering=STREAM_CHUNK) as fh:
            async for chunk in request_stream:
                if not chunk:
                    continue
                fh.write(chunk)
                hasher.update(chunk)
                received += len(chunk)
                if received > MAX_STREAM_BYTES:
                    raise RuntimeError("stream_too_large")
                if received - last_progress >= PROGRESS_EVERY or (expected_size and received >= expected_size):
                    last_progress = received
                    with _LOCK:
                        if token in _LISTENERS:
                            _LISTENERS[token]["bytes_received"] = received
                if expected_size and received > expected_size + (1024 * 1024):
                    raise RuntimeError("stream_too_large")

        # final progress
        with _LOCK:
            if token in _LISTENERS:
                _LISTENERS[token]["bytes_received"] = received

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


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(STREAM_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def push_backup_file(
    path: Path,
    *,
    dest_base_url: str,
    token: str,
    sha256: str | None = None,
    timeout: float = 3600.0,
    progress_cb: Any | None = None,
) -> dict:
    """Source side: stream a local zip to destination /api/stream/receive/{token}."""
    if not path.is_file():
        return {"ok": False, "error": "file_missing"}
    try:
        base = _normalize_dest_url(dest_base_url)
    except UnsafeDestinationError:
        raise
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not token or not str(token).strip():
        return {"ok": False, "error": "token_missing"}
    url = f"{base}/api/stream/receive/{token.strip()}"
    size = path.stat().st_size
    if size < 64:
        return {"ok": False, "error": "file_too_small"}
    if not sha256:
        sha256 = _file_sha256(path)

    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Length": str(size),
        "X-Backup-Filename": path.name,
        "X-Backup-Sha256": sha256,
        "X-Backup-Size": str(size),
        "Connection": "close",
    }

    def _iter_file():
        sent = 0
        last_report = 0
        if progress_cb:
            try:
                progress_cb(0, size, phase="connecting")
            except TypeError:
                progress_cb(0, size)
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(STREAM_CHUNK)
                if not chunk:
                    break
                sent += len(chunk)
                if progress_cb and (sent - last_report >= PROGRESS_EVERY or sent >= size):
                    last_report = sent
                    try:
                        progress_cb(sent, size, phase="sending")
                    except TypeError:
                        progress_cb(sent, size)
                yield chunk
        if progress_cb:
            try:
                progress_cb(size, size, phase="sending")
            except TypeError:
                progress_cb(size, size)

    try:
        timeout_cfg = httpx.Timeout(timeout, connect=30.0, read=timeout, write=timeout)
        limits = httpx.Limits(max_connections=2, max_keepalive_connections=0)
        with httpx.Client(timeout=timeout_cfg, follow_redirects=False, limits=limits) as client:
            # Generator body streams without loading the whole zip into RAM
            resp = client.put(url, content=_iter_file(), headers=headers)
        if resp.status_code >= 400:
            return {"ok": False, "error": "http_error", "status_code": resp.status_code}
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text[:400]}
        return {"ok": True, "response": body, "sha256": sha256, "size_bytes": size, "dest": url}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
