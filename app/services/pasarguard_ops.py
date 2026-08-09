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
    "Can't locate revision identified by",
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

# Stamp Marzban-shaped DBs (still have `proxies`) just before PasarGuard transforms
# (gozargah_node → groups → migrate_to_groups → move/drop proxies).
_MARZBAN_BRIDGE_REVISIONS = (
    "0b62f893092b",  # parent of c41c441de44c (gozargah-node)
    "2b231de97dc3",  # common shared Marzban revision
    "dd725e4d3628",
    "6980e98bba01",
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


async def fetch_compose_logs(
    migrator,
    services: list[str],
    tail: int = 200,
    *,
    timeout: int = 30,
) -> str:
    """Fetch compose logs quietly — do not echo every line into the job UI."""
    cwd = str(PASARGUARD_DIR)
    ok, out = await migrator._run_cmd(
        ["docker", "compose", "logs", "--no-color", "--tail", str(tail), *services],
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
) -> str:
    """Panel logs only by default — DB restart FATAL lines are not panel failures."""
    pg = await fetch_compose_logs(migrator, ["pasarguard"], tail=tail, timeout=timeout)
    if not include_db:
        return pg
    target_db = migrator.params.get("target_db")
    db_svc = resolve_db_service(target_db) if target_db else None
    if db_svc:
        db_logs = await fetch_compose_logs(
            migrator, [db_svc], tail=min(tail, 80), timeout=min(timeout, 20)
        )
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
    # Quiet + short timeouts: during MySQL bigint ALTER, docker can stall.
    ok, out = await migrator._run_cmd(
        ["docker", "compose", "ps", "--format", "{{.Name}} {{.Status}}", "pasarguard"],
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
        ["docker", "compose", "ps", "-q", "pasarguard"],
        cwd=cwd,
        timeout=10,
        quiet=True,
    )
    if ok2 and (ids or "").strip():
        cid = (ids or "").strip().splitlines()[0].strip()
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
            cmd = (
                f'cd "{cwd}" && docker compose exec -T {service} '
                f'{bin_name} -u {user} -p"{pwd_q}" -h {host} -N -e '
                f'"SHOW FULL PROCESSLIST"'
            )
            ok, out = await migrator._run_cmd(cmd, timeout=10, quiet=True)
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


async def _pasarguard_container_running(migrator) -> bool:
    return (await _pasarguard_container_state(migrator)) == "running"


async def _ensure_pasarguard_up(migrator) -> None:
    cwd = str(PASARGUARD_DIR)
    await migrator._run_cmd(
        ["docker", "compose", "up", "-d", "pasarguard"],
        cwd=cwd,
        timeout=180,
    )


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
        )
    except Exception as e:
        migrator.job.log(f"Marzban pre-boot heal note: {e}")
        return False


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
        try:
            admin = await resolve_live_admin_connection(migrator, target_db, env_text=env)
        except RuntimeError as probe_err:
            if target_db not in ("mysql", "mariadb"):
                raise
            # Root password in the volume may differ from every .env candidate —
            # sync_mysql_roles_to_password will try skip-grant recovery.
            migrator.job.log(f"Live admin probe failed ({probe_err}) — sync with recovery")
            admin = {
                "user": "root",
                "password": url_pwd,
                "database": parsed.get("database")
                or read_env_var(env, "DB_NAME")
                or "pasarguard",
            }
        if target_db in ("mysql", "mariadb"):
            await sync_mysql_roles_to_password(
                migrator,
                target_db,
                admin,
                app_user=app_user,
                password=url_pwd,
                env_text=env,
                db_name=parsed.get("database") or read_env_var(env, "DB_NAME") or "pasarguard",
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


def _alembic_bootstrap_active(
    output: str,
    *,
    started_at: float,
    now: float,
    window: float = _ALEMBIC_BOOTSTRAP_WINDOW,
) -> bool:
    """True briefly while alembic printed Context/DDL assume but not Running upgrade yet.

    Prevents a soft recreate / silent stamp-heal from firing in the gap before the
    first revision line. Hard failures and startups clear this. Light installs that
    never touch alembic are unaffected (no bootstrap markers).
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
    tail = "\n".join(text.splitlines()[-40:])
    return any(m in tail for m in _ALEMBIC_BOOTSTRAP_MARKERS)


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


def _alembic_wait_active(
    output: str,
    *,
    last_upgrade_sig: str | None,
    last_progress_at: float,
    now: float,
    stuck_limit: float,
    started_at: float | None = None,
    bootstrap_window: float = _ALEMBIC_BOOTSTRAP_WINDOW,
) -> bool:
    """True while alembic should be treated as in-progress.

    Remembers the last seen 'Running upgrade' so a temporary docker logs/ps
    timeout (common during heavy MySQL ALTER) does not look like a dead panel.
    Also covers the short Context-only bootstrap window before the first upgrade.
    """
    if any(m in (output or "") for m in STARTUP_MARKERS):
        return False
    if _alembic_hard_fail(output or ""):
        return False
    if _alembic_still_running(output):
        return True
    if started_at is not None and _alembic_bootstrap_active(
        output, started_at=started_at, now=now, window=bootstrap_window
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
    """
    soft_budget = max(60, int(max_wait))
    # Hard ceiling: large Marzban→PG chains (esp. bigint id) can take a long time.
    absolute_cap = max(soft_budget * 4, 3600)
    # Same revision with no new upgrade line for this long ⇒ treat as stuck.
    # Heavy DDL (bigint on large dumps) gets a longer same-revision budget.
    # Light non-heavy stuck budget stays close to soft_budget so failed small
    # migrates do not sit until the absolute cap.
    stuck_same_upgrade_default = max(300, min(900, soft_budget))
    stuck_same_upgrade_heavy = max(3600, soft_budget * 2, 900)

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
    silent_loop_healed = False
    soft_up_during_alembic = False
    prev_restart_count = 0
    alembic_extensions = 0
    probe_i = 0
    last_upgrade_sig: str | None = None
    last_known_state = "unknown"
    last_ddl_status: str | None = None
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
                migrator, tail=log_tail, timeout=log_timeout
            )
            if _alembic_wait_active(
                out_end,
                last_upgrade_sig=last_upgrade_sig,
                last_progress_at=last_progress_at,
                now=now,
                stuck_limit=stuck_limit,
                started_at=started_at,
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
            migrator, tail=log_tail, timeout=log_timeout
        )
        # Only surface real failures into the job log (not 200× docker log spam).
        _log_failures_from_output(migrator, out)

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
        )

        # Alembic still applying — never force-recreate; only wait / soft-up if dead.
        if alembic_active:
            sig = (
                last_upgrade_sig
                or _last_alembic_upgrade_line(out)
                or ("bootstrap" if _alembic_bootstrap_active(
                    out, started_at=started_at, now=now
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

        hit = _check_logs_for_failure(out)
        if hit:
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
                if await _try_heal_duplicate_unique_names(migrator, out):
                    healed = True
                    migrator.job.log(
                        f"Panel error detected ({hit}) — Marzban pre-boot heal applied, "
                        "recreating panel…"
                    )
                if not healed:
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
        # Skip if we saw an upgrade OR are still in Context bootstrap —
        # stamp-head heal would skip remaining Marzban→PG revisions.
        restart_count = _count_restarts_in_logs(out)
        bootstrap_now = _alembic_bootstrap_active(
            out, started_at=started_at, now=now
        )
        if (
            restart_count >= 2
            and not silent_loop_healed
            and not last_upgrade_sig
            and not bootstrap_now
        ):
            has_startup = any(marker in (out or "") for marker in STARTUP_MARKERS)
            if not has_startup:
                silent_loop_healed = True
                await _heal_silent_restart_loop(migrator)
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
    last_up = last_upgrade_sig or _last_alembic_upgrade_line(out)
    if last_up and not any(m in (out or "") for m in STARTUP_MARKERS):
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
    from app.services.native_migration.sql_staging import (
        _safe_mysql_ident,
        mysql_shell_e_arg,
    )

    safe_db = _safe_mysql_ident(db)
    e_sql = mysql_shell_e_arg(
        f"SELECT version_num FROM `{safe_db}`.alembic_version LIMIT 1"
    )
    for bin_name in mysql_client_bins(target_db, service):
        cmd = (
            f'cd "{cwd}" && docker compose exec -T {service} '
            f'{bin_name} -u {user} -p"{pwd_q}" -h {host} -N -e {e_sql}'
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
        from app.services.native_migration.sql_staging import (
            _safe_mysql_ident,
            mysql_shell_e_arg,
        )

        safe_db = _safe_mysql_ident(db)
        e_sql = mysql_shell_e_arg(
            f"SELECT version_num FROM `{safe_db}`.alembic_version LIMIT 1"
        )
        for bin_name in mysql_client_bins(target_db, service):
            cmd = (
                f'cd "{cwd}" && docker compose exec -T {service} '
                f'{bin_name} -u {user} -p"{pwd_q}" -h {host} -N -e {e_sql}'
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
    return None


def _is_duplicate_schema_error(output: str) -> bool:
    low = (output or "").lower()
    return "duplicatecolumn" in low or "already exists" in low


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
    """Upgrade schema to head only — never bootstrap to a source revision."""
    migrator.job.log("Alembic upgrade head...")
    ok, out = await _run_pasarguard_alembic(
        migrator, "upgrade", "head", url_override=url_override,
    )
    if ok:
        return

    if _is_missing_revision_error(out or "") and heal_db and heal_conn:
        missing = _parse_missing_revision(out or "")
        migrator.job.log(
            f"Alembic missing revision {missing} — healing staging stamp..."
        )
        if await heal_unknown_alembic_revision(
            migrator, heal_db, heal_conn, missing_revision=missing,
        ):
            ok2, out2 = await _run_pasarguard_alembic(
                migrator, "upgrade", "head", url_override=url_override,
            )
            if ok2:
                return
            out = out2 or out

    if _is_duplicate_schema_error(out or "") and heal_db:
        migrator.job.log("Schema partially exists — healing alembic_version...")
        if await _heal_alembic_duplicate_schema(
            migrator, heal_db, out or "", heal_conn=heal_conn,
        ):
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
