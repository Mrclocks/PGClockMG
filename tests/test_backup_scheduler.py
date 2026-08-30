"""Unit tests for backup schedule interval helpers."""

from datetime import datetime, timedelta, timezone

from app.services.backup_scheduler import due_for_scheduled_run
from app.services.backup_settings import normalize_interval_hours, normalize_schedule, parse_last_run_at


def test_normalize_interval_hours():
    assert normalize_interval_hours(1) == 1
    assert normalize_interval_hours(3) == 3
    assert normalize_interval_hours(6) == 6
    assert normalize_interval_hours(12) == 12
    assert normalize_interval_hours(24) == 24
    assert normalize_interval_hours(2) == 24
    assert normalize_interval_hours("3") == 3
    assert normalize_interval_hours(None) == 24
    assert normalize_interval_hours("nope") == 24


def test_normalize_schedule_sanitizes():
    out = normalize_schedule({"enabled": 1, "interval_hours": 7, "send_telegram": "yes"})
    assert out == {"enabled": True, "interval_hours": 24, "send_telegram": True}
    out2 = normalize_schedule({"enabled": False, "interval_hours": 6, "send_telegram": False})
    assert out2["interval_hours"] == 6
    assert "last_run_at" not in out2


def test_parse_last_run_at():
    dt = parse_last_run_at("2026-08-30T12:00:00Z")
    assert dt is not None
    assert dt.tzinfo is not None
    assert parse_last_run_at("") is None
    assert parse_last_run_at(None) is None
    assert parse_last_run_at("not-a-date") is None


def test_due_for_scheduled_run_interval():
    now = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)
    assert due_for_scheduled_run(enabled=False, interval_hours=1, last_run_at=None, now=now) is False
    assert due_for_scheduled_run(enabled=True, interval_hours=3, last_run_at=None, now=now) is True

    recent = (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    assert due_for_scheduled_run(enabled=True, interval_hours=3, last_run_at=recent, now=now) is False

    old = (now - timedelta(hours=3, minutes=1)).isoformat().replace("+00:00", "Z")
    assert due_for_scheduled_run(enabled=True, interval_hours=3, last_run_at=old, now=now) is True
