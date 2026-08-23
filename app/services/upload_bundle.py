"""Grouped upload slots — full ZIP or separate files."""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path

from app.config import UPLOAD_DIR
from app.services.archive_guard import resolve_within, safe_extract_zip_file, safe_upload_name
from app.services.backup_analyzer import analyze_upload_directory, resolve_extract_root
from app.services.upload_requirements import get_upload_requirements

BUNDLES_ROOT = UPLOAD_DIR / "bundles"
MANIFEST = "manifest.json"


def create_bundle_id() -> str:
    return str(uuid.uuid4())[:12]


def bundle_dir(bundle_id: str) -> Path:
    d = resolve_within(BUNDLES_ROOT, bundle_id)
    if d is None:
        raise ValueError("Invalid bundle id")
    return d


def _manifest_path(bundle_id: str) -> Path:
    return bundle_dir(bundle_id) / MANIFEST


def _load_manifest(bundle_id: str) -> dict:
    p = _manifest_path(bundle_id)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"bundle_id": bundle_id, "slots": {}}


def _save_manifest(bundle_id: str, data: dict) -> None:
    d = bundle_dir(bundle_id)
    d.mkdir(parents=True, exist_ok=True)
    _manifest_path(bundle_id).write_text(json.dumps(data, indent=2), encoding="utf-8")


def init_bundle(bundle_id: str | None = None) -> str:
    bid = bundle_id or create_bundle_id()
    d = bundle_dir(bid)
    d.mkdir(parents=True, exist_ok=True)
    if not _manifest_path(bid).exists():
        _save_manifest(bid, {"bundle_id": bid, "slots": {}})
    return bid


def _slot_dir(bundle_id: str, slot: str) -> Path:
    d = resolve_within(bundle_dir(bundle_id) / "slots", slot)
    if d is None:
        raise ValueError("Invalid slot name")
    return d


def _validate_slot_file(
    slot: str,
    filename: str,
    source_db: str | None,
    panel_id: str | None = None,
) -> str | None:
    name = filename.lower()
    ext = Path(filename).suffix.lower()
    if slot == "env" and (name.endswith(".env") or name == "env" or ext in (".env", ".txt")):
        return None

    # Prefer accept list from upload requirements when panel is known
    if panel_id:
        try:
            reqs = get_upload_requirements(panel_id, source_db)
            for s in reqs.get("slots") or []:
                if s.get("id") == slot:
                    allowed = [a.lower() for a in (s.get("accept") or [])]
                    if allowed and ext in allowed:
                        return None
                    if allowed:
                        return f"Invalid file type for {slot}: {ext}"
        except Exception:
            pass

    rules = {
        "bundle_zip": [".zip"],
        "database": [".sqlite3", ".db", ".sql", ".json"] if panel_id == "hiddify" else (
            [".sqlite3", ".db", ".sql"] if not source_db else (
                [".sqlite3", ".db"] if source_db == "sqlite" else [".sql", ".json"]
            )
        ),
        "env": [".env", ".txt"],
        "xray_config": [".json"],
        "certs": [".zip"],
        "templates": [".zip"],
    }
    allowed = rules.get(slot, [])
    if ext not in allowed:
        return f"Invalid file type for {slot}: {ext}"
    return None


