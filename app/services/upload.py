"""File upload handler."""

import shutil
import tempfile
import uuid
from pathlib import Path

from app.config import UPLOAD_DIR
from app.services.archive_guard import safe_extract_zip_file, safe_upload_name
from app.services.backup_analyzer import analyze_upload_directory, get_upload_dir as _dir_for_id


def save_upload(src_path: str | Path, filename: str) -> dict:
    cleanup_tmp = False
    cleanup_path: Path | None = None
    if isinstance(src_path, (bytes, bytearray)):
        tmp_dir = Path(tempfile.mkdtemp(prefix="pg-upload-save-"))
        src_tmp = tmp_dir / safe_upload_name(filename)
        src_tmp.write_bytes(bytes(src_path))
        src_path = src_tmp
        cleanup_tmp = True
        cleanup_path = src_tmp
    try:
        src_path = Path(src_path)
        upload_id = str(uuid.uuid4())[:12]
        dest_dir = UPLOAD_DIR / upload_id
        dest_dir.mkdir(parents=True, exist_ok=True)

        filename = safe_upload_name(filename)
        dest_file = dest_dir / filename
        shutil.copy2(src_path, dest_file)

        zip_error = None
        zip_meta = None

        if filename.lower().endswith(".zip"):
            try:
                zip_meta = safe_extract_zip_file(dest_file, dest_dir / "extracted")
            except ValueError as e:
                zip_error = str(e)

        analysis = analyze_upload_directory(dest_dir)
        detected = _legacy_detected(analysis)

        result = {
            "upload_id": upload_id,
            "filename": filename,
            "path": str(dest_file),
            "size": dest_file.stat().st_size,
            "detected": detected,
            "analysis": analysis,
        }
        if zip_meta:
            result["zip_preflight"] = {
                "files": zip_meta.files,
                "total_uncompressed": zip_meta.total_uncompressed,
                "largest_entry": zip_meta.largest_entry,
                "compression_ratio": zip_meta.compression_ratio,
            }
        if zip_error:
            result["error"] = zip_error
        return result
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


def get_upload_dir(upload_id: str) -> Path | None:
    return _dir_for_id(upload_id, UPLOAD_DIR)


def get_upload_path(upload_id: str) -> str | None:
    upload_dir = get_upload_dir(upload_id)
    if not upload_dir:
        return None
    zips = sorted(p for p in upload_dir.iterdir() if p.is_file() and p.suffix.lower() == ".zip")
    if zips:
        return str(zips[0])
    for f in sorted(upload_dir.iterdir()):
        if f.is_file() and f.name != "stream_meta.json":
            return str(f)
    return str(upload_dir)


def get_upload_analysis(upload_id: str) -> dict | None:
    upload_dir = get_upload_dir(upload_id)
    if not upload_dir:
        return None
    return analyze_upload_directory(upload_dir)


def _legacy_detected(analysis: dict) -> dict:
    return {
        "has_sqlite": analysis["categories"].get("database_sqlite", 0) > 0,
        "has_sql": analysis["categories"].get("database_sql", 0) > 0,
        "has_env": analysis["categories"].get("config_env", 0) > 0,
        "panel_hint": analysis.get("panel_hint"),
        "source_db": analysis.get("detected_source_db"),
        "backup_ok": analysis.get("backup_ok"),
    }
