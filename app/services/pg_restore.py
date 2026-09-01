"""Smart PasarGuard backup restore (fixes version/password pitfalls)."""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import re
import shutil
import sqlite3
import tempfile
import traceback
import zipfile
from pathlib import Path
from typing import Callable

from app.config import PASARGUARD_DIR, PASARGUARD_ENV, PASARGUARD_DATA, UPLOAD_DIR, WORK_DIR
from app.services.archive_guard import safe_extract as _guarded_zip_extract, safe_upload_name
from app.services.env_migration import (
    detect_db_type_from_env,
    env_points_to_db,
    extract_env_summary,
    finalize_pasarguard_env_after_restore,
    read_env_var,
)
from app.services.migrators.base import MigrationJob
from app.services.pg_access import get_panel_access_info
from app.services.prerequisites import is_pasarguard_installed, get_pasarguard_db_type
from app.services.upload import get_upload_path

PASARGUARD_BACKUP_DIR = PASARGUARD_DIR / "backup"
_restore_jobs: dict[str, MigrationJob] = {}
_restore_tasks: set[asyncio.Task] = set()
MAX_FINISHED_RESTORE_JOBS = 20


def _prune_finished_restore_jobs() -> None:
    finished = [j for j in _restore_jobs.values() if j.status in ("success", "error")]
    for job in finished[: max(0, len(finished) - MAX_FINISHED_RESTORE_JOBS)]:
        _restore_jobs.pop(job.job_id, None)
        job.clear_log_callbacks()

SUPPORTED_RESTORE_DBS = frozenset({
    "sqlite", "mysql", "mariadb", "postgresql", "timescaledb",
})


def get_restore_job(job_id: str) -> MigrationJob | None:
    return _restore_jobs.get(job_id)


def soft_db_family(a: str | None, b: str | None) -> bool:
    """True when engines are interchangeable for *native* restore (no convert).

    - mysql ↔ mariadb: same wire protocol / dump style
    - postgresql → timescaledb: plain PG dumps restore into Timescale fine
    - timescaledb → postgresql: NOT soft — Timescale dumps need convert/strip
    """
    if not a or not b:
        return False
    if a == b:
        return True
    if {a, b} <= {"mysql", "mariadb"}:
        return True
    # Plain PostgreSQL backup can land on Timescale (superset of PG)
    if a == "postgresql" and b == "timescaledb":
        return True
    return False


def should_sync_alembic_before_panel_boot(needs_convert: bool) -> bool:
    """Same-engine / soft-family restores must sync alembic while panel is down.

    Convert path already landed schema at head; starting the panel first and then
    running one-shot alembic caused dual migrate on Timescale/PG (panel dies after
    Context, wizard hangs at ~90%).
    """
    return not bool(needs_convert)


def extract_psql_errors(text: str, limit: int = 12) -> str:
    """Pull ERROR/FATAL lines out of noisy psql dump output for user-facing messages."""
    if not text:
        return ""
    lines = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if re.match(r"^(ERROR|FATAL|PANIC):", s, re.I):
            lines.append(s)
        elif lines and re.match(r"^(DETAIL|HINT|CONTEXT):", s, re.I):
            lines.append(s)
    if not lines:
        return (text or "")[-1200:]
    return "\n".join(lines[:limit])


def filter_timescaledb_extension_sql(sql: str, *, strip_all: bool = False) -> str:
    """Strip TimescaleDB extension / toolkit DDL that plain PostgreSQL cannot run.

    When restoring a Timescale backup into stock PostgreSQL, set strip_all=True to
    also drop hypertable helpers and any other timescaledb-qualified statements.
    """
    return "\n".join(
        ln for ln in sql.splitlines() if not _ts_extension_line_dropped(ln, strip_all)
    )


def _ts_extension_line_dropped(ln: str, strip_all: bool) -> bool:
    if re.search(
        r"^\s*(DROP|CREATE)\s+EXTENSION\s+(IF\s+(EXISTS|NOT\s+EXISTS)\s+)?"
        r"timescaledb(_toolkit)?\b",
        ln,
        re.I,
    ):
        return True
    if re.search(r"^\s*COMMENT\s+ON\s+EXTENSION\s+timescaledb", ln, re.I):
        return True
    if strip_all:
        # Internal Timescale schemas / objects
        if re.search(r"_timescaledb_(catalog|internal|config|cache|functions)\b", ln, re.I):
            return True
        if re.search(
            r"timescaledb_(pre|post)_restore\s*\("
            r"|create_hypertable\s*\("
            r"|add_dimension\s*\("
            r"|set_chunk_time_interval\s*\("
            r"|compress_chunk\s*\("
            r"|decompress_chunk\s*\("
            r"|alter_job\s*\("
            r"|add_retention_policy\s*\("
            r"|remove_retention_policy\s*\("
            r"|add_compression_policy\s*\("
            r"|remove_compression_policy\s*\("
            r"|timescaledb\.",
            ln,
            re.I,
        ):
            return True
        # Storage parameters / WITH options referencing timescaledb
        if re.search(r"timescaledb\.", ln, re.I):
            return True
        if re.search(r"\btimescaledb\b", ln, re.I) and re.search(
            r"^\s*(CREATE|ALTER|DROP|SELECT|COMMENT|GRANT|REVOKE|SET)\b", ln, re.I
        ):
            return True
    return False


def filter_timescaledb_extension_sql_file(
    src: Path, dest: Path, *, strip_all: bool = False,
) -> Path:
    """Stream `src` into `dest`, dropping the same lines as the in-memory filter."""
    with open(src, "r", encoding="utf-8", errors="ignore") as fh, \
            open(dest, "w", encoding="utf-8") as out:
        for raw in fh:
            ln = raw.rstrip("\n").rstrip("\r")
            if _ts_extension_line_dropped(ln, strip_all):
                continue
            out.write(ln)
            out.write("\n")
    return dest


def filter_globals_sql(sql: str) -> str:
    """Rewrite globals.sql so it is idempotent when roles already exist.

    pg_dumpall emits plain ``CREATE ROLE`` statements that fail with
    *"already exists"* when the cluster was initialised by docker-compose before
    the restore.  We wrap each one in a DO-block that silently swallows
    ``duplicate_object`` so the restore proceeds without errors on a live cluster.

    ``CREATE DATABASE`` is deliberately left alone: PostgreSQL refuses to run it
    inside a DO-block, and globals are applied with ON_ERROR_STOP=0 while the
    manifest loop recreates each database explicitly, so a duplicate is harmless.
    """
    out: list[str] = []
    buf: list[str] = []
    in_stmt = False

    def _flush_role_create(stmt_lines: list[str]) -> None:
        raw = "\n".join(stmt_lines).rstrip(";").strip()
        # Wrap in an anonymous block that ignores duplicate_object
        out.append(
            "DO $pg_restore_idempotent$\n"
            "BEGIN\n"
            f"  {raw};\n"
            "EXCEPTION WHEN duplicate_object OR duplicate_database THEN\n"
            "  NULL;\n"
            "END\n"
            "$pg_restore_idempotent$;"
        )

    for ln in sql.splitlines():
        stripped = ln.strip()
        # Detect start of a CREATE ROLE / CREATE DATABASE statement
        if not in_stmt and re.match(r"CREATE\s+ROLE\b", stripped, re.I):
            in_stmt = True
            buf = [ln]
            if stripped.endswith(";"):
                _flush_role_create(buf)
                buf = []
                in_stmt = False
            continue
        if in_stmt:
            buf.append(ln)
            if stripped.endswith(";"):
                _flush_role_create(buf)
                buf = []
                in_stmt = False
            continue
        out.append(ln)

    # Flush any unterminated statement
    if buf:
        _flush_role_create(buf)

    return "\n".join(out)


def _sql_literal(value: str) -> str:
    return "'" + (value or "").replace("'", "''") + "'"


_PG_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]{0,62}$")


def safe_pg_identifier(name: str | None, *, what: str = "identifier") -> str:
    """Validate a PostgreSQL identifier that came from backup metadata."""
    cleaned = (name or "").strip().strip('"')
    if not _PG_IDENT_RE.match(cleaned):
        raise RuntimeError(f"Unsafe {what} in backup manifest: {name!r}")
    return cleaned


# TimescaleDB 2.29.0 replaced chunk.schema_name/table_name with relid.
# Restoring a pre-2.29 dump into 2.29+ fails with:
#   ERROR: column "schema_name" of relation "chunk" does not exist
TS_LAST_SCHEMA_NAME_CHUNK = "2.28.3"
TS_FIRST_RELID_CHUNK = "2.29.0"

# Catalog columns/tables that exist only from a given TimescaleDB version onward, so a
# dump listing one of them cannot be loaded into an older extension. The chunk catalog
# swap is a two-sided boundary (see catalog era above); these are one-sided floors,
# which is what makes a NEWER backup fail on an OLDER server, e.g.
#   ERROR: column "schema_change_timestamp" of relation "continuous_agg" does not exist
#   ERROR: relation "_timescaledb_catalog.chunk_column_stats" does not exist
# Both maps are read off timescale/timescaledb sql/pre_install/tables.sql per release tag.
TS_CATALOG_COLUMN_FLOORS: dict[tuple[str, str], str] = {
    ("chunk", "creation_time"): "2.13.0",
    ("compression_chunk_size", "numrows_frozen_immediately"): "2.13.0",
    ("compression_settings", "compress_relid"): "2.19.0",
    ("compression_settings", "index"): "2.22.0",
    ("continuous_agg", "schema_change_timestamp"): "2.28.0",
    ("dimension_slice", "chunk_id"): "2.28.0",
    ("chunk", "relid"): TS_FIRST_RELID_CHUNK,
}

TS_CATALOG_TABLE_FLOORS: dict[str, str] = {
    "continuous_aggs_watermark": "2.11.0",
    "compression_settings": "2.14.0",
    "chunk_column_stats": "2.16.0",
    "continuous_aggs_materialization_ranges": "2.22.0",
    "chunk_rewrite": "2.24.0",
    "bgw_job": "2.25.0",
    "continuous_aggs_jobs_refresh_ranges": "2.26.3",
    "continuous_aggs_tenant_tracking": TS_FIRST_RELID_CHUNK,
    "hypertable_cagg_settings": TS_FIRST_RELID_CHUNK,
}

# Tables owned by the timescaledb extension. A missing column on one of them is version
# skew; the same error on an application table is a real schema problem.
TS_CATALOG_RELATIONS = frozenset({
    "chunk",
    "chunk_column_stats",
    "chunk_constraint",
    "chunk_index",
    "compression_chunk_size",
    "compression_settings",
    "continuous_agg",
    "continuous_aggs_bucket_function",
    "continuous_aggs_hypertable_invalidation_log",
    "continuous_aggs_invalidation_threshold",
    "continuous_aggs_materialization_invalidation_log",
    "continuous_aggs_materialization_ranges",
    "continuous_aggs_watermark",
    "dimension",
    "dimension_slice",
    "hypertable",
    "hypertable_compression",
})

_TS_COPY_HEADER_RE = re.compile(
    r"COPY\s+(_timescaledb_catalog\.)?\"?(\w+)\"?\s*\(([^)]*)\)\s+FROM\s+stdin",
    re.I,
)
_TS_MISSING_COLUMN_RE = re.compile(
    r"column\s+\"?(\w+)\"?\s+of\s+relation\s+\"?(\w+)\"?\s+does\s+not\s+exist",
    re.I,
)
_TS_MISSING_RELATION_RE = re.compile(
    r"relation\s+\"?_timescaledb_catalog\.(\w+)\"?\s+does\s+not\s+exist",
    re.I,
)


def parse_timescale_wanted(versions: list[str] | None) -> str | None:
    """Pick a concrete TimescaleDB version like 2.28.1 from backup metadata."""
    if not versions:
        return None
    # Prefer dotted semver (ignore empty / "latest")
    scored = []
    for v in versions:
        v = (v or "").strip()
        if re.match(r"^\d+\.\d+(\.\d+)?$", v):
            scored.append(v)
    if scored:
        return max(scored, key=_ts_sort_key)
    return versions[0].strip() or None


def _ts_sort_key(ver: str) -> tuple:
    return (_ts_version_tuple(ver) or (-1, -1, -1), ver)


def sort_ts_versions(versions) -> list[str]:
    """Deduplicate and order Timescale versions numerically, not lexicographically."""
    return sorted({(v or "").strip() for v in versions if (v or "").strip()}, key=_ts_sort_key)


def _ts_version_tuple(ver: str | None) -> tuple[int, ...] | None:
    """Parse dotted Timescale version into a comparable tuple."""
    if not ver:
        return None
    m = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", (ver or "").strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def ts_version_ge(a: str | None, b: str | None) -> bool:
    ta, tb = _ts_version_tuple(a), _ts_version_tuple(b)
    if ta is None or tb is None:
        return False
    return ta >= tb


def ts_version_lt(a: str | None, b: str | None) -> bool:
    ta, tb = _ts_version_tuple(a), _ts_version_tuple(b)
    if ta is None or tb is None:
        return False
    return ta < tb


def _max_ts_version(a: str | None, b: str | None) -> str | None:
    if not a:
        return b
    if not b:
        return a
    return b if ts_version_lt(a, b) else a


def detect_ts_mismatch_from_text(text: str) -> tuple[str, str] | None:
    """Parse official restore error: backup version X vs server Y."""
    if not text:
        return None
    m = re.search(
        r"backup version[:\s]+([0-9.]+).*?(?:server|target).*?version[:\s]+([0-9.]+)",
        text,
        re.I | re.S,
    )
    if m:
        return m.group(1), m.group(2)
    m2 = re.search(
        r"TimescaleDB version mismatch.*?([0-9.]+).*?([0-9.]+)",
        text,
        re.I | re.S,
    )
    if m2:
        return m2.group(1), m2.group(2)
    return None


def is_ts_catalog_mismatch_error(text: str) -> bool:
    """True when psql failed because Timescale catalog columns don't match the dump era.

    Classic case after TimescaleDB 2.29.0: dump COPY lists schema_name/table_name on
    _timescaledb_catalog.chunk, but the live extension only has relid.
    """
    if not text:
        return False
    low = text.lower()
    catalog_cols = (
        "schema_name",
        "table_name",
        "relid",
        "compressed_chunk_id",
        "compression_state",
        "compressed_hypertable_id",
    )
    if "of relation \"chunk\"" in low or "of relation 'chunk'" in low:
        if any(c in low for c in catalog_cols) and (
            "does not exist" in low or "undefined_column" in low
        ):
            return True
    if "of relation \"hypertable\"" in low or "of relation 'hypertable'" in low:
        if any(c in low for c in ("compression_state", "compressed_hypertable_id", "status")) and (
            "does not exist" in low or "undefined_column" in low
        ):
            return True
    # COPY column list / INSERT target mismatches against Timescale catalogs
    if "_timescaledb_catalog" in low and "does not exist" in low and any(
        c in low for c in catalog_cols
    ):
        return True
    # Any timescaledb-owned catalog table, which also covers the reverse skew of a
    # newer backup on an older server (continuous_agg.schema_change_timestamp, 2.28.0).
    if any(
        m.group(2).lower() in TS_CATALOG_RELATIONS
        for m in _TS_MISSING_COLUMN_RE.finditer(text)
    ):
        return True
    # A catalog table the dump has and this extension does not (chunk_column_stats…)
    if _TS_MISSING_RELATION_RE.search(text):
        return True
    return False


def detect_dump_chunk_catalog_era(sql_text: str) -> str | None:
    """Return 'schema_name' (pre-2.29) or 'relid' (2.29+) from dump COPY headers."""
    if not sql_text:
        return None
    # COPY [_timescaledb_catalog.]chunk (col, ...) FROM stdin;
    for m in re.finditer(
        r"COPY\s+(?:_timescaledb_catalog\.)?chunk\s*\(([^)]+)\)\s+FROM\s+stdin",
        sql_text,
        re.I,
    ):
        cols = {c.strip().strip('"').lower() for c in m.group(1).split(",")}
        if "schema_name" in cols or "table_name" in cols:
            return "schema_name"
        if "relid" in cols:
            return "relid"
    # Fallback: bare column mentions next to catalog.chunk in dump
    if re.search(
        r"_timescaledb_catalog\.chunk[^\n]{0,200}\bschema_name\b",
        sql_text,
        re.I,
    ):
        return "schema_name"
    if re.search(
        r"_timescaledb_catalog\.chunk[^\n]{0,200}\brelid\b",
        sql_text,
        re.I,
    ):
        return "relid"
    return None


def _ts_floor_for_copy_header(table: str, cols_blob: str, *, qualified: bool) -> str | None:
    """Version floor implied by one `COPY <catalog table> (cols…)` header.

    Table-level floors need the _timescaledb_catalog schema in the dump so generic
    names (bgw_job, dimension) can never be read off an application table.
    """
    table = (table or "").strip().strip('"').lower()
    cols = {c.strip().strip('"').lower() for c in (cols_blob or "").split(",")}
    floor: str | None = None
    if qualified:
        floor = TS_CATALOG_TABLE_FLOORS.get(table)
    for (marker_table, marker_col), version in TS_CATALOG_COLUMN_FLOORS.items():
        if marker_table == table and marker_col in cols:
            floor = _max_ts_version(floor, version)
    return floor


def detect_dump_ts_catalog_floor(sql_text: str) -> str | None:
    """Lowest TimescaleDB version whose catalog can accept this dump text."""
    if not sql_text:
        return None
    floor: str | None = None
    for m in _TS_COPY_HEADER_RE.finditer(sql_text):
        floor = _max_ts_version(
            floor,
            _ts_floor_for_copy_header(m.group(2), m.group(3), qualified=bool(m.group(1))),
        )
    return floor


def ts_floor_from_error_text(text: str) -> str | None:
    """Version floor implied by a psql missing column/relation error."""
    if not text:
        return None
    floor: str | None = None
    for m in _TS_MISSING_COLUMN_RE.finditer(text):
        floor = _max_ts_version(
            floor,
            TS_CATALOG_COLUMN_FLOORS.get((m.group(2).lower(), m.group(1).lower())),
        )
    for m in _TS_MISSING_RELATION_RE.finditer(text):
        floor = _max_ts_version(floor, TS_CATALOG_TABLE_FLOORS.get(m.group(1).lower()))
    return floor


def ts_pin_for_floor(min_ver: str, catalog_era: str | None = None) -> str:
    """Image version that satisfies a dump's floor without crossing the 2.29 boundary."""
    if ts_version_lt(min_ver, TS_FIRST_RELID_CHUNK) and catalog_era != "relid":
        return TS_LAST_SCHEMA_NAME_CHUNK
    return min_ver


def _backup_dump_files(root: Path, *, max_files: int = 8) -> list[Path]:
    """Dump SQL files in a backup, both official and third-party layouts."""
    candidates: list[Path] = []
    seen: set[Path] = set()

    def _add(path: Path | None) -> None:
        if path is None:
            return
        path = Path(path)
        if not path.is_file():
            return
        if path.name.lower().endswith(".gz"):
            path = _ensure_plain_sql(path)
        key = path.resolve() if path.exists() else path
        if key in seen:
            return
        seen.add(key)
        candidates.append(path)

    _add(root / "db_backup.sql")
    pg = root / "pg_dump"
    if pg.is_dir():
        for p in sorted(pg.glob("*.sql")):
            _add(p)
    art = discover_backup_artifacts(root)
    _add(art.get("dump_path"))
    if art.get("layout") == "multi" and art.get("dump_path"):
        pg_dir = Path(art["dump_path"]).parent
        if pg_dir.is_dir():
            for p in sorted(pg_dir.glob("*.sql")):
                _add(p)
    return candidates[:max_files]


def _scan_file_ts_catalog_floor(path: Path) -> str | None:
    """Stream a dump line by line, reading only its COPY headers.

    pg_dump always writes the column list on the same line as COPY, so this stays
    cheap on multi-gigabyte dumps and never buffers a whole file.
    """
    floor: str | None = None
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if line[:5].upper() != "COPY " and line.lstrip()[:5].upper() != "COPY ":
                    continue
                floor = _max_ts_version(floor, detect_dump_ts_catalog_floor(line))
    except OSError:
        return floor
    return floor


def detect_backup_ts_catalog_floor(root: Path) -> str | None:
    """Lowest TimescaleDB version that can accept the dumps inside a backup."""
    floor: str | None = None
    for path in _backup_dump_files(root):
        floor = _max_ts_version(floor, _scan_file_ts_catalog_floor(path))
    return floor


def _iter_backup_sql_texts(root: Path, *, max_files: int = 8, max_bytes: int = 1_200_000) -> list[str]:
    """Read a bounded set of dump SQL texts from a backup root.

    For large single dumps, also sample the file tail — Timescale catalog COPY
    statements often appear late in pg_dump output.
    """
    texts: list[str] = []
    candidates = _backup_dump_files(root, max_files=max_files)
    for path in candidates[:max_files]:
        try:
            size = path.stat().st_size
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                head = fh.read(max_bytes)
                texts.append(head)
                if size > max_bytes:
                    # Tail sample for late catalog COPY blocks
                    fh.seek(max(0, size - max_bytes))
                    tail = fh.read(max_bytes)
                    if tail and tail != head:
                        texts.append(tail)
        except OSError:
            continue
    return texts


def detect_backup_chunk_catalog_era(root: Path) -> str | None:
    """Scan backup dumps for Timescale chunk catalog era."""
    candidates = _backup_dump_files(root, max_files=8)
    for path in candidates[:8]:
        era = _scan_file_chunk_catalog_era(path)
        if era:
            return era
        # Fallback samples if streaming missed oddly wrapped SQL
        try:
            size = path.stat().st_size
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                era = detect_dump_chunk_catalog_era(fh.read(1_200_000))
                if era:
                    return era
                if size > 1_200_000:
                    fh.seek(max(0, size - 1_200_000))
                    era = detect_dump_chunk_catalog_era(fh.read(1_200_000))
                    if era:
                        return era
        except OSError:
            continue
    return None


def _scan_file_chunk_catalog_era(path: Path) -> str | None:
    """Stream-scan a dump for COPY _timescaledb_catalog.chunk (...) headers."""
    copy_start = re.compile(
        r"COPY\s+(?:_timescaledb_catalog\.)?chunk\s*\(",
        re.I,
    )
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            buf = ""
            while True:
                piece = fh.read(256_000)
                if not piece:
                    break
                buf = (buf[-500:] + piece) if buf else piece
                m = copy_start.search(buf)
                if not m:
                    continue
                rest = buf[m.end() - 1:]  # from '('
                while ")" not in rest:
                    more = fh.read(64_000)
                    if not more:
                        break
                    rest += more
                end = rest.find(")")
                if end < 0:
                    continue
                cols_blob = rest[1:end]
                cols = {c.strip().strip('"').lower() for c in cols_blob.split(",")}
                if "schema_name" in cols or "table_name" in cols:
                    return "schema_name"
                if "relid" in cols:
                    return "relid"
    except OSError:
        return None
    return None


