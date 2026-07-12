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
    "sqlalchemy.exc.",
    "DuplicateColumnError",
    "ProgrammingError",
    "Traceback (most recent call last)",
    "FATAL:",
    "could not connect",
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


async def fetch_pasarguard_logs(migrator, tail: int = 150) -> str:
    cwd = str(PASARGUARD_DIR)
    ok, out = await migrator._run_cmd(
        ["docker", "compose", "logs", "--no-color", "--tail", str(tail), "pasarguard"],
        cwd=cwd,
        timeout=25,
    )
    return out if ok else ""


async def verify_pasarguard_healthy(migrator, max_wait: int = 90) -> None:
    """Fail migration if PasarGuard container logs show startup/migration errors."""
    migrator.job.log("Verifying PasarGuard started without errors...")
    cwd = str(PASARGUARD_DIR)

    for attempt in range(max(1, max_wait // 3)):
        out = await fetch_pasarguard_logs(migrator, tail=180)
        _log_failures_from_output(migrator, out)

        for pattern in FAIL_LOG_PATTERNS:
            if pattern in (out or ""):
                snippet = _extract_failure_snippet(out)
                raise RuntimeError(
                    "PasarGuard failed after migration — container logs contain errors. "
                    f"Check: {pattern}\n{snippet}"
                )

        if any(marker in (out or "") for marker in STARTUP_MARKERS):
            migrator.job.log("PasarGuard healthy — no errors in container logs")
            return

        ok_run, running = await migrator._run_cmd(
            ["docker", "compose", "ps", "--status", "running", "-q", "pasarguard"],
            cwd=cwd,
            timeout=15,
        )
        if ok_run and running.strip() and attempt >= 5:
            out2 = await fetch_pasarguard_logs(migrator, tail=180)
            for pattern in FAIL_LOG_PATTERNS:
                if pattern in (out2 or ""):
                    snippet = _extract_failure_snippet(out2)
                    raise RuntimeError(
                        "PasarGuard container is running but logs contain errors.\n" + snippet
                    )
            migrator.job.log("PasarGuard container running")
            return

        await asyncio.sleep(3)

    out = await fetch_pasarguard_logs(migrator, tail=200)
    snippet = _extract_failure_snippet(out)
    raise RuntimeError(
        "PasarGuard did not become healthy within timeout. Recent logs:\n" + snippet
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
            migrator.job.log("PasarGuard container running — proceeding")
            return True

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
        await wait_pasarguard_ready(migrator, max_wait=60)


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
    cwd = str(PASARGUARD_DIR)
    commands = [
        ["docker", "compose", "exec", "-T", "pasarguard", "alembic", "upgrade", "head"],
        ["docker", "compose", "exec", "-T", "pasarguard", "uv", "run", "alembic", "upgrade", "head"],
        ["docker", "compose", "exec", "-T", "pasarguard", "bash", "-lc", "uv run alembic upgrade head"],
        ["docker", "compose", "exec", "-T", "pasarguard", "bash", "-lc", "cd /code && uv run alembic upgrade head"],
    ]
    for cmd in commands:
        ok, out = await migrator._run_cmd(cmd, cwd=cwd, timeout=300)
        if ok:
            migrator.job.log("Alembic upgrade head completed")
            return True
        if out and "already at head" in out.lower():
            return True
    return False


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
    Boot PasarGuard on the target DB so Alembic creates schema (all DB types),
    then align alembic_version with source when needed for db-migrations.
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

    migrator.job.log("Starting PasarGuard on target database (force recreate)...")
    await start_pasarguard(migrator, wait=True, recreate=True)
    await asyncio.sleep(8)

    target_version = await read_target_alembic_version(migrator, target_db)
    if not target_version:
        migrator.job.log("Target schema empty — running Alembic upgrade...")
        await run_alembic_upgrade(migrator)
        await asyncio.sleep(5)
        await start_pasarguard(migrator, wait=True, recreate=True)
        target_version = await read_target_alembic_version(migrator, target_db)

    if not target_version:
        raise RuntimeError(
            f"Target database ({target_db}) has no Alembic schema. "
            f"Check credentials: database '{conn.get('database')}', user '{conn.get('user')}'."
        )

    migrator.job.log(f"Target Alembic version after init: {target_version}")

    if source_version and source_version != target_version:
        migrator.job.log(
            f"Aligning target alembic_version {target_version} → {source_version} for db-migrations"
        )
        if await set_target_alembic_version(migrator, target_db, source_version):
            target_version = source_version

    return target_version
