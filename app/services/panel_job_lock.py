"""Shared busy-gate for panel-mutating jobs (migrate + PasarGuard restore).

In-memory gate (running job objects) plus an on-disk ``.panel_busy`` lock so the
backup scheduler / manual backup create can skip while restore/migrate runs.
Also tracks an incomplete-job marker so a crash mid-op surfaces a simple warning.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.config import BACKUP_HOME
from app.services.migrators.base import MigrationJob

JobKind = Literal["migrate", "restore"]

PANEL_BUSY_FILE = BACKUP_HOME / ".panel_busy"
INCOMPLETE_JOB_FILE = BACKUP_HOME / ".panel_job_incomplete"
BUSY_MAX_AGE_SEC = 6 * 3600


@dataclass
class RunningPanelJob:
    kind: JobKind
    job: MigrationJob


class PanelJobAlreadyRunning(RuntimeError):
    """Raised when migrate/restore would overlap another panel-mutating job."""

    def __init__(self, running: RunningPanelJob):
        self.running = running
        self.job = running.job
        self.kind = running.kind
        super().__init__(
            f"A {running.kind} job is already running "
            f"(job_id={running.job.job_id}, progress={running.job.progress}%). "
            "Wait for it to finish before starting another migrate/restore."
        )


class PanelBusyLocked(RuntimeError):
    """Raised when a manual backup is blocked by an active restore/migrate lock."""

    def __init__(self, info: dict[str, Any] | None = None):
        self.info = info or {}
        kind = self.info.get("kind") or "restore/migrate"
        super().__init__(
            f"A {kind} job is in progress — backup is paused until it finishes."
        )


def get_running_panel_job() -> RunningPanelJob | None:
    """Return the active migrate or restore job, if any."""
    from app.services.orchestrator import get_running_migration_job
    from app.services.pg_restore import get_running_restore_job

    mig = get_running_migration_job()
    if mig:
        return RunningPanelJob(kind="migrate", job=mig)
    rest = get_running_restore_job()
    if rest:
        return RunningPanelJob(kind="restore", job=rest)
    return None


def ensure_panel_idle() -> None:
    """Raise PanelJobAlreadyRunning if migrate or restore is in flight."""
    running = get_running_panel_job()
    if running:
        raise PanelJobAlreadyRunning(running)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we cannot signal it — treat as alive.
        return True
    except OSError:
        return False


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _write_json_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_panel_busy() -> dict[str, Any] | None:
    """Return the live busy lock payload, or None if absent/stale/dead."""
    info = _read_json_file(PANEL_BUSY_FILE)
    if not info:
        return None
    try:
        pid = int(info.get("pid") or 0)
        started = float(info.get("started_at") or 0)
    except (TypeError, ValueError):
        _clear_file(PANEL_BUSY_FILE)
        return None
    age = time.time() - started if started else BUSY_MAX_AGE_SEC + 1
    if age > BUSY_MAX_AGE_SEC or not _pid_alive(pid):
        _clear_file(PANEL_BUSY_FILE)
        return None
    return info


def is_panel_busy_locked() -> bool:
    return read_panel_busy() is not None


def acquire_panel_busy(*, kind: JobKind, job_id: str) -> None:
    """Write the on-disk busy lock (caller already passed ensure_panel_idle)."""
    payload = {
        "pid": os.getpid(),
        "kind": kind,
        "job_id": job_id,
        "started_at": time.time(),
    }
    _write_json_file(PANEL_BUSY_FILE, payload)


def release_panel_busy(*, job_id: str | None = None) -> None:
    info = _read_json_file(PANEL_BUSY_FILE)
    if not info:
        _clear_file(PANEL_BUSY_FILE)
        return
    if job_id and info.get("job_id") and info.get("job_id") != job_id:
        return
    if int(info.get("pid") or 0) not in (0, os.getpid()):
        # Another process owns it — leave alone unless stale.
        if read_panel_busy() is None:
            return
        return
    _clear_file(PANEL_BUSY_FILE)


def write_incomplete_job_marker(
    *,
    kind: JobKind,
    job_id: str,
    pre_restore_backup_id: str | None = None,
    pre_restore_path: str | None = None,
) -> None:
    payload = {
        "kind": kind,
        "job_id": job_id,
        "started_at": time.time(),
        "pre_restore_backup_id": pre_restore_backup_id,
        "pre_restore_path": pre_restore_path,
        "message": "Last panel operation did not finish cleanly.",
    }
    _write_json_file(INCOMPLETE_JOB_FILE, payload)


def clear_incomplete_job_marker(*, job_id: str | None = None) -> None:
    info = _read_json_file(INCOMPLETE_JOB_FILE)
    if job_id and info and info.get("job_id") and info.get("job_id") != job_id:
        return
    _clear_file(INCOMPLETE_JOB_FILE)


def read_incomplete_job_marker() -> dict[str, Any] | None:
    return _read_json_file(INCOMPLETE_JOB_FILE)


def incomplete_operation_status() -> dict[str, Any] | None:
    """Shape suitable for /api/info — None when no leftover marker."""
    info = read_incomplete_job_marker()
    if not info:
        return None
    return {
        "kind": info.get("kind"),
        "job_id": info.get("job_id"),
        "started_at": info.get("started_at"),
        "pre_restore_backup_id": info.get("pre_restore_backup_id"),
        "pre_restore_path": info.get("pre_restore_path"),
        "message": info.get("message")
        or "Last restore/migration did not finish cleanly.",
    }


def _clear_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def try_pre_restore_backup(log_fn=None) -> dict[str, Any] | None:
    """Take a safety backup before restore/migrate. Never raises; never blocks the job.

    Exempt from the panel busy lock (called before the lock is written).
    """
    def _log(msg: str) -> None:
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass

    try:
        from app.services.prerequisites import is_pasarguard_installed

        if not is_pasarguard_installed():
            _log("Pre-restore safety backup skipped: PasarGuard is not installed")
            return None
    except Exception as exc:
        _log(f"Pre-restore safety backup skipped: {exc}")
        return None

    try:
        from app.services.backup_engine import create_backup_bundle, resolve_backup_path

        result = create_backup_bundle(trigger="pre_restore")
        if (result or {}).get("status") != "success":
            _log(
                "Pre-restore safety backup failed "
                f"(continuing): {(result or {}).get('error') or 'unknown'}"
            )
            return None
        backup_id = result.get("backup_id")
        path = resolve_backup_path(str(backup_id or "")) if backup_id else None
        _log(
            "Pre-restore safety backup ready: "
            f"{result.get('filename') or backup_id}"
        )
        return {
            "backup_id": backup_id,
            "filename": result.get("filename"),
            "path": str(path) if path else None,
            "size_bytes": result.get("size_bytes"),
        }
    except Exception as exc:
        _log(f"Pre-restore safety backup failed (continuing): {exc}")
        return None
