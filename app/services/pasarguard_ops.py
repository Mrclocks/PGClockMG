"""Non-interactive PasarGuard Docker operations (no hanging CLI)."""

from __future__ import annotations

import asyncio
import re
import sqlite3
import tempfile
from pathlib import Path

from app.config import PASARGUARD_DIR, PASARGUARD_ENV, PASARGUARD_DATA
from app.services.db_credentials import get_target_connection, migration_port

STARTUP_MARKERS = (
    "Application startup complete",
    "Uvicorn running",
)

FAIL_LOG_PATTERNS = (
    "Database migrations failed",
    "ERROR: Database migrations failed",
    "sqlalchemy.exc.",
    "asyncpg.exceptions.",
    "DuplicateColumnError",
    "ProgrammingError",
    "Traceback (most recent call last)",
    "could not connect",
    "connection refused",
    "password authentication failed",
    "SASL authentication failed",
    "cache lookup failed for type",
    "Application startup failed",
    "ValueError:",
    "SSL certificate file",
    "column \"user_template_id\" of relation \"next_plans\" already exists",
)

# Harmless lines from DB restarts — must not fail the panel health check
BENIGN_LOG_PATTERNS = (
    "terminating background worker",
    "due to administrator command",
    "checkpoint starting:",
    "checkpoint complete:",
    "database system is shut down",
    "database system is ready to accept connections",
    "shutting down",
)

# Noise from no-SSL banners / SSH tunnel hints — never treat as the root cause
BANNER_NOISE_PATTERNS = (
    "ssh -L",
    "navigate to",
    "on your computer",
    "Then, navigate",
    "#####",
)


DB_SERVICES = {
    "timescaledb": ("timescaledb", "postgresql"),
    "postgresql": ("postgresql", "timescaledb"),
    # Soft family: MariaDB stacks may use service name ``mysql:`` (or vice versa)
    "mysql": ("mysql", "mariadb"),
    "mariadb": ("mariadb", "mysql"),
    "sqlite": tuple(),
}

# Aliases that may appear in labels / raw API params → canonical engine ids
TARGET_DB_ALIASES = {
    "postgres": "postgresql",
    "pgsql": "postgresql",
    "pg": "postgresql",
    "timescale": "timescaledb",
    "tsdb": "timescaledb",
    "maria": "mariadb",
}

PASARGUARD_SERVICE_CANDIDATES = ("pasarguard", "panel", "app", "pg")


def normalize_target_db(target_db: str | None) -> str:
    """Map aliases to canonical PasarGuard engine ids."""
    raw = (target_db or "sqlite").strip().lower()
    return TARGET_DB_ALIASES.get(raw, raw)


def mysql_client_bins(db_type: str = "", service: str | None = None) -> list[str]:
    """SQL client binaries to try inside MySQL/MariaDB containers."""
    name = f"{service or ''} {db_type or ''}".lower()
    if "maria" in name:
        return ["mariadb", "mysql"]
    return ["mysql", "mariadb"]


def mysql_admin_bins(db_type: str = "", service: str | None = None) -> list[str]:
    """Admin ping binaries (MariaDB often ships ``mariadb-admin`` only)."""
    name = f"{service or ''} {db_type or ''}".lower()
    if "maria" in name:
        return ["mariadb-admin", "mysqladmin"]
    return ["mysqladmin", "mariadb-admin"]


def _compose_text() -> str:
    compose = PASARGUARD_DIR / "docker-compose.yml"
    return compose.read_text(encoding="utf-8", errors="ignore") if compose.exists() else ""


def resolve_db_service(target_db: str) -> str | None:
    target_db = normalize_target_db(target_db)
    if target_db == "sqlite":
        return None
    text = _compose_text()
    for name in DB_SERVICES.get(target_db, (target_db,)):
        if name and re.search(rf"^\s*{re.escape(name)}\s*:", text, re.MULTILINE):
            return name
    # Do not invent a missing service name (avoids targeting plain postgresql for
    # Timescale dumps, or a non-existent ``mysql`` when only ``mariadb`` exists).
    return None


