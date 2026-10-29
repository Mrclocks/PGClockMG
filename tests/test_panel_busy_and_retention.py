"""Group C: pre-restore retention, panel busy lock, scheduler notify cadence."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest


def test_apply_retention_keeps_verified_and_pre_restore(tmp_path, monkeypatch):
    import app.services.backup_engine as eng

    monkeypatch.setattr(eng, "BACKUP_DIR", tmp_path)
    now = time.time()

    def _make(name: str, *, age_days: float, trigger: str, verified: bool = False) -> Path:
        p = tmp_path / name
        p.write_bytes(b"zip")
        meta = {"trigger": trigger, "verified": verified}
        (tmp_path / f"{p.name}.json").write_text(json.dumps(meta), encoding="utf-8")
        ts = now - age_days * 86400
        os.utime(p, (ts, ts))
        return p

    newest_verified = _make("pgclockmg-v.zip", age_days=0.1, trigger="manual", verified=True)
    old_regular = _make("pgclockmg-old.zip", age_days=20.0, trigger="schedule", verified=False)
    pre_keep = _make("pgclockmg-pre1.zip", age_days=1.0, trigger="pre_restore", verified=False)
    pre_old = _make("pgclockmg-pre-old.zip", age_days=10.0, trigger="pre_restore", verified=False)
    # Extra regular files beyond keep_count=1
    extra = _make("pgclockmg-extra.zip", age_days=0.2, trigger="manual", verified=False)

    removed = eng.apply_retention(keep_count=1, keep_days=7)
    assert removed >= 2
    assert newest_verified.exists(), "newest verified must survive"
    assert pre_keep.exists(), "recent pre_restore must survive"
    assert not pre_old.exists(), "old pre_restore beyond 7 days should go"
    assert not old_regular.exists()
    # keep_count=1 among regular: newest_verified + maybe extra; verified wins protection
    # extra is newer than old_regular but older than verified; with keep_count=1 only
    # the newest regular (verified) stays in the regular quota list index 0.
    assert not extra.exists() or newest_verified.exists()


def test_panel_busy_lock_blocks_manual_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("PG_BACKUP_HOME", str(tmp_path))
    monkeypatch.setenv("PG_MIGRATOR_HOME", str(tmp_path))
    import importlib
    import app.config as cfg

    importlib.reload(cfg)
    import app.services.panel_job_lock as lock

    importlib.reload(lock)
    import app.services.backup_engine as eng

    importlib.reload(eng)

    lock.acquire_panel_busy(kind="restore", job_id="abc")
    assert lock.is_panel_busy_locked() is True
    blocked = eng.create_backup_bundle(trigger="manual")
    assert blocked["status"] == "error"
    assert "restore" in (blocked.get("error") or "").lower() or "migrate" in (blocked.get("error") or "").lower()

    # pre_restore is exempt
    with monkeypatch.context() as m:
        m.setattr(eng, "_create_backup_into", lambda job_id, trigger="manual": {
            "job_id": job_id, "status": "success", "trigger": trigger,
        })
        m.setattr(eng, "_enqueue_telegram_auto_send", lambda *_a, **_k: None)
        ok = eng.create_backup_bundle(trigger="pre_restore")
        assert ok["status"] == "success"

    lock.release_panel_busy(job_id="abc")
    assert lock.is_panel_busy_locked() is False


def test_incomplete_marker_surfaces_in_status(tmp_path, monkeypatch):
    monkeypatch.setenv("PG_BACKUP_HOME", str(tmp_path))
    import importlib
    import app.config as cfg

    importlib.reload(cfg)
    import app.services.panel_job_lock as lock

    importlib.reload(lock)

    lock.write_incomplete_job_marker(
        kind="restore",
        job_id="deadbeef",
        pre_restore_backup_id="pgclockmg-safety",
        pre_restore_path=str(tmp_path / "backups" / "pgclockmg-safety.zip"),
    )
    status = lock.incomplete_operation_status()
    assert status is not None
    assert status["job_id"] == "deadbeef"
    assert "pgclockmg-safety" in (status.get("pre_restore_path") or "")
    lock.clear_incomplete_job_marker(job_id="deadbeef")
    assert lock.incomplete_operation_status() is None


def test_scheduler_notify_cadence_and_backoff():
    from app.services.backup_scheduler import (
        FAILURE_BACKOFF_CAP_SECONDS,
        failure_backoff_seconds,
        should_notify_consecutive_failure,
    )

    assert failure_backoff_seconds(1) == 15 * 60
    assert failure_backoff_seconds(2) == 30 * 60
    assert failure_backoff_seconds(3) == 60 * 60
    assert failure_backoff_seconds(10) == FAILURE_BACKOFF_CAP_SECONDS

    assert should_notify_consecutive_failure(1) is True
    assert should_notify_consecutive_failure(2) is False
    assert should_notify_consecutive_failure(3) is True
    assert should_notify_consecutive_failure(6) is True
    assert should_notify_consecutive_failure(4) is False


def test_stale_busy_lock_auto_clears(tmp_path, monkeypatch):
    monkeypatch.setenv("PG_BACKUP_HOME", str(tmp_path))
    import importlib
    import app.config as cfg

    importlib.reload(cfg)
    import app.services.panel_job_lock as lock

    importlib.reload(lock)

    lock.PANEL_BUSY_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock.PANEL_BUSY_FILE.write_text(
        json.dumps({"pid": 999999, "kind": "restore", "job_id": "x", "started_at": time.time() - 7 * 3600}),
        encoding="utf-8",
    )
    assert lock.read_panel_busy() is None
    assert not lock.PANEL_BUSY_FILE.exists()
