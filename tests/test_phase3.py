"""Phase 3: fixed daily run time and updater rollback."""

from datetime import datetime, timezone

from app.services.backup_scheduler import due_for_scheduled_run as due
from app.services.backup_settings import next_run_after, normalize_run_at, normalize_schedule

NOW = datetime(2026, 3, 10, 5, 0, tzinfo=timezone.utc)  # 08:30 in Asia/Tehran
KW = dict(enabled=True, interval_hours=24, run_at="03:00", timezone_name="Asia/Tehran", now=NOW)


def test_run_at_validation():
    assert normalize_run_at("03:00") == "03:00"
    assert normalize_run_at("3:00") == ""
    assert normalize_run_at("24:00") == ""
    assert normalize_run_at(None) == ""
    assert normalize_schedule({"run_at": "23:59"})["run_at"] == "23:59"
    assert normalize_schedule({"run_at": "bad"})["run_at"] == ""


def test_fixed_slot_due_logic():
    assert due(last_success_at=None, **KW) is True
    # served after today's 03:00 Tehran slot (= 23:30Z the day before)
    assert due(last_success_at="2026-03-10T00:00:30Z", **KW) is False
    # last success before the slot → owed
    assert due(last_success_at="2026-03-09T00:00:00Z", **KW) is True


def test_run_at_ignored_for_short_intervals_and_when_empty():
    base = dict(enabled=True, last_success_at="2026-03-10T04:00:00Z", now=NOW)
    assert due(interval_hours=3, run_at="03:00", timezone_name="UTC", **base) is False  # rolling: 1h elapsed
    assert due(interval_hours=24, run_at="", timezone_name="UTC", **base) is False


def test_next_run_with_run_at():
    nxt = next_run_after(
        last_success_at="2026-03-10T00:00:30Z", interval_hours=24,
        timezone_name="Asia/Tehran", now=NOW, run_at="03:00",
    )
    assert nxt["at_utc"] == "2026-03-10T23:30:00Z"
    nxt = next_run_after(
        last_success_at=None, interval_hours=24, timezone_name="Asia/Tehran", now=NOW, run_at="23:00",
    )
    assert nxt["at_utc"] == "2026-03-10T19:30:00Z"


def test_updater_rollback_restores_previous_app(tmp_path):
    from app.services.backup_updater import _rollback_swap

    app_dst = tmp_path / "app"
    old = tmp_path / "app.old"
    app_dst.mkdir()
    (app_dst / "main.py").write_text("new")
    old.mkdir()
    (old / "main.py").write_text("old")
    req = tmp_path / "requirements.txt.pre-update"
    req.write_text("fastapi==1")
    (tmp_path / "requirements.txt").write_text("fastapi==2")

    _rollback_swap(old, app_dst, req)
    assert (app_dst / "main.py").read_text() == "old"
    assert not old.exists()
    assert (tmp_path / "requirements.txt").read_text() == "fastapi==1"