def _target_conn(migrator) -> dict:
    return get_target_connection(migrator.params)


def _log_failures_from_output(migrator, output: str) -> None:
    for line in (output or "").splitlines():
        if _line_indicates_failure(line):
            migrator.job.log(line)


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text or "")


def _is_banner_noise(line: str) -> bool:
    low = line.lower()
    # Keep real failures even if they share a word with banners
    if any(p.lower() in low for p in FAIL_LOG_PATTERNS):
        return False
    return any(n.lower() in low for n in BANNER_NOISE_PATTERNS)


def _line_indicates_failure(line: str) -> bool:
    if any(b in line for b in BENIGN_LOG_PATTERNS):
        return False
    if _is_banner_noise(line):
        return False
    return any(p in line for p in FAIL_LOG_PATTERNS)


def _extract_failure_snippet(output: str) -> str:
    clean = _strip_ansi(output or "")
    lines = clean.splitlines()
    hits = [ln for ln in lines if _line_indicates_failure(ln)]
    if hits:
        return "\n".join(hits[-16:])
    useful = []
    for ln in lines:
        if not ln.strip() or _is_banner_noise(ln):
            continue
        if any(x in ln for x in ("ERROR", "Error", "Traceback", "Exception", "failed", "FATAL", "ValueError")):
            useful.append(ln)
    if useful:
        return "\n".join(useful[-20:])
    non_banner = [ln for ln in lines if ln.strip() and not _is_banner_noise(ln)]
    return "\n".join(non_banner[-20:]) if non_banner else clean[-1500:]


async def fetch_compose_logs(migrator, services: list[str], tail: int = 200) -> str:
    cwd = str(PASARGUARD_DIR)
    ok, out = await migrator._run_cmd(
        ["docker", "compose", "logs", "--no-color", "--tail", str(tail), *services],
        cwd=cwd,
        timeout=30,
    )
    return out if ok else ""


async def fetch_pasarguard_logs(migrator, tail: int = 150, *, include_db: bool = False) -> str:
    """Panel logs only by default — DB restart FATAL lines are not panel failures."""
    pg = await fetch_compose_logs(migrator, ["pasarguard"], tail=tail)
    if not include_db:
        return pg
    target_db = migrator.params.get("target_db")
    db_svc = resolve_db_service(target_db) if target_db else None
    if db_svc:
        db_logs = await fetch_compose_logs(migrator, [db_svc], tail=min(tail, 80))
        return f"{pg}\n{db_logs}"
    return pg


def _check_logs_for_failure(output: str) -> str | None:
    for line in (output or "").splitlines():
        if _line_indicates_failure(line):
            for pattern in FAIL_LOG_PATTERNS:
                if pattern in line:
                    return pattern
    return None


async def _pasarguard_container_state(migrator) -> str:
    """Return running | restarting | exited | unknown for the panel service."""
    cwd = str(PASARGUARD_DIR)
    ok, out = await migrator._run_cmd(
        ["docker", "compose", "ps", "--format", "{{.Name}} {{.Status}}", "pasarguard"],
        cwd=cwd,
        timeout=20,
    )
    text = (out or "").lower()
    if ok and text.strip():
        if "restarting" in text:
            return "restarting"
        if "up " in text or "(healthy)" in text or "running" in text:
            return "running"
        if "exit" in text or "dead" in text or "created" in text:
            return "exited"

    ok2, ids = await migrator._run_cmd(
        ["docker", "compose", "ps", "-q", "pasarguard"],
        cwd=cwd,
        timeout=15,
    )
    if ok2 and (ids or "").strip():
        cid = (ids or "").strip().splitlines()[0].strip()
        ok3, st = await migrator._run_cmd(
            ["docker", "inspect", "-f", "{{.State.Status}}", cid],
            cwd=cwd,
            timeout=15,
        )
        status = (st or "").strip().lower()
        if status in ("running", "restarting", "exited", "dead", "created"):
            return status if status != "dead" else "exited"
        if status:
            return status
    return "unknown"


async def _pasarguard_container_running(migrator) -> bool:
    return (await _pasarguard_container_state(migrator)) == "running"


