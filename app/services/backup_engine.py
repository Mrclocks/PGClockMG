"""Create PasarGuard full-bundle backups compatible with PGClockMG restore."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.config import (
    BACKUP_DIR,
    BACKUP_JOBS_DIR,
    PASARGUARD_DATA,
    PASARGUARD_DIR,
    PASARGUARD_ENV,
    WORK_DIR,
)
from app.services.env_migration import (
    detect_db_type_from_env,
    get_pasarguard_admin_connection,
    parse_sqlalchemy_url,
    read_env_var,
    sqlite_fs_path_from_url,
)
from app.services.pasarguard_ops import mysql_client_bins, resolve_db_service
from app.services.prerequisites import get_pasarguard_db_type, is_pasarguard_installed

LogFn = Callable[[str], None]

_CREATE_LOCK = threading.Lock()
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

STAT_TABLES = ("users", "nodes", "admins", "inbounds", "hosts", "groups")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _log(job: dict, msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    job.setdefault("logs", []).append(line)
    if len(job["logs"]) > 500:
        job["logs"] = job["logs"][-400:]


def _set_progress(job: dict, pct: int, phase: str | None = None) -> None:
    job["progress"] = max(0, min(100, int(pct)))
    if phase:
        job["phase"] = phase
        _log(job, phase)


def get_backup_job(job_id: str) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _parse_count_output(text: str) -> int | None:
    """Extract a COUNT(*) integer from docker client stdout/stderr noise."""
    for line in reversed((text or "").strip().splitlines()):
        s = line.strip().strip('"').strip("'")
        if s.isdigit():
            return int(s)
    return None


def _pg_count_credentials(db_type: str) -> list[tuple[str, str, str]]:
    """Ordered (user, password, database) candidates for live COUNT queries."""
    text = ""
    if PASARGUARD_ENV.is_file():
        try:
            text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
    admin = get_pasarguard_admin_connection(db_type, env_text=text)
    url = read_env_var(text, "SQLALCHEMY_DATABASE_URL") or ""
    parsed = {}
    try:
        parsed = parse_sqlalchemy_url(url, text) if url else {}
    except Exception:
        parsed = {}
    database = (
        admin.get("database")
        or parsed.get("database")
        or read_env_var(text, "POSTGRES_DB")
        or read_env_var(text, "DB_NAME")
        or "pasarguard"
    )
    pairs: list[tuple[str, str]] = []
    def _add(user: str | None, password: str | None) -> None:
        u = (user or "").strip()
        p = password if password is not None else ""
        if not u:
            return
        key = (u, p)
        if key not in pairs:
            pairs.append(key)

    _add(admin.get("user"), admin.get("password"))
    _add(parsed.get("user"), parsed.get("password"))
    _add(read_env_var(text, "DB_USER"), read_env_var(text, "DB_PASSWORD"))
    _add(read_env_var(text, "POSTGRES_USER"), read_env_var(text, "POSTGRES_PASSWORD"))
    _add("postgres", read_env_var(text, "POSTGRES_PASSWORD") or read_env_var(text, "DB_PASSWORD"))
    _add("pasarguard", read_env_var(text, "DB_PASSWORD") or read_env_var(text, "POSTGRES_PASSWORD"))
    # Cross-try passwords with each username (common when POSTGRES_PASSWORD ≠ DB_PASSWORD)
    users = list(dict.fromkeys(u for u, _ in pairs))
    pwds = list(dict.fromkeys(p for _, p in pairs if p))
    for u in users:
        for p in pwds:
            _add(u, p)
    return [(u, p, database) for u, p in pairs]


def _mysql_count_credentials(db_type: str) -> list[tuple[str, str, str]]:
    text = ""
    if PASARGUARD_ENV.is_file():
        try:
            text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
    admin = get_pasarguard_admin_connection(db_type, env_text=text)
    url = read_env_var(text, "SQLALCHEMY_DATABASE_URL") or ""
    parsed = {}
    try:
        parsed = parse_sqlalchemy_url(url, text) if url else {}
    except Exception:
        parsed = {}
    database = (
        admin.get("database")
        or parsed.get("database")
        or read_env_var(text, "MYSQL_DATABASE")
        or read_env_var(text, "DB_NAME")
        or "pasarguard"
    )
    pairs: list[tuple[str, str]] = []
    def _add(user: str | None, password: str | None) -> None:
        u = (user or "").strip()
        if not u:
            return
        key = (u, password if password is not None else "")
        if key not in pairs:
            pairs.append(key)

    _add(admin.get("user"), admin.get("password"))
    _add("root", read_env_var(text, "MYSQL_ROOT_PASSWORD") or admin.get("password"))
    _add(parsed.get("user"), parsed.get("password"))
    _add(read_env_var(text, "MYSQL_USER") or read_env_var(text, "DB_USER"),
         read_env_var(text, "MYSQL_PASSWORD") or read_env_var(text, "DB_PASSWORD"))
    users = list(dict.fromkeys(u for u, _ in pairs))
    pwds = list(dict.fromkeys(p for _, p in pairs if p))
    for u in users:
        for p in pwds:
            _add(u, p)
    return [(u, p, database) for u, p in pairs]


def _docker_sql_counts(db_type: str) -> dict[str, int | None]:
    """COUNT PasarGuard tables via docker compose for mysql/mariadb/pg/timescale."""
    out: dict[str, int | None] = {t: None for t in STAT_TABLES}
    svc = resolve_db_service(db_type)
    if not svc:
        # Soft-family fallback: try the other service name in the family
        for alt in (("postgresql", "timescaledb"), ("timescaledb", "postgresql"),
                    ("mysql", "mariadb"), ("mariadb", "mysql")):
            if db_type == alt[0]:
                svc = resolve_db_service(alt[1])
                if svc:
                    break
    if not svc:
        return out

    if db_type in ("mysql", "mariadb"):
        creds = _mysql_count_credentials(db_type)
        bins = mysql_client_bins(db_type, svc)
        working: tuple[str, str, str, str] | None = None  # user, pwd, db, binary
        for user, password, database in creds:
            for binary in bins:
                ok, text = _run(
                    [
                        "docker", "compose", "exec", "-T",
                        "-e", f"MYSQL_PWD={password}",
                        svc, binary, "-u", user, "-N", "-e",
                        "SELECT COUNT(*) FROM `users`;", database,
                    ],
                    cwd=str(PASARGUARD_DIR),
                    timeout=60,
                )
                if ok and _parse_count_output(text) is not None:
                    working = (user, password, database, binary)
                    break
            if working:
                break
        if not working:
            return out
        user, password, database, binary = working
        for table in STAT_TABLES:
            ok, text = _run(
                [
                    "docker", "compose", "exec", "-T",
                    "-e", f"MYSQL_PWD={password}",
                    svc, binary, "-u", user, "-N", "-e",
                    f"SELECT COUNT(*) FROM `{table}`;", database,
                ],
                cwd=str(PASARGUARD_DIR),
                timeout=60,
            )
            if ok:
                out[table] = _parse_count_output(text)
        return out

    # PostgreSQL / TimescaleDB
    creds = _pg_count_credentials(db_type)
    working_pg: tuple[str, str, str] | None = None
    for user, password, database in creds:
        ok, text = _run(
            [
                "docker", "compose", "exec", "-T",
                "-e", f"PGPASSWORD={password}",
                svc, "psql", "-U", user, "-d", database, "-At", "-v", "ON_ERROR_STOP=1",
                "-c", 'SELECT COUNT(*) FROM "users";',
            ],
            cwd=str(PASARGUARD_DIR),
            timeout=60,
        )
        if ok and _parse_count_output(text) is not None:
            working_pg = (user, password, database)
            break
    if not working_pg:
        return out
    user, password, database = working_pg
    for table in STAT_TABLES:
        ok, text = _run(
            [
                "docker", "compose", "exec", "-T",
                "-e", f"PGPASSWORD={password}",
                svc, "psql", "-U", user, "-d", database, "-At", "-v", "ON_ERROR_STOP=1",
                "-c", f'SELECT COUNT(*) FROM "{table}";',
            ],
            cwd=str(PASARGUARD_DIR),
            timeout=60,
        )
        if ok:
            out[table] = _parse_count_output(text)
    return out


def _estimate_sql_dump_counts(sql_text: str) -> dict[str, int | None]:
    """Best-effort row counts from pg_dump / mysqldump text for manifest display."""
    out: dict[str, int | None] = {t: None for t in STAT_TABLES}
    if not sql_text:
        return out
    for table in STAT_TABLES:
        n = 0
        copy_re = re.compile(
            rf"(?is)COPY\s+(?:public\.)?[\"`]?{re.escape(table)}[\"`]?\s*\([^;]*?\)\s+FROM\s+stdin;\s*(.*?)\\.\s*"
        )
        for m in copy_re.finditer(sql_text):
            block = (m.group(1) or "").strip()
            if block:
                n += sum(1 for ln in block.splitlines() if ln.strip())
        insert_re = re.compile(
            rf"(?i)INSERT\s+INTO\s+[\"`\[]?{re.escape(table)}[\"`\]]?\s*(?:\([^)]*\))?\s*VALUES",
        )
        for m in insert_re.finditer(sql_text):
            # Count top-level value groups until semicolon
            rest = sql_text[m.end():]
            end = rest.find(";")
            chunk = rest if end < 0 else rest[:end]
            depth = 0
            for ch in chunk:
                if ch == "(":
                    if depth == 0:
                        n += 1
                    depth += 1
                elif ch == ")" and depth:
                    depth -= 1
        out[table] = n if n > 0 else None
    return out


def _counts_from_dump_artifact(dump_path: Path, db_type: str) -> dict[str, int | None]:
    if db_type == "sqlite":
        return _sqlite_counts(dump_path)
    try:
        # Cap read size — large dumps still usually put table data early enough
        text = dump_path.read_text(encoding="utf-8", errors="ignore")[:8_000_000]
    except OSError:
        return {t: None for t in STAT_TABLES}
    return _estimate_sql_dump_counts(text)


def _merge_counts(
    primary: dict | None,
    fallback: dict | None,
) -> dict[str, int | None]:
    primary = primary or {}
    fallback = fallback or {}
    out: dict[str, int | None] = {}
    for t in STAT_TABLES:
        a = primary.get(t)
        b = fallback.get(t)
        if isinstance(a, int):
            out[t] = a
        elif isinstance(b, int):
            out[t] = b
        else:
            out[t] = None
    return out


def _enrich_manifest_from_zip(path: Path, meta: dict) -> dict:
    """Fill missing counts/db_type from the zip's pgclockmg-manifest.json."""
    counts = meta.get("counts") if isinstance(meta.get("counts"), dict) else {}
    has_counts = any(isinstance(counts.get(t), int) for t in STAT_TABLES)
    if has_counts and meta.get("db_type"):
        return meta
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = zf.namelist()
            man_name = next(
                (n for n in names if n == "pgclockmg-manifest.json" or n.endswith("/pgclockmg-manifest.json")),
                None,
            )
            if not man_name:
                return meta
            inner = json.loads(zf.read(man_name).decode("utf-8", errors="ignore"))
    except Exception:
        return meta
    if not isinstance(inner, dict):
        return meta
    merged = dict(meta)
    if not merged.get("db_type") and inner.get("db_type"):
        merged["db_type"] = inner.get("db_type")
    inner_counts = inner.get("counts") if isinstance(inner.get("counts"), dict) else {}
    merged["counts"] = _merge_counts(counts, inner_counts)
    return merged


