"""Smart PasarGuard backup restore (fixes version/password pitfalls)."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sqlite3
import tempfile
import traceback
import zipfile
from pathlib import Path
from typing import Callable

from app.config import PASARGUARD_DIR, PASARGUARD_ENV, PASARGUARD_DATA, UPLOAD_DIR
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

SUPPORTED_RESTORE_DBS = frozenset({
    "sqlite", "mysql", "mariadb", "postgresql", "timescaledb",
})


def get_restore_job(job_id: str) -> MigrationJob | None:
    return _restore_jobs.get(job_id)


def soft_db_family(a: str | None, b: str | None) -> bool:
    """True when engines are interchangeable for restore (mysql↔mariadb, pg↔timescale)."""
    if not a or not b:
        return False
    if a == b:
        return True
    return {a, b} <= {"mysql", "mariadb"} or {a, b} <= {"postgresql", "timescaledb"}


def filter_timescaledb_extension_sql(sql: str) -> str:
    """Strip CREATE/DROP EXTENSION timescaledb lines (pre/post restore handles it)."""
    return "\n".join(
        ln for ln in sql.splitlines()
        if not re.search(
            r"^\s*(DROP|CREATE)\s+EXTENSION\s+(IF\s+(EXISTS|NOT\s+EXISTS)\s+)?timescaledb\b",
            ln,
            re.I,
        )
    )


def _sql_literal(value: str) -> str:
    return "'" + (value or "").replace("'", "''") + "'"


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
    return scored[0] if scored else (versions[0].strip() or None)


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
    dest.mkdir(parents=True, exist_ok=True)
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/"):
            raise ValueError(f"Unsafe zip entry: {info.filename}")
        target = dest / name
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


def _find_env(root: Path) -> Path | None:
    for p in [root / ".env", *root.rglob(".env")]:
        if p.is_file() and p.name == ".env":
            return p
    return None


def _find_backup_root(extracted: Path) -> Path:
    """Prefer directory that contains .env + dump artifacts."""
    env = _find_env(extracted)
    if env:
        return env.parent
    for cand in [extracted, *extracted.iterdir()]:
        if cand.is_dir() and (
            (cand / "db_backup.sql").exists()
            or (cand / "pg_dump" / "manifest.tsv").exists()
            or (cand / "db.sqlite3").exists()
        ):
            return cand
    return extracted


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

    tmp = Path(tempfile.mkdtemp(prefix="pg-backup-analyze-"))
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            _safe_extract(zf, tmp)
        root = _find_backup_root(tmp)
        env_path = _find_env(root)
        env_text = env_path.read_text(encoding="utf-8", errors="ignore") if env_path else ""
        # Backup .env must NOT use live compose (that would mislabel every PG backup as Timescale)
        db_type = detect_db_type_from_env(env_text, prefer_compose=False) if env_text else None
        summary = extract_env_summary(env_text) if env_text else None

        layout = "none"
        if (root / "pg_dump" / "manifest.tsv").exists():
            layout = "multi"
        elif (root / "db_backup.sql").exists():
            layout = "single"
        elif (root / "db.sqlite3").exists() or list(root.rglob("db.sqlite3")):
            layout = "sqlite_file"

        ts_versions = _parse_manifest_ts_versions(root)
        # Official Timescale backups keep postgresql+asyncpg URL — use manifest / dump hints
        if db_type in (None, "postgresql"):
            if ts_versions:
                db_type = "timescaledb"
            elif _backup_sql_mentions_timescale(root):
                db_type = "timescaledb"

        table_counts = _estimate_backup_table_counts(root, layout)
        installed = is_pasarguard_installed()
        installed_db = get_pasarguard_db_type() if installed else None

        warnings: list[dict] = []
        ok = True
        if not env_path:
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
        if db_type and installed_db and db_type != installed_db:
            if soft_db_family(db_type, installed_db):
                warnings.append({
                    "en": f"Related engines (backup={db_type}, installed={installed_db}) — restore continues automatically.",
                    "fa": f"موتورهای هم‌خانواده (بکاپ={db_type}، نصب={installed_db}) — ریستور خودکار ادامه می‌یابد.",
                    "ru": f"Смежные СУБД (backup={db_type}, installed={installed_db}) — восстановление продолжится.",
                })
            else:
                experimental_db_change = True
                warnings.append({
                    "en": f"Database differs (backup={db_type}, installed={installed_db}). Confirm auto-convert to continue.",
                    "fa": f"دیتابیس فرق دارد (بکاپ={db_type}، نصب={installed_db}). برای تبدیل خودکار تأیید کنید.",
                    "ru": f"Тип БД отличается (backup={db_type}, installed={installed_db}). Подтвердите автоконвертацию.",
                })

        if layout == "none" and db_type != "sqlite":
            ok = False
            warnings.append({
                "en": "No database dump found in backup (expected db_backup.sql or pg_dump/).",
                "fa": "دامپ دیتابیس داخل بکاپ پیدا نشد (db_backup.sql یا pg_dump/).",
                "ru": "Дамп БД в бэкапе не найден.",
            })

        if ts_versions:
            warnings.append({
                "en": f"Backup TimescaleDB: {', '.join(sorted(set(ts_versions)))}. Wizard auto-aligns the image before restore.",
                "fa": f"نسخه TimescaleDB بکاپ: {', '.join(sorted(set(ts_versions)))}. قبل از ریستور ایمیج سرور هم‌تراز می‌شود.",
                "ru": f"TimescaleDB в бэкапе: {', '.join(sorted(set(ts_versions)))}. Образ будет выровнен автоматически.",
            })

        if table_counts:
            preview = ", ".join(f"{k}={v}" for k, v in list(table_counts.items())[:6])
            warnings.append({
                "en": f"Backup data preview: {preview}",
                "fa": f"پیش‌نمایش داده بکاپ: {preview}",
                "ru": f"Данные в бэкапе: {preview}",
            })
        elif layout != "none":
            warnings.append({
                "en": "Could not estimate row counts from backup — restore will still verify after import.",
                "fa": "شمارش ردیف‌های بکاپ ممکن نشد — بعد از ایمپورت حتماً verify می‌شود.",
                "ru": "Не удалось оценить строки бэкапа — проверка будет после импорта.",
            })

        return {
            "ok": ok,
            "filename": zip_path.name,
            "size": zip_path.stat().st_size,
            "backup_db": db_type,
            "installed_db": installed_db,
            "db_match": (db_type == installed_db) if (db_type and installed_db) else None,
            "soft_match": soft_db_family(db_type, installed_db) if (db_type and installed_db) else None,
            "experimental_db_change": experimental_db_change,
            "supported_target_dbs": sorted(SUPPORTED_RESTORE_DBS),
            "layout": layout,
            "timescaledb_versions": sorted(set(ts_versions)),
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

    job = MigrationJob()
    _restore_jobs[job.job_id] = job
    asyncio.create_task(_run_restore(job, params, analysis))
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


async def _run(job: MigrationJob, cmd: list[str], cwd: str | None = None, timeout: int = 600) -> tuple[bool, str]:
    job.log(f"$ {' '.join(cmd)}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out = (out_b or b"").decode("utf-8", errors="replace")
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

    async def _run_cmd(self, cmd, cwd=None, timeout=600):
        return await _run(self.job, cmd, cwd=cwd, timeout=timeout)


def _read_current_env() -> str:
    return PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore") if PASARGUARD_ENV.exists() else ""


def _set_env_var(text: str, key: str, value: str) -> str:
    pattern = rf"(?m)^\s*#?\s*{re.escape(key)}\s*=.*$"
    line = f'{key}="{value}"'
    if re.search(pattern, text):
        return re.sub(pattern, lambda _m: line, text, count=1)
    return text.rstrip() + "\n" + line + "\n"


async def _compose(job: MigrationJob, *args: str, timeout: int = 300) -> tuple[bool, str]:
    return await _run(job, ["docker", "compose", *args], cwd=str(PASARGUARD_DIR), timeout=timeout)


async def _detect_db_container(job: MigrationJob, db_type: str) -> str | None:
    ok, out = await _run(job, ["docker", "compose", "ps", "--services"], cwd=str(PASARGUARD_DIR), timeout=30)
    services = set((out or "").split())
    candidates = {
        "timescaledb": ["timescaledb"],
        "postgresql": ["postgresql", "postgres", "timescaledb"],
        "mysql": ["mysql"],
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
    return candidates[0] if candidates else None


async def _read_timescaledb_version(job: MigrationJob, container: str, password: str, user: str = "postgres") -> str | None:
    ok, out = await _run(
        job,
        [
            "docker", "compose", "exec", "-T",
            "-e", f"PGPASSWORD={password}", container,
            "psql", "-U", user, "-d", "postgres", "-At",
            "-c", "SELECT default_version FROM pg_available_extensions WHERE name = 'timescaledb';",
        ],
        cwd=str(PASARGUARD_DIR),
        timeout=30,
    )
    if not ok:
        return None
    ver = (out or "").strip().splitlines()
    return ver[-1].strip() if ver else None


async def _align_timescaledb_image(job: MigrationJob, wanted: str, *, wipe_data: bool = True) -> None:
    """Pin compose timescaledb image to backup version and optionally recreate volume.

    Matches official PasarGuard guidance:
      image: timescale/timescaledb:{backup_version}-pgXX
      rm -rf /var/lib/postgresql/pasarguard

    NEVER call with wipe_data=True after a successful dump restore — that empties the panel.
    """
    compose = PASARGUARD_DIR / "docker-compose.yml"
    wanted = parse_timescale_wanted([wanted]) or wanted
    if not compose.exists() or not wanted:
        return
    text = compose.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"timescale/timescaledb:([^\s\"']+)", text)
    current_tag = m.group(1) if m else "latest-pg17"
    pg_suf = "pg17"
    m2 = re.search(r"(pg\d+)", current_tag)
    if m2:
        pg_suf = m2.group(1)
    new_tag = f"{wanted}-{pg_suf}"
    if current_tag == new_tag:
        job.log(f"TimescaleDB image already at {new_tag}")
        return

    job.log(f"Aligning TimescaleDB image: {current_tag} → {new_tag}")
    new_text = re.sub(
        r"(image:\s*timescale/timescaledb:)[^\s\"']+",
        rf"\g<1>{new_tag}",
        text,
        count=1,
    )
    compose.write_text(new_text, encoding="utf-8")

    job.set_progress(25, "Recreating TimescaleDB with matching version...")
    await _compose(job, "stop", "pasarguard", timeout=120)
    await _compose(job, "stop", "timescaledb", "pgbouncer", timeout=120)
    data_dir = Path("/var/lib/postgresql/pasarguard")
    if wipe_data and data_dir.exists():
        job.log(f"Resetting DB data directory {data_dir} for version alignment")
        shutil.rmtree(data_dir, ignore_errors=True)
        data_dir.mkdir(parents=True, exist_ok=True)
    elif not wipe_data:
        job.log("Timescale image tag updated without wiping data volume")
    ok, out = await _compose(job, "up", "-d", "timescaledb", "pgbouncer", timeout=300)
    if not ok:
        raise RuntimeError(f"Failed to recreate TimescaleDB:\n{out[-2000:]}")
    await asyncio.sleep(10)
    # Verify extension version when possible
    cur_env = _read_current_env()
    pw = read_env_var(cur_env, "DB_PASSWORD") or read_env_var(cur_env, "POSTGRES_PASSWORD") or ""
    user = read_env_var(cur_env, "DB_USER") or "postgres"
    live = await _read_timescaledb_version(job, "timescaledb", pw, user=user)
    if live and live != wanted:
        job.log(f"Warning: live TimescaleDB={live} after align (wanted {wanted}) — continuing")
    else:
        job.log(f"TimescaleDB ready (version probe: {live or 'n/a'})")


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
    roles = []
    for r in (user, db_name):
        if r and r not in roles:
            roles.append(r)
    pg_super = read_env_var(_read_current_env(), "POSTGRES_USER")
    if pg_super and pg_super not in roles:
        roles.append(pg_super)
    lit = _sql_literal(password)
    for role in roles:
        ok, out = await _run(
            job,
            [
                "docker", "compose", "exec", "-T",
                "-e", f"PGPASSWORD={password}",
                svc, "psql", "-U", user, "-d", "postgres", "-v", "ON_ERROR_STOP=0",
                "-c", f'ALTER ROLE "{role}" WITH PASSWORD {lit};',
            ],
            cwd=str(PASARGUARD_DIR),
            timeout=30,
        )
        # Also try as postgres superuser if first attempt failed
        if not ok and user != "postgres":
            await _run(
                job,
                [
                    "docker", "compose", "exec", "-T",
                    "-e", f"PGPASSWORD={password}",
                    svc, "psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=0",
                    "-c", f'ALTER ROLE "{role}" WITH PASSWORD {lit};',
                ],
                cwd=str(PASARGUARD_DIR),
                timeout=30,
            )
        job.log(f"Synced password for role {role}")
    # Restart pgbouncer so auth cache picks up new SCRAM secrets
    await _compose(job, "restart", "pgbouncer", timeout=90)
    await asyncio.sleep(3)


async def _heal_panel_auth_if_needed(job: MigrationJob, password: str, user: str, db_name: str, db_type: str) -> None:
    """If panel crash-loops on SASL/password, re-sync roles and restart."""
    if db_type not in ("postgresql", "timescaledb", "mysql", "mariadb"):
        return
    ok, logs = await _run(
        job,
        ["docker", "compose", "logs", "--tail", "80", "pasarguard"],
        cwd=str(PASARGUARD_DIR),
        timeout=40,
    )
    blob = logs or ""
    if not is_auth_failure_text(blob):
        return
    job.log("Detected DB authentication failure in panel logs — auto-healing credentials...")
    if db_type in ("postgresql", "timescaledb"):
        svc = "timescaledb" if db_type == "timescaledb" else await _detect_db_container(job, db_type)
        if svc:
            await _sync_pg_role_passwords(job, svc, password, user or "postgres", db_name or "pasarguard")
    await _compose(job, "restart", "pasarguard", timeout=120)
    await asyncio.sleep(6)
    ok2, logs2 = await _run(
        job,
        ["docker", "compose", "logs", "--tail", "40", "pasarguard"],
        cwd=str(PASARGUARD_DIR),
        timeout=40,
    )
    if is_auth_failure_text(logs2 or ""):
        job.log("Auth still failing after heal — check DB_PASSWORD in /opt/pasarguard/.env")
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
) -> str:
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
    if not path or (backup_db == "sqlite" and not Path(path).exists()):
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

            async def _run_cmd(self, cmd, cwd=None, timeout=600):
                if isinstance(cmd, str):
                    proc = await asyncio.create_subprocess_shell(
                        cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        cwd=cwd,
                    )
                    out_b, _ = await proc.communicate()
                    out = (out_b or b"").decode("utf-8", errors="replace")
                    return proc.returncode == 0, out
                ok, out = await _run(self.job, cmd, cwd=cwd, timeout=timeout)
                return ok, out or ""

        # Target DB must be reachable with verified admin credentials before convert
        env_text = _read_current_env()
        if target_db != "sqlite":
            svc = "timescaledb" if target_db == "timescaledb" else await _detect_db_container(job, target_db)
            if svc:
                await _compose(job, "up", "-d", svc, "pgbouncer", timeout=300)
                await asyncio.sleep(5)
            probe_mini = _Mini(job, {"target_db": target_db, "_auto_db_credentials": True})
            admin = await resolve_live_admin_connection(probe_mini, target_db, env_text=env_text)
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
            mig_params = {
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
        causes_fa = [
            "رمز POSTGRES_PASSWORD در .env با رمز واقعی کانتینر TimescaleDB/PostgreSQL یکی نیست",
            "PgBouncer کش قدیمی دارد — ویزارد نقش‌ها را هم‌تراز و pgbouncer را ریستارت می‌کند",
            "بعد از ریستور postgres، globals.sql ممکن است نقش‌ها را با رمز بکاپ برگرداند",
        ]
    elif "character varying(32)" in low or "stringdatarighttruncation" in low:
        fa = "خطای ثبت نسخه alembic بعد از کپی داده (نسخه نامعتبر)."
        en = "Alembic version stamp failed after data copy (invalid revision string)."
        causes_fa = [
            "خروجی docker compose با نسخه alembic قاطی شده بود — در v2.3.5+ اصلاح شد",
            "اسکیمای target قبلاً با alembic upgrade head ساخته شده و دیگر نیاز به stamp دستی نیست",
        ]
    elif "timescale" in low and "version" in low:
        fa = "نسخه TimescaleDB بکاپ با سرور هم‌خوان نیست."
        en = "TimescaleDB version mismatch between backup and server."
        causes_fa = ["ویزارد معمولاً ایمیج را هم‌تراز می‌کند — دوباره تلاش کنید یا لاگ کامل را ببینید."]
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
            "SSL نامعتبر یا خطای اتصال به PostgreSQL/PgBouncer — لاگ واقعی ValueError/asyncpg را ببینید",
            "روی سرور: docker compose -f /opt/pasarguard/docker-compose.yml logs pasarguard --tail 80",
        ]
    elif "pasarguard failed to start" in low or "did not reach ready state" in low:
        fa = "پنل PasarGuard بعد از ریستور بالا نیامد."
        en = "PasarGuard panel did not start after restore."
        causes_fa = ["لاگ pasarguard-1 را ببینید", "ممکن است SSL یا SQLALCHEMY_DATABASE_URL اشتباه باشد"]
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
    job.log(
        f"Finalized .env for {final_db} "
        f"(URL driver: {url.split('://')[0] if '://' in url else '?'}, "
        f"SSL={'ok ' + str(cert) if ssl_ok else 'disabled/missing'})"
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
    candidates: list[Path] = []
    single = root / "db_backup.sql"
    if single.exists():
        candidates.append(single)
    pg = root / "pg_dump"
    if pg.is_dir():
        candidates.extend(p for p in pg.glob("*.sql") if p.is_file())
    for path in candidates[:8]:
        try:
            head = path.read_text(encoding="utf-8", errors="ignore")[:200_000]
        except Exception:
            continue
        if re.search(r"timescaledb", head, re.I):
            return True
    return False


def _estimate_sql_table_counts(sql_text: str) -> dict[str, int]:
    """Best-effort row estimates from pg_dump / mysqldump text."""
    from app.services.native_migration.copy_core import VERIFY_TABLES

    out: dict[str, int] = {}
    if not sql_text:
        return out
    for table in VERIFY_TABLES:
        n = 0
        # pg_dump COPY … FROM stdin; … \.
        copy_re = re.compile(
            rf"(?is)COPY\s+(?:public\.)?[`\"]?{re.escape(table)}[`\"]?\s*\([^;]*?\)\s+FROM\s+stdin;\s*(.*?)\\.\s*"
        )
        for m in copy_re.finditer(sql_text):
            block = m.group(1).strip()
            if block:
                n += sum(1 for ln in block.splitlines() if ln.strip())
        # INSERT INTO table / `table` / "table"
        insert_re = re.compile(
            rf"(?i)INSERT\s+INTO\s+[`\"\[]?{re.escape(table)}[`\"\]]?\s*(?:\([^)]*\))?\s*VALUES\s*",
        )
        for m in insert_re.finditer(sql_text):
            # Count value tuples after VALUES — approximate by top-level '(' groups until ';'
            rest = sql_text[m.end(): m.end() + 500_000]
            end = rest.find(";")
            chunk = rest if end < 0 else rest[:end]
            tuples = len(re.findall(r"\(", chunk))
            n += max(tuples, 1)
        if n > 0:
            out[table] = n
    return out


def _estimate_backup_table_counts(root: Path, layout: str | None = None) -> dict[str, int]:
    """Estimate critical table row counts from backup files before restore."""
    if layout == "sqlite_file" or (root / "db.sqlite3").exists() or list(root.rglob("db.sqlite3")):
        src = root / "db.sqlite3"
        if not src.exists():
            found = list(root.rglob("db.sqlite3"))
            src = found[0] if found else None
        if src and src.exists():
            return _snapshot_sqlite_counts(src)

    chunks: list[str] = []
    single = root / "db_backup.sql"
    if single.exists():
        try:
            chunks.append(single.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            pass
    manifest = root / "pg_dump" / "manifest.tsv"
    if manifest.exists():
        for line in manifest.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            dump_path = root / "pg_dump" / parts[3]
            if dump_path.exists():
                try:
                    chunks.append(dump_path.read_text(encoding="utf-8", errors="ignore"))
                except Exception:
                    pass
    if not chunks:
        for p in (root / "pg_dump").glob("*.sql") if (root / "pg_dump").is_dir() else []:
            try:
                chunks.append(p.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                pass

    merged: dict[str, int] = {}
    for text in chunks:
        for k, v in _estimate_sql_table_counts(text).items():
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
        mysql_cmd = "mariadb" if final_db == "mariadb" else "mysql"
        for table in tables_to_check:
            safe = "".join(c for c in table if c.isalnum() or c == "_")
            if safe != table:
                continue
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
                        break
            elif table in expected:
                raise RuntimeError(
                    f"Could not COUNT {table} after restore — verification failed hard."
                )
    else:
        raise RuntimeError(f"Unsupported final_db for verification: {final_db}")

    gaps = []
    for table, want in expected.items():
        got = actual.get(table, -1)
        if got < 0:
            gaps.append(f"{table}: unreadable/{want}")
        elif got < want:
            gaps.append(f"{table}: {got}/{want}")
        else:
            job.log(f"Verified {table}: {got} rows (expected ≥{want})")

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
            + ", ".join(f"{k}={v}" for k, v in actual.items() if v > 0) or "all empty"
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
        root = _find_backup_root(work)
        backup_env_path = _find_env(root)
        if not backup_env_path:
            raise RuntimeError("Backup .env missing")
        backup_env = backup_env_path.read_text(encoding="utf-8", errors="ignore")
        # Prefer analyze() result (timescale manifest override); never trust live compose for backup label
        backup_db = analysis.get("backup_db") or detect_db_type_from_env(backup_env, prefer_compose=False)
        current_env = _read_current_env()
        install_env_snapshot = current_env
        installed_db = detect_db_type_from_env(current_env) or get_pasarguard_db_type()

        job.log(f"Backup DB={backup_db}, installed DB={installed_db}, layout={analysis.get('layout')}")

        # Preserve CURRENT live credentials (password mismatch fix)
        cur_url = read_env_var(current_env, "SQLALCHEMY_DATABASE_URL")
        cur_db_pass = read_env_var(current_env, "DB_PASSWORD")
        cur_mysql_root = read_env_var(current_env, "MYSQL_ROOT_PASSWORD")
        cur_user = read_env_var(current_env, "DB_USER")
        cur_name = read_env_var(current_env, "DB_NAME")
        cur_pg_pass = read_env_var(current_env, "POSTGRES_PASSWORD") or cur_db_pass

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

        # TimescaleDB version alignment BEFORE restore only (may wipe empty/new volume)
        ts_versions = analysis.get("timescaledb_versions") or []
        wanted_ts = parse_timescale_wanted(ts_versions)
        if installed_db in ("timescaledb", "postgresql") or backup_db in ("timescaledb", "postgresql"):
            if wanted_ts and (installed_db == "timescaledb" or soft_db_family(backup_db, "timescaledb")):
                container = await _detect_db_container(job, installed_db or "timescaledb")
                live_ver = None
                if container:
                    live_ver = await _read_timescaledb_version(
                        job, container,
                        cur_pg_pass or "",
                        user=cur_user or "postgres",
                    )
                if live_ver and live_ver != wanted_ts:
                    job.log(f"TimescaleDB mismatch: live={live_ver} backup={wanted_ts}")
                    await _align_timescaledb_image(job, wanted_ts, wipe_data=True)
                elif not live_ver and installed_db == "timescaledb":
                    job.log("Could not probe live TimescaleDB — pinning image to backup version")
                    await _align_timescaledb_image(job, wanted_ts, wipe_data=True)

        # Destination = installed panel DB. Soft-family (mysql↔mariadb, pg↔timescale) needs no convert.
        target_db = installed_db or params.get("target_db") or backup_db
        needs_convert = bool(
            backup_db and target_db
            and backup_db != target_db
            and not soft_db_family(backup_db, target_db)
        )
        if needs_convert:
            job.log(f"DB mismatch — will auto-convert {backup_db} → {target_db}")

        job.set_progress(40, "Restoring database...")
        await _compose(job, "stop", "pasarguard", timeout=120)

        restore_engine = backup_db
        if backup_db == "sqlite" or analysis.get("layout") == "sqlite_file":
            await _restore_sqlite(job, root)
            expected_counts = _snapshot_sqlite_counts(PASARGUARD_DATA / "db.sqlite3") or expected_counts
            if expected_counts:
                job.log(
                    "Backup SQLite counts: "
                    + ", ".join(f"{k}={v}" for k, v in expected_counts.items())
                )
        elif backup_db in ("mysql", "mariadb"):
            if needs_convert:
                dump = root / "db_backup.sql"
                if not dump.exists():
                    raise RuntimeError("db_backup.sql missing — cannot convert without dump")
                job.log(
                    f"Hard convert path: skip native {backup_db} container restore; "
                    f"will import dump → {target_db}"
                )
            else:
                await _restore_mysql(job, root, backup_db, current_env, backup_env)
        elif backup_db in ("postgresql", "timescaledb"):
            if needs_convert:
                dump = root / "db_backup.sql"
                if not dump.exists() and analysis.get("layout") != "multi":
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
                )
                svc = await _detect_db_container(job, restore_into or installed_db or backup_db)
                if svc and (cur_pg_pass or cur_db_pass):
                    await _sync_pg_role_passwords(
                        job, svc, cur_pg_pass or cur_db_pass or "",
                        cur_user or "postgres", cur_name or "pasarguard",
                    )
        else:
            raise RuntimeError(f"Unsupported backup database: {backup_db}")

        job.set_progress(75, "Merging configuration...")
        preserve: dict[str, str | None] = {
            "DB_PASSWORD": cur_db_pass,
            "MYSQL_ROOT_PASSWORD": cur_mysql_root,
            "DB_USER": cur_user,
            "DB_NAME": cur_name,
            "POSTGRES_PASSWORD": cur_pg_pass,
        }
        if not needs_convert and cur_url:
            preserve["SQLALCHEMY_DATABASE_URL"] = cur_url

        await _merge_env_after_restore(
            job, backup_env, install_env_snapshot,
            preserve=preserve,
        )

        await _restore_data_files(job, root)

        # Source path for cross-DB convert (must exist before workdir cleanup)
        convert_source: str | None = None
        if restore_engine == "sqlite" or analysis.get("layout") == "sqlite_file":
            convert_source = str(PASARGUARD_DATA / "db.sqlite3")
        else:
            dump = root / "db_backup.sql"
            if dump.exists():
                convert_source = str(dump)
            elif analysis.get("layout") == "multi":
                # Prefer primary dump from manifest
                manifest = root / "pg_dump" / "manifest.tsv"
                if manifest.exists():
                    for line in manifest.read_text(encoding="utf-8", errors="ignore").splitlines():
                        parts = line.split("\t")
                        if len(parts) >= 4:
                            cand = root / "pg_dump" / parts[3]
                            if cand.exists():
                                convert_source = str(cand)
                                break

        final_db = restore_engine
        copy_stats: dict = {}
        copy_report: dict = {}
        if target_db and restore_engine != target_db and not soft_db_family(restore_engine, target_db):
            final_db, copy_stats, copy_report = await _maybe_cross_db_after_restore(
                job, params, restore_engine, target_db,
                cur_db_pass or cur_pg_pass or "",
                cur_user or "pasarguard",
                cur_name or "pasarguard",
                source_path=convert_source,
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

        # After convert: force DB roles to match the password we keep in .env (install)
        # Convert may have temporarily synced a different resolved password.
        final_engine_pre = final_db or target_db or restore_engine or backup_db
        live_admin = (copy_report or {}).get("live_admin") or {}
        verify_user = (
            cur_user
            or live_admin.get("user")
            or "pasarguard"
        )
        verify_pass = cur_db_pass or cur_pg_pass or live_admin.get("password") or ""
        verify_db = cur_name or live_admin.get("database") or "pasarguard"
        if (
            final_engine_pre in ("postgresql", "timescaledb")
            and verify_pass
            and (needs_convert or soft_db_family(restore_engine, target_db))
        ):
            svc = await _detect_db_container(job, final_engine_pre)
            if svc:
                job.log(
                    f"Aligning DB roles to install password (user={verify_user}) "
                    "so .env and Timescale/Postgres match"
                )
                await _sync_pg_role_passwords(
                    job, svc, verify_pass, verify_user, verify_db,
                )

        job.set_progress(88, "Finalizing .env for target database...")
        await _finalize_env_after_restore(
            job,
            install_env_snapshot,
            final_db or target_db or restore_engine or backup_db,
            verify_pass,
            verify_user,
            verify_db,
        )
        _env_completeness_checklist(
            job,
            final_db or target_db or restore_engine or backup_db,
            backup_env,
        )

        if final_db and final_db != "sqlite" and (needs_convert or restore_engine == "sqlite"):
            _relocate_sqlite_after_convert(job)

        job.set_progress(90, "Starting PasarGuard...")
        # Force recreate so panel picks up finalized .env (DB URL / SSL)
        ok, out = await _compose(job, "up", "-d", "--force-recreate", "pasarguard", timeout=300)
        if not ok:
            job.log(f"compose recreate warning: {out[-1500:]}")
            ok, out = await _compose(job, "up", "-d", timeout=300)
        if not ok:
            # Do NOT wipe Timescale volume here — that caused empty-panel false success
            job.log(f"compose up warning: {out[-1500:]}")
            mismatch = detect_ts_mismatch_from_text(out)
            if mismatch:
                job.log(
                    f"Timescale mismatch noted ({mismatch[0]} vs {mismatch[1]}) — "
                    "retag only, no data wipe after restore"
                )
                await _align_timescaledb_image(job, mismatch[0], wipe_data=False)
                ok, out = await _compose(job, "up", "-d", timeout=300)
                if not ok:
                    raise RuntimeError(f"PasarGuard failed to start after restore:\n{out[-2000:]}")
        await asyncio.sleep(8)

        await _heal_panel_auth_if_needed(
            job,
            verify_pass,
            verify_user,
            verify_db,
            final_db or restore_engine or "",
        )

        # Best-effort schema align — use live credentials, never blind postgres/default
        final_engine = final_db or installed_db or backup_db
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
        # Convert already upgraded to head + pinned alembic_version — only heal if needed
        if not needs_convert:
            try:
                from app.services.pasarguard_ops import sync_alembic_for_startup

                await sync_alembic_for_startup(mini, final_engine)
            except Exception as e:
                job.log(f"Alembic sync note: {e}")
        else:
            job.log("Skipping full alembic re-sync after convert (schema already at head)")

        # Re-read password from finalized .env in case finalize adjusted it
        env_now = _read_current_env()
        verify_pass = (
            read_env_var(env_now, "DB_PASSWORD")
            or read_env_var(env_now, "POSTGRES_PASSWORD")
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
            require_any_data=bool(expected_counts) or bool(analysis.get("table_counts")),
        )

        from app.services.pasarguard_ops import verify_pasarguard_healthy

        await verify_pasarguard_healthy(mini)

        access = get_panel_access_info()
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


async def _restore_sqlite(job: MigrationJob, root: Path) -> None:
    src = root / "db.sqlite3"
    if not src.exists():
        found = list(root.rglob("db.sqlite3"))
        src = found[0] if found else None
    if not src or not src.exists():
        # sometimes under var/lib path in archive
        raise RuntimeError("db.sqlite3 not found in backup")
    PASARGUARD_DATA.mkdir(parents=True, exist_ok=True)
    dest = PASARGUARD_DATA / "db.sqlite3"
    if dest.exists():
        shutil.copy2(dest, dest.with_suffix(".sqlite3.bak-before-restore"))
    shutil.copy2(src, dest)
    job.log(f"SQLite restored → {dest}")


async def _restore_mysql(
    job: MigrationJob, root: Path, db_type: str, current_env: str, backup_env: str
) -> None:
    dump = root / "db_backup.sql"
    if not dump.exists():
        raise RuntimeError("db_backup.sql missing")
    svc = await _detect_db_container(job, db_type)
    if not svc:
        raise RuntimeError("MySQL/MariaDB container not found")

    root_pw = read_env_var(current_env, "MYSQL_ROOT_PASSWORD") or read_env_var(backup_env, "MYSQL_ROOT_PASSWORD")
    db_user = read_env_var(current_env, "DB_USER") or read_env_var(backup_env, "DB_USER") or "root"
    db_pass = read_env_var(current_env, "DB_PASSWORD") or read_env_var(backup_env, "DB_PASSWORD")
    db_name = read_env_var(current_env, "DB_NAME") or read_env_var(backup_env, "DB_NAME") or "pasarguard"
    mysql_cmd = "mariadb" if db_type == "mariadb" else "mysql"

    await _compose(job, "up", "-d", svc, timeout=180)
    await asyncio.sleep(5)

    attempts = []
    if root_pw:
        attempts.append(("root", root_pw, None))
    if db_user and db_pass:
        attempts.append((db_user, db_pass, db_name))
        attempts.append((db_user, db_pass, None))
    # also try backup passwords if different
    b_root = read_env_var(backup_env, "MYSQL_ROOT_PASSWORD")
    b_pass = read_env_var(backup_env, "DB_PASSWORD")
    if b_root and b_root != root_pw:
        attempts.append(("root", b_root, None))
    if b_pass and b_pass != db_pass:
        attempts.append((db_user, b_pass, db_name))

    last_err = ""
    for user, pwd, db in attempts:
        cmd = ["docker", "compose", "exec", "-T", "-e", f"MYSQL_PWD={pwd}", svc, mysql_cmd, "-u", user]
        if db:
            cmd.append(db)
        job.log(f"Trying MySQL restore as {user}" + (f"/{db}" if db else ""))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(PASARGUARD_DIR),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        data = dump.read_bytes()
        out_b, _ = await proc.communicate(input=data)
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
    await _compose(job, "up", "-d", svc, timeout=180)
    # pgbouncer if present
    await _compose(job, "up", "-d", "pgbouncer", timeout=120)
    await asyncio.sleep(6)

    password = (
        read_env_var(current_env, "DB_PASSWORD")
        or read_env_var(current_env, "POSTGRES_PASSWORD")
        or read_env_var(backup_env, "DB_PASSWORD")
        or read_env_var(backup_env, "POSTGRES_PASSWORD")
        or ""
    )
    user = read_env_var(current_env, "DB_USER") or read_env_var(backup_env, "DB_USER") or "postgres"
    db_name = read_env_var(current_env, "DB_NAME") or read_env_var(backup_env, "DB_NAME") or "pasarguard"

    if not password:
        raise RuntimeError("No database password available for PostgreSQL restore")

    async def psql(sql: str, db: str = "postgres", use_file: Path | None = None) -> tuple[bool, str]:
        cmd = [
            "docker", "compose", "exec", "-T",
            "-e", f"PGPASSWORD={password}",
            svc, "psql", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", db,
        ]
        if use_file:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(PASARGUARD_DIR),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            data = use_file.read_bytes()
            out_b, _ = await proc.communicate(input=data)
            return proc.returncode == 0, (out_b or b"").decode("utf-8", errors="replace")
        proc = await asyncio.create_subprocess_exec(
            *cmd, "-c", sql,
            cwd=str(PASARGUARD_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out_b, _ = await proc.communicate()
        return proc.returncode == 0, (out_b or b"").decode("utf-8", errors="replace")

    layout = analysis.get("layout")
    manifest = root / "pg_dump" / "manifest.tsv"
    restored_any = False
    use_timescale = db_type == "timescaledb" or bool(analysis.get("timescaledb_versions"))
    if layout == "multi" and manifest.exists():
        globals_sql = root / "pg_dump" / "globals.sql"
        if globals_sql.exists():
            job.log("Restoring globals...")
            await psql("", use_file=globals_sql)

        for line in manifest.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            dbn, owner, has_ts, filename = parts[0], parts[1], parts[2], parts[3]
            dump_path = root / "pg_dump" / filename
            if not dump_path.exists():
                raise RuntimeError(f"Missing dump file in backup: pg_dump/{filename}")
            job.log(f"Restoring database {dbn}...")
            await psql(
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{dbn}' AND pid <> pg_backend_pid();"
            )
            await psql(f'DROP DATABASE IF EXISTS "{dbn}";')
            owner_q = owner or user
            ok, out = await psql(f'CREATE DATABASE "{dbn}" OWNER "{owner_q}";')
            if not ok:
                raise RuntimeError(f"CREATE DATABASE {dbn} failed:\n{out[-1000:]}")
            filtered = None
            if has_ts == "1" or use_timescale:
                await psql("CREATE EXTENSION IF NOT EXISTS timescaledb;", db=dbn)
                await psql("SELECT timescaledb_pre_restore();", db=dbn)
                filtered = dump_path.with_suffix(dump_path.suffix + ".filtered")
                filtered.write_text(
                    filter_timescaledb_extension_sql(
                        dump_path.read_text(encoding="utf-8", errors="ignore")
                    ),
                    encoding="utf-8",
                )
                ok, out = await psql("", db=dbn, use_file=filtered)
                await psql("SELECT timescaledb_post_restore();", db=dbn)
            else:
                ok, out = await psql("", db=dbn, use_file=dump_path)
            if not ok:
                # Align BEFORE retry — wipe only when this DB was just created empty
                mismatch = detect_ts_mismatch_from_text(out)
                if mismatch or ("timescaledb" in (out or "").lower() and "version" in (out or "").lower()):
                    wanted = (mismatch[0] if mismatch else parse_timescale_wanted(analysis.get("timescaledb_versions"))) or ""
                    if wanted:
                        job.log(f"Timescale restore error — aligning to {wanted} and retrying {dbn}")
                        await _align_timescaledb_image(job, wanted, wipe_data=True)
                        await _compose(job, "up", "-d", svc, timeout=180)
                        await asyncio.sleep(8)
                        await psql(f'CREATE DATABASE "{dbn}" OWNER "{owner_q}";')
                        await psql("CREATE EXTENSION IF NOT EXISTS timescaledb;", db=dbn)
                        await psql("SELECT timescaledb_pre_restore();", db=dbn)
                        ok, out = await psql("", db=dbn, use_file=filtered if filtered else dump_path)
                        await psql("SELECT timescaledb_post_restore();", db=dbn)
                if not ok:
                    raise RuntimeError(f"Failed restoring {dbn}:\n{out[-2000:]}")
            restored_any = True
            job.log(f"Database {dbn} restored")
        if not restored_any:
            raise RuntimeError("Multi-dump restore finished with zero databases restored")
        return

    # Legacy single dump
    dump = root / "db_backup.sql"
    if not dump.exists():
        raise RuntimeError("db_backup.sql missing")
    await psql(
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid();"
    )
    await psql(f'DROP DATABASE IF EXISTS "{db_name}";')
    ok, out = await psql(f'CREATE DATABASE "{db_name}" OWNER "{user}";')
    if not ok:
        raise RuntimeError(f"CREATE DATABASE failed: {out[-1000:]}")
    if use_timescale or db_type == "timescaledb":
        await psql("CREATE EXTENSION IF NOT EXISTS timescaledb;", db=db_name)
        await psql("SELECT timescaledb_pre_restore();", db=db_name)
        filtered = root / "db_backup_filtered.sql"
        filtered.write_text(
            filter_timescaledb_extension_sql(dump.read_text(encoding="utf-8", errors="ignore")),
            encoding="utf-8",
        )
        ok, out = await psql("", db=db_name, use_file=filtered)
        await psql("SELECT timescaledb_post_restore();", db=db_name)
    else:
        ok, out = await psql("", db=db_name, use_file=dump)
    if not ok:
        raise RuntimeError(f"PostgreSQL dump restore failed:\n{out[-2000:]}")
    job.log(f"PostgreSQL dump restored into {db_name}")
    return


async def _merge_env_after_restore(
    job: MigrationJob, backup_env: str, current_env: str, preserve: dict
) -> None:
    """Write backup .env (panel settings) but keep live DB credentials.

    App settings from backup win (ports, telegram, subscription) — this is the
    previous panel. Install only fills missing keys and provides DB auth.
    """
    text = backup_env
    # Only fill panel listen settings if backup omitted them
    for key in ("UVICORN_PORT", "UVICORN_HOST", "UVICORN_ROOT_PATH", "ALLOWED_ORIGINS"):
        if read_env_var(text, key):
            continue
        cur = read_env_var(current_env, key)
        if cur is not None:
            text = _set_env_var(text, key, cur)

    for key, val in preserve.items():
        if val:
            text = _set_env_var(text, key, val)

    db_pass = preserve.get("DB_PASSWORD") or preserve.get("POSTGRES_PASSWORD")
    if db_pass:
        text = _set_env_var(text, "DB_PASSWORD", db_pass)
        if "POSTGRES_PASSWORD" in current_env or read_env_var(current_env, "POSTGRES_PASSWORD"):
            text = _set_env_var(text, "POSTGRES_PASSWORD", db_pass)

    if PASARGUARD_ENV.exists():
        shutil.copy2(PASARGUARD_ENV, PASARGUARD_ENV.with_suffix(".env.bak-before-restore"))
    PASARGUARD_ENV.write_text(text, encoding="utf-8")
    job.log("Merged .env (backup app settings; DB URL finalized after convert)")


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
