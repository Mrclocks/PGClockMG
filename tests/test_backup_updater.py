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
