"""Smart PasarGuard backup restore (fixes version/password pitfalls)."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import traceback
import zipfile
from pathlib import Path
from typing import Callable

from app.config import PASARGUARD_DIR, PASARGUARD_ENV, PASARGUARD_DATA, UPLOAD_DIR
from app.services.env_migration import (
    detect_db_type_from_env,
    extract_env_summary,
    read_env_var,
)
from app.services.migrators.base import MigrationJob
from app.services.pg_access import get_panel_access_info
from app.services.prerequisites import is_pasarguard_installed, get_pasarguard_db_type
from app.services.upload import get_upload_path

PASARGUARD_BACKUP_DIR = PASARGUARD_DIR / "backup"
_restore_jobs: dict[str, MigrationJob] = {}


def get_restore_job(job_id: str) -> MigrationJob | None:
    return _restore_jobs.get(job_id)


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
        db_type = detect_db_type_from_env(env_text) if env_text else None
        summary = extract_env_summary(env_text) if env_text else None

        layout = "none"
        if (root / "pg_dump" / "manifest.tsv").exists():
            layout = "multi"
        elif (root / "db_backup.sql").exists():
            layout = "single"
        elif (root / "db.sqlite3").exists() or list(root.rglob("db.sqlite3")):
            layout = "sqlite_file"

        ts_versions = _parse_manifest_ts_versions(root)
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
        elif db_type and installed_db and db_type != installed_db:
            # mysql≈mariadb soft mismatch
            soft = {db_type, installed_db} <= {"mysql", "mariadb"} or {db_type, installed_db} <= {"postgresql", "timescaledb"}
            if not soft:
                ok = False
                warnings.append({
                    "en": f"Database mismatch: backup={db_type}, installed={installed_db}. Install PasarGuard with the SAME database as the backup.",
                    "fa": f"ناسازگاری دیتابیس: بکاپ={db_type}، نصب={installed_db}. باید همان نوع دیتابیس بکاپ نصب شود.",
                    "ru": f"Несовпадение БД: backup={db_type}, installed={installed_db}. Установите ту же СУБД, что в бэкапе.",
                })
            else:
                warnings.append({
                    "en": f"Related DB engines: backup={db_type}, installed={installed_db} — restore will proceed carefully.",
                    "fa": f"موتورهای مرتبط: بکاپ={db_type}، نصب={installed_db} — ریستور با احتیاط ادامه می‌یابد.",
                    "ru": f"Смежные СУБД: backup={db_type}, installed={installed_db}.",
                })

        if layout == "none" and db_type != "sqlite":
            ok = False
            warnings.append({
                "en": "No database dump found in backup (expected db_backup.sql or pg_dump/)",
                "fa": "دامپ دیتابیس در بکاپ یافت نشد",
                "ru": "Дамп БД в бэкапе не найден",
            })

        if ts_versions:
            warnings.append({
                "en": f"Backup TimescaleDB version(s): {', '.join(sorted(set(ts_versions)))}. Wizard will auto-align the server image if needed.",
                "fa": f"نسخه TimescaleDB بکاپ: {', '.join(sorted(set(ts_versions)))}. در صورت نیاز نسخه ایمیج سرور هم‌تراز می‌شود.",
                "ru": f"Версия TimescaleDB в бэкапе: {', '.join(sorted(set(ts_versions)))}.",
            })

        return {
            "ok": ok,
            "filename": zip_path.name,
            "size": zip_path.stat().st_size,
            "backup_db": db_type,
            "installed_db": installed_db,
            "db_match": (db_type == installed_db) if (db_type and installed_db) else None,
            "layout": layout,
            "timescaledb_versions": sorted(set(ts_versions)),
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
        job.status = "error"
        job.message = str(e)
        job.log(f"ERROR: {e}")
        job.log(traceback.format_exc())
        job.result = {"error": str(e)}


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


def _read_current_env() -> str:
    return PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore") if PASARGUARD_ENV.exists() else ""


def _set_env_var(text: str, key: str, value: str) -> str:
    pattern = rf"(?m)^\s*#?\s*{re.escape(key)}\s*=.*$"
    line = f'{key}="{value}"'
    if re.search(pattern, text):
        return re.sub(pattern, line, text, count=1)
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


async def _align_timescaledb_image(job: MigrationJob, wanted: str) -> None:
    """Pin compose timescaledb image to backup version and recreate volume."""
    compose = PASARGUARD_DIR / "docker-compose.yml"
    if not compose.exists() or not wanted:
        return
    text = compose.read_text(encoding="utf-8", errors="ignore")
    # Detect pg major from current image tag like latest-pg17 or 2.17.2-pg17
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
    # Wipe DB data dir so extension version matches fresh init
    data_dir = Path("/var/lib/postgresql/pasarguard")
    if data_dir.exists():
        job.log(f"Resetting DB data directory {data_dir} for version alignment")
        shutil.rmtree(data_dir, ignore_errors=True)
        data_dir.mkdir(parents=True, exist_ok=True)
    ok, out = await _compose(job, "up", "-d", "timescaledb", "pgbouncer", timeout=300)
    if not ok:
        raise RuntimeError(f"Failed to recreate TimescaleDB:\n{out[-2000:]}")
    await asyncio.sleep(8)


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
        backup_db = detect_db_type_from_env(backup_env) or analysis.get("backup_db")
        current_env = _read_current_env()
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

        # TimescaleDB version alignment
        ts_versions = analysis.get("timescaledb_versions") or []
        if installed_db in ("timescaledb", "postgresql") and ts_versions:
            container = await _detect_db_container(job, installed_db or "timescaledb")
            if container:
                # Prefer service name via compose exec
                svc = "timescaledb" if installed_db == "timescaledb" else container
                live_ver = await _read_timescaledb_version(
                    job, svc if installed_db == "timescaledb" else container,
                    cur_pg_pass or "",
                    user=cur_user or "postgres",
                )
                wanted = ts_versions[0]
                if live_ver and live_ver != wanted:
                    job.log(f"TimescaleDB mismatch: live={live_ver} backup={wanted}")
                    await _align_timescaledb_image(job, wanted)
                elif not live_ver:
                    job.log("Could not probe live TimescaleDB version — pinning image to backup version")
                    await _align_timescaledb_image(job, wanted)

        job.set_progress(40, "Restoring database...")
        await _compose(job, "stop", "pasarguard", timeout=120)

        if backup_db == "sqlite" or analysis.get("layout") == "sqlite_file":
            await _restore_sqlite(job, root)
        elif backup_db in ("mysql", "mariadb"):
            await _restore_mysql(job, root, backup_db, current_env, backup_env)
        elif backup_db in ("postgresql", "timescaledb"):
            await _restore_postgres(job, root, backup_db, current_env, backup_env, analysis)
        else:
            raise RuntimeError(f"Unsupported backup database: {backup_db}")

        job.set_progress(75, "Merging configuration...")
        await _merge_env_after_restore(
            job, backup_env, current_env,
            preserve={
                "SQLALCHEMY_DATABASE_URL": cur_url,
                "DB_PASSWORD": cur_db_pass,
                "MYSQL_ROOT_PASSWORD": cur_mysql_root,
                "DB_USER": cur_user,
                "DB_NAME": cur_name,
                "POSTGRES_PASSWORD": cur_pg_pass,
            },
        )

        # Copy non-db data files (certs, xray, etc.) carefully
        await _restore_data_files(job, root)

        job.set_progress(90, "Starting PasarGuard...")
        ok, out = await _compose(job, "up", "-d", timeout=300)
        if not ok:
            job.log(f"compose up warning: {out[-1500:]}")
        await asyncio.sleep(5)

        # Best-effort schema align
        try:
            from app.services.pasarguard_ops import sync_alembic_for_startup

            class _Mini:
                def __init__(self, j, p):
                    self.job = j
                    self.params = p
                async def _run_cmd(self, cmd, cwd=None, timeout=600):
                    return await _run(self.job, cmd, cwd=cwd, timeout=timeout)

            mini = _Mini(job, {
                "target_db": installed_db or backup_db,
                "target_db_password": cur_db_pass or cur_pg_pass,
                "target_db_user": cur_user,
                "target_db_name": cur_name or "pasarguard",
                "target_db_host": "127.0.0.1",
            })
            await sync_alembic_for_startup(mini, installed_db or backup_db)
        except Exception as e:
            job.log(f"Alembic sync note: {e}")

        access = get_panel_access_info()
        access["restored"] = True
        access["backup_db"] = backup_db
        access["staged_backup"] = str(staged)
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
    svc = "timescaledb" if db_type == "timescaledb" else await _detect_db_container(job, db_type)
    if not svc:
        svc = "postgresql"
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
                job.log(f"Missing dump {filename}, skip")
                continue
            job.log(f"Restoring database {dbn}...")
            await psql(
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{dbn}' AND pid <> pg_backend_pid();"
            )
            await psql(f'DROP DATABASE IF EXISTS "{dbn}";')
            owner_q = owner or user
            ok, out = await psql(f'CREATE DATABASE "{dbn}" OWNER "{owner_q}";')
            if not ok:
                job.log(f"CREATE DATABASE failed: {out[-400:]}")
                continue
            if has_ts == "1":
                await psql("CREATE EXTENSION IF NOT EXISTS timescaledb;", db=dbn)
                await psql("SELECT timescaledb_pre_restore();", db=dbn)
                filtered = dump_path.with_suffix(dump_path.suffix + ".filtered")
                filtered.write_text(
                    "\n".join(
                        ln for ln in dump_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                        if not re.search(r"^\s*(DROP|CREATE)\s+EXTENSION\s+(IF\s+(EXISTS|NOT\s+EXISTS)\s+)?timescaledb\b", ln, re.I)
                    ),
                    encoding="utf-8",
                )
                ok, out = await psql("", db=dbn, use_file=filtered)
                await psql("SELECT timescaledb_post_restore();", db=dbn)
            else:
                ok, out = await psql("", db=dbn, use_file=dump_path)
            if not ok:
                raise RuntimeError(f"Failed restoring {dbn}:\n{out[-2000:]}")
            job.log(f"Database {dbn} restored")
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
    if db_type == "timescaledb":
        await psql("CREATE EXTENSION IF NOT EXISTS timescaledb;", db=db_name)
        await psql("SELECT timescaledb_pre_restore();", db=db_name)
        filtered = root / "db_backup_filtered.sql"
        filtered.write_text(
            "\n".join(
                ln for ln in dump.read_text(encoding="utf-8", errors="ignore").splitlines()
                if not re.search(r"^\s*(DROP|CREATE)\s+EXTENSION\s+(IF\s+(EXISTS|NOT\s+EXISTS)\s+)?timescaledb\b", ln, re.I)
            ),
            encoding="utf-8",
        )
        ok, out = await psql("", db=db_name, use_file=filtered)
        await psql("SELECT timescaledb_post_restore();", db=db_name)
    else:
        ok, out = await psql("", db=db_name, use_file=dump)
    if not ok:
        raise RuntimeError(f"PostgreSQL restore failed:\n{out[-2000:]}")
    job.log("PostgreSQL dump restored")


async def _merge_env_after_restore(
    job: MigrationJob, backup_env: str, current_env: str, preserve: dict
) -> None:
    """Write backup .env but keep live DB credentials so containers still auth."""
    # Start from backup (app settings, telegram, etc.)
    text = backup_env
    # Keep SSL/port from CURRENT install (this server's certs/ports)
    for key in (
        "UVICORN_SSL_CERTFILE", "UVICORN_SSL_KEYFILE", "UVICORN_SSL_CA_TYPE",
        "UVICORN_PORT", "UVICORN_HOST", "UVICORN_ROOT_PATH", "ALLOWED_ORIGINS",
    ):
        cur = read_env_var(current_env, key)
        if cur is not None:
            text = _set_env_var(text, key, cur)

    for key, val in preserve.items():
        if val:
            text = _set_env_var(text, key, val)

    # Ensure POSTGRES_PASSWORD mirrors DB_PASSWORD when needed
    db_pass = preserve.get("DB_PASSWORD") or preserve.get("POSTGRES_PASSWORD")
    if db_pass:
        text = _set_env_var(text, "DB_PASSWORD", db_pass)
        if "POSTGRES_PASSWORD" in current_env or "timescaledb" in (preserve.get("SQLALCHEMY_DATABASE_URL") or ""):
            text = _set_env_var(text, "POSTGRES_PASSWORD", db_pass)

    if PASARGUARD_ENV.exists():
        shutil.copy2(PASARGUARD_ENV, PASARGUARD_ENV.with_suffix(".env.bak-before-restore"))
    PASARGUARD_ENV.write_text(text, encoding="utf-8")
    job.log("Merged .env (preserved current DB credentials & SSL/port)")


async def _restore_data_files(job: MigrationJob, root: Path) -> None:
    """Copy certs/templates/xray-like dirs from backup without clobbering DB files."""
    skip_names = {
        ".env", "db_backup.sql", "db_backup_filtered.sql", "db.sqlite3",
        "docker-compose.yml", "pg_dump",
    }
    for item in root.iterdir():
        if item.name in skip_names or item.name.startswith("pasarguard_"):
            continue
        if item.name.endswith(".sql") or item.name.endswith(".filtered"):
            continue
        dest = PASARGUARD_DIR / item.name
        try:
            if item.is_dir():
                if dest.exists():
                    # merge copy
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

    # Data dir pieces (excluding sqlite already handled)
    data_src = root / "var" / "lib" / "pasarguard"
    if not data_src.exists():
        alt = list(root.rglob("xray_config.json"))
        # optional
    if data_src.exists():
        PASARGUARD_DATA.mkdir(parents=True, exist_ok=True)
        for item in data_src.iterdir():
            if item.name == "db.sqlite3":
                continue
            dest = PASARGUARD_DATA / item.name
            try:
                if item.is_dir():
                    if dest.exists():
                        shutil.rmtree(dest, ignore_errors=True)
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)
            except Exception as e:
                job.log(f"Skip data {item.name}: {e}")
    job.log("App/data files restored (best-effort)")
