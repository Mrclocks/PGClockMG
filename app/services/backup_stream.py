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

from app.config import UPLOAD_DIR
from app.services.backup_net import UnsafeDestinationError, normalize_stream_dest_url

_LOCK = threading.RLock()
_LISTENERS: dict[str, dict[str, Any]] = {}

LISTENER_TTL_SEC = 30 * 60
LISTENER_READY_TTL_SEC = 6 * 60 * 60  # keep ready listeners long enough for the UI
STREAM_CHUNK = 8 * 1024 * 1024  # 8 MiB — fewer syscalls for large zips
PROGRESS_EVERY = 1 * 1024 * 1024  # update listener progress every 1 MiB (smoother UI)
MAX_STREAM_BYTES = 20 * 1024 * 1024 * 1024  # 20 GiB hard cap even without expected_size
PUSH_JOB_TTL_SEC = 60 * 60
PUSH_JOB_MAX_FINISHED = 50
PUSH_RETRY_BACKOFF_SEC = (2, 4, 8)

_PUSH_JOBS: dict[str, dict[str, Any]] = {}
_PUSH_LOCK = threading.RLock()


def _prune_push_jobs() -> None:
    """Drop old finished push jobs (age > 1h or more than 50 finished)."""
    now = time.time()
    finished = [
        (k, v) for k, v in list(_PUSH_JOBS.items())
        if v.get("status") in ("success", "error")
    ]
    for key, job in finished:
        started = float(job.get("started_at") or 0)
        if started and (now - started) > PUSH_JOB_TTL_SEC:
            _PUSH_JOBS.pop(key, None)
    finished = sorted(
        (
            (k, v) for k, v in _PUSH_JOBS.items()
            if v.get("status") in ("success", "error")
        ),
        key=lambda kv: float(kv[1].get("started_at") or 0),
    )
    while len(finished) > PUSH_JOB_MAX_FINISHED:
        key, _ = finished.pop(0)
        _PUSH_JOBS.pop(key, None)


def get_push_job(job_id: str) -> dict | None:
    with _PUSH_LOCK:
        _prune_push_jobs()
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
        _prune_push_jobs()
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
        if not info:
            return None
        # Keep listening tokens alive while the UI polls (large transfers need headroom).
        if info.get("status") == "listening":
            info["expires_at"] = time.time() + LISTENER_TTL_SEC
        return dict(info)


def mark_listener_consumed(token: str) -> None:
    with _LOCK:
        info = _LISTENERS.get(token)
        if info:
            info["status"] = "consumed"


def _normalize_dest_url(dest_base_url: str) -> str:
    return normalize_stream_dest_url(dest_base_url)


class ListenerBusyError(RuntimeError):
    """Raised when a second writer tries to use a receiving listener."""

    def __init__(self):
        super().__init__("listener_busy")


async def receive_stream(
    token: str,
    request_stream,
    *,
    filename: str | None = None,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
) -> dict:
    """Consume an ASGI/Starlette request body stream into an upload zip."""
    stale_partial: str | None = None
    with _LOCK:
        _purge_expired()
        info = _LISTENERS.get(token)
        if not info:
            raise FileNotFoundError("listener_not_found")
        if float(info["expires_at"]) < time.time():
            raise TimeoutError("listener_expired")
        status = info.get("status")
        # Error must not burn the token — allow retry until expiry.
        # Concurrent writers on a receiving listener are rejected (409).
        if status == "receiving":
            raise ListenerBusyError()
        if status not in ("listening", "error"):
            raise RuntimeError(f"listener_status_{status}")
        stale_partial = info.get("partial_path")
        info["status"] = "receiving"
        info["error"] = None
        info["expected_sha256"] = expected_sha256
        info["expected_size"] = expected_size
        info["filename"] = filename or "streamed-backup.zip"
        info["partial_path"] = None
        info["upload_id"] = None
        info["bytes_received"] = 0

    if stale_partial:
        try:
            Path(stale_partial).unlink(missing_ok=True)
        except OSError:
            pass

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
                            _LISTENERS[token]["expires_at"] = time.time() + LISTENER_TTL_SEC
                if expected_size and received > expected_size + (1024 * 1024):
                    raise RuntimeError("stream_too_large")

        # final progress
        with _LOCK:
            if token in _LISTENERS:
                _LISTENERS[token]["bytes_received"] = received
                _LISTENERS[token]["expires_at"] = time.time() + LISTENER_TTL_SEC

        digest = hasher.hexdigest()
        if expected_sha256 and digest.lower() != expected_sha256.lower():
            raise RuntimeError("checksum_mismatch")
        if expected_size is not None and received != expected_size:
            raise RuntimeError(f"size_mismatch:{received}:{expected_size}")
        if received < 64:
            raise RuntimeError("empty_stream")

        os.replace(partial, dest)

        # Fail at transfer time if the zip is corrupt / missing required members —
        # do not mark ready and wait for restore analyze.
        from app.services.backup_engine import verify_backup_archive

        verified = verify_backup_archive(dest, expected_sha256=digest)
        if not verified.get("ok"):
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError(f"stream_zip_invalid:{verified.get('error') or 'verify_failed'}")

        meta = {
            "upload_id": upload_id,
            "filename": info.get("filename") or safe_name,
            "size_bytes": received,
            "sha256": digest,
            "source": "stream",
            "received_at": time.time(),
            "integrity": {
                "ok": True,
                "crc_ok": bool(verified.get("crc_ok")),
                "members": verified.get("members"),
            },
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
                    # Ready stays discoverable longer so the UI does not show "not found".
                    "expires_at": time.time() + LISTENER_READY_TTL_SEC,
                })
        return {"ok": True, "upload_id": upload_id, "size_bytes": received, "sha256": digest}
    except Exception as exc:
        # Drop the whole receive dir (partial + any verified dest) so retries start clean.
        try:
            shutil.rmtree(dest_dir, ignore_errors=True)
        except Exception:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
        with _LOCK:
            if token in _LISTENERS:
                _LISTENERS[token]["status"] = "error"
                _LISTENERS[token]["error"] = str(exc)
                _LISTENERS[token]["partial_path"] = None
                _LISTENERS[token]["upload_id"] = None
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