def save_bundle_slot(
    bundle_id: str,
    slot: str,
    src_path: str | Path,
    filename: str,
    panel_id: str | None = None,
    source_db: str | None = None,
    marzban_mode: str | None = None,
) -> dict:
    err = _validate_slot_file(slot, filename, source_db, panel_id=panel_id)
    if err:
        return {"ok": False, "error": err}

    init_bundle(bundle_id)
    cleanup_tmp = False
    cleanup_path: Path | None = None
    if isinstance(src_path, (bytes, bytearray)):
        tmp_dir = Path(tempfile.mkdtemp(prefix="pg-bundle-slot-"))
        src_tmp = tmp_dir / safe_upload_name(filename)
        src_tmp.write_bytes(bytes(src_path))
        src_path = src_tmp
        cleanup_tmp = True
        cleanup_path = src_tmp
    try:
        src_path = Path(src_path)
        sdir = _slot_dir(bundle_id, slot)
        if sdir.exists():
            shutil.rmtree(sdir)
        sdir.mkdir(parents=True, exist_ok=True)

        filename = safe_upload_name(filename)
        dest = sdir / filename
        shutil.copy2(src_path, dest)

        slot_meta: dict = {
            "filename": filename,
            "size": dest.stat().st_size,
            "path": str(dest),
            "ok": True,
        }

        if slot == "bundle_zip" or filename.lower().endswith(".zip"):
            try:
                report = safe_extract_zip_file(dest, sdir / "extracted")
                slot_meta["zip_preflight"] = {
                    "files": report.files,
                    "total_uncompressed": report.total_uncompressed,
                    "largest_entry": report.largest_entry,
                    "compression_ratio": report.compression_ratio,
                }
                if slot != "bundle_zip":
                    slot_meta["extracted"] = True
            except ValueError as e:
                slot_meta["ok"] = False
                slot_meta["error"] = str(e)

        if slot in ("bundle_zip", "database", "certs", "templates"):
            analysis = analyze_upload_directory(sdir)
            slot_meta["analysis"] = analysis
            if slot == "bundle_zip":
                slot_meta["ok"] = analysis.get("backup_ok", False)
            elif slot == "database":
                slot_meta["ok"] = _database_slot_ok(
                    dest, analysis, source_db=source_db, panel_id=panel_id,
                )
            else:
                slot_meta["ok"] = dest.exists()
        elif slot == "env":
            slot_meta["ok"] = dest.exists()
            text = dest.read_text(encoding="utf-8", errors="ignore")
            slot_meta["has_mysql_password"] = "MYSQL_ROOT_PASSWORD" in text or "MYSQL_PASSWORD" in text
        elif slot == "xray_config":
            slot_meta["ok"] = dest.exists() and dest.suffix.lower() == ".json"
        else:
            slot_meta["ok"] = dest.exists()

        manifest = _load_manifest(bundle_id)
        manifest["slots"][slot] = slot_meta
        manifest["panel_id"] = panel_id
        manifest["source_db"] = source_db
        manifest["marzban_mode"] = marzban_mode
        _save_manifest(bundle_id, manifest)

        status = validate_bundle(bundle_id, panel_id, source_db, marzban_mode)
        return {
            "ok": slot_meta.get("ok", False),
            "bundle_id": bundle_id,
            "slot": slot,
            "slot_meta": slot_meta,
            "bundle_status": status,
        }
    finally:
        if cleanup_tmp and cleanup_path is not None:
            try:
                cleanup_path.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                cleanup_path.parent.rmdir()
            except Exception:
                pass


def _database_slot_ok(
    dest: Path,
    analysis: dict,
    *,
    source_db: str | None,
    panel_id: str | None,
) -> bool:
    """Mark database slot complete for sqlite/sql/Hiddify JSON uploads."""
    if not dest.exists():
        return False
    ext = dest.suffix.lower()
    cats = analysis.get("categories") or {}
    paths = analysis.get("paths") or {}
    if panel_id == "hiddify" or analysis.get("panel_hint") == "hiddify":
        return bool(
            paths.get("hiddify_json")
            or cats.get("database_hiddify_json", 0) > 0
            or ext == ".json"
            or cats.get("database_sql", 0) > 0
            or ext == ".sql"
        )
    if source_db == "sqlite":
        return cats.get("database_sqlite", 0) > 0 or ext in (".sqlite3", ".db")
    if source_db in ("mysql", "mariadb"):
        return (
            cats.get("database_sql", 0) > 0
            or ext == ".sql"
            or cats.get("database_hiddify_json", 0) > 0
            or (ext == ".json" and analysis.get("panel_hint") == "hiddify")
        )
    return True


