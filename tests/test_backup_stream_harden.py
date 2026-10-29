"""Group B: stream receive retry, push errors, ready TTL, push-job prune."""

from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest


def _tiny_backup_zip(tmp_path: Path) -> Path:
    path = tmp_path / "ok.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(".env", "A=1\n")
        zf.writestr("db_backup.sql", "-- dump\n" + ("x" * 80))
    return path


@pytest.mark.asyncio
async def test_receive_stream_allows_retry_after_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PG_MIGRATOR_HOME", str(tmp_path))
    import importlib
    import app.config as cfg

    importlib.reload(cfg)
    import app.services.backup_stream as stream

    importlib.reload(stream)

    info = stream.create_listener()
    token = info["token"]

    async def _bad_body():
        yield b"tiny"

    with pytest.raises(RuntimeError):
        await stream.receive_stream(token, _bad_body())
    after = stream.get_listener(token)
    assert after is not None
    assert after["status"] == "error"

    good = _tiny_backup_zip(tmp_path).read_bytes()

    async def _good_body():
        yield good

    with patch("app.services.backup_engine.verify_backup_archive", return_value={"ok": True, "crc_ok": True}):
        result = await stream.receive_stream(token, _good_body())
    assert result["ok"] is True
    ready = stream.get_listener(token)
    assert ready["status"] == "ready"
    assert ready["upload_id"] == result["upload_id"]


@pytest.mark.asyncio
async def test_receive_stream_rejects_concurrent_receiving(tmp_path, monkeypatch):
    monkeypatch.setenv("PG_MIGRATOR_HOME", str(tmp_path))
    import importlib
    import app.config as cfg

    importlib.reload(cfg)
    import app.services.backup_stream as stream

    importlib.reload(stream)

    info = stream.create_listener()
    token = info["token"]
    with stream._LOCK:
        stream._LISTENERS[token]["status"] = "receiving"

    async def _body():
        yield b"x" * 100

    with pytest.raises(stream.ListenerBusyError):
        await stream.receive_stream(token, _body())


def test_ready_listener_ttl_is_six_hours(tmp_path, monkeypatch):
    monkeypatch.setenv("PG_MIGRATOR_HOME", str(tmp_path))
    import importlib
    import app.config as cfg

    importlib.reload(cfg)
    import app.services.backup_stream as stream

    importlib.reload(stream)

    info = stream.create_listener()
    token = info["token"]
    now = time.time()
    with stream._LOCK:
        stream._LISTENERS[token]["status"] = "ready"
        stream._LISTENERS[token]["upload_id"] = "abc123"
        stream._LISTENERS[token]["expires_at"] = now + stream.LISTENER_READY_TTL_SEC

    got = stream.get_listener(token)
    assert got is not None
    assert got["status"] == "ready"
    assert got["expires_at"] - now >= stream.LISTENER_READY_TTL_SEC - 1
    assert stream.LISTENER_READY_TTL_SEC == 6 * 60 * 60


def test_push_backup_file_includes_status_and_body(tmp_path, monkeypatch):
    import app.services.backup_stream as stream

    path = _tiny_backup_zip(tmp_path)

    class _Resp:
        status_code = 409
        text = '{"detail":"listener_status_error"}'

        def json(self):
            return {"detail": "listener_status_error"}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def put(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(stream.httpx, "Client", _Client)
    monkeypatch.setattr(stream, "_normalize_dest_url", lambda u: "http://dest.example")
    result = stream.push_backup_file(path, dest_base_url="http://dest.example", token="tok")
    assert result["ok"] is False
    assert result["status_code"] == 409
    assert "http_409" in result["error"]
    assert "listener_status_error" in result["error"]


def test_push_backup_file_retries_network_errors(tmp_path, monkeypatch):
    import app.services.backup_stream as stream

    path = _tiny_backup_zip(tmp_path)
    sleeps: list[float] = []
    monkeypatch.setattr(stream.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(stream, "_normalize_dest_url", lambda u: "http://dest.example")

    calls = {"n": 0}

    class _Ok:
        status_code = 200
        text = '{"ok":true}'

        def json(self):
            return {"ok": True}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def put(self, *a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise httpx.ConnectError("boom")
            return _Ok()

    monkeypatch.setattr(stream.httpx, "Client", _Client)
    result = stream.push_backup_file(path, dest_base_url="http://dest.example", token="tok")
    assert result["ok"] is True
    assert calls["n"] == 3
    assert sleeps == [2, 4]


def test_push_backup_file_retries_http_409(tmp_path, monkeypatch):
    """After a drop, destination may still report receiving → 409; retry with backoff."""
    import app.services.backup_stream as stream

    path = _tiny_backup_zip(tmp_path)
    sleeps: list[float] = []
    monkeypatch.setattr(stream.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(stream, "_normalize_dest_url", lambda u: "http://dest.example")

    calls = {"n": 0}

    class _Busy:
        status_code = 409
        text = '{"detail":"listener_busy"}'

        def json(self):
            return {"detail": "listener_busy"}

    class _Ok:
        status_code = 200
        text = '{"ok":true}'

        def json(self):
            return {"ok": True}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def put(self, *a, **k):
            calls["n"] += 1
            if calls["n"] < 2:
                return _Busy()
            return _Ok()

    monkeypatch.setattr(stream.httpx, "Client", _Client)
    result = stream.push_backup_file(path, dest_base_url="http://dest.example", token="tok")
    assert result["ok"] is True
    assert calls["n"] == 2
    assert sleeps == [2]


def test_prune_push_jobs_drops_old_and_caps(tmp_path, monkeypatch):
    import app.services.backup_stream as stream

    with stream._PUSH_LOCK:
        stream._PUSH_JOBS.clear()
        now = time.time()
        stream._PUSH_JOBS["old"] = {
            "job_id": "old", "status": "success", "started_at": now - 7200,
        }
        for i in range(55):
            stream._PUSH_JOBS[f"j{i}"] = {
                "job_id": f"j{i}",
                "status": "error" if i % 2 else "success",
                "started_at": now - i,
            }
        stream._prune_push_jobs()
        assert "old" not in stream._PUSH_JOBS
        finished = [
            j for j in stream._PUSH_JOBS.values()
            if j.get("status") in ("success", "error")
        ]
        assert len(finished) <= stream.PUSH_JOB_MAX_FINISHED
