"""Tests for backup panel self-update helpers."""

from __future__ import annotations


def test_version_compare():
    from app.services.backup_updater import parse_version, version_lt

    assert parse_version("4.1.7") == (4, 1, 7)
    assert parse_version("v4.1.6") == (4, 1, 6)
    assert version_lt("4.1.6", "4.1.7")
    assert not version_lt("4.1.7", "4.1.7")
    assert not version_lt("4.1.7", "4.1.6")
    print("OK: version compare")


def test_check_for_update_uses_admin_alias(monkeypatch):
    from app.services import backup_updater as up

    up._CHECK_CACHE["at"] = 0
    up._CHECK_CACHE["payload"] = None

    class _Resp:
        status_code = 200
        def json(self):
            return {
                "tag_name": "v9.9.9",
                "name": "v9.9.9",
                "body": "- Fixed things\n- More",
                "html_url": "https://example/release",
                "published_at": "2026-01-01T00:00:00Z",
            }
        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *a, **k):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def get(self, url, headers=None):
            assert "releases/latest" in url
            return _Resp()

    monkeypatch.setattr(up.httpx, "Client", _Client)
    info = up.check_for_update(current="4.1.7", force=True)
    assert info["available"] is True
    assert info["latest"] == "9.9.9"
    assert "Fixed things" in info["body"]
    print("OK: check_for_update mock")


def test_check_up_to_date(monkeypatch):
    from app.services import backup_updater as up

    up._CHECK_CACHE["at"] = 0
    up._CHECK_CACHE["payload"] = None

    class _Resp:
        status_code = 200
        def json(self):
            return {"tag_name": "v4.1.7", "name": "v4.1.7", "body": "same", "html_url": "x"}
        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *a, **k):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def get(self, url, headers=None):
            return _Resp()

    monkeypatch.setattr(up.httpx, "Client", _Client)
    info = up.check_for_update(current="4.1.7", force=True)
    assert info["available"] is False
    print("OK: up to date")


def test_apply_update_returns_immediately(monkeypatch, tmp_path):
    """apply_update must not block on GitHub — job starts at 5% and returns."""
    from app.services import backup_updater as up
    import time

    up._UPDATE_JOB = None
    up._CHECK_CACHE["at"] = 0
    up._CHECK_CACHE["payload"] = None

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "backup_main.py").write_text("APP_VERSION = '4.2.0'\n", encoding="utf-8")
    monkeypatch.setattr(up, "BACKUP_HOME", tmp_path)

    started = {"worker": False}

    def slow_check(**kwargs):
        time.sleep(0.4)
        return {
            "ok": True,
            "latest_tag": "v4.2.1",
            "available": True,
            "error": None,
        }

    def fake_download(tag, dest_zip, **kwargs):
        started["worker"] = True
        # Create a minimal zip with app/
        import zipfile
        root = tmp_path / "pack"
        (root / "rel" / "app").mkdir(parents=True)
        (root / "rel" / "app" / "x.py").write_text("ok", encoding="utf-8")
        (root / "rel" / "requirements.txt").write_text("httpx\n", encoding="utf-8")
        with zipfile.ZipFile(dest_zip, "w") as zf:
            zf.write(root / "rel" / "app" / "x.py", "rel/app/x.py")
            zf.write(root / "rel" / "requirements.txt", "rel/requirements.txt")

    monkeypatch.setattr(up, "check_for_update", slow_check)
    monkeypatch.setattr(up, "_download_release_archive", fake_download)
    monkeypatch.setattr(up, "_schedule_restart", lambda: False)

    t0 = time.time()
    job = up.apply_update(current="4.2.0")
    elapsed = time.time() - t0
    assert elapsed < 0.25, f"apply_update blocked for {elapsed:.2f}s"
    assert job["status"] == "running"
    assert job["progress"] == 5

    # Wait for worker to finish
    for _ in range(50):
        j = up.get_update_job()
        if j and j.get("status") in ("success", "error"):
            break
        time.sleep(0.1)
    j = up.get_update_job()
    assert j["status"] == "success", j
    assert (tmp_path / "app" / "x.py").is_file()
    print("OK: apply_update non-blocking + zip install")


def test_find_app_dir_in_zipball_layout(tmp_path):
    from app.services.backup_updater import _find_app_dir

    root = tmp_path / "Mrclocks-PGClockMG-abc"
    (root / "app").mkdir(parents=True)
    assert _find_app_dir(tmp_path) == root / "app"
    print("OK: find app dir")
