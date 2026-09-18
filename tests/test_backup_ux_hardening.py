"""Tests for backup reliability helpers: stream LAN URLs, disk preflight, TTL."""

from __future__ import annotations

import time
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.backup_net import (
    UnsafeDestinationError,
    normalize_public_http_url,
    normalize_stream_dest_url,
)
from app.services.backup_engine import (
    assert_enough_disk_for_backup,
    required_free_bytes_for_backup,
)


def test_stream_url_allows_private_and_localhost():
    assert normalize_stream_dest_url("http://10.0.0.5:7000") == "http://10.0.0.5:7000"
    assert normalize_stream_dest_url("http://192.168.1.10:7000/") == "http://192.168.1.10:7000"
    assert normalize_stream_dest_url("http://127.0.0.1:7000") == "http://127.0.0.1:7000"
    assert normalize_stream_dest_url("http://localhost:7000") == "http://localhost:7000"


def test_stream_url_still_blocks_metadata():
    with pytest.raises(UnsafeDestinationError):
        normalize_stream_dest_url("http://169.254.169.254/latest/meta-data")
    with pytest.raises(UnsafeDestinationError):
        normalize_stream_dest_url("http://metadata.google.internal")


def test_webhook_public_url_still_blocks_private():
    with pytest.raises(UnsafeDestinationError):
        normalize_public_http_url("http://10.0.0.5:7000")
    with pytest.raises(UnsafeDestinationError):
        normalize_public_http_url("http://127.0.0.1:7000")


def test_required_free_bytes_scales_with_estimate():
    small = required_free_bytes_for_backup(10 * 1024 * 1024)
    # Modest panels must not demand a hard 1 GiB floor.
    assert small >= 256 * 1024 * 1024
    assert small < 1024 * 1024 * 1024
    large = required_free_bytes_for_backup(2 * 1024 * 1024 * 1024)
    assert large >= 2 * 1024 * 1024 * 1024 * 2


def test_disk_preflight_fails_when_free_is_tiny(tmp_path, monkeypatch):
    import app.services.backup_engine as eng

    monkeypatch.setattr(eng, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(eng, "BACKUP_DIR", tmp_path / "bak")
    monkeypatch.setattr(eng, "estimate_backup_source_bytes", lambda: 2 * 1024 * 1024 * 1024)

    class FakeUsage:
        free = 100 * 1024 * 1024  # 100 MiB

    monkeypatch.setattr(eng.shutil, "disk_usage", lambda _p: FakeUsage)
    with pytest.raises(RuntimeError, match="Not enough free disk"):
        assert_enough_disk_for_backup()


def test_listener_ttl_refreshed_while_listening():
    from app.services import backup_stream as stream

    stream._LISTENERS.clear()
    info = stream.create_listener()
    token = info["token"]
    with stream._LOCK:
        stream._LISTENERS[token]["expires_at"] = time.time() + 5
    before = stream._LISTENERS[token]["expires_at"]
    got = stream.get_listener(token)
    assert got["status"] == "listening"
    after = stream._LISTENERS[token]["expires_at"]
    assert after > before
    assert after >= time.time() + stream.LISTENER_TTL_SEC - 2


def test_create_and_stream_job_uses_create_then_push(monkeypatch, tmp_path):
    from app.services import backup_stream as stream

    zpath = tmp_path / "pgclockmg-demo.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr(".env", "X=1\n")
        zf.writestr("db.sqlite3", b"SQLite format 3\x00" + b"\x00" * 80)

    calls = {"create": 0, "push": 0}

    def fake_create(*, trigger="manual"):
        calls["create"] += 1
        assert trigger == "manual+stream"
        return {
            "status": "success",
            "backup_id": "demo",
            "filename": zpath.name,
            "size_bytes": zpath.stat().st_size,
        }

    def fake_resolve(backup_id):
        assert backup_id == "demo"
        return zpath

    def fake_push(path, *, dest_base_url, token, sha256=None, progress_cb=None):
        calls["push"] += 1
        assert path == zpath
        assert "10.1.2.3" in dest_base_url
        assert token == "tokentoken"
        if progress_cb:
            progress_cb(10, 100, phase="sending")
            progress_cb(100, 100, phase="sending")
        return {"ok": True, "sha256": "abc", "size_bytes": path.stat().st_size}

    monkeypatch.setattr("app.services.backup_engine.create_backup_bundle", fake_create)
    monkeypatch.setattr("app.services.backup_engine.resolve_backup_path", fake_resolve)
    monkeypatch.setattr(stream, "push_backup_file", fake_push)

    started = stream.start_create_and_stream_async(
        dest_base_url="http://10.1.2.3:7000",
        token="tokentoken",
    )
    job_id = started["job_id"]
    deadline = time.time() + 5
    while time.time() < deadline:
        job = stream.get_push_job(job_id)
        if job and job["status"] in ("success", "error"):
            break
        time.sleep(0.05)
    job = stream.get_push_job(job_id)
    assert job["status"] == "success", job
    assert calls == {"create": 1, "push": 1}
    assert job.get("backup_id") == "demo"