def _http_error_payload(resp: httpx.Response) -> dict:
    text = (resp.text or "")[:400]
    detail = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            detail = str(body.get("detail") or body.get("error") or "")[:200]
    except Exception:
        detail = ""
    if not detail:
        detail = text.strip().replace("\n", " ")[:200] or "http_error"
    code = f"http_{resp.status_code}:{detail}"
    return {
        "ok": False,
        "error": code,
        "status_code": resp.status_code,
        "response_text": text,
    }


def _is_retryable_network_error(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.RemoteProtocolError,
            httpx.ReadError,
            httpx.WriteError,
        ),
    )


def _is_retryable_http_status(status_code: int) -> bool:
    """409 = destination still briefly ``receiving`` after a dropped connection."""
    return int(status_code) == 409


def _notify_retry_progress(progress_cb: Any | None, size: int) -> None:
    if not progress_cb:
        return
    try:
        progress_cb(0, size, phase="connecting")
    except TypeError:
        try:
            progress_cb(0, size)
        except Exception:
            pass


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
    tok = token.strip()
    url = f"{base}/api/stream/receive/{tok}"
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

    timeout_cfg = httpx.Timeout(timeout, connect=30.0, read=timeout, write=timeout)
    limits = httpx.Limits(max_connections=2, max_keepalive_connections=0)
    last_error: str | None = None

    # Initial attempt + up to 3 retries with 2s / 4s / 8s backoff.
    # Do NOT probe /api/stream/status (auth-gated on older builds / unnecessary):
    # just re-PUT; receive answers 404/410/409 clearly.
    for attempt in range(1 + len(PUSH_RETRY_BACKOFF_SEC)):
        try:
            with httpx.Client(timeout=timeout_cfg, follow_redirects=False, limits=limits) as client:
                resp = client.put(url, content=_iter_file(), headers=headers)
            if resp.status_code >= 400:
                if (
                    _is_retryable_http_status(resp.status_code)
                    and attempt < len(PUSH_RETRY_BACKOFF_SEC)
                ):
                    last_error = _http_error_payload(resp).get("error")
                    _notify_retry_progress(progress_cb, size)
                    time.sleep(PUSH_RETRY_BACKOFF_SEC[attempt])
                    continue
                return _http_error_payload(resp)
            try:
                body = resp.json()
            except Exception:
                body = {"raw": (resp.text or "")[:400]}
            return {"ok": True, "response": body, "sha256": sha256, "size_bytes": size, "dest": url}
        except Exception as exc:
            if not _is_retryable_network_error(exc):
                return {"ok": False, "error": str(exc)}
            last_error = str(exc)
            if attempt >= len(PUSH_RETRY_BACKOFF_SEC):
                break
            _notify_retry_progress(progress_cb, size)
            time.sleep(PUSH_RETRY_BACKOFF_SEC[attempt])

    return {"ok": False, "error": last_error or "network_error"}


