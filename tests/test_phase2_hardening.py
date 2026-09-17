"""Phase-2 hardening: safe zip extract, setup-token fail-closed."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from app.services.archive_guard import safe_extract_zip_file
from app.services.backup_auth import (
    issue_setup_token,
    setup_token_is_required,
    verify_setup_token,
)
from app.services.upload_bundle import init_bundle, prepare_bundle_workspace, save_bundle_slot


def test_safe_extract_blocks_zip_slip(tmp_path):
    zpath = tmp_path / "slip.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("../evil.txt", "nope")
        zf.writestr("ok.txt", "yes")
    dest = tmp_path / "out"
    with pytest.raises(ValueError, match="Unsafe zip entry"):
        safe_extract_zip_file(zpath, dest)
    assert not (tmp_path / "evil.txt").exists()


def test_safe_extract_counts_actual_bytes(tmp_path, monkeypatch):
    import app.services.archive_guard as ag

    monkeypatch.setattr(ag, "MAX_ZIP_TOTAL_BYTES", 200)
    monkeypatch.setattr(ag, "MAX_ZIP_ENTRY_BYTES", 1000)
    zpath = tmp_path / "big.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("payload.bin", b"Z" * 500)
    with pytest.raises(ValueError, match="safe extraction limit|too large"):
        safe_extract_zip_file(zpath, tmp_path / "out")


def test_bundle_workspace_rejects_zip_slip(tmp_path, monkeypatch):
    monkeypatch.setenv("PG_MIGRATOR_HOME", str(tmp_path))
    import importlib
    import app.config as cfg
    importlib.reload(cfg)
    import app.services.upload_bundle as ub
    importlib.reload(ub)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../escape.pem", "bad")
    bid = ub.init_bundle()
    ub.save_bundle_slot(
        bid, "database", b"sqlite", "db.sqlite3",
        panel_id="marzban", source_db="sqlite", marzban_mode="fresh",
    )
    result = ub.save_bundle_slot(
        bid, "certs", buf.getvalue(), "certs.zip",
        panel_id="marzban", source_db="sqlite", marzban_mode="fresh",
    )
    assert result["ok"] is False
    assert "Unsafe zip entry" in (result.get("slot_meta") or {}).get("error", "")


def test_prepare_bundle_uses_safe_extract_for_db_zip(tmp_path, monkeypatch):
    """prepare_bundle_workspace must use safe_extract (zip slip raises)."""
    monkeypatch.setenv("PG_MIGRATOR_HOME", str(tmp_path))
    import importlib
    import app.config as cfg
    importlib.reload(cfg)
    import app.services.upload_bundle as ub
    importlib.reload(ub)

    bid = ub.init_bundle()
    ub.save_bundle_slot(
        bid, "database", b"sqlite-content", "db.sqlite3",
        panel_id="marzban", source_db="sqlite", marzban_mode="fresh",
    )
    # Plant a malicious certs zip directly into the slot path used by prepare.
    sdir = ub._slot_dir(bid, "certs")
    sdir.mkdir(parents=True, exist_ok=True)
    zpath = sdir / "certs.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("../pwned.txt", "nope")
    manifest = ub._load_manifest(bid)
    manifest["slots"]["certs"] = {"path": str(zpath), "filename": "certs.zip", "ok": True}
    ub._save_manifest(bid, manifest)

    with pytest.raises(ValueError, match="Unsafe zip entry"):
        ub.prepare_bundle_workspace(bid)
    assert not (ub.bundle_dir(bid) / "pwned.txt").exists()


def test_setup_token_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("PG_BACKUP_HOME", str(tmp_path))
    monkeypatch.setenv("PG_MIGRATOR_HOME", str(tmp_path))
    import importlib
    import app.config as cfg
    importlib.reload(cfg)
    from app.services import backup_auth
    importlib.reload(backup_auth)

    assert backup_auth.setup_token_is_required() is False
    assert backup_auth.verify_setup_token(None) is False
    assert backup_auth.verify_setup_token("anything") is False

    tok = backup_auth.issue_setup_token()
    assert backup_auth.setup_token_is_required() is True
    assert backup_auth.verify_setup_token(tok) is True
    assert backup_auth.verify_setup_token("wrong-token-value-here!!!!!") is False