def _parse_compose_ts_versions(root: Path) -> list[str]:
    """Read timescale/timescaledb:X.Y.Z image pins from backup compose files."""
    versions: list[str] = []
    for name in (
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
    ):
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in re.finditer(
            r"timescale/timescaledb:(\d+\.\d+(?:\.\d+)?)",
            text,
            re.I,
        ):
            versions.append(m.group(1))
    return versions


def _parse_sql_ts_versions(sql_text: str) -> list[str]:
    """Best-effort Timescale version strings embedded in dump SQL / comments."""
    if not sql_text:
        return []
    found: list[str] = []
    patterns = (
        r"timescaledb[_ -]?version[:\s\"]+(\d+\.\d+(?:\.\d+)?)",
        r"backup version[:\s]+(\d+\.\d+(?:\.\d+)?)",
        r"timescale/timescaledb:(\d+\.\d+(?:\.\d+)?)",
        r"extension[:\s]+timescaledb[^\n]{0,80}?(\d+\.\d+\.\d+)",
    )
    for pat in patterns:
        for m in re.finditer(pat, sql_text, re.I):
            found.append(m.group(1))
    return found


def infer_ts_version_from_catalog_era(era: str | None) -> str | None:
    """Map dump catalog fingerprint to a concrete Docker-taggable Timescale version."""
    if era == "schema_name":
        return TS_LAST_SCHEMA_NAME_CHUNK
    if era == "relid":
        return TS_FIRST_RELID_CHUNK
    return None


def resolve_wanted_ts_for_live(
    wanted: str | None,
    *,
    live_ver: str | None,
    catalog_era: str | None,
    min_ver: str | None = None,
) -> str | None:
    """Choose Timescale image version that can accept this dump on the live server.

    Prefer an explicit backup version. When missing, use catalog-era inference so
    pre-2.29 dumps (schema_name on chunk) are not restored into latest-pg17 (2.29+).

    ``min_ver`` is the dump's catalog floor (see detect_backup_ts_catalog_floor). It
    only changes the outcome when the era/version logic alone would leave the live
    extension too old to read the dump.
    """
    base = _resolve_wanted_ts_by_era(wanted, live_ver=live_ver, catalog_era=catalog_era)
    if not min_ver:
        return base
    if base:
        return base if not ts_version_lt(base, min_ver) else ts_pin_for_floor(min_ver, catalog_era)
    if not live_ver or ts_version_lt(live_ver, min_ver):
        return ts_pin_for_floor(min_ver, catalog_era)
    return None


def _resolve_wanted_ts_by_era(
    wanted: str | None,
    *,
    live_ver: str | None,
    catalog_era: str | None,
) -> str | None:
    if catalog_era == "schema_name":
        # Dump COPY lists schema_name — cannot land on TimescaleDB 2.29+
        if not live_ver or ts_version_ge(live_ver, TS_FIRST_RELID_CHUNK):
            if wanted and ts_version_lt(wanted, TS_FIRST_RELID_CHUNK):
                return wanted
            return TS_LAST_SCHEMA_NAME_CHUNK
        # Live already pre-2.29 and catalog-compatible
        if wanted and wanted != live_ver:
            return wanted
        return None
    if catalog_era == "relid":
        if not live_ver or ts_version_lt(live_ver, TS_FIRST_RELID_CHUNK):
            if wanted and ts_version_ge(wanted, TS_FIRST_RELID_CHUNK):
                return wanted
            return TS_FIRST_RELID_CHUNK
        if wanted and wanted != live_ver:
            return wanted
        return None
    if wanted and live_ver and wanted != live_ver:
        return wanted
    if wanted and not live_ver:
        return wanted
    return None


def wanted_ts_for_restore_retry(out: str, analysis: dict) -> str | None:
    """Pick Timescale version to align to after a failed dump restore attempt."""
    mismatch = detect_ts_mismatch_from_text(out or "")
    if mismatch and mismatch[0]:
        return mismatch[0]
    era = analysis.get("timescaledb_chunk_catalog")
    # The failing column itself names the version the dump needs; the analysis floor
    # covers dumps whose error text is less specific.
    floor = ts_floor_from_error_text(out or "") or analysis.get("timescaledb_min_version")
    wanted = parse_timescale_wanted(analysis.get("timescaledb_versions"))
    if wanted:
        if era == "schema_name" and ts_version_ge(wanted, TS_FIRST_RELID_CHUNK):
            return TS_LAST_SCHEMA_NAME_CHUNK
        if floor and ts_version_lt(wanted, floor):
            return ts_pin_for_floor(floor, era)
        return wanted
    if floor:
        return ts_pin_for_floor(floor, era)
    if is_ts_catalog_mismatch_error(out or ""):
        if not era:
            low = (out or "").lower()
            if "schema_name" in low or "table_name" in low or "compressed_chunk_id" in low:
                era = "schema_name"
            elif "relid" in low:
                era = "relid"
        return infer_ts_version_from_catalog_era(era) or TS_LAST_SCHEMA_NAME_CHUNK
    return None


