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

# Multi-worker compose uses ROLE=backend instead of all-in-one banner.
PANEL_BOOT_MARKERS = STARTUP_MARKERS + (
    "Starting backend",
    "Starting all-in-one",
)

FAIL_LOG_PATTERNS = (
    "Database migrations failed",
    "ERROR: Database migrations failed",
    "Can't locate revision identified by",
    "sqlalchemy.exc.",
    "asyncpg.exceptions.",
    "DuplicateColumnError",
    "ProgrammingError",
    "Traceback (most recent call last)",
    "password authentication failed",
    "SASL authentication failed",
    "cache lookup failed for type",
    "Application startup failed",
    "SSL certificate file",
    "NATS is required when running more than 1 worker",
    "column \"user_template_id\" of relation \"next_plans\" already exists",
)

# Brief docker/DB bounce lines — do not hard-fail health when the panel port is up.
# Still surfaced in snippets when paired with "Application startup failed".
TRANSIENT_CONNECT_PATTERNS = (
    "connection refused",
    "could not connect",
)

# Stamp Marzban-shaped DBs (still have `proxies`) just before PasarGuard transforms
# (gozargah_node → groups → migrate_to_groups → move/drop proxies).
_MARZBAN_BRIDGE_REVISIONS = (
    "0b62f893092b",  # parent of c41c441de44c (gozargah-node)
    "2b231de97dc3",  # common shared Marzban revision
    "dd725e4d3628",
    "6980e98bba01",
)

# How many successive alembic "schema already applied" stamps we allow while
# waiting for the panel (one per stuck revision, e.g. expire_temp then next).
_MAX_ALEMBIC_DUP_HEALS = 6

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

