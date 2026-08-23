"""Zip upload safety guards shared by upload, restore, and cleanup flows."""

from __future__ import annotations

import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path


DEFAULT_MAX_UPLOAD_BYTES = 500 * 1024 * 1024
DEFAULT_MAX_OVERRIDE_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_ZIP_FILES = 20_000
DEFAULT_MAX_ZIP_ENTRY_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_ZIP_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
DEFAULT_MAX_ZIP_RATIO = 200


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


MAX_UPLOAD_BYTES = _env_int("PG_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES)
MAX_OVERRIDE_UPLOAD_BYTES = _env_int("PG_MAX_OVERRIDE_UPLOAD_BYTES", DEFAULT_MAX_OVERRIDE_UPLOAD_BYTES)
MAX_ZIP_FILES = _env_int("PG_MAX_ZIP_FILES", DEFAULT_MAX_ZIP_FILES)
MAX_ZIP_ENTRY_BYTES = _env_int("PG_MAX_ZIP_ENTRY_BYTES", DEFAULT_MAX_ZIP_ENTRY_BYTES)
MAX_ZIP_TOTAL_BYTES = _env_int("PG_MAX_ZIP_TOTAL_BYTES", DEFAULT_MAX_ZIP_TOTAL_BYTES)
MAX_ZIP_RATIO = _env_int("PG_MAX_ZIP_RATIO", DEFAULT_MAX_ZIP_RATIO)


@dataclass(frozen=True)
class ZipPreflight:
    files: int
    total_uncompressed: int
    total_compressed: int
    largest_entry: int
    compression_ratio: int


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def is_safe_id(value: str | None) -> bool:
    """True for ids we generate (uuid slices) — never a path fragment."""
    return bool(value and _SAFE_ID_RE.match(value))


def resolve_within(base: Path, name: str | None) -> Path | None:
    """base/name, but only when `name` is a safe id that stays inside `base`."""
    if not is_safe_id(name):
        return None
    target = base / str(name)
    try:
        target.resolve().relative_to(base.resolve())
    except (ValueError, OSError):
        return None
    return target


def safe_upload_name(filename: str | None) -> str:
    name = Path(filename or "upload.bin").name.strip()
    if not name or name in (".", ".."):
        return "upload.bin"
    return name.replace("\x00", "_")


def allowed_upload_bytes(allow_override: bool = False) -> int:
    if allow_override and MAX_OVERRIDE_UPLOAD_BYTES > MAX_UPLOAD_BYTES:
        return MAX_OVERRIDE_UPLOAD_BYTES
    return MAX_UPLOAD_BYTES


def preflight_zip_file(path: str | Path) -> ZipPreflight:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            return preflight_zip(zf)
    except zipfile.BadZipFile as e:
        raise ValueError("Bad zip file") from e


def preflight_zip(zf: zipfile.ZipFile) -> ZipPreflight:
    infos = zf.infolist()
    total_uncompressed = 0
    total_compressed = 0
    files = 0
    largest_entry = 0

    for info in infos:
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/"):
            raise ValueError(f"Unsafe zip entry: {info.filename}")
        if info.is_dir():
            continue
        files += 1
        if files > MAX_ZIP_FILES:
            raise ValueError("Zip contains too many files")
        if info.file_size > MAX_ZIP_ENTRY_BYTES:
            raise ValueError(f"Zip entry too large: {info.filename}")
        total_uncompressed += info.file_size
        total_compressed += max(info.compress_size, 0)
        largest_entry = max(largest_entry, info.file_size)
        if total_uncompressed > MAX_ZIP_TOTAL_BYTES:
            raise ValueError("Zip expands beyond the safe extraction limit")

    ratio_base = max(total_compressed, 1)
    compression_ratio = total_uncompressed // ratio_base if total_uncompressed else 0
    if total_uncompressed and compression_ratio > MAX_ZIP_RATIO:
        raise ValueError("Zip compression ratio is too high")

    return ZipPreflight(
        files=files,
        total_uncompressed=total_uncompressed,
        total_compressed=total_compressed,
        largest_entry=largest_entry,
        compression_ratio=compression_ratio,
    )


def safe_extract_zip_file(path: str | Path, dest: Path) -> ZipPreflight:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            return safe_extract(zf, dest)
    except zipfile.BadZipFile as e:
        raise ValueError("Bad zip file") from e


def safe_extract(zf: zipfile.ZipFile, dest: Path) -> ZipPreflight:
    report = preflight_zip(zf)
    dest.mkdir(parents=True, exist_ok=True)
    extracted_total = 0
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        target = dest / name
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
        extracted_total += info.file_size
        if extracted_total > MAX_ZIP_TOTAL_BYTES:
            raise ValueError("Zip expands beyond the safe extraction limit")
    return report
