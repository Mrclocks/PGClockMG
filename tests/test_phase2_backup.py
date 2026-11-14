"""Phase 2: Telegram retry queue, retry_after, custom API base, stale alert, manifest warnings."""

import time
from pathlib import Path

import httpx
import pytest


def test_normalize_api_base():
    from app.services.backup_telegram import DEFAULT_API_BASE, _api_url, normalize_api_base

    assert normalize_api_base("") == DEFAULT_API_BASE
    assert normalize_api_base("tg.example.com/") == "https://tg.example.com"
    assert normalize_api_base("http://10.0.0.5:8081") == "http://10.0.0.5:8081"
    with pytest.raises(ValueError):
        normalize_api_base("ftp://x")
    assert _api_url({"api_base_url": "https://w.example.com"}, "1:ab", "getMe") == "https://w.example.com/bot1:ab/getMe"
    assert _api_url({}, "1:ab", "getMe") == "https://api.telegram.org/bot1:ab/getMe"


def test_retry_after_is_honored_and_capped():
    from app.services.backup_telegram import _retry_after_seconds

    r = httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 7}})
    assert _retry_after_seconds(r, 1.5) == 8.0
    r = httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 9999}})
    assert _retry_after_seconds(r, 1.5) == 65.0
    r = httpx.Response(502, text="bad gateway")
    assert _retry_after_seconds(r, 1.5) == 1.5


def test_queue_retry_flow(tmp_path, monkeypatch):
    from app.services import backup_telegram_queue as q

    monkeypatch.setattr(q, "QUEUE_FILE", tmp_path / "pending.json")
    f = tmp_path / "pgclockmg-1.zip"
    f.write_bytes(b"x" * 100)
    q.enqueue("pgclockmg-1", "boom")
    assert len(q.pending()) == 1

    resolve = lambda bid: f if bid == "pgclockmg-1" else None
    # not due yet
    out = q.process_due(lambda p, i: {"ok": True}, resolve)
    assert out["attempted"] == 0 and len(q.pending()) == 1
    # due, send fails → stays with backoff
    later = time.time() + 11 * 60
    out = q.process_due(lambda p, i: {"ok": False, "error": "net"}, resolve, now=later)
    assert out["failed"] == 1 and q.pending()[0]["attempts"] == 1
    # due again, succeeds → removed
    out = q.process_due(lambda p, i: {"ok": True}, resolve, now=later + 3600)
    assert out["sent"] == 1 and q.pending() == []


def test_queue_drops_missing_and_old(tmp_path, monkeypatch):
    from app.services import backup_telegram_queue as q

    monkeypatch.setattr(q, "QUEUE_FILE", tmp_path / "pending.json")
    q.enqueue("gone")
    out = q.process_due(lambda p, i: {"ok": True}, lambda b: None)
    assert out["expired"] == ["gone"] and q.pending() == []


def test_queue_keeps_only_newest(tmp_path, monkeypatch):
    from app.services import backup_telegram_queue as q

    monkeypatch.setattr(q, "QUEUE_FILE", tmp_path / "pending.json")
    for i in range(5):
        q.enqueue(f"b{i}")
    assert [i["backup_id"] for i in q.pending()] == ["b2", "b3", "b4"]


def test_stale_backup_hours():
    from datetime import datetime, timedelta, timezone

    from app.services.backup_scheduler import stale_backup_hours

    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    ok = {"enabled": True, "interval_hours": 24, "last_success_at": (now - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    assert stale_backup_hours(ok, now) is None
    old = {**ok, "last_success_at": (now - timedelta(hours=80)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    assert stale_backup_hours(old, now) > 72
    assert stale_backup_hours({**old, "enabled": False}, now) is None
    assert stale_backup_hours({"enabled": True, "interval_hours": 24}, now) is None


def test_copy_tree_records_skipped_files(tmp_path, monkeypatch):
    import shutil

    from app.services import backup_engine as be

    src = tmp_path / "src"
    src.mkdir()
    (src / "ok.pem").write_text("a")
    (src / "bad.pem").write_text("b")
    real = shutil.copy2

    def flaky(a, b, *args, **kw):
        if str(a).endswith("bad.pem"):
            raise PermissionError("nope")
        return real(a, b, *args, **kw)

    monkeypatch.setattr(be.shutil, "copy2", flaky)
    skipped: list = []
    be._copy_tree_filtered(src, tmp_path / "dst", skipped=skipped)
    assert (tmp_path / "dst" / "ok.pem").exists()
    assert skipped and skipped[0]["reason"].startswith("copy_failed")


def test_dump_footer_detection(tmp_path):
    from app.services.backup_engine import _dump_footer_present

    pg = tmp_path / "a.sql"
    pg.write_text("CREATE TABLE x();\n-- PostgreSQL database dump complete\n")
    assert _dump_footer_present(pg, "postgresql")
    pg.write_text("CREATE TABLE x();\nINSERT")
    assert not _dump_footer_present(pg, "postgresql")
    my = tmp_path / "b.sql"
    my.write_text("INSERT ...;\n-- Dump completed on 2026-01-01\n")
    assert _dump_footer_present(my, "mysql")
    assert _dump_footer_present(my, "sqlite")
