"""File upload handler."""

import shutil
import uuid
import zipfile
from pathlib import Path

from app.config import UPLOAD_DIR


def save_upload(file_content: bytes, filename: str) -> dict:
    upload_id = str(uuid.uuid4())[:12]
    dest_dir = UPLOAD_DIR / upload_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_file = dest_dir / filename
    dest_file.write_bytes(file_content)

    extracted = []
    if filename.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(dest_file, "r") as zf:
                zf.extractall(dest_dir / "extracted")
            extracted = [str(p.relative_to(dest_dir)) for p in (dest_dir / "extracted").rglob("*") if p.is_file()]
        except zipfile.BadZipFile:
            pass

    # Auto-detect contents
    detected = _detect_contents(dest_dir)

    return {
        "upload_id": upload_id,
        "filename": filename,
        "path": str(dest_file),
        "size": len(file_content),
        "extracted_files": extracted[:20],
        "detected": detected,
    }


def get_upload_path(upload_id: str) -> str | None:
    upload_dir = UPLOAD_DIR / upload_id
    if not upload_dir.exists():
        return None
    files = list(upload_dir.iterdir())
    if not files:
        return None
    # Return the main uploaded file (not extracted subdir)
    for f in files:
        if f.is_file():
            return str(f)
    return str(upload_dir)


def _detect_contents(directory: Path) -> dict:
    detected = {
        "has_sqlite": False,
        "has_sql": False,
        "has_env": False,
        "panel_hint": None,
    }

    search_dir = directory / "extracted" if (directory / "extracted").exists() else directory

    for p in search_dir.rglob("*"):
        if not p.is_file():
            continue
        name = p.name.lower()
        if name in ("db.sqlite3", "x-ui.db", "marzban.db"):
            detected["has_sqlite"] = True
            if "x-ui" in name:
                detected["panel_hint"] = "3x-ui"
            elif "marzban" in name or name == "db.sqlite3":
                detected["panel_hint"] = "marzban"
        if name.endswith(".sql"):
            detected["has_sql"] = True
            if "hiddify" in str(p).lower():
                detected["panel_hint"] = "hiddify"
            elif "marzban" in str(p).lower():
                detected["panel_hint"] = "marzban"
        if name == ".env":
            detected["has_env"] = True

    return detected
