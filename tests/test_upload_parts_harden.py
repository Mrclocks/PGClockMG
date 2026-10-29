"""Group A: upload threadpool, part-name dates, ratio ceiling, cleanup."""

from __future__ import annotations

import io
import threading
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


def _zip_bytes(files: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, raw in files.items():
            data = raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode()
            zf.writestr(name, data)
    return buf.getvalue()


def _panel_zip() -> bytes:
    return _zip_bytes({
        ".env": "UVICORN_PORT=8000\nSQLALCHEMY_DATABASE_URL=sqlite:////var/lib/pasarguard/db.sqlite3\n",
        "db.sqlite3": b"SQLite format 3\x00" + b"\x00" * 64,
    })


def test_date_named_backup_is_not_a_part():
    from app.services.backup_parts import looks_like_part_filename, parse_part_filename

    for name in (
        "backup-2025-10-29.zip",
        "pasarguard-2025-09-12.zip",
        "pgclockmg-2024-01-02.zip",
    ):
        assert parse_part_filename(name) is None, name
        assert looks_like_part_filename(name) is False, name

    # Real Telegram split names still parse.
    spec = parse_part_filename("panel-demo-1-3.zip")
    assert spec is not None
    assert spec.index == 1 and spec.total == 3


def test_single_complete_zip_with_partish_name_skips_repack(tmp_path, monkeypatch):
    monkeypatch.setenv("PG_MIGRATOR_HOME", str(tmp_path))
    import importlib
    import app.config as cfg

    importlib.reload(cfg)
    import app.services.upload as up

    importlib.reload(up)
    import app.services.backup_parts as parts

    importlib.reload(parts)

    payload = _panel_zip()
    # Looks like part 10/29 but content is a complete panel backup.
    name = "backup-10-29.zip"
    path = tmp_path / name
    path.write_bytes(payload)

    with patch.object(parts, "repack_clean_zip", side_effect=AssertionError("repack should not run")):
        result = parts.assemble_part_paths([(name, path)])
    assert result.get("upload_id")
    assert result.get("assembled_from_parts") is False
    assert result.get("parts_count") == 1
    assert zipfile.is_zipfile(result["path"])


def test_heal_concatenated_zip_does_not_load_whole_file(tmp_path):
    from app.services.backup_parts import HealReport, heal_concatenated_zip

    payload = _panel_zip()
    src = tmp_path / "raw.bin"
    src.write_bytes(b"JUNK!!" + payload + b"TRAIL")
    dest = tmp_path / "healed.zip"
    heals = HealReport()

    original_read_bytes = Path.read_bytes

    def _forbid_full_read(self):
        if self.resolve() == src.resolve():
            raise AssertionError("heal must not call read_bytes() on the whole archive")
        return original_read_bytes(self)

    with patch.object(Path, "read_bytes", _forbid_full_read):
        out = heal_concatenated_zip(src, dest, heals)
    assert out == dest
    assert zipfile.is_zipfile(dest)
    assert any("leading" in n.lower() or "زائد" in n for n in heals.notes)


def test_panel_backup_raises_ratio_ceiling(tmp_path, monkeypatch):
    from app.services import archive_guard as ag

    monkeypatch.setattr(ag, "MAX_ZIP_RATIO", 5)
    monkeypatch.setattr(ag, "MAX_PANEL_ZIP_RATIO", 1000)

    # Highly compressible text dump that looks like a panel backup.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(".env", "A=1\n")
        zf.writestr("db_backup.sql", ("--" + ("x" * 10) + "\n") * 5000)
    with zipfile.ZipFile(buf, "r") as zf:
        report = ag.preflight_zip(zf, allow_large=False)
    assert report.compression_ratio > 5
    assert report.compression_ratio <= 1000


def test_non_panel_zip_still_blocked_by_ratio(monkeypatch):
    from app.services import archive_guard as ag

    monkeypatch.setattr(ag, "MAX_ZIP_RATIO", 5)
    monkeypatch.setattr(ag, "MAX_PANEL_ZIP_RATIO", 1000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("notes.txt", "y" * 50_000)
    with zipfile.ZipFile(buf, "r") as zf:
        with pytest.raises(ValueError, match="compression ratio"):
            ag.preflight_zip(zf, allow_large=False)


def test_save_upload_removes_dir_on_zip_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PG_MIGRATOR_HOME", str(tmp_path))
    import importlib
    import app.config as cfg

    importlib.reload(cfg)
    import app.services.upload as up

    importlib.reload(up)
    import app.services.archive_guard as ag

    importlib.reload(ag)
    up.safe_extract_zip_file = ag.safe_extract_zip_file
    up.resolve_allow_large_for_zip = ag.resolve_allow_large_for_zip

    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"PK\x03\x04not-a-real-zip")
    result = up.save_upload(bad, "bad.zip")
    assert result.get("error")
    dest = Path(result["path"]).parent
    assert not dest.exists()


def test_api_upload_does_not_block_event_loop(tmp_path, monkeypatch):
    """While /api/upload save runs in a threadpool, a quick GET still answers."""
    monkeypatch.setenv("PG_MIGRATOR_HOME", str(tmp_path))
    import importlib
    import app.config as cfg
    from app.services import auth as auth_mod

    importlib.reload(cfg)
    token_file = tmp_path / ".access_token"
    monkeypatch.setattr(auth_mod, "TOKEN_FILE", token_file)
    auth_mod._cached_token = None
    auth_mod.ensure_token()
    token = auth_mod.get_token()

    import app.main as main_mod
    import app.services.upload as up

    importlib.reload(up)
    importlib.reload(main_mod)

    gate = threading.Event()
    entered = threading.Event()

    def _slow_save(*_a, **_k):
        entered.set()
        gate.wait(timeout=5)
        return {
            "upload_id": "deadbeefcafe",
            "filename": "slow.zip",
            "path": str(tmp_path / "slow.zip"),
            "size": 1,
            "detected": {},
            "analysis": {},
            "allow_large_upload": False,
        }

    monkeypatch.setattr(main_mod, "save_upload", _slow_save)

    payload = _panel_zip()
    got = {}

    def _upload():
        with TestClient(main_mod.app) as client:
            r = client.post(
                "/api/upload",
                headers={"X-Auth-Token": token},
                files={"file": ("slow.zip", payload, "application/zip")},
            )
            got["upload"] = r.status_code

    t = threading.Thread(target=_upload)
    t.start()
    assert entered.wait(timeout=5)

    t0 = time.monotonic()
    with TestClient(main_mod.app) as client:
        info = client.get("/api/info", headers={"X-Auth-Token": token})
    elapsed = time.monotonic() - t0
    gate.set()
    t.join(timeout=5)

    assert info.status_code == 200
    assert elapsed < 1.5, f"GET /api/info blocked for {elapsed:.2f}s"
    assert got.get("upload") == 200
    auth_mod._cached_token = None


def test_zip_allow_large_requires_panel_layout(tmp_path, monkeypatch):
    from app.config import BACKUP_DIR
    from app.services.pg_restore import _zip_allow_large

    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setenv("PG_MIGRATOR_HOME", str(tmp_path))
    import importlib
    import app.config as cfg

    importlib.reload(cfg)

    random_zip = upload_dir / "notes.zip"
    with zipfile.ZipFile(random_zip, "w") as zf:
        zf.writestr("readme.txt", "hi")
    assert _zip_allow_large(random_zip) is False

    panel_zip = upload_dir / "panel.zip"
    panel_zip.write_bytes(_panel_zip())
    assert _zip_allow_large(panel_zip) is True
