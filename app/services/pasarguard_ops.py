"""Non-interactive PasarGuard Docker operations (no hanging CLI)."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from pathlib import Path

from app.config import PASARGUARD_DIR, PASARGUARD_ENV, PASARGUARD_DATA
from app.services.db_credentials import get_target_connection

STARTUP_MARKERS = (
    "Application startup complete",
    "Uvicorn running",
)

FAIL_LOG_PATTERNS = (
    "Database migrations failed",
    "ERROR: Database migrations failed",
    "sqlalchemy.exc.",
    "asyncpg.exceptions.DuplicateColumnError",
    "DuplicateColumnError",
    "ProgrammingError",
    "Traceback (most recent call last)",
    "FATAL:",
    "could not connect",
    "column \"user_template_id\" of relation \"next_plans\" already exists",
)

DB_SERVICES = {
    "timescaledb": ("timescaledb", "postgresql"),
    "postgresql": ("postgresql", "timescaledb"),
    "mysql": ("mysql",),
    "mariadb": ("mariadb", "mysql"),
    "sqlite": tuple(),
}


def _compose_text() -> str:
    compose = PASARGUARD_DIR / "docker-compose.yml"
    return compose.read_text(encoding="utf-8", errors="ignore") if compose.exists() else ""


def resolve_db_service(target_db: str) -> str | None:
    if target_db == "sqlite":
        return None
    text = _compose_text()
    for name in DB_SERVICES.get(target_db, (target_db,)):
        if name and re.search(rf"^\s*{re.escape(name)}\s*:", text, re.MULTILINE):
            return name
    return DB_SERVICES.get(target_db, (None,))[0]


def _target_conn(migrator) -> dict:
    return get_target_connection(migrator.params)


def _log_failures_from_output(migrator, output: str) -> None:
    for line in (output or "").splitlines():
        if any(p in line for p in FAIL_LOG_PATTERNS):
            migrator.job.log(line)


def _extract_failure_snippet(output: str) -> str:
    lines = (output or "").splitlines()
    hits = [ln for ln in lines if any(p in ln for p in FAIL_LOG_PATTERNS)]
    if hits:
        return "\n".join(hits[-12:])
    return (output or "")[-2000:]


async def fetch_compose_logs(migrator, services: list[str], tail: int = 200) -> str:
    cwd = str(PASARGUARD_DIR)
    ok, out = await migrator._run_cmd(
        ["docker", "compose", "logs", "--no-color", "--tail", str(tail), *services],
        cwd=cwd,
        timeout=30,
    )
    return out if ok else ""


async def fetch_pasarguard_logs(migrator, tail: int = 150) -> str:
    pg = await fetch_compose_logs(migrator, ["pasarguard"], tail=tail)
    target_db = migrator.params.get("target_db")
    db_svc = resolve_db_service(target_db) if target_db else None
    if db_svc:
        db_logs = await fetch_compose_logs(migrator, [db_svc], tail=min(tail, 80))
        return f"{pg}\n{db_logs}"
    return pg


def _check_logs_for_failure(output: str) -> str | None:
    for pattern in FAIL_LOG_PATTERNS:
        if pattern in (output or ""):
            return pattern
    return None


async def _pasarguard_container_running(migrator) -> bool:
    cwd = str(PASARGUARD_DIR)
    ok, running = await migrator._run_cmd(
        ["docker", "compose", "ps", "--status", "running", "-q", "pasarguard"],
        cwd=cwd,
        timeout=15,
    )
    return bool(ok and running.strip())


async def verify_pasarguard_healthy(migrator, max_wait: int = 120) -> None:
    """Fail unless PasarGuard logs show a clean startup (no migration errors)."""
    migrator.job.log("Verifying PasarGuard started without errors...")
    await asyncio.sleep(12)

    stable_ready = 0
    attempts = max(8, max_wait // 4)
    for _ in range(attempts):
        out = await fetch_pasarguard_logs(migrator, tail=300)
        _log_failures_from_output(migrator, out)

        hit = _check_logs_for_failure(out)
        if hit:
            raise RuntimeError(
                "PasarGuard failed to start — see container logs.\n"
                + _extract_failure_snippet(out)
            )

        if not await _pasarguard_container_running(migrator):
            raise RuntimeError(
                "PasarGuard container is not running.\n" + _extract_failure_snippet(out)
            )

        if any(marker in (out or "") for marker in STARTUP_MARKERS):
            stable_ready += 1
            if stable_ready >= 2:
                migrator.job.log("PasarGuard healthy — application startup confirmed")
                return
        else:
            stable_ready = 0

        await asyncio.sleep(4)

    out = await fetch_pasarguard_logs(migrator, tail=350)
    hit = _check_logs_for_failure(out)
    if hit:
        raise RuntimeError(
            "PasarGuard startup failed.\n" + _extract_failure_snippet(out)
        )
    raise RuntimeError(
        "PasarGuard did not reach ready state (no 'Application startup complete' in logs).\n"
        + _extract_failure_snippet(out)
    )


def read_sqlite_alembic_version(sqlite_path: str | Path) -> str | None:
    path = Path(sqlite_path)
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(str(path))
        try:
            cur = conn.execute("SELECT version_num FROM alembic_version LIMIT 1")
            row = cur.fetchone()
            return str(row[0]).strip() if row and row[0] else None
        finally:
            conn.close()
    except Exception:
        return None


async def read_mysql_alembic_version(migrator, target_db: str) -> str | None:
    service = resolve_db_service(target_db)
    if not service:
        return None
    conn = _target_conn(migrator)
    user = conn.get("user") or "root"
    pwd = conn.get("password") or "password"
    host = conn.get("host") or "127.0.0.1"
    db = conn.get("database") or "pasarguard"
    cwd = str(PASARGUARD_DIR)
    cmd = (
        f'cd "{cwd}" && docker compose exec -T {service} '
        f'mysql -u {user} -p"{pwd}" -h {host} -N -e '
        f'"SELECT version_num FROM `{db}`.alembic_version LIMIT 1"'
    )
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    version = (stdout or b"").decode("utf-8", errors="ignore").strip()
    return version or None


def read_source_alembic_version(
    source_db: str,
    source_path: str | Path | None,
    password: str | None = None,
) -> str | None:
    if source_db == "sqlite" and source_path:
        return read_sqlite_alembic_version(source_path)
    return None


async def docker_compose_up(migrator, services: list[str] | None = None) -> bool:
    cwd = str(PASARGUARD_DIR)
    cmd = ["docker", "compose", "up", "-d"]
    if services:
        cmd.extend(services)
    ok, _ = await migrator._run_cmd(cmd, cwd=cwd, timeout=180)
    return ok


async def wait_pasarguard_ready(migrator, max_wait: int = 90, strict: bool = False) -> bool:
    cwd = str(PASARGUARD_DIR)
    migrator.job.log("Waiting for PasarGuard to become ready...")

    for attempt in range(max(1, max_wait // 3)):
        out = await fetch_pasarguard_logs(migrator, tail=100)
        for pattern in FAIL_LOG_PATTERNS:
            if pattern in (out or ""):
                if strict:
                    raise RuntimeError(
                        "PasarGuard startup error:\n" + _extract_failure_snippet(out)
                    )
                migrator.job.log(f"Detected PasarGuard log error: {pattern}")

        if any(marker in (out or "") for marker in STARTUP_MARKERS):
            migrator.job.log("PasarGuard ready")
            return True

        ok_run, running = await migrator._run_cmd(
            ["docker", "compose", "ps", "--status", "running", "-q", "pasarguard"],
            cwd=cwd,
            timeout=15,
        )
        if ok_run and running.strip() and attempt >= 4:
            migrator.job.log("PasarGuard container running — waiting for application startup...")
            # Do not return True here — caller must use verify_pasarguard_healthy

        await asyncio.sleep(3)

    if strict:
        out = await fetch_pasarguard_logs(migrator, tail=120)
        raise RuntimeError(
            "PasarGuard readiness timeout.\n" + _extract_failure_snippet(out)
        )
    migrator.job.log("PasarGuard readiness timeout — continuing")
    return False


async def start_pasarguard(migrator, wait: bool = True, recreate: bool = False) -> None:
    cwd = str(PASARGUARD_DIR)
    cmd = ["docker", "compose", "up", "-d"]
    if recreate:
        cmd.extend(["--force-recreate", "pasarguard"])
    else:
        cmd.append("pasarguard")
    await migrator._run_cmd(cmd, cwd=cwd, timeout=180)
    if wait:
        await wait_pasarguard_ready(migrator)


async def restart_pasarguard(migrator, wait: bool = True) -> None:
    cwd = str(PASARGUARD_DIR)
    migrator.job.log("Restarting PasarGuard (docker compose)...")
    ok, _ = await migrator._run_cmd(
        ["docker", "compose", "restart", "pasarguard"],
        cwd=cwd,
        timeout=120,
    )
    if not ok:
        await migrator._run_cmd(
            ["docker", "compose", "up", "-d", "--force-recreate", "pasarguard"],
            cwd=cwd,
            timeout=180,
        )
    if wait:
        await wait_pasarguard_ready(migrator, max_wait=30, strict=False)


async def _wait_db_service(migrator, target_db: str, service: str, attempts: int = 20) -> None:
    cwd = str(PASARGUARD_DIR)
    conn = _target_conn(migrator)
    user = conn.get("user") or ("postgres" if service in ("postgresql", "timescaledb") else "root")
    pwd = conn.get("password") or "password"
    db = conn.get("database") or "pasarguard"
    host = conn.get("host") or "127.0.0.1"

    for _ in range(attempts):
        if service in ("postgresql", "timescaledb"):
            cmd = (
                f'cd "{cwd}" && docker compose exec -T {service} '
                f'env PGPASSWORD="{pwd}" psql -U {user} -d {db} -c "SELECT 1"'
            )
        elif service in ("mysql", "mariadb"):
            cmd = (
                f'cd "{cwd}" && docker compose exec -T {service} '
                f'mysqladmin ping -h {host} -u {user} -p"{pwd}"'
            )
        else:
            return

        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        await proc.wait()
        if proc.returncode == 0:
            migrator.job.log(f"Database service {service} ready (db={db}, user={user})")
            return
        await asyncio.sleep(3)

    migrator.job.log(f"Warning: {service} readiness check timed out — continuing")


async def read_target_alembic_version(migrator, target_db: str) -> str | None:
    if target_db == "sqlite":
        conn = _target_conn(migrator)
        path = conn.get("sqlite_path") or str(PASARGUARD_DATA / "db.sqlite3")
        return read_sqlite_alembic_version(path)

    service = resolve_db_service(target_db)
    if not service:
        return None

    conn = _target_conn(migrator)
    user = conn.get("user") or ("postgres" if service in ("postgresql", "timescaledb") else "root")
    pwd = conn.get("password") or "password"
    db = conn.get("database") or "pasarguard"
    cwd = str(PASARGUARD_DIR)

    if service in ("postgresql", "timescaledb"):
        cmd = (
            f'cd "{cwd}" && docker compose exec -T {service} '
            f'env PGPASSWORD="{pwd}" psql -U {user} -d {db} -tAc '
            f'"SELECT version_num FROM alembic_version LIMIT 1"'
        )
    elif service in ("mysql", "mariadb"):
        host = conn.get("host") or "127.0.0.1"
        cmd = (
            f'cd "{cwd}" && docker compose exec -T {service} '
            f'mysql -u {user} -p"{pwd}" -h {host} -N -e '
            f'"SELECT version_num FROM `{db}`.alembic_version LIMIT 1"'
        )
    else:
        return None

    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    version = (stdout or b"").decode("utf-8", errors="ignore").strip()
    return version or None


async def run_alembic_upgrade(migrator) -> bool:
    ok, out = await _run_pasarguard_alembic(migrator, "upgrade", "head")
    if ok:
        migrator.job.log("Alembic upgrade head completed")
        return True
    if out and "already at head" in out.lower():
        return True
    return False


async def _run_pasarguard_alembic(migrator, *args: str) -> tuple[bool, str]:
    """Run alembic inside one-shot pasarguard container (avoids all-in-one startup)."""
    cwd = str(PASARGUARD_DIR)
    quoted = " ".join(args)
    commands = [
        ["docker", "compose", "run", "--rm", "--no-deps", "pasarguard", "bash", "-lc", f"uv run alembic {quoted}"],
        ["docker", "compose", "run", "--rm", "--no-deps", "pasarguard", "bash", "-lc", f"cd /code && uv run alembic {quoted}"],
        ["docker", "compose", "exec", "-T", "pasarguard", "bash", "-lc", f"uv run alembic {quoted}"],
    ]
    last_out = ""
    for cmd in commands:
        ok, out = await migrator._run_cmd(cmd, cwd=cwd, timeout=300)
        last_out = out or last_out
        if ok:
            return True, last_out
    return False, last_out


async def stamp_alembic_head(migrator) -> bool:
    ok, out = await _run_pasarguard_alembic(migrator, "stamp", "head")
    if ok:
        migrator.job.log("Alembic stamped to head")
        return True
    migrator.job.log(f"alembic stamp head failed: {(out or '')[-500:]}")
    return False


async def _pg_column_exists(migrator, table: str, column: str) -> bool:
    target_db = migrator.params.get("target_db")
    service = resolve_db_service(target_db or "")
    if not service:
        return False
    conn = _target_conn(migrator)
    user = conn.get("user") or "postgres"
    pwd = conn.get("password") or ""
    db = conn.get("database") or "pasarguard"
    cwd = str(PASARGUARD_DIR)
    sql = (
        "SELECT 1 FROM information_schema.columns "
        f"WHERE table_name='{table}' AND column_name='{column}' LIMIT 1"
    )
    cmd = (
        f'cd "{cwd}" && docker compose exec -T {service} '
        f'env PGPASSWORD="{pwd}" psql -U {user} -d {db} -tAc "{sql}"'
    )
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return False
    return (stdout or b"").decode("utf-8", errors="ignore").strip() == "1"


async def _target_has_public_tables(migrator, target_db: str) -> bool:
    service = resolve_db_service(target_db)
    if not service:
        return False
    conn = _target_conn(migrator)
    user = conn.get("user") or "postgres"
    pwd = conn.get("password") or ""
    db = conn.get("database") or "pasarguard"
    cwd = str(PASARGUARD_DIR)
    sql = (
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE' LIMIT 1"
    )
    cmd = (
        f'cd "{cwd}" && docker compose exec -T {service} '
        f'env PGPASSWORD="{pwd}" psql -U {user} -d {db} -tAc "{sql}"'
    )
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return False
    return (stdout or b"").decode("utf-8", errors="ignore").strip() == "1"


async def finalize_target_alembic_after_import(migrator, target_db: str) -> None:
    """
    After db-migrations: if schema already has columns from a prior head-migration
    but alembic_version is still at Marzban revision, stamp head to prevent
    DuplicateColumnError on PasarGuard restart.
    """
    if target_db not in ("postgresql", "timescaledb"):
        return

    if await _pg_column_exists(migrator, "next_plans", "user_template_id"):
        current = await read_target_alembic_version(migrator, target_db)
        migrator.job.log(
            f"Schema already has PasarGuard columns (alembic={current or 'none'}) — stamping head"
        )
        if not await stamp_alembic_head(migrator):
            raise RuntimeError(
                "Database schema is ahead of alembic_version but 'alembic stamp head' failed. "
                "Drop/recreate the target database or fix alembic_version manually."
            )


async def set_target_alembic_version(
    migrator, target_db: str, version: str,
) -> bool:
    if not version:
        return False

    if target_db == "sqlite":
        conn = _target_conn(migrator)
        path = Path(conn.get("sqlite_path") or PASARGUARD_DATA / "db.sqlite3")
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        try:
            db = sqlite3.connect(str(path))
            db.execute("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32))")
            db.execute("DELETE FROM alembic_version")
            db.execute("INSERT INTO alembic_version (version_num) VALUES (?)", (version,))
            db.commit()
            db.close()
            migrator.job.log(f"SQLite alembic_version set to {version}")
            return True
        except Exception:
            return False

    service = resolve_db_service(target_db)
    if not service:
        return False

    conn = _target_conn(migrator)
    user = conn.get("user") or ("postgres" if service in ("postgresql", "timescaledb") else "root")
    pwd = conn.get("password") or "password"
    db = conn.get("database") or "pasarguard"
    cwd = str(PASARGUARD_DIR)

    if service in ("postgresql", "timescaledb"):
        sql = (
            f"DELETE FROM alembic_version; "
            f"INSERT INTO alembic_version (version_num) VALUES ('{version}');"
        )
        cmd = (
            f'cd "{cwd}" && docker compose exec -T {service} '
            f'env PGPASSWORD="{pwd}" psql -U {user} -d {db} -c "{sql}"'
        )
    elif service in ("mysql", "mariadb"):
        host = conn.get("host") or "127.0.0.1"
        sql = (
            f"DELETE FROM alembic_version; "
            f"INSERT INTO alembic_version (version_num) VALUES ('{version}');"
        )
        cmd = (
            f'cd "{cwd}" && docker compose exec -T {service} '
            f'mysql -u {user} -p"{pwd}" -h {host} {db} -e "{sql}"'
        )
    else:
        return False

    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    await proc.wait()
    if proc.returncode == 0:
        migrator.job.log(f"Target alembic_version set to {version} (db={db}, user={user})")
        return True
    return False


async def ensure_schema_initialized(
    migrator,
    target_db: str,
    source_db: str | None = None,
    source_path: str | Path | None = None,
) -> str | None:
    """
    Prepare target DB schema at source Alembic revision for db-migrations.
    Uses one-shot `alembic upgrade` (not full PasarGuard all-in-one startup).
    """
    cwd = str(PASARGUARD_DIR)
    conn = _target_conn(migrator)
    migrator.job.log(
        f"Target DB connection (user input): "
        f"type={target_db}, user={conn.get('user')}, database={conn.get('database')}, "
        f"host={conn.get('host')}, port={conn.get('port') or 'default'}"
    )

    source_version = read_source_alembic_version(source_db or "sqlite", source_path)
    if source_version:
        migrator.job.log(f"Source Alembic version: {source_version}")

    service = resolve_db_service(target_db)
    if service:
        migrator.job.log(f"Ensuring DB service {service} is running...")
        await docker_compose_up(migrator, [service])
        await _wait_db_service(migrator, target_db, service)
    elif target_db == "sqlite":
        PASARGUARD_DATA.mkdir(parents=True, exist_ok=True)
        sqlite_path = Path(conn.get("sqlite_path") or PASARGUARD_DATA / "db.sqlite3")
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        migrator.job.log(f"Target SQLite path: {sqlite_path}")

    migrator.job.log("Stopping PasarGuard before schema init...")
    await migrator._run_cmd(["docker", "compose", "stop", "pasarguard"], cwd=cwd, timeout=120)

    if target_db in ("postgresql", "timescaledb") and await _pg_column_exists(
        migrator, "next_plans", "user_template_id"
    ):
        migrator.job.log("Target DB already has PasarGuard schema from a previous attempt")
        if not await stamp_alembic_head(migrator):
            raise RuntimeError(
                "Target database schema exists but alembic_version is out of sync. "
                "Use a fresh empty database or run: docker compose exec pasarguard uv run alembic stamp head"
            )
        target_version = await read_target_alembic_version(migrator, target_db)
        migrator.job.log(f"Target Alembic version after heal: {target_version}")
        return target_version

    revision = source_version or "head"
    migrator.job.log(f"Running alembic upgrade {revision} (one-shot, no panel startup)...")
    ok, out = await _run_pasarguard_alembic(migrator, "upgrade", revision)
    if not ok:
        raise RuntimeError(
            f"Failed to initialize target schema with alembic upgrade {revision}:\n{(out or '')[-3000:]}"
        )

    target_version = await read_target_alembic_version(migrator, target_db)
    if not target_version:
        raise RuntimeError(
            f"Target database ({target_db}) has no Alembic schema after upgrade. "
            f"Check credentials: database '{conn.get('database')}', user '{conn.get('user')}'."
        )

    migrator.job.log(f"Target Alembic version after init: {target_version}")
    return target_version
