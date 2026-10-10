"""Assemble Telegram-style split backup parts into one zip for restore.

This is a pre-restore layer only. It never calls restore/migrate logic —
callers hand the resulting ``upload_id`` to the existing analyze/restore path.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.services.archive_guard import (
    allowed_upload_bytes,
    looks_like_panel_backup_zip,
    resolve_allow_large_for_zip,
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


def parse_part_filename(filename: str) -> PartSpec | None:
    """Return part metadata when the name looks like a split volume."""
    name = safe_upload_name(filename)
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
            # total unknown until we see the set; placeholder 0 → filled later
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


def _bilingual(fa: str, en: str) -> str:
    return f"{fa} — {en}"


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

    cleaned = [safe_upload_name(n) for n in filenames]
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

    # Deduplicate by index (prefer first; conflict checked later via digest)
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


def assemble_part_paths(
    items: list[tuple[str, Path]],
    *,
    allow_large: bool = False,
) -> dict:
    """Concatenate ordered parts, verify zip, then ``save_upload`` the result.

    ``items`` is ``(original_filename, path)`` for each uploaded part.
    """
    if not items:
        raise BackupPartsError(
            _bilingual(
                "هیچ فایلی برای اسمبل پارت‌ها ارسال نشده.",
                "No files were sent to assemble backup parts.",
            ),
            code="empty",
        )

    # Single complete zip (auto-heal mis-routed or oddly named whole archive)
    if len(items) == 1:
        name, path = items[0]
        name = safe_upload_name(name)
        if _is_valid_zip(path):
            result = save_upload(path, name if name.lower().endswith(".zip") else f"{Path(name).stem}.zip",
                                 allow_large=allow_large)
            if result.get("error"):
                raise BackupPartsError(str(result["error"]), code="zip_extract")
            result = dict(result)
            result["assembled_from_parts"] = False
            result["parts_count"] = 1
            return result
        spec = parse_part_filename(name)
        if spec:
            raise BackupPartsError(
                _bilingual(
                    f"فقط پارت {spec.index}/{spec.total or '?'} ({name}) آمده و فایل zip کامل نیست. "
                    f"همهٔ پارت‌های این بکاپ را با هم انتخاب و آپلود کنید.",
                    f"Only part {spec.index}/{spec.total or '?'} ({name}) was uploaded and it is not a "
                    f"complete zip. Select and upload every part of this backup together.",
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

    names = [safe_upload_name(n) for n, _ in items]
    stem, ordered_specs = plan_parts(names)
    by_name = {safe_upload_name(n): Path(p) for n, p in items}

    staged: list[_StagedPart] = []
    total_size = 0
    digest_by_index: dict[int, str] = {}
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
        digest = _file_digest(path)
        prev = digest_by_index.get(spec.index)
        if prev and prev != digest:
            raise BackupPartsError(
                _bilingual(
                    f"دو محتوای متفاوت برای پارت {spec.index}/{spec.total} آپلود شده.",
                    f"Conflicting content for part {spec.index}/{spec.total}.",
                ),
                code="content_conflict",
            )
        digest_by_index[spec.index] = digest
        total_size += size
        staged.append(_StagedPart(spec=spec, path=path, size=size, sha256=digest))

    # Same streaming ceiling as zip uploads: allow up to the override cap, then
    # require panel-backup layout / explicit large tick after merge.
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

    tmp_dir = Path(tempfile.mkdtemp(prefix="pg-parts-assemble-"))
    merged_name = safe_upload_name(stem if stem.lower().endswith(".zip") else f"{stem}.zip")
    merged_path = tmp_dir / merged_name
    try:
        with merged_path.open("wb") as out:
            for part in staged:
                with part.path.open("rb") as inp:
                    shutil.copyfileobj(inp, out, length=1024 * 1024)

        if not zipfile.is_zipfile(merged_path):
            raise BackupPartsError(
                _bilingual(
                    "بعد از چسباندن پارت‌ها فایل zip معتبر ساخته نشد. "
                    "پارت‌ها ناقص‌اند، ترتیب/مجموعه اشتباه است، یا یکی خراب شده. "
                    "همهٔ پارت‌های همان بکاپ را دوباره انتخاب کنید.",
                    "Concatenated parts did not form a valid zip. "
                    "Parts are incomplete, from the wrong set, or corrupted. "
                    "Re-select every part of the same backup.",
                ),
                code="merge_not_zip",
            )

        try:
            with zipfile.ZipFile(merged_path, "r") as zf:
                bad = zf.testzip()
                if bad is not None:
                    raise BackupPartsError(
                        _bilingual(
                            f"zip بعد از اسمبل خراب است (ورود خراب: {bad}). پارت‌ها را دوباره دانلود/آپلود کنید.",
                            f"Assembled zip is corrupt (bad entry: {bad}). Re-download/upload the parts.",
                        ),
                        code="merge_crc",
                    )
        except zipfile.BadZipFile as e:
            raise BackupPartsError(
                _bilingual(
                    "zip اسمبل‌شده قابل خواندن نیست. پارت‌ها ناقص یا خراب‌اند.",
                    "Assembled zip cannot be read. Parts are incomplete or corrupt.",
                ),
                code="merge_bad_zip",
            ) from e

        use_large = bool(allow_large)
        if not use_large and merged_path.stat().st_size > allowed_upload_bytes(False):
            if looks_like_panel_backup_zip(merged_path):
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
        use_large = resolve_allow_large_for_zip(merged_path, use_large)

        result = save_upload(merged_path, merged_name, allow_large=use_large)
        if result.get("error"):
            raise BackupPartsError(str(result["error"]), code="zip_extract")

        result = dict(result)
        result["assembled_from_parts"] = True
        result["parts_count"] = len(staged)
        result["parts"] = [
            {
                "filename": p.spec.filename,
                "index": p.spec.index,
                "total": p.spec.total,
                "size": p.size,
            }
            for p in staged
        ]
        result["merged_filename"] = merged_name
        return result
    finally:
        try:
            if merged_path.exists():
                merged_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            tmp_dir.rmdir()
        except OSError:
            shutil.rmtree(tmp_dir, ignore_errors=True)