async def _ensure_pasarguard_up(migrator) -> None:
    cwd = str(PASARGUARD_DIR)
    await migrator._run_cmd(
        ["docker", "compose", "up", "-d", "pasarguard"],
        cwd=cwd,
        timeout=180,
    )


async def _try_heal_db_auth_mismatch(migrator, logs: str) -> bool:
    """If panel logs show Access denied / SASL fail, re-sync DB users to .env password."""
    low = (logs or "").lower()
    auth_hit = any(
        s in low
        for s in (
            "access denied for user",
            "password authentication failed",
            "sasl authentication failed",
            "authentication failed",
        )
    )
    if not auth_hit:
        return False

    target_db = (migrator.params or {}).get("target_db")
    if target_db not in ("mysql", "mariadb", "postgresql", "timescaledb"):
        return False

    try:
        from app.services.db_auth import (
            read_env_text,
            resolve_live_admin_connection,
            sync_mysql_roles_to_password,
            sync_postgres_roles_to_app_password,
        )
        from app.services.env_migration import parse_sqlalchemy_url, read_env_var

        env = read_env_text()
        url = read_env_var(env, "SQLALCHEMY_DATABASE_URL") or ""
        parsed = parse_sqlalchemy_url(url) if url else {}
        app_user = (
            parsed.get("user")
            or read_env_var(env, "DB_USER")
            or read_env_var(env, "MYSQL_USER")
            or "pasarguard"
        )
        url_pwd = (
            parsed.get("password")
            or read_env_var(env, "DB_PASSWORD")
            or read_env_var(env, "MYSQL_ROOT_PASSWORD")
            or ""
        )
        if not url_pwd:
            return False

        migrator.job.log(
            f"Auth failure in panel logs — syncing {target_db} roles for user={app_user}…"
        )
        admin = await resolve_live_admin_connection(migrator, target_db, env_text=env)
        if target_db in ("mysql", "mariadb"):
            await sync_mysql_roles_to_password(
                migrator,
                target_db,
                admin,
                app_user=app_user,
                password=url_pwd,
                env_text=env,
            )
        else:
            await sync_postgres_roles_to_app_password(
                migrator, target_db, admin, env_text=env,
            )
        return True
    except Exception as e:
        migrator.job.log(f"DB auth heal note: {e}")
        return False


def _count_restarts_in_logs(output: str) -> int:
    """Count how many times 'Starting backend...' appears — each one is a restart."""
    return (output or "").count("Starting backend...")


async def _heal_silent_restart_loop(migrator) -> bool:
    """Attempt to fix a silent restart-loop (panel exits without any error log).

    The panel starts, runs alembic context check, then silently exits and
    restarts.  The most common cause after a DB restore is a stale
    alembic_version that makes PasarGuard's startup code exit non-zero before
    uvicorn binds its socket.  Stamp alembic head and force-recreate the
    container.
    """
    target_db = (migrator.params or {}).get("target_db")
    if target_db not in ("postgresql", "timescaledb", "mysql", "mariadb", "sqlite"):
        return False
    migrator.job.log(
        "Silent restart loop detected — stamping alembic head and force-recreating panel..."
    )
    try:
        await stamp_alembic_head(migrator)
    except Exception as e:
        migrator.job.log(f"alembic stamp note: {e}")

    cwd = str(PASARGUARD_DIR)
    await migrator._run_cmd(["docker", "compose", "stop", "pasarguard"], cwd=cwd, timeout=60)
    await asyncio.sleep(3)
    await migrator._run_cmd(
        ["docker", "compose", "up", "-d", "--force-recreate", "pasarguard"],
        cwd=cwd,
        timeout=180,
    )
    return True


