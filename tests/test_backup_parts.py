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
    import sqlite3
    import app.config as cfg

    importlib.reload(cfg)
    import app.services.upload as up

    importlib.reload(up)
    import app.services.backup_parts as parts

    importlib.reload(parts)

    db_path = tmp_path / "src.sqlite3"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT)")
    con.execute("INSERT INTO users(username) VALUES ('alice')")
    con.commit()
    con.close()
    payload = _zip_bytes({
        ".env": (
            "UVICORN_PORT=8000\n"
            "SQLALCHEMY_DATABASE_URL=sqlite:////var/lib/pasarguard/db.sqlite3\n"
        ),
        "db.sqlite3": db_path.read_bytes(),
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
        assert zf.read("db.sqlite3").startswith(b"SQLite format 3")

    from app.services.pg_restore import analyze_pasarguard_backup

    analysis = analyze_pasarguard_backup(upload_id=result["upload_id"])
    assert analysis.get("layout") == "sqlite_file"
    assert analysis.get("ok") is True


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

    import sqlite3

    db_path = tmp_path / "api-src.sqlite3"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE t(id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    payload = _zip_bytes({
        ".env": "UVICORN_PORT=8000\nSQLALCHEMY_DATABASE_URL=sqlite:////var/lib/pasarguard/db.sqlite3\n",
        "db.sqlite3": db_path.read_bytes(),
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


def _real_sqlite_payload(tmp_path: Path) -> bytes:
    import sqlite3

    db_path = tmp_path / f"db-{len(list(tmp_path.iterdir()))}.sqlite3"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT)")
    con.execute("INSERT INTO users(username) VALUES ('alice')")
    con.commit()
    con.close()
    return _zip_bytes({
        ".env": (
            "UVICORN_PORT=8000\n"
            "SQLALCHEMY_DATABASE_URL=sqlite:////var/lib/pasarguard/db.sqlite3\n"
        ),
        "db.sqlite3": db_path.read_bytes(),
    })


def test_normalize_download_noise_names():
    from app.services.backup_parts import normalize_part_filename, parse_part_filename

    assert normalize_part_filename("backup-1-2 (1).zip") == "backup-1-2.zip"
    assert normalize_part_filename("backup-2-2.zip.zip") == "backup-2-2.zip"
    spec = parse_part_filename("pgclockmg-1-3 (2).zip")
    assert spec is not None
    assert spec.index == 1 and spec.total == 3


def test_assemble_heals_leading_trailing_junk(tmp_path, monkeypatch):
    monkeypatch.setenv("PG_MIGRATOR_HOME", str(tmp_path))
    import importlib
    import app.config as cfg

    importlib.reload(cfg)
    import app.services.upload as up

    importlib.reload(up)
    import app.services.backup_parts as parts

    importlib.reload(parts)

    payload = _real_sqlite_payload(tmp_path)
    chunks = _split_bytes(payload, 2)
    # Pretend a downloader prepended/appended junk (common manual-extract failure).
    p1 = tmp_path / "junk-1-2.zip"
    p2 = tmp_path / "junk-2-2 (1).zip"
    p1.write_bytes(b"GARBAGE" + chunks[0])
    p2.write_bytes(chunks[1] + b"\nTRAIL")

    result = parts.assemble_part_paths([(p1.name, p1), (p2.name, p2)])
    assert result["assembled_from_parts"] is True
    assert result.get("heals")
    assert any("junk" in h.lower() or "زائد" in h for h in result["heals"])

    from app.services.pg_restore import analyze_pasarguard_backup

    analysis = analyze_pasarguard_backup(upload_id=result["upload_id"])
    assert analysis.get("layout") == "sqlite_file"
    assert analysis.get("ok") is True


def test_assemble_unwraps_zip_of_parts_and_double_wrap(tmp_path, monkeypatch):
    monkeypatch.setenv("PG_MIGRATOR_HOME", str(tmp_path))
    import importlib
    import app.config as cfg

    importlib.reload(cfg)
    import app.services.upload as up

    importlib.reload(up)
    import app.services.backup_parts as parts

    importlib.reload(parts)

    payload = _real_sqlite_payload(tmp_path)
    chunks = _split_bytes(payload, 2)

    # Case A: one container zip holding both Telegram parts.
    container = tmp_path / "all-parts.zip"
    with zipfile.ZipFile(container, "w") as zf:
        zf.writestr("wrap-1-2.zip", chunks[0])
        zf.writestr("wrap-2-2.zip", chunks[1])

    result = parts.assemble_part_paths([(container.name, container)])
    assert result["assembled_from_parts"] is True
    assert result["parts_count"] == 2
    from app.services.pg_restore import analyze_pasarguard_backup

    analysis = analyze_pasarguard_backup(upload_id=result["upload_id"])
    assert analysis.get("ok") is True
    assert analysis.get("layout") == "sqlite_file"

    # Case B: each chunk re-zipped alone (manual "fix" people often try).
    wrapped_items = []
    for i, chunk in enumerate(chunks, start=1):
        name = f"rewrap-{i}-2.zip"
        path = tmp_path / name
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(f"chunk-{i}.bin", chunk)
        path.write_bytes(buf.getvalue())
        wrapped_items.append((name, path))

    result2 = parts.assemble_part_paths(wrapped_items)
    assert result2["assembled_from_parts"] is True
    assert any("wrapper" in h.lower() or "لایه" in h for h in (result2.get("heals") or []))
    analysis2 = analyze_pasarguard_backup(upload_id=result2["upload_id"])
    assert analysis2.get("ok") is True
    assert analysis2.get("layout") == "sqlite_file"


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