def start_create_and_stream_async(
    *,
    dest_base_url: str,
    token: str,
) -> dict:
    """Create a fresh backup then push it — one job the UI can poll end-to-end.

    Existing create / stream-send endpoints remain unchanged; this only adds a
    combined path. Failures during create never delete older backups.
    """
    # Validate destination before starting create (fail fast, no orphan work).
    try:
        dest = normalize_stream_dest_url(dest_base_url)
    except UnsafeDestinationError:
        raise
    except ValueError as exc:
        raise UnsafeDestinationError(str(exc)) from exc
    tok = (token or "").strip()
    if not tok:
        raise ValueError("token_missing")

    job_id = secrets.token_hex(6)
    job = {
        "job_id": job_id,
        "status": "queued",  # queued | creating | connecting | sending | success | error
        "phase": "queued",
        "bytes_sent": 0,
        "bytes_total": 0,
        "error": None,
        "result": None,
        "backup_id": None,
        "filename": None,
        "started_at": time.time(),
    }
    with _PUSH_LOCK:
        _prune_push_jobs()
        _PUSH_JOBS[job_id] = job

    def _run() -> None:
        from app.services.backup_engine import create_backup_bundle, resolve_backup_path

        def on_progress(sent: int, total: int, *, phase: str = "sending") -> None:
            with _PUSH_LOCK:
                j = _PUSH_JOBS.get(job_id)
                if not j:
                    return
                j["status"] = phase
                j["phase"] = phase
                j["bytes_sent"] = int(sent)
                j["bytes_total"] = int(total)

        try:
            with _PUSH_LOCK:
                j = _PUSH_JOBS.get(job_id) or job
                j["status"] = "creating"
                j["phase"] = "creating"
                _PUSH_JOBS[job_id] = j

            created = create_backup_bundle(trigger="manual+stream")
            if (created or {}).get("status") != "success":
                with _PUSH_LOCK:
                    j = _PUSH_JOBS.get(job_id) or job
                    j["status"] = "error"
                    j["phase"] = "creating"
                    j["error"] = (created or {}).get("error") or "backup_create_failed"
                    j["result"] = {"create": created}
                    _PUSH_JOBS[job_id] = j
                return

            backup_id = created.get("backup_id")
            path = resolve_backup_path(str(backup_id or ""))
            if not path:
                with _PUSH_LOCK:
                    j = _PUSH_JOBS.get(job_id) or job
                    j["status"] = "error"
                    j["error"] = "backup_file_missing_after_create"
                    j["result"] = {"create": created}
                    _PUSH_JOBS[job_id] = j
                return

            sha = None
            sidecar = path.with_suffix(path.suffix + ".json")
            if sidecar.is_file():
                try:
                    meta = json.loads(sidecar.read_text(encoding="utf-8"))
                    sha = (meta or {}).get("sha256")
                except Exception:
                    sha = None

            with _PUSH_LOCK:
                j = _PUSH_JOBS.get(job_id) or job
                j["backup_id"] = backup_id
                j["filename"] = path.name
                j["bytes_total"] = int(path.stat().st_size) if path.is_file() else 0
                j["status"] = "connecting"
                j["phase"] = "connecting"
                j["result"] = {"create": {
                    "backup_id": backup_id,
                    "filename": path.name,
                    "size_bytes": created.get("size_bytes"),
                }}
                _PUSH_JOBS[job_id] = j

            result = push_backup_file(
                path,
                dest_base_url=dest,
                token=tok,
                sha256=sha,
                progress_cb=on_progress,
            )
            with _PUSH_LOCK:
                j = _PUSH_JOBS.get(job_id) or job
                if result.get("ok"):
                    j["status"] = "success"
                    j["phase"] = "success"
                    j["bytes_sent"] = int(result.get("size_bytes") or j.get("bytes_total") or 0)
                    create_meta = (j.get("result") or {}).get("create") or {}
                    j["result"] = {"create": create_meta, "stream": result}
                    j["error"] = None
                else:
                    j["status"] = "error"
                    j["phase"] = "sending"
                    j["error"] = result.get("error") or "stream_failed"
                    create_meta = (j.get("result") or {}).get("create") or {}
                    j["result"] = {"create": create_meta, "stream": result}
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

    threading.Thread(target=_run, daemon=True, name=f"create-stream-{job_id}").start()
    return {
        "job_id": job_id,
        "status": "queued",
        "phase": "queued",
        "bytes_total": 0,
    }
