"""Large-upload override must raise zip entry ceilings (not only HTTP upload size)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from app.services import archive_guard as ag
from app.services.archive_guard import (
    preflight_zip,
    safe_extract_zip_file,
    zip_entry_limit_bytes,
)


def test_zip_entry_limit_rises_with_allow_large():
    assert zip_entry_limit_bytes(False) == ag.MAX_ZIP_ENTRY_BYTES
    assert zip_entry_limit_bytes(True) >= ag.MAX_OVERRIDE_ZIP_ENTRY_BYTES
    assert zip_entry_limit_bytes(True) > zip_entry_limit_bytes(False)


def test_preflight_blocks_oversized_entry_without_override(monkeypatch):
    monkeypatch.setattr(ag, "MAX_ZIP_ENTRY_BYTES", 100)
    monkeypatch.setattr(ag, "MAX_OVERRIDE_ZIP_ENTRY_BYTES", 10_000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("db.sqlite3", b"x" * 500)
    with zipfile.ZipFile(buf, "r") as zf:
        with pytest.raises(ValueError, match="Zip entry too large: db.sqlite3"):
            preflight_zip(zf, allow_large=False)


def test_preflight_allows_oversized_entry_with_override(monkeypatch):
    monkeypatch.setattr(ag, "MAX_ZIP_ENTRY_BYTES", 100)
    monkeypatch.setattr(ag, "MAX_OVERRIDE_ZIP_ENTRY_BYTES", 10_000)
    monkeypatch.setattr(ag, "MAX_ZIP_TOTAL_BYTES", 50)
    monkeypatch.setattr(ag, "MAX_OVERRIDE_ZIP_TOTAL_BYTES", 50_000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("db.sqlite3", b"x" * 500)
    with zipfile.ZipFile(buf, "r") as zf:
        report = preflight_zip(zf, allow_large=True)
    assert report.largest_entry == 500


def test_safe_extract_honors_allow_large(tmp_path, monkeypatch):
    monkeypatch.setattr(ag, "MAX_ZIP_ENTRY_BYTES", 100)
    monkeypatch.setattr(ag, "MAX_OVERRIDE_ZIP_ENTRY_BYTES", 10_000)
    monkeypatch.setattr(ag, "MAX_ZIP_TOTAL_BYTES", 50)
    monkeypatch.setattr(ag, "MAX_OVERRIDE_ZIP_TOTAL_BYTES", 50_000)
    zpath = tmp_path / "big.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("db.sqlite3", b"Z" * 800)
    with pytest.raises(ValueError, match="Zip entry too large"):
        safe_extract_zip_file(zpath, tmp_path / "out-a", allow_large=False)
    report = safe_extract_zip_file(zpath, tmp_path / "out-b", allow_large=True)
    assert (tmp_path / "out-b" / "db.sqlite3").read_bytes() == b"Z" * 800
    assert report.largest_entry == 800


def test_save_upload_passes_allow_large(tmp_path, monkeypatch):
    monkeypatch.setenv("PG_MIGRATOR_HOME", str(tmp_path))
    import importlib
    import app.config as cfg
    importlib.reload(cfg)
    import app.services.upload as up
    importlib.reload(up)
    import app.services.archive_guard as ag_mod
    importlib.reload(ag_mod)
    # Re-bind after reload
    up.safe_extract_zip_file = ag_mod.safe_extract_zip_file
    up.resolve_allow_large_for_zip = ag_mod.resolve_allow_large_for_zip

    monkeypatch.setattr(ag_mod, "MAX_ZIP_ENTRY_BYTES", 100)
    monkeypatch.setattr(ag_mod, "MAX_OVERRIDE_ZIP_ENTRY_BYTES", 10_000)
    monkeypatch.setattr(ag_mod, "MAX_ZIP_TOTAL_BYTES", 50)
    monkeypatch.setattr(ag_mod, "MAX_OVERRIDE_ZIP_TOTAL_BYTES", 50_000)

    zpath = tmp_path / "payload.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        # Non-panel junk — must still require explicit allow_large
        zf.writestr("random.bin", b"Q" * 400)

    blocked = up.save_upload(zpath, "payload.zip", allow_large=False)
    assert blocked.get("error")
    assert "too large" in blocked["error"].lower()

    ok = up.save_upload(zpath, "payload.zip", allow_large=True)
    assert not ok.get("error"), ok.get("error")
    assert ok.get("allow_large_upload") is True


def test_panel_backup_zip_auto_allows_large_entry(tmp_path, monkeypatch):
    """Recognized panel backup layout auto-raises entry ceiling without UI tick."""
    monkeypatch.setenv("PG_MIGRATOR_HOME", str(tmp_path))
    import importlib
    import app.config as cfg
    importlib.reload(cfg)
    import app.services.upload as up
    importlib.reload(up)
    import app.services.archive_guard as ag_mod
    importlib.reload(ag_mod)
    up.safe_extract_zip_file = ag_mod.safe_extract_zip_file
    up.resolve_allow_large_for_zip = ag_mod.resolve_allow_large_for_zip

    monkeypatch.setattr(ag_mod, "MAX_ZIP_ENTRY_BYTES", 100)
    monkeypatch.setattr(ag_mod, "MAX_OVERRIDE_ZIP_ENTRY_BYTES", 10_000)
    monkeypatch.setattr(ag_mod, "MAX_ZIP_TOTAL_BYTES", 50)
    monkeypatch.setattr(ag_mod, "MAX_OVERRIDE_ZIP_TOTAL_BYTES", 50_000)

    zpath = tmp_path / "panel.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr(".env", "UVICORN_PORT=8000\n")
        zf.writestr("db.sqlite3", b"S" * 400)

    assert ag_mod.looks_like_panel_backup_zip(zpath)
    ok = up.save_upload(zpath, "panel.zip", allow_large=False)
    assert not ok.get("error"), ok.get("error")
    assert ok.get("allow_large_upload") is True