def validate_bundle(
    bundle_id: str,
    panel_id: str | None = None,
    source_db: str | None = None,
    marzban_mode: str | None = None,
) -> dict:
    manifest = _load_manifest(bundle_id)
    panel_id = panel_id or manifest.get("panel_id")
    source_db = source_db or manifest.get("source_db")
    marzban_mode = marzban_mode or manifest.get("marzban_mode")

    if not panel_id:
        return {"ok": False, "complete": False, "slots": [], "missing": [], "mode": "unknown"}

    reqs = get_upload_requirements(panel_id, source_db, marzban_mode)
    if reqs["upload_mode"] == "none":
        return {"ok": True, "complete": True, "slots": [], "missing": [], "mode": "none", "upload_mode": "none"}

    slots_status = []
    missing = []
    zip_slot = manifest["slots"].get("bundle_zip")

    if zip_slot and zip_slot.get("ok") and zip_slot.get("analysis", {}).get("backup_ok"):
        for s in reqs["slots"]:
            slots_status.append({
                "id": s["id"],
                "required": s["required"],
                "ok": True,
                "via": "bundle_zip",
                "filename": zip_slot.get("filename"),
            })
        return {
            "ok": True,
            "complete": True,
            "slots": slots_status,
            "missing": [],
            "mode": "zip",
            "upload_mode": reqs["upload_mode"],
            "analysis": zip_slot.get("analysis"),
        }

    for s in reqs["slots"]:
        if s["id"] == "bundle_zip":
            continue
        meta = manifest["slots"].get(s["id"])
        ok = bool(meta and meta.get("ok"))
        slots_status.append({
            "id": s["id"],
            "required": s["required"],
            "ok": ok,
            "filename": meta.get("filename") if meta else None,
            "label": s["label"],
            "hint": s["hint"],
        })
        if s["required"] and not ok:
            missing.append(s)

    if reqs["upload_mode"] == "optional" and not any(m.get("ok") for m in manifest["slots"].values()):
        return {
            "ok": True,
            "complete": True,
            "slots": slots_status,
            "missing": [],
            "mode": "server",
            "upload_mode": "optional",
        }

    complete = len(missing) == 0
    analysis = None
    db_meta = manifest["slots"].get("database") or zip_slot
    if db_meta:
        analysis = db_meta.get("analysis")

    return {
        "ok": complete,
        "complete": complete,
        "slots": slots_status,
        "missing": [{"id": m["id"], "label": m["label"]} for m in missing],
        "mode": "separate",
        "upload_mode": reqs["upload_mode"],
        "analysis": analysis,
    }


def get_bundle_status(bundle_id: str) -> dict | None:
    try:
        manifest_path = _manifest_path(bundle_id)
    except ValueError:
        return None
    if not manifest_path.exists():
        return None
    manifest = _load_manifest(bundle_id)
    return validate_bundle(
        bundle_id,
        manifest.get("panel_id"),
        manifest.get("source_db"),
        manifest.get("marzban_mode"),
    )


def prepare_bundle_workspace(bundle_id: str) -> Path:
    """Merge all bundle slots into a single work directory for migrators."""
    manifest = _load_manifest(bundle_id)
    work = bundle_dir(bundle_id) / "workspace"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)

    zip_slot = manifest["slots"].get("bundle_zip")
    if zip_slot and zip_slot.get("ok"):
        sdir = _slot_dir(bundle_id, "bundle_zip")
        root = resolve_extract_root(sdir)
        for p in root.rglob("*"):
            if p.is_file():
                rel = p.relative_to(root)
                dest = work / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest)
        return work

    slot = manifest["slots"].get("database")
    if slot:
        src = Path(slot["path"])
        if src.suffix.lower() == ".zip":
            with zipfile.ZipFile(src, "r") as zf:
                zf.extractall(work / "db_extracted")
        else:
            # Preserve panel-specific names (x-ui.db); normalize other sqlite dumps
            lower = src.name.lower()
            if lower == "x-ui.db":
                name = "x-ui.db"
            elif src.suffix.lower() in (".sqlite3", ".db"):
                name = "db.sqlite3"
            else:
                name = src.name
            shutil.copy2(src, work / name)

    for key, subpath in (("env", ".env"), ("xray_config", "xray_config.json")):
        meta = manifest["slots"].get(key)
        if meta:
            shutil.copy2(meta["path"], work / subpath)

    for key, folder in (("certs", "certs"), ("templates", "templates")):
        meta = manifest["slots"].get(key)
        if not meta:
            continue
        src = Path(meta["path"])
        dest = work / folder
        dest.mkdir(parents=True, exist_ok=True)
        if src.suffix.lower() == ".zip":
            with zipfile.ZipFile(src, "r") as zf:
                zf.extractall(dest)
        else:
            shutil.copytree(src, dest, dirs_exist_ok=True)

    return work


def bundle_has_upload(bundle_id: str) -> bool:
    try:
        manifest = _load_manifest(bundle_id)
    except ValueError:
        return False
    return bool(manifest.get("slots"))
