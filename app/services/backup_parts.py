"""Assemble Telegram-style split backup parts into one clean zip for restore.

Pre-restore only: normalize / unwrap / concat / heal / repack, then hand a
normal ``upload_id`` to the existing analyze/restore path. Does not call
restore or migrate engines.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from app.services.archive_guard import (
    allowed_upload_bytes,
    looks_like_panel_backup_zip,
    resolve_allow_large_for_zip,
    safe_extract_zip_file,
    safe_upload_name,
)
from app.services.upload import save_upload

# Telegram outbound naming: ``stem-1-3.zip`` … ``stem-3-3.zip``
_PART_RE = re.compile(
    r"^(?P<stem>.+)-(?P<index>\d+)-(?P<total>\d+)(?P<suffix>\.[A-Za-z0-9]+)$"
)
# Common alternate splits: ``name.zip.001`` / ``name.z01``
_NUM_SUFFIX_RE = re.compile(
    r"^(?P<stem>.+\.zip)\.(?P<index>\d{1,3})$",
    re.IGNORECASE,
)
_Z_SUFFIX_RE = re.compile(
    r"^(?P<stem>.+)\.z(?P<index>\d{2})$",
    re.IGNORECASE,
)
# Downloaders append `` (1)``, `` copy``, ``.download``; browsers may double ``.zip``.
_NOISE_SUFFIX_RE = re.compile(
    r"(?:\s*\(\d+\))+(?=\.[^.]+$)"
    r"|(?:\s*[-_.]?copy)+(?=\.[^.]+$)"
    r"|(?:\.download|\.crdownload|\.part)$",
    re.IGNORECASE,
)
_DOUBLE_ZIP_RE = re.compile(r"\.zip\.zip$", re.IGNORECASE)

_ZIP_LOCAL = b"PK\x03\x04"
_ZIP_EOCD = b"PK\x05\x06"
_ZIP_EOCD64 = b"PK\x06\x06"
_ZIP_SPAN = b"PK\x07\x08"  # spanning marker some tools dislike


class BackupPartsError(ValueError):
    """User-facing assemble failure with a clear bilingual message."""

    def __init__(self, message: str, *, code: str = "parts_error"):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PartSpec:
    filename: str
    stem: str
    index: int
    total: int
    kind: str  # telegram | zip_num | z_split | whole


@dataclass
class _StagedPart:
    spec: PartSpec
    path: Path
    size: int
    sha256: str


@dataclass
class HealReport:
    notes: list[str] = field(default_factory=list)

    def add(self, fa: str, en: str) -> None:
        self.notes.append(_bilingual(fa, en))


def _bilingual(fa: str, en: str) -> str:
    return f"{fa} — {en}"


def normalize_part_filename(filename: str) -> str:
    """Strip download noise so Telegram part patterns still parse."""
    name = safe_upload_name(filename)
    prev = None
    while prev != name:
        prev = name
        name = _NOISE_SUFFIX_RE.sub("", name)
        if _DOUBLE_ZIP_RE.search(name):
            name = _DOUBLE_ZIP_RE.sub(".zip", name)
    return safe_upload_name(name)


def parse_part_filename(filename: str) -> PartSpec | None:
    """Return part metadata when the name looks like a split volume."""
    name = normalize_part_filename(filename)
    m = _PART_RE.match(name)
    if m:
        index = int(m.group("index"))
        total = int(m.group("total"))
        if total >= 2 and 1 <= index <= total:
            return PartSpec(
                filename=name,
                stem=f"{m.group('stem')}{m.group('suffix')}",
                index=index,
                total=total,
                kind="telegram",
            )
        return None

    m = _NUM_SUFFIX_RE.match(name)
    if m:
        index = int(m.group("index"))
        if index >= 1:
            return PartSpec(
                filename=name,
                stem=m.group("stem"),
                index=index,
                total=0,
                kind="zip_num",
            )

    m = _Z_SUFFIX_RE.match(name)
    if m:
        index = int(m.group("index"))
        if index >= 1:
            return PartSpec(
                filename=name,
                stem=f"{m.group('stem')}.zip",
                index=index,
                total=0,
                kind="z_split",
            )
    return None


def looks_like_part_filename(filename: str) -> bool:
    return parse_part_filename(filename) is not None


def _file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _is_valid_zip(path: Path) -> bool:
    if not zipfile.is_zipfile(path):
        return False
    try:
        with zipfile.ZipFile(path, "r") as zf:
            bad = zf.testzip()
            return bad is None
    except (OSError, zipfile.BadZipFile):
        return False


def _looks_like_html(path: Path) -> bool:
    try:
        head = path.read_bytes()[:256].lstrip().lower()
    except OSError:
        return False
    return head.startswith((b"<!doctype html", b"<html", b"<head"))


def _zip_file_entries(path: Path) -> list[zipfile.ZipInfo]:
    with zipfile.ZipFile(path, "r") as zf:
        return [i for i in zf.infolist() if not i.is_dir()]


def _normalize_unknown_totals(parts: list[PartSpec]) -> list[PartSpec]:
    """Fill total for zip.001 / .z01 sets from the highest index seen."""
    by_key: dict[tuple[str, str], list[PartSpec]] = {}
    for p in parts:
        by_key.setdefault((p.kind, p.stem.lower()), []).append(p)

    out: list[PartSpec] = []
    for (_kind, _stem), group in by_key.items():
        if group[0].total > 0:
            out.extend(group)
            continue
        max_idx = max(g.index for g in group)
        if max_idx < 2 and len(group) < 2:
            out.extend(group)
            continue
        total = max(max_idx, len({g.index for g in group}))
        for g in group:
            out.append(
                PartSpec(
                    filename=g.filename,
                    stem=g.stem,
                    index=g.index,
                    total=total,
                    kind=g.kind,
                )
            )
    return out


def plan_parts(filenames: list[str]) -> tuple[str, list[PartSpec]]:
    """Validate filenames and return (merged_stem, ordered unique PartSpecs)."""
    if not filenames:
        raise BackupPartsError(
            _bilingual(
                "هیچ فایلی برای اسمبل پارت‌ها ارسال نشده.",
                "No files were sent to assemble backup parts.",
            ),
            code="empty",
        )

    cleaned = [normalize_part_filename(n) for n in filenames]
    parsed: list[PartSpec] = []
    wholes: list[str] = []
    for name in cleaned:
        spec = parse_part_filename(name)
        if spec is None:
            wholes.append(name)
        else:
            parsed.append(spec)

    if wholes and parsed:
        raise BackupPartsError(
            _bilingual(
                "فایل‌های کامل و پارت‌های چندتکه با هم مخلوط شده‌اند: "
                f"{', '.join(wholes[:5])} / {[p.filename for p in parsed][:5]}. "
                "فقط پارت‌های یک بکاپ را با هم انتخاب کنید.",
                "Complete archives and split parts were mixed together. "
                "Select only the parts of one backup.",
            ),
            code="mixed_whole_and_parts",
        )

    if not parsed:
        if len(wholes) == 1:
            raise BackupPartsError(
                _bilingual(
                    "این فایل به‌صورت پارت شناخته نشد. برای بکاپ تک‌فایله از آپلود معمولی استفاده کنید.",
                    "This file is not a recognized split part. Use the normal single-file upload.",
                ),
                code="not_parts",
            )
        raise BackupPartsError(
            _bilingual(
                "نام فایل‌ها الگوی پارت بکاپ نیست (مثلاً backup-1-3.zip). "
                "اگر بکاپ تک‌فایله است فقط یک zip آپلود کنید.",
                "Filenames are not recognized split parts (e.g. backup-1-3.zip). "
                "For a single-file backup, upload one zip only.",
            ),
            code="unrecognized_names",
        )

    parsed = _normalize_unknown_totals(parsed)

    stems = {p.stem.lower() for p in parsed}
    totals = {p.total for p in parsed if p.total > 0}
    kinds = {p.kind for p in parsed}
    if len(stems) > 1 or len(kinds) > 1:
        raise BackupPartsError(
            _bilingual(
                "پارت‌ها از چند بکاپ/الگوی مختلف هستند. فقط پارت‌های یک فایل را با هم بفرستید.",
                "Parts belong to different backups or naming patterns. "
                "Send only parts of one archive.",
            ),
            code="mixed_sets",
        )

    total = next(iter(totals)) if totals else max(p.index for p in parsed)
    if total < 2:
        raise BackupPartsError(
            _bilingual(
                "برای اسمبل حداقل ۲ پارت لازم است.",
                "At least 2 parts are required to assemble.",
            ),
            code="need_two",
        )

    by_index: dict[int, PartSpec] = {}
    for p in parsed:
        if p.total and p.total != total:
            raise BackupPartsError(
                _bilingual(
                    f"تعداد پارت‌ها ناسازگار است (یکی {p.total} و دیگری {total}).",
                    f"Inconsistent part totals ({p.total} vs {total}).",
                ),
                code="total_mismatch",
            )
        if p.index < 1 or p.index > total:
            raise BackupPartsError(
                _bilingual(
                    f"شماره پارت نامعتبر: {p.filename} (باید بین ۱ و {total} باشد).",
                    f"Invalid part index in {p.filename} (expected 1..{total}).",
                ),
                code="bad_index",
            )
        prev = by_index.get(p.index)
        if prev and prev.filename != p.filename:
            raise BackupPartsError(
                _bilingual(
                    f"دو فایل برای پارت {p.index}/{total} آمده: {prev.filename} و {p.filename}.",
                    f"Two files claim part {p.index}/{total}: {prev.filename} and {p.filename}.",
                ),
                code="duplicate_index",
            )
        by_index[p.index] = PartSpec(
            filename=p.filename,
            stem=p.stem,
            index=p.index,
            total=total,
            kind=p.kind,
        )

    missing = [i for i in range(1, total + 1) if i not in by_index]
    if missing:
        have = ", ".join(f"{i}/{total}" for i in sorted(by_index))
        need = ", ".join(f"{i}/{total}" for i in missing)
        raise BackupPartsError(
            _bilingual(
                f"پارت‌های ناقص. موجود: {have}. ناقص: {need}. همهٔ {total} پارت را با هم انتخاب کنید.",
                f"Incomplete parts. Have: {have}. Missing: {need}. Select all {total} parts together.",
            ),
            code="missing_parts",
        )

    ordered = [by_index[i] for i in range(1, total + 1)]
    return ordered[0].stem, ordered


def _expand_inputs(
    items: list[tuple[str, Path]],
    work: Path,
    heals: HealReport,
) -> list[tuple[str, Path]]:
    """Unwrap zip-of-parts and single-file wrappers that break manual extract."""
    out: list[tuple[str, Path]] = []
    for idx, (raw_name, path) in enumerate(items):
        name = normalize_part_filename(raw_name)
        if _looks_like_html(path):
            raise BackupPartsError(
                _bilingual(
                    f"«{raw_name}» صفحه HTML است نه پارت بکاپ (دانلود خراب).",
                    f"«{raw_name}» is an HTML page, not a backup part (failed download).",
                ),
                code="html_download",
            )

        if not zipfile.is_zipfile(path):
            out.append((name, path))
            continue

        try:
            entries = _zip_file_entries(path)
        except zipfile.BadZipFile:
            out.append((name, path))
            continue

        partish = [
            e for e in entries
            if parse_part_filename(Path(e.filename.replace("\\", "/")).name) is not None
        ]

        # One zip that contains the whole split set → extract those parts.
        if len(partish) >= 2:
            box = work / f"container-{idx}"
            box.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(path, "r") as zf:
                for e in partish:
                    base = normalize_part_filename(Path(e.filename.replace("\\", "/")).name)
                    dest = box / base
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(e) as src, dest.open("wb") as dst:
                        shutil.copyfileobj(src, dst, length=1024 * 1024)
                    out.append((base, dest))
            heals.add(
                f"از داخل «{raw_name}» پارت‌های بکاپ استخراج شد.",
                f"Extracted backup parts from inside «{raw_name}».",
            )
            continue

        # Each Telegram chunk re-zipped alone → unwrap the single payload.
        if len(entries) == 1 and looks_like_part_filename(name):
            e = entries[0]
            inner_name = normalize_part_filename(Path(e.filename.replace("\\", "/")).name)
            box = work / f"unwrap-{idx}"
            box.mkdir(parents=True, exist_ok=True)
            dest = box / (inner_name if inner_name else name)
            with zipfile.ZipFile(path, "r") as zf, zf.open(e) as src, dest.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            # Keep the outer part name for ordering; payload is unwrapped bytes.
            out.append((name, dest))
            heals.add(
                f"لایه zip اضافه از «{raw_name}» برداشته شد.",
                f"Removed extra zip wrapper from «{raw_name}».",
            )
            continue

        # Single complete backup zip (not a part set)
        if len(entries) >= 1 and not looks_like_part_filename(name) and _is_valid_zip(path):
            out.append((name if name.lower().endswith(".zip") else f"{Path(name).stem}.zip", path))
            continue

        out.append((name, path))

    return out


def _find_eocd_offset(data: bytes) -> int | None:
    """Return start of EOCD (classic EOCD lives in the last 64KiB+22 bytes)."""
    max_back = min(len(data), (1 << 16) + 22)
    window = data[-max_back:]
    pos = window.rfind(_ZIP_EOCD)
    if pos >= 0:
        return len(data) - len(window) + pos
    pos = data.rfind(_ZIP_EOCD64)
    if pos >= 0:
        return pos
    return None


def heal_concatenated_zip(src: Path, dest: Path, heals: HealReport) -> Path:
    """Trim leading/trailing junk so a concatenated Telegram split becomes a real zip."""
    data = src.read_bytes()
    if not data:
        raise BackupPartsError(
            _bilingual("فایل اسمبل‌شده خالی است.", "Assembled file is empty."),
            code="empty_merge",
        )

    start = data.find(_ZIP_LOCAL)
    if start < 0:
        start = data.find(_ZIP_SPAN)
    if start < 0:
        raise BackupPartsError(
            _bilingual(
                "بعد از چسباندن پارت‌ها امضای zip پیدا نشد. "
                "پارت‌ها ناقص‌اند یا از چند بکاپ مخلوط شده‌اند. "
                "(پارت‌های تلگرام به‌تنهایی قابل اکسترکت دستی نیستند — باید با هم چسبانده شوند.)",
                "No zip signature after concatenating parts. "
                "Parts are incomplete or mixed from different backups. "
                "(Telegram parts are not manually extractable alone — they must be merged.)",
            ),
            code="merge_not_zip",
        )

    if start > 0:
        data = data[start:]
        heals.add(
            f"{start} بایت زائد از ابتدای فایل حذف شد.",
            f"Removed {start} leading junk bytes.",
        )

    eocd = _find_eocd_offset(data)
    if eocd is not None:
        # EOCD size = 22 + comment length (uint16 at offset eocd+20)
        comment_len = 0
        if data[eocd:eocd + 4] == _ZIP_EOCD and eocd + 22 <= len(data):
            comment_len = int.from_bytes(data[eocd + 20 : eocd + 22], "little")
            end = eocd + 22 + comment_len
            if end < len(data):
                trimmed = len(data) - end
                data = data[:end]
                heals.add(
                    f"{trimmed} بایت زائد از انتهای فایل حذف شد.",
                    f"Removed {trimmed} trailing junk bytes.",
                )
            elif end > len(data):
                # Truncated comment — keep what we have; ZipFile may still open.
                heals.add(
                    "کامنت انتهای zip ناقص بود؛ با دادهٔ موجود ادامه داده شد.",
                    "Zip end comment was truncated; continued with available bytes.",
                )

    dest.write_bytes(data)
    return dest


def repack_clean_zip(src: Path, dest: Path, heals: HealReport, *, allow_large: bool) -> Path:
    """Extract then write a fresh non-spanned zip so restore extract is reliable."""
    extract_dir = dest.parent / f"{dest.stem}-extracted"
    if extract_dir.exists():
        shutil.rmtree(extract_dir, ignore_errors=True)
    extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        safe_extract_zip_file(src, extract_dir, allow_large=allow_large)
    except ValueError as e:
        # Retry once after a second heal pass (in case extract guard tripped on odd paths).
        raise BackupPartsError(
            _bilingual(
                f"اکسترکت zip اسمبل‌شده ناموفق بود: {e}. "
                "پارت‌ها را دوباره از تلگرام دانلود کنید و همه را با هم آپلود کنید.",
                f"Extracting the assembled zip failed: {e}. "
                "Re-download the parts from Telegram and upload them all together.",
            ),
            code="extract_failed",
        ) from e

    files = [p for p in extract_dir.rglob("*") if p.is_file()]
    if not files:
        raise BackupPartsError(
            _bilingual(
                "بعد از اکسترکت هیچ فایلی داخل بکاپ نبود.",
                "Assembled zip extracted to zero files.",
            ),
            code="empty_extract",
        )

    # Collapse a single wrapper directory (common when users zip a folder).
    top_dirs = [p for p in extract_dir.iterdir() if p.is_dir()]
    top_files = [p for p in extract_dir.iterdir() if p.is_file()]
    root = extract_dir
    if not top_files and len(top_dirs) == 1:
        only = top_dirs[0]
        # Only unwrap generic wrapper names — keep real panel trees.
        if only.name.lower() in {
            "backup", "backups", "extract", "extracted", "tmp", "temp",
            "download", "downloads", "pasarguard-backup", "pg-backup",
        }:
            root = only
            heals.add(
                f"پوشهٔ اضافی «{only.name}» از ریشه بکاپ حذف شد.",
                f"Removed extra wrapper folder «{only.name}» from backup root.",
            )

    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            rel = path.relative_to(root).as_posix()
            # Normalize unsafe / Windows paths into a clean relative name.
            rel = rel.lstrip("/").replace("\\", "/")
            if ".." in rel.split("/"):
                continue
            zf.write(path, arcname=rel)

    heals.add(
        "یک zip تمیز و یک‌تکه از محتوای اکسترکت‌شده ساخته شد.",
        "Built a clean single-volume zip from extracted contents.",
    )
    return dest


def _verify_zip_readable(path: Path) -> None:
    if not zipfile.is_zipfile(path):
        raise BackupPartsError(
            _bilingual(
                "بعد از ترمیم هنوز zip معتبر نیست. پارت‌ها ناقص یا خراب‌اند.",
                "Still not a valid zip after healing. Parts are incomplete or corrupt.",
            ),
            code="merge_not_zip",
        )
    try:
        with zipfile.ZipFile(path, "r") as zf:
            bad = zf.testzip()
            if bad is not None:
                raise BackupPartsError(
                    _bilingual(
                        f"zip بعد از اسمبل خراب است (ورود خراب: {bad}). پارت‌ها را دوباره دانلود/آپلود کنید.",
                        f"Assembled zip is corrupt (bad entry: {bad}). Re-download/upload the parts.",
                    ),
                    code="merge_crc",
                )
            if not zf.namelist():
                raise BackupPartsError(
                    _bilingual(
                        "zip اسمبل‌شده خالی است.",
                        "Assembled zip has no entries.",
                    ),
                    code="empty_zip",
                )
    except zipfile.BadZipFile as e:
        raise BackupPartsError(
            _bilingual(
                "zip اسمبل‌شده قابل خواندن نیست. پارت‌ها ناقص یا خراب‌اند.",
                "Assembled zip cannot be read. Parts are incomplete or corrupt.",
            ),
            code="merge_bad_zip",
        ) from e


def _save_result(
    path: Path,
    filename: str,
    *,
    allow_large: bool,
    assembled: bool,
    parts_count: int,
    parts_meta: list[dict] | None,
    heals: HealReport,
) -> dict:
    use_large = bool(allow_large)
    if not use_large and path.stat().st_size > allowed_upload_bytes(False):
        if looks_like_panel_backup_zip(path):
            use_large = True
        else:
            limit_mb = allowed_upload_bytes(False) // (1024 * 1024)
            raise BackupPartsError(
                _bilingual(
                    f"حجم بکاپ اسمبل‌شده از {limit_mb}MB بیشتر است. تیک «آپلود بزرگ» را بزنید "
                    "یا یک بکاپ استاندارد پنل آپلود کنید.",
                    f"Assembled backup exceeds {limit_mb}MB. Enable «large upload» "
                    "or upload a standard panel backup.",
                ),
                code="need_large",
            )
    use_large = resolve_allow_large_for_zip(path, use_large)
    result = save_upload(path, filename, allow_large=use_large)
    if result.get("error"):
        raise BackupPartsError(str(result["error"]), code="zip_extract")
    result = dict(result)
    result["assembled_from_parts"] = assembled
    result["parts_count"] = parts_count
    result["merged_filename"] = filename
    if parts_meta is not None:
        result["parts"] = parts_meta
    if heals.notes:
        result["heals"] = list(heals.notes)
    return result


def assemble_part_paths(
    items: list[tuple[str, Path]],
    *,
    allow_large: bool = False,
) -> dict:
    """Normalize, merge, heal, repack parts → ``save_upload`` clean zip."""
    if not items:
        raise BackupPartsError(
            _bilingual(
                "هیچ فایلی برای اسمبل پارت‌ها ارسال نشده.",
                "No files were sent to assemble backup parts.",
            ),
            code="empty",
        )

    heals = HealReport()
    tmp_dir = Path(tempfile.mkdtemp(prefix="pg-parts-assemble-"))
    try:
        expanded = _expand_inputs(items, tmp_dir / "expanded", heals)

        # Single complete zip after unwrap / container expansion
        if len(expanded) == 1:
            name, path = expanded[0]
            name = normalize_part_filename(name)
            if _is_valid_zip(path):
                out_name = name if name.lower().endswith(".zip") else f"{Path(name).stem}.zip"
                clean = tmp_dir / out_name
                try:
                    repack_clean_zip(path, clean, heals, allow_large=allow_large)
                    final_path = clean
                except BackupPartsError:
                    # Already a fine zip — hand through without failing the upload.
                    final_path = path
                return _save_result(
                    final_path,
                    out_name,
                    allow_large=allow_large,
                    assembled=False,
                    parts_count=1,
                    parts_meta=None,
                    heals=heals,
                )
            spec = parse_part_filename(name)
            if spec:
                raise BackupPartsError(
                    _bilingual(
                        f"فقط پارت {spec.index}/{spec.total or '?'} ({name}) آمده و فایل zip کامل نیست. "
                        f"همهٔ پارت‌های این بکاپ را با هم انتخاب و آپلود کنید. "
                        f"پارت‌های تلگرام را جداگانه اکسترکت نکنید.",
                        f"Only part {spec.index}/{spec.total or '?'} ({name}) was uploaded and it is not a "
                        f"complete zip. Select and upload every part together. "
                        f"Do not extract Telegram parts individually.",
                    ),
                    code="single_part",
                )
            raise BackupPartsError(
                _bilingual(
                    f"فایل «{name}» zip معتبر نیست.",
                    f"File «{name}» is not a valid zip archive.",
                ),
                code="invalid_zip",
            )

        # Map by normalized name (download noise already stripped).
        by_name: dict[str, Path] = {}
        for n, p in expanded:
            key = normalize_part_filename(n)
            if key in by_name and _file_digest(by_name[key]) != _file_digest(p):
                raise BackupPartsError(
                    _bilingual(
                        f"دو محتوای متفاوت با نام پارت «{key}» آپلود شده.",
                        f"Conflicting content for part name «{key}».",
                    ),
                    code="content_conflict",
                )
            by_name[key] = p

        stem, ordered_specs = plan_parts(list(by_name.keys()))

        staged: list[_StagedPart] = []
        total_size = 0
        for spec in ordered_specs:
            path = by_name.get(spec.filename)
            if path is None or not path.is_file():
                raise BackupPartsError(
                    _bilingual(
                        f"فایل پارت پیدا نشد: {spec.filename}",
                        f"Part file not found: {spec.filename}",
                    ),
                    code="missing_file",
                )
            size = path.stat().st_size
            if size <= 0:
                raise BackupPartsError(
                    _bilingual(
                        f"پارت خالی است: {spec.filename}",
                        f"Empty part file: {spec.filename}",
                    ),
                    code="empty_part",
                )
            if _looks_like_html(path):
                raise BackupPartsError(
                    _bilingual(
                        f"پارت «{spec.filename}» HTML است (دانلود خراب).",
                        f"Part «{spec.filename}» is HTML (failed download).",
                    ),
                    code="html_download",
                )
            digest = _file_digest(path)
            total_size += size
            staged.append(_StagedPart(spec=spec, path=path, size=size, sha256=digest))

        stream_cap = allowed_upload_bytes(True)
        if total_size > stream_cap:
            limit_mb = stream_cap // (1024 * 1024)
            raise BackupPartsError(
                _bilingual(
                    f"حجم مجموع پارت‌ها از {limit_mb} مگابایت بیشتر است.",
                    f"Combined parts exceed {limit_mb} MB.",
                ),
                code="too_large",
            )

        merged_name = safe_upload_name(stem if stem.lower().endswith(".zip") else f"{stem}.zip")
        raw_merged = tmp_dir / f"raw-{merged_name}"
        with raw_merged.open("wb") as out:
            for part in staged:
                with part.path.open("rb") as inp:
                    shutil.copyfileobj(inp, out, length=1024 * 1024)

        heals.add(
            f"{len(staged)} پارت به ترتیب چسبانده شد.",
            f"Concatenated {len(staged)} parts in order.",
        )

        healed = tmp_dir / f"healed-{merged_name}"
        heal_concatenated_zip(raw_merged, healed, heals)
        _verify_zip_readable(healed)

        clean = tmp_dir / merged_name
        repack_clean_zip(healed, clean, heals, allow_large=allow_large or looks_like_panel_backup_zip(healed))
        _verify_zip_readable(clean)

        return _save_result(
            clean,
            merged_name,
            allow_large=allow_large,
            assembled=True,
            parts_count=len(staged),
            parts_meta=[
                {
                    "filename": p.spec.filename,
                    "index": p.spec.index,
                    "total": p.spec.total,
                    "size": p.size,
                }
                for p in staged
            ],
            heals=heals,
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
