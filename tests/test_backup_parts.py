"""Multipart backup assemble layer — isolated from restore/migrate engines."""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _zip_bytes(files: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, raw in files.items():
            data = raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode()
            zf.writestr(name, data)
    return buf.getvalue()


def _split_bytes(data: bytes, n: int) -> list[bytes]:
    assert n >= 2
    size = len(data)
    chunk = (size + n - 1) // n
    parts = []
    for i in range(n):
        parts.append(data[i * chunk : (i + 1) * chunk])
    assert b"".join(parts) == data
    assert all(len(p) > 0 for p in parts[:-1])
    return parts


def test_parse_telegram_part_names():
    from app.services.backup_parts import looks_like_part_filename, parse_part_filename
    from app.services.backup_telegram import telegram_part_filename

    base = Path("pgclockmg-20260908-020711.zip")
    p1 = telegram_part_filename(base, 1, 3)
    p2 = telegram_part_filename(base, 2, 3)
    p3 = telegram_part_filename(base, 3, 3)
    assert p1 == "pgclockmg-20260908-020711-1-3.zip"
    for name, idx in ((p1, 1), (p2, 2), (p3, 3)):
        spec = parse_part_filename(name)
        assert spec is not None
        assert spec.index == idx
        assert spec.total == 3
        assert spec.stem == "pgclockmg-20260908-020711.zip"
        assert looks_like_part_filename(name)

    assert parse_part_filename("plain-backup.zip") is None
    assert parse_part_filename("backup-1-1.zip") is None  # total must be >= 2


def test_plan_parts_sorts_and_detects_missing():
    from app.services.backup_parts import BackupPartsError, plan_parts

    stem, ordered = plan_parts(
        ["demo-2-3.zip", "demo-1-3.zip", "demo-3-3.zip"]
    )
    assert stem == "demo.zip"
    assert [p.index for p in ordered] == [1, 2, 3]

    with pytest.raises(BackupPartsError) as ei:
        plan_parts(["demo-1-3.zip", "demo-3-3.zip"])
    assert ei.value.code == "missing_parts"
    assert "2/3" in str(ei.value)


def test_assemble_telegram_parts_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("PG_MIGRATOR_HOME", str(tmp_path))
    import importlib
    import app.config as cfg

    importlib.reload(cfg)
    import app.services.upload as up

    importlib.reload(up)
    import app.services.backup_parts as parts

    importlib.reload(parts)

    payload = _zip_bytes({
        ".env": "UVICORN_PORT=8000\nSQLALCHEMY_DATABASE_URL=sqlite:////var/lib/pasarguard/db.sqlite3\n",
        "db.sqlite3": b"SQLITE-DEMO" + b"\x00" * 200,
    })
    chunks = _split_bytes(payload, 3)
    items = []
    for i, chunk in enumerate(chunks, start=1):
        name = f"panel-demo-{i}-3.zip"
        path = tmp_path / name
        path.write_bytes(chunk)
        items.append((name, path))

    # Out of order on purpose
    items = [items[1], items[2], items[0]]
    result = parts.assemble_part_paths(items, allow_large=False)
    assert result["assembled_from_parts"] is True
    assert result["parts_count"] == 3
    assert result.get("upload_id")
    assert not result.get("error"), result.get("error")

    merged = Path(result["path"])
    assert merged.is_file()
    assert zipfile.is_zipfile(merged)
    with zipfile.ZipFile(merged, "r") as zf:
        assert "db.sqlite3" in zf.namelist()
        assert zf.read("db.sqlite3").startswith(b"SQLITE-DEMO")


def test_assemble_single_incomplete_part_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PG_MIGRATOR_HOME", str(tmp_path))
    import importlib
    import app.config as cfg

    importlib.reload(cfg)
    import app.services.backup_parts as parts

    importlib.reload(parts)

    path = tmp_path / "panel-1-2.zip"
    path.write_bytes(b"not-a-zip-chunk")
    with pytest.raises(parts.BackupPartsError) as ei:
        parts.assemble_part_paths([(path.name, path)])
    assert ei.value.code == "single_part"
    assert "1/2" in ei.value.message


def test_assemble_rejects_mixed_stems(tmp_path, monkeypatch):
    monkeypatch.setenv("PG_MIGRATOR_HOME", str(tmp_path))
    import importlib
    import app.config as cfg

    importlib.reload(cfg)
    import app.services.backup_parts as parts

    importlib.reload(parts)

    a = tmp_path / "alpha-1-2.zip"
    b = tmp_path / "beta-2-2.zip"
    a.write_bytes(b"aaa")
    b.write_bytes(b"bbb")
    with pytest.raises(parts.BackupPartsError) as ei:
        parts.assemble_part_paths([(a.name, a), (b.name, b)])
    assert ei.value.code == "mixed_sets"


def test_api_upload_parts_and_single_upload_untouched(tmp_path, monkeypatch):
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

    importlib.reload(main_mod)

    payload = _zip_bytes({
        ".env": "UVICORN_PORT=8000\n",
        "db.sqlite3": b"X" * 120,
    })
    chunks = _split_bytes(payload, 2)

    with TestClient(main_mod.app) as client:
        # Existing single-file upload still works (migration / single restore path).
        single = client.post(
            "/api/upload",
            headers={"X-Auth-Token": token},
            files={"file": ("whole.zip", payload, "application/zip")},
        )
        assert single.status_code == 200, single.text
        assert single.json().get("upload_id")

        # Multipart assemble endpoint.
        r = client.post(
            "/api/upload-parts",
            headers={"X-Auth-Token": token},
            files=[
                ("files", ("bundle-2-2.zip", chunks[1], "application/zip")),
                ("files", ("bundle-1-2.zip", chunks[0], "application/zip")),
            ],
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("assembled_from_parts") is True
        assert body.get("parts_count") == 2
        assert body.get("upload_id")

        # Clear error when a part is missing.
        bad = client.post(
            "/api/upload-parts",
            headers={"X-Auth-Token": token},
            files=[("files", ("bundle-1-2.zip", chunks[0], "application/zip"))],
        )
        assert bad.status_code == 400
        detail = bad.json().get("detail") or ""
        assert "1/2" in detail or "part" in detail.lower() or "پارت" in detail

    auth_mod._cached_token = None


def test_api_upload_parts_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("PG_MIGRATOR_HOME", str(tmp_path))
    import importlib
    import app.config as cfg
    from app.services import auth as auth_mod

    importlib.reload(cfg)
    token_file = tmp_path / ".access_token"
    monkeypatch.setattr(auth_mod, "TOKEN_FILE", token_file)
    auth_mod._cached_token = None
    auth_mod.ensure_token()

    import app.main as main_mod

    importlib.reload(main_mod)
    with TestClient(main_mod.app) as client:
        r = client.post(
            "/api/upload-parts",
            files=[("files", ("a-1-2.zip", b"x", "application/zip"))],
        )
        assert r.status_code == 401
    auth_mod._cached_token = None