def resolve_backup_manifest(path: Path) -> dict:
    """Sidecar meta merged with in-zip pgclockmg-manifest (fills missing counts)."""
    meta = _read_sidecar_meta(path) or {}
    return _enrich_manifest_from_zip(path, meta)


def list_backup_files() -> list[dict]:
    items: list[dict] = []
    if not BACKUP_DIR.is_dir():
        return items
    for path in sorted(BACKUP_DIR.glob("pgclockmg-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        meta = resolve_backup_manifest(path)
        st = path.stat()
        items.append({
            "id": path.stem,
            "filename": path.name,
            "size_bytes": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "manifest": meta,
        })
    return items


def resolve_backup_path(backup_id: str) -> Path | None:
    if not backup_id or not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", backup_id):
        return None
    # accept with or without .zip
    name = backup_id if backup_id.endswith(".zip") else f"{backup_id}.zip"
    path = BACKUP_DIR / name
    if path.is_file():
        return path
    # also allow stem match for pgclockmg-...
    for cand in BACKUP_DIR.glob("pgclockmg-*.zip"):
        if cand.stem == backup_id or cand.name == backup_id:
            return cand
    return None


def delete_backup_file(backup_id: str) -> bool:
    path = resolve_backup_path(backup_id)
    if not path:
        return False
    try:
        path.unlink(missing_ok=True)
        meta = path.with_suffix(path.suffix + ".json")
        meta.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def apply_retention(keep: int) -> int:
    keep = max(1, int(keep or 10))
    files = sorted(BACKUP_DIR.glob("pgclockmg-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for path in files[keep:]:
        try:
            path.unlink(missing_ok=True)
            path.with_suffix(path.suffix + ".json").unlink(missing_ok=True)
            removed += 1
        except OSError:
            pass
    return removed


def _read_sidecar_meta(path: Path) -> dict | None:
    meta = path.with_suffix(path.suffix + ".json")
    if not meta.is_file():
        return None
    try:
        return json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_sidecar_meta(path: Path, data: dict) -> None:
    meta = path.with_suffix(path.suffix + ".json")
    meta.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(meta, 0o600)
        os.chmod(path, 0o600)
    except OSError:
        pass


def _run(cmd: list[str], *, cwd: str | None = None, env: dict | None = None, timeout: int = 600) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return r.returncode == 0, out
    except Exception as exc:
        return False, str(exc)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _copy_tree_filtered(src: Path, dest: Path, *, skip_names: set[str] | None = None) -> None:
    skip_names = skip_names or set()
    if not src.is_dir():
        return
    dest.mkdir(parents=True, exist_ok=True)
    for root, dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        # prune heavy/irrelevant dirs
        dirs[:] = [
            d for d in dirs
            if d not in skip_names and d not in {".git", "__pycache__", "node_modules"}
        ]
        target_dir = dest / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            if name in skip_names:
                continue
            sp = Path(root) / name
            # skip huge sqlite wal copies if we already dump db separately — still include certs etc.
            try:
                if sp.stat().st_size > 2 * 1024 * 1024 * 1024:
                    continue
            except OSError:
                continue
            dp = target_dir / name
            try:
                shutil.copy2(sp, dp)
            except OSError:
                continue


def live_panel_stats() -> dict:
    """Best-effort live counts from the installed PasarGuard database."""
    result = {
        "ok": False,
        "db_type": None,
        "counts": {t: None for t in STAT_TABLES},
        "error": None,
    }
    if not is_pasarguard_installed():
        result["error"] = "pasarguard_not_installed"
        return result
    db_type = get_pasarguard_db_type() or "sqlite"
    result["db_type"] = db_type
    try:
        if db_type == "sqlite":
            env_text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore") if PASARGUARD_ENV.is_file() else ""
            path = _resolve_sqlite_path(env_text)
            if not path.is_file():
                result["error"] = "sqlite_missing"
                return result
            counts = _sqlite_counts(path)
            result["counts"] = counts
            result["ok"] = True
            return result
        counts = _docker_sql_counts(db_type)
        result["counts"] = counts
        result["ok"] = any(v is not None for v in counts.values())
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result


def _sqlite_counts(path: Path) -> dict[str, int | None]:
    out: dict[str, int | None] = {t: None for t in STAT_TABLES}
    try:
        conn = _sqlite_connect_ro(path, timeout=10)
    except sqlite3.Error:
        return out
    try:
        cur = conn.cursor()
        for table in STAT_TABLES:
            try:
                cur.execute(f'SELECT COUNT(*) FROM "{table}"')
                out[table] = int(cur.fetchone()[0])
            except sqlite3.Error:
                out[table] = None
    finally:
        conn.close()
    return out


def _normalize_sqlite_fs_path(raw: str | Path) -> Path:
    """Collapse UNC-style ``//path`` into a normal absolute Unix path."""
    s = str(raw).strip().split("?", 1)[0].split("#", 1)[0]
    if s.startswith("//") or s.startswith("\\\\"):
        s = "/" + s.lstrip("/\\")
    return Path(s).expanduser()


def _sqlite_connect_ro(path: Path, *, timeout: float = 120) -> sqlite3.Connection:
    """Open SQLite read-only via a proper ``file:///...`` URI (never ``file://host``)."""
    path = _normalize_sqlite_fs_path(path)
    # resolve() also collapses //host-style paths on Linux
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    uri = resolved.as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=timeout)


def _resolve_sqlite_path(env_text: str = "") -> Path:
    """Prefer SQLALCHEMY sqlite path from .env; fall back to official data dir.

    Covers absolute, relative, aiosqlite, query strings, and miswritten slash counts
    so backup never opens ``file://var/...`` (invalid URI authority).
    """
    url = read_env_var(env_text, "SQLALCHEMY_DATABASE_URL") or ""
    candidates: list[Path] = []

    parsed = sqlite_fs_path_from_url(url)
    if parsed:
        p = _normalize_sqlite_fs_path(parsed)
        if not p.is_absolute():
            # Relative paths: try PasarGuard data dir, then install dir, then cwd
            candidates.append(PASARGUARD_DATA / p)
            candidates.append(PASARGUARD_DIR / p)
            candidates.append(Path.cwd() / p)
        else:
            candidates.append(p)
            # Docker-style /var/lib/pasarguard/... may also exist under PASARGUARD_DATA
            try:
                posix = p.as_posix()
                if posix.startswith("/var/lib/pasarguard/"):
                    rel = posix[len("/var/lib/pasarguard/") :]
                    if rel:
                        candidates.append(PASARGUARD_DATA / rel)
            except Exception:
                pass

    candidates.append(PASARGUARD_DATA / "db.sqlite3")
    # Deduplicate while preserving order
    seen: set[str] = set()
    ordered: list[Path] = []
    for c in candidates:
        key = str(c)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(c)

    for c in ordered:
        if c.is_file():
            return _normalize_sqlite_fs_path(c)
    # Prefer a candidate whose parent exists (for clearer errors / upcoming create)
    for c in ordered:
        if c.parent.is_dir():
            return _normalize_sqlite_fs_path(c)
    return _normalize_sqlite_fs_path(PASARGUARD_DATA / "db.sqlite3")


def _dump_sqlite(dest: Path, job: dict, *, env_text: str = "") -> None:
    src = _resolve_sqlite_path(env_text)
    src = _normalize_sqlite_fs_path(src)
    if not src.is_file():
        raise RuntimeError(f"SQLite database not found at {src}")
    _log(job, f"Copying SQLite database from {src}…")
    dest.parent.mkdir(parents=True, exist_ok=True)
    src_conn = _sqlite_connect_ro(src, timeout=120)
    try:
        dst_conn = sqlite3.connect(str(dest))
        try:
            with dst_conn:
                src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    if dest.stat().st_size < 64:
        raise RuntimeError("SQLite backup copy is empty")


def _safe_db_ident(value: str | None, *, fallback: str | None = None, kind: str = "database") -> str:
    """Reject empty / path-like / URI-tainted DB identifiers from misparsed .env URLs."""
    raw = (value or "").strip()
    if not raw:
        if fallback is not None:
            return fallback
        raise RuntimeError(f"Missing {kind} name in PasarGuard .env")
    # Never allow filesystem or URI fragments to leak into docker dump args
    if raw.startswith(("/", ".", "file:")) or "://" in raw or "\\" in raw:
        raise RuntimeError(f"Invalid {kind} name from .env: {raw!r}")
    if not re.fullmatch(r"[A-Za-z0-9_$-]{1,128}", raw):
        raise RuntimeError(f"Unsafe {kind} name from .env: {raw!r}")
    return raw


def _dump_postgres(db_type: str, dest: Path, job: dict) -> None:
    svc = resolve_db_service(db_type)
    if not svc:
        raise RuntimeError(f"No Docker DB service found for {db_type}")
    conn = get_pasarguard_admin_connection(db_type)
    users: list[str] = []
    for cand in (
        conn.get("user"),
        "postgres",
        read_env_var(PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore") if PASARGUARD_ENV.is_file() else "", "DB_USER"),
        "pasarguard",
    ):
        if not cand:
            continue
        try:
            safe = _safe_db_ident(str(cand), kind="user")
        except RuntimeError:
            continue
        if safe not in users:
            users.append(safe)
    if not users:
        users = ["postgres"]
    password = conn.get("password") or ""
    database = _safe_db_ident(conn.get("database"), fallback="pasarguard", kind="database")
    last_err = ""
    for user in users:
        _log(job, f"Running pg_dump via {svc} as {user} (db={database})…")
        cmd = [
            "docker", "compose", "exec", "-T",
            "-e", f"PGPASSWORD={password}",
            svc, "pg_dump",
            "-U", user,
            "-d", database,
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-acl",
            "--encoding=UTF8",
        ]
        # Keep TimescaleDB extension DDL in the dump for native Timescale restores.
        try:
            with dest.open("wb") as out:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(PASARGUARD_DIR),
                    stdout=out,
                    stderr=subprocess.PIPE,
                )
                _, err = proc.communicate(timeout=1800)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError("pg_dump timed out")
        if proc.returncode == 0 and dest.is_file() and dest.stat().st_size >= 64:
            # Best-effort globals (roles) — used by some restore paths; safe to ignore failure
            globals_path = dest.parent / "globals.sql"
            try:
                gcmd = [
                    "docker", "compose", "exec", "-T",
                    "-e", f"PGPASSWORD={password}",
                    svc, "pg_dumpall",
                    "-U", user,
                    "--globals-only",
                    "--no-role-passwords",
                ]
                with globals_path.open("wb") as gout:
                    gproc = subprocess.Popen(
                        gcmd,
                        cwd=str(PASARGUARD_DIR),
                        stdout=gout,
                        stderr=subprocess.DEVNULL,
                    )
                    gproc.communicate(timeout=120)
                if not globals_path.is_file() or globals_path.stat().st_size < 16:
                    globals_path.unlink(missing_ok=True)
                else:
                    _log(job, "Included globals.sql (roles)")
            except Exception:
                try:
                    globals_path.unlink(missing_ok=True)
                except OSError:
                    pass
            return
        last_err = (err or b"").decode("utf-8", errors="replace")[-1500:]
        if "password authentication failed" in last_err.lower() or "role" in last_err.lower():
            continue
    raise RuntimeError(last_err or "pg_dump failed")


def _dump_mysql(db_type: str, dest: Path, job: dict) -> None:
    svc = resolve_db_service(db_type)
    if not svc:
        raise RuntimeError(f"No Docker DB service found for {db_type}")
    conn = get_pasarguard_admin_connection(db_type)
    env_text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore") if PASARGUARD_ENV.is_file() else ""
    users: list[str] = []
    for cand in (
        conn.get("user"),
        "root",
        read_env_var(env_text, "MYSQL_USER"),
        read_env_var(env_text, "DB_USER"),
        "pasarguard",
    ):
        if not cand:
            continue
        try:
            safe = _safe_db_ident(str(cand), kind="user")
        except RuntimeError:
            continue
        if safe not in users:
            users.append(safe)
    if not users:
        users = ["root"]
    password = conn.get("password") or ""
    database = _safe_db_ident(conn.get("database"), fallback="pasarguard", kind="database")
    _log(job, f"Running mysqldump via {svc} (db={database})…")
    dump_bins = ["mysqldump", "mariadb-dump"]
    if "maria" in (svc or "").lower() or db_type == "mariadb":
        dump_bins = ["mariadb-dump", "mysqldump"]
    last_err = ""
    for user in users:
        for binary in dump_bins:
            cmd = [
                "docker", "compose", "exec", "-T",
                "-e", f"MYSQL_PWD={password}",
                svc, binary,
                "-u", user,
                "--single-transaction",
                "--quick",
                "--routines",
                "--triggers",
                "--events",
                "--hex-blob",
                "--default-character-set=utf8mb4",
                "--set-gtid-purged=OFF",
                "--column-statistics=0",
                database,
            ]
            try:
                with dest.open("wb") as out:
                    proc = subprocess.Popen(
                        cmd,
                        cwd=str(PASARGUARD_DIR),
                        stdout=out,
                        stderr=subprocess.PIPE,
                    )
                    _, err = proc.communicate(timeout=1800)
            except FileNotFoundError:
                continue
            except subprocess.TimeoutExpired:
                proc.kill()
                raise RuntimeError("mysqldump timed out")
            if proc.returncode == 0 and dest.is_file() and dest.stat().st_size >= 64:
                _log(job, f"mysqldump ok ({binary} as {user})")
                return
            last_err = (err or b"").decode("utf-8", errors="replace")[-1500:]
            # Retry without flags some servers reject
            if "unknown variable" in last_err.lower() or "unknown option" in last_err.lower():
                cmd2 = [
                    "docker", "compose", "exec", "-T",
                    "-e", f"MYSQL_PWD={password}",
                    svc, binary,
                    "-u", user,
                    "--single-transaction",
                    "--routines",
                    "--triggers",
                    "--events",
                    "--hex-blob",
                    "--default-character-set=utf8mb4",
                    database,
                ]
                with dest.open("wb") as out:
                    proc = subprocess.Popen(
                        cmd2,
                        cwd=str(PASARGUARD_DIR),
                        stdout=out,
                        stderr=subprocess.PIPE,
                    )
                    _, err = proc.communicate(timeout=1800)
                if proc.returncode == 0 and dest.is_file() and dest.stat().st_size >= 64:
                    return
                last_err = (err or b"").decode("utf-8", errors="replace")[-1500:]
            if "executable file not found" in last_err.lower() or "no such file" in last_err.lower():
                continue
            if "access denied" in last_err.lower():
                break  # try next user
    raise RuntimeError(last_err or "mysqldump failed")


def _collect_extra_files(staging: Path, job: dict) -> None:
    """Copy panel assets into a restore-compatible layout.

    Layout matches what ``pg_restore._restore_data_files`` expects:
      .env, docker-compose.yml, certs/, templates/, xray_config.json,
      and other /var/lib/pasarguard files under var/lib/pasarguard/.
    """
    if PASARGUARD_ENV.is_file():
        shutil.copy2(PASARGUARD_ENV, staging / ".env")
        _log(job, f"Included .env from {PASARGUARD_ENV}")
    else:
        raise RuntimeError(f"Missing PasarGuard .env at {PASARGUARD_ENV}")

    compose_copied = False
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        p = PASARGUARD_DIR / name
        if p.is_file():
            shutil.copy2(p, staging / name)
            _log(job, f"Included {name} from {PASARGUARD_DIR}")
            compose_copied = True
            break
    if not compose_copied:
        _log(job, "WARN: no docker-compose.yml found under /opt/pasarguard")

    # Official data root used by restore
    data_root = staging / "var" / "lib" / "pasarguard"
    data_root.mkdir(parents=True, exist_ok=True)

    certs = PASARGUARD_DATA / "certs"
    if certs.is_dir():
        _copy_tree_filtered(certs, staging / "certs")
        _copy_tree_filtered(certs, data_root / "certs")
        _log(job, f"Included certs/ from {certs}")

    templates = PASARGUARD_DATA / "templates"
    if templates.is_dir():
        _copy_tree_filtered(templates, staging / "templates")
        _copy_tree_filtered(templates, data_root / "templates")
        _log(job, f"Included templates/ from {templates}")

    # Skip live DB files / engine datadirs — dumps are produced separately
    skip_files = {
        "db.sqlite3", "db.sqlite3-wal", "db.sqlite3-shm",
    }
    skip_dirs = {
        "mysql", "mariadb", "postgresql", "postgres", "timescaledb",
        "pgdata", "pgbouncer",
    }

    if PASARGUARD_DATA.is_dir():
        for child in PASARGUARD_DATA.iterdir():
            if child.name in skip_files or child.name in skip_dirs:
                continue
            if child.name in ("certs", "templates"):
                continue  # already mirrored at zip root + data tree
            if child.is_file():
                # Prefer restore hot-paths at zip root for xray_config.json
                if child.name == "xray_config.json":
                    shutil.copy2(child, staging / "xray_config.json")
                    shutil.copy2(child, data_root / "xray_config.json")
                    _log(job, "Included xray_config.json")
                    continue
                try:
                    shutil.copy2(child, data_root / child.name)
                except OSError as exc:
                    _log(job, f"Skip data file {child.name}: {exc}")
            elif child.is_dir():
                try:
                    _copy_tree_filtered(child, data_root / child.name)
                    _log(job, f"Included data/{child.name}/ from {PASARGUARD_DATA}")
                except OSError as exc:
                    _log(job, f"Skip data dir {child.name}: {exc}")

    # Light extras from /opt/pasarguard (not the whole tree)
    for name in ("xray_config.json", "config.json"):
        p = PASARGUARD_DIR / name
        if p.is_file() and not (staging / name).exists():
            shutil.copy2(p, staging / name)
            _log(job, f"Included {name} from {PASARGUARD_DIR}")



def create_backup_bundle(*, trigger: str = "manual") -> dict:
    """Synchronously build a full-bundle zip. Returns job dict."""
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "status": "running",
            "trigger": trigger,
            "logs": [],
            "progress": 0,
            "phase": "starting",
            "started_at": _utc_now(),
            "finished_at": None,
            "backup_id": None,
            "filename": None,
            "size_bytes": None,
            "manifest": None,
            "error": None,
        }
    result = _create_backup_into(job_id, trigger=trigger)
    try:
        BACKUP_JOBS_DIR.mkdir(parents=True, exist_ok=True)
        (BACKUP_JOBS_DIR / f"{job_id}.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass
    return result


def start_backup_async(*, trigger: str = "manual") -> dict:
    """Start backup in a background thread; return running job stub."""
    job_id = uuid.uuid4().hex[:12]
    job = {
        "job_id": job_id,
        "status": "queued",
        "trigger": trigger,
        "logs": [],
        "progress": 0,
        "phase": "queued",
        "started_at": _utc_now(),
        "finished_at": None,
        "backup_id": None,
        "filename": None,
        "size_bytes": None,
        "manifest": None,
        "error": None,
    }
    with _jobs_lock:
        _jobs[job_id] = job

    def _safe_runner():
        try:
            with _jobs_lock:
                _jobs[job_id]["status"] = "running"
                _jobs[job_id]["logs"].append(f"[{time.strftime('%H:%M:%S')}] Starting backup…")
            result = _create_backup_into(job_id, trigger=trigger)
            with _jobs_lock:
                _jobs[job_id] = result
        except Exception as exc:
            with _jobs_lock:
                j = _jobs.get(job_id) or job
                j["status"] = "error"
                j["error"] = str(exc)
                j["finished_at"] = _utc_now()
                _jobs[job_id] = j

    threading.Thread(target=_safe_runner, name=f"backup-{job_id}", daemon=True).start()
    return dict(job)


def _create_backup_into(job_id: str, *, trigger: str) -> dict:
    """Like create_backup_bundle but writes into an existing job_id."""
    with _jobs_lock:
        job = _jobs.setdefault(job_id, {
            "job_id": job_id,
            "status": "running",
            "trigger": trigger,
            "logs": [],
            "progress": 0,
            "phase": "starting",
            "started_at": _utc_now(),
            "finished_at": None,
            "backup_id": None,
            "filename": None,
            "size_bytes": None,
            "manifest": None,
            "error": None,
        })
        job["status"] = "running"
        job["progress"] = max(int(job.get("progress") or 0), 2)
        job["phase"] = "starting"

    if not _CREATE_LOCK.acquire(blocking=False):
        job["status"] = "error"
        job["error"] = "Another backup is already running"
        job["finished_at"] = _utc_now()
        _log(job, job["error"])
        return dict(job)

    staging: Path | None = None
    try:
        if not is_pasarguard_installed():
            raise RuntimeError("PasarGuard is not installed on this server")
        if not PASARGUARD_ENV.is_file():
            raise RuntimeError("PasarGuard .env not found")

        _set_progress(job, 8, "Reading PasarGuard environment…")
        env_text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
        db_type = get_pasarguard_db_type() or detect_db_type_from_env(env_text, prefer_compose=True) or "sqlite"
        _set_progress(job, 14, f"Detected database: {db_type}")

        staging = Path(tempfile.mkdtemp(prefix="pg-backup-", dir=str(WORK_DIR)))
        _set_progress(job, 22, "Collecting panel files…")
        _collect_extra_files(staging, job)

        _set_progress(job, 35, f"Dumping {db_type} database…")
        if db_type == "sqlite":
            _dump_sqlite(staging / "db.sqlite3", job, env_text=env_text)
        elif db_type in ("postgresql", "timescaledb"):
            _dump_postgres(db_type, staging / "db_backup.sql", job)
        elif db_type in ("mysql", "mariadb"):
            _dump_mysql(db_type, staging / "db_backup.sql", job)
        else:
            raise RuntimeError(f"Unsupported database type: {db_type}")

        # Verify dump artifact before zipping
        if db_type == "sqlite":
            dump_path = staging / "db.sqlite3"
        else:
            dump_path = staging / "db_backup.sql"
        if not dump_path.is_file() or dump_path.stat().st_size < 64:
            raise RuntimeError(f"Database dump missing or empty: {dump_path.name}")
        _set_progress(job, 55, f"Dump ready: {dump_path.name} ({dump_path.stat().st_size} bytes)")

        stats = live_panel_stats()
        counts = stats.get("counts") or {}
        # If live docker COUNT failed (common on PG password mismatch), fall back
        # to counting rows from the dump we just produced so the UI still shows specs.
        dump_counts = _counts_from_dump_artifact(dump_path, db_type)
        counts = _merge_counts(counts, dump_counts)
        if any(isinstance(counts.get(t), int) for t in STAT_TABLES):
            _set_progress(
                job,
                62,
                "Panel counts: "
                + ", ".join(f"{k}={counts.get(k)}" for k in STAT_TABLES if counts.get(k) is not None),
            )
        else:
            _set_progress(job, 62, "Panel counts unavailable (live query + dump estimate both empty)")

        # Refuse zip when live/dump panel has data but the dump looks empty.
        live_users = counts.get("users")
        if isinstance(live_users, int) and live_users > 0:
            dump_users = dump_counts.get("users")
            if db_type == "sqlite":
                dump_users = _sqlite_counts(dump_path).get("users")
            elif dump_users is None:
                try:
                    sample = dump_path.read_text(encoding="utf-8", errors="ignore")[:2_000_000]
                except Exception:
                    sample = ""
                lower = sample.lower()
                if (
                    "copy public.users" in lower
                    or "copy users" in lower
                    or "insert into `users`" in lower
                    or "insert into users" in lower
                ):
                    dump_users = live_users
                elif "create table" in lower and "users" in lower:
                    dump_users = 0
            if dump_users == 0:
                raise RuntimeError(
                    f"Backup dump has 0 users but panel reports {live_users} — refusing empty dump"
                )

        version = read_env_var(env_text, "APP_VERSION") or None

        # Stamp engine into bundled .env so restore analyze is unambiguous
        env_out = staging / ".env"
        if env_out.is_file() and db_type:
            try:
                text = env_out.read_text(encoding="utf-8", errors="ignore")
                if "PASARGUARD_DB_ENGINE=" not in text:
                    text = text.rstrip() + f"\nPASARGUARD_DB_ENGINE={db_type}\n"
                    env_out.write_text(text, encoding="utf-8")
            except Exception:
                pass

        manifest = {
            "format": "pgclockmg-full-bundle",
            "format_version": 1,
            "created_at": _utc_now(),
            "trigger": trigger,
            "hostname": os.uname().nodename if hasattr(os, "uname") else "",
            "pasarguard_version": version,
            "db_type": db_type,
            "counts": {k: counts.get(k) for k in STAT_TABLES},
            "files": sorted(str(p.relative_to(staging)) for p in staging.rglob("*") if p.is_file()),
        }
        (staging / "pgclockmg-manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"pgclockmg-{db_type}-{_stamp()}.zip"
        out_path = BACKUP_DIR / filename
        _set_progress(job, 72, f"Writing zip {filename}…")
        with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for path in staging.rglob("*"):
                if path.is_file():
                    zf.write(path, arcname=str(path.relative_to(staging)))

        digest = _sha256_file(out_path)
        manifest["sha256"] = digest
        manifest["size_bytes"] = out_path.stat().st_size
        manifest["filename"] = filename
        _write_sidecar_meta(out_path, manifest)
        _set_progress(job, 86, "Verifying backup archive…")

        with zipfile.ZipFile(out_path, "r") as zf:
            names = set(zf.namelist())
        if ".env" not in names and not any(n.endswith("/.env") for n in names):
            raise RuntimeError("Backup zip missing .env")
        has_dump = (
            "db_backup.sql" in names
            or "db.sqlite3" in names
            or any(n.endswith("/db_backup.sql") or n.endswith("/db.sqlite3") for n in names)
        )
        if not has_dump:
            raise RuntimeError("Backup zip missing database dump")

        job["backup_id"] = out_path.stem
        job["filename"] = filename
        job["size_bytes"] = out_path.stat().st_size
        job["manifest"] = manifest
        _set_progress(job, 90, f"Backup ready ({job['size_bytes']} bytes)")

        from app.services.backup_settings import update_settings
        update_settings({
            "last_backup": {
                "backup_id": job["backup_id"],
                "filename": filename,
                "created_at": manifest["created_at"],
                "size_bytes": job["size_bytes"],
                "db_type": db_type,
                "counts": manifest["counts"],
                "sha256": digest,
            },
            "last_error": None,
        })

        # Web-panel manual backups: auto-send to Telegram while job is still running
        # so the UI poll does not resolve before the document is uploaded.
        try:
            from app.services.backup_telegram import maybe_auto_send_backup

            _set_progress(job, 93, "Checking Telegram delivery…")
            tg_send = maybe_auto_send_backup({**dict(job), "status": "success", "trigger": trigger})
            if tg_send is not None:
                job["telegram"] = tg_send
                if tg_send.get("ok"):
                    _set_progress(job, 98, "Telegram: backup document sent")
                else:
                    _set_progress(job, 98, f"Telegram send failed: {tg_send.get('error') or 'unknown'}")
            else:
                _set_progress(job, 98, "Telegram auto-send skipped")
        except Exception as tg_exc:
            job["telegram"] = {"ok": False, "error": str(tg_exc)}
            _set_progress(job, 98, f"Telegram send failed: {tg_exc}")

        job["status"] = "success"
        job["progress"] = 100
        job["phase"] = "done"
        job["finished_at"] = _utc_now()
        return dict(job)
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)
        job["finished_at"] = _utc_now()
        _log(job, f"ERROR: {exc}")
        try:
            from app.services.backup_settings import update_settings
            update_settings({"last_error": {"at": _utc_now(), "message": str(exc)}})
        except Exception:
            pass
        return dict(job)
    finally:
        _CREATE_LOCK.release()
        if staging and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