# Telegram bot polling conflict — panel is usually already up; spam fills docker logs
# and pushes "Application startup complete" out of the --tail window.
TELEGRAM_NOISE_PATTERNS = (
    "TelegramConflictError",
    "Failed to fetch updates",
    "terminated by other getUpdates request",
    "only one bot instance is running",
    "Conflict: terminated by other getUpdates",
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
    parts: list[str] = []
    for p in _active_compose_paths():
        try:
            parts.append(p.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return "\n".join(parts)


def _multi_overlay_services(text: str) -> bool:
    return bool(
        re.search(
            r"^\s*(nats|panel|node-worker|scheduler)\s*:",
            text,
            re.MULTILINE,
        )
    )


def _active_compose_paths() -> list[Path]:
    """Pick the compose file(s) PasarGuard actually uses (incl. multi-worker layout).

    When ``docker-compose.multi.yml`` defines worker-stack services, return both the
    base file and the overlay so DB services and NATS/panel are in one project.
    """
    main: Path | None = None
    for name in ("docker-compose.yml", "docker-compose.yaml"):
        p = PASARGUARD_DIR / name
        if p.exists():
            main = p
            break
    multi = PASARGUARD_DIR / "docker-compose.multi.yml"
    if main is not None and multi.exists():
        try:
            multi_text = multi.read_text(encoding="utf-8", errors="ignore")
            if _multi_overlay_services(multi_text):
                return [main, multi]
        except OSError:
            pass
        return [main]
    if main is not None:
        return [main]
    if multi.exists():
        return [multi]
    return []


def compose_file_prefix() -> list[str]:
    """Extra docker compose -f args for the active compose file(s)."""
    paths = _active_compose_paths()
    if not paths:
        return []
    if len(paths) == 1 and paths[0].name in ("docker-compose.yml", "docker-compose.yaml"):
        return []
    args: list[str] = []
    for p in paths:
        args.extend(["-f", str(p)])
    return args


def panel_compose_service() -> str:
    """Compose service name for the PasarGuard panel/backend container."""
    return resolve_pasarguard_service()


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


def _is_transient_connect_line(line: str) -> bool:
    low = (line or "").lower()
    if "password authentication" in low or "sasl authentication" in low:
        return False
    return any(p in low for p in TRANSIENT_CONNECT_PATTERNS)


def _is_telegram_noise_line(line: str) -> bool:
    return any(p in (line or "") for p in TELEGRAM_NOISE_PATTERNS)


def _strip_telegram_noise(output: str) -> str:
    return "\n".join(
        ln for ln in (output or "").splitlines() if not _is_telegram_noise_line(ln)
    )


def _logs_dominated_by_telegram_noise(output: str) -> bool:
    """True when most non-empty log lines are Telegram getUpdates conflict spam."""
    lines = [
        ln
        for ln in (output or "").splitlines()
        if ln.strip() and not _is_banner_noise(ln)
    ]
    if len(lines) < 3:
        return False
    noise = sum(1 for ln in lines if _is_telegram_noise_line(ln))
    return noise >= max(3, (len(lines) + 1) // 2)


def _is_banner_noise(line: str) -> bool:
    low = line.lower()
    # Keep real failures even if they share a word with banners
    if any(p.lower() in low for p in FAIL_LOG_PATTERNS):
        return False
    return any(n.lower() in low for n in BANNER_NOISE_PATTERNS)


def _line_indicates_failure(line: str) -> bool:
    if any(b in line for b in BENIGN_LOG_PATTERNS):
        return False
    if _is_telegram_noise_line(line):
        return False
    if _is_transient_connect_line(line):
        return False
    if _is_banner_noise(line):
        return False
    # Multi-worker Uvicorn prints bare Traceback headers while workers retry;
    # the following Error/Exception line is the actionable signal.
    if "Traceback (most recent call last)" in line:
        return False
    # Bare "ValueError:" is too broad (retry noise); real crashes also emit
    # "Application startup failed" which remains a hard FAIL pattern.
    if "ValueError:" in line and "Application startup failed" not in line:
        return False
    return any(p in line for p in FAIL_LOG_PATTERNS)


def _logs_only_transient_connect_noise(output: str) -> bool:
    """True when logs show connect bounces and no hard FAIL patterns remain."""
    if _check_logs_for_failure(output):
        return False
    return any(_is_transient_connect_line(ln) for ln in (output or "").splitlines())


def _extract_failure_snippet(output: str) -> str:
    clean = _strip_ansi(output or "")
    lines = clean.splitlines()

    # Uvicorn often prints the real exception *before* generic startup failed lines.
    root_causes: list[str] = []
    for idx, ln in enumerate(lines):
        if "Application startup failed" not in ln:
            continue
        for prev in lines[max(0, idx - 45) : idx]:
            if not prev.strip() or _is_banner_noise(prev) or _is_telegram_noise_line(prev):
                continue
            pl = prev.strip()
            if any(
                x in pl
                for x in (
                    "Traceback",
                    "Error:",
                    "Exception:",
                    "ERROR:",
                    "ValueError",
                    "asyncpg",
                    "sqlalchemy",
                    "NATS",
                    "SSL",
                    "could not connect",
                    "connection refused",
                    "password authentication",
                    "SASL authentication",
                    "Can't locate revision",
                    "FATAL:",
                    "Database migrations failed",
                )
            ):
                if prev not in root_causes:
                    root_causes.append(prev)

    hits = [ln for ln in lines if _line_indicates_failure(ln)]
    if hits or root_causes:
        base = hits[-16:]
        # Multi-worker Uvicorn often prints bare Traceback headers — attach exception lines.
        extras: list[str] = []
        for idx, ln in enumerate(lines):
            if "Traceback (most recent call last)" not in ln:
                continue
            for follow in lines[idx + 1 : idx + 24]:
                fs = follow.strip()
                if not fs:
                    continue
                if fs.startswith("Traceback (most recent call last)"):
                    break
                if _is_telegram_noise_line(follow):
                    continue
                if (
                    re.match(r"^[A-Za-z_][\w.]*(?:Error|Exception):", fs)
                    or fs.startswith("RuntimeError:")
                    or "NATS is required" in fs
                ):
                    extras.append(follow)
                    break
        merged = (root_causes + base + extras)[-24:]
        if merged:
            return "\n".join(merged)
        return "\n".join(base)
    useful = []
    for ln in lines:
        if not ln.strip() or _is_banner_noise(ln) or _is_telegram_noise_line(ln):
            continue
        if any(x in ln for x in ("ERROR", "Error", "Traceback", "Exception", "failed", "FATAL", "ValueError")):
            useful.append(ln)
    if useful:
        return "\n".join(useful[-20:])
    non_banner = [
        ln
        for ln in lines
        if ln.strip() and not _is_banner_noise(ln) and not _is_telegram_noise_line(ln)
    ]
    if non_banner:
        return "\n".join(non_banner[-20:])
    # Pure Telegram spam — do not present it as the restore root cause.
    if _logs_dominated_by_telegram_noise(clean):
        return (
            "(TelegramConflictError log noise ignored — not a panel boot failure; "
            "another bot instance is polling the same token.)"
        )
    return clean[-1500:]


async def fetch_compose_logs(
    migrator,
    services: list[str],
    tail: int = 200,
    *,
    timeout: int = 30,
    since: str | None = None,
) -> str:
    """Fetch compose logs quietly — do not echo every line into the job UI."""
    cwd = str(PASARGUARD_DIR)
    prefix = compose_file_prefix()
    cmd = ["docker", "compose", *prefix, "logs", "--no-color", "--tail", str(tail)]
    if since:
        cmd.extend(["--since", since])
    cmd.extend(services)
    ok, out = await migrator._run_cmd(
        cmd,
        cwd=cwd,
        timeout=timeout,
        quiet=True,
    )
    return out if ok else ""


async def fetch_pasarguard_logs(
    migrator,
    tail: int = 150,
    *,
    include_db: bool = False,
    timeout: int = 30,
    since: str | None = None,
) -> str:
    """Panel logs only by default — DB restart FATAL lines are not panel failures."""
    panel_svc = resolve_pasarguard_service()
    pg = await fetch_compose_logs(
        migrator, [panel_svc], tail=tail, timeout=timeout, since=since,
    )
    if not include_db:
        return pg
    target_db = migrator.params.get("target_db")
    db_svc = resolve_db_service(target_db) if target_db else None
    if db_svc:
        db_logs = await fetch_compose_logs(
            migrator, [db_svc], tail=min(tail, 80), timeout=min(timeout, 20), since=since,
        )
        return f"{pg}\n{db_logs}"
    return pg


async def fetch_extended_panel_logs(migrator, tail: int = 500) -> str:
    """Full recent panel logs (no --since) for root-cause extraction after startup failure."""
    return await fetch_pasarguard_logs(migrator, tail=tail, since=None)


async def _panel_port_is_listening(migrator) -> bool:
    """Best-effort TCP check against finalized UVICORN_PORT (localhost)."""
    import socket

    from app.services.env_migration import read_env_var

    try:
        env_text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return True  # cannot read env — don't block on probe
    port_raw = (read_env_var(env_text, "UVICORN_PORT") or "8000").strip()
    try:
        port = int(port_raw)
    except ValueError:
        return True
    if port <= 0 or port > 65535:
        return True
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2.5):
            return True
    except OSError:
        migrator.job.log(f"Panel port {port} not accepting connections yet")
        return False


def _panel_startup_markers_for_stack(stack: dict | None = None) -> tuple[str, ...]:
    """Multi-worker needs Application startup complete — Uvicorn bind alone is not enough."""
    if stack and (stack.get("orchestrate") or (stack.get("uvicorn_workers") or 1) > 1):
        return ("Application startup complete", "Starting backend")
    return STARTUP_MARKERS


def _logs_show_panel_startup(output: str, stack: dict | None = None) -> bool:
    return any(marker in (output or "") for marker in _panel_startup_markers_for_stack(stack))


def _check_logs_for_failure(output: str) -> str | None:
    for line in (output or "").splitlines():
        if _line_indicates_failure(line):
            for pattern in FAIL_LOG_PATTERNS:
                if pattern in line:
                    return pattern
    return None


async def _pasarguard_container_state(migrator) -> str:
    """Return running | restarting | exited | unknown for the panel service.

    ``unknown`` is reserved for probe timeout/failure under load.
    A successful empty ``compose ps`` means the panel is not running → ``exited``
    (do not confuse a dead/missing panel with a flaky docker daemon).
    """
    cwd = str(PASARGUARD_DIR)
    panel = panel_compose_service()
    prefix = compose_file_prefix()
    # Quiet + short timeouts: during MySQL bigint ALTER, docker can stall.
    ok, out = await migrator._run_cmd(
        ["docker", "compose", *prefix, "ps", "--format", "{{.Name}} {{.Status}}", panel],
        cwd=cwd,
        timeout=12,
        quiet=True,
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
        ["docker", "compose", *prefix, "ps", "-q", panel],
        cwd=cwd,
        timeout=10,
        quiet=True,
    )
    if ok2 and (ids or "").strip():
        cid = extract_docker_container_id(ids or "")
        if not cid:
            return "unknown"
        ok3, st = await migrator._run_cmd(
            ["docker", "inspect", "-f", "{{.State.Status}}", cid],
            cwd=cwd,
            timeout=10,
            quiet=True,
        )
        status = (st or "").strip().lower()
        if status in ("running", "restarting", "exited", "dead", "created"):
            return status if status != "dead" else "exited"
        if status:
            return status
        return "unknown"

    # Successful empty probe: no running container. Check exited via ps -a.
    if ok2 and not (ids or "").strip():
        ok_a, out_a = await migrator._run_cmd(
            [
                "docker", "compose", *prefix, "ps", "-a",
                "--format", "{{.Status}}", panel,
            ],
            cwd=cwd,
            timeout=10,
            quiet=True,
        )
        if ok_a:
            text_a = (out_a or "").lower()
            if text_a.strip():
                if "restarting" in text_a:
                    return "restarting"
                if "up " in text_a or "(healthy)" in text_a or "running" in text_a:
                    return "running"
                return "exited"
            # Service has no containers at all
            return "exited"
    return "unknown"


_MYSQL_DDL_STATE_HINTS = (
    "alter table",
    "copy to tmp table",
    "copying to",
    "rename result table",
    "adding indexes",
    "repair by",
    "waiting for table metadata lock",
    "waiting for table level lock",
)


async def _mysql_ddl_status(migrator) -> str | None:
    """If MySQL/MariaDB is mid-DDL (e.g. bigint ALTER), return a short status.

    Fail-soft: never raises. Used only to heartbeats / progress refresh during
    heavy alembic — does not change restore or light-migrate paths.
    """
    target_db = (migrator.params or {}).get("target_db")
    if target_db not in ("mysql", "mariadb"):
        return None
    service = resolve_db_service(target_db)
    if not service:
        return None
    try:
        conn = _target_conn(migrator)
        user = conn.get("user") or "root"
        pwd = conn.get("password") or ""
        host = conn.get("host") or "127.0.0.1"
        if not pwd:
            return None
        pwd_q = (pwd or "").replace('"', '\\"')
        cwd = str(PASARGUARD_DIR)
        for bin_name in mysql_client_bins(target_db, service):
            cmd = [
                "docker", "compose", "exec", "-T",
                "-e", f"MYSQL_PWD={pwd}",
                service, bin_name, "-u", user, "-h", host, "-N",
                "-e", "SHOW FULL PROCESSLIST",
            ]
            ok, out = await migrator._run_cmd(cmd, cwd=cwd, timeout=10, quiet=True)
            if not ok or not (out or "").strip() or out == "Timeout":
                continue
            for line in (out or "").splitlines():
                low = line.lower()
                if any(h in low for h in _MYSQL_DDL_STATE_HINTS):
                    # Keep it short for the job log
                    snippet = " ".join(line.split())
                    return snippet[:160]
            return None
    except Exception:
        return None
    return None


async def _ensure_pasarguard_up(migrator) -> None:
    from app.services.multiworker_stack import start_panel_stack

    ok, out = await start_panel_stack(migrator.job, force_recreate=True)
    if not ok:
        migrator.job.log(f"Panel stack restart warning: {(out or '')[-500:]}")


async def _try_heal_duplicate_unique_names(migrator, logs: str) -> bool:
    """If alembic failed on duplicate names or orphan FKs, run Marzban pre-boot heal."""
    from app.services.marzban_preboot_heal import (
        heal_marzban_preboot,
        logs_indicate_marzban_preboot_issue,
    )

    if not logs_indicate_marzban_preboot_issue(logs or ""):
        return False
    try:
        stats = await heal_marzban_preboot(migrator)
        return bool(
            (stats.get("renamed") or 0)
            or (stats.get("orphans_deleted") or 0)
            or (stats.get("orphans_nulled") or 0)
            or (stats.get("usage_tables_truncated") or 0)
        )
    except Exception as e:
        migrator.job.log(f"Marzban pre-boot heal note: {e}")
        return False


async def _try_heal_db_auth_mismatch(migrator, logs: str, *, force: bool = False) -> bool:
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
    if not auth_hit and not force:
        return False

    target_db = (migrator.params or {}).get("target_db")
    if target_db not in ("mysql", "mariadb", "postgresql", "timescaledb"):
        return False

    try:
        from app.services.db_auth import (
            ensure_target_auth_ready,
            read_env_text,
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
            or read_env_var(env, "POSTGRES_PASSWORD")
            or read_env_var(env, "DB_PASSWORD")
            or read_env_var(env, "MYSQL_ROOT_PASSWORD")
            or ""
        )
        if not url_pwd:
            return False

        migrator.job.log(
            f"Auth failure in panel logs — syncing {target_db} roles for user={app_user}…"
        )
        await ensure_target_auth_ready(
            migrator,
            target_db,
            env_text=env,
            password=url_pwd,
            sync_roles=True,
            refresh_pgbouncer=True,
        )
        return True
    except Exception as e:
        migrator.job.log(f"DB auth heal note: {e}")
        return False


async def _try_heal_pgbouncer_stale(migrator) -> bool:
    """Recreate PgBouncer when its env disagrees with finalized panel credentials."""
    from app.services.db_auth import refresh_pgbouncer_if_stale

    target_db = (migrator.params or {}).get("target_db")
    if target_db not in ("postgresql", "timescaledb"):
        return False
    return await refresh_pgbouncer_if_stale(migrator, target_db)


async def _try_heal_nats_multiworker(migrator, logs: str) -> bool:
    """Align NATS env and bring NATS up before panel workers retry."""
    from app.services.db_auth import read_env_text
    from app.services.multiworker_stack import (
        NATS_SERVICE,
        align_nats_env_for_compose,
        compose_has_service,
        detect_multiworker_stack,
        ensure_nats_ready,
    )

    stack = detect_multiworker_stack()
    workers = int(stack.get("uvicorn_workers") or 1)
    if workers <= 1 and not stack.get("uses_nats"):
        return False

    low = (logs or "").lower()
    if workers <= 1 and "nats" not in low:
        return False

    if workers > 1 and not compose_has_service(NATS_SERVICE):
        migrator.job.log(
            f"UVICORN_WORKERS={workers} but no `{NATS_SERVICE}` service in compose — "
            "set UVICORN_WORKERS=1 or add NATS to docker-compose"
        )
        return False

    try:
        env = read_env_text()
        new_env = align_nats_env_for_compose(env)
        if new_env != env:
            PASARGUARD_ENV.write_text(new_env, encoding="utf-8")
            migrator.job.log("Aligned NATS_URL / NATS_ENABLED in .env for multi-worker boot")

        if compose_has_service(NATS_SERVICE):
            await ensure_nats_ready(migrator.job, force_recreate=True, required=True)
            return True
        return new_env != env
    except Exception as e:
        migrator.job.log(f"NATS multi-worker heal note: {e}")
        return False


async def _try_heal_alembic_duplicate_from_logs(migrator, logs: str) -> bool:
    """If panel alembic failed because objects already exist, align alembic_version."""
    text = logs or ""
    if not _is_duplicate_schema_error(text):
        return False
    if not _is_panel_migration_failure_context(text):
        return False

    target_db = (migrator.params or {}).get("target_db")
    if target_db not in ("postgresql", "timescaledb", "mysql", "mariadb", "sqlite"):
        return False
    try:
        migrator.job.log(
            "Alembic duplicate schema in panel logs — healing alembic_version…"
        )
        return await _heal_alembic_duplicate_schema(migrator, target_db, text)
    except Exception as e:
        migrator.job.log(f"Alembic duplicate-schema heal note: {e}")
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
    from app.services.multiworker_stack import start_panel_stack, stop_panel_stack

    await stop_panel_stack(migrator.job)
    await asyncio.sleep(3)
    ok, out = await start_panel_stack(migrator.job, force_recreate=True)
    if not ok:
        migrator.job.log(f"Silent heal recreate warning: {(out or '')[-500:]}")
    return True


# Alembic activity markers — panel is still migrating; keep waiting
_ALEMBIC_ACTIVITY_MARKERS = (
    "Running upgrade",
    "Context impl",
    "Will assume transactional DDL",
    "Will assume non-transactional DDL",
    "alembic.runtime.migration",
)

_ALEMBIC_HARD_FAIL_MARKERS = (
    "Can't locate revision",
    "Database migrations failed",
    "ERROR: Database migrations failed",
)

# Early alembic lines before the first "Running upgrade" (must not stamp/recreate yet)
_ALEMBIC_BOOTSTRAP_MARKERS = (
    "Context impl",
    "Will assume transactional DDL",
    "Will assume non-transactional DDL",
)

# Soft bring-up once if panel truly exited while we still remember an upgrade
_ALEMBIC_EXITED_SOFT_UP_AFTER = 90.0
# How long Context-only counts as active before first Running upgrade (light DBs stay short)
_ALEMBIC_BOOTSTRAP_WINDOW = 180.0


def _alembic_hard_fail(output: str) -> bool:
    text = output or ""
    return any(h in text for h in _ALEMBIC_HARD_FAIL_MARKERS)


def _alembic_still_running(output: str) -> bool:
    """True if logs show alembic mid-upgrade without a completed startup.

    Only recent 'Running upgrade' lines count as active work. Context/impl lines
    also appear on hard failures (e.g. Can't locate revision), so they must not
    alone extend forever — see `_alembic_bootstrap_active`. Stale upgrade lines
    outside the trailing window are ignored.
    """
    text = output or ""
    if any(m in text for m in STARTUP_MARKERS):
        return False
    if _alembic_hard_fail(text):
        return False
    lines = text.splitlines()
    tail = "\n".join(lines[-40:])
    return "Running upgrade" in tail


def _log_fetch_unusable(output: str) -> bool:
    """True when docker logs probe timed out / returned nothing useful."""
    text = (output or "").strip()
    if not text:
        return True
    # migrator._run_cmd / fetch helpers often surface bare "Timeout"
    return text.lower() in {"timeout", "timed out", "time out"}


def _has_alembic_bootstrap_markers(output: str) -> bool:
    """True if recent log lines show Context/DDL assume (before Running upgrade)."""
    text = output or ""
    if "Running upgrade" in text:
        return False
    tail = "\n".join(text.splitlines()[-40:])
    return any(m in tail for m in _ALEMBIC_BOOTSTRAP_MARKERS)


def _alembic_bootstrap_active(
    output: str,
    *,
    started_at: float,
    now: float,
    window: float = _ALEMBIC_BOOTSTRAP_WINDOW,
    saw_bootstrap: bool = False,
) -> bool:
    """True briefly while alembic printed Context/DDL assume but not Running upgrade yet.

    Prevents a soft recreate / silent stamp-heal from firing in the gap before the
    first revision line. Hard failures and startups clear this. Light installs that
    never touch alembic are unaffected (no bootstrap markers).

    ``saw_bootstrap`` remembers a prior Context sighting so an empty/timeout log
    fetch under Docker load does not drop the bootstrap wait early.
    """
    text = output or ""
    if any(m in text for m in STARTUP_MARKERS):
        return False
    if _alembic_hard_fail(text):
        return False
    if "Running upgrade" in text:
        return False
    if (now - started_at) > window:
        return False
    if saw_bootstrap and _log_fetch_unusable(text):
        return True
    return _has_alembic_bootstrap_markers(text)


def _last_alembic_upgrade_line(output: str) -> str | None:
    """Return the most recent 'Running upgrade …' line from panel logs."""
    last = None
    for line in (output or "").splitlines():
        if "Running upgrade" in line:
            last = line.strip()
    return last


def _is_heavy_alembic_upgrade(upgrade_line: str | None) -> bool:
    """Revisions that rewrite large tables — must not be interrupted by recreate."""
    low = (upgrade_line or "").lower()
    return any(
        s in low
        for s in (
            "bigint",
            "use bigint for id",
            "alter column",
            "alter table",
            "change column",
            "modify column",
            "convert.",
            "migrate data",
            "migrate_to_groups",
            "rebuild",
            "drop proxies",
            "create index",
        )
    )


def _should_refresh_alembic_progress(container_state: str | None, logs: str) -> bool:
    """Whether same-revision log lines should bump last_progress_at.

    - running / restarting: DDL may still be active with no new alembic lines
    - unknown / empty logs: docker probe timed out under load — keep waiting
    - exited: stale 'Running upgrade' in docker logs must NOT reset the stuck timer
      (otherwise a light failed migrate waits until the absolute cap)
    """
    state = (container_state or "").lower()
    if state in ("running", "restarting", "unknown"):
        return True
    if state in ("exited", "dead", "created"):
        return False
    # Empty / timed-out log fetch while state probe also failed
    if not (logs or "").strip():
        return True
    return False


def _panel_logs_show_startup_activity(output: str) -> bool:
    """Evidence the panel started all-in-one / alembic (not a dead/empty probe)."""
    text = output or ""
    if not text.strip():
        return False
    if "Starting all-in-one" in text:
        return True
    if "Starting backend" in text:
        return True
    return any(m in text for m in _ALEMBIC_ACTIVITY_MARKERS)


def _container_confirmed_dead(container_state: str | None) -> bool:
    """True when docker successfully reported the panel is not running."""
    return (container_state or "").lower() in ("exited", "dead", "created")


def _alembic_wait_active(
    output: str,
    *,
    last_upgrade_sig: str | None,
    last_progress_at: float,
    now: float,
    stuck_limit: float,
    started_at: float | None = None,
    bootstrap_window: float = _ALEMBIC_BOOTSTRAP_WINDOW,
    saw_bootstrap: bool = False,
    container_state: str | None = None,
) -> bool:
    """True while alembic should be treated as in-progress.

    Remembers the last seen 'Running upgrade' so a temporary docker logs/ps
    timeout (common during heavy MySQL ALTER) does not look like a dead panel.
    Also covers the short Context-only bootstrap window before the first upgrade,
    including remembered bootstrap when log fetch times out.

    Context-only bootstrap is NOT treated as active when the panel container is
    confirmed exited/missing — stale Context lines must not freeze restore at 90%.
    """
    if any(m in (output or "") for m in STARTUP_MARKERS):
        return False
    if _alembic_hard_fail(output or ""):
        return False
    dead = _container_confirmed_dead(container_state)
    if _alembic_still_running(output):
        # Stale upgrade lines while panel is dead: rely on remembered sig + stuck timer
        # (last_progress_at is not refreshed for exited — see verify loop).
        if not dead:
            return True
    if (
        not dead
        and started_at is not None
        and _alembic_bootstrap_active(
            output,
            started_at=started_at,
            now=now,
            window=bootstrap_window,
            saw_bootstrap=saw_bootstrap,
        )
    ):
        return True
    # Log fetch may have timed out / been empty while DDL still runs.
    if last_upgrade_sig and (now - last_progress_at) < stuck_limit:
        return True
    return False


async def verify_pasarguard_healthy(migrator, max_wait: int = 180) -> None:
    """Fail unless PasarGuard logs show a clean startup (no migration errors).

    max_wait is a soft budget. While alembic is clearly still applying revisions,
    the wait is extended so long schema upgrades (e.g. custom Marzban → PasarGuard
    on large MySQL dumps — bigint id alters, etc.) are not aborted early.

    Critical: never recreate / force-up the panel while alembic is mid-upgrade —
    that interrupts MySQL ALTER TABLE and leaves the migrate stuck at ~70%.

    Light / clean DBs still finish on normal startup markers within soft_budget;
    long waits only engage when alembic activity is observed.

    Log markers are scoped with ``docker compose logs --since`` from this wait's
    start so a previous boot's "Application startup complete" cannot false-pass.
    """
    from datetime import datetime, timezone
    from app.services.multiworker_stack import detect_multiworker_stack

    stack = detect_multiworker_stack()
    soft_budget = max(60, int(max_wait))
    if stack["orchestrate"]:
        soft_budget = max(soft_budget, 240)
        migrator.job.log(
            f"Multi-worker stack (workers={stack['uvicorn_workers']}, "
            f"nats={'yes' if stack['uses_nats'] else 'no'}) — "
            f"health budget {soft_budget}s"
        )
    # Hard ceiling: large Marzban→PG chains (esp. bigint id) can take a long time.
    absolute_cap = max(soft_budget * 4, 3600)
    # Same revision with no new upgrade line for this long ⇒ treat as stuck.
    # Heavy DDL (bigint on large dumps) gets a longer same-revision budget.
    # Light non-heavy stuck budget stays close to soft_budget so failed small
    # migrates do not sit until the absolute cap.
    stuck_same_upgrade_default = max(300, min(900, soft_budget))
    stuck_same_upgrade_heavy = max(3600, soft_budget * 2, 900)

    # Ignore startup markers from before this verification window.
    boot_since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    migrator.job.log(
        f"Verifying PasarGuard started without errors "
        f"(budget {soft_budget}s, alembic cap {absolute_cap}s)..."
    )
    await asyncio.sleep(8)

    stable_ready = 0
    not_running_streak = 0
    unknown_streak = 0
    restarting_streak = 0
    healed_once = False
    alembic_dup_heals = 0
    silent_loop_healed = False
    soft_up_during_alembic = False
    prev_restart_count = 0
    alembic_extensions = 0
    probe_i = 0
    last_upgrade_sig: str | None = None
    last_known_state = "unknown"
    last_ddl_status: str | None = None
    saw_alembic_bootstrap = False
    last_progress_at = asyncio.get_event_loop().time()
    last_heartbeat_at = 0.0
    revision_started_at = asyncio.get_event_loop().time()
    started_at = asyncio.get_event_loop().time()
    deadline = started_at + soft_budget
    while True:
        now = asyncio.get_event_loop().time()
        heavy_mode = _is_heavy_alembic_upgrade(last_upgrade_sig)
        stuck_limit = (
            stuck_same_upgrade_heavy if heavy_mode else stuck_same_upgrade_default
        )
        # Heavy bigint chains on large dumps may exceed the default absolute cap.
        effective_cap = max(absolute_cap, 7200) if heavy_mode else absolute_cap
        # Under heavy MySQL DDL, docker is slow — short quiet probes + longer sleeps.
        log_tail = 80 if heavy_mode else 200
        log_timeout = 12 if heavy_mode else 25
        sleep_for = 20 if heavy_mode else 5

        if now >= deadline:
            # Final chance: if alembic is still active under the absolute cap, keep going.
            out_end = await fetch_pasarguard_logs(
                migrator, tail=log_tail, timeout=log_timeout, since=boot_since,
            )
            if _has_alembic_bootstrap_markers(out_end):
                saw_alembic_bootstrap = True
            if _alembic_wait_active(
                out_end,
                last_upgrade_sig=last_upgrade_sig,
                last_progress_at=last_progress_at,
                now=now,
                stuck_limit=stuck_limit,
                started_at=started_at,
                saw_bootstrap=saw_alembic_bootstrap,
                container_state=last_known_state,
            ) and (now - started_at) < effective_cap:
                extra = 180
                deadline = now + extra
                alembic_extensions += 1
                migrator.job.log(
                    f"Alembic still active at soft deadline — extending "
                    f"(+{extra}s, total {int(now - started_at)}s, "
                    f"last: {(last_upgrade_sig or _last_alembic_upgrade_line(out_end) or '?')[:120]})"
                )
            else:
                break

        out = await fetch_pasarguard_logs(
            migrator, tail=log_tail, timeout=log_timeout, since=boot_since,
        )
        # Only surface real failures into the job log (not 200× docker log spam).
        _log_failures_from_output(migrator, out)

        if _has_alembic_bootstrap_markers(out):
            saw_alembic_bootstrap = True

        probe_i += 1
        # Heavy mode: skip docker ps every other cycle (daemon often stalls on ALTER).
        if heavy_mode and last_upgrade_sig and (probe_i % 2 == 0):
            state = last_known_state
        else:
            state = await _pasarguard_container_state(migrator)
            last_known_state = state

        # Refresh alembic progress from logs when the panel still looks alive.
        if _alembic_still_running(out):
            sig = _last_alembic_upgrade_line(out) or "Running upgrade"
            if sig != last_upgrade_sig:
                last_upgrade_sig = sig
                last_progress_at = now
                revision_started_at = now
                migrator.job.log(f"Alembic progress: {sig[:160]}")
                if _is_heavy_alembic_upgrade(sig):
                    migrator.job.set_progress(
                        max(getattr(migrator.job, "progress", 0) or 0, 70),
                        "MySQL heavy schema upgrade (bigint) — please wait…",
                    )
            elif _should_refresh_alembic_progress(state, out):
                # Same revision, container alive / probe flaky — DDL may still run.
                last_progress_at = now
            # exited + stale upgrade line: do not bump last_progress_at
        elif not (out or "").strip() and last_upgrade_sig and state in (
            "running",
            "restarting",
            "unknown",
            "",
        ):
            # Log fetch timed out under load — keep memory progress alive.
            last_progress_at = now
        elif (
            _log_fetch_unusable(out)
            and saw_alembic_bootstrap
            and not last_upgrade_sig
            and state in ("running", "restarting", "unknown", "")
            and (now - started_at) <= _ALEMBIC_BOOTSTRAP_WINDOW
        ):
            # Bootstrap Context was seen, then docker logs timed out — keep waiting.
            last_progress_at = now

        # Confirm MySQL is still rewriting tables (real progress under silent logs).
        if heavy_mode and (probe_i % 2 == 1):
            ddl = await _mysql_ddl_status(migrator)
            if ddl:
                last_ddl_status = ddl
                last_progress_at = now

        alembic_active = _alembic_wait_active(
            out,
            last_upgrade_sig=last_upgrade_sig,
            last_progress_at=last_progress_at,
            now=now,
            stuck_limit=stuck_limit,
            started_at=started_at,
            saw_bootstrap=saw_alembic_bootstrap,
            container_state=state,
        )

        # Alembic still applying — never force-recreate; only wait / soft-up if dead.
        if alembic_active:
            sig = (
                last_upgrade_sig
                or _last_alembic_upgrade_line(out)
                or ("bootstrap" if _alembic_bootstrap_active(
                    out,
                    started_at=started_at,
                    now=now,
                    saw_bootstrap=saw_alembic_bootstrap,
                ) else "Running upgrade")
            )
            elapsed = int(now - started_at)
            same_for = int(now - last_progress_at)
            on_rev = int(now - revision_started_at)
            if same_for >= stuck_limit:
                raise RuntimeError(
                    "PasarGuard alembic appears stuck on the same revision "
                    f"for {same_for}s.\n"
                    f"Last upgrade: {sig}\n"
                    + _extract_failure_snippet(out)
                )
            if elapsed >= effective_cap:
                raise RuntimeError(
                    "PasarGuard alembic exceeded maximum wait "
                    f"({effective_cap}s) without reaching ready state.\n"
                    f"Last upgrade: {sig}\n"
                    + _extract_failure_snippet(out)
                )
            # Panel truly exited while we still remember an upgrade: soft bring-up
            # once (no --force-recreate) so a crashed runner can resume. Skip while
            # state is unknown — that usually means the host is busy with DDL.
            if (
                state == "exited"
                and last_upgrade_sig
                and not soft_up_during_alembic
                and same_for >= _ALEMBIC_EXITED_SOFT_UP_AFTER
            ):
                soft_up_during_alembic = True
                migrator.job.log(
                    "PasarGuard exited during remembered alembic — "
                    "soft bring-up once (not force-recreate)…"
                )
                await _ensure_pasarguard_up(migrator)
                last_progress_at = now
                await asyncio.sleep(8)
                continue
            # Keep soft deadline ahead while work continues
            if deadline - now < 90:
                extra = 180
                deadline = now + extra
                alembic_extensions += 1
            # Heartbeat so the UI does not look frozen during multi-minute ALTER.
            if (now - last_heartbeat_at) >= 30:
                last_heartbeat_at = now
                heavy = " [heavy DDL]" if _is_heavy_alembic_upgrade(sig) else ""
                ddl_bit = f", mysql={last_ddl_status}" if last_ddl_status else ""
                migrator.job.set_progress(
                    max(getattr(migrator.job, "progress", 0) or 0, 70),
                    f"Schema upgrade in progress ({on_rev}s on current revision)…",
                )
                migrator.job.log(
                    f"Alembic still running{heavy} — waiting "
                    f"(elapsed {elapsed}s, on-rev {on_rev}s, state={state}"
                    f"{ddl_bit}); not restarting panel..."
                )
            # Transient unknown/exited probe noise is ignored during alembic.
            not_running_streak = 0
            unknown_streak = 0
            restarting_streak = 0
            await asyncio.sleep(sleep_for)
            continue

        # Context-only + confirmed dead panel: soft bring-up once (data may already
        # be restored; do not freeze at 90% treating stale Context as live DDL).
        if (
            state == "exited"
            and saw_alembic_bootstrap
            and not last_upgrade_sig
            and not soft_up_during_alembic
        ):
            soft_up_during_alembic = True
            migrator.job.log(
                "PasarGuard exited after alembic Context (no Running upgrade) — "
                "soft bring-up once…"
            )
            await _ensure_pasarguard_up(migrator)
            await asyncio.sleep(8)
            continue

        hit = _check_logs_for_failure(out)
        if hit:
            out_full = await fetch_extended_panel_logs(migrator, tail=500)
            if out_full.strip():
                out = out_full
            # Schema-ahead of alembic_version can fail on several revisions in a
            # row (e.g. expire_temp then the next ADD COLUMN). Allow a small
            # stamp budget independent of the one-shot recreate for other heals.
            if (
                alembic_dup_heals < _MAX_ALEMBIC_DUP_HEALS
                and await _try_heal_alembic_duplicate_from_logs(migrator, out)
            ):
                alembic_dup_heals += 1
                migrator.job.log(
                    f"Panel error detected ({hit}) — alembic_version healed for "
                    f"duplicate schema ({alembic_dup_heals}/{_MAX_ALEMBIC_DUP_HEALS}), "
                    "recreating panel…"
                )
                await _ensure_pasarguard_up(migrator)
                await asyncio.sleep(10)
                continue
            # Give crash-loop a moment and one recreate before hard-fail
            if not healed_once:
                healed_once = True
                healed = False
                # Auto-heal MySQL/PG password drift after cross-DB (Access denied / SASL)
                if await _try_heal_db_auth_mismatch(migrator, out):
                    healed = True
                    migrator.job.log(
                        f"Panel error detected ({hit}) — DB auth healed, recreating panel…"
                    )
                # Marzban dumps: unique names / orphan FKs that block alembic
                elif await _try_heal_duplicate_unique_names(migrator, out):
                    healed = True
                    migrator.job.log(
                        f"Panel error detected ({hit}) — Marzban pre-boot heal applied, "
                        "recreating panel…"
                    )
                elif await _try_heal_nats_multiworker(migrator, out):
                    healed = True
                    migrator.job.log(
                        f"Panel error detected ({hit}) — NATS stack heal applied, "
                        "recreating panel…"
                    )
                elif (
                    hit == "Application startup failed"
                    and (migrator.params or {}).get("target_db")
                    in ("postgresql", "timescaledb", "mysql", "mariadb")
                    and await _try_heal_db_auth_mismatch(migrator, out, force=True)
                ):
                    healed = True
                    migrator.job.log(
                        f"Panel error detected ({hit}) — DB credentials re-synced, "
                        "recreating panel…"
                    )
                elif await _try_heal_pgbouncer_stale(migrator):
                    healed = True
                    migrator.job.log(
                        f"Panel error detected ({hit}) — pgbouncer recreated, "
                        "recreating panel stack…"
                    )
                if not healed:
                    migrator.job.log(
                        f"Panel error detected ({hit}) — recreating panel stack once…"
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
            ext = await fetch_extended_panel_logs(migrator, tail=500)
            snippet_src = ext if ext.strip() else out
            raise RuntimeError(
                "PasarGuard failed to start — see container logs.\n"
                + _extract_failure_snippet(snippet_src)
                + db_hint
            )

        # Detect silent restart loop: panel exits without any error log
        # (alembic context prints but uvicorn never starts)
        # Skip if we saw an upgrade OR are still in Context bootstrap —
        # stamp-head heal would skip remaining Marzban→PG revisions.
        restart_count = _count_restarts_in_logs(out)
        bootstrap_now = _alembic_bootstrap_active(
            out,
            started_at=started_at,
            now=now,
            saw_bootstrap=saw_alembic_bootstrap,
        )
        if (
            restart_count >= 2
            and not silent_loop_healed
            and not last_upgrade_sig
            and not bootstrap_now
        ):
            has_startup = _logs_show_panel_startup(out, stack)
            if not has_startup:
                silent_loop_healed = True
                await _heal_silent_restart_loop(migrator)
                boot_since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                await asyncio.sleep(15)
                prev_restart_count = 0
                continue
        prev_restart_count = restart_count

        if state != "running":
            not_running_streak += 1
            if state == "unknown":
                unknown_streak += 1
                restarting_streak = 0
            elif state == "restarting":
                restarting_streak += 1
                unknown_streak = 0
            else:
                unknown_streak = 0
                restarting_streak = 0
            migrator.job.log(f"PasarGuard container state={state} (wait {not_running_streak})")
            # docker compose ps often returns unknown/timeout while MySQL DDL
            # saturates the host — never recreate on unknown alone.
            if state == "unknown":
                if unknown_streak >= 12:
                    # Flaky docker ps under load: keep waiting only while there is
                    # evidence of *live* work — remembered Running upgrade, or
                    # Context bootstrap still inside its short window.
                    # Stale Context after a dead panel must NOT extend forever.
                    alive = bool(last_upgrade_sig) or (
                        (now - started_at) <= _ALEMBIC_BOOTSTRAP_WINDOW
                        and (
                            saw_alembic_bootstrap
                            or _panel_logs_show_startup_activity(out)
                        )
                    )
                    if (
                        alive
                        and not _alembic_hard_fail(out)
                        and (now - started_at) < effective_cap
                    ):
                        unknown_streak = 0
                        if deadline - now < 90:
                            deadline = now + 180
                        if (now - last_heartbeat_at) >= 30:
                            last_heartbeat_at = now
                            migrator.job.log(
                                "Docker state still unknown — continuing wait "
                                "(panel logs show startup/alembic activity)…"
                            )
                        await asyncio.sleep(5)
                        continue
                    raise RuntimeError(
                        "PasarGuard container state stayed unknown too long.\n"
                        + _extract_failure_snippet(out)
                    )
                await asyncio.sleep(5)
                continue
            # Docker healthcheck thrash — wait, do not recreate.
            if state == "restarting":
                if restarting_streak >= 24:
                    raise RuntimeError(
                        "PasarGuard container kept restarting without becoming ready.\n"
                        + _extract_failure_snippet(out)
                    )
                await asyncio.sleep(5)
                continue
            if not_running_streak >= 2 and not healed_once and state == "exited":
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
        unknown_streak = 0
        restarting_streak = 0
        if _logs_show_panel_startup(out, stack):
            stable_ready += 1
            if stable_ready >= 2:
                if await _panel_port_is_listening(migrator):
                    migrator.job.log("PasarGuard healthy — application startup confirmed")
                    return
                migrator.job.log(
                    "Startup marker seen but panel port not listening yet — waiting…"
                )
                stable_ready = 1
        elif _logs_dominated_by_telegram_noise(out) or _logs_only_transient_connect_noise(out):
            # Telegram spam or brief connection-refused bounce while the HTTP
            # port is already up — do not false-fail the restore.
            reason = (
                "TelegramConflictError log noise"
                if _logs_dominated_by_telegram_noise(out)
                else "transient connection noise"
            )
            if probe_i % 2 == 1:
                ext = await fetch_extended_panel_logs(migrator, tail=1500)
                if ext.strip():
                    out = ext
            if _logs_show_panel_startup(out, stack):
                stable_ready += 1
                if stable_ready >= 2 and await _panel_port_is_listening(migrator):
                    migrator.job.log(
                        f"PasarGuard healthy — startup marker found ({reason} ignored)"
                    )
                    return
                if stable_ready >= 2:
                    stable_ready = 1
            elif (
                await _panel_port_is_listening(migrator)
                and not _check_logs_for_failure(out)
            ):
                stable_ready += 1
                if stable_ready >= 2:
                    migrator.job.log(
                        f"PasarGuard healthy — panel port listening ({reason} ignored)"
                    )
                    return
            else:
                stable_ready = 0
        else:
            stable_ready = 0

        await asyncio.sleep(4)

    out = await fetch_pasarguard_logs(migrator, tail=400, since=boot_since)
    # Last chance: Telegram spam / connect bounce may have emptied markers.
    if (
        _logs_dominated_by_telegram_noise(out)
        or _logs_only_transient_connect_noise(out)
        or not _logs_show_panel_startup(out, stack)
    ):
        ext = await fetch_extended_panel_logs(migrator, tail=2000)
        if ext.strip():
            out = ext
    hit = _check_logs_for_failure(out)
    if hit:
        raise RuntimeError(
            "PasarGuard startup failed.\n" + _extract_failure_snippet(out)
        )
    if _logs_show_panel_startup(out, stack) and await _panel_port_is_listening(migrator):
        migrator.job.log(
            "PasarGuard healthy — startup confirmed after extended log scan"
        )
        return
    if (
        last_known_state == "running"
        and await _panel_port_is_listening(migrator)
        and (
            _logs_dominated_by_telegram_noise(out)
            or _logs_only_transient_connect_noise(out)
            or "(TelegramConflictError log noise ignored" in _extract_failure_snippet(out)
        )
    ):
        migrator.job.log(
            "PasarGuard healthy — panel port listening at deadline "
            "(log noise ignored)"
        )
        return
    last_up = last_upgrade_sig or _last_alembic_upgrade_line(out)
    if last_up and not _logs_show_panel_startup(out, stack):
        raise RuntimeError(
            "PasarGuard did not finish alembic / reach ready state in time.\n"
            f"Last upgrade still in progress or incomplete: {last_up}\n"
            "Large Marzban MySQL upgrades (e.g. bigint id) can take a long time — "
            "retry after updating PGClockMG, or check `docker compose logs pasarguard`.\n"
            + _extract_failure_snippet(out)
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
    cmd = ["docker", "compose", *compose_file_prefix(), "up", "-d"]
    if services:
        cmd.extend(services)
    ok, _ = await migrator._run_cmd(cmd, cwd=cwd, timeout=180)
    return ok


async def wait_pasarguard_ready(migrator, max_wait: int = 90, strict: bool = False) -> bool:
    from app.services.multiworker_stack import detect_multiworker_stack

    cwd = str(PASARGUARD_DIR)
    stack = detect_multiworker_stack()
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

        if _logs_show_panel_startup(out, stack):
            migrator.job.log("PasarGuard ready")
            return True

        ok_run, running = await migrator._run_cmd(
            [
                "docker", "compose", *compose_file_prefix(),
                "ps", "--status", "running", "-q", panel_compose_service(),
            ],
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


async def _wait_db_service(migrator, target_db: str, service: str, attempts: int = 20) -> None:
    cwd = str(PASARGUARD_DIR)
    conn = _target_conn(migrator)
    user = conn.get("user") or ("postgres" if service in ("postgresql", "timescaledb") else "root")
    pwd = conn.get("password") or "password"
    db = conn.get("database") or "pasarguard"
    host = conn.get("host") or "127.0.0.1"
    pwd_q = (pwd or "").replace('"', '\\"')

    for _ in range(attempts):
        cmds: list[list[str]] = []
        if service in ("postgresql", "timescaledb"):
            cmds = [[
                "docker", "compose", "exec", "-T",
                "-e", f"PGPASSWORD={pwd}",
                service, "psql", "-U", user, "-d", db, "-c", "SELECT 1",
            ]]
        elif service in ("mysql", "mariadb"):
            for admin_bin in mysql_admin_bins(target_db, service):
                cmds.append([
                    "docker", "compose", "exec", "-T",
                    "-e", f"MYSQL_PWD={pwd}",
                    service, admin_bin, "ping", "-h", host, "-u", user,
                ])
        else:
            return

        ready = False
        for cmd in cmds:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=cwd,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
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
        cmd = [
            "docker", "compose", "exec", "-T",
            "-e", f"PGPASSWORD={pwd}",
            service, "psql", "-U", user, "-d", db, "-tAc",
            "SELECT version_num FROM alembic_version LIMIT 1",
        ]
    elif service in ("mysql", "mariadb"):
        host = conn.get("host") or "127.0.0.1"
        pwd_q = (pwd or "").replace('"', '\\"')
        from app.services.native_migration.source_version import normalize_alembic_revision
        from app.services.native_migration.sql_staging import (
            _safe_mysql_ident,
            mysql_shell_e_arg,
        )

        safe_db = _safe_mysql_ident(db)
        e_sql = mysql_shell_e_arg(
            f"SELECT version_num FROM `{safe_db}`.alembic_version LIMIT 1"
        )
        for bin_name in mysql_client_bins(target_db, service):
            cmd = [
                "docker", "compose", "exec", "-T",
                "-e", f"MYSQL_PWD={pwd}",
                service, bin_name, "-u", user, "-h", host, "-N", "-e", e_sql,
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=cwd,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
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

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    version = (stdout or b"").decode("utf-8", errors="ignore").strip()
    from app.services.native_migration.source_version import normalize_alembic_revision

    return normalize_alembic_revision(version)


def resolve_pasarguard_service() -> str:
    """Resolve panel service; overlay wins when multi-worker is active."""
    paths = _active_compose_paths()
    if not paths:
        return "pasarguard"

    per_file: list[str] = []
    for path in reversed(paths):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name in PASARGUARD_SERVICE_CANDIDATES:
            if re.search(rf"^\s*{re.escape(name)}\s*:", text, re.MULTILINE):
                per_file.append(name)
                break

    if not per_file:
        return "pasarguard"

    if "panel" in per_file and "pasarguard" in per_file:
        from app.services.env_migration import read_env_var

        env = (
            PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
            if PASARGUARD_ENV.exists()
            else ""
        )
        workers_raw = read_env_var(env, "UVICORN_WORKERS")
        try:
            workers = max(1, int((workers_raw or "1").strip()))
        except ValueError:
            workers = 1
        nats_on = (read_env_var(env, "NATS_ENABLED") or "").strip().lower() in (
            "1", "true", "yes", "on",
        )
        if workers > 1 or nats_on:
            return "panel"
        return "pasarguard"

    return per_file[0]


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
    from urllib.parse import quote_plus

    target_db = params["target_db"]
    conn = get_target_connection(params)
    pwd = quote_plus(conn.get("password") or "")
    user = quote_plus(
        conn.get("user")
        or ("postgres" if target_db in ("postgresql", "timescaledb") else "root")
    )
    db = conn.get("database") or "pasarguard"
    port = migration_port(conn, target_db)
    if target_db in ("postgresql", "timescaledb"):
        # asyncpg maps URL ``ssl=`` to sslmode — must be disable/allow/prefer/…
        # ``timeout=20`` fails hung connect fast so endpoint rotation can continue.
        return (
            f"postgresql+asyncpg://{user}:{pwd}@127.0.0.1:{port}/{db}"
            f"?ssl=disable&timeout=20"
        )
    if target_db in ("mysql", "mariadb"):
        return f"mysql+asyncmy://{user}:{pwd}@127.0.0.1:{port}/{db}"
    path = conn.get("sqlite_path") or (PASARGUARD_DATA / "db.sqlite3").as_posix()
    return f"sqlite+aiosqlite:///{path}"


def _tcp_port_open(host: str, port: int | str, *, timeout: float = 2.0) -> bool:
    """Fast TCP probe — used so alembic does not hang 10min on dead 127.0.0.1:5432."""
    import socket

    try:
        port_i = int(port)
    except (TypeError, ValueError):
        return False
    if not host or port_i <= 0:
        return False
    try:
        with socket.create_connection((host, port_i), timeout=timeout):
            return True
    except OSError:
        return False


def _alembic_url_engine(url: str) -> str:
    """Dialect family for an alembic SQLAlchemy URL (sqlite / mysql / postgresql)."""
    from urllib.parse import urlparse

    if not url:
        return ""
    try:
        scheme = (urlparse(url).scheme or "").lower()
    except Exception:
        scheme = ""
    base = scheme.split("+", 1)[0]
    if base == "sqlite":
        return "sqlite"
    if base in ("mysql", "mariadb"):
        return "mysql"
    if base in ("postgresql", "postgres"):
        return "postgresql"
    low = url.lower()
    if low.startswith("sqlite"):
        return "sqlite"
    if "mysql" in low.split("://", 1)[0]:
        return "mysql"
    if "postgres" in low.split("://", 1)[0]:
        return "postgresql"
    return ""


def _rewrite_sqlalchemy_host_port(url: str, host: str, port: str | int) -> str:
    """Replace host:port in a SQLAlchemy URL while keeping user/pass/db/query.

    Never rewrite SQLite URLs — injecting host:port produces
    ``sqlite+aiosqlite://127.0.0.1:5432//path`` which SQLAlchemy rejects.
    """
    from urllib.parse import urlparse, urlunparse

    if not url or not host:
        return url
    if _alembic_url_engine(url) == "sqlite":
        return url
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    userinfo = ""
    if "@" in (parsed.netloc or ""):
        userinfo, _sep, _hostport = parsed.netloc.rpartition("@")
        userinfo = userinfo + "@"
    new_netloc = f"{userinfo}{host}:{int(port)}"
    return urlunparse(parsed._replace(netloc=new_netloc))


def _ensure_asyncpg_ssl_false(url: str) -> str:
    """Normalize local/docker asyncpg URLs for alembic.

    - ``ssl=disable`` (asyncpg sslmode; ``ssl=false`` is invalid)
    - ``timeout=20`` connection timeout so a hung TCP/auth path fails fast
      and endpoint rotation can continue (instead of sitting silent for 10min)
    """
    if "postgresql+asyncpg://" not in (url or ""):
        return url
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    try:
        parsed = urlparse(url)
    except Exception:
        return url
    q = dict(parse_qsl(parsed.query, keep_blank_values=True))
    # Drop legacy / invalid aliases that asyncpg rejects as sslmode.
    for key in ("ssl", "sslmode"):
        val = (q.get(key) or "").strip().lower()
        if val in ("", "false", "0", "no", "off", "none", "disable"):
            q.pop(key, None)
        elif val in ("true", "1", "yes", "on"):
            # Prefer was historically "try TLS" — for local docker we still
            # want plain TCP; callers that need TLS pass an explicit mode.
            q.pop(key, None)
    q["ssl"] = "disable"
    q.pop("sslmode", None)  # single canonical knob via ssl=
    # asyncpg connect() timeout (seconds). Keep existing if already set lower.
    try:
        existing = float(q.get("timeout") or "0")
    except (TypeError, ValueError):
        existing = 0.0
    if existing <= 0 or existing > 20:
        q["timeout"] = "20"
    return urlunparse(parsed._replace(query=urlencode(q)))


# docker compose merges stderr warnings into the same pipe as ``ps -q``.
# Example noise: time="…" level=warning msg="The \"PGADMIN_EMAIL\" variable is not set"
_DOCKER_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{12,64}$", re.I)


def extract_docker_container_id(output: str) -> str:
    """Pick a real container id from noisy ``docker compose ps -q`` output.

    Never trust the first line — Compose variable-interpolation warnings often
    land before the hex id and previously poisoned ``--network=container:…``.
    """
    found: list[str] = []
    for raw in (output or "").splitlines():
        line = (raw or "").strip()
        if not line:
            continue
        if _DOCKER_CONTAINER_ID_RE.fullmatch(line):
            found.append(line)
            continue
        tok = line.split()[0]
        if _DOCKER_CONTAINER_ID_RE.fullmatch(tok):
            found.append(tok)
    return found[-1] if found else ""


async def _compose_service_container_id(migrator, service: str) -> str:
    """Running compose service container id, or empty string."""
    ok, cid = await migrator._run_cmd(
        ["docker", "compose", *compose_file_prefix(), "ps", "-q", service],
        cwd=str(PASARGUARD_DIR),
        timeout=30,
        quiet=True,
    )
    if not ok:
        return ""
    return extract_docker_container_id(cid or "")


async def _compose_network_name(migrator, service: str) -> str:
    """First docker network attached to a compose service container."""
    container = await _compose_service_container_id(migrator, service)
    if not container:
        return ""
    ok2, nets = await migrator._run_cmd(
        [
            "docker", "inspect", "--format",
            "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}",
            container,
        ],
        timeout=20,
        quiet=True,
    )
    if not ok2:
        return ""
    for tok in (nets or "").split():
        tok = tok.strip()
        if tok:
            return tok
    return ""


def _is_alembic_url_construction_error(output: str) -> bool:
    """True when the SQLAlchemy URL itself is invalid — do not rotate endpoints."""
    low = (output or "").lower()
    if "invalid sqlite url" in low:
        return True
    if "argumenterror" in low and "sqlite" in low:
        return True
    if "could not parse rfc1738 url" in low:
        return True
    if "invalid argument" in low and "--network" in low:
        # docker run rejected a polluted --network=container:<compose warning>
        return True
    if "sslmode" in low and "must be one of" in low:
        return True
    if "clientconfigurationerror" in low and "ssl" in low:
        return True
    return False


def _is_alembic_connect_auth_error(output: str) -> bool:
    """True when alembic failed to reach / authenticate to the DB (healable)."""
    if _is_alembic_url_construction_error(output):
        return False
    text = (output or "").strip()
    # Outer docker/command timeout from _run_cmd — treat as connect hang.
    if text == "Timeout" or text.lower() == "timeout":
        return True
    low = text.lower()
    if "timed out after" in low or "command timed out" in low:
        return True
    needles = (
        "password authentication failed",
        "invalidpassworderror",
        "sasl authentication failed",
        "authentication failed for",
        "no pg_hba.conf entry",
        "could not connect",
        "connection refused",
        "connectionreseterror",
        "connectiondoesnotexisterror",
        "timeoutError",
        "timeout expired",
        "timed out",
        "network is unreachable",
        "no route to host",
        "name or service not known",
        "temporary failure in name resolution",
        "nodename nor servname",
        "gaierror",
        "ssl connection has been closed",
        "server closed the connection",
        "failed to establish a new connection",
        "cannot connect to server",
        "connection was closed",
        "oserror:",
        "asyncpg.exceptions.invalidpassworderror",
        "asyncpg.exceptions.cannotconnectnowerror",
        "could not translate host name",
    )
    if any(n.lower() in low for n in needles):
        return True
    # Mid-env.py OperationalError without a schema DDL marker → treat as connect.
    # Do NOT match bare "asyncpg" — every PG alembic failure mentions it.
    if "operationalerror" in low and not _is_duplicate_schema_error(output):
        if any(
            s in low
            for s in (
                "connect",
                "password",
                "timeout",
                "refused",
                "hba",
                "name or service not known",
                "gaierror",
                "connectionreset",
                "server closed",
            )
        ):
            return True
    return False


async def _alembic_endpoint_strategies(
    migrator, url: str,
) -> list[tuple[list[str], str, str]]:
    """Ordered ``(net_args, url, label)`` endpoints to try until alembic succeeds.

    Never route alembic DDL through PgBouncer (:6432). Prefer paths that match
    HBA / SCRAM the way the panel does (compose DNS, then DB container netns).

    Strategies follow the **URL dialect**, not ``params['target_db']``. Phase 1
    of sqlite→timescaledb upgrades an intermediate SQLite file while target_db
    is already timescaledb — rewriting that URL with PG host:port is what
    produced ``Invalid SQLite URL: sqlite+aiosqlite://127.0.0.1:5432//…``.
    """
    # Best-effort: keep compose ps -q free of PGADMIN interpolation warnings.
    try:
        from app.services.env_migration import silence_compose_pgadmin_warnings

        silence_compose_pgadmin_warnings()
    except Exception:
        pass

    url = _ensure_asyncpg_ssl_false(url)
    url_engine = _alembic_url_engine(url)
    target_db = (migrator.params or {}).get("target_db") or ""

    # File / MySQL URLs: host network only — never inject PG endpoints.
    if url_engine == "sqlite":
        return [(["--network", "host"], url, "sqlite-file")]
    if url_engine == "mysql" or (
        url_engine != "postgresql" and target_db not in ("postgresql", "timescaledb")
    ):
        return [(["--network", "host"], url, "host")]

    conn = get_target_connection(migrator.params)
    port = migration_port(conn, target_db if target_db in ("postgresql", "timescaledb") else "timescaledb")
    strategies: list[tuple[list[str], str, str]] = []
    seen: set[tuple[tuple[str, ...], str]] = set()

    def _add(net_args: list[str], rewritten: str, label: str) -> None:
        key = (tuple(net_args), rewritten)
        if key in seen:
            return
        seen.add(key)
        strategies.append(
            (net_args, _ensure_asyncpg_ssl_false(rewritten), label)
        )

    svc = resolve_db_service(
        target_db if target_db in ("postgresql", "timescaledb") else "timescaledb"
    )
    # Prefer compose DNS / DB netns FIRST. Host-loopback can accept TCP yet hang
    # on auth for minutes; that used to freeze restore UI at 93% before rotating.
    if svc:
        net = await _compose_network_name(migrator, svc)
        if net:
            _add(
                ["--network", net],
                _rewrite_sqlalchemy_host_port(url, svc, "5432"),
                f"compose-dns:{svc}",
            )

        cid = await _compose_service_container_id(migrator, svc)
        if cid:
            # Share the DB container network namespace → always hit 127.0.0.1:5432
            # inside the same netns (works when host publish + bridge HBA fail).
            _add(
                [f"--network=container:{cid}"],
                _rewrite_sqlalchemy_host_port(url, "127.0.0.1", "5432"),
                f"container-netns:{svc}",
            )

        from app.services.db_auth import (
            _resolve_pg_container_ip_endpoint,
            _resolve_pg_host_endpoint,
        )

        _img, pub_host, pub_port = await _resolve_pg_host_endpoint(migrator, svc)
        if pub_host and pub_port and _tcp_port_open(pub_host, pub_port):
            _add(
                ["--network", "host"],
                _rewrite_sqlalchemy_host_port(url, pub_host, pub_port),
                f"published:{pub_host}:{pub_port}",
            )

        _cimg, cip, cip_port = await _resolve_pg_container_ip_endpoint(migrator, svc)
        if cip and cip_port and _tcp_port_open(cip, cip_port):
            _add(
                ["--network", "host"],
                _rewrite_sqlalchemy_host_port(url, cip, cip_port),
                f"docker-bridge:{cip}:{cip_port}",
            )

    if _tcp_port_open("127.0.0.1", port):
        _add(
            ["--network", "host"],
            _rewrite_sqlalchemy_host_port(url, "127.0.0.1", port),
            "host-loopback",
        )

    if not strategies:
        _add(
            ["--network", "host"],
            _rewrite_sqlalchemy_host_port(url, "127.0.0.1", port),
            f"host-fallback:{port}",
        )
    return strategies


async def _resolve_alembic_network_and_url(
    migrator, url: str,
) -> tuple[list[str], str]:
    """Preferred docker --network args + URL (first auto-heal strategy)."""
    strategies = await _alembic_endpoint_strategies(migrator, url)
    net_args, resolved, label = strategies[0]
    if label.startswith("compose-dns:"):
        svc = label.split(":", 1)[-1]
        net = net_args[1] if len(net_args) > 1 else "?"
        migrator.job.log(
            f"Alembic via compose network '{net}' → {svc}:5432 "
            f"(host not published)"
        )
    elif label.startswith("docker-bridge:"):
        migrator.job.log(
            f"Alembic via docker-bridge {label.split(':', 1)[-1]} "
            f"(host not published — fallback)"
        )
    elif label.startswith("published:"):
        migrator.job.log(
            f"Alembic via published {label.split(':', 1)[-1]} "
            f"(127.0.0.1 not listening)"
        )
    elif label.startswith("container-netns:"):
        migrator.job.log(
            f"Alembic via DB container network namespace "
            f"({label.split(':', 1)[-1]} → 127.0.0.1:5432)"
        )
    elif label.startswith("host-fallback"):
        migrator.job.log(
            "Alembic WARNING: no reachable PG endpoint; "
            f"trying {label} (may hang or fail in env.py)"
        )
    return net_args, resolved


# Kept for tests / callers that only need the URL rewrite path.
async def _resolve_reachable_alembic_url(migrator, url: str) -> str:
    _net, resolved = await _resolve_alembic_network_and_url(migrator, url)
    return resolved


def _format_alembic_failure(output: str) -> str:
    """Surface the real exception, not mid-traceback frames for the UI."""
    clean = _strip_ansi(output or "")
    lines = clean.splitlines()
    if not lines:
        return "(no alembic output)"

    root_idxs: list[int] = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            continue
        if re.match(r"^[A-Za-z_][\w.]*(?:Error|Exception):", s):
            root_idxs.append(i)
        elif s.startswith("FATAL:") or "password authentication failed" in s.lower():
            root_idxs.append(i)
        elif "Could not connect" in s or "Connection refused" in s:
            root_idxs.append(i)
        elif "ssl" in s.lower() and ("error" in s.lower() or "required" in s.lower()):
            root_idxs.append(i)
    if root_idxs:
        i = root_idxs[-1]
        head = lines[max(0, i - 3) : i + 1]
        return "\n".join(head) + "\n---\n" + "\n".join(lines[-40:])
    return "\n".join(lines[-60:])


def build_sqlite_alembic_url(path: str | Path) -> str:
    return f"sqlite+aiosqlite:///{Path(path).as_posix()}"


def build_alembic_url_from_conn(db_type: str, conn: dict) -> str:
    """Build alembic SQLAlchemy URL for any engine from a connection dict."""
    from urllib.parse import quote_plus

    if db_type == "sqlite":
        path = conn.get("sqlite_path") or str(PASARGUARD_DATA / "db.sqlite3")
        return build_sqlite_alembic_url(path)
    pwd = quote_plus(conn.get("password") or "")
    user = quote_plus(
        conn.get("user")
        or ("postgres" if db_type in ("postgresql", "timescaledb") else "root")
    )
    db = conn.get("database") or "pasarguard"
    port = migration_port(conn, db_type)
    host = conn.get("host") or "127.0.0.1"
    if db_type in ("postgresql", "timescaledb"):
        return (
            f"postgresql+asyncpg://{user}:{pwd}@{host}:{port}/{db}"
            f"?ssl=disable&timeout=20"
        )
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


async def _run_pasarguard_alembic_once(
    migrator,
    *args: str,
    url: str,
    net_args: list[str],
    label: str,
) -> tuple[bool, str]:
    """Single alembic docker-run attempt on one endpoint."""
    image = resolve_pasarguard_image()
    conn = get_target_connection(migrator.params)
    safe_host = "127.0.0.1"
    safe_port = migration_port(conn, migrator.params.get("target_db", ""))
    url_engine = _alembic_url_engine(url)
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.hostname:
            safe_host = parsed.hostname
        if parsed.port:
            safe_port = str(parsed.port)
    except Exception:
        pass
    migrator.job.log(f"Alembic [{label}]: {' '.join(args)}")
    migrator.job.log(
        f"Alembic DB: user={conn.get('user')}, db={conn.get('database')}, "
        f"host={safe_host}:{safe_port}"
    )
    try:
        migrator.job.set_progress(
            max(getattr(migrator.job, "progress", 0) or 0, 93),
            f"Alembic {' '.join(args)} via {safe_host}:{safe_port}…",
        )
    except Exception:
        pass

    cmd: list[str] = ["docker", "run", "--rm", *net_args]
    cmd.extend([
        "-e", f"SQLALCHEMY_DATABASE_URL={url}",
        "-v", f"{PASARGUARD_DATA}:/var/lib/pasarguard",
        "-w", "/code",
        "--entrypoint", "python",
        image, "-m", "alembic", *args,
    ])
    # Empty PG schema create should finish quickly; keep sqlite upgrades longer.
    # Connect hangs fail via URL timeout=20 + this outer cap, then rotate.
    attempt_timeout = 600 if url_engine == "sqlite" else 180
    try:
        ok, out = await migrator._run_cmd(cmd, timeout=attempt_timeout)
    except FileNotFoundError:
        return False, "docker command not found"
    if ok or _alembic_output_indicates_success(out or ""):
        return True, out or ""
    return False, out or ""


async def _run_pasarguard_alembic(
    migrator, *args: str, url_override: str | None = None,
) -> tuple[bool, str]:
    """Run python -m alembic in panel image.

    Auto-heals connect/auth by rotating endpoints: compose DNS → DB container
    netns → published → docker-bridge → host loopback. Schema errors return
    immediately so callers can stamp/heal alembic_version.
    """
    base_url = url_override or build_local_alembic_url(migrator.params)
    strategies = await _alembic_endpoint_strategies(migrator, base_url)
    last_out = ""
    for idx, (net_args, url, label) in enumerate(strategies):
        ok, out = await _run_pasarguard_alembic_once(
            migrator, *args, url=url, net_args=net_args, label=label,
        )
        if ok:
            if idx > 0:
                migrator.job.log(
                    f"Alembic succeeded via endpoint [{label}] "
                    f"after {idx} prior connect attempt(s)"
                )
            return True, out or ""
        last_out = out or last_out
        # Schema/revision / bad-URL problems won't change with a different TCP path.
        if (
            _is_missing_revision_error(out or "")
            or _is_duplicate_schema_error(out or "")
            or _is_alembic_url_construction_error(out or "")
        ):
            return False, out or ""
        if idx + 1 < len(strategies) and _is_alembic_connect_auth_error(out or ""):
            migrator.job.log(
                f"Alembic [{label}] connect/auth failed — "
                f"auto-trying next endpoint ({idx + 2}/{len(strategies)})…"
            )
            continue
        # Schema / programming / unknown errors will not heal by changing TCP path.
        break
    return False, last_out


def _parse_missing_revision(output: str) -> str | None:
    """Extract revision id from Alembic 'Can't locate revision identified by 'XXX''."""
    m = re.search(
        r"Can't locate revision identified by ['\"]([0-9a-fA-F]+)['\"]",
        output or "",
    )
    return m.group(1).lower() if m else None


def _is_missing_revision_error(output: str) -> bool:
    return _parse_missing_revision(output) is not None


def _parse_upgrade_target_revision(output: str) -> str | None:
    m = re.search(r"Running upgrade\s+\S+\s*->\s*([0-9a-f]+)", output, re.I)
    if m:
        return m.group(1)
    m = re.search(r"versions/([0-9a-f]+)_", output, re.I)
    if m:
        return m.group(1)
    # asyncmy / alembic sometimes only logs the destination revision id nearby
    m = re.search(
        r"(?:upgrade(?:d)?\s+to|revision\s+)([0-9a-f]{12,})",
        output or "",
        re.I,
    )
    if m:
        return m.group(1)
    return None


# MySQL / MariaDB schema-object errno (from asyncmy / pymysql wrappers).
# 1050 ER_TABLE_EXISTS_ERROR — CREATE TABLE when table exists
# 1060 ER_DUP_FIELDNAME     — ADD COLUMN when column exists  (e.g. expire_temp)
# 1061 ER_DUP_KEYNAME       — ADD INDEX when index exists
_MYSQL_SCHEMA_DUP_ERRNO_RE = re.compile(r"\(\s*10(?:50|60|61)\s*,")
_MYSQL_SCHEMA_DUP_ERRNO_WORD_RE = re.compile(r"\berror\s+10(?:50|60|61)\b", re.I)

# Engine-native + SQLAlchemy phrasings for "schema object already present".
# Intentionally does NOT match bare "duplicatecolumn" without separators only —
# we normalize whitespace/underscores when matching CamelCase exception names.
_SCHEMA_DUP_PHRASE_RES = (
    re.compile(r"duplicate\s+column\s+name", re.I),
    re.compile(r"duplicate\s+key\s+name", re.I),
    re.compile(r"duplicate\s+table(?:\s+name)?\b", re.I),
    re.compile(r"duplicatecolumn(?:error)?", re.I),
    re.compile(r"duplicatetable(?:error)?", re.I),
    re.compile(r"duplicateobject(?:error)?", re.I),
    # PostgreSQL / SQLAlchemy: «column "x" of relation "y" already exists»
    re.compile(
        r"(?:column|table|relation|index|constraint|type|sequence)\b"
        r"[^\n]{0,120}\balready\s+exists",
        re.I,
    ),
    re.compile(
        r"already\s+exists[^\n]{0,60}\b"
        r"(?:column|table|relation|index|constraint|type|sequence)\b",
        re.I,
    ),
)

_ROLE_OR_DB_EXISTS_RE = re.compile(
    r"\b(?:role|database|user)\b[^\n]{0,80}\balready\s+exists",
    re.I,
)


def _is_role_or_database_already_exists_noise(output: str) -> bool:
    """True for CREATE ROLE / DATABASE noise that must not stamp alembic.

    Returns False when the same blob also carries a real schema-duplicate signal
    (mixed restore + panel logs).
    """
    text = output or ""
    if not _ROLE_OR_DB_EXISTS_RE.search(text):
        return False
    if _MYSQL_SCHEMA_DUP_ERRNO_RE.search(text) or _MYSQL_SCHEMA_DUP_ERRNO_WORD_RE.search(text):
        return False
    for pat in _SCHEMA_DUP_PHRASE_RES:
        if pat.search(text):
            return False
    compact = re.sub(r"[\s_]+", "", text.lower())
    if "duplicatecolumn" in compact or "duplicatetable" in compact:
        return False
    low = text.lower()
    if any(w in low for w in ("column", "table", "relation", "index", "constraint")) and (
        "already exists" in low or "duplicate" in low
    ):
        return False
    return True


def _is_duplicate_schema_error(output: str) -> bool:
    """True when alembic failed because schema objects already exist.

    Typical after cross-DB / Marzban migrate: physical schema is ahead of
    ``alembic_version``, so the next ``ADD COLUMN`` / ``CREATE TABLE`` blows up.

    Must recognize **engine-native** wording, not only SQLAlchemy's
    ``DuplicateColumnError`` / ``already exists``:

    - MySQL/MariaDB ``(1060, "Duplicate column name 'expire_temp'")``
    - MySQL/MariaDB ``(1050, "Table 'api_keys' already exists")``
    - MySQL/MariaDB ``(1061, "Duplicate key name '…'")``
    - PostgreSQL ``column "x" of relation "y" already exists``
    - SQLite ``duplicate column name: expire_temp``

    Does **not** treat ``role "…" already exists`` as schema drift.
    """
    text = output or ""
    if not text.strip():
        return False
    if _is_role_or_database_already_exists_noise(text):
        return False

    if _MYSQL_SCHEMA_DUP_ERRNO_RE.search(text) or _MYSQL_SCHEMA_DUP_ERRNO_WORD_RE.search(text):
        return True

    for pat in _SCHEMA_DUP_PHRASE_RES:
        if pat.search(text):
            return True

    # CamelCase exception names after whitespace collapse
    compact = re.sub(r"[\s_]+", "", text.lower())
    if "duplicatecolumn" in compact or "duplicatetable" in compact or "duplicateobject" in compact:
        return True

    low = text.lower()
    # Legacy soft match: object + already exists (PG / older SQLAlchemy wording)
    if "already exists" in low and any(
        w in low for w in ("column", "table", "relation", "index", "constraint", "key name")
    ):
        return True
    return False


def _is_panel_migration_failure_context(output: str) -> bool:
    """Gate heal to real panel/alembic failures (not restore ROLE noise alone)."""
    low = (output or "").lower()
    return any(
        marker in low
        for marker in (
            "database migrations failed",
            "sqlalchemy",
            "asyncmy",
            "asyncpg",
            "pymysql",
            "psycopg",
            "alembic",
            "operationalerror",
            "programmingerror",
            "duplicatecolumn",
            "running upgrade",
        )
    )


def _conn_table_exists(db_type: str, conn: dict, table: str) -> bool:
    """Check whether a table exists on a live connection (staging-safe)."""
    host = conn.get("host") or "127.0.0.1"
    port = int(migration_port(conn, db_type))
    user = conn.get("user") or (
        "postgres" if db_type in ("postgresql", "timescaledb") else "root"
    )
    password = conn.get("password") or ""
    database = conn.get("database") or "pasarguard"
    table_l = (table or "").lower()
    try:
        if db_type == "sqlite":
            path = conn.get("sqlite_path") or str(PASARGUARD_DATA / "db.sqlite3")
            db = sqlite3.connect(path)
            try:
                row = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                    (table,),
                ).fetchone()
                return bool(row)
            finally:
                db.close()
        if db_type in ("postgresql", "timescaledb"):
            import psycopg2

            with psycopg2.connect(
                host=host, port=port, dbname=database, user=user, password=password,
            ) as pg:
                with pg.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema='public' AND table_name=%s LIMIT 1",
                        (table_l,),
                    )
                    return bool(cur.fetchone())
        import pymysql

        with pymysql.connect(
            host=host, port=port, user=user, password=password,
            database=database, charset="utf8mb4",
        ) as mysql:
            with mysql.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema=%s AND table_name=%s LIMIT 1",
                    (database, table),
                )
                return bool(cur.fetchone())
    except Exception:
        return False


def write_alembic_version_on_conn(
    db_type: str, conn: dict, version: str | None,
) -> bool:
    """Clear and optionally set alembic_version on a specific connection.

    Used for intermediate/staging DBs so we never stamp the wrong compose service
    or the live Marzban source when working on a pgmig_* copy.
    """
    host = conn.get("host") or "127.0.0.1"
    port = int(migration_port(conn, db_type))
    user = conn.get("user") or (
        "postgres" if db_type in ("postgresql", "timescaledb") else "root"
    )
    password = conn.get("password") or ""
    database = conn.get("database") or "pasarguard"
    try:
        if db_type == "sqlite":
            path = Path(conn.get("sqlite_path") or PASARGUARD_DATA / "db.sqlite3")
            path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(str(path))
            try:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS alembic_version "
                    "(version_num VARCHAR(32) NOT NULL)"
                )
                db.execute("DELETE FROM alembic_version")
                if version:
                    db.execute(
                        "INSERT INTO alembic_version (version_num) VALUES (?)",
                        (version,),
                    )
                db.commit()
                return True
            finally:
                db.close()

        if db_type in ("postgresql", "timescaledb"):
            import psycopg2

            with psycopg2.connect(
                host=host, port=port, dbname=database, user=user, password=password,
            ) as pg:
                with pg.cursor() as cur:
                    cur.execute(
                        "CREATE TABLE IF NOT EXISTS alembic_version "
                        "(version_num VARCHAR(32) NOT NULL)"
                    )
                    cur.execute("DELETE FROM alembic_version")
                    if version:
                        cur.execute(
                            "INSERT INTO alembic_version (version_num) VALUES (%s)",
                            (version,),
                        )
                pg.commit()
            return True

        import pymysql

        with pymysql.connect(
            host=host, port=port, user=user, password=password,
            database=database, charset="utf8mb4", autocommit=True,
        ) as mysql:
            with mysql.cursor() as cur:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS alembic_version "
                    "(version_num VARCHAR(32) NOT NULL)"
                )
                cur.execute("DELETE FROM alembic_version")
                if version:
                    cur.execute(
                        "INSERT INTO alembic_version (version_num) VALUES (%s)",
                        (version,),
                    )
        return True
    except Exception:
        return False


async def _revision_known_to_pasarguard(migrator, revision: str) -> bool:
    if not revision:
        return False
    ok, out = await _run_pasarguard_alembic(migrator, "show", revision)
    text = (out or "").lower()
    if "can't locate" in text or "no such revision" in text or "invalid revision key" in text:
        return False
    if ok or _alembic_output_indicates_success(out or ""):
        return True
    # Infra/docker noise — do not treat as missing
    return True


async def _pick_marzban_bridge_revision(migrator) -> str | None:
    for rev in _MARZBAN_BRIDGE_REVISIONS:
        if await _revision_known_to_pasarguard(migrator, rev):
            return rev
    # Prefer a deterministic fallback so staging heal still works if `alembic show` is flaky
    return _MARZBAN_BRIDGE_REVISIONS[0] if _MARZBAN_BRIDGE_REVISIONS else None


async def heal_unknown_alembic_revision(
    migrator,
    db_type: str,
    conn: dict,
    *,
    missing_revision: str | None = None,
) -> bool:
    """Replace an unknown alembic stamp on staging/intermediate only.

    Marzban-shaped (proxies present, groups absent): stamp just before PasarGuard
    transform migrations so panel-boot can run proxies→inbounds/groups.
    PasarGuard-shaped: stamp head.
    """
    if conn.get("_ephemeral_container") is None and conn.get("_allow_live_alembic_heal") is not True:
        # Refuse to mutate a live source unless it is clearly a staging DB name.
        db_name = str(conn.get("database") or "")
        if not db_name.startswith("pgmig_") and db_type != "sqlite":
            migrator.job.log(
                "Refusing alembic heal on non-staging connection "
                f"(db={db_name}) — live Marzban left untouched"
            )
            return False

    label = missing_revision or "unknown"
    has_proxies = _conn_table_exists(db_type, conn, "proxies")
    has_groups = _conn_table_exists(db_type, conn, "groups")

    if has_proxies and not has_groups:
        bridge = await _pick_marzban_bridge_revision(migrator)
        if not bridge:
            migrator.job.log(
                f"Unknown alembic revision {label} on Marzban-shaped DB, "
                "but no bridge revision found in PasarGuard image"
            )
            return False
        migrator.job.log(
            f"Unknown alembic revision {label} — Marzban-shaped schema detected; "
            f"stamping bridge {bridge} on staging (proxies→PasarGuard transforms)"
        )
        return write_alembic_version_on_conn(db_type, conn, bridge)

    head = await get_alembic_head_revision(migrator)
    if head:
        migrator.job.log(
            f"Unknown alembic revision {label} — stamping PasarGuard head {head} on staging"
        )
        return write_alembic_version_on_conn(db_type, conn, head)

    migrator.job.log(
        f"Unknown alembic revision {label} — clearing alembic_version on staging"
    )
    return write_alembic_version_on_conn(db_type, conn, None)


async def run_alembic_upgrade_head(
    migrator,
    *,
    url_override: str | None = None,
    heal_db: str | None = None,
    heal_conn: dict | None = None,
) -> None:
    """Upgrade schema to head — auto-heal connect/auth + stamp skew until success."""
    migrator.job.log("Alembic upgrade head...")
    max_auth_heals = 2
    auth_heals = 0
    last_out = ""

    for attempt in range(1, 8):
        ok, out = await _run_pasarguard_alembic(
            migrator, "upgrade", "head", url_override=url_override,
        )
        if ok:
            if attempt > 1:
                migrator.job.log(
                    f"Alembic upgrade head succeeded after auto-heal "
                    f"(attempt {attempt})"
                )
            return
        last_out = out or last_out

        if _is_missing_revision_error(last_out) and heal_db and heal_conn:
            missing = _parse_missing_revision(last_out)
            migrator.job.log(
                f"Alembic missing revision {missing} — healing staging stamp..."
            )
            if await heal_unknown_alembic_revision(
                migrator, heal_db, heal_conn, missing_revision=missing,
            ):
                continue

        if _is_duplicate_schema_error(last_out) and heal_db:
            migrator.job.log("Schema partially exists — healing alembic_version...")
            if await _heal_alembic_duplicate_schema(
                migrator, heal_db, last_out, heal_conn=heal_conn,
            ):
                continue

        if (
            _is_alembic_connect_auth_error(last_out)
            and auth_heals < max_auth_heals
        ):
            healed = await _try_heal_db_auth_mismatch(
                migrator, last_out, force=True,
            )
            if healed:
                auth_heals += 1
                migrator.job.log(
                    f"Alembic DB auth healed "
                    f"({auth_heals}/{max_auth_heals}) — retrying upgrade head…"
                )
                continue
            # Auth heal unavailable — endpoint rotation already tried inside
            # _run_pasarguard_alembic; one more full pass in case roles settled.
            if attempt < 3:
                migrator.job.log(
                    "Alembic connect still failing — retrying endpoints…"
                )
                continue

        # Non-healable or heals exhausted
        break

    raise RuntimeError(
        "Failed alembic upgrade head:\n" + _format_alembic_failure(last_out or "")
    )


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


async def _heal_alembic_duplicate_schema(
    migrator, target_db: str, output: str, *, heal_conn: dict | None = None,
) -> bool:
    """When schema already has migration changes but alembic_version lags, stamp the right revision."""
    target_rev = _parse_upgrade_target_revision(output)
    if not target_rev:
        target_rev = await get_alembic_head_revision(migrator)
    if target_rev:
        migrator.job.log(f"Healing alembic_version → {target_rev} (schema already migrated)")
        if heal_conn is not None:
            if write_alembic_version_on_conn(target_db, heal_conn, target_rev):
                return True
        elif await set_target_alembic_version(migrator, target_db, target_rev):
            return True
    migrator.job.log("Falling back to alembic stamp head...")
    if heal_conn is not None:
        head = await get_alembic_head_revision(migrator)
        if head and write_alembic_version_on_conn(target_db, heal_conn, head):
            return True
    elif await stamp_alembic_head(migrator):
        return True
    head = await get_alembic_head_revision(migrator)
    if head:
        if heal_conn is not None:
            return write_alembic_version_on_conn(target_db, heal_conn, head)
        return await set_target_alembic_version(migrator, target_db, head)
    return False


async def _run_alembic_upgrade_head_with_heal(
    migrator, target_db: str, max_attempts: int | None = None,
) -> None:
    """Run upgrade head; auto-heal duplicate schema + connect/auth and retry."""
    attempts = _MAX_ALEMBIC_DUP_HEALS if max_attempts is None else int(max_attempts)
    attempts = max(attempts, 4)
    last_out = ""
    auth_heals = 0
    for attempt in range(1, attempts + 1):
        ok, out = await _run_pasarguard_alembic(migrator, "upgrade", "head")
        last_out = out or last_out
        if ok or (out and "already at head" in (out or "").lower()):
            return
        if _is_duplicate_schema_error(out or ""):
            migrator.job.log(
                f"Alembic duplicate schema (attempt {attempt}/{attempts}) — healing..."
            )
            if await _heal_alembic_duplicate_schema(migrator, target_db, out or ""):
                continue
        if _is_alembic_connect_auth_error(out or "") and auth_heals < 2:
            if await _try_heal_db_auth_mismatch(migrator, out or "", force=True):
                auth_heals += 1
                migrator.job.log(
                    f"Alembic auth healed ({auth_heals}/2) before panel sync — retrying…"
                )
                continue
            migrator.job.log(
                f"Alembic connect fail (attempt {attempt}/{attempts}) — "
                "retrying alternate endpoints…"
            )
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
    from app.services.multiworker_stack import stop_panel_stack

    await stop_panel_stack(migrator.job)

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


async def safe_start_pasarguard(migrator, *, health_max_wait: int | None = None) -> None:
    """Start PasarGuard and fail if the panel does not become healthy.

    health_max_wait: optional soft budget for verify_pasarguard_healthy.
    Panel-boot schema upgrades (Marzban→PasarGuard) should pass a larger value.
    """
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
                ["docker", "compose", *compose_file_prefix(), "restart", "pgbouncer"],
                cwd=cwd,
                timeout=120,
            )
            await asyncio.sleep(3)

    migrator.job.log("Starting PasarGuard panel...")
    from app.services.multiworker_stack import start_panel_stack

    ok, out = await start_panel_stack(migrator.job, force_recreate=True)
    if not ok:
        raise RuntimeError(f"PasarGuard start failed:\n{(out or '')[-2000:]}")
    wait = 180 if health_max_wait is None else int(health_max_wait)
    await verify_pasarguard_healthy(migrator, max_wait=wait)


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
        cmd = [
            "docker", "compose", "exec", "-T",
            "-e", f"PGPASSWORD={pwd}",
            service, "psql", "-U", user, "-d", db, "-c", sql,
        ]
    elif service in ("mysql", "mariadb"):
        host = conn.get("host") or "127.0.0.1"
        sql = (
            f"DELETE FROM alembic_version; "
            f"INSERT INTO alembic_version (version_num) VALUES ('{version}');"
        )
        for bin_name in mysql_client_bins(target_db, service):
            cmd = [
                "docker", "compose", "exec", "-T",
                "-e", f"MYSQL_PWD={pwd}",
                service, bin_name, "-u", user, "-h", host, db, "-e", sql,
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=cwd,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
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

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    await proc.wait()
    if proc.returncode == 0:
        migrator.job.log(f"Target alembic_version set to {version} (db={db}, user={user})")
        return True
    return False


