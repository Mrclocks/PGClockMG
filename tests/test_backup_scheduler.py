"""Unit tests for backup schedule, retention, integrity, and notify helpers."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import zipfile

from app.services.backup_engine import apply_retention, verify_backup_archive
from app.services.backup_scheduler import (
    FAILURE_RETRY_SECONDS,
    due_for_scheduled_run,
    should_send_scheduled_telegram,
)
from app.services.backup_settings import (
    normalize_destinations,
    normalize_interval_hours,
    normalize_schedule,
    normalize_timezone,
    parse_last_run_at,
)
from app.services.backup_telegram import resolve_destinations


def test_normalize_interval_hours():
    assert normalize_interval_hours(0.5) == 0.5
    assert normalize_interval_hours("0.5") == 0.5
    assert normalize_interval_hours(1) == 1.0
    assert normalize_interval_hours(2) == 2.0
    assert normalize_interval_hours(3) == 3.0
    assert normalize_interval_hours(6) == 6.0
    assert normalize_interval_hours(8) == 8.0
    assert normalize_interval_hours(12) == 12.0
    assert normalize_interval_hours(24) == 24.0
    assert normalize_interval_hours(7) == 24.0
    assert normalize_interval_hours("3") == 3.0
    assert normalize_interval_hours(None) == 24.0
    assert normalize_interval_hours("nope") == 24.0


def test_normalize_timezone():
    assert normalize_timezone("Asia/Tehran") == "Asia/Tehran"
    assert normalize_timezone("Europe/Moscow") == "Europe/Moscow"
    assert normalize_timezone("UTC") == "UTC"
    assert normalize_timezone("tehran") == "Asia/Tehran"
    assert normalize_timezone("Moscow") == "Europe/Moscow"
    # Outside the panel whitelist → UTC
    assert normalize_timezone("Asia/Dubai") == "UTC"
    assert normalize_timezone("Not/AZone") == "UTC"
    assert normalize_timezone("") == "UTC"


def test_normalize_schedule_sanitizes():
    out = normalize_schedule({
        "enabled": 1,
        "interval_hours": 7,
        "timezone": "Asia/Tehran",
        "send_telegram": "yes",
        "notify_on_failure": 0,
    })
    assert out["enabled"] is True
    assert out["interval_hours"] == 24.0
    assert out["timezone"] == "Asia/Tehran"
    assert out["send_telegram"] is True
    assert out["notify_on_failure"] is False
    assert "last_run_at" not in out

    half = normalize_schedule({"enabled": True, "interval_hours": 0.5, "timezone": "UTC"})
    assert half["interval_hours"] == 0.5


def test_parse_last_run_at():
    dt = parse_last_run_at("2026-08-30T12:00:00Z")
    assert dt is not None
    assert dt.tzinfo is not None
    assert parse_last_run_at("") is None
    assert parse_last_run_at(None) is None
    assert parse_last_run_at("not-a-date") is None


def test_due_for_scheduled_run_interval():
    now = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)
    assert due_for_scheduled_run(enabled=False, interval_hours=1, now=now) is False
    assert due_for_scheduled_run(enabled=True, interval_hours=3, last_success_at=None, now=now) is True

    recent = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert due_for_scheduled_run(
        enabled=True, interval_hours=3, last_success_at=recent, now=now
    ) is False

    old = (now - timedelta(hours=3, minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert due_for_scheduled_run(
        enabled=True, interval_hours=3, last_success_at=old, now=now
    ) is True

    # 30-minute interval
    almost = (now - timedelta(minutes=29)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert due_for_scheduled_run(
        enabled=True, interval_hours=0.5, last_success_at=almost, now=now
    ) is False
    ready = (now - timedelta(minutes=31)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert due_for_scheduled_run(
        enabled=True, interval_hours=0.5, last_success_at=ready, now=now
    ) is True


def test_due_respects_failure_backoff():
    now = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)
    success = (now - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    attempt = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert due_for_scheduled_run(
        enabled=True,
        interval_hours=3,
        last_success_at=success,
        last_attempt_at=attempt,
        now=now,
    ) is False

    attempt_old = (now - timedelta(seconds=FAILURE_RETRY_SECONDS + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert due_for_scheduled_run(
        enabled=True,
        interval_hours=3,
        last_success_at=success,
        last_attempt_at=attempt_old,
        now=now,
    ) is True


def test_legacy_last_run_at_counts_as_success():
    now = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)
    legacy = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert due_for_scheduled_run(
        enabled=True,
        interval_hours=3,
        last_run_at=legacy,
        now=now,
    ) is False


def test_should_send_scheduled_telegram(monkeypatch):
    import app.services.backup_telegram as tg

    monkeypatch.setattr(tg, "telegram_ready", lambda cfg: True)
    monkeypatch.setattr(
        tg,
        "telegram_config",
        lambda settings=None: {"enabled": True, "bot_token": "t", "chat_id": "1"},
    )

    assert should_send_scheduled_telegram(
        {"telegram": {"enabled": True}},
        {"send_telegram": False},
    ) is True  # telegram.enabled fallback

    assert should_send_scheduled_telegram(
        {"telegram": {"enabled": False}},
        {"send_telegram": True},
    ) is True

    monkeypatch.setattr(
        tg,
        "telegram_config",
        lambda settings=None: {"enabled": False, "bot_token": "t", "chat_id": "1"},
    )
    assert should_send_scheduled_telegram(
        {"telegram": {"enabled": False}},
        {"send_telegram": False},
    ) is False

    monkeypatch.setattr(tg, "telegram_ready", lambda cfg: False)
    assert should_send_scheduled_telegram(
        {"telegram": {"enabled": True}},
        {"send_telegram": True},
    ) is False


def test_normalize_destinations_and_resolve():
    dests = normalize_destinations(
        [{"chat_id": "222", "message_thread_id": 9}],
        primary_chat="111",
        primary_thread=3,
    )
    assert dests[0]["chat_id"] == "111"
    assert dests[0]["message_thread_id"] == 3
    assert dests[1]["chat_id"] == "222"
    assert dests[1]["message_thread_id"] == 9

    resolved = resolve_destinations({
        "chat_id": "111",
        "message_thread_id": 3,
        "destinations": [{"chat_id": "222", "message_thread_id": 9}],
    })
    assert len(resolved) == 2


def test_apply_retention_by_days(tmp_path, monkeypatch):
    import app.services.backup_engine as eng
    import time

    monkeypatch.setattr(eng, "BACKUP_DIR", tmp_path)
    now = time.time()
    paths = []
    for i, age_days in enumerate([0.1, 2.0, 10.0]):
        p = tmp_path / f"pgclockmg-sqlite-old{i}.zip"
        p.write_bytes(b"x")
        (tmp_path / f"{p.name}.json").write_text("{}", encoding="utf-8")
        # set mtime
        ts = now - age_days * 86400
        import os
        os.utime(p, (ts, ts))
        paths.append(p)

    removed = apply_retention(keep_count=10, keep_days=7)
    assert removed == 1
    assert paths[0].exists()
    assert paths[1].exists()
    assert not paths[2].exists()


def test_verify_backup_archive_ok(tmp_path):
    zpath = tmp_path / "ok.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr(".env", "A=1\n")
        zf.writestr("db_backup.sql", "-- dump\n" + ("x" * 100))
    result = verify_backup_archive(zpath)
    assert result["ok"] is True
    assert result["crc_ok"] is True
    assert result["sha256"]


def test_verify_backup_archive_missing_dump(tmp_path):
    zpath = tmp_path / "bad.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr(".env", "A=1\n")
    result = verify_backup_archive(zpath)
    assert result["ok"] is False
    assert result["error"] == "missing_db_dump"
