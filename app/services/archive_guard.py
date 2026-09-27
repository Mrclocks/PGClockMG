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
# Panel sqlite dumps of several GB are legitimate; zip-bomb still constrained by
# compression ratio + total uncompressed caps.
DEFAULT_MAX_ZIP_ENTRY_BYTES = 8 * 1024 * 1024 * 1024
DEFAULT_MAX_ZIP_TOTAL_BYTES = 16 * 1024 * 1024 * 1024
DEFAULT_MAX_ZIP_RATIO = 200
# When the UI "large upload" override is on, raise entry/total further.
DEFAULT_MAX_OVERRIDE_ZIP_ENTRY_BYTES = 32 * 1024 * 1024 * 1024
DEFAULT_MAX_OVERRIDE_ZIP_TOTAL_BYTES = 64 * 1024 * 1024 * 1024


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
MAX_OVERRIDE_ZIP_ENTRY_BYTES = _env_int(
    "PG_MAX_OVERRIDE_ZIP_ENTRY_BYTES", DEFAULT_MAX_OVERRIDE_ZIP_ENTRY_BYTES,
)
MAX_OVERRIDE_ZIP_TOTAL_BYTES = _env_int(
    "PG_MAX_OVERRIDE_ZIP_TOTAL_BYTES", DEFAULT_MAX_OVERRIDE_ZIP_TOTAL_BYTES,
)


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


def zip_entry_limit_bytes(allow_large: bool = False) -> int:
    """Per-member uncompressed limit; large-upload override raises the ceiling."""
    if allow_large:
        return max(MAX_ZIP_ENTRY_BYTES, MAX_OVERRIDE_ZIP_ENTRY_BYTES)
    return MAX_ZIP_ENTRY_BYTES


def zip_total_limit_bytes(allow_large: bool = False) -> int:
    if allow_large:
        return max(MAX_ZIP_TOTAL_BYTES, MAX_OVERRIDE_ZIP_TOTAL_BYTES)
    return MAX_ZIP_TOTAL_BYTES


def looks_like_panel_backup_zip(path: str | Path | zipfile.ZipFile) -> bool:
    """True when zip namelist looks like a PasarGuard/PGClock panel backup.

    Used to auto-raise large-upload ceilings for legitimate panel dumps without
    weakening ratio / path-traversal bomb guards.
    """
    try:
        if isinstance(path, zipfile.ZipFile):
            names = [n.replace("\\", "/").lstrip("./") for n in path.namelist()]
        else:
            with zipfile.ZipFile(path, "r") as zf:
                names = [n.replace("\\", "/").lstrip("./") for n in zf.namelist()]
    except (OSError, zipfile.BadZipFile):
        return False

    lower = [n.lower() for n in names]
    has_db = False
    for n in lower:
        base = n.rsplit("/", 1)[-1]
        if base in ("db.sqlite3", "db_backup.sql") or n.endswith("/db.sqlite3"):
            has_db = True
            break
        if n == "pg_dump/manifest.tsv" or n.startswith("pg_dump/"):
            has_db = True
            break
        if base.endswith(".sql") and "dump" in base:
            has_db = True
            break
    if not has_db:
        # Any root-level .sql / .sqlite3 alongside .env is enough.
        has_env = any(n == ".env" or n.endswith("/.env") for n in lower)
        has_sqlish = any(
            n.rsplit("/", 1)[-1].endswith((".sql", ".sqlite3", ".db"))
            for n in lower
            if not n.endswith("/")
        )
        return has_env and has_sqlish
    return True


def resolve_allow_large_for_zip(path: str | Path, requested: bool = False) -> bool:
    """Honor explicit override, else auto-large for recognized panel backup zips."""
    if requested:
        return True
    return looks_like_panel_backup_zip(path)


def _entry_too_large_message(filename: str, size: int, limit: int, *, allow_large: bool) -> str:
    size_mb = max(1, size // (1024 * 1024))
    limit_mb = max(1, limit // (1024 * 1024))
    if allow_large:
        return (
            f"Zip entry too large: {filename} ({size_mb} MB > {limit_mb} MB). "
            f"Raise PG_MAX_OVERRIDE_ZIP_ENTRY_BYTES (or PG_MAX_ZIP_ENTRY_BYTES) on the server."
        )
    return (
        f"Zip entry too large: {filename} ({size_mb} MB > {limit_mb} MB). "
        f"Enable «large upload» override in the wizard, or raise PG_MAX_ZIP_ENTRY_BYTES."
    )


def preflight_zip(zf: zipfile.ZipFile, *, allow_large: bool = False) -> ZipPreflight:
    infos = zf.infolist()
    total_uncompressed = 0
    total_compressed = 0
    files = 0
    largest_entry = 0
    entry_limit = zip_entry_limit_bytes(allow_large)
    total_limit = zip_total_limit_bytes(allow_large)

    for info in infos:
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/"):
            raise ValueError(f"Unsafe zip entry: {info.filename}")
        if info.is_dir():
            continue
        files += 1
        if files > MAX_ZIP_FILES:
            raise ValueError("Zip contains too many files")
        if info.file_size > entry_limit:
            raise ValueError(
                _entry_too_large_message(
                    info.filename, info.file_size, entry_limit, allow_large=allow_large,
                )
            )
        total_uncompressed += info.file_size
        total_compressed += max(info.compress_size, 0)
        largest_entry = max(largest_entry, info.file_size)
        if total_uncompressed > total_limit:
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


def safe_extract_zip_file(
    path: str | Path,
    dest: Path,
    *,
    allow_large: bool = False,
) -> ZipPreflight:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            return safe_extract(zf, dest, allow_large=allow_large)
    except zipfile.BadZipFile as e:
        raise ValueError("Bad zip file") from e


def safe_extract(
    zf: zipfile.ZipFile,
    dest: Path,
    *,
    allow_large: bool = False,
) -> ZipPreflight:
    report = preflight_zip(zf, allow_large=allow_large)
    entry_limit = zip_entry_limit_bytes(allow_large)
    total_limit = zip_total_limit_bytes(allow_large)
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    extracted_total = 0
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if not name or name.startswith("/") or ".." in name.split("/"):
            raise ValueError(f"Unsafe zip entry: {info.filename}")
        target = dest / name
        try:
            resolved = target.resolve()
            resolved.relative_to(dest_resolved)
        except (ValueError, OSError) as exc:
            raise ValueError(f"Unsafe zip entry: {info.filename}") from exc
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with zf.open(info) as src, open(target, "wb") as out:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                written += len(chunk)
                if written > entry_limit:
                    raise ValueError(
                        _entry_too_large_message(
                            info.filename, written, entry_limit, allow_large=allow_large,
                        )
                    )
                if extracted_total + written > total_limit:
                    raise ValueError("Zip expands beyond the safe extraction limit")
        extracted_total += written
        if extracted_total > total_limit:
            raise ValueError("Zip expands beyond the safe extraction limit")
    return report