def collect_backup_ts_versions(root: Path) -> list[str]:
    """Gather explicit Timescale versions from manifest, compose, and dump SQL.

    Catalog fingerprints (schema_name vs relid) are tracked separately via
    detect_backup_chunk_catalog_era() — they must not be injected as fake
    semver pins that force unnecessary image realigns on already-compatible hosts.
    """
    versions = list(_parse_manifest_ts_versions(root))
    versions.extend(_parse_compose_ts_versions(root))
    for text in _iter_backup_sql_texts(root):
        versions.extend(_parse_sql_ts_versions(text))
    out: list[str] = []
    seen: set[str] = set()
    for v in versions:
        v = (v or "").strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def is_auth_failure_text(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(
        s in low
        for s in (
            "sasl authentication failed",
            "password authentication failed",
            "access denied for user",
            "protocolviolationerror",
            "authentication failed",
        )
    )


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    _guarded_zip_extract(zf, dest)


def _find_env(root: Path) -> Path | None:
    for p in [root / ".env", *root.rglob(".env")]:
        if p.is_file() and p.name == ".env":
            return p
    return None


def _official_dump_here(path: Path) -> bool:
    return (
        (path / "db_backup.sql").is_file()
        or (path / "pg_dump" / "manifest.tsv").is_file()
        or (path / "db.sqlite3").is_file()
    )


def _find_backup_root(extracted: Path) -> Path:
    """Prefer official co-located .env+dump; otherwise the extract top.

    Third-party bots zip /opt/pasarguard + /var/lib/pasarguard, so .env and the
    dump live in different folders. Searching only next to .env misses them.
    """
    env = _find_env(extracted)
    if env and _official_dump_here(env.parent):
        return env.parent
    for cand in [extracted, *extracted.iterdir()]:
        if cand.is_dir() and _official_dump_here(cand):
            return cand
    return extracted


_SKIP_BACKUP_DIR_NAMES = frozenset({
    ".git", "node_modules", "__pycache__", "proc", "sys", "dev",
})
_SKIP_SQL_NAMES = frozenset({
    "globals.sql", "roles.sql",
    "db_backup_filtered.sql", "db_backup_pg_plain.sql",
})
_PANEL_SQL_HINTS = (
    "alembic_version",
    "users_groups_association",
    "inbounds_groups_association",
    "core_configs",
    "create table users",
    "create table `users`",
    'create table "users"',
    "insert into users",
    "insert into `users`",
    "copy public.users",
    "create table hosts",
    "create table nodes",
    "create table admins",
    "create table inbounds",
)


def _backup_rel_parts(path: Path, root: Path) -> tuple[str, ...]:
    try:
        return path.relative_to(root).parts
    except ValueError:
        return path.parts


def _is_sqlite_file(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(16).startswith(b"SQLite format 3")
    except OSError:
        return False


def _open_sql_text(path: Path):
    """Text-mode reader for .sql or .sql.gz dumps."""
    name = path.name.lower()
    if name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="ignore")
    return open(path, "r", encoding="utf-8", errors="ignore")


def _read_sql_head(path: Path, max_bytes: int = 96_000) -> str:
    try:
        with _open_sql_text(path) as fh:
            return fh.read(max_bytes)
    except OSError:
        return ""


def _ensure_plain_sql(path: Path) -> Path:
    """Decompress .sql.gz next to the archive so mysql/psql can read it."""
    name = path.name.lower()
    if not name.endswith(".gz"):
        return path
    dest_name = path.name[: -3] if name.endswith(".gz") else path.name
    if not dest_name.lower().endswith(".sql"):
        dest_name = dest_name + ".sql"
    dest = path.parent / dest_name
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    with gzip.open(path, "rb") as src, open(dest, "wb") as out:
        shutil.copyfileobj(src, out)
    return dest


def _sniff_sql_dump(path: Path) -> tuple[int, str | None]:
    """Score a file as a DB dump and guess engine. (0, None) = not a dump."""
    name = path.name.lower()
    if name in _SKIP_SQL_NAMES:
        return 0, None
    if not (name.endswith(".sql") or name.endswith(".sql.gz") or name.endswith(".sql.gzip")):
        return 0, None
    head = _read_sql_head(path)
    if not head or not head.strip():
        return 0, None
    low = head.lower()
    score = 0
    engine: str | None = None
    if "timescaledb" in low:
        engine = "timescaledb"
        score += 8
    elif "-- postgresql database dump" in low or "copy public." in low or "set statement_timeout" in low:
        engine = "postgresql"
        score += 6
    elif "-- mariadb dump" in low or "engine=aria" in low:
        engine = "mariadb"
        score += 6
    elif "-- mysql dump" in low or "engine=innodb" in low or "lock tables" in low or "/*!40101" in low:
        engine = "mysql"
        score += 6
    elif "create table" in low or "insert into" in low or re.search(r"^\s*copy\s+", low, re.M):
        score += 2
        if "`" in head:
            engine = "mysql"
        elif "copy " in low:
            engine = "postgresql"
    if score <= 0:
        return 0, None
    for hint in _PANEL_SQL_HINTS:
        if hint in low:
            score += 3
    if name == "db_backup.sql":
        score += 10
    elif any(tok in name for tok in ("dump", "backup", "pasarguard", "marzban", "database")):
        score += 4
    return score, engine


def _sqlite_panel_score(path: Path) -> int:
    if not _is_sqlite_file(path):
        return 0
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = {
                str(r[0])
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            conn.close()
    except Exception:
        return 1
    score = 1
    for table in ("users", "hosts", "nodes", "alembic_version", "admins", "inbounds", "groups"):
        if table in tables:
            score += 3
    # 3x-ui dumps belong on the migrator, not PasarGuard restore
    if "client_traffics" in tables and "users" not in tables:
        score -= 6
    return score


def _has_raw_mysql_datadir(root: Path) -> bool:
    for p in root.rglob("ibdata1"):
        if p.is_file() and not _path_skipped_dir(p, root):
            return True
    return False


def _path_skipped_dir(path: Path, root: Path) -> bool:
    return any(part in _SKIP_BACKUP_DIR_NAMES for part in _backup_rel_parts(path, root)[:-1])


def _pick_pg_dump_app_file(pg_dir: Path) -> Path | None:
    manifest = pg_dir / "manifest.tsv"
    if manifest.is_file():
        for line in manifest.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            name = safe_upload_name(parts[3])
            if name.lower() in _SKIP_SQL_NAMES:
                continue
            cand = pg_dir / name
            if cand.is_file():
                return cand
    sqls = sorted(
        p for p in pg_dir.glob("*.sql")
        if p.is_file() and p.name.lower() not in _SKIP_SQL_NAMES
    )
    return sqls[0] if sqls else None


def discover_backup_artifacts(root: Path, *, env_db: str | None = None) -> dict:
    """Locate a dump/sqlite in official PasarGuard zips *or* third-party layouts."""
    result: dict = {
        "layout": "none",
        "dump_path": None,
        "sqlite_path": None,
        "dump_engine": None,
        "has_raw_mysql_datadir": False,
    }
    env_db = (env_db or "").strip().lower() or None
    server_env = env_db in ("mysql", "mariadb", "postgresql", "timescaledb")

    def _set_multi(pg_dir: Path) -> dict:
        result["layout"] = "multi"
        result["dump_path"] = _pick_pg_dump_app_file(pg_dir)
        result["dump_engine"] = env_db if env_db in ("postgresql", "timescaledb") else "postgresql"
        if result["dump_path"]:
            sniffed = _sniff_sql_dump(result["dump_path"])[1]
            if sniffed:
                result["dump_engine"] = sniffed
        return result

    def _set_single(path: Path, engine: str | None) -> dict:
        result["layout"] = "single"
        result["dump_path"] = path
        result["dump_engine"] = engine or env_db
        return result

    def _set_sqlite(path: Path) -> dict:
        result["layout"] = "sqlite_file"
        result["sqlite_path"] = path
        result["dump_engine"] = "sqlite"
        return result

    # --- official names at this root ---
    if (root / "pg_dump" / "manifest.tsv").is_file():
        return _set_multi(root / "pg_dump")
    official_sql = root / "db_backup.sql"
    if official_sql.is_file():
        return _set_single(official_sql, _sniff_sql_dump(official_sql)[1])
    official_sqlite = root / "db.sqlite3"
    if official_sqlite.is_file() and _is_sqlite_file(official_sqlite) and not server_env:
        return _set_sqlite(official_sqlite)

    # --- nested official names (wrapper folder / var/lib tree) ---
    for man in root.rglob("manifest.tsv"):
        if _path_skipped_dir(man, root):
            continue
        if man.parent.name == "pg_dump" and man.is_file():
            return _set_multi(man.parent)
    for sql in root.rglob("db_backup.sql"):
        if sql.is_file() and not _path_skipped_dir(sql, root):
            return _set_single(sql, _sniff_sql_dump(sql)[1])

    sql_candidates: list[tuple[int, Path, str | None]] = []
    sqlite_candidates: list[tuple[int, Path]] = []
    for path in root.rglob("*"):
        if not path.is_file() or _path_skipped_dir(path, root):
            continue
        name = path.name.lower()
        if name.endswith(".sql") or name.endswith(".sql.gz") or name.endswith(".sql.gzip"):
            score, engine = _sniff_sql_dump(path)
            if score > 0:
                sql_candidates.append((score, path, engine))
            continue
        if name.endswith((".sqlite3", ".sqlite", ".db")) or name == "db.sqlite3":
            score = _sqlite_panel_score(path)
            if score > 0:
                sqlite_candidates.append((score, path))

    if sql_candidates:
        sql_candidates.sort(key=lambda x: (-x[0], len(str(x[1]))))
        _score, path, engine = sql_candidates[0]
        return _set_single(path, engine)

    # Leftover sqlite next to a MySQL/PG .env is not the dump — bots zip /var/lib
    # without running mysqldump. Only accept sqlite when the backup is (or looks) sqlite.
    sqlite_candidates.sort(key=lambda x: (-x[0], len(str(x[1]))))
    if sqlite_candidates and not server_env:
        best_score, best_path = sqlite_candidates[0]
        if best_score >= 1:
            return _set_sqlite(best_path)
    elif sqlite_candidates and server_env:
        # Keep the path for a clearer error; do not treat it as the dump.
        result["sqlite_path"] = sqlite_candidates[0][1]

    result["has_raw_mysql_datadir"] = _has_raw_mysql_datadir(root)
    return result


def resolve_backup_sql_dump(root: Path, *, env_db: str | None = None) -> Path | None:
    art = discover_backup_artifacts(root, env_db=env_db)
    path = art.get("dump_path")
    if not path:
        return None
    return _ensure_plain_sql(Path(path))


def resolve_backup_sqlite(root: Path, *, env_db: str | None = None) -> Path | None:
    art = discover_backup_artifacts(root, env_db=env_db)
    path = art.get("sqlite_path")
    return Path(path) if path else None


def _parse_manifest_ts_versions(root: Path) -> list[str]:
    manifest = root / "pg_dump" / "manifest.tsv"
    if not manifest.exists():
        return []
    versions = []
    for line in manifest.read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = line.split("\t")
        if len(parts) >= 5 and parts[4].strip():
            versions.append(parts[4].strip())
    return versions


def analyze_pasarguard_backup(upload_id: str | None = None, path: str | Path | None = None) -> dict:
    """Inspect uploaded PasarGuard backup zip."""
    if path:
        zip_path = Path(path)
    elif upload_id:
        p = get_upload_path(upload_id)
        if not p:
            raise FileNotFoundError("Upload not found")
        zip_path = Path(p)
        if zip_path.is_dir():
            # find first zip inside
            zips = list(zip_path.rglob("*.zip"))
            if not zips:
                raise FileNotFoundError("No zip in upload")
            zip_path = zips[0]
    else:
        raise ValueError("upload_id or path required")

    if not zip_path.exists():
        raise FileNotFoundError(str(zip_path))

    tmp = Path(tempfile.mkdtemp(prefix="pg-backup-analyze-", dir=str(WORK_DIR)))
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            _safe_extract(zf, tmp)
        # Search the whole extract. Third-party bots nest .env under opt/pasarguard
        # and the dump at the zip root (or the reverse).
        env_path = _find_env(tmp)
        env_text = env_path.read_text(encoding="utf-8", errors="ignore") if env_path else ""
        # Backup .env must NOT use live compose (that would mislabel every PG backup as Timescale)
        db_type = detect_db_type_from_env(env_text, prefer_compose=False) if env_text else None
        summary = extract_env_summary(env_text) if env_text else None

        # Prefer PGClockBackup manifest when present (db_type + live panel counts)
        manifest_meta: dict = {}
        for cand in (tmp / "pgclockmg-manifest.json", tmp / "manifest.json"):
            if cand.is_file():
                try:
                    manifest_meta = json.loads(cand.read_text(encoding="utf-8"))
                except Exception:
                    manifest_meta = {}
                break
        if isinstance(manifest_meta.get("db_type"), str) and manifest_meta["db_type"].strip():
            man_db = manifest_meta["db_type"].strip().lower()
            if man_db in ("sqlite", "postgresql", "timescaledb", "mysql", "mariadb"):
                if not db_type or (db_type == "postgresql" and man_db == "timescaledb"):
                    db_type = man_db
                elif db_type != man_db and not soft_db_family(db_type, man_db):
                    # Manifest is authoritative for PGClockBackup bundles
                    if manifest_meta.get("format") == "pgclockmg-full-bundle":
                        db_type = man_db

        artifacts = discover_backup_artifacts(tmp, env_db=db_type)
        layout = artifacts["layout"]
        if not db_type:
            db_type = artifacts.get("dump_engine")
        elif db_type == "postgresql" and artifacts.get("dump_engine") == "timescaledb":
            db_type = "timescaledb"

        ts_versions = sort_ts_versions(collect_backup_ts_versions(tmp))
        chunk_catalog_era = detect_backup_chunk_catalog_era(tmp)
        ts_min_version = detect_backup_ts_catalog_floor(tmp)
        # Official Timescale backups keep postgresql+asyncpg URL — use manifest / dump hints
        if db_type in (None, "postgresql"):
            if ts_versions or chunk_catalog_era or ts_min_version:
                db_type = "timescaledb"
            elif _backup_sql_mentions_timescale(tmp):
                db_type = "timescaledb"

        table_counts = _estimate_backup_table_counts(tmp, layout)
        man_counts = manifest_meta.get("counts") if isinstance(manifest_meta, dict) else None
        if isinstance(man_counts, dict):
            for k, v in man_counts.items():
                if isinstance(v, int) and v > 0 and (k not in table_counts or table_counts.get(k, 0) < v):
                    table_counts[k] = v
        installed = is_pasarguard_installed()
        installed_db = get_pasarguard_db_type() if installed else None

        warnings: list[dict] = []
        ok = True
        if not env_path:
            if layout != "none" and db_type:
                warnings.append({
                    "en": "Backup has no .env — live panel settings are kept; only the database is restored.",
                    "fa": "بکاپ فاقد .env است — تنظیمات پنل نصب‌شده می‌ماند؛ فقط دیتابیس ریستور می‌شود.",
                    "ru": "В бэкапе нет .env — настройки панели сохранятся; восстановится только БД.",
                })
            else:
                ok = False
                warnings.append({
                    "en": "Backup is missing .env — cannot detect database type",
                    "fa": "بکاپ فاقد .env است — نوع دیتابیس مشخص نیست",
                    "ru": "В бэкапе нет .env — тип БД неизвестен",
                })
        if not installed:
            ok = False
            warnings.append({
                "en": "PasarGuard is not installed on this server",
                "fa": "PasarGuard روی این سرور نصب نیست",
                "ru": "PasarGuard не установлен",
            })
        experimental_db_change = False
        convert_blocked = False
        if db_type and installed_db and db_type != installed_db:
            from app.panels import can_convert_databases

            if soft_db_family(db_type, installed_db):
                warnings.append({
                    "en": f"Related engines (backup={db_type}, installed={installed_db}) — restore continues automatically.",
                    "fa": f"موتورهای هم‌خانواده (بکاپ={db_type}، نصب={installed_db}) — ریستور خودکار ادامه می‌یابد.",
                    "ru": f"Смежные СУБД (backup={db_type}, installed={installed_db}) — восстановление продолжится.",
                })
            elif not can_convert_databases(db_type, installed_db):
                convert_blocked = True
                ok = False
                if installed_db == "sqlite" and db_type != "sqlite":
                    warnings.append({
                        "en": f"Cannot convert {db_type} → SQLite. Install PasarGuard with MySQL/MariaDB/PostgreSQL/TimescaleDB yourself, then restore.",
                        "fa": f"نمی‌شود {db_type} را به SQLite تبدیل کرد. خودتان PasarGuard را با دیتابیس سروری نصب کنید، بعد ریستور کنید.",
                        "ru": f"Нельзя конвертировать {db_type} → SQLite. Установите PasarGuard с серверной БД сами, затем восстановите.",
                    })
                else:
                    warnings.append({
                        "en": f"Conversion {db_type} → {installed_db} is not supported.",
                        "fa": f"تبدیل {db_type} به {installed_db} پشتیبانی نمی‌شود.",
                        "ru": f"Конвертация {db_type} → {installed_db} не поддерживается.",
                    })
            else:
                experimental_db_change = True
                warnings.append({
                    "en": f"Database differs (backup={db_type}, installed={installed_db}). Auto-convert will run on restore.",
                    "fa": f"نوع دیتابیس فرق دارد (بکاپ={db_type}، نصب={installed_db}). موقع ریستور خودش تبدیل می‌شود.",
                    "ru": f"Тип БД отличается (backup={db_type}, installed={installed_db}). При восстановлении будет автоконвертация.",
                })

        if layout == "none":
            ok = False
            if artifacts.get("has_raw_mysql_datadir"):
                warnings.append({
                    "en": "Zip has a raw MySQL data directory, not an SQL dump. The backup script must run mysqldump.",
                    "fa": "این زیپ پوشه خام MySQL دارد، نه دامپ SQL. اسکریپت بکاپ باید mysqldump بگیرد.",
                    "ru": "В zip сырой каталог MySQL, а не SQL-дамп. Скрипт должен делать mysqldump.",
                })
            else:
                warnings.append({
                    "en": "No database dump in the zip. A .sql dump or sqlite file is required (any filename).",
                    "fa": "دامپ دیتابیس داخل بکاپ پیدا نشد. یک فایل .sql یا sqlite لازم است (اسم فایل مهم نیست).",
                    "ru": "В архиве нет дампа БД. Нужен файл .sql или sqlite (имя файла не важно).",
                })

        if ts_versions:
            warnings.append({
                "en": f"Backup TimescaleDB: {', '.join(ts_versions)}. Wizard auto-aligns the image before restore.",
                "fa": f"نسخه TimescaleDB بکاپ: {', '.join(ts_versions)}. قبل از ریستور ایمیج سرور هم‌تراز می‌شود.",
                "ru": f"TimescaleDB в бэкапе: {', '.join(ts_versions)}. Образ будет выровнен автоматически.",
            })
        elif chunk_catalog_era == "schema_name":
            warnings.append({
                "en": (
                    f"Backup uses pre-2.29 Timescale chunk catalog (schema_name). "
                    f"Wizard will pin image to {TS_LAST_SCHEMA_NAME_CHUNK} before restore."
                ),
                "fa": (
                    f"بکاپ کاتالوگ قدیمی Timescale (schema_name قبل از 2.29) دارد. "
                    f"قبل از ریستور ایمیج روی {TS_LAST_SCHEMA_NAME_CHUNK} پین می‌شود."
                ),
                "ru": (
                    f"Бэкап со старым каталогом Timescale (schema_name до 2.29). "
                    f"Образ будет закреплён на {TS_LAST_SCHEMA_NAME_CHUNK}."
                ),
            })
        elif ts_min_version:
            pin = ts_pin_for_floor(ts_min_version, chunk_catalog_era)
            warnings.append({
                "en": (
                    f"Backup catalog needs TimescaleDB {ts_min_version} or newer. "
                    f"Wizard pins the image to {pin} before restore if the server is older."
                ),
                "fa": (
                    f"کاتالوگ بکاپ به TimescaleDB {ts_min_version} یا بالاتر نیاز دارد. "
                    f"اگر سرور قدیمی‌تر باشد، قبل از ریستور ایمیج روی {pin} پین می‌شود."
                ),
                "ru": (
                    f"Каталог бэкапа требует TimescaleDB {ts_min_version} или новее. "
                    f"Если сервер старее, образ будет закреплён на {pin}."
                ),
            })

        # table_counts kept for server-side verify only — not shown in the wizard UI

        return {
            "ok": ok,
            "filename": zip_path.name,
            "size": zip_path.stat().st_size,
            "backup_db": db_type,
            "installed_db": installed_db,
            "db_match": (db_type == installed_db) if (db_type and installed_db) else None,
            "soft_match": soft_db_family(db_type, installed_db) if (db_type and installed_db) else None,
            "experimental_db_change": experimental_db_change,
            "convert_blocked": convert_blocked,
            "supported_target_dbs": sorted(SUPPORTED_RESTORE_DBS),
            "layout": layout,
            "dump_name": (
                Path(artifacts["dump_path"]).name if artifacts.get("dump_path")
                else (Path(artifacts["sqlite_path"]).name if artifacts.get("sqlite_path") else None)
            ),
            "timescaledb_versions": ts_versions,
            "timescaledb_chunk_catalog": chunk_catalog_era,
            "timescaledb_min_version": ts_min_version,
            "table_counts": table_counts,
            "env_summary": {k: v for k, v in (summary or {}).items() if k != "db_password"},
            "has_env": bool(env_path),
            "warnings": warnings,
            "zip_path": str(zip_path),
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def start_pasarguard_restore(params: dict) -> MigrationJob:
    if not is_pasarguard_installed():
        raise ValueError("PasarGuard is not installed")
    upload_id = params.get("upload_id")
    if not upload_id:
        raise ValueError("upload_id required")
    analysis = analyze_pasarguard_backup(upload_id=upload_id)
    if not analysis.get("ok") and not params.get("force"):
        msgs = [w.get("en") for w in analysis.get("warnings") or [] if w.get("en")]
        raise ValueError("; ".join(msgs) or "Backup validation failed")

    # Destination is always the DB already installed on this PasarGuard panel
    target_db = (analysis.get("installed_db") or params.get("target_db") or analysis.get("backup_db") or "").strip()
    backup_db = analysis.get("backup_db")
    if target_db and target_db not in SUPPORTED_RESTORE_DBS:
        raise ValueError(f"Unsupported target database: {target_db}")
    params = {
        **params,
        "target_db": target_db or backup_db,
        # Auto-convert when backup engine ≠ installed engine (no UI confirmation)
        "accept_experimental": True,
    }

    _prune_finished_restore_jobs()
    job = MigrationJob()
    _restore_jobs[job.job_id] = job
    task = asyncio.create_task(_run_restore(job, params, analysis))
    _restore_tasks.add(task)
    task.add_done_callback(_restore_tasks.discard)
    return job


async def _run_restore(job: MigrationJob, params: dict, analysis: dict) -> None:
    job.status = "running"
    try:
        result = await _restore_backup(job, params, analysis)
        job.result = result
        job.status = "success"
        job.set_progress(100, "Restore completed")
    except Exception as e:
        explain = getattr(e, "explain", None)
        if not isinstance(explain, dict):
            explain = explain_restore_error(
                e,
                analysis.get("backup_db"),
                params.get("target_db") or analysis.get("installed_db"),
            )
        job.status = "error"
        job.message = explain.get("fa") or explain.get("en") or str(e)
        job.log(f"ERROR: {explain.get('detail') or e}")
        job.log(traceback.format_exc())
        job.result = {"error": str(e), "error_explain": explain}


async def _run(
    job: MigrationJob,
    cmd: list[str],
    cwd: str | None = None,
    timeout: int = 600,
    *,
    quiet: bool = False,
) -> tuple[bool, str]:
    if not quiet:
        job.log(f"$ {' '.join(cmd)}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out = (out_b or b"").decode("utf-8", errors="replace")
        if not quiet:
            for line in out.splitlines()[-40:]:
                if line.strip():
                    job.log(line)
        return proc.returncode == 0, out
    except Exception as e:
        return False, str(e)


class _RestoreMini:
    """Lightweight migrator shim for restore-time PasarGuard ops."""

    def __init__(self, job: MigrationJob, params: dict):
        self.job = job
        self.params = params

    async def _run_cmd(self, cmd, cwd=None, timeout=600, *, quiet: bool = False):
        return await _run(self.job, cmd, cwd=cwd, timeout=timeout, quiet=quiet)


def _read_current_env() -> str:
    return PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore") if PASARGUARD_ENV.exists() else ""


def _set_env_var(text: str, key: str, value: str | None) -> str:
    """Set KEY=value and remove any duplicate prior assignments of KEY."""
    from app.services.env_migration import _set_env_var_simple

    return _set_env_var_simple(text, key, value)


async def _compose(job: MigrationJob, *args: str, timeout: int = 300) -> tuple[bool, str]:
    return await _run(job, _compose_argv(*args), cwd=str(PASARGUARD_DIR), timeout=timeout)


def _compose_argv(*args: str) -> list[str]:
    """docker compose with active compose file prefix (main + multi overlay when present)."""
    from app.services.pasarguard_ops import compose_file_prefix

    return ["docker", "compose", *compose_file_prefix(), *args]


def _compose_has_service(name: str) -> bool:
    from app.services.multiworker_stack import compose_has_service

    return compose_has_service(name)


async def _compose_up_services(job: MigrationJob, *services: str, timeout: int = 300) -> tuple[bool, str]:
    """Start only services that exist in docker-compose.yml (skips missing pgbouncer etc.)."""
    existing = [s for s in services if s and _compose_has_service(s)]
    if not existing:
        return True, ""
    return await _compose(job, "up", "-d", *existing, timeout=timeout)


def _mysql_client_bins(db_type: str, svc: str | None = None) -> list[str]:
    """Client binaries to try (MariaDB images often ship `mariadb`, MySQL ships `mysql`)."""
    name = f"{svc or ''} {db_type or ''}".lower()
    if "maria" in name:
        return ["mariadb", "mysql"]
    if "mysql" in name:
        return ["mysql", "mariadb"]
    return ["mysql", "mariadb"]


async def _detect_db_container(job: MigrationJob, db_type: str) -> str | None:
    ok, out = await _run(
        job,
        _compose_argv("ps", "--services"),
        cwd=str(PASARGUARD_DIR),
        timeout=30,
    )
    services = set((out or "").split())
    candidates = {
        "timescaledb": ["timescaledb", "postgresql", "postgres"],
        "postgresql": ["postgresql", "postgres", "timescaledb"],
        "mysql": ["mysql", "mariadb"],
        "mariadb": ["mariadb", "mysql"],
    }.get(db_type, [])
    for c in candidates:
        if c in services:
            return c
    # fallback: container name from docker ps
    ok2, out2 = await _run(job, ["docker", "ps", "--format", "{{.Names}}"], timeout=20)
    for line in (out2 or "").splitlines():
        name = line.strip()
        for c in candidates:
            if c in name.lower():
                return name
    # Do not invent a service name that is not running — callers must fail clearly
    return None


async def _read_timescaledb_version(job: MigrationJob, container: str, password: str, user: str = "postgres") -> str | None:
    ok, out = await _run(
        job,
        _compose_argv(
            "exec", "-T",
            "-e", f"PGPASSWORD={password}", container,
            "psql", "-U", user, "-d", "postgres", "-At",
            "-c", "SELECT default_version FROM pg_available_extensions WHERE name = 'timescaledb';",
        ),
        cwd=str(PASARGUARD_DIR),
        timeout=30,
    )
    if not ok:
        return None
    ver = (out or "").strip().splitlines()
    return ver[-1].strip() if ver else None


async def _wait_for_postgres_ready(
    job: MigrationJob,
    svc: str,
    password: str,
    user: str = "postgres",
    timeout: int = 120,
) -> bool:
    """Poll until PostgreSQL inside `svc` accepts connections, up to `timeout` seconds."""
    deadline = asyncio.get_event_loop().time() + timeout
    attempt = 0
    while asyncio.get_event_loop().time() < deadline:
        attempt += 1
        proc = await asyncio.create_subprocess_exec(
            *_compose_argv(
                "exec", "-T",
                "-e", f"PGPASSWORD={password}",
                svc, "psql", "-U", user, "-d", "postgres", "-c", "SELECT 1;",
            ),
            cwd=str(PASARGUARD_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out_b, _ = await proc.communicate()
        if proc.returncode == 0:
            job.log(f"PostgreSQL ready after {attempt} probe(s)")
            return True
        out_txt = (out_b or b"").decode("utf-8", errors="replace").strip()
        # Stop retrying on auth failures (wrong password) — no point waiting
        if "password authentication failed" in out_txt.lower() or "role" in out_txt.lower() and "does not exist" in out_txt.lower():
            job.log(f"PostgreSQL auth error during readiness probe: {out_txt[:200]}")
            return False
        wait = min(4, deadline - asyncio.get_event_loop().time())
        if wait > 0:
            await asyncio.sleep(wait)
    job.log(f"PostgreSQL did not become ready within {timeout}s")
    return False


async def _align_timescaledb_image(job: MigrationJob, wanted: str, *, wipe_data: bool = True) -> None:
    """Pin compose timescaledb image to backup version and optionally recreate volume.

    Matches official PasarGuard guidance:
      image: timescale/timescaledb:{backup_version}-pgXX
      rm -rf /var/lib/postgresql/pasarguard

    NEVER call with wipe_data=True after a successful dump restore — that empties the panel.

    Compose mutations are atomic and always reverted on pull/ENOSPC failure so we
    never leave an empty docker-compose.yml (which breaks every later compose exec).
    """
    compose = PASARGUARD_DIR / "docker-compose.yml"
    wanted = parse_timescale_wanted([wanted]) or wanted
    if not compose.exists() or not wanted:
        return
    if not re.match(r"^\d+\.\d+(\.\d+)?$", wanted):
        job.log(f"Ignoring unusable TimescaleDB version from backup: {wanted!r}")
        return
    text = compose.read_text(encoding="utf-8", errors="ignore")
    if not _compose_looks_usable(text):
        raise RuntimeError(
            f"docker-compose.yml is empty or unusable at {compose} — "
            "refusing Timescale image align (restore compose from backup first)"
        )
    m = re.search(r"timescale/timescaledb:([^\s\"']+)", text)
    current_tag = m.group(1) if m else "latest-pg17"
    pg_suf = "pg17"
    m2 = re.search(r"(pg\d+)", current_tag)
    if m2:
        pg_suf = m2.group(1)
    new_tag = f"{wanted}-{pg_suf}"
    if current_tag == new_tag:
        job.log(f"TimescaleDB image already pinned to {new_tag}")
        return

    free = disk_free_bytes("/var/lib")
    if free >= 0 and free < _TS_PULL_MIN_FREE_BYTES:
        raise RuntimeError(
            f"Not enough free disk to pull TimescaleDB {new_tag} "
            f"({free // (1024 * 1024)} MiB free; need ≥{_TS_PULL_MIN_FREE_BYTES // (1024 * 1024)} MiB). "
            "Free disk space or skip image align and clear timescaledb.restoring manually."
        )

    job.log(f"Aligning TimescaleDB image: {current_tag} → {new_tag}")
    new_text = re.sub(
        r"(image:\s*timescale/timescaledb:)[^\s\"']+",
        rf"\g<1>{new_tag}",
        text,
        count=1,
    )
    bak = compose.with_suffix(".yml.pgclockmg.bak")
    try:
        atomic_write_text(bak, text)
        atomic_write_text(compose, new_text)
    except OSError as e:
        restore_text_file(compose, text)
        raise RuntimeError(
            f"Could not update docker-compose.yml for Timescale align ({e}). "
            "Compose left unchanged."
        ) from e

    def _revert_compose(reason: str) -> None:
        if restore_text_file(compose, text):
            job.log(f"Reverted timescaledb image tag to {current_tag} ({reason})")
        else:
            job.log(
                f"CRITICAL: could not revert docker-compose.yml after {reason} — "
                f"restore from {bak} if the file is empty"
            )

    job.set_progress(25, "Pulling TimescaleDB image...")
    # Pull the exact image first so `compose up` never silently uses a stale cached layer
    ok_pull, out_pull = await _compose(job, "pull", "timescaledb", timeout=600)
    pull_blob = out_pull or ""
    if (not ok_pull) or is_enospc_text(pull_blob):
        _revert_compose("image pull failed" + (" / ENOSPC" if is_enospc_text(pull_blob) else ""))
        if wipe_data:
            raise RuntimeError(
                f"TimescaleDB image {new_tag} could not be pulled — "
                f"restore stopped before touching the database:\n{pull_blob[-800:]}"
            )
        raise RuntimeError(
            f"TimescaleDB image {new_tag} could not be pulled "
            f"(compose tag reverted):\n{pull_blob[-800:]}"
        )

    job.set_progress(28, "Recreating TimescaleDB with matching version...")
    from app.services.multiworker_stack import stop_panel_stack

    await stop_panel_stack(job)
    stop_svcs = ["timescaledb"]
    if _compose_has_service("pgbouncer"):
        stop_svcs.append("pgbouncer")
    await _compose(job, "stop", *stop_svcs, timeout=120)
    data_dir = Path("/var/lib/postgresql/pasarguard")
    if wipe_data and data_dir.exists():
        job.log(f"Resetting DB data directory {data_dir} for version alignment")
        shutil.rmtree(data_dir, ignore_errors=True)
        data_dir.mkdir(parents=True, exist_ok=True)
    elif not wipe_data:
        job.log("Timescale image tag updated without wiping data volume")
    ok, out = await _compose_up_services(job, "timescaledb", "pgbouncer", timeout=300)
    if not ok:
        _revert_compose("compose up failed")
        raise RuntimeError(f"Failed to recreate TimescaleDB:\n{out[-2000:]}")

    # Wait for PostgreSQL to be ready instead of a fixed sleep
    cur_env = _read_current_env()
    from app.services.db_auth import build_postgres_auth_attempts

    container_env = await _read_pg_container_init_env(job, "timescaledb")
    pw = (
        read_env_var(cur_env, "POSTGRES_PASSWORD")
        or read_env_var(cur_env, "DB_PASSWORD")
        or container_env.get("POSTGRES_PASSWORD")
        or ""
    )
    pg_user = (
        read_env_var(cur_env, "POSTGRES_USER")
        or read_env_var(cur_env, "DB_USER")
        or container_env.get("POSTGRES_USER")
        or "pasarguard"
    )
    ready = False
    ready_user = pg_user
    for auth_user, auth_pwd in build_postgres_auth_attempts(
        cur_env,
        preferred_user=pg_user,
        preferred_password=pw,
        container_user=container_env.get("POSTGRES_USER"),
        container_password=container_env.get("POSTGRES_PASSWORD"),
        include_trust=True,
    ):
        if auth_pwd is None:
            ok_t, out_t = await _psql_in_db_container(
                job, "timescaledb",
                user=auth_user,
                password=None,
                database="postgres",
                sql="SELECT 1;",
                timeout=15,
            )
            if _psql_exec_succeeded(ok_t, out_t):
                ready = True
                ready_user = auth_user
                break
            continue
        if await _wait_for_postgres_ready(job, "timescaledb", auth_pwd, user=auth_user, timeout=40):
            ready = True
            ready_user = auth_user
            pw = auth_pwd
            break
    if not ready:
        _revert_compose("DB not ready after align")
        raise RuntimeError("TimescaleDB container did not become ready after image alignment")

    # Verify extension version
    live = await _read_timescaledb_version(job, "timescaledb", pw or "x", user=ready_user)
    if not live and ready_user != pg_user:
        live = await _read_timescaledb_version(job, "timescaledb", pw or "x", user=pg_user)
    if live and live != wanted:
        job.log(f"Warning: live TimescaleDB={live} after align (wanted {wanted}) — continuing")
    else:
        job.log(f"TimescaleDB ready (version probe: {live or 'n/a'})")


async def _sync_mysql_passwords(
    job: MigrationJob,
    svc: str,
    password: str,
    user: str = "root",
    db_type: str = "mysql",
    db_name: str = "pasarguard",
    auth_passwords: list[str] | None = None,
    *,
    allow_skip_grant_recovery: bool = True,
) -> bool:
    """Align MySQL/MariaDB root (and app user) passwords to the value we keep in .env.

    Tries every known root password candidate first (env + extras). Only if root is
    unreachable does it use temporary ``--skip-grant-tables`` recovery on the same
    data volume — never wipes MySQL data.
    """
    if not password or not svc:
        return False

    from app.services.db_auth import (
        build_mysql_role_password_sql,
        mysql_sync_auth_candidates,
        recover_mysql_passwords_via_skip_grants,
    )

    env_now = _read_current_env()
    if user and user != "root":
        app_user = user
    else:
        # Still align the panel app role even when caller syncs as root.
        app_user = (
            read_env_var(env_now, "DB_USER")
            or read_env_var(env_now, "MYSQL_USER")
            or "pasarguard"
        )

    sql = build_mysql_role_password_sql(
        password, app_user=app_user, db_name=db_name or "pasarguard",
    )
    auth_pwds = mysql_sync_auth_candidates(
        password,
        *(auth_passwords or []),
        env_text=env_now,
    )
    job.log(
        f"Syncing MySQL passwords on {svc} (app user={app_user}, "
        f"auth candidates={len(auth_pwds)})..."
    )
    last_out = ""
    for bin_name in _mysql_client_bins(db_type, svc):
        for auth_pwd in auth_pwds:
            ok, out = await _run(
                job,
                [
                    "docker", "compose", "exec", "-T",
                    "-e", f"MYSQL_PWD={auth_pwd}",
                    svc, bin_name, "-u", "root",
                    "-e", sql,
                ],
                cwd=str(PASARGUARD_DIR),
                timeout=60,
            )
            if ok:
                job.log(f"Synced MySQL passwords on {svc} ({bin_name})")
                return True
            last_out = out or last_out
        # Retry without assuming current password (fresh container / empty root)
        ok2, out2 = await _run(
            job,
            [
                "docker", "compose", "exec", "-T",
                svc, bin_name, "-u", "root",
                "-e", sql,
            ],
            cwd=str(PASARGUARD_DIR),
            timeout=60,
        )
        if ok2:
            job.log(f"Synced MySQL passwords on {svc} ({bin_name}, no-password retry)")
            return True
        last_out = out2 or last_out
    job.log(f"MySQL password sync note: {(last_out or '')[-300:]}")

    if not allow_skip_grant_recovery:
        return False

    async def _run_list(cmd, cwd=None, timeout=600):
        return await _run(job, cmd, cwd=cwd, timeout=timeout)

    return await recover_mysql_passwords_via_skip_grants(
        _run_list,
        service=svc,
        password=password,
        app_user=app_user,
        db_type=db_type,
        db_name=db_name or "pasarguard",
        compose_cwd=str(PASARGUARD_DIR),
        log=job.log,
    )


def parse_ts_post_restore_catalog_mismatch(text: str) -> tuple[str, str] | None:
    """Parse ``catalog version mismatch, expected "X" seen "Y"`` from post_restore."""
    m = re.search(
        r'catalog version mismatch,\s*expected\s+"([^"]+)"\s+seen\s+"([^"]+)"',
        text or "",
        re.I,
    )
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip()


def is_enospc_text(text: str) -> bool:
    low = (text or "").lower()
    return (
        "no space left on device" in low
        or "errno 28" in low
        or "[errno 28]" in low
        or "enospc" in low
    )


def disk_free_bytes(path: str | Path) -> int:
    try:
        return int(shutil.disk_usage(str(path)).free)
    except OSError:
        return -1


# Docker image pulls for Timescale layers routinely need multiple GB free.
_TS_PULL_MIN_FREE_BYTES = 3 * 1024 * 1024 * 1024


def atomic_write_text(path: Path, content: str) -> None:
    """Write file atomically so a full disk never leaves an empty compose/env."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def restore_text_file(path: Path, content: str, *, label: str = "file") -> bool:
    """Best-effort restore of a text file after a failed mutation."""
    try:
        atomic_write_text(path, content)
        return True
    except OSError:
        # Last ditch: try non-atomic write so we at least put bytes back.
        try:
            path.write_text(content, encoding="utf-8")
            return True
        except OSError:
            return False


async def _read_pg_container_init_env(
    job: MigrationJob,
    svc: str,
) -> dict[str, str]:
    """Read POSTGRES_* from the running DB container (image init source of truth)."""
    out: dict[str, str] = {}
    for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        ok, raw = await _run(
            job,
            _compose_argv("exec", "-T", svc, "printenv", key),
            cwd=str(PASARGUARD_DIR),
            timeout=15,
            quiet=True,
        )
        if not ok:
            continue
        val = (raw or "").strip().splitlines()
        if val and val[-1].strip():
            out[key] = val[-1].strip()
    return out


def _psql_exec_succeeded(ok: bool, out: str) -> bool:
    """Treat connection success with FATAL/ERROR lines as failure."""
    if not ok:
        return False
    low = (out or "").lower()
    if "fatal:" in low:
        return False
    # Ignore NOTICE; real SQL failures are ERROR:
    for line in (out or "").splitlines():
        s = line.strip().lower()
        if s.startswith("error:"):
            return False
    return True


def _compose_looks_usable(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return "services:" in t.lower() or bool(re.search(r"^\s*\w+\s*:", t, re.M))


async def _resolve_running_db_container_id(job: MigrationJob, svc: str) -> str | None:
    """Find a running container id/name when ``docker compose exec`` is unusable."""
    from app.services.pasarguard_ops import _active_compose_paths

    paths = _active_compose_paths()
    if paths:
        ok, out = await _run(
            job,
            _compose_argv("ps", "-q", svc),
            cwd=str(PASARGUARD_DIR),
            timeout=20,
            quiet=True,
        )
        cid = (out or "").strip().splitlines()
        if ok and cid and cid[-1].strip():
            return cid[-1].strip()
    # Fallback: match running container names.
    ok2, out2 = await _run(
        job,
        ["docker", "ps", "--format", "{{.ID}} {{.Names}}"],
        timeout=20,
        quiet=True,
    )
    if not ok2:
        return None
    svc_l = (svc or "").lower()
    for line in (out2 or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        cid, name = parts[0], parts[1].lower()
        if svc_l and svc_l in name:
            return cid
        if "timescaledb" in name or "postgres" in name:
            if svc_l in ("timescaledb", "postgresql", "postgres"):
                return cid
    return None


async def _psql_in_db_container(
    job: MigrationJob,
    svc: str,
    *,
    user: str,
    password: str | None,
    database: str,
    sql: str,
    timeout: int = 30,
) -> tuple[bool, str]:
    """Run psql inside the DB container; fall back to ``docker exec`` if compose is broken."""
    cmd = _compose_argv("exec", "-T")
    if password is not None:
        cmd.extend(["-e", f"PGPASSWORD={password}"])
    cmd.extend(
        [
            svc, "psql", "-U", user, "-d", database,
            "-v", "ON_ERROR_STOP=0", "-At", "-c", sql,
        ]
    )
    ok, out = await _run(job, cmd, cwd=str(PASARGUARD_DIR), timeout=timeout, quiet=True)
    if ok or not _compose_exec_unusable(out or ""):
        return ok, out

    cid = await _resolve_running_db_container_id(job, svc)
    if not cid:
        return ok, out
    job.log(f"docker compose exec unusable — falling back to docker exec {cid[:12]}…")
    cmd2 = ["docker", "exec", "-i"]
    if password is not None:
        cmd2.extend(["-e", f"PGPASSWORD={password}"])
    cmd2.extend(
        [
            cid, "psql", "-U", user, "-d", database,
            "-v", "ON_ERROR_STOP=0", "-At", "-c", sql,
        ]
    )
    return await _run(job, cmd2, timeout=timeout, quiet=True)


def _compose_exec_unusable(text: str) -> bool:
    low = (text or "").lower()
    return (
        "empty compose file" in low
        or "no configuration file provided" in low
        or "compose.yaml" in low and "not found" in low
        or "no such service" in low
        or "cannot find" in low and "compose" in low
    )


async def _sync_pg_role_passwords(
    job: MigrationJob,
    svc: str,
    password: str,
    user: str,
    db_name: str,
) -> None:
    """Force DB roles to match live .env — fixes SASL auth after globals.sql restore."""
    if not password:
        return

    from app.services.db_auth import (
        build_postgres_auth_attempts,
        postgres_role_candidates,
        summarize_pg_auth_errors,
    )

    env_now = _read_current_env()
    container_env = await _read_pg_container_init_env(job, svc)
    roles = postgres_role_candidates(
        env_now,
        user,
        db_name,
        container_user=container_env.get("POSTGRES_USER"),
        include_postgres_fallback=True,
    )
    auth_attempts = build_postgres_auth_attempts(
        env_now,
        preferred_user=user,
        preferred_password=password,
        extra_users=(db_name, container_env.get("POSTGRES_USER")),
        container_user=container_env.get("POSTGRES_USER"),
        container_password=container_env.get("POSTGRES_PASSWORD"),
        include_trust=True,
    )
    # Prefer an admin session that can ALTER ROLE (try app DB then postgres).
    admin_dbs = []
    for d in (db_name, container_env.get("POSTGRES_DB"), "pasarguard", "postgres"):
        if d and d not in admin_dbs:
            admin_dbs.append(d)

    lit = _sql_literal(password)
    sync_errors: list[tuple[str, str]] = []
    for role in roles:
        sql = f'ALTER ROLE "{role}" WITH PASSWORD {lit};'
        synced = False
        for auth_user, auth_pwd in auth_attempts:
            for admin_db in admin_dbs:
                ok, out = await _psql_in_db_container(
                    job, svc,
                    user=auth_user,
                    password=auth_pwd,
                    database=admin_db,
                    sql=sql,
                    timeout=30,
                )
                if _psql_exec_succeeded(ok, out):
                    job.log(f"Synced password for role {role} (as {auth_user})")
                    synced = True
                    break
                err = extract_psql_errors(out or "")[:240] or (out or "")[-240:]
                if err:
                    sync_errors.append((auth_user, err))
            if synced:
                break
        if not synced:
            job.log(
                f"Could not sync password for role {role}: "
                f"{summarize_pg_auth_errors(sync_errors[-6:]) or 'all auth attempts failed'}"
            )
    # Restart pgbouncer so auth cache picks up new SCRAM secrets (ignore if absent)
    if _compose_has_service("pgbouncer"):
        await _compose(job, "restart", "pgbouncer", timeout=90)
        await asyncio.sleep(3)


async def _ensure_timescaledb_not_in_restore_mode(
    job: MigrationJob,
    password: str,
    user: str,
    db_name: str,
) -> None:
    """Guard against TimescaleDB being stuck in restore mode before panel start.

    If timescaledb_pre_restore() was called but post_restore() was never
    confirmed, the extension keeps the DB in maintenance mode and PasarGuard
    silently crashes on every connect attempt — producing a restart loop with
    no visible error in panel logs (often stops right after alembic Context).

    Strategy:
    1. Auth via .env + container POSTGRES_* + local socket trust (not only postgres).
    2. Clear restore mode on the app DB and every other connectable DB that is on.
    3. ``timescaledb_post_restore()`` (twice — known Timescale GUC quirk).
    4. On catalog version mismatch: align image to the dump catalog version (no wipe).
    5. Emergency ``ALTER DATABASE … RESET/SET timescaledb.restoring`` + session SET.
    6. Re-verify on a fresh connection; only hard-fail if still on.
    """
    from app.services.db_auth import (
        build_postgres_auth_attempts,
        summarize_pg_auth_errors,
    )

    svc = await _detect_db_container(job, "timescaledb") or await _detect_db_container(job, "postgresql")
    if not svc:
        return

    check_sql = "SELECT current_setting('timescaledb.restoring', true);"
    post_sql = "SELECT timescaledb_post_restore();"
    # Emergency clear mirrors what post_restore does for the GUC when the function
    # itself cannot run (permission / catalog check) — enough to stop panel death.
    emergency_sql = (
        "DO $ts$ DECLARE db text := current_database(); BEGIN "
        "EXECUTE format('ALTER DATABASE %I RESET timescaledb.restoring', db); "
        "EXECUTE format('ALTER DATABASE %I SET timescaledb.restoring = %L', db, 'off'); "
        "EXCEPTION WHEN OTHERS THEN "
        "EXECUTE format('ALTER DATABASE %I SET timescaledb.restoring = %L', db, 'off'); "
        "END $ts$; "
        "SET timescaledb.restoring TO off;"
    )
    grant_sql_tmpl = 'ALTER ROLE "{role}" WITH SUPERUSER;'

    dbn = (db_name or "").strip() or "pasarguard"
    env_now = _read_current_env()
    container_env = await _read_pg_container_init_env(job, svc)
    auth_attempts = build_postgres_auth_attempts(
        env_now,
        preferred_user=user,
        preferred_password=password,
        extra_users=(dbn, container_env.get("POSTGRES_USER")),
        container_user=container_env.get("POSTGRES_USER"),
        container_password=container_env.get("POSTGRES_PASSWORD"),
        include_trust=True,
    )
    if not auth_attempts:
        auth_attempts = [(user or "pasarguard", password or None)]

    async def _try(
        sql: str,
        auth_user: str,
        auth_pwd: str | None,
        database: str,
        *,
        timeout: int = 20,
    ) -> tuple[bool, str]:
        ok, out = await _psql_in_db_container(
            job, svc,
            user=auth_user,
            password=auth_pwd,
            database=database,
            sql=sql,
            timeout=timeout,
        )
        return _psql_exec_succeeded(ok, out), out

    async def _find_auth_for_db(database: str) -> tuple[tuple[str, str | None] | None, str, list[tuple[str, str]]]:
        errors: list[tuple[str, str]] = []
        restoring_val = ""
        for auth_user, auth_pwd in auth_attempts:
            ok, out = await _try(check_sql, auth_user, auth_pwd, database, timeout=20)
            if not ok:
                err = extract_psql_errors(out or "")[:240] or (out or "")[-240:]
                if err:
                    errors.append((auth_user, err))
                continue
            val = (out or "").strip().splitlines()
            restoring_val = val[-1].strip().lower() if val else ""
            return (auth_user, auth_pwd), restoring_val, errors
        return None, restoring_val, errors

    async def _list_databases(seed_auth: tuple[str, str | None] | None) -> list[str]:
        dbs = [dbn]
        order = list(auth_attempts)
        if seed_auth is not None:
            order = [seed_auth] + [a for a in auth_attempts if a != seed_auth]
        list_sql = (
            "SELECT datname FROM pg_database "
            "WHERE datallowconn AND NOT datistemplate ORDER BY 1;"
        )
        for auth_user, auth_pwd in order:
            for probe_db in (dbn, "postgres", container_env.get("POSTGRES_DB") or "pasarguard"):
                if not probe_db:
                    continue
                ok, out = await _try(list_sql, auth_user, auth_pwd, probe_db, timeout=20)
                if not ok:
                    continue
                found = []
                for line in (out or "").splitlines():
                    name = line.strip()
                    if name and re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", name):
                        found.append(name)
                if found:
                    # App DB first, then the rest.
                    ordered = [dbn] + [d for d in found if d != dbn]
                    return list(dict.fromkeys(ordered))
        return dbs

    async def _promote_superuser(working: tuple[str, str | None], database: str) -> None:
        role = working[0]
        sql = grant_sql_tmpl.format(role=role.replace('"', '""'))
        for auth_user, auth_pwd in auth_attempts:
            ok, _out = await _try(sql, auth_user, auth_pwd, database, timeout=20)
            if ok:
                job.log(f"Granted SUPERUSER to role {role} (as {auth_user}) for post_restore")
                return

    async def _run_post_restore(
        database: str,
        attempts: list[tuple[str, str | None]],
    ) -> tuple[bool, tuple[str, str | None] | None, list[tuple[str, str]], str | None]:
        """Returns cleared, working_auth, errors, catalog_seen_version."""
        errors: list[tuple[str, str]] = []
        catalog_seen: str | None = None
        for auth_user, auth_pwd in attempts:
            ok, out = await _try(post_sql, auth_user, auth_pwd, database, timeout=45)
            if ok:
                job.log(f"TimescaleDB restore mode cleared successfully on {database} (as {auth_user})")
                return True, (auth_user, auth_pwd), errors, catalog_seen
            err = extract_psql_errors(out or "")[:400] or (out or "")[-400:]
            if err:
                errors.append((auth_user, err))
            mismatch = parse_ts_post_restore_catalog_mismatch(out or "")
            if mismatch:
                # Align to the catalog version stored in the restored dump ("seen").
                catalog_seen = mismatch[1]
        return False, None, errors, catalog_seen

    async def _emergency_clear(
        database: str,
        attempts: list[tuple[str, str | None]],
    ) -> tuple[bool, tuple[str, str | None] | None, list[tuple[str, str]]]:
        errors: list[tuple[str, str]] = []
        for auth_user, auth_pwd in attempts:
            ok, out = await _try(emergency_sql, auth_user, auth_pwd, database, timeout=30)
            if ok:
                job.log(f"Emergency cleared timescaledb.restoring on {database} (as {auth_user})")
                return True, (auth_user, auth_pwd), errors
            err = extract_psql_errors(out or "")[:240] or (out or "")[-240:]
            if err:
                errors.append((auth_user, err))
        return False, None, errors

    async def _confirm_off(
        database: str,
        preferred: tuple[str, str | None] | None,
    ) -> tuple[bool, str]:
        order = list(auth_attempts)
        if preferred is not None:
            order = [preferred] + [a for a in auth_attempts if a != preferred]
        for auth_user, auth_pwd in order:
            ok, out = await _try(check_sql, auth_user, auth_pwd, database, timeout=20)
            if not ok:
                continue
            val = (out or "").strip().splitlines()
            after = val[-1].strip().lower() if val else ""
            return True, after
        return False, ""

    async def _clear_one_database(database: str) -> None:
        working, restoring, check_errors = await _find_auth_for_db(database)
        need_post = restoring == "on" or working is None
        if not need_post:
            job.log(f"TimescaleDB restore mode on {database}: {restoring or 'off/n/a'} — OK")
            return

        if restoring == "on":
            job.log(f"TimescaleDB {database} is still in restore mode — clearing now")
        else:
            job.log(
                f"TimescaleDB restore-mode check inconclusive on {database} — "
                "forcing post_restore / emergency clear"
            )
            if check_errors:
                job.log(f"restore-mode check notes ({database}):\n{summarize_pg_auth_errors(check_errors)}")

        attempts = list(auth_attempts)
        if working is not None:
            attempts = [working] + [a for a in auth_attempts if a != working]
            # Reading the GUC does not require SUPERUSER; post_restore does.
            await _promote_superuser(working, database)

        cleared, used_auth, post_errors, catalog_seen = await _run_post_restore(database, attempts)

        # Known Timescale quirk: first post_restore may leave GUC visible as on.
        if cleared:
            ok_c, after = await _confirm_off(database, used_auth)
            if ok_c and after == "on":
                job.log(f"timescaledb.restoring still on in {database} after first post_restore — retrying")
                cleared2, used_auth2, post_errors2, catalog_seen2 = await _run_post_restore(database, attempts)
                catalog_seen = catalog_seen or catalog_seen2
                post_errors.extend(post_errors2)
                if cleared2:
                    used_auth = used_auth2 or used_auth
                    cleared = True
                else:
                    cleared = False

        if not cleared and catalog_seen:
            # Catalog mismatch (e.g. expected 2.28 seen 2.27): post_restore cannot run
            # until the image matches. Prefer emergency GUC clear FIRST — it does not
            # need a multi-GB docker pull (ENOSPC) and unblocks PasarGuard immediately.
            # Only then optionally try a no-wipe image align when disk allows.
            job.log(
                f"timescaledb_post_restore catalog mismatch on {database} "
                f"(dump catalog {catalog_seen}) — emergency-clearing restoring GUC first"
            )
            emerg_ok, emerg_auth, emerg_errors = await _emergency_clear(database, attempts)
            post_errors.extend(emerg_errors)
            if emerg_ok:
                cleared = True
                used_auth = emerg_auth or used_auth
            else:
                pin = parse_timescale_wanted([catalog_seen]) or catalog_seen
                free = disk_free_bytes("/var/lib")
                if free >= 0 and free < _TS_PULL_MIN_FREE_BYTES:
                    job.log(
                        f"Skipping Timescale image align to {pin}: only "
                        f"{free // (1024 * 1024)} MiB free (need ≥"
                        f"{_TS_PULL_MIN_FREE_BYTES // (1024 * 1024)} MiB)"
                    )
                else:
                    job.log(
                        f"Emergency clear failed — trying image align to dump catalog "
                        f"{pin} (no data wipe)"
                    )
                    try:
                        await _align_timescaledb_image(job, pin, wipe_data=False)
                    except Exception as e:
                        job.log(f"Timescale align after catalog mismatch note: {e}")
                    else:
                        cleared, used_auth, post_errors2, _ = await _run_post_restore(
                            database, attempts
                        )
                        post_errors.extend(post_errors2)
                        if cleared:
                            ok_c, after = await _confirm_off(database, used_auth)
                            if ok_c and after == "on":
                                await _run_post_restore(database, attempts)

        if not cleared:
            emerg_ok, emerg_auth, emerg_errors = await _emergency_clear(database, attempts)
            post_errors.extend(emerg_errors)
            if emerg_ok:
                cleared = True
                used_auth = emerg_auth or used_auth
                # Best-effort: still call post_restore once workers can start.
                await _run_post_restore(database, attempts)

        if not cleared:
            detail = summarize_pg_auth_errors(post_errors or check_errors) or "all clear attempts failed"
            tried = ", ".join(dict.fromkeys(u for u, _ in attempts))
            if restoring == "on" or working is None:
                raise RuntimeError(
                    "TimescaleDB is stuck in restoring=on and timescaledb_post_restore() failed.\n"
                    f"Database: {database}\n"
                    f"Tried roles: {tried}\n"
                    f"{detail}\n"
                    "PasarGuard would crash silently after alembic Context — refusing to continue."
                )
            job.log(f"timescaledb_post_restore warning on {database}: {detail}")
            return

        ok_c, after = await _confirm_off(database, used_auth)
        if ok_c and after == "on":
            # Last resort emergency if post claimed success but DB setting remains.
            emerg_ok, emerg_auth, _ = await _emergency_clear(database, attempts)
            if emerg_ok:
                ok_c, after = await _confirm_off(database, emerg_auth or used_auth)
            if ok_c and after == "on":
                raise RuntimeError(
                    "TimescaleDB timescaledb.restoring is still on after post_restore — "
                    f"database={database}. Panel would die silently. Refusing to mark restore as success."
                )
        if ok_c:
            job.log(f"TimescaleDB restore mode confirmed clear on {database} ({after or 'off/n/a'})")
        else:
            job.log(
                f"TimescaleDB clear on {database} succeeded; could not re-confirm GUC (auth) — continuing"
            )

    seed_auth, seed_restoring, _ = await _find_auth_for_db(dbn)
    databases = await _list_databases(seed_auth)
    # If the app DB is on, always clear it; also clear any other DB still on.
    targets: list[str] = []
    for database in databases:
        if database == dbn:
            targets.append(database)
            continue
        _auth, restoring, _errs = await _find_auth_for_db(database)
        if restoring == "on":
            targets.append(database)
    if not targets:
        targets = [dbn]

    if seed_restoring != "on" and seed_auth is not None and targets == [dbn]:
        # Fast path already confirmed off above in _clear_one when we call it —
        # still invoke for consistent logging.
        pass

    for database in targets:
        await _clear_one_database(database)


async def _heal_panel_auth_if_needed(job: MigrationJob, password: str, user: str, db_name: str, db_type: str) -> None:
    """If panel crash-loops on SASL/password, re-sync roles and restart."""
    from app.services.pasarguard_ops import panel_compose_service

    if db_type not in ("postgresql", "timescaledb", "mysql", "mariadb"):
        return
    panel = panel_compose_service()
    ok, logs = await _run(
        job,
        _compose_argv("logs", "--tail", "80", panel),
        cwd=str(PASARGUARD_DIR),
        timeout=40,
    )
    blob = logs or ""
    if not is_auth_failure_text(blob):
        return
    job.log("Detected DB authentication failure in panel logs — auto-healing credentials...")
    if db_type in ("postgresql", "timescaledb"):
        svc = await _detect_db_container(job, db_type)
        if svc:
            await _sync_pg_role_passwords(job, svc, password, user or "pasarguard", db_name or "pasarguard")
    elif db_type in ("mysql", "mariadb"):
        svc = await _detect_db_container(job, db_type)
        if svc and password:
            await _sync_mysql_passwords(
                job, svc, password,
                user=user or "pasarguard",
                db_type=db_type,
                db_name=db_name or "pasarguard",
            )
    await _compose(job, "up", "-d", "--force-recreate", panel, timeout=120)
    await asyncio.sleep(6)
    ok2, logs2 = await _run(
        job,
        _compose_argv("logs", "--tail", "40", panel),
        cwd=str(PASARGUARD_DIR),
        timeout=40,
    )
    if is_auth_failure_text(logs2 or ""):
        job.log("Auth still failing after heal — check DB_PASSWORD / POSTGRES_PASSWORD in /opt/pasarguard/.env")
    else:
        job.log("Auth heal applied — panel should start cleanly")


async def _maybe_cross_db_after_restore(
    job: MigrationJob,
    params: dict,
    backup_db: str,
    target_db: str,
    password: str,
    user: str,
    db_name: str,
    source_path: str | None = None,
    *,
    install_env_snapshot: str | None = None,
) -> tuple[str, dict, dict]:
    """Convert restored backup engine → installed PasarGuard DB (auto)."""
    if not target_db or backup_db == target_db or soft_db_family(backup_db, target_db):
        return target_db or backup_db, {}, {}

    job.set_progress(85, f"Converting {backup_db} → {target_db}…")
    job.log(f"Auto DB convert: {backup_db} → {target_db}")

    # Resolve source path for two-phase engine
    path = source_path
    if not path:
        if backup_db == "sqlite":
            path = str(PASARGUARD_DATA / "db.sqlite3")
        else:
            path = ""
    if backup_db == "sqlite" and (not path or not Path(path).exists()):
        path = str(PASARGUARD_DATA / "db.sqlite3")
    if not path or not Path(path).exists():
        raise RuntimeError(
            f"Cannot convert {backup_db} → {target_db}: source file missing ({path or 'n/a'})"
        )

    try:
        from app.services.native_migration.cross_db import run_cross_db_migration
        from app.services.db_auth import migration_params_from_connection, resolve_live_admin_connection

        class _Mini:
            def __init__(self, j, p):
                self.job = j
                self.params = p
                self.copy_stats = {}
                self.copy_report = {}

            async def _run_cmd(self, cmd, cwd=None, timeout=600, *, quiet: bool = False):
                if isinstance(cmd, str):
                    proc = await asyncio.create_subprocess_shell(
                        cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        cwd=cwd,
                        start_new_session=True,
                    )
                    out_b, _ = await proc.communicate()
                    out = (out_b or b"").decode("utf-8", errors="replace")
                    return proc.returncode == 0, out
                ok, out = await _run(
                    self.job, cmd, cwd=cwd, timeout=timeout, quiet=quiet
                )
                return ok, out or ""

        # Prefer install .env for target auth — merged backup .env often still has
        # Timescale/Postgres secrets and incomplete MYSQL_* until finalize.
        env_text = install_env_snapshot or _read_current_env()
        if target_db != "sqlite":
            svc = "timescaledb" if target_db == "timescaledb" else await _detect_db_container(job, target_db)
            if svc:
                # MySQL/MariaDB installs have no pgbouncer — only start services that exist
                extras = ("pgbouncer",) if target_db in ("postgresql", "timescaledb") else ()
                await _compose_up_services(job, svc, *extras, timeout=300)
                await asyncio.sleep(5)
            probe_mini = _Mini(job, {"target_db": target_db, "_auto_db_credentials": True})
            try:
                admin = await resolve_live_admin_connection(
                    probe_mini, target_db, env_text=env_text,
                )
            except RuntimeError:
                # Fallback: try live merged .env (same-engine soft path may have updated it)
                if install_env_snapshot:
                    job.log("Install-snapshot auth failed — retrying with live .env")
                    admin = await resolve_live_admin_connection(
                        probe_mini, target_db, env_text=_read_current_env(),
                    )
                else:
                    raise
            if target_db in ("postgresql", "timescaledb"):
                await _sync_pg_role_passwords(
                    job,
                    svc or "timescaledb",
                    admin.get("password") or password or "",
                    admin.get("user") or "postgres",
                    db_name or "pasarguard",
                )
            mig_params = migration_params_from_connection(backup_db, target_db, admin)
        else:
            mig_params: dict = {
                "source_db": backup_db,
                "target_db": target_db,
                "target_db_user": user,
                "target_db_password": password,
                "target_db_name": db_name or "pasarguard",
            }

        mig_params["_auto_db_credentials"] = True
        mini = _Mini(job, mig_params)
        await run_cross_db_migration(mini, path, backup_db, target_db)
        stats = getattr(mini, "copy_stats", None) or {}
        report = getattr(mini, "copy_report", None) or {}
        # Remember credentials that actually worked during convert
        if target_db != "sqlite":
            report["live_admin"] = {
                "user": mig_params.get("target_db_user"),
                "password": mig_params.get("target_db_password"),
                "database": mig_params.get("target_db_name") or db_name or "pasarguard",
            }
        job.result = {**(job.result or {}), "copy_stats": stats, "copy_report": report}
        job.log(f"DB convert finished: now {target_db}")
        return target_db, stats, report
    except Exception as e:
        job.log(f"DB convert failed — target schema may have been reset; "
                f"retry restore. Underlying: {e}")
        explain = explain_restore_error(e, backup_db, target_db)
        err = RuntimeError(explain.get("en") or str(e))
        err.explain = explain  # type: ignore[attr-defined]
        raise err from e


def explain_restore_error(exc: Exception, backup_db: str | None = None, target_db: str | None = None) -> dict:
    """Human-readable multilingual restore/convert error."""
    raw = str(exc) or exc.__class__.__name__
    low = raw.lower()
    fa = "ریستور یا تبدیل دیتابیس ناموفق بود."
    en = "Restore or database conversion failed."
    ru = "Восстановление или конвертация БД не удалась."
    causes_fa: list[str] = []

    if "missing 1 required positional argument" in low or "source_path" in low:
        fa = "خطای داخلی تبدیل دیتابیس (پارامتر مسیر منبع)."
        en = "Internal DB conversion error (source path)."
        causes_fa = ["نسخه ویزارد قدیمی بود — آپدیت کنید و دوباره ریستور کنید."]
    elif "unsupported cross-db" in low:
        fa = f"تبدیل {backup_db} به {target_db} پشتیبانی نمی‌شود."
        en = f"Conversion {backup_db} → {target_db} is not supported."
        causes_fa = ["این ترکیب موتور دیتابیس قابل تبدیل خودکار نیست."]
    elif is_auth_failure_text(raw) or ("password" in low and "auth" in low) or "authentication failed" in low:
        fa = "احراز هویت دیتابیس شکست خورد (پسورد/SASL)."
        en = "Database authentication failed (password/SASL)."
        tgt = (target_db or "").lower()
        bak = (backup_db or "").lower()
        mysqlish = (
            tgt in ("mysql", "mariadb")
            or "mysql/mariadb authentication" in low
            or "access denied for user" in low
            or (not tgt and ("mysql" in low or "mariadb" in low))
        )
        if mysqlish:
            causes_fa = [
                "رمز MYSQL_ROOT_PASSWORD / DB_PASSWORD در .env نصب با رمز واقعی کانتینر MySQL/MariaDB یکی نیست",
                "کانتینر MariaDB ممکن است فقط باینری mariadb داشته باشد — ویزارد هر دو کلاینت را امتحان می‌کند",
                "بعد از تبدیل از Timescale، ویزارد باید از رمز نصب (نه رمز Postgres بکاپ) استفاده کند",
            ]
            if bak in ("postgresql", "timescaledb"):
                causes_fa.insert(
                    0,
                    f"بکاپ={bak} → نصب={tgt or 'mysql/mariadb'}: رمز نصب MySQL/MariaDB را نگه دارید",
                )
        elif tgt in ("postgresql", "timescaledb") or (
            bak in ("postgresql", "timescaledb") and tgt not in ("mysql", "mariadb")
        ):
            causes_fa = [
                "رمز POSTGRES_PASSWORD در .env با رمز واقعی کانتینر TimescaleDB/PostgreSQL یکی نیست",
                "PgBouncer کش قدیمی دارد — ویزارد نقش‌ها را هم‌تراز و pgbouncer را ریستارت می‌کند",
                "بعد از ریستور postgres، globals.sql ممکن است نقش‌ها را با رمز بکاپ برگرداند",
            ]
        else:
            causes_fa = [
                "رمز دیتابیس در .env با رمز واقعی کانتینر یکی نیست",
                "بعد از ریستور/تبدیل، نقش‌ها ممکن است با رمز دیگری هم‌خوان شده باشند",
                "لاگ کامل کانتینر دیتابیس را برای جزئیات auth ببینید",
            ]
    elif "character varying(32)" in low or "stringdatarighttruncation" in low:
        fa = "خطای ثبت نسخه alembic بعد از کپی داده (نسخه نامعتبر)."
        en = "Alembic version stamp failed after data copy (invalid revision string)."
        causes_fa = [
            "خروجی docker compose با نسخه alembic قاطی شده بود — در v2.3.5+ اصلاح شد",
            "اسکیمای target قبلاً با alembic upgrade head ساخته شده و دیگر نیاز به stamp دستی نیست",
        ]
    elif "no space left" in low or "enospc" in low or "errno 28" in low:
        fa = "فضای دیسک سرور پر است (دانلود ایمیج Timescale یا نوشتن فایل شکست خورد)."
        en = "Server disk is full (Timescale image pull or file write failed)."
        ru = "На диске сервера закончилось место (pull образа Timescale / запись файла)."
        causes_fa = [
            "لاگ docker: no space left on device — ایمیج Timescale چند گیگابایت فضا می‌خواهد",
            "دیسک را آزاد کنید (docker image prune / لاگ‌ها) و دوباره ریستور کنید",
            "ویزارد جدید بدون pull، حالت restoring را اضطراری خاموش می‌کند تا پنل بالا بیاید",
        ]
    elif "empty compose file" in low:
        fa = "فایل docker-compose.yml خالی یا خراب شده است."
        en = "docker-compose.yml is empty or unusable."
        ru = "Файл docker-compose.yml пуст или повреждён."
        causes_fa = [
            "پر شدن دیسک هنگام تغییر تگ ایمیج ممکن است compose را خراب کند — از .yml.pgclockmg.bak برگردانید",
            "ویزارد جدید compose را atomic می‌نویسد و در ENOSPC برمی‌گرداند",
        ]
    elif "catalog version mismatch" in low and "timescale" in low:
        fa = "نسخه کاتالوگ TimescaleDB بکاپ با ایمیج در حال اجرا یکی نیست (post_restore)."
        en = "TimescaleDB catalog version in the dump does not match the running image (post_restore)."
        ru = "Версия каталога TimescaleDB в дампе не совпадает с образом (post_restore)."
        causes_fa = [
            "بکاپ multi ممکن است متادیتای 2.27 و 2.28 داشته باشد — ایمیج باید با کاتالوگ دامپ هم‌تراز شود",
            "ویزارد ایمیج را بدون پاک کردن دیتا هم‌تراز می‌کند و دوباره post_restore می‌زند",
            "اگر باز هم خطا بود، نسخه timescaledb در docker-compose.yml را با نسخه بکاپ یکی کنید",
        ]
    elif "restoring=on" in low or "timescaledb_post_restore" in low or (
        "timescaledb.restoring" in low and "still on" in low
    ):
        fa = "TimescaleDB در حالت ریستور گیر کرده و post_restore موفق نشد."
        en = "TimescaleDB is stuck in restore mode and post_restore failed."
        ru = "TimescaleDB застрял в режиме restore и post_restore не удался."
        causes_fa = [
            "نقش واقعی سوپریوزر کانتینر معمولاً POSTGRES_USER / DB_USER است — نقش postgres ممکن است اصلاً وجود نداشته باشد",
            "گاهی timescaledb_post_restore به‌خاطر catalog version mismatch خطا می‌دهد — ویزارد ایمیج را هم‌تراز و در نهایت GUC را اضطراری خاموش می‌کند",
            "اگر دوباره خطا شد، لاگ را برای catalog version mismatch / Tried roles / Database: ببینید",
        ]
    elif "timescale" in low and "version" in low:
        fa = "نسخه TimescaleDB بکاپ با سرور هم‌خوان نیست."
        en = "TimescaleDB version mismatch between backup and server."
        causes_fa = ["ویزارد معمولاً ایمیج را هم‌تراز می‌کند — دوباره تلاش کنید یا لاگ کامل را ببینید."]
    elif is_ts_catalog_mismatch_error(raw) or (
        "schema_name" in low and "chunk" in low and "does not exist" in low
    ):
        fa = "کاتالوگ TimescaleDB بکاپ با نسخه نصب‌شده سازگار نیست (مثلاً schema_name در chunk)."
        en = "TimescaleDB catalog in the backup does not match the installed extension (e.g. chunk.schema_name)."
        needs_ver = ts_floor_from_error_text(raw)
        if needs_ver:
            pin = ts_pin_for_floor(needs_ver)
            fa = f"بکاپ از TimescaleDB جدیدتری گرفته شده — کاتالوگ آن به نسخه {needs_ver} یا بالاتر نیاز دارد."
            en = (
                f"Backup catalog needs TimescaleDB {needs_ver} or newer — "
                f"the installed extension is older."
            )
            ru = f"Каталог бэкапа требует TimescaleDB {needs_ver} или новее — установленная версия старее."
            causes_fa = [
                f"نسخه TimescaleDB سرور از بکاپ قدیمی‌تر است — ایمیج باید روی {pin} پین شود",
                "ویزارد جدید این حالت را قبل از ریستور تشخیص می‌دهد و ایمیج را هم‌تراز می‌کند — آپدیت کنید و دوباره ریستور کنید",
                f"دستی: در docker-compose.yml ایمیج timescaledb را روی {pin}-pgXX بگذارید، volume را پاک کنید و ریستور را تکرار کنید",
            ]
        else:
            causes_fa = [
                f"از TimescaleDB 2.29 ستون schema_name از جدول chunk حذف شده — بکاپ‌های قدیمی نیاز به ایمیج {TS_LAST_SCHEMA_NAME_CHUNK} دارند",
                "ویزارد در نسخه جدید اثر انگشت دامپ را تشخیص می‌دهد و ایمیج را قبل از ریستور هم‌تراز می‌کند — آپدیت کنید و دوباره ریستور کنید",
                "اگر هنوز خطا می‌دهد، در docker-compose.yml ایمیج timescaledb را دستی روی 2.28.3-pgXX بگذارید و volume را پاک کنید",
            ]
    elif (
        "certificate files were not restored" in low
        or "certs restore failed" in low
        or ("ssl certificate file" in low and "does not exist" in low)
    ):
        fa = "گواهی SSL بکاپ به /var/lib/pasarguard/certs منتقل نشد یا در .env مپ نشد."
        en = "Backup SSL certs were not restored/mapped under /var/lib/pasarguard/certs."
        causes_fa = [
            "پوشه certs باید داخل زیپ بکاپ باشد (نه فقط مسیر در .env)",
            "در v2.4.0+ certs به /var/lib/pasarguard/certs کپی و UVICORN_SSL_* روی همان مسیر مپ می‌شود",
            "اگر بکاپ بدون certs گرفته شده، دوباره با certs بکاپ بگیرید یا پنل را بدون SSL نصب کنید",
        ]
    elif "dict can not be used as parameter" in low or "dict cannot be used as parameter" in low:
        fa = "مقدار JSON از Postgres به‌صورت dict به MariaDB/MySQL پاس شد."
        en = "PostgreSQL JSON/JSONB dict was passed raw to MySQL/MariaDB (invalid bind param)."
        causes_fa = [
            "ستون‌های permissions / proxy_settings / config باید قبل از insert به JSON string تبدیل شوند",
            "در v2.8.10+ همه dict/list برای MySQL serialize می‌شوند — آپدیت و دوباره ریستور کنید",
        ]
    elif "incorrect datetime value" in low or "1292" in low:
        fa = "فرمت تاریخ/زمان Postgres با ستون DATETIME در MySQL/MariaDB سازگار نبود."
        en = "PostgreSQL timestamptz value is incompatible with MySQL/MariaDB DATETIME."
        causes_fa = [
            "مقادیر با پسوند +00:00 باید بدون timezone نوشته شوند — در v2.8.9+ اصلاح شد",
            "آپدیت ویزارد و دوباره ریستور/تبدیل کنید",
        ]
    elif "migration incomplete" in low:
        fa = "بخشی از داده‌ها کپی نشد (کاربر/هاست/گروه/نود ناقص)."
        en = "Incomplete data copy (users/hosts/groups/nodes)."
        causes_fa = [
            "تبدیل باید ۱۰۰٪ باشد — در v2.3.9+ کپی ناقص fail می‌شود",
            "لاگ Row skip را برای جدول مشکل‌دار ببینید",
        ]
    elif "restore verification failed" in low or "data incomplete" in low or "panel database is empty" in low:
        fa = "داده به موتور مقصد منتقل نشده (موفقیت کاذب قطع شد)."
        en = "Data was not transferred into the target database (false success blocked)."
        causes_fa = [
            "دامپ خالی/ناموفق بود یا بعد از ریستور حجم Timescale پاک شده بود — در v2.5.0 wipe بعد از ریستور حذف شد",
            "verify اجباری: اگر بکاپ کاربر/هاست دارد، پنل خالی دیگر SUCCESS نمی‌شود",
            "لاگ Verified / expected counts را ببینید",
        ]
    elif "pasarguard container is not running" in low:
        fa = "کانتینر PasarGuard بالا نیامد (ری‌استارت یا کرش)."
        en = "PasarGuard container is not running (crash/restart loop)."
        causes_fa = [
            "بعد از تبدیل، .env هنوز URL اشتباه (مثلاً sqlite) داشت — در v2.3.8+ از .env نصب حفظ می‌شود",
            "multi-worker: NATS باید قبل از پنل بالا باشد و NATS_URL باید nats://nats:4222 باشد نه localhost",
            "SSL نامعتبر یا خطای اتصال به PostgreSQL/PgBouncer — لاگ واقعی ValueError/asyncpg را ببینید",
            "روی سرور: docker compose -f /opt/pasarguard/docker-compose.yml logs pasarguard --tail 80",
        ]
    elif "application startup failed" in low:
        fa = "پنل بعد از ریستور در مرحله startup کرش کرد (Application startup failed)."
        en = "Panel crashed during application startup after restore."
        ru = "Панель упала на этапе application startup после restore."
        causes_fa = [
            "علت واقعی معمولاً چند خط بالاتر در لاگ است — ویزارد v4.4.3+ آن را در پیام خطا می‌آورد",
            "multi-worker: NATS باید قبل از پنل بالا باشد و NATS_URL=nats://nats:4222",
            "Timescale/PostgreSQL: mismatch پسورد .env با DB یا PgBouncer stale cache",
            "SSL: فایل cert/key در /var/lib/pasarguard/certs موجود باشد",
            "روی سرور: docker compose logs pasarguard --tail 200",
        ]
    elif "nats is required" in low or (
        "nats" in low and "multi-worker" in low
    ) or (
        "traceback" in low and "nats" in low and "worker" in low
    ):
        fa = "پنل multi-worker بدون NATS سالم بالا نیامد."
        en = "Multi-worker panel failed because NATS was not ready or NATS_URL was wrong."
        causes_fa = [
            "UVICORN_WORKERS>1 نیاز به NATS_ENABLED=1 و سرویس nats در compose دارد",
            "NATS_URL داخل کانتینر باید nats://nats:4222 باشد — localhost کار نمی‌کند",
            "ویزارد جدید NATS را قبل از پنل بالا می‌آورد؛ اگر باز خطا بود node-worker/scheduler را هم چک کنید",
        ]
    elif "pasarguard failed to start" in low or "did not reach ready state" in low:
        fa = "پنل PasarGuard بعد از ریستور بالا نیامد."
        en = "PasarGuard panel did not start after restore."
        causes_fa = [
            "لاگ pasarguard/panel را ببینید",
            "multi-worker: NATS_URL و بالا بودن nats را چک کنید",
            "ممکن است SSL یا SQLALCHEMY_DATABASE_URL اشتباه باشد",
        ]
    elif "cannot stage" in low and ("timescaledb" in low or "postgresql" in low):
        fa = "دامپ Timescale/PostgreSQL برای تبدیل استیج نشد (سرویس مبدأ روی سرور نیست)."
        en = "Could not stage Timescale/PostgreSQL dump for conversion (source engine not running)."
        causes_fa = [
            "وقتی مقصد MySQL/MariaDB است، ویزارد باید دامپ را در کانتینر موقت Timescale لود کند",
            "در v2.6.3+ استیج موقت برای timescaledb→mysql اضافه شد — آپدیت کنید و دوباره ریستور کنید",
            "دسترسی Docker برای pull ایمیج timescale/timescaledb لازم است",
        ]
    elif "no such file" in low or "missing" in low or "not found" in low:
        fa = "فایل دامپ یا دیتابیس منبع پیدا نشد."
        en = "Source dump/database file was not found."
        causes_fa = ["بکاپ ناقص است", "مسیر /var/lib/pasarguard یا دامپ zip خراب است"]
    elif "docker" in low or "compose" in low:
        fa = "مشکل در Docker / docker compose هنگام ریستور."
        en = "Docker / compose problem during restore."
        causes_fa = ["سرویس Docker بالا نیست", "کانتینر دیتابیس استارت نمی‌شود"]
    else:
        causes_fa = ["جزئیات فنی در لاگ آمده است", f"پیام: {raw[:240]}"]

    if backup_db and target_db and backup_db != target_db:
        fa += f" (بکاپ={backup_db} → نصب={target_db})"
        en += f" (backup={backup_db} → installed={target_db})"

    return {
        "en": en,
        "fa": fa,
        "ru": ru,
        "causes_fa": causes_fa,
        "detail": raw,
    }


async def _finalize_env_after_restore(
    job: MigrationJob,
    install_env_snapshot: str,
    final_db: str,
    password: str | None,
    user: str | None,
    db_name: str | None,
) -> None:
    """Write finalized .env: backup panel settings + target DB URL + remapped SSL."""
    from app.services.env_migration import (
        align_ssl_env_to_disk,
        ssl_cert_files_exist,
    )

    text = (
        PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
        if PASARGUARD_ENV.exists()
        else install_env_snapshot
    )
    backup_wanted_ssl = bool(read_env_var(text, "UVICORN_SSL_CERTFILE"))
    finalized = finalize_pasarguard_env_after_restore(
        text,
        final_db,
        password,
        install_env_snapshot,
        db_user=user,
        db_name=db_name,
    )
    # Second pass after certs are on disk
    finalized = align_ssl_env_to_disk(finalized)
    from app.services.multiworker_stack import align_nats_env_for_compose, detect_multiworker_stack

    pre_stack = detect_multiworker_stack(finalized)
    finalized = align_nats_env_for_compose(finalized)
    post_stack = detect_multiworker_stack(finalized)
    if post_stack["uses_nats"]:
        nats_url = read_env_var(finalized, "NATS_URL") or "?"
        job.log(
            f"Multi-worker NATS: enabled (workers={post_stack['uvicorn_workers']}, "
            f"NATS_URL={nats_url})"
        )
    elif pre_stack["has_nats_service"] and not post_stack["uses_nats"]:
        job.log("NATS service in compose but disabled in .env — leaving single-worker boot path")

    if not env_points_to_db(finalized, final_db):
        raise RuntimeError(
            f".env SQLALCHEMY_DATABASE_URL does not match final engine {final_db}"
        )
    if PASARGUARD_ENV.exists():
        shutil.copy2(PASARGUARD_ENV, PASARGUARD_ENV.with_suffix(".env.bak-before-finalize"))
    PASARGUARD_ENV.write_text(finalized, encoding="utf-8")
    url = read_env_var(finalized, "SQLALCHEMY_DATABASE_URL") or ""
    cert = read_env_var(finalized, "UVICORN_SSL_CERTFILE")
    key = read_env_var(finalized, "UVICORN_SSL_KEYFILE")
    ssl_ok = ssl_cert_files_exist(cert, key)
    from app.services.env_migration import _sqlalchemy_url_line_pattern
    import re as _re
    url_n = len(_re.findall(_sqlalchemy_url_line_pattern(), finalized))
    job.log(
        f"Finalized .env for {final_db} "
        f"(URL driver: {url.split('://')[0] if '://' in url else '?'}, "
        f"SQLALCHEMY lines={url_n}, "
        f"SSL={'ok ' + str(cert) if ssl_ok else 'disabled/missing'})"
    )
    if url_n != 1:
        raise RuntimeError(
            f".env must contain exactly 1 SQLALCHEMY_DATABASE_URL after finalize, found {url_n}"
        )
    if backup_wanted_ssl and not ssl_ok:
        raise RuntimeError(
            "Backup .env requires SSL but certificate files were not restored to "
            "/var/lib/pasarguard/certs/. Include certs/ in the backup zip and retry."
        )


def _relocate_sqlite_after_convert(job: MigrationJob) -> None:
    """Prevent PasarGuard from falling back to local SQLite after server DB convert."""
    sqlite_path = PASARGUARD_DATA / "db.sqlite3"
    if not sqlite_path.exists():
        return
    bak = PASARGUARD_DATA / f"db.sqlite3.pre-convert-{job.job_id}.bak"
    if bak.exists():
        bak.unlink()
    shutil.move(str(sqlite_path), str(bak))
    job.log(f"Moved SQLite aside → {bak.name} (panel uses server DB)")


def _count_sqlite_table(path: Path, table: str) -> int:
    if not path.exists():
        return 0
    try:
        conn = sqlite3.connect(str(path))
        try:
            row = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except Exception:
        return 0


def _count_sqlite_users(path: Path) -> int:
    return _count_sqlite_table(path, "users")


def _snapshot_sqlite_counts(path: Path) -> dict[str, int]:
    from app.services.native_migration.copy_core import VERIFY_TABLES

    out: dict[str, int] = {}
    for table in VERIFY_TABLES:
        n = _count_sqlite_table(path, table)
        if n > 0:
            out[table] = n
    return out


def _backup_sql_mentions_timescale(root: Path) -> bool:
    for path in _backup_dump_files(root)[:8]:
        try:
            head = _read_sql_head(path, 200_000)
        except Exception:
            continue
        if re.search(r"timescaledb", head, re.I):
            return True
    return False


def _estimate_sql_table_counts(sql_text: str) -> dict[str, int]:
    """Best-effort row estimates from pg_dump / mysqldump text."""
    from app.services.sql_dump_counts import RESTORE_COUNT_TABLES, estimate_sql_dump_counts_from_text

    raw = estimate_sql_dump_counts_from_text(sql_text, tables=RESTORE_COUNT_TABLES)
    return {k: v for k, v in raw.items() if isinstance(v, int) and v > 0}


def _estimate_backup_table_counts(root: Path, layout: str | None = None) -> dict[str, int]:
    """Estimate critical table row counts from backup files before restore."""
    from app.services.sql_dump_counts import RESTORE_COUNT_TABLES, scan_sql_dump_file

    art = discover_backup_artifacts(root)
    sqlite_src = art.get("sqlite_path")
    if layout == "sqlite_file" or sqlite_src:
        src = Path(sqlite_src) if sqlite_src else root / "db.sqlite3"
        if not src.exists():
            found = list(root.rglob("db.sqlite3"))
            src = found[0] if found else None
        if src and src.exists():
            return _snapshot_sqlite_counts(src)

    paths = _backup_dump_files(root, max_files=8)
    merged: dict[str, int] = {}
    for path in paths:
        try:
            meta = scan_sql_dump_file(path, tables=RESTORE_COUNT_TABLES)
        except Exception:
            continue
        counts = meta.get("counts") or {}
        for k, v in counts.items():
            if isinstance(v, int) and v > 0:
                merged[k] = merged.get(k, 0) + v
    return merged


async def _count_pg_table(
    job: MigrationJob,
    svc: str,
    password: str,
    user: str,
    db_name: str,
    table: str,
) -> int:
    safe = "".join(c for c in table if c.isalnum() or c == "_")
    if safe != table:
        return -1
    cwd = str(PASARGUARD_DIR)
    cmd = [
        "docker", "compose", "exec", "-T",
        "-e", f"PGPASSWORD={password}",
        svc, "psql", "-t", "-A", "-U", user, "-d", db_name,
        "-c", f'SELECT COUNT(*) FROM "{safe}";',
    ]
    ok, out = await _run(job, cmd, cwd=cwd, timeout=60)
    # Fallback: named container when compose cwd/project is confused
    if not ok or "no configuration file" in (out or "").lower():
        ok2, names = await _run(
            job, ["docker", "ps", "--format", "{{.Names}}"], timeout=20,
        )
        container = None
        for line in (names or "").splitlines():
            n = line.strip()
            if svc in n.lower() or (svc == "timescaledb" and "timescale" in n.lower()):
                container = n
                break
        if container:
            cmd2 = [
                "docker", "exec", "-e", f"PGPASSWORD={password}",
                container, "psql", "-t", "-A", "-U", user, "-d", db_name,
                "-c", f'SELECT COUNT(*) FROM "{safe}";',
            ]
            ok, out = await _run(job, cmd2, timeout=60)
    if not ok:
        return -1
    for line in (out or "").splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    return -1


async def _count_pg_users(
    job: MigrationJob,
    svc: str,
    password: str,
    user: str,
    db_name: str,
) -> int:
    return await _count_pg_table(job, svc, password, user, db_name, "users")


async def _verify_restored_data(
    job: MigrationJob,
    final_db: str,
    password: str,
    user: str,
    db_name: str,
    expected: dict[str, int] | int | None,
    *,
    require_any_data: bool = False,
) -> dict[str, int]:
    """Fail restore if critical tables lost rows — never soft-skip when data was expected."""
    from app.services.native_migration.copy_core import VERIFY_TABLES, STRICT_COMPLETE_TABLES

    if isinstance(expected, int):
        expected = {"users": expected} if expected > 0 else {}
    expected = {k: v for k, v in (expected or {}).items() if isinstance(v, int) and v > 0}

    actual: dict[str, int] = {}
    tables_to_check = list(dict.fromkeys(list(expected.keys()) + list(VERIFY_TABLES)))

    if final_db == "sqlite":
        path = PASARGUARD_DATA / "db.sqlite3"
        for table in tables_to_check:
            actual[table] = _count_sqlite_table(path, table)
    elif final_db in ("postgresql", "timescaledb"):
        svc = await _detect_db_container(job, final_db)
        if not svc:
            svc = "timescaledb" if final_db == "timescaledb" else "postgresql"
        # Probe which service actually answers (try install user, then live_admin)
        probed = None
        candidates_svc = [svc, "timescaledb", "postgresql"] if final_db in ("postgresql", "timescaledb") else [svc]
        pwd_tries = [password]
        # If install password fails, try common env aliases already tried via caller
        for cand in candidates_svc:
            if not cand:
                continue
            for pwd in pwd_tries:
                n = await _count_pg_table(job, cand, pwd, user, db_name, "users")
                if n >= 0:
                    probed = cand
                    password = pwd
                    actual["users"] = n
                    break
            if probed:
                break
        if not probed:
            raise RuntimeError(
                f"Could not verify restored data — DB service for {final_db} is not reachable "
                f"(compose cwd / auth). Tried user={user}."
            )
        for table in tables_to_check:
            if table == "users" and "users" in actual:
                continue
            n = await _count_pg_table(job, probed, password, user, db_name, table)
            if n >= 0:
                actual[table] = n
            elif table in expected:
                raise RuntimeError(
                    f"Could not COUNT {table} after restore — verification failed hard."
                )
    elif final_db in ("mysql", "mariadb"):
        svc = await _detect_db_container(job, final_db)
        if not svc:
            raise RuntimeError(
                f"Could not verify restored data — {final_db} container missing."
            )
        client_bins = _mysql_client_bins(final_db, svc)
        for table in tables_to_check:
            safe = "".join(c for c in table if c.isalnum() or c == "_")
            if safe != table:
                continue
            counted = False
            for mysql_cmd in client_bins:
                cmd = [
                    "docker", "compose", "exec", "-T",
                    "-e", f"MYSQL_PWD={password}",
                    svc, mysql_cmd, "-N", "-u", user, db_name,
                    "-e", f"SELECT COUNT(*) FROM `{safe}`;",
                ]
                ok, out = await _run(job, cmd, cwd=str(PASARGUARD_DIR), timeout=60)
                if ok:
                    for line in (out or "").splitlines():
                        if line.strip().isdigit():
                            actual[table] = int(line.strip())
                            counted = True
                            break
                if counted:
                    break
            if not counted and table in expected:
                raise RuntimeError(
                    f"Could not COUNT {table} after restore — verification failed hard."
                )
    else:
        raise RuntimeError(f"Unsupported final_db for verification: {final_db}")

    gaps = []
    soft_gaps = []
    # settings row counts often drift across PasarGuard versions (many KV rows →
    # one JSON blob) and mysqldump INSERT estimators over-count parentheses in
    # JSON values. Do not fail restore on settings alone when data is present.
    soft_tables = frozenset({"settings"})
    for table, want in expected.items():
        got = actual.get(table, -1)
        if got < 0:
            gaps.append(f"{table}: unreadable/{want}")
        elif got < want:
            msg = f"{table}: {got}/{want}"
            if table in soft_tables and got > 0:
                soft_gaps.append(msg)
                job.log(f"Verified {table}: {got} rows (expected ≥{want}, soft OK)")
            else:
                gaps.append(msg)
        else:
            job.log(f"Verified {table}: {got} rows (expected ≥{want})")

    if soft_gaps:
        job.log(
            "Soft verify note (non-fatal): " + "; ".join(soft_gaps)
        )

    # Even without precise dump estimates: refuse empty critical panel after restore
    critical = [t for t in STRICT_COMPLETE_TABLES if t in ("users", "hosts", "groups", "nodes", "admins", "inbounds")]
    critical_total = sum(actual.get(t, 0) for t in critical)
    expected_total = sum(expected.get(t, 0) for t in critical)
    if expected_total > 0 and critical_total == 0:
        gaps.append(f"critical_tables: 0 rows but backup estimated {expected_total}")
    if require_any_data and expected_total > 0 and critical_total == 0:
        gaps.append("panel data empty after restore")

    if gaps:
        raise RuntimeError(
            "Restore verification failed — data incomplete after convert/restore:\n"
            + "\n".join(gaps)
            + "\nUsers/hosts/groups/nodes/inbounds/admins must transfer. "
            "Env/certs alone are not a successful restore."
        )

    if not expected and require_any_data:
        job.log(
            "Warning: no backup row estimates; live counts: "
            + (", ".join(f"{k}={v}" for k, v in actual.items() if v > 0) or "all empty")
        )
        if critical_total == 0:
            raise RuntimeError(
                "Restore verification failed — panel database is empty after restore "
                "(users/hosts/groups/nodes all 0). Env/certs transfer is not enough."
            )

    return {k: v for k, v in actual.items() if v >= 0}


def _env_completeness_checklist(job: MigrationJob, final_db: str, backup_env: str) -> dict:
    """Log that panel env (port, subscription, telegram) survived change-DB."""
    text = _read_current_env()
    keys = [
        "SQLALCHEMY_DATABASE_URL",
        "UVICORN_PORT",
        "UVICORN_HOST",
        "UVICORN_SSL_CERTFILE",
        "UVICORN_SSL_KEYFILE",
        "SUBSCRIPTION_URL_PREFIX",
        "SUBSCRIPTION_PATH",
        "TELEGRAM_API_TOKEN",
        "TELEGRAM_ADMIN_ID",
        "XRAY_JSON",
        "SUDO_USERNAME",
    ]
    report: dict[str, str] = {}
    for key in keys:
        val = read_env_var(text, key)
        bak = read_env_var(backup_env, key)
        if key == "SQLALCHEMY_DATABASE_URL":
            ok = env_points_to_db(text, final_db)
            report[key] = "ok" if ok else "WRONG_ENGINE"
            job.log(f"Env check {key}: {'matches ' + final_db if ok else 'MISMATCH'}")
            continue
        if key.startswith("UVICORN_SSL_"):
            if bak and not val:
                report[key] = "MISSING_SSL"
                job.log(f"Env check {key}: missing (backup had SSL)")
            elif val:
                report[key] = "ok"
                job.log(f"Env check {key}: {val}")
            else:
                report[key] = "empty"
            continue
        if bak and not val:
            report[key] = "MISSING"
            job.log(f"Env check {key}: missing (was in backup)")
        elif val:
            report[key] = "ok"
            job.log(f"Env check {key}: present")
        else:
            report[key] = "empty"
    missing = [k for k, v in report.items() if v in ("MISSING", "WRONG_ENGINE", "MISSING_SSL")]
    if "SQLALCHEMY_DATABASE_URL" in missing:
        raise RuntimeError(
            f".env SQLALCHEMY_DATABASE_URL does not match final engine {final_db}"
        )
    if any(v == "MISSING_SSL" for v in report.values()):
        raise RuntimeError(
            "Backup SSL settings were not mapped into .env — certs restore failed."
        )
    return report


async def _restore_backup(job: MigrationJob, params: dict, analysis: dict) -> dict:
    upload_id = params["upload_id"]
    zip_path = Path(analysis["zip_path"])
    if not zip_path.exists():
        p = get_upload_path(upload_id)
        zip_path = Path(p) if p else zip_path
        if zip_path.is_dir():
            zips = list(zip_path.rglob("*.zip"))
            zip_path = zips[0] if zips else zip_path

    job.set_progress(5, "Extracting backup...")
    work = Path(tempfile.mkdtemp(prefix="pg-restore-work-", dir=str(UPLOAD_DIR)))
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            _safe_extract(zf, work)
        root = work
        current_env = _read_current_env()
        install_env_snapshot = current_env
        backup_env_path = _find_env(work)
        if backup_env_path:
            backup_env = backup_env_path.read_text(encoding="utf-8", errors="ignore")
        else:
            job.log("Backup has no .env — keeping live panel settings")
            backup_env = current_env
        # Prefer analyze() result (timescale manifest override); never trust live compose for backup label
        backup_db = analysis.get("backup_db") or detect_db_type_from_env(backup_env, prefer_compose=False)
        dump_path = resolve_backup_sql_dump(work, env_db=backup_db)
        if dump_path:
            try:
                rel = dump_path.relative_to(work)
            except ValueError:
                rel = dump_path.name
            job.log(f"Using dump file: {rel}")
        installed_db = detect_db_type_from_env(current_env) or get_pasarguard_db_type()

        job.log(f"Backup DB={backup_db}, installed DB={installed_db}, layout={analysis.get('layout')}")

        # Preserve CURRENT live credentials (password mismatch fix)
        cur_url = read_env_var(current_env, "SQLALCHEMY_DATABASE_URL")
        cur_db_pass = read_env_var(current_env, "DB_PASSWORD")
        cur_mysql_root = read_env_var(current_env, "MYSQL_ROOT_PASSWORD")
        cur_user = read_env_var(current_env, "DB_USER")
        cur_name = read_env_var(current_env, "DB_NAME")
        cur_pg_pass = read_env_var(current_env, "POSTGRES_PASSWORD") or cur_db_pass
        job.add_secret(cur_db_pass, cur_mysql_root, cur_pg_pass)

        # Stage archive into official backup dir for traceability
        PASARGUARD_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        staged = PASARGUARD_BACKUP_DIR / f"pgclockmg_restore_{job.job_id}.zip"
        shutil.copy2(zip_path, staged)
        job.log(f"Staged backup at {staged}")

        # Baseline expectations from backup files (before any wipe/convert)
        expected_counts: dict[str, int] = dict(analysis.get("table_counts") or {})
        if not expected_counts:
            expected_counts = _estimate_backup_table_counts(root, analysis.get("layout"))
        if expected_counts:
            job.log(
                "Backup expected counts: "
                + ", ".join(f"{k}={v}" for k, v in list(expected_counts.items())[:10])
            )
        else:
            job.log("Backup expected counts: unavailable — will require non-empty panel after restore")

        # TimescaleDB version alignment ONLY when restoring into a live PG-family engine.
        # Hard convert (timescale → mysql) must not run psql against the mysql container.
        ts_versions = analysis.get("timescaledb_versions") or []
        wanted_ts = parse_timescale_wanted(ts_versions)
        catalog_era = analysis.get("timescaledb_chunk_catalog")
        if not catalog_era and backup_db in ("timescaledb", "postgresql"):
            catalog_era = detect_backup_chunk_catalog_era(root)
            if catalog_era:
                analysis["timescaledb_chunk_catalog"] = catalog_era
                job.log(f"Detected Timescale chunk catalog era from dump: {catalog_era}")
        ts_min_version = analysis.get("timescaledb_min_version")
        if not ts_min_version and backup_db in ("timescaledb", "postgresql"):
            ts_min_version = detect_backup_ts_catalog_floor(root)
            if ts_min_version:
                analysis["timescaledb_min_version"] = ts_min_version
        if ts_min_version:
            job.log(f"Backup catalog requires TimescaleDB >= {ts_min_version}")
        if installed_db in ("timescaledb", "postgresql"):
            container = await _detect_db_container(job, installed_db)
            live_ver = None
            if container:
                live_ver = await _read_timescaledb_version(
                    job, container,
                    cur_pg_pass or "",
                    user=cur_user or "postgres",
                )
            # Single-layout dumps often lack manifest versions — infer from chunk catalog era
            # so pre-2.29 backups are not restored into timescale/timescaledb:latest (2.29+).
            align_to = resolve_wanted_ts_for_live(
                wanted_ts, live_ver=live_ver, catalog_era=catalog_era,
                # Plain PostgreSQL installs have no timescaledb image to pin — that
                # path strips Timescale DDL from the dump instead.
                min_ver=ts_min_version if installed_db == "timescaledb" else None,
            )
            if align_to and live_ver and live_ver != align_to:
                job.log(
                    f"TimescaleDB mismatch: live={live_ver} backup={align_to}"
                    + (f" (catalog={catalog_era})" if catalog_era else "")
                    + (f" (min={ts_min_version})" if ts_min_version else "")
                )
                await _align_timescaledb_image(job, align_to, wipe_data=True)
            elif align_to and not live_ver and installed_db == "timescaledb":
                job.log(
                    f"Could not probe live TimescaleDB — pinning image to backup version {align_to}"
                )
                await _align_timescaledb_image(job, align_to, wipe_data=True)
            elif (
                catalog_era == "schema_name"
                and live_ver
                and ts_version_ge(live_ver, TS_FIRST_RELID_CHUNK)
                and not align_to
            ):
                # Defensive: fingerprint says old dump, live is 2.29+
                job.log(
                    f"TimescaleDB catalog era mismatch: dump has schema_name, live={live_ver} — "
                    f"pinning to {TS_LAST_SCHEMA_NAME_CHUNK}"
                )
                await _align_timescaledb_image(job, TS_LAST_SCHEMA_NAME_CHUNK, wipe_data=True)

        # Destination = installed panel DB. Soft-family (mysql↔mariadb, pg↔timescale) needs no convert.
        target_db = installed_db or params.get("target_db") or backup_db
        from app.panels import can_convert_databases
        if (
            backup_db and target_db
            and backup_db != target_db
            and not soft_db_family(backup_db, target_db)
            and not can_convert_databases(backup_db, target_db)
        ):
            raise RuntimeError(
                f"Unsupported cross-DB conversion: {backup_db} → {target_db}. "
                "Non-SQLite backups cannot restore into SQLite — PasarGuard must already use a server DB."
            )
        needs_convert = bool(
            backup_db and target_db
            and backup_db != target_db
            and not soft_db_family(backup_db, target_db)
        )
        if needs_convert:
            job.log(f"DB mismatch — will auto-convert {backup_db} → {target_db}")

        # Backup passwords (same-engine restore must put OLD password into new .env)
        bak_db_pass = read_env_var(backup_env, "DB_PASSWORD")
        bak_mysql_root = read_env_var(backup_env, "MYSQL_ROOT_PASSWORD")
        bak_pg_pass = read_env_var(backup_env, "POSTGRES_PASSWORD") or bak_db_pass
        bak_user = read_env_var(backup_env, "DB_USER") or read_env_var(backup_env, "POSTGRES_USER")
        bak_name = read_env_var(backup_env, "DB_NAME") or read_env_var(backup_env, "POSTGRES_DB")
        bak_url = read_env_var(backup_env, "SQLALCHEMY_DATABASE_URL")
        job.add_secret(bak_db_pass, bak_mysql_root, bak_pg_pass)

        job.set_progress(40, "Restoring database...")
        from app.services.multiworker_stack import stop_panel_stack

        await stop_panel_stack(job)

        restore_engine = backup_db
        if backup_db == "sqlite" or analysis.get("layout") == "sqlite_file":
            restore_engine = "sqlite"
            await _restore_sqlite(job, root)
            expected_counts = _snapshot_sqlite_counts(PASARGUARD_DATA / "db.sqlite3") or expected_counts
            if expected_counts:
                job.log(
                    "Backup SQLite counts: "
                    + ", ".join(f"{k}={v}" for k, v in expected_counts.items())
                )
        elif backup_db in ("mysql", "mariadb"):
            if needs_convert:
                dump = dump_path
                if not dump or not dump.exists():
                    raise RuntimeError("SQL dump missing — cannot convert without dump")
                job.log(
                    f"Hard convert path: skip native {backup_db} container restore; "
                    f"will import dump → {target_db}"
                )
            else:
                # Soft family / same engine: always restore into the INSTALLED service
                restore_into = installed_db if soft_db_family(backup_db, installed_db) else backup_db
                await _restore_mysql(
                    job, root, restore_into or backup_db, current_env, backup_env,
                    dump=dump_path,
                )
                # Same-engine: force MySQL roles to backup password (written into .env next)
                sync_pass = bak_db_pass or bak_mysql_root or ""
                svc = await _detect_db_container(job, restore_into or installed_db or backup_db)
                if svc and sync_pass:
                    await _sync_mysql_passwords(
                        job, svc, sync_pass,
                        user=bak_user or cur_user or "pasarguard",
                        db_type=restore_into or installed_db or backup_db,
                        db_name=bak_name or cur_name or "pasarguard",
                        auth_passwords=[
                            p for p in (
                                bak_mysql_root, bak_db_pass,
                                cur_mysql_root, cur_db_pass,
                            ) if p
                        ],
                    )
        elif backup_db in ("postgresql", "timescaledb"):
            if needs_convert:
                dump = dump_path
                if not dump or not dump.exists():
                    if analysis.get("layout") != "multi":
                        raise RuntimeError("PostgreSQL dump missing — cannot convert without dump")
                job.log(
                    f"Hard convert path: skip native {backup_db} restore into foreign engine; "
                    f"will import dump → {target_db}"
                )
            else:
                # Soft family / same engine: always restore into the INSTALLED service
                restore_into = installed_db if soft_db_family(backup_db, installed_db) else backup_db
                await _restore_postgres(
                    job, root, restore_into or backup_db, current_env, backup_env, analysis,
                    dump=dump_path,
                )
                svc = await _detect_db_container(job, restore_into or installed_db or backup_db)
                # Same-engine: sync roles to BACKUP password (globals.sql restores old secrets)
                sync_pass = bak_pg_pass or bak_db_pass or ""
                if svc and sync_pass:
                    await _sync_pg_role_passwords(
                        job, svc, sync_pass,
                        bak_user or cur_user or "postgres",
                        bak_name or cur_name or "pasarguard",
                    )
        else:
            raise RuntimeError(f"Unsupported backup database: {backup_db}")

        if not restore_engine:
            raise RuntimeError(f"Unsupported backup database: {backup_db}")

        job.set_progress(75, "Merging configuration...")
        if needs_convert:
            # Hard convert into already-installed target — keep install credentials only
            preserve = {
                "DB_PASSWORD": cur_db_pass,
                "DB_USER": cur_user,
                "DB_NAME": cur_name,
            }
            if (target_db or "") in ("mysql", "mariadb"):
                preserve["MYSQL_ROOT_PASSWORD"] = cur_mysql_root or cur_db_pass
            elif (target_db or "") in ("postgresql", "timescaledb"):
                preserve["POSTGRES_PASSWORD"] = cur_pg_pass or cur_db_pass
        else:
            # Same / soft-family engine: put OLD (backup) DB password into the new .env
            # so panel auth matches roles restored from the dump.
            same_pass = bak_db_pass or bak_pg_pass or bak_mysql_root or ""
            same_root = bak_mysql_root or same_pass or ""
            same_pg = bak_pg_pass or bak_db_pass or same_pass or ""
            if not same_pass and (backup_db or "") != "sqlite" and (target_db or "") != "sqlite":
                raise RuntimeError(
                    "Same-engine restore needs a database password in the backup .env "
                    "(DB_PASSWORD / POSTGRES_PASSWORD / MYSQL_ROOT_PASSWORD)."
                )
            preserve = {
                "DB_PASSWORD": same_pass,
                "DB_USER": bak_user or cur_user,
                "DB_NAME": bak_name or cur_name,
            }
            family_eng = (target_db or backup_db or "").lower()
            if family_eng in ("mysql", "mariadb"):
                preserve["MYSQL_ROOT_PASSWORD"] = same_root
            elif family_eng in ("postgresql", "timescaledb"):
                preserve["POSTGRES_PASSWORD"] = same_pg
            job.log(
                "Same-engine restore: writing backup DB password into live .env "
                "(avoids auth mismatch when dump/globals restored old roles)"
            )
            # Keep install URL host/port layout but swap password to backup secret
            if cur_url and same_pass:
                from app.services.env_migration import _replace_sqlalchemy_password
                preserve["SQLALCHEMY_DATABASE_URL"] = _replace_sqlalchemy_password(cur_url, same_pass)
            elif cur_url:
                preserve["SQLALCHEMY_DATABASE_URL"] = cur_url
            elif bak_url:
                preserve["SQLALCHEMY_DATABASE_URL"] = bak_url

        await _merge_env_after_restore(
            job, backup_env, install_env_snapshot,
            preserve=preserve,
            target_db=target_db or backup_db,
        )

        await _restore_data_files(job, root)

        # Source path for cross-DB convert (must exist before workdir cleanup)
        convert_source: str | None = None
        if restore_engine == "sqlite" or analysis.get("layout") == "sqlite_file":
            convert_source = str(PASARGUARD_DATA / "db.sqlite3")
        else:
            dump = dump_path or resolve_backup_sql_dump(root, env_db=backup_db)
            if dump and dump.exists():
                convert_source = str(dump)
            elif analysis.get("layout") == "multi":
                # Prefer the application DB dump (skip globals.sql which has roles only)
                manifest = root / "pg_dump" / "manifest.tsv"
                candidates: list[Path] = []
                if manifest.exists():
                    for line in manifest.read_text(encoding="utf-8", errors="ignore").splitlines():
                        parts = line.split("\t")
                        if len(parts) >= 4:
                            cand = root / "pg_dump" / safe_upload_name(parts[3])
                            if cand.exists():
                                candidates.append(cand)
                if not candidates and (root / "pg_dump").is_dir():
                    candidates = sorted((root / "pg_dump").glob("*.sql"))
                for cand in candidates:
                    if cand.name.lower() in ("globals.sql", "roles.sql"):
                        continue
                    convert_source = str(cand)
                    break
                if not convert_source and candidates:
                    convert_source = str(candidates[0])
                if convert_source:
                    job.log(f"Hard convert source dump: {Path(convert_source).name}")

        final_db = restore_engine
        copy_stats: dict = {}
        copy_report: dict = {}
        if target_db and restore_engine != target_db and not soft_db_family(restore_engine, target_db):
            final_db, copy_stats, copy_report = await _maybe_cross_db_after_restore(
                job, params, restore_engine, target_db,
                cur_mysql_root or cur_db_pass or cur_pg_pass or "",
                cur_user or ("root" if (target_db or "") in ("mysql", "mariadb") else "pasarguard"),
                cur_name or "pasarguard",
                source_path=convert_source,
                install_env_snapshot=install_env_snapshot,
            )
            src_from_copy = (copy_report or {}).get("source_counts") or {}
            if src_from_copy:
                from app.services.native_migration.copy_core import VERIFY_TABLES

                expected_counts = {
                    k: v
                    for k, v in src_from_copy.items()
                    if isinstance(v, int) and v > 0 and k in VERIFY_TABLES
                }
            soft_gaps = (copy_report or {}).get("soft_incomplete") or []
            if soft_gaps:
                job.log(
                    "Non-critical tables partially skipped (orphans/history OK): "
                    + ", ".join(
                        f"{i['table']} {i['copied']}/{i['source']}" for i in soft_gaps[:8]
                    )
                )
            if copy_report.get("has_gaps"):
                crit = copy_report.get("critical_incomplete") or copy_report.get("incomplete") or []
                raise RuntimeError(
                    "Migration incomplete — critical tables were not fully copied:\n"
                    + ", ".join(
                        f"{i.get('table')} {i.get('copied')}/{i.get('source')}" for i in crit
                    )
                )
        elif target_db and soft_db_family(restore_engine, target_db):
            final_db = target_db

        # After convert / same-engine: credentials must match what we wrote into .env
        final_engine_pre = final_db or target_db or restore_engine or backup_db
        if not final_engine_pre:
            raise RuntimeError(
                "Could not determine the target database engine after restore"
            )
        live_admin = (copy_report or {}).get("live_admin") or {}
        if needs_convert:
            verify_user = cur_user or live_admin.get("user") or "pasarguard"
            verify_db = cur_name or live_admin.get("database") or "pasarguard"
            if (final_engine_pre or "") in ("mysql", "mariadb"):
                verify_pass = (
                    cur_mysql_root or cur_db_pass or live_admin.get("password") or ""
                )
                if not verify_user or verify_user == "pasarguard":
                    # MySQL convert auth uses root more often than app user
                    verify_user = cur_user or "root"
            else:
                # Prefer POSTGRES_PASSWORD (cur_pg_pass already falls back to DB_PASSWORD)
                verify_pass = cur_pg_pass or live_admin.get("password") or ""
        else:
            verify_user = bak_user or cur_user or live_admin.get("user") or "pasarguard"
            verify_pass = (
                bak_db_pass or bak_pg_pass or bak_mysql_root
                or live_admin.get("password") or ""
            )
            verify_db = bak_name or cur_name or live_admin.get("database") or "pasarguard"
        if (
            final_engine_pre in ("postgresql", "timescaledb")
            and verify_pass
        ):
            svc = await _detect_db_container(job, final_engine_pre)
            if svc:
                job.log(
                    f"Aligning DB roles to {'install' if needs_convert else 'backup'} password "
                    f"(user={verify_user}) so .env and Timescale/Postgres match"
                )
                await _sync_pg_role_passwords(
                    job, svc, verify_pass, verify_user, verify_db,
                )
        elif final_engine_pre in ("mysql", "mariadb") and verify_pass:
            svc = await _detect_db_container(job, final_engine_pre)
            if svc:
                job.log(
                    f"Aligning MySQL roles to {'install' if needs_convert else 'backup'} password "
                    f"(user={verify_user})"
                )
                await _sync_mysql_passwords(
                    job, svc, verify_pass,
                    user=verify_user or "pasarguard",
                    db_type=final_engine_pre,
                    db_name=verify_db or "pasarguard",
                    auth_passwords=[
                        p for p in (
                            cur_mysql_root, cur_db_pass,
                            bak_mysql_root, bak_db_pass,
                            live_admin.get("password"),
                        ) if p
                    ],
                )

        job.set_progress(88, "Finalizing .env for target database...")
        await _finalize_env_after_restore(
            job,
            install_env_snapshot,
            final_engine_pre,
            verify_pass,
            verify_user,
            verify_db,
        )
        _env_completeness_checklist(
            job,
            final_engine_pre,
            backup_env,
        )

        if final_db and final_db != "sqlite" and (needs_convert or restore_engine == "sqlite"):
            _relocate_sqlite_after_convert(job)

        job.set_progress(90, "Starting PasarGuard...")
        final_engine = final_db or installed_db or backup_db or final_engine_pre
        mini_params: dict = {
            "target_db": final_engine,
            "target_db_password": verify_pass,
            "target_db_user": verify_user,
            "target_db_name": verify_db,
            "target_db_host": "127.0.0.1",
            "_auto_db_credentials": True,
        }
        if final_engine in ("postgresql", "timescaledb"):
            mini_params["_resolved_target_conn"] = {
                "user": verify_user,
                "password": verify_pass,
                "database": verify_db,
                "host": "127.0.0.1",
                "port": "5432",
                "db_type": final_engine,
            }
        mini = _RestoreMini(job, mini_params)

        # Timescale stuck in restoring=on → panel dies after alembic Context with
        # almost no error (seen on timescaledb→timescaledb same-engine restores).
        if (final_db or restore_engine or "") in ("timescaledb", "postgresql"):
            await _ensure_timescaledb_not_in_restore_mode(
                job, verify_pass, verify_user, verify_db,
            )

        # Same-engine / soft-family: run one-shot alembic WHILE the panel is down.
        # Starting the panel first caused dual alembic (all-in-one + one-shot) on
        # the same DB — Timescale/PG restores hung or the panel vanished mid-Context.
        # Convert path already aligned schema to head — skip re-sync there.
        if should_sync_alembic_before_panel_boot(needs_convert):
            job.log("Stopping panel stack before one-shot alembic sync (avoid dual migrate)…")
            from app.services.multiworker_stack import stop_panel_stack

            await stop_panel_stack(job)
            try:
                from app.services.pasarguard_ops import sync_alembic_for_startup

                await sync_alembic_for_startup(mini, final_engine)
            except Exception as e:
                job.log(f"Alembic sync note: {e}")
            if final_engine in ("timescaledb", "postgresql"):
                await _ensure_timescaledb_not_in_restore_mode(
                    job, verify_pass, verify_user, verify_db,
                )
        else:
            job.log("Skipping full alembic re-sync after convert (schema already at head)")

        # Force recreate so panel picks up finalized .env (DB URL / SSL / NATS).
        # Multi-worker stacks need NATS ready before panel workers boot.
        from app.services.multiworker_stack import start_panel_stack

        ok, out = await start_panel_stack(job, force_recreate=True)
        if not ok:
            job.log(f"compose recreate warning: {out[-1500:]}")
            mismatch = detect_ts_mismatch_from_text(out)
            if mismatch:
                job.log(
                    f"Timescale mismatch noted ({mismatch[0]} vs {mismatch[1]}) — "
                    "retag only, no data wipe after restore"
                )
                await _align_timescaledb_image(job, mismatch[0], wipe_data=False)
                ok, out = await start_panel_stack(job, force_recreate=True)
            if not ok:
                raise RuntimeError(
                    "PasarGuard failed to start after restore (force-recreate):\n"
                    f"{out[-2000:]}"
                )
        from app.services.multiworker_stack import detect_multiworker_stack

        boot_wait = 20 if detect_multiworker_stack().get("orchestrate") else 8
        await asyncio.sleep(boot_wait)

        await _heal_panel_auth_if_needed(
            job,
            verify_pass,
            verify_user,
            verify_db,
            final_db or restore_engine or "",
        )

        # Re-read password from finalized .env in case finalize adjusted it
        env_now = _read_current_env()
        if final_engine in ("mysql", "mariadb"):
            verify_pass = (
                read_env_var(env_now, "DB_PASSWORD")
                or read_env_var(env_now, "MYSQL_ROOT_PASSWORD")
                or read_env_var(env_now, "MYSQL_PASSWORD")
                or verify_pass
            )
        else:
            verify_pass = (
                read_env_var(env_now, "POSTGRES_PASSWORD")
                or read_env_var(env_now, "DB_PASSWORD")
                or verify_pass
            )
        verify_user = read_env_var(env_now, "DB_USER") or verify_user
        verify_db = read_env_var(env_now, "DB_NAME") or verify_db

        verified = await _verify_restored_data(
            job,
            final_engine,
            verify_pass,
            verify_user,
            verify_db,
            expected_counts,
            # Always require panel data when we cannot estimate backup rows
            # (matches the log line above) — env/certs alone are not success.
            require_any_data=True,
        )

        if params.get("disable_nodes_after_restore"):
            await _disable_nodes_after_restore(
                job, final_engine, verify_pass, verify_user, verify_db,
            )

        from app.services.pasarguard_ops import verify_pasarguard_healthy

        await verify_pasarguard_healthy(mini)

        access = get_panel_access_info()
        access["nodes_disabled"] = bool(params.get("disable_nodes_after_restore"))
        access["restored"] = True
        access["backup_db"] = backup_db
        access["final_db"] = final_db
        access["staged_backup"] = str(staged)
        access["auto_db_convert"] = bool(
            backup_db and final_db and backup_db != final_db and not soft_db_family(backup_db, final_db)
        )
        access["copy_stats"] = copy_stats or verified
        access["copy_report"] = copy_report
        access["verified_counts"] = verified
        return access
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def _disable_nodes_after_restore(
    job: MigrationJob,
    db_type: str,
    password: str,
    user: str,
    db_name: str,
) -> None:
    """Set all nodes to disabled state after restore so the operator can enable them manually.

    PasarGuard uses a ``status`` TEXT column on the ``nodes`` table.
    Known status values: 'healthy', 'unhealthy', 'disabled'.
    We set every non-disabled node to 'disabled'.
    """
    job.log("Disabling all nodes as requested (nodes_disabled_after_restore)...")

    if db_type in ("postgresql", "timescaledb"):
        svc = await _detect_db_container(job, db_type)
        if not svc:
            job.log("Could not detect DB container — skipping node disable")
            return
        sql = "UPDATE nodes SET status = 'disabled' WHERE status != 'disabled';"
        ok, out = await _run(
            job,
            [
                "docker", "compose", "exec", "-T",
                "-e", f"PGPASSWORD={password}",
                svc, "psql", "-U", user, "-d", db_name,
                "-v", "ON_ERROR_STOP=0", "-c", sql,
            ],
            cwd=str(PASARGUARD_DIR),
            timeout=30,
        )
        if ok:
            job.log("All nodes set to disabled (PostgreSQL/TimescaleDB)")
        else:
            job.log(f"Node disable warning: {(out or '')[-300:]}")

    elif db_type in ("mysql", "mariadb"):
        svc = await _detect_db_container(job, db_type)
        if not svc:
            job.log("Could not detect DB container — skipping node disable")
            return
        sql = "UPDATE nodes SET status = 'disabled' WHERE status != 'disabled';"
        last_out = ""
        for bin_name in _mysql_client_bins(db_type, svc):
            ok, out = await _run(
                job,
                [
                    "docker", "compose", "exec", "-T",
                    "-e", f"MYSQL_PWD={password}",
                    svc, bin_name, "-u", user,
                    "-D", db_name, "-e", sql,
                ],
                cwd=str(PASARGUARD_DIR),
                timeout=30,
            )
            if ok:
                job.log(f"All nodes set to disabled (MySQL/MariaDB via {bin_name})")
                return
            last_out = out or last_out
        job.log(f"Node disable warning: {last_out[-300:]}")

    elif db_type == "sqlite":
        sqlite_path = PASARGUARD_DATA / "db.sqlite3"
        if not sqlite_path.exists():
            job.log("SQLite file not found — skipping node disable")
            return
        try:
            import sqlite3 as _sqlite3
            conn = _sqlite3.connect(str(sqlite_path))
            cur = conn.cursor()
            cur.execute("UPDATE nodes SET status = 'disabled' WHERE status != 'disabled'")
            updated = cur.rowcount
            conn.commit()
            conn.close()
            job.log(f"All nodes set to disabled (SQLite, {updated} rows updated)")
        except Exception as e:
            job.log(f"Node disable warning (SQLite): {e}")

    else:
        job.log(f"Node disable skipped — unsupported db_type: {db_type}")


async def _restore_sqlite(job: MigrationJob, root: Path) -> None:
    src = resolve_backup_sqlite(root, env_db="sqlite")
    if not src or not src.exists():
        src = root / "db.sqlite3"
        if not src.exists():
            found = list(root.rglob("db.sqlite3"))
            src = found[0] if found else None
    if not src or not src.exists():
        raise RuntimeError("SQLite database not found in backup")
    PASARGUARD_DATA.mkdir(parents=True, exist_ok=True)
    dest = PASARGUARD_DATA / "db.sqlite3"
    if dest.exists():
        shutil.copy2(dest, dest.with_suffix(".sqlite3.bak-before-restore"))
    shutil.copy2(src, dest)
    job.log(f"SQLite restored from {src.name} → {dest}")


async def _restore_mysql(
    job: MigrationJob,
    root: Path,
    db_type: str,
    current_env: str,
    backup_env: str,
    dump: Path | None = None,
) -> None:
    dump = dump or resolve_backup_sql_dump(root, env_db=db_type)
    if not dump or not dump.exists():
        raise RuntimeError("SQL dump missing")
    svc = await _detect_db_container(job, db_type)
    if not svc:
        raise RuntimeError("MySQL/MariaDB container not found")

    # Prefer backup secrets for dump restore (roles inside dump match backup passwords),
    # then fall back to live install credentials.
    root_pw = (
        read_env_var(backup_env, "MYSQL_ROOT_PASSWORD")
        or read_env_var(current_env, "MYSQL_ROOT_PASSWORD")
    )
    db_user = (
        read_env_var(backup_env, "DB_USER")
        or read_env_var(current_env, "DB_USER")
        or "root"
    )
    db_pass = (
        read_env_var(backup_env, "DB_PASSWORD")
        or read_env_var(current_env, "DB_PASSWORD")
    )
    db_name = (
        read_env_var(backup_env, "DB_NAME")
        or read_env_var(current_env, "DB_NAME")
        or "pasarguard"
    )
    client_bins = _mysql_client_bins(db_type, svc)

    await _compose(job, "up", "-d", svc, timeout=180)
    await asyncio.sleep(5)

    attempts = []
    if root_pw:
        attempts.append(("root", root_pw, None))
    if db_user and db_pass:
        attempts.append((db_user, db_pass, db_name))
        attempts.append((db_user, db_pass, None))
    # also try install passwords if different from backup
    c_root = read_env_var(current_env, "MYSQL_ROOT_PASSWORD")
    c_pass = read_env_var(current_env, "DB_PASSWORD")
    if c_root and c_root != root_pw:
        attempts.append(("root", c_root, None))
    if c_pass and c_pass != db_pass:
        attempts.append((db_user, c_pass, db_name))

    last_err = ""
    for user, pwd, db in attempts:
        for mysql_cmd in client_bins:
            cmd = [
                "docker", "compose", "exec", "-T",
                "-e", f"MYSQL_PWD={pwd}", svc, mysql_cmd, "-u", user,
            ]
            if db:
                cmd.append(db)
            job.log(f"Trying MySQL restore as {user}" + (f"/{db}" if db else "") + f" ({mysql_cmd})")
            with open(dump, "rb") as dump_fh:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(PASARGUARD_DIR),
                    stdin=dump_fh,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                out_b, _ = await proc.communicate()
            out = (out_b or b"").decode("utf-8", errors="replace")
            if proc.returncode == 0:
                job.log("MySQL/MariaDB dump restored")
                return
            last_err = out[-1500:]
            job.log(f"Attempt failed: {last_err[:300]}")
    raise RuntimeError(f"MySQL restore failed after password attempts:\n{last_err}")


async def _restore_postgres(
    job: MigrationJob,
    root: Path,
    db_type: str,
    current_env: str,
    backup_env: str,
    analysis: dict,
    dump: Path | None = None,
) -> None:
    """Restore PG/Timescale dump into the live installed service (db_type = restore-into engine)."""
    svc = await _detect_db_container(job, db_type)
    if not svc:
        # Fallbacks: timescaledb installs often still answer as timescaledb service
        for cand in ("timescaledb", "postgresql"):
            probed = await _detect_db_container(job, cand)
            if probed:
                svc = probed
                break
    if not svc:
        svc = "timescaledb" if db_type == "timescaledb" else "postgresql"
    job.log(f"PostgreSQL restore into service `{svc}` (engine={db_type})")
    await _compose_up_services(job, svc, "pgbouncer", timeout=180)

    password = (
        read_env_var(current_env, "DB_PASSWORD")
        or read_env_var(current_env, "POSTGRES_PASSWORD")
        or read_env_var(backup_env, "DB_PASSWORD")
        or read_env_var(backup_env, "POSTGRES_PASSWORD")
        or ""
    )
    user = (
        read_env_var(current_env, "DB_USER")
        or read_env_var(current_env, "POSTGRES_USER")
        or read_env_var(backup_env, "DB_USER")
        or read_env_var(backup_env, "POSTGRES_USER")
        or "pasarguard"
    )
    db_name = (
        read_env_var(current_env, "DB_NAME")
        or read_env_var(current_env, "POSTGRES_DB")
        or read_env_var(backup_env, "DB_NAME")
        or read_env_var(backup_env, "POSTGRES_DB")
        or "pasarguard"
    )

    if not password:
        raise RuntimeError("No database password available for PostgreSQL restore")

    # Collect all candidate passwords and pick whichever the live container accepts.
    # After a fresh wipe the container initialises with POSTGRES_PASSWORD from compose env;
    # that value may differ from DB_PASSWORD. Also try container POSTGRES_* and socket trust.
    from app.services.db_auth import build_postgres_auth_attempts, postgres_password_candidates

    container_env = await _read_pg_container_init_env(job, svc)
    password_candidates = list(dict.fromkeys(filter(None, [
        password,
        container_env.get("POSTGRES_PASSWORD"),
        *postgres_password_candidates(current_env),
        *postgres_password_candidates(backup_env),
    ])))
    auth_attempts = build_postgres_auth_attempts(
        current_env or backup_env,
        preferred_user=user,
        preferred_password=password,
        extra_users=(
            read_env_var(backup_env, "DB_USER"),
            read_env_var(backup_env, "POSTGRES_USER"),
            db_name,
            container_env.get("POSTGRES_USER"),
        ),
        extra_passwords=tuple(password_candidates),
        container_user=container_env.get("POSTGRES_USER"),
        container_password=container_env.get("POSTGRES_PASSWORD"),
        include_trust=True,
    )

    effective_password = password
    effective_user = user
    pg_ready = False
    for auth_user, auth_pwd in auth_attempts:
        # Readiness helper requires a password string; for trust attempts use ""
        # and also try a direct socket SELECT without PGPASSWORD below.
        if auth_pwd is None:
            ok_trust, out_trust = await _psql_in_db_container(
                job, svc,
                user=auth_user,
                password=None,
                database="postgres",
                sql="SELECT 1;",
                timeout=20,
            )
            if _psql_exec_succeeded(ok_trust, out_trust):
                effective_user = auth_user
                # Keep a real password for later PGPASSWORD-based dump pipes
                effective_password = (
                    password
                    or container_env.get("POSTGRES_PASSWORD")
                    or next((p for p in password_candidates if p), "")
                )
                pg_ready = True
                job.log(f"PostgreSQL ready via local trust as {auth_user}")
                break
            continue
        if await _wait_for_postgres_ready(job, svc, auth_pwd, user=auth_user, timeout=45):
            effective_password = auth_pwd
            effective_user = auth_user
            pg_ready = True
            if auth_pwd != password or auth_user != user:
                job.log(
                    f"PostgreSQL accepted auth with user={auth_user} "
                    f"(credentials differ from current .env) — adjusting for restore"
                )
            break

    if not pg_ready:
        raise RuntimeError(f"PostgreSQL service `{svc}` did not become ready before restore")

    # Use the verified credentials for the rest of this restore session
    password = effective_password
    user = effective_user

    async def psql(
        sql: str,
        db: str = "postgres",
        use_file: Path | None = None,
        *,
        on_error_stop: bool = True,
    ) -> tuple[bool, str]:
        stop = "ON_ERROR_STOP=1" if on_error_stop else "ON_ERROR_STOP=0"
        cmd = [
            "docker", "compose", "exec", "-T",
            "-e", f"PGPASSWORD={password}",
            svc, "psql", "-v", stop, "-U", user, "-d", db,
        ]
        if use_file:
            with open(use_file, "rb") as sql_fh:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(PASARGUARD_DIR),
                    stdin=sql_fh,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                out_b, _ = await proc.communicate()
            return proc.returncode == 0, (out_b or b"").decode("utf-8", errors="replace")
        proc = await asyncio.create_subprocess_exec(
            *cmd, "-c", sql,
            cwd=str(PASARGUARD_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out_b, _ = await proc.communicate()
        return proc.returncode == 0, (out_b or b"").decode("utf-8", errors="replace")

    async def verify_app_tables(dbn: str) -> tuple[bool, str]:
        """After tolerant dump import, require core PasarGuard tables to exist."""
        ok, out = await psql(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE' "
            "AND table_name IN ('users','admins','hosts','inbounds','nodes','groups');",
            db=dbn,
        )
        if not ok:
            return False, out
        n = 0
        for line in (out or "").splitlines():
            if line.strip().isdigit():
                n = int(line.strip())
                break
        return n >= 3, f"core_tables={n}"

    async def restore_dump_file(dbn: str, path: Path, *, tolerant: bool) -> tuple[bool, str]:
        ok, out = await psql("", db=dbn, use_file=path, on_error_stop=not tolerant)
        if tolerant:
            # Non-zero is OK if leftover Timescale noise failed — verify panel tables
            verified, detail = await verify_app_tables(dbn)
            if verified:
                errs = extract_psql_errors(out)
                if errs:
                    job.log(f"Dump import had non-fatal errors (ignored):\n{errs[:600]}")
                job.log(f"Verified app schema after tolerant restore ({detail})")
                return True, out
            return False, (
                f"Tolerant restore did not create core tables ({detail}).\n"
                f"{extract_psql_errors(out)}"
            )
        if not ok:
            return False, extract_psql_errors(out) or out
        return True, out

    layout = analysis.get("layout")
    manifest = root / "pg_dump" / "manifest.tsv"
    restored_any = False
    backup_has_ts = (
        bool(analysis.get("timescaledb_versions"))
        or (analysis.get("backup_db") == "timescaledb")
        or (analysis.get("db_type") == "timescaledb")
    )
    # Only use Timescale restore helpers if the TARGET service actually has the extension.
    # Soft-family timescaledb→postgresql must strip TS DDL — plain PG has no timescaledb.
    target_has_ts = False
    if db_type == "timescaledb" or (svc or "").lower() == "timescaledb":
        target_has_ts = True
    else:
        probed = await _read_timescaledb_version(job, svc, password, user=user)
        target_has_ts = bool(probed)
    use_timescale = target_has_ts
    strip_for_plain_pg = backup_has_ts and not target_has_ts
    if strip_for_plain_pg:
        job.log(
            "Backup is TimescaleDB but target is plain PostgreSQL — "
            "stripping timescaledb extension DDL and restoring as PostgreSQL"
        )

    if layout == "multi" and manifest.exists():
        globals_sql = root / "pg_dump" / "globals.sql"
        if globals_sql.exists():
            job.log("Restoring globals...")
            # Globals often include extension bits — never hard-fail the whole restore on them
            gtext = globals_sql.read_text(encoding="utf-8", errors="ignore")
            # Make CREATE ROLE idempotent so "role already exists" never aborts the restore
            gtext = filter_globals_sql(gtext)
            if strip_for_plain_pg:
                gtext = filter_timescaledb_extension_sql(gtext, strip_all=True)
            cmd = [
                "docker", "compose", "exec", "-T",
                "-e", f"PGPASSWORD={password}",
                svc, "psql", "-v", "ON_ERROR_STOP=0", "-U", user, "-d", "postgres",
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(PASARGUARD_DIR),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            await proc.communicate(input=gtext.encode("utf-8"))

        for line in manifest.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            dbn, owner, has_ts, filename = parts[0], parts[1], parts[2], parts[3]
            filename = safe_upload_name(filename)
            dump_path = root / "pg_dump" / filename
            if not dump_path.exists():
                raise RuntimeError(f"Missing dump file in backup: pg_dump/{filename}")
            # Skip role-only / globals-style dumps that aren't the app DB
            if filename.lower() in ("globals.sql", "roles.sql"):
                continue
            dbn = safe_pg_identifier(dbn, what="database name")
            owner_q = safe_pg_identifier(owner or user, what="database owner")
            job.log(f"Restoring database {dbn}...")
            await psql(
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = {_sql_literal(dbn)} AND pid <> pg_backend_pid();"
            )
            await psql(f'DROP DATABASE IF EXISTS "{dbn}";')
            ok, out = await psql(f'CREATE DATABASE "{dbn}" OWNER "{owner_q}";')
            if not ok:
                raise RuntimeError(f"CREATE DATABASE {dbn} failed:\n{out[-1000:]}")

            dump_wants_ts = (has_ts == "1") or backup_has_ts
            filtered: Path | None = None
            restore_file = dump_path

            if use_timescale and dump_wants_ts:
                ok_ext, out_ext = await psql(
                    "CREATE EXTENSION IF NOT EXISTS timescaledb;", db=dbn,
                )
                if not ok_ext:
                    raise RuntimeError(
                        f"Target cannot create timescaledb extension:\n"
                        f"{extract_psql_errors(out_ext)}"
                    )
                await psql("SELECT timescaledb_pre_restore();", db=dbn)
                filtered = filter_timescaledb_extension_sql_file(
                    dump_path, dump_path.with_suffix(dump_path.suffix + ".filtered"),
                )
                restore_file = filtered
                ok, out = await restore_dump_file(dbn, restore_file, tolerant=False)
                ok_post, out_post = await psql("SELECT timescaledb_post_restore();", db=dbn)
                if not ok_post:
                    job.log(f"timescaledb_post_restore warning: {extract_psql_errors(out_post)[:300]}")
                    # Retry once — extension may need a moment after dump load
                    await asyncio.sleep(3)
                    await psql("SELECT timescaledb_post_restore();", db=dbn)
            elif dump_wants_ts and not use_timescale:
                # Timescale backup → plain PostgreSQL (fallback if convert path not used)
                filtered = filter_timescaledb_extension_sql_file(
                    dump_path, dump_path.with_suffix(dump_path.suffix + ".pg-plain"),
                    strip_all=True,
                )
                restore_file = filtered
                ok, out = await restore_dump_file(dbn, restore_file, tolerant=True)
            else:
                ok, out = await restore_dump_file(dbn, dump_path, tolerant=False)

            if filtered and filtered.exists():
                try:
                    filtered.unlink()
                except OSError:
                    pass

            if not ok:
                # Align BEFORE retry — only when target actually has Timescale
                mismatch = detect_ts_mismatch_from_text(out)
                catalog_err = is_ts_catalog_mismatch_error(out or "")
                if use_timescale and (
                    mismatch
                    or catalog_err
                    or ("timescaledb" in (out or "").lower() and "version" in (out or "").lower())
                ):
                    wanted = wanted_ts_for_restore_retry(out or "", analysis) or ""
                    if wanted:
                        job.log(f"Timescale restore error — aligning to {wanted} and retrying {dbn}")
                        # _align_timescaledb_image already starts the container and
                        # waits for readiness — no extra sleep or compose up needed
                        await _align_timescaledb_image(job, wanted, wipe_data=True)
                        await psql(f'DROP DATABASE IF EXISTS "{dbn}";')
                        ok2, out2 = await psql(f'CREATE DATABASE "{dbn}" OWNER "{owner_q}";')
                        if not ok2:
                            raise RuntimeError(
                                f"CREATE DATABASE {dbn} failed after image align:\n{out2[-1000:]}"
                            )
                        await psql("CREATE EXTENSION IF NOT EXISTS timescaledb;", db=dbn)
                        await psql("SELECT timescaledb_pre_restore();", db=dbn)
                        filtered2 = filter_timescaledb_extension_sql_file(
                            dump_path, dump_path.with_suffix(dump_path.suffix + ".filtered"),
                        )
                        ok, out = await restore_dump_file(dbn, filtered2, tolerant=False)
                        ok_post2, out_post2 = await psql("SELECT timescaledb_post_restore();", db=dbn)
                        if not ok_post2:
                            job.log(f"timescaledb_post_restore (retry) warning: {extract_psql_errors(out_post2)[:300]}")
                            await asyncio.sleep(3)
                            await psql("SELECT timescaledb_post_restore();", db=dbn)
                if not ok and dump_wants_ts and not use_timescale:
                    job.log("Retrying Timescale→PG dump with tolerant import...")
                    filtered3 = filter_timescaledb_extension_sql_file(
                        dump_path, dump_path.with_suffix(dump_path.suffix + ".pg-plain-retry"),
                        strip_all=True,
                    )
                    ok, out = await restore_dump_file(dbn, filtered3, tolerant=True)
                if not ok:
                    raise RuntimeError(
                        f"Failed restoring {dbn}:\n{extract_psql_errors(out)}"
                    )
            restored_any = True
            job.log(f"Database {dbn} restored")
        if not restored_any:
            raise RuntimeError("Multi-dump restore finished with zero databases restored")
        return

    # Legacy / third-party single dump
    dump = dump or resolve_backup_sql_dump(root, env_db=analysis.get("backup_db") or db_type)
    if not dump or not dump.exists():
        raise RuntimeError("SQL dump missing")

    # PGClockBackup ships globals.sql at zip root (beside db_backup.sql).
    # Apply roles best-effort before the app dump — same idea as multi-layout.
    root_globals = root / "globals.sql"
    if root_globals.is_file():
        job.log("Restoring root globals.sql (roles)…")
        gtext = filter_globals_sql(
            root_globals.read_text(encoding="utf-8", errors="ignore")
        )
        if strip_for_plain_pg:
            gtext = filter_timescaledb_extension_sql(gtext, strip_all=True)
        cmd = [
            "docker", "compose", "exec", "-T",
            "-e", f"PGPASSWORD={password}",
            svc, "psql", "-v", "ON_ERROR_STOP=0", "-U", user, "-d", "postgres",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(PASARGUARD_DIR),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await proc.communicate(input=gtext.encode("utf-8"))

    await psql(
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid();"
    )
    await psql(f'DROP DATABASE IF EXISTS "{db_name}";')
    ok, out = await psql(f'CREATE DATABASE "{db_name}" OWNER "{user}";')
    if not ok:
        raise RuntimeError(f"CREATE DATABASE failed: {out[-1000:]}")
    if use_timescale and (backup_has_ts or db_type == "timescaledb"):
        ok_ext, out_ext = await psql("CREATE EXTENSION IF NOT EXISTS timescaledb;", db=db_name)
        if not ok_ext:
            raise RuntimeError(
                f"Target cannot create timescaledb extension:\n{extract_psql_errors(out_ext)}"
            )
        await psql("SELECT timescaledb_pre_restore();", db=db_name)
        filtered = filter_timescaledb_extension_sql_file(
            dump, root / "db_backup_filtered.sql",
        )
        ok, out = await restore_dump_file(db_name, filtered, tolerant=False)
        if not ok:
            wanted = wanted_ts_for_restore_retry(out or "", analysis) or ""
            if not wanted and is_ts_catalog_mismatch_error(out or ""):
                era = (
                    analysis.get("timescaledb_chunk_catalog")
                    or detect_backup_chunk_catalog_era(root)
                )
                wanted = infer_ts_version_from_catalog_era(era) or TS_LAST_SCHEMA_NAME_CHUNK
            if wanted:
                job.log(
                    f"Timescale single-dump restore error — aligning to {wanted} and retrying"
                )
                await _align_timescaledb_image(job, wanted, wipe_data=True)
                await psql(
                    f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid();"
                )
                await psql(f'DROP DATABASE IF EXISTS "{db_name}";')
                ok_db, out_db = await psql(f'CREATE DATABASE "{db_name}" OWNER "{user}";')
                if not ok_db:
                    raise RuntimeError(
                        f"CREATE DATABASE failed after image align:\n{out_db[-1000:]}"
                    )
                await psql("CREATE EXTENSION IF NOT EXISTS timescaledb;", db=db_name)
                await psql("SELECT timescaledb_pre_restore();", db=db_name)
                filter_timescaledb_extension_sql_file(dump, filtered)
                ok, out = await restore_dump_file(db_name, filtered, tolerant=False)
        ok_post_s, out_post_s = await psql("SELECT timescaledb_post_restore();", db=db_name)
        if not ok_post_s:
            job.log(f"timescaledb_post_restore (single) warning: {extract_psql_errors(out_post_s)[:300]}")
            await asyncio.sleep(3)
            await psql("SELECT timescaledb_post_restore();", db=db_name)
    elif backup_has_ts and not use_timescale:
        filtered = filter_timescaledb_extension_sql_file(
            dump, root / "db_backup_pg_plain.sql", strip_all=True,
        )
        ok, out = await restore_dump_file(db_name, filtered, tolerant=True)
    else:
        ok, out = await restore_dump_file(db_name, dump, tolerant=False)
    if not ok:
        raise RuntimeError(f"PostgreSQL dump restore failed:\n{extract_psql_errors(out)}")
    job.log(f"PostgreSQL dump restored into {db_name}")
    return


async def _merge_env_after_restore(
    job: MigrationJob,
    backup_env: str,
    current_env: str,
    preserve: dict,
    *,
    target_db: str | None = None,
) -> None:
    """Write backup .env (panel settings) but keep live DB credentials.

    App settings from backup win (ports, telegram, subscription) — this is the
    previous panel. Install only fills missing keys and provides DB auth.
    """
    from app.services.env_migration import (
        _set_sqlalchemy_url,
        _sqlalchemy_url_line_pattern,
        _unset_env_var,
    )
    import re as _re

    text = backup_env
    # Only fill panel listen settings if backup omitted them
    for key in ("UVICORN_PORT", "UVICORN_HOST", "UVICORN_ROOT_PATH", "DASHBOARD_PATH", "ALLOWED_ORIGINS"):
        if read_env_var(text, key):
            continue
        cur = read_env_var(current_env, key)
        if cur is not None:
            text = _set_env_var(text, key, cur)

    for key, val in preserve.items():
        if not val:
            continue
        if key == "SQLALCHEMY_DATABASE_URL":
            continue  # handled below — must collapse duplicates
        text = _set_env_var(text, key, val)

    db_pass = preserve.get("DB_PASSWORD") or preserve.get("POSTGRES_PASSWORD") or preserve.get(
        "MYSQL_ROOT_PASSWORD"
    )
    if db_pass:
        text = _set_env_var(text, "DB_PASSWORD", db_pass)
        # Only mirror into engine-specific secret keys that belong on the target
        tgt = (target_db or "").lower()
        if tgt in ("postgresql", "timescaledb") and (
            "POSTGRES_PASSWORD" in preserve
            or "POSTGRES_PASSWORD" in current_env
            or read_env_var(current_env, "POSTGRES_PASSWORD")
        ):
            text = _set_env_var(text, "POSTGRES_PASSWORD", db_pass)
        if tgt in ("mysql", "mariadb") and (
            "MYSQL_ROOT_PASSWORD" in preserve
            or read_env_var(current_env, "MYSQL_ROOT_PASSWORD")
        ):
            text = _set_env_var(text, "MYSQL_ROOT_PASSWORD", db_pass)

    # Strip foreign-engine secrets left over from a Timescale/Postgres backup
    # when converting into MySQL/MariaDB (and the reverse).
    tgt = (target_db or "").lower()
    if tgt in ("mysql", "mariadb"):
        for key in (
            "POSTGRES_PASSWORD",
            "POSTGRES_USER",
            "POSTGRES_DB",
            "POSTGRES_HOST",
            "POSTGRES_PORT",
        ):
            text = _unset_env_var(text, key)
    elif tgt in ("postgresql", "timescaledb"):
        for key in (
            "MYSQL_ROOT_PASSWORD",
            "MYSQL_PASSWORD",
            "MYSQL_USER",
            "MYSQL_DATABASE",
            "MYSQL_HOST",
            "MYSQL_PORT",
        ):
            text = _unset_env_var(text, key)

    # Always normalize SQLALCHEMY to a single line. Backup .env files sometimes
    # contain the same sqlite URL 2–3 times; docker last-wins would ignore a later
    # finalize that only rewrote the first line.
    preserved_url = preserve.get("SQLALCHEMY_DATABASE_URL")
    if preserved_url:
        text = _set_sqlalchemy_url(text, str(preserved_url))
    else:
        # Hard convert path: strip backup engine URLs; finalize writes the target URL.
        text = _re.sub(_sqlalchemy_url_line_pattern(), "", text)
        text = _re.sub(r"\n{3,}", "\n\n", text)

    if PASARGUARD_ENV.exists():
        shutil.copy2(PASARGUARD_ENV, PASARGUARD_ENV.with_suffix(".env.bak-before-restore"))
    PASARGUARD_ENV.write_text(text, encoding="utf-8")
    n = len(_re.findall(_sqlalchemy_url_line_pattern(), text))
    job.log(
        "Merged .env (backup app settings; "
        f"SQLALCHEMY lines={n}; DB URL finalized after convert)"
    )


def _copy_tree_replace(src: Path, dest: Path) -> int:
    """Replace dest with src tree; return number of files copied."""
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)
    return sum(1 for p in dest.rglob("*") if p.is_file())


def _find_named_dir(root: Path, name: str) -> Path | None:
    """Find a directory named `name` that looks like real content (not empty)."""
    preferred = [
        root / name,
        root / "var" / "lib" / "pasarguard" / name,
        root / "var" / "lib" / "marzban" / name,
        root / "opt" / "pasarguard" / name,
        root / "opt" / "marzban" / name,
    ]
    for p in preferred:
        if p.is_dir() and any(p.rglob("*")):
            return p
    for p in root.rglob(name):
        if p.is_dir() and any(f.is_file() for f in p.rglob("*")):
            # Prefer dirs that contain pem/json over empty shells
            return p
    return None


async def _restore_data_files(job: MigrationJob, root: Path) -> None:
    """
    Replace panel assets from backup onto this server.

    Critical: certs/templates/xray go under /var/lib/pasarguard (not /opt/pasarguard),
    because .env SSL paths are /var/lib/pasarguard/certs/...
    """
    PASARGUARD_DATA.mkdir(parents=True, exist_ok=True)
    restored: list[str] = []

    # --- certs → /var/lib/pasarguard/certs (full replace) ---
    certs_src = _find_named_dir(root, "certs")
    if certs_src:
        n = _copy_tree_replace(certs_src, PASARGUARD_DATA / "certs")
        job.log(f"Restored certs/ → /var/lib/pasarguard/certs/ ({n} files)")
        restored.append(f"certs:{n}")
    else:
        # Loose pem files anywhere in backup
        pems = [p for p in root.rglob("*.pem") if p.is_file()]
        if pems:
            dest = PASARGUARD_DATA / "certs" / "imported"
            dest.mkdir(parents=True, exist_ok=True)
            for p in pems:
                shutil.copy2(p, dest / p.name)
            job.log(f"Restored {len(pems)} loose .pem files → certs/imported/")
            restored.append(f"pem:{len(pems)}")
        else:
            job.log("No certs/ found in backup")

    # --- templates → /var/lib/pasarguard/templates ---
    templates_src = _find_named_dir(root, "templates")
    if templates_src:
        n = _copy_tree_replace(templates_src, PASARGUARD_DATA / "templates")
        job.log(f"Restored templates/ → /var/lib/pasarguard/templates/ ({n} files)")
        restored.append(f"templates:{n}")
        v2ray = PASARGUARD_DATA / "templates" / "v2ray"
        xray = PASARGUARD_DATA / "templates" / "xray"
        if v2ray.exists() and not xray.exists():
            v2ray.rename(xray)
            job.log("Renamed templates/v2ray → templates/xray")

    # --- xray_config.json ---
    xray_src = None
    for cand in (
        root / "xray_config.json",
        root / "var" / "lib" / "pasarguard" / "xray_config.json",
        root / "var" / "lib" / "marzban" / "xray_config.json",
    ):
        if cand.is_file():
            xray_src = cand
            break
    if not xray_src:
        found = list(root.rglob("xray_config.json"))
        xray_src = found[0] if found else None
    if xray_src:
        dest = PASARGUARD_DATA / "xray_config.json"
        text = xray_src.read_text(encoding="utf-8", errors="ignore")
        text = text.replace("/var/lib/marzban", "/var/lib/pasarguard").replace("/opt/marzban", "/opt/pasarguard")
        dest.write_text(text, encoding="utf-8")
        job.log("Restored xray_config.json → /var/lib/pasarguard/")
        restored.append("xray_config")

    # --- full var/lib/pasarguard tree (except db.sqlite3) ---
    for data_src in (
        root / "var" / "lib" / "pasarguard",
        root / "var" / "lib" / "marzban",
    ):
        if not data_src.is_dir():
            continue
        for item in data_src.iterdir():
            if item.name in ("db.sqlite3", "certs", "templates"):
                continue  # already handled / skip sqlite
            dest = PASARGUARD_DATA / item.name
            try:
                if item.is_dir():
                    n = _copy_tree_replace(item, dest)
                    job.log(f"Restored data/{item.name}/ ({n} files)")
                else:
                    shutil.copy2(item, dest)
                    job.log(f"Restored data/{item.name}")
                restored.append(item.name)
            except Exception as e:
                job.log(f"Skip data {item.name}: {e}")

    # --- other top-level assets into /opt/pasarguard (not certs/templates) ---
    skip_names = {
        ".env", "db_backup.sql", "db_backup_filtered.sql", "db.sqlite3",
        "docker-compose.yml", "pg_dump", "certs", "templates", "var", "opt",
        "xray_config.json",
    }
    for item in root.iterdir():
        if item.name in skip_names or item.name.startswith("pasarguard_"):
            continue
        if item.name.endswith(".sql") or item.name.endswith(".filtered"):
            continue
        if item.suffix.lower() in {".gz", ".db", ".sqlite", ".sqlite3"}:
            continue
        # Never put certs under /opt — already handled
        if item.name.lower() in ("fullchain.pem", "privkey.pem", "cert.pem", "key.pem"):
            dest = PASARGUARD_DATA / "certs" / "imported"
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dest / item.name)
            continue
        dest = PASARGUARD_DIR / item.name
        try:
            if item.is_dir():
                if dest.exists():
                    for sub in item.rglob("*"):
                        if sub.is_file():
                            rel = sub.relative_to(item)
                            target = dest / rel
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(sub, target)
                else:
                    shutil.copytree(item, dest)
            elif item.is_file():
                shutil.copy2(item, dest)
        except Exception as e:
            job.log(f"Skip copying {item.name}: {e}")

    cert_count = sum(1 for p in (PASARGUARD_DATA / "certs").rglob("*") if p.is_file()) if (PASARGUARD_DATA / "certs").exists() else 0
    job.log(
        f"App/data files restored — certs_on_disk={cert_count}, items={', '.join(restored) or 'none'}"
    )
