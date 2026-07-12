"""File upload handler."""

import uuid
import zipfile
from pathlib import Path

from app.config import UPLOAD_DIR
from app.services.backup_analyzer import analyze_upload_directory, get_upload_dir as _dir_for_id


def save_upload(file_content: bytes, filename: str) -> dict:
    upload_id = str(uuid.uuid4())[:12]
    dest_dir = UPLOAD_DIR / upload_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_file = dest_dir / filename
    dest_file.write_bytes(file_content)

    if filename.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(dest_file, "r") as zf:
                zf.extractall(dest_dir / "extracted")
        except zipfile.BadZipFile:
            pass

    analysis = analyze_upload_directory(dest_dir)
    detected = _legacy_detected(analysis)

    return {
        "upload_id": upload_id,
        "filename": filename,
        "path": str(dest_file),
        "size": len(file_content),
        "detected": detected,
        "analysis": analysis,
    }


def get_upload_dir(upload_id: str) -> Path | None:
    return _dir_for_id(upload_id, UPLOAD_DIR)


def get_upload_path(upload_id: str) -> str | None:
    upload_dir = get_upload_dir(upload_id)
    if not upload_dir:
        return None
    for f in upload_dir.iterdir():
        if f.is_file():
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