async def verify_pasarguard_healthy(migrator, max_wait: int = 180) -> None:
    """Fail unless PasarGuard logs show a clean startup (no migration errors)."""
    migrator.job.log("Verifying PasarGuard started without errors...")
    await asyncio.sleep(8)

    stable_ready = 0
    not_running_streak = 0
    healed_once = False
    silent_loop_healed = False
    prev_restart_count = 0
    attempts = max(12, max_wait // 4)
    for i in range(attempts):
        out = await fetch_pasarguard_logs(migrator, tail=400)
        _log_failures_from_output(migrator, out)

        hit = _check_logs_for_failure(out)
        if hit:
            # Give crash-loop a moment and one recreate before hard-fail
            if not healed_once:
                healed_once = True
                # Auto-heal MySQL/PG password drift after cross-DB (Access denied / SASL)
                if await _try_heal_db_auth_mismatch(migrator, out):
                    migrator.job.log(
                        f"Panel error detected ({hit}) — DB auth healed, recreating panel…"
                    )
                else:
                    migrator.job.log(
                        f"Panel error detected ({hit}) — recreating pasarguard once…"
                    )
                await _ensure_pasarguard_up(migrator)
                await asyncio.sleep(10)
                continue
            db_hint = ""
            target_db = migrator.params.get("target_db")
            if target_db in ("postgresql", "timescaledb"):
                db_svc = resolve_db_service(target_db)
                if db_svc:
                    db_logs = await fetch_compose_logs(migrator, [db_svc], tail=40)
                    if db_logs.strip():
                        db_hint = f"\n\n--- {db_svc} (reference) ---\n{db_logs[-1500:]}"
            raise RuntimeError(
                "PasarGuard failed to start — see container logs.\n"
                + _extract_failure_snippet(out)
                + db_hint
            )

        # Detect silent restart loop: panel exits without any error log
        # (alembic context prints but uvicorn never starts)
        restart_count = _count_restarts_in_logs(out)
        if restart_count >= 2 and not silent_loop_healed:
            # Only heal if we see multiple restarts without a startup marker
            has_startup = any(marker in (out or "") for marker in STARTUP_MARKERS)
            if not has_startup:
                silent_loop_healed = True
                await _heal_silent_restart_loop(migrator)
                await asyncio.sleep(15)
                prev_restart_count = 0
                continue
        prev_restart_count = restart_count

        state = await _pasarguard_container_state(migrator)
        if state != "running":
            not_running_streak += 1
            migrator.job.log(f"PasarGuard container state={state} (wait {not_running_streak})")
            if not_running_streak >= 2 and not healed_once:
                healed_once = True
                migrator.job.log("Bringing pasarguard back up…")
                await _ensure_pasarguard_up(migrator)
            if not_running_streak >= 6:
                raise RuntimeError(
                    "PasarGuard container is not running.\n" + _extract_failure_snippet(out)
                )
            await asyncio.sleep(5)
            continue

        not_running_streak = 0
        if any(marker in (out or "") for marker in STARTUP_MARKERS):
            stable_ready += 1
            if stable_ready >= 2:
                migrator.job.log("PasarGuard healthy — application startup confirmed")
                return
        else:
            stable_ready = 0

        await asyncio.sleep(4)

    out = await fetch_pasarguard_logs(migrator, tail=400)
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
    pwd_q = (pwd or "").replace('"', '\\"')
    for bin_name in mysql_client_bins(target_db, service):
        cmd = (
            f'cd "{cwd}" && docker compose exec -T {service} '
            f'{bin_name} -u {user} -p"{pwd_q}" -h {host} -N -e '
            f'"SELECT version_num FROM `{db}`.alembic_version LIMIT 1"'
        )
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            continue
        version = (stdout or b"").decode("utf-8", errors="ignore").strip()
        if version:
            return version
    return None


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
        hit = _check_logs_for_failure(out)
        if hit:
            if strict:
                raise RuntimeError(
                    "PasarGuard startup error:\n" + _extract_failure_snippet(out)
                )
            migrator.job.log(f"Detected PasarGuard log error: {hit}")

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
    pwd_q = (pwd or "").replace('"', '\\"')

    for _ in range(attempts):
        cmds: list[str] = []
        if service in ("postgresql", "timescaledb"):
            cmds = [
                (
                    f'cd "{cwd}" && docker compose exec -T {service} '
                    f'env PGPASSWORD="{pwd_q}" psql -U {user} -d {db} -c "SELECT 1"'
                )
            ]
        elif service in ("mysql", "mariadb"):
            for admin_bin in mysql_admin_bins(target_db, service):
                cmds.append(
                    f'cd "{cwd}" && docker compose exec -T {service} '
                    f'{admin_bin} ping -h {host} -u {user} -p"{pwd_q}"'
                )
        else:
            return

        ready = False
        for cmd in cmds:
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            await proc.wait()
            if proc.returncode == 0:
                ready = True
                break
        if ready:
            migrator.job.log(f"Database service {service} ready (db={db}, user={user})")
            return
        await asyncio.sleep(3)

    migrator.job.log(f"Warning: {service} readiness check timed out — continuing")


async def read_target_alembic_version(migrator, target_db: str) -> str | None:
    if target_db == "sqlite":
        conn = _target_conn(migrator)
        path = conn.get("sqlite_path") or (PASARGUARD_DATA / "db.sqlite3").as_posix()
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
        pwd_q = (pwd or "").replace('"', '\\"')
        from app.services.native_migration.source_version import normalize_alembic_revision

        for bin_name in mysql_client_bins(target_db, service):
            cmd = (
                f'cd "{cwd}" && docker compose exec -T {service} '
                f'{bin_name} -u {user} -p"{pwd_q}" -h {host} -N -e '
                f'"SELECT version_num FROM `{db}`.alembic_version LIMIT 1"'
            )
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                continue
            version = (stdout or b"").decode("utf-8", errors="ignore").strip()
            if version:
                return normalize_alembic_revision(version)
        return None
    else:
        return None

    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    version = (stdout or b"").decode("utf-8", errors="ignore").strip()
    from app.services.native_migration.source_version import normalize_alembic_revision

    return normalize_alembic_revision(version)


async def run_alembic_upgrade(migrator) -> bool:
    ok, out = await _run_pasarguard_alembic(migrator, "upgrade", "head")
    if ok:
        migrator.job.log("Alembic upgrade head completed")
        return True
    if out and "already at head" in out.lower():
        return True
    return False


def resolve_pasarguard_service() -> str:
    text = _compose_text()
    for name in PASARGUARD_SERVICE_CANDIDATES:
        if re.search(rf"^\s*{re.escape(name)}\s*:", text, re.MULTILINE):
            return name
    return "pasarguard"


def _discover_compose_profiles() -> list[str]:
    text = _compose_text()
    found: list[str] = []
    for block in re.finditer(r"profiles:\s*\n((?:[ \t]+-\s*[^\n]+\n?)+)", text):
        for item in re.findall(r"-\s*['\"]?([^'\"\n]+)['\"]?", block.group(1)):
            name = item.strip()
            if name and name not in found:
                found.append(name)
    return found


def _compose_cmd(*args: str, profiles: list[str] | None = None) -> list[str]:
    cmd: list[str] = ["docker", "compose"]
    for profile in profiles or []:
        cmd.extend(["--profile", profile])
    env_file = PASARGUARD_ENV if PASARGUARD_ENV.exists() else PASARGUARD_DIR / ".env"
    if env_file.exists():
        cmd.extend(["--env-file", str(env_file)])
    cmd.extend(args)
    return cmd


def _alembic_output_indicates_success(output: str) -> bool:
    low = (output or "").lower()
    return any(
        marker in low
        for marker in (
            "running upgrade",
            "already at head",
            "stamp",
            "(head)",
        )
    )


async def _ensure_pasarguard_image(migrator, service: str | None = None) -> None:
    svc = service or resolve_pasarguard_service()
    cwd = str(PASARGUARD_DIR)
    migrator.job.log(f"Ensuring Docker image for {svc} is available...")
    await migrator._run_cmd(_compose_cmd("pull", svc), cwd=cwd, timeout=600)


def resolve_pasarguard_image() -> str:
    text = _compose_text()
    svc = resolve_pasarguard_service()
    block = re.search(rf"^\s*{re.escape(svc)}\s*:\s*\n((?:[ \t]+[^\n]+\n)*)", text, re.MULTILINE)
    if block:
        m = re.search(r"image:\s*['\"]?([^'\"\n]+)", block.group(1))
        if m:
            return m.group(1).strip()
    return "pasarguard/panel:latest"


def build_local_alembic_url(params: dict) -> str:
    target_db = params["target_db"]
    conn = get_target_connection(params)
    pwd = conn.get("password") or ""
    user = conn.get("user") or ("postgres" if target_db in ("postgresql", "timescaledb") else "root")
    db = conn.get("database") or "pasarguard"
    port = migration_port(conn, target_db)
    if target_db in ("postgresql", "timescaledb"):
        return f"postgresql+asyncpg://{user}:{pwd}@127.0.0.1:{port}/{db}"
    if target_db in ("mysql", "mariadb"):
        return f"mysql+asyncmy://{user}:{pwd}@127.0.0.1:{port}/{db}"
    path = conn.get("sqlite_path") or (PASARGUARD_DATA / "db.sqlite3").as_posix()
    return f"sqlite+aiosqlite:///{path}"


def build_sqlite_alembic_url(path: str | Path) -> str:
    return f"sqlite+aiosqlite:///{Path(path).as_posix()}"


def build_alembic_url_from_conn(db_type: str, conn: dict) -> str:
    """Build alembic SQLAlchemy URL for any engine from a connection dict."""
    pwd = conn.get("password") or ""
    if db_type == "sqlite":
        path = conn.get("sqlite_path") or str(PASARGUARD_DATA / "db.sqlite3")
        return build_sqlite_alembic_url(path)
    user = conn.get("user") or (
        "postgres" if db_type in ("postgresql", "timescaledb") else "root"
    )
    db = conn.get("database") or "pasarguard"
    port = migration_port(conn, db_type)
    host = "127.0.0.1"
    if db_type in ("postgresql", "timescaledb"):
        return f"postgresql+asyncpg://{user}:{pwd}@{host}:{port}/{db}"
    return f"mysql+asyncmy://{user}:{pwd}@{host}:{port}/{db}"


def sanitize_env_text_for_docker(text: str) -> str:
    """Convert Compose-style KEY = value lines to docker run --env-file format.

    Docker rejects keys with whitespace (e.g. 'UVICORN_HOST ').
    """
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key or any(ch.isspace() for ch in key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        lines.append(f"{key}={value}")
    return "\n".join(lines) + ("\n" if lines else "")


def write_docker_env_file(src: Path) -> Path:
    """Write a temp env file safe for `docker run --env-file`."""
    text = src.read_text(encoding="utf-8", errors="ignore")
    fd, path = tempfile.mkstemp(prefix="pgmig-env-", suffix=".env")
    try:
        with open(fd, "w", encoding="utf-8") as fh:
            fh.write(sanitize_env_text_for_docker(text))
    except Exception:
        Path(path).unlink(missing_ok=True)
        raise
    return Path(path)


async def _run_pasarguard_alembic(
    migrator, *args: str, url_override: str | None = None,
) -> tuple[bool, str]:
    """Run python -m alembic in panel image with host network.

    Always uses 127.0.0.1 + sanitized env. Pass url_override for intermediate DBs.
    """
    image = resolve_pasarguard_image()
    url = url_override or build_local_alembic_url(migrator.params)
    conn = get_target_connection(migrator.params)
    migrator.job.log(f"Host-network alembic: {' '.join(args)}")
    migrator.job.log(
        f"Alembic DB: user={conn.get('user')}, db={conn.get('database')}, "
        f"host={conn.get('host')}:{migration_port(conn, migrator.params.get('target_db', ''))}"
    )

    cmd: list[str] = ["docker", "run", "--rm", "--network", "host"]
    cmd.extend([
        "-e", f"SQLALCHEMY_DATABASE_URL={url}",
        "-v", f"{PASARGUARD_DATA}:/var/lib/pasarguard",
        "-w", "/code",
        "--entrypoint", "python",
        image, "-m", "alembic", *args,
    ])
    try:
        ok, out = await migrator._run_cmd(cmd, timeout=600)
    except FileNotFoundError:
        return False, "docker command not found"
    if ok or _alembic_output_indicates_success(out or ""):
        return True, out or ""
    return False, out or ""


async def run_alembic_upgrade_head(
    migrator, *, url_override: str | None = None, heal_db: str | None = None,
) -> None:
    """Upgrade schema to head only — never bootstrap to a source revision."""
    migrator.job.log("Alembic upgrade head...")
    ok, out = await _run_pasarguard_alembic(
        migrator, "upgrade", "head", url_override=url_override,
    )
    if ok:
        return
    if _is_duplicate_schema_error(out or "") and heal_db:
        migrator.job.log("Schema partially exists — healing alembic_version...")
        if await _heal_alembic_duplicate_schema(migrator, heal_db, out or ""):
            ok2, out2 = await _run_pasarguard_alembic(
                migrator, "upgrade", "head", url_override=url_override,
            )
            if ok2:
                return
            out = out2 or out
    raise RuntimeError(f"Failed alembic upgrade head:\n{(out or '')[-3000:]}")


async def get_alembic_head_revision(migrator) -> str | None:
    ok, out = await _run_pasarguard_alembic(migrator, "heads")
    if not ok:
        return None
    for line in (out or "").splitlines():
        m = re.search(r"([0-9a-f]{12,})\s*\(head\)", line, re.I)
        if m:
            return m.group(1)
        m = re.match(r"^([0-9a-f]{12,})", line.strip(), re.I)
        if m:
            return m.group(1)
    return None


def _parse_upgrade_target_revision(output: str) -> str | None:
    m = re.search(r"Running upgrade\s+\S+\s*->\s*([0-9a-f]+)", output, re.I)
    if m:
        return m.group(1)
    m = re.search(r"versions/([0-9a-f]+)_", output, re.I)
    if m:
        return m.group(1)
    return None


def _is_duplicate_schema_error(output: str) -> bool:
    low = (output or "").lower()
    return "duplicatecolumn" in low or "already exists" in low


async def _heal_alembic_duplicate_schema(migrator, target_db: str, output: str) -> bool:
    """When schema already has migration changes but alembic_version lags, stamp the right revision."""
    target_rev = _parse_upgrade_target_revision(output)
    if not target_rev:
        target_rev = await get_alembic_head_revision(migrator)
    if target_rev:
        migrator.job.log(f"Healing alembic_version → {target_rev} (schema already migrated)")
        if await set_target_alembic_version(migrator, target_db, target_rev):
            return True
    migrator.job.log("Falling back to alembic stamp head...")
    if await stamp_alembic_head(migrator):
        return True
    head = await get_alembic_head_revision(migrator)
    if head:
        return await set_target_alembic_version(migrator, target_db, head)
    return False


async def _run_alembic_upgrade_head_with_heal(
    migrator, target_db: str, max_attempts: int = 6,
) -> None:
    """Run upgrade head; on duplicate-column errors heal alembic_version and retry."""
    last_out = ""
    for attempt in range(1, max_attempts + 1):
        ok, out = await _run_pasarguard_alembic(migrator, "upgrade", "head")
        last_out = out or last_out
        if ok or (out and "already at head" in (out or "").lower()):
            return
        if _is_duplicate_schema_error(out or ""):
            migrator.job.log(f"Alembic duplicate schema (attempt {attempt}/{max_attempts}) — healing...")
            if await _heal_alembic_duplicate_schema(migrator, target_db, out or ""):
                continue
        break
    raise RuntimeError(
        "Failed to sync Alembic before PasarGuard startup. "
        f"The wizard could not align alembic_version with the database schema.\n{last_out[-3000:]}"
    )


async def sync_alembic_for_startup(migrator, target_db: str) -> None:
    """
    Align alembic_version with physical schema BEFORE PasarGuard all-in-one starts.
    Prevents DuplicateColumnError on panel restart after cross-DB migration.
    """
    cwd = str(PASARGUARD_DIR)
    await migrator._run_cmd(["docker", "compose", "stop", "pasarguard"], cwd=cwd, timeout=120)

    if target_db == "sqlite":
        migrator.job.log("SQLite target — running alembic upgrade head (one-shot)...")
        await _run_alembic_upgrade_head_with_heal(migrator, target_db)
        return

    if target_db not in ("postgresql", "timescaledb", "mysql", "mariadb"):
        return

    current = await read_target_alembic_version(migrator, target_db)
    migrator.job.log(f"Target alembic before sync: {current or '(none)'}")

    migrator.job.log("Running alembic upgrade head (one-shot, before panel start)...")
    await _run_alembic_upgrade_head_with_heal(migrator, target_db)
    final = await read_target_alembic_version(migrator, target_db)
    migrator.job.log(f"Alembic ready for startup: {final or 'head'}")


async def safe_start_pasarguard(migrator) -> None:
    """Start PasarGuard and fail if the panel does not become healthy."""
    cwd = str(PASARGUARD_DIR)
    target_db = (migrator.params or {}).get("target_db")
    # After DROP SCHEMA CASCADE, PgBouncer may hold stale enum OIDs
    if target_db in ("postgresql", "timescaledb"):
        from app.services.pasarguard_ops import _compose_text
        import re
        text = _compose_text()
        if re.search(r"^\s*pgbouncer\s*:", text, re.M):
            migrator.job.log("Restarting pgbouncer before panel start (clear type cache)...")
            await migrator._run_cmd(
                ["docker", "compose", "restart", "pgbouncer"],
                cwd=cwd,
                timeout=120,
            )
            await asyncio.sleep(3)

    migrator.job.log("Starting PasarGuard panel...")
    await migrator._run_cmd(
        ["docker", "compose", "up", "-d", "--force-recreate", "pasarguard"],
        cwd=cwd,
        timeout=180,
    )
    await verify_pasarguard_healthy(migrator)


async def stamp_alembic_head(migrator) -> bool:
    ok, out = await _run_pasarguard_alembic(migrator, "stamp", "head")
    if ok:
        migrator.job.log("Alembic stamped to head")
        return True
    head = await get_alembic_head_revision(migrator)
    target_db = migrator.params.get("target_db")
    if head and target_db and await set_target_alembic_version(migrator, target_db, head):
        migrator.job.log(f"Alembic stamped to head via SQL ({head})")
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
    """After db-migrations — sync alembic before PasarGuard starts."""
    await sync_alembic_for_startup(migrator, target_db)


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
        pwd_q = (pwd or "").replace('"', '\\"')
        for bin_name in mysql_client_bins(target_db, service):
            cmd = (
                f'cd "{cwd}" && docker compose exec -T {service} '
                f'{bin_name} -u {user} -p"{pwd_q}" -h {host} {db} -e "{sql}"'
            )
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            await proc.wait()
            if proc.returncode == 0:
                migrator.job.log(
                    f"Target alembic_version set to {version} "
                    f"(db={db}, user={user}, client={bin_name})"
                )
                return True
        return False
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

    revision = source_version or "head"
    migrator.job.log(f"Running alembic upgrade {revision} (one-shot, no panel startup)...")
    ok, out = await _run_pasarguard_alembic(migrator, "upgrade", revision)
    if not ok:
        if _is_duplicate_schema_error(out or ""):
            migrator.job.log("Schema partially exists — healing alembic_version...")
            if await _heal_alembic_duplicate_schema(migrator, target_db, out or ""):
                target_version = await read_target_alembic_version(migrator, target_db)
                migrator.job.log(f"Target Alembic version after heal: {target_version}")
                return target_version
        raise RuntimeError(
            f"Failed to initialize target schema with alembic upgrade {revision}. "
            "The wizard runs alembic in a one-shot container (panel does not need to be running).\n"
            f"{(out or '')[-3000:]}"
        )

    target_version = await read_target_alembic_version(migrator, target_db)
    if not target_version:
        raise RuntimeError(
            f"Target database ({target_db}) has no Alembic schema after upgrade. "
            f"Check credentials: database '{conn.get('database')}', user '{conn.get('user')}'."
        )

    migrator.job.log(f"Target Alembic version after init: {target_version}")
    return target_version
