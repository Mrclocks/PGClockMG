"""Live database credential resolution — probe .env candidates until auth succeeds."""

from __future__ import annotations

import asyncio
import os

from app.config import PASARGUARD_DIR, PASARGUARD_ENV
from app.services.env_migration import read_env_var, read_compose_db_credentials
from app.services.pasarguard_ops import resolve_db_service, migration_port


def _unique_strings(*values: str | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        if not v:
            continue
        s = str(v).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def postgres_password_candidates(env_text: str | None) -> list[str]:
    text = env_text or ""
    compose = read_compose_db_credentials(text)
    url_pwd = None
    url = read_env_var(text, "SQLALCHEMY_DATABASE_URL") or ""
    if url:
        from app.services.env_migration import parse_sqlalchemy_url

        url_pwd = parse_sqlalchemy_url(url).get("password")
    return _unique_strings(
        read_env_var(text, "POSTGRES_PASSWORD"),
        compose.get("password"),
        read_env_var(text, "DB_PASSWORD"),
        url_pwd,
    )


def mysql_password_candidates(env_text: str | None) -> list[str]:
    text = env_text or ""
    compose = read_compose_db_credentials(text)
    url_pwd = None
    url = read_env_var(text, "SQLALCHEMY_DATABASE_URL") or ""
    if url:
        from app.services.env_migration import parse_sqlalchemy_url

        url_pwd = parse_sqlalchemy_url(url).get("password")
    return _unique_strings(
        read_env_var(text, "MYSQL_ROOT_PASSWORD"),
        read_env_var(text, "MYSQL_PASSWORD"),
        compose.get("password"),
        read_env_var(text, "DB_PASSWORD"),
        url_pwd,
    )


def install_server_password(env_text: str | None, db_type: str) -> str:
    """Canonical install password for a server target (never from sqlite backups).

    SQLite backups have no DB password — sqlite→Timescale/MySQL convert must
    authenticate only with the *installed* panel secrets (and container init).
    """
    text = env_text or ""
    if db_type in ("postgresql", "timescaledb"):
        cands = postgres_password_candidates(text)
        return cands[0] if cands else ""
    if db_type in ("mysql", "mariadb"):
        cands = mysql_password_candidates(text)
        return cands[0] if cands else ""
    return ""


def install_auth_env_for_convert(
    *,
    backup_db: str,
    target_db: str,
    install_env_snapshot: str | None,
    live_env: str | None = None,
) -> str:
    """Env text used to resolve *target* auth during convert.

    For passwordless sources (sqlite), always prefer the pre-merge install
    snapshot so a merged backup .env cannot wipe POSTGRES_/MYSQL_ secrets.
    """
    install = (install_env_snapshot or "").strip()
    live = (live_env or "").strip()
    if (backup_db or "").lower() == "sqlite":
        # SQLite has no server password — never authenticate the target from the
        # post-merge live .env (which is backup-base and often lacks POSTGRES_*).
        base = install or live
        if not base:
            return ""
        # Ensure engine password keys exist even when only the URL held the secret.
        pwd = install_server_password(base, target_db)
        if pwd and target_db in ("postgresql", "timescaledb"):
            if not read_env_var(base, "POSTGRES_PASSWORD"):
                from app.services.env_migration import _set_env_var_simple

                base = _set_env_var_simple(base, "POSTGRES_PASSWORD", pwd)
            if not read_env_var(base, "DB_PASSWORD"):
                from app.services.env_migration import _set_env_var_simple

                base = _set_env_var_simple(base, "DB_PASSWORD", pwd)
        elif pwd and target_db in ("mysql", "mariadb"):
            if not read_env_var(base, "MYSQL_ROOT_PASSWORD"):
                from app.services.env_migration import _set_env_var_simple

                base = _set_env_var_simple(base, "MYSQL_ROOT_PASSWORD", pwd)
            if not read_env_var(base, "DB_PASSWORD"):
                from app.services.env_migration import _set_env_var_simple

                base = _set_env_var_simple(base, "DB_PASSWORD", pwd)
        return base
    return install or live


def postgres_admin_users(env_text: str | None) -> list[str]:
    text = env_text or ""
    users = _unique_strings(
        read_env_var(text, "DB_USER"),
        read_env_var(text, "POSTGRES_USER"),
    )
    return users or ["postgres"]


def postgres_role_candidates(
    env_text: str | None,
    *preferred: str | None,
    container_user: str | None = None,
    include_postgres_fallback: bool = True,
) -> list[str]:
    """Ordered PostgreSQL login roles for restore/ops.

    Prefer caller/app roles and the container's ``POSTGRES_USER``. The literal
    role ``postgres`` is only a last resort — many PasarGuard/Timescale images
    never create it, so treating it as required causes false hard-fails.
    """
    text = env_text or ""
    url_user = None
    url = read_env_var(text, "SQLALCHEMY_DATABASE_URL") or ""
    if url:
        from app.services.env_migration import parse_sqlalchemy_url

        url_user = parse_sqlalchemy_url(url).get("user")
    users = _unique_strings(
        *preferred,
        container_user,
        read_env_var(text, "DB_USER"),
        read_env_var(text, "POSTGRES_USER"),
        url_user,
        read_env_var(text, "DB_NAME"),
        read_env_var(text, "POSTGRES_DB"),
    )
    if include_postgres_fallback and "postgres" not in users:
        users.append("postgres")
    return users or (["postgres"] if include_postgres_fallback else [])


def build_postgres_auth_attempts(
    env_text: str | None,
    *,
    preferred_user: str | None = None,
    preferred_password: str | None = None,
    extra_users: tuple[str | None, ...] = (),
    extra_passwords: tuple[str | None, ...] = (),
    container_user: str | None = None,
    container_password: str | None = None,
    include_trust: bool = True,
) -> list[tuple[str, str | None]]:
    """Build ``(role, password|None)`` attempts for ``psql`` inside the DB container.

    ``password is None`` means omit ``PGPASSWORD`` and rely on local socket trust
    (common in official Postgres/Timescale images). Password attempts follow.
    Missing ``postgres`` role failures should be treated as skippable by callers.
    """
    users = postgres_role_candidates(
        env_text,
        preferred_user,
        *extra_users,
        container_user=container_user,
    )
    passwords = _unique_strings(
        preferred_password,
        container_password,
        *extra_passwords,
        *postgres_password_candidates(env_text),
    )
    attempts: list[tuple[str, str | None]] = []
    seen: set[tuple[str, str]] = set()

    def _add(user: str, password: str | None) -> None:
        key = (user, "" if password is None else password)
        if key in seen:
            return
        seen.add(key)
        attempts.append((user, password))

    for user in users:
        if include_trust:
            _add(user, None)
        for pwd in passwords:
            _add(user, pwd)
    return attempts


def summarize_pg_auth_errors(
    errors: list[tuple[str, str]],
    *,
    limit: int = 4,
) -> str:
    """Pick the most useful auth/SQL errors (skip noise from missing optional roles)."""
    if not errors:
        return ""

    def _score(item: tuple[str, str]) -> tuple[int, int]:
        user, err = item
        low = (err or "").lower()
        # Deprioritize the classic false lead when role postgres was never created.
        if user == "postgres" and "does not exist" in low:
            return (3, 0)
        if "does not exist" in low and "role" in low:
            return (2, 0)
        if "password authentication failed" in low:
            return (1, 0)
        return (0, 0)

    ranked = sorted(enumerate(errors), key=lambda pair: (_score(pair[1]), pair[0]))
    parts: list[str] = []
    for _, (user, err) in ranked[:limit]:
        msg = (err or "").strip()
        if not msg:
            continue
        parts.append(f"as {user}: {msg}")
    return "\n".join(parts)


def mysql_admin_users(env_text: str | None) -> list[str]:
    text = env_text or ""
    return _unique_strings(
        read_env_var(text, "MYSQL_ROOT_USER"),
        "root",
        read_env_var(text, "DB_USER"),
    )


def target_database_name(env_text: str | None, db_type: str) -> str:
    text = env_text or ""
    compose = read_compose_db_credentials(text)
    if db_type in ("postgresql", "timescaledb"):
        return (
            compose.get("database")
            or read_env_var(text, "POSTGRES_DB")
            or read_env_var(text, "DB_NAME")
            or "pasarguard"
        )
    if db_type in ("mysql", "mariadb"):
        return (
            compose.get("database")
            or read_env_var(text, "MYSQL_DATABASE")
            or read_env_var(text, "DB_NAME")
            or "pasarguard"
        )
    return "pasarguard"


def read_env_text() -> str:
    if PASARGUARD_ENV.exists():
        return PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
    return ""


def _pg_probe_databases(env_text: str | None, db_type: str = "postgresql") -> list[str]:
    """Databases to try for auth probes (Timescale images may omit ``postgres``)."""
    text = env_text or ""
    primary = target_database_name(text, db_type)
    return _unique_strings(primary, "postgres", "pasarguard", "template1")


async def read_db_container_init_env(migrator, service: str) -> dict[str, str]:
    """Read POSTGRES_*/MYSQL_* from the running DB container (init source of truth)."""
    if not service:
        return {}
    keys = (
        "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB",
        "MYSQL_ROOT_PASSWORD", "MYSQL_PASSWORD", "MYSQL_USER", "MYSQL_DATABASE",
        "MARIADB_ROOT_PASSWORD", "MARIADB_PASSWORD", "MARIADB_USER", "MARIADB_DATABASE",
        "DB_USER", "DB_PASSWORD", "DB_NAME",
    )
    out: dict[str, str] = {}
    for key in keys:
        ok, raw = await migrator._run_cmd(
            ["docker", "compose", "exec", "-T", service, "printenv", key],
            cwd=str(PASARGUARD_DIR),
            timeout=15,
        )
        if not ok:
            continue
        val = (raw or "").strip().splitlines()
        if val and val[-1].strip():
            out[key] = val[-1].strip()
    return out


async def _probe_pg(
    migrator,
    service: str,
    user: str,
    password: str,
    database: str,
) -> bool:
    """In-container psql probe. Under local ``trust``, any password succeeds."""
    if not password:
        return False
    cmd = [
        "docker", "compose", "exec", "-T",
        "-e", f"PGPASSWORD={password}",
        service, "psql", "-U", user, "-d", database, "-tAc", "SELECT 1",
    ]
    ok, out = await migrator._run_cmd(cmd, cwd=str(PASARGUARD_DIR), timeout=25)
    return ok and "1" in (out or "")


async def _probe_pg_any_db(
    migrator,
    service: str,
    user: str,
    password: str,
    databases: list[str],
) -> str | None:
    """Return the first database name that accepts the credentials, else None."""
    for database in databases:
        if await _probe_pg(migrator, service, user, password, database):
            return database
    return None


async def _pg_in_container_is_trust(
    migrator,
    service: str,
    user: str,
    database: str,
) -> bool:
    """True when the container local socket accepts a deliberately wrong password.

    Official Postgres/Timescale images use ``host all all 127.0.0.1/32 trust`` for
    the first loopback rule, so in-container probes cannot validate credentials.
    """
    bogus = f"pgmig-trust-check-{os.getpid()}-{id(migrator)}"
    return await _probe_pg(migrator, service, user, bogus, database)


def _parse_published_port(ports_json: str, container_port: str = "5432/tcp") -> tuple[str, str]:
    """Host IP/port published for a container port. Prefers loopback bindings."""
    import json

    try:
        ports = json.loads(ports_json or "{}") or {}
    except (ValueError, TypeError):
        return "", ""
    fallback = ("", "")
    for binding in ports.get(container_port) or []:
        if not isinstance(binding, dict):
            continue
        host_ip = str(binding.get("HostIp") or "").strip()
        host_port = str(binding.get("HostPort") or "").strip()
        if not host_port:
            continue
        if host_ip in ("", "0.0.0.0", "::", "[::]"):
            host_ip = "127.0.0.1"
        if host_ip == "127.0.0.1":
            return host_ip, host_port
        if not fallback[1]:
            fallback = (host_ip, host_port)
    return fallback


def _docker_inspect_first_line(out: str | None) -> str:
    lines = (out or "").strip().splitlines()
    return lines[0].strip() if lines else ""


async def _resolve_pg_host_endpoint(migrator, service: str) -> tuple[str, str, str]:
    """Return ``(image, host, port)`` for a published host TCP probe."""
    ok, cid = await migrator._run_cmd(
        ["docker", "compose", "ps", "-q", service],
        cwd=str(PASARGUARD_DIR),
        timeout=30,
    )
    container = _docker_inspect_first_line(cid) if ok else ""
    if not container:
        return "", "", ""
    ok_img, image_out = await migrator._run_cmd(
        ["docker", "inspect", "--format", "{{.Config.Image}}", container],
        timeout=30,
    )
    image = _docker_inspect_first_line(image_out) if ok_img else ""
    ok_ports, ports_out = await migrator._run_cmd(
        ["docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}", container],
        timeout=30,
    )
    host, port = _parse_published_port(ports_out or "") if ok_ports else ("", "")
    return image, host, port


async def _resolve_pg_container_ip_endpoint(
    migrator, service: str,
) -> tuple[str, str, str]:
    """Return ``(image, container_ip, 5432)`` for TCP via docker bridge (no publish needed).

    Many Timescale installs only expose PgBouncer (:6432) and leave the DB
    unpublished. On Linux the bridge IP is reachable from the host, so SCRAM
    can still be verified without a published 5432.
    """
    ok, cid = await migrator._run_cmd(
        ["docker", "compose", "ps", "-q", service],
        cwd=str(PASARGUARD_DIR),
        timeout=30,
    )
    container = _docker_inspect_first_line(cid) if ok else ""
    if not container:
        return "", "", ""
    ok_img, image_out = await migrator._run_cmd(
        ["docker", "inspect", "--format", "{{.Config.Image}}", container],
        timeout=30,
    )
    image = _docker_inspect_first_line(image_out) if ok_img else ""
    ok_ip, ip_out = await migrator._run_cmd(
        [
            "docker", "inspect", "--format",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            container,
        ],
        timeout=30,
    )
    ip = ""
    if ok_ip:
        # Prefer the first non-empty IPv4-looking token.
        for tok in (ip_out or "").replace("\n", " ").split():
            tok = tok.strip()
            if tok and tok[0].isdigit():
                ip = tok
                break
    if not (image and ip):
        return image, "", ""
    return image, ip, "5432"


async def _resolve_pg_tcp_endpoints(
    migrator, service: str,
) -> list[tuple[str, str, str]]:
    """Ordered TCP targets: published 5432 → pgbouncer 6432 → container IP:5432."""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str, str]] = []

    def _add(image: str, host: str, port: str) -> None:
        if not (image and host and port):
            return
        key = (host, port)
        if key in seen:
            return
        seen.add(key)
        out.append((image, host, port))

    image, host, port = await _resolve_pg_host_endpoint(migrator, service)
    _add(image, host, port)

    # Panel often publishes PgBouncer only — password check via :6432 still
    # proves SCRAM; migration_port later remaps copy traffic to direct 5432.
    try:
        from app.services.multiworker_stack import compose_has_service
    except Exception:
        compose_has_service = None  # type: ignore
    if compose_has_service and compose_has_service("pgbouncer"):
        pb_img, pb_host, pb_port = await _resolve_pg_host_endpoint(migrator, "pgbouncer")
        if not pb_port:
            # Parse 6432 explicitly if Ports JSON uses that key only.
            ok, cid = await migrator._run_cmd(
                ["docker", "compose", "ps", "-q", "pgbouncer"],
                cwd=str(PASARGUARD_DIR),
                timeout=30,
            )
            container = _docker_inspect_first_line(cid) if ok else ""
            if container:
                ok_ports, ports_out = await migrator._run_cmd(
                    [
                        "docker", "inspect", "--format",
                        "{{json .NetworkSettings.Ports}}", container,
                    ],
                    timeout=30,
                )
                if ok_ports:
                    pb_host, pb_port = _parse_published_port(
                        ports_out or "", "6432/tcp",
                    )
                if not pb_img:
                    ok_img, image_out = await migrator._run_cmd(
                        [
                            "docker", "inspect", "--format",
                            "{{.Config.Image}}", container,
                        ],
                        timeout=30,
                    )
                    pb_img = _docker_inspect_first_line(image_out) if ok_img else ""
        # Prefer DB image for psql client when probing pgbouncer.
        _add(image or pb_img, pb_host, pb_port or "")

    cip_img, cip, cip_port = await _resolve_pg_container_ip_endpoint(migrator, service)
    _add(cip_img or image, cip, cip_port)

    return out


async def _probe_pg_via_host_tcp(
    migrator,
    *,
    image: str,
    host: str,
    port: str,
    user: str,
    password: str,
    database: str,
) -> bool:
    """Authenticate via host → TCP port (published or docker-bridge IP).

    Uses ``--network host`` so published ports and docker-bridge IPs both work
    on typical Linux VPS installs. Secondary to :func:`_probe_pg_scram_inside`.
    """
    if not image or not host or not port or not password:
        return False
    cmd = [
        "docker", "run", "--rm", "--network", "host",
        "--entrypoint", "psql",
        "-e", f"PGPASSWORD={password}",
        image,
        "-h", host, "-p", port, "-U", user, "-d", database, "-tAc", "SELECT 1",
    ]
    ok, out = await migrator._run_cmd(cmd, timeout=90)
    return ok and "1" in (out or "")


# In-container SCRAM proof: connect to the container's *own eth0 IP*, which hits
# ``host all all all scram-sha-256`` on official images — NOT local/127.0.0.1 trust.
# Needs no published host port, no firewall hole, no ``docker run --network host``.
_PG_SCRAM_INSIDE_SCRIPT = r"""
set +e
IP=""
for cand in $(hostname -I 2>/dev/null; hostname -i 2>/dev/null); do
  case "$cand" in
    127.*|::1|"") ;;
    *.*) IP="$cand"; break ;;
  esac
done
if [ -z "$IP" ]; then
  IP=$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1)
fi
if [ -z "$IP" ]; then
  echo "pgclockmg-scram:no-ip"
  exit 2
fi
OUT=$(psql -h "$IP" -p "${PGPORT:-5432}" -U "$PGUSER" -d "$PGDATABASE" -tAc "SELECT 1" 2>/tmp/pgclockmg-scram.err)
EC=$?
if [ "$EC" -eq 0 ] && echo "$OUT" | grep -q '^[[:space:]]*1[[:space:]]*$'; then
  echo "pgclockmg-scram:ok ip=$IP"
  exit 0
fi
echo "pgclockmg-scram:fail ip=$IP"
tail -c 240 /tmp/pgclockmg-scram.err 2>/dev/null
exit 1
"""


async def _probe_pg_scram_inside(
    migrator,
    service: str,
    user: str,
    password: str,
    database: str,
) -> bool:
    """True when ``password`` authenticates over in-container eth0 TCP (SCRAM).

    This is the ground-truth password check for Timescale/Postgres restores:
    independent of host port publishes, ufw, and ``compose run`` volume pitfalls.
    """
    if not service or not user or not password or not database:
        return False
    ok, out = await migrator._run_cmd(
        [
            "docker", "compose", "exec", "-T",
            "-e", f"PGPASSWORD={password}",
            "-e", f"PGUSER={user}",
            "-e", f"PGDATABASE={database}",
            service, "bash", "-lc", _PG_SCRAM_INSIDE_SCRIPT,
        ],
        cwd=str(PASARGUARD_DIR),
        timeout=45,
    )
    return bool(ok and "pgclockmg-scram:ok" in (out or ""))


async def _pg_eth0_is_trust(
    migrator,
    service: str,
    user: str,
    database: str,
) -> bool:
    """True when eth0 TCP also accepts a bogus password (wide-open hba)."""
    bogus = f"pgmig-eth0-trust-{os.getpid()}-{id(migrator)}"
    return await _probe_pg_scram_inside(migrator, service, user, bogus, database)


async def _probe_pg_scram_any_db(
    migrator,
    service: str,
    user: str,
    password: str,
    databases: list[str],
) -> str | None:
    for database in databases:
        if await _probe_pg_scram_inside(migrator, service, user, password, database):
            return database
    return None


async def _pick_pg_migration_endpoint(
    migrator, service: str,
) -> tuple[str, str]:
    """Best host:port for host-side asyncpg after SCRAM is proven inside."""
    endpoints = await _resolve_pg_tcp_endpoints(migrator, service)
    for _img, host, port in endpoints:
        if host in ("127.0.0.1", "localhost"):
            return host, port
    if endpoints:
        return endpoints[0][1], endpoints[0][2]
    # Last resort — rare (no bridge IP). Callers that need TCP will still fail
    # loudly later; SCRAM-inside already proved the password.
    return "127.0.0.1", "5432"


def _persist_install_pg_password(password: str, env_text: str) -> str:
    """Write POSTGRES_PASSWORD / DB_PASSWORD (+ URL) into live .env; return text."""
    try:
        from app.services.env_migration import (
            _replace_sqlalchemy_password,
            _set_env_var_simple,
            _set_sqlalchemy_url,
        )
        from app.config import PASARGUARD_ENV as _PG_ENV

        if not _PG_ENV.exists():
            return env_text
        live = _PG_ENV.read_text(encoding="utf-8", errors="ignore")
        live = _set_env_var_simple(live, "POSTGRES_PASSWORD", password)
        live = _set_env_var_simple(live, "DB_PASSWORD", password)
        url = read_env_var(live, "SQLALCHEMY_DATABASE_URL") or ""
        if url and "sqlite" not in url.lower():
            live = _set_sqlalchemy_url(
                live, _replace_sqlalchemy_password(url, password),
            )
        _PG_ENV.write_text(live, encoding="utf-8")
        return live
    except Exception:
        return env_text


def _pg_sql_literal(value: str) -> str:
    return "'" + (value or "").replace("'", "''") + "'"


async def _pg_alter_role_via_trust(
    migrator,
    service: str,
    *,
    as_user: str,
    role: str,
    password: str,
    database: str = "postgres",
) -> bool:
    """``ALTER ROLE … PASSWORD`` over the container local socket (trust / peer).

    Omits ``PGPASSWORD`` so we never depend on the stale SCRAM secret that TCP
    rejected — official images allow this on the in-container socket.
    """
    if not service or not as_user or not role or not password:
        return False
    sql = f'ALTER ROLE "{role}" WITH PASSWORD {_pg_sql_literal(password)}'
    # Prefer the named role first; then retry as the image OS user ``postgres``
    # (peer auth) when local trust was disabled for application roles.
    attempts: list[list[str]] = [
        [
            "docker", "compose", "exec", "-T",
            service, "psql", "-U", as_user, "-d", database,
            "-v", "ON_ERROR_STOP=1", "-c", sql,
        ],
        [
            "docker", "compose", "exec", "-T", "-u", "postgres",
            service, "psql", "-d", database,
            "-v", "ON_ERROR_STOP=1", "-c", sql,
        ],
    ]
    for cmd in attempts:
        ok, out = await migrator._run_cmd(cmd, cwd=str(PASARGUARD_DIR), timeout=30)
        if ok:
            return True
        low = (out or "").lower()
        # Role missing is skippable (e.g. literal postgres on Timescale images).
        if "does not exist" in low and "role" in low:
            return False
    return False


async def recover_postgres_passwords_via_trust(
    migrator,
    service: str,
    env_text: str,
    *,
    password: str,
    admin_users: list[str] | None = None,
) -> bool:
    """Force .env password onto live roles when TCP auth failed under local trust.

    MySQL has skip-grant recovery for the equivalent lockout; PostgreSQL images
    usually expose local ``trust``, which is enough to ``ALTER ROLE`` without
    knowing the previous SCRAM secret. After this, host/TCP probes match .env
    again and migration can continue.
    """
    if not password or not service:
        return False
    text = env_text or ""
    container_env: dict[str, str] = {}
    try:
        container_env = await read_db_container_init_env(migrator, service)
    except Exception:
        container_env = {}
    roles = postgres_role_candidates(
        text,
        container_env.get("POSTGRES_USER"),
        container_env.get("DB_USER"),
        include_postgres_fallback=True,
    )
    users = list(
        _unique_strings(
            *(admin_users or []),
            *postgres_admin_users(text),
            container_env.get("POSTGRES_USER"),
            container_env.get("DB_USER"),
            "postgres",
        )
    ) or ["postgres"]
    db_name = target_database_name(text, "postgresql")
    admin_dbs = _unique_strings(
        db_name,
        container_env.get("POSTGRES_DB"),
        "postgres",
        "pasarguard",
    )
    migrator.job.log(
        f"PostgreSQL trust recovery: aligning {len(roles)} role(s) to .env password "
        "(TCP rejected every candidate)..."
    )
    any_ok = False
    for role in roles:
        synced = False
        for as_user in users:
            for admin_db in admin_dbs:
                if await _pg_alter_role_via_trust(
                    migrator,
                    service,
                    as_user=as_user,
                    role=role,
                    password=password,
                    database=admin_db,
                ):
                    migrator.job.log(
                        f"Trust-synced password for role {role} (as {as_user} on {admin_db})"
                    )
                    synced = True
                    any_ok = True
                    break
            if synced:
                break
        if not synced:
            migrator.job.log(f"Trust recovery could not ALTER ROLE {role}")
    if any_ok:
        # Env strings often already match; force recreate so :6432 stops SASL-failing.
        try:
            await refresh_pgbouncer_if_stale(
                migrator,
                "postgresql",
                env_text=text,
                password=password,
                force=True,
            )
        except Exception as exc:
            migrator.job.log(f"PgBouncer refresh after trust recovery note: {exc}")
    return any_ok


def _pg_single_user_alter_script(roles: list[str], password: str) -> str:
    """Bash script: find PGDATA, run ``postgres --single`` ALTER ROLE for each role."""
    lit = _pg_sql_literal(password)
    alters = "\n".join(
        f'ALTER ROLE "{role}" WITH PASSWORD {lit};' for role in roles if role
    )
    # Do not use an f-string for the whole script — passwords may contain `{`.
    return (
        "set -e\n"
        "PGDATA=\"\"\n"
        # Prefer well-known paths, then a bounded find (Timescale HA / custom mounts).
        "for d in \\\n"
        "  \"${PGDATA:-}\" \\\n"
        "  /var/lib/postgresql/data \\\n"
        "  /home/postgres/pgdata/data \\\n"
        "  /var/lib/postgresql/pgdata \\\n"
        "  /pgdata \\\n"
        "  /var/lib/postgresql\n"
        "do\n"
        "  [ -n \"$d\" ] || continue\n"
        "  if [ -f \"$d/PG_VERSION\" ]; then\n"
        "    PGDATA=\"$d\"\n"
        "    break\n"
        "  fi\n"
        "done\n"
        "if [ -z \"$PGDATA\" ]; then\n"
        "  found=$(find /var/lib /home /pgdata /data -name PG_VERSION 2>/dev/null | head -1 || true)\n"
        "  if [ -n \"$found\" ]; then\n"
        "    PGDATA=$(dirname \"$found\")\n"
        "  fi\n"
        "fi\n"
        "if [ -z \"$PGDATA\" ]; then\n"
        "  echo \"pgclockmg-heal: PGDATA not found\" >&2\n"
        "  exit 1\n"
        "fi\n"
        "echo \"pgclockmg-heal: using PGDATA=$PGDATA\"\n"
        "run_as_pg() {\n"
        "  if [ \"$(id -u)\" = \"0\" ]; then\n"
        "    if command -v gosu >/dev/null 2>&1; then\n"
        "      gosu postgres \"$@\"\n"
        "    elif command -v su-exec >/dev/null 2>&1; then\n"
        "      su-exec postgres \"$@\"\n"
        "    elif command -v runuser >/dev/null 2>&1; then\n"
        "      runuser -u postgres -- \"$@\"\n"
        "    else\n"
        "      \"$@\"\n"
        "    fi\n"
        "  else\n"
        "    \"$@\"\n"
        "  fi\n"
        "}\n"
        "SQL=$(cat <<'PGCLOCKMG_SQL'\n"
        f"{alters}\n"
        "PGCLOCKMG_SQL\n"
        ")\n"
        # Try single-user against postgres DB name, then pasarguard (Timescale often\n"
        # has no 'postgres' DB).\n"
        "ok=0\n"
        "for db in postgres pasarguard template1; do\n"
        "  if printf '%s\\n' \"$SQL\" | run_as_pg postgres --single -D \"$PGDATA\" \"$db\"\n"
        "  then\n"
        "    ok=1\n"
        "    echo \"pgclockmg-heal: single-user ALTER via db=$db\"\n"
        "    break\n"
        "  fi\n"
        "done\n"
        "if [ \"$ok\" != \"1\" ]; then\n"
        "  echo \"pgclockmg-heal: single-user ALTER failed for all DBs\" >&2\n"
        "  exit 1\n"
        "fi\n"
        "echo \"pgclockmg-heal: single-user ALTER done\"\n"
    )


def _pg_hba_trust_prepare_script() -> str:
    """Rewrite pg_hba.conf to trust (backup first) so a normal boot accepts ALTER."""
    return (
        "set -e\n"
        "PGDATA=\"\"\n"
        "for d in /var/lib/postgresql/data /home/postgres/pgdata/data "
        "/var/lib/postgresql/pgdata /pgdata /var/lib/postgresql; do\n"
        "  if [ -f \"$d/PG_VERSION\" ]; then PGDATA=\"$d\"; break; fi\n"
        "done\n"
        "if [ -z \"$PGDATA\" ]; then\n"
        "  found=$(find /var/lib /home /pgdata /data -name PG_VERSION 2>/dev/null | head -1 || true)\n"
        "  [ -n \"$found\" ] && PGDATA=$(dirname \"$found\")\n"
        "fi\n"
        "if [ -z \"$PGDATA\" ] || [ ! -f \"$PGDATA/pg_hba.conf\" ]; then\n"
        "  echo \"pgclockmg-heal: pg_hba.conf not found\" >&2\n"
        "  exit 1\n"
        "fi\n"
        "cp -a \"$PGDATA/pg_hba.conf\" \"$PGDATA/pg_hba.conf.pgclockmg.bak\"\n"
        "cat > \"$PGDATA/pg_hba.conf\" <<'HBA'\n"
        "# pgclockmg temporary trust heal — restored after ALTER\n"
        "local all all trust\n"
        "host all all 127.0.0.1/32 trust\n"
        "host all all ::1/32 trust\n"
        "host all all 0.0.0.0/0 trust\n"
        "host all all ::/0 trust\n"
        "HBA\n"
        "echo \"pgclockmg-heal: pg_hba trust installed on $PGDATA\"\n"
    )


def _pg_hba_trust_restore_script() -> str:
    return (
        "set -e\n"
        "PGDATA=\"\"\n"
        "for d in /var/lib/postgresql/data /home/postgres/pgdata/data "
        "/var/lib/postgresql/pgdata /pgdata /var/lib/postgresql; do\n"
        "  if [ -f \"$d/PG_VERSION\" ]; then PGDATA=\"$d\"; break; fi\n"
        "done\n"
        "if [ -z \"$PGDATA\" ]; then\n"
        "  found=$(find /var/lib /home /pgdata /data -name PG_VERSION 2>/dev/null | head -1 || true)\n"
        "  [ -n \"$found\" ] && PGDATA=$(dirname \"$found\")\n"
        "fi\n"
        "bak=\"$PGDATA/pg_hba.conf.pgclockmg.bak\"\n"
        "if [ -n \"$PGDATA\" ] && [ -f \"$bak\" ]; then\n"
        "  mv -f \"$bak\" \"$PGDATA/pg_hba.conf\"\n"
        "  echo \"pgclockmg-heal: pg_hba restored\"\n"
        "else\n"
        "  echo \"pgclockmg-heal: no pg_hba backup to restore\"\n"
        "fi\n"
    )


async def _pg_service_container_id(migrator, service: str) -> str:
    """Return compose container id for ``service`` (running or stopped)."""
    cwd = str(PASARGUARD_DIR)
    for args in (
        ["docker", "compose", "ps", "-aq", service],
        ["docker", "compose", "ps", "-q", service],
    ):
        ok, out = await migrator._run_cmd(args, cwd=cwd, timeout=30)
        cid = (out or "").strip().splitlines()
        if ok and cid and cid[-1].strip():
            return cid[-1].strip()
    return ""


async def _pg_service_image(migrator, container_id: str) -> str:
    if not container_id:
        return ""
    ok, out = await migrator._run_cmd(
        ["docker", "inspect", "--format", "{{.Config.Image}}", container_id],
        timeout=30,
    )
    return (out or "").strip().splitlines()[0].strip() if ok else ""


async def _start_heal_sidecar_volumes_from(
    migrator,
    *,
    service: str,
    heal_name: str,
) -> tuple[bool, str]:
    """Start a one-shot container that mounts the *exact* live DB volume.

    ``docker compose run`` can attach a fresh anonymous volume when the image
    declares VOLUME and compose does not pin a named volume — then ALTER ROLE
    would heal a throwaway datadir while the real Timescale cluster stays
    locked. ``--volumes-from`` of the existing service container avoids that.
    """
    cwd = str(PASARGUARD_DIR)
    await migrator._run_cmd(["docker", "rm", "-f", heal_name], cwd=cwd, timeout=60)

    # Prefer stopping cleanly so single-user / hba edits are safe.
    await migrator._run_cmd(
        ["docker", "compose", "stop", service], cwd=cwd, timeout=120,
    )
    cid = await _pg_service_container_id(migrator, service)
    image = await _pg_service_image(migrator, cid) if cid else ""
    if cid and image:
        migrator.job.log(
            f"PostgreSQL heal sidecar: volumes-from={cid[:12]} image={image}"
        )
        ok, out = await migrator._run_cmd(
            [
                "docker", "run", "-d",
                "--name", heal_name,
                "--volumes-from", cid,
                "--entrypoint", "bash",
                image,
                "-lc", "sleep 3600",
            ],
            cwd=cwd,
            timeout=180,
        )
        if ok:
            return True, "volumes-from"
        migrator.job.log(
            f"PostgreSQL heal: volumes-from run failed, falling back to compose run: "
            f"{(out or '')[-200:]}"
        )

    # Fallback (still better than nothing on unusual setups).
    ok2, out2 = await migrator._run_cmd(
        [
            "docker", "compose", "run", "-d", "--no-deps",
            "--name", heal_name,
            "--entrypoint", "bash",
            service,
            "-lc", "sleep 3600",
        ],
        cwd=cwd,
        timeout=180,
    )
    if ok2:
        return True, "compose-run"
    migrator.job.log(
        f"PostgreSQL heal: could not start sidecar: {(out2 or '')[-300:]}"
    )
    return False, ""


async def recover_postgres_passwords_via_live_hba(
    migrator,
    service: str,
    env_text: str,
    *,
    password: str,
    admin_users: list[str] | None = None,
) -> bool:
    """Patch ``pg_hba.conf`` *inside the running* Timescale container, ALTER, restore.

    Does not stop the DB and cannot attach the wrong volume. This is the primary
    escalation after plain trust ALTER fails (custom scram-only local hba).
    """
    if not password or not service:
        return False

    text = env_text or ""
    container_env: dict[str, str] = {}
    try:
        container_env = await read_db_container_init_env(migrator, service)
    except Exception:
        container_env = {}
    roles = postgres_role_candidates(
        text,
        container_env.get("POSTGRES_USER"),
        container_env.get("DB_USER"),
        *(admin_users or []),
        include_postgres_fallback=True,
    ) or ["postgres", "pasarguard"]
    users = list(
        _unique_strings(
            *(admin_users or []),
            *postgres_admin_users(text),
            container_env.get("POSTGRES_USER"),
            "postgres",
        )
    ) or ["postgres"]
    admin_dbs = _unique_strings(
        target_database_name(text, "postgresql"),
        container_env.get("POSTGRES_DB"),
        "postgres",
        "pasarguard",
    )
    cwd = str(PASARGUARD_DIR)

    migrator.job.log(
        f"PostgreSQL live-HBA recovery on {service}: patch pg_hba → reload → "
        f"ALTER {len(roles)} role(s) → restore (no stop, same volume)..."
    )

    # Ensure service is up.
    await migrator._run_cmd(
        ["docker", "compose", "up", "-d", service], cwd=cwd, timeout=180,
    )
    await asyncio.sleep(2)

    patched = False
    try:
        ok, out = await migrator._run_cmd(
            [
                "docker", "compose", "exec", "-T", "-u", "root",
                service, "bash", "-lc", _pg_hba_trust_prepare_script(),
            ],
            cwd=cwd,
            timeout=60,
        )
        if not ok or "pgclockmg-heal: pg_hba trust installed" not in (out or ""):
            # Some images disallow root exec — try without -u root.
            ok2, out2 = await migrator._run_cmd(
                [
                    "docker", "compose", "exec", "-T",
                    service, "bash", "-lc", _pg_hba_trust_prepare_script(),
                ],
                cwd=cwd,
                timeout=60,
            )
            if not ok2 or "pgclockmg-heal: pg_hba trust installed" not in (out2 or ""):
                migrator.job.log(
                    f"PostgreSQL live-HBA: could not patch pg_hba: "
                    f"{(out2 or out or '')[-300:]}"
                )
                return False
        patched = True

        # Reload so trust takes effect without restart.
        reloaded = False
        for as_user in users:
            for admin_db in admin_dbs:
                for cmd in (
                    [
                        "docker", "compose", "exec", "-T", "-u", "postgres",
                        service, "psql", "-d", admin_db, "-c",
                        "SELECT pg_reload_conf();",
                    ],
                    [
                        "docker", "compose", "exec", "-T",
                        service, "psql", "-U", as_user, "-d", admin_db, "-c",
                        "SELECT pg_reload_conf();",
                    ],
                ):
                    rok, _ = await migrator._run_cmd(cmd, cwd=cwd, timeout=20)
                    if rok:
                        reloaded = True
                        break
                if reloaded:
                    break
            if reloaded:
                break
        if not reloaded:
            # pg_ctl reload as postgres OS user
            await migrator._run_cmd(
                [
                    "docker", "compose", "exec", "-T", "-u", "postgres",
                    service, "bash", "-lc",
                    "pg_ctl reload -D \"${PGDATA:-/var/lib/postgresql/data}\" || true",
                ],
                cwd=cwd,
                timeout=30,
            )
        await asyncio.sleep(1)

        any_ok = False
        for role in roles:
            synced = False
            for as_user in users:
                for admin_db in admin_dbs:
                    if await _pg_alter_role_via_trust(
                        migrator,
                        service,
                        as_user=as_user,
                        role=role,
                        password=password,
                        database=admin_db,
                    ):
                        migrator.job.log(
                            f"Live-HBA synced password for role {role} "
                            f"(as {as_user} on {admin_db})"
                        )
                        synced = True
                        any_ok = True
                        break
                if synced:
                    break
            if not synced:
                migrator.job.log(f"Live-HBA could not ALTER ROLE {role}")
        return any_ok
    finally:
        if patched:
            await migrator._run_cmd(
                [
                    "docker", "compose", "exec", "-T", "-u", "root",
                    service, "bash", "-lc", _pg_hba_trust_restore_script(),
                ],
                cwd=cwd,
                timeout=60,
            )
            # Best-effort reload after restore
            await migrator._run_cmd(
                [
                    "docker", "compose", "exec", "-T", "-u", "postgres",
                    service, "bash", "-lc",
                    "psql -d postgres -c 'SELECT pg_reload_conf();' "
                    "|| psql -d pasarguard -c 'SELECT pg_reload_conf();' || true",
                ],
                cwd=cwd,
                timeout=20,
            )
            try:
                await refresh_pgbouncer_if_stale(
                    migrator,
                    "postgresql",
                    env_text=text,
                    password=password,
                    force=True,
                )
            except Exception as exc:
                migrator.job.log(f"PgBouncer refresh after live-HBA note: {exc}")


async def recover_postgres_passwords_via_single_user(
    migrator,
    service: str,
    env_text: str,
    *,
    password: str,
    admin_users: list[str] | None = None,
) -> bool:
    """Last-resort PG password reset — same volume, ``postgres --single`` (no auth).

    Used when in-container trust/peer ALTER cannot run (custom ``pg_hba``, no
    local trust, or SCRAM lockout). Stops the compose DB service, runs a one-shot
    sibling via ``--volumes-from`` the *existing* container (not ``compose run``,
    which can attach a throwaway anonymous volume), sets role passwords to the
    install .env secret, then brings the normal service back. Does not wipe data.
    """
    if not password or not service:
        return False

    text = env_text or ""
    container_env: dict[str, str] = {}
    try:
        container_env = await read_db_container_init_env(migrator, service)
    except Exception:
        container_env = {}
    roles = postgres_role_candidates(
        text,
        container_env.get("POSTGRES_USER"),
        container_env.get("DB_USER"),
        *(admin_users or []),
        include_postgres_fallback=True,
    )
    if not roles:
        roles = ["postgres", "pasarguard"]

    cwd = str(PASARGUARD_DIR)
    heal_name = f"pasarguard-{service}-pwd-heal"
    script = _pg_single_user_alter_script(roles, password)

    migrator.job.log(
        f"PostgreSQL nuclear recovery on {service}: single-user ALTER for "
        f"{len(roles)} role(s) (volumes-from live container, no data wipe)..."
    )

    success = False
    started = False
    try:
        started, mode = await _start_heal_sidecar_volumes_from(
            migrator, service=service, heal_name=heal_name,
        )
        if not started:
            migrator.job.log("PostgreSQL nuclear: heal sidecar failed to start")
            return False
        migrator.job.log(f"PostgreSQL nuclear: sidecar mode={mode}")
        await asyncio.sleep(2)

        ok, out = await migrator._run_cmd(
            ["docker", "exec", heal_name, "bash", "-lc", script],
            cwd=cwd,
            timeout=120,
        )
        if not ok or "pgclockmg-heal: single-user ALTER done" not in (out or ""):
            migrator.job.log(
                f"PostgreSQL nuclear: single-user ALTER failed: {(out or '')[-400:]}"
            )
            return False
        migrator.job.log("PostgreSQL nuclear: role passwords set via single-user mode")
        success = True
        return True
    finally:
        if started:
            await migrator._run_cmd(
                ["docker", "stop", heal_name], cwd=cwd, timeout=60,
            )
        await migrator._run_cmd(
            ["docker", "rm", "-f", heal_name], cwd=cwd, timeout=60,
        )
        up_ok, up_out = await migrator._run_cmd(
            ["docker", "compose", "up", "-d", service], cwd=cwd, timeout=180,
        )
        if not up_ok:
            migrator.job.log(
                f"PostgreSQL nuclear: failed to restart {service}: {(up_out or '')[-300:]}"
            )
        elif success:
            # Wait until TCP-ish local probe accepts the new password.
            probe_dbs = _pg_probe_databases(text, "postgresql")
            users = list(
                _unique_strings(
                    *(admin_users or []),
                    *postgres_admin_users(text),
                    container_env.get("POSTGRES_USER"),
                    "postgres",
                )
            ) or ["postgres"]
            for _ in range(30):
                await asyncio.sleep(2)
                ready = False
                for user in users:
                    if await _probe_pg_any_db(
                        migrator, service, user, password, probe_dbs,
                    ):
                        ready = True
                        break
                if ready:
                    migrator.job.log(
                        f"PostgreSQL nuclear: {service} accepting new password"
                    )
                    break
            try:
                await refresh_pgbouncer_if_stale(
                    migrator,
                    "postgresql",
                    env_text=text,
                    password=password,
                    force=True,
                )
            except Exception as exc:
                migrator.job.log(
                    f"PgBouncer refresh after nuclear recovery note: {exc}"
                )


async def recover_postgres_passwords_via_hba_trust(
    migrator,
    service: str,
    env_text: str,
    *,
    password: str,
    admin_users: list[str] | None = None,
) -> bool:
    """Second nuclear path: temporary ``pg_hba.conf`` trust → ALTER → restore hba.

    Used when ``postgres --single`` cannot run (odd image layout). Uses
    ``--volumes-from`` the live service container so we edit the real datadir,
    not a throwaway ``compose run`` anonymous volume. No data wipe.
    """
    if not password or not service:
        return False

    text = env_text or ""
    container_env: dict[str, str] = {}
    try:
        container_env = await read_db_container_init_env(migrator, service)
    except Exception:
        container_env = {}
    roles = postgres_role_candidates(
        text,
        container_env.get("POSTGRES_USER"),
        container_env.get("DB_USER"),
        *(admin_users or []),
        include_postgres_fallback=True,
    ) or ["postgres", "pasarguard"]
    users = list(
        _unique_strings(
            *(admin_users or []),
            *postgres_admin_users(text),
            container_env.get("POSTGRES_USER"),
            "postgres",
        )
    ) or ["postgres"]
    admin_dbs = _unique_strings(
        target_database_name(text, "postgresql"),
        container_env.get("POSTGRES_DB"),
        "postgres",
        "pasarguard",
    )
    cwd = str(PASARGUARD_DIR)
    heal_name = f"pasarguard-{service}-hba-heal"

    migrator.job.log(
        f"PostgreSQL HBA-trust recovery on {service}: temporary trust → ALTER "
        f"{len(roles)} role(s) → restore pg_hba (volumes-from live container)..."
    )

    hba_patched = False
    try:
        started, mode = await _start_heal_sidecar_volumes_from(
            migrator, service=service, heal_name=heal_name,
        )
        if not started:
            migrator.job.log("PostgreSQL HBA heal: sidecar failed to start")
            return False
        migrator.job.log(f"PostgreSQL HBA heal: sidecar mode={mode}")
        await asyncio.sleep(2)
        ok, out = await migrator._run_cmd(
            ["docker", "exec", heal_name, "bash", "-lc", _pg_hba_trust_prepare_script()],
            cwd=cwd,
            timeout=60,
        )
        if not ok or "pgclockmg-heal: pg_hba trust installed" not in (out or ""):
            migrator.job.log(
                f"PostgreSQL HBA heal: could not patch pg_hba: {(out or '')[-300:]}"
            )
            return False
        hba_patched = True
    finally:
        await migrator._run_cmd(["docker", "stop", heal_name], cwd=cwd, timeout=60)
        await migrator._run_cmd(["docker", "rm", "-f", heal_name], cwd=cwd, timeout=60)

    # Boot normal service under temporary trust, ALTER, then restore hba.
    up_ok, up_out = await migrator._run_cmd(
        ["docker", "compose", "up", "-d", service], cwd=cwd, timeout=180,
    )
    if not up_ok:
        migrator.job.log(
            f"PostgreSQL HBA heal: restart failed: {(up_out or '')[-300:]}"
        )
        return False

    any_ok = False
    try:
        for _ in range(40):
            await asyncio.sleep(2)
            # Under trust, omit password.
            for as_user in users:
                for admin_db in admin_dbs:
                    probe = await _pg_alter_role_via_trust(
                        migrator,
                        service,
                        as_user=as_user,
                        role=roles[0],
                        password=password,
                        database=admin_db,
                    )
                    if probe:
                        break
                else:
                    continue
                break
            else:
                continue
            break

        for role in roles:
            synced = False
            for as_user in users:
                for admin_db in admin_dbs:
                    if await _pg_alter_role_via_trust(
                        migrator,
                        service,
                        as_user=as_user,
                        role=role,
                        password=password,
                        database=admin_db,
                    ):
                        migrator.job.log(
                            f"HBA-trust synced password for role {role} "
                            f"(as {as_user} on {admin_db})"
                        )
                        synced = True
                        any_ok = True
                        break
                if synced:
                    break
            if not synced:
                migrator.job.log(f"HBA-trust could not ALTER ROLE {role}")
    finally:
        if hba_patched:
            # Restore original pg_hba via volumes-from sidecar, then reload.
            await migrator._run_cmd(
                ["docker", "compose", "stop", service], cwd=cwd, timeout=120,
            )
            started2, _ = await _start_heal_sidecar_volumes_from(
                migrator, service=service, heal_name=heal_name,
            )
            if started2:
                await asyncio.sleep(1)
                await migrator._run_cmd(
                    [
                        "docker", "exec", heal_name, "bash", "-lc",
                        _pg_hba_trust_restore_script(),
                    ],
                    cwd=cwd,
                    timeout=60,
                )
            await migrator._run_cmd(["docker", "stop", heal_name], cwd=cwd, timeout=60)
            await migrator._run_cmd(["docker", "rm", "-f", heal_name], cwd=cwd, timeout=60)
            await migrator._run_cmd(
                ["docker", "compose", "up", "-d", service], cwd=cwd, timeout=180,
            )
            await asyncio.sleep(3)
            # Best-effort reload if still trusted briefly
            for as_user in users:
                for admin_db in admin_dbs:
                    await migrator._run_cmd(
                        [
                            "docker", "compose", "exec", "-T",
                            service, "psql", "-U", as_user, "-d", admin_db,
                            "-c", "SELECT pg_reload_conf();",
                        ],
                        cwd=cwd,
                        timeout=20,
                    )

    if any_ok:
        try:
            await refresh_pgbouncer_if_stale(
                migrator,
                "postgresql",
                env_text=text,
                password=password,
                force=True,
            )
        except Exception as exc:
            migrator.job.log(f"PgBouncer refresh after HBA heal note: {exc}")
    return any_ok


async def force_align_postgres_password(
    migrator,
    service: str,
    env_text: str,
    *,
    password: str,
    admin_users: list[str] | None = None,
) -> bool:
    """Align live PG/Timescale roles to ``password``.

    Order: trust ALTER → **live-HBA** (patch running container, same volume) →
    single-user (``--volumes-from``) → stopped HBA-trust sidecar.

    This is the automation that makes sqlite→Timescale (and any convert) keep
    going when .env and the volume SCRAM secret drifted: we never ask the
    operator to hand-edit passwords mid-restore.
    """
    if not password or not service:
        return False
    ok = await recover_postgres_passwords_via_trust(
        migrator,
        service,
        env_text,
        password=password,
        admin_users=admin_users,
    )
    if ok:
        return True
    migrator.job.log(
        "Trust recovery insufficient — escalating to live pg_hba patch "
        "(same running container / volume)..."
    )
    ok = await recover_postgres_passwords_via_live_hba(
        migrator,
        service,
        env_text,
        password=password,
        admin_users=admin_users,
    )
    if ok:
        return True
    migrator.job.log(
        "Live-HBA insufficient — escalating to PostgreSQL single-user "
        "password heal (volumes-from live container)..."
    )
    ok = await recover_postgres_passwords_via_single_user(
        migrator,
        service,
        env_text,
        password=password,
        admin_users=admin_users,
    )
    if ok:
        return True
    migrator.job.log(
        "Single-user heal insufficient — escalating to temporary pg_hba trust "
        "via volumes-from sidecar..."
    )
    return await recover_postgres_passwords_via_hba_trust(
        migrator,
        service,
        env_text,
        password=password,
        admin_users=admin_users,
    )


def mint_install_db_password() -> str:
    """Generate a install password when .env has none (sqlite source path)."""
    import secrets

    return secrets.token_urlsafe(24)


async def _probe_mysql(
    migrator,
    service: str,
    user: str,
    password: str,
    database: str,
) -> bool:
    if not password:
        return False
    # MariaDB images often ship `mariadb` only; MySQL ships `mysql`.
    # Prefer service-aware order (mariadb service → try mariadb client first).
    bins = ("mariadb", "mysql") if "maria" in (service or "").lower() else ("mysql", "mariadb")
    for bin_name in bins:
        # Prefer named DB; fall back to no-DB probe (avoids false auth fail on missing schema)
        for db_arg in (database, ""):
            cmd = [
                "docker", "compose", "exec", "-T",
                "-e", f"MYSQL_PWD={password}",
                service, bin_name, "-u", user, "-N", "-e", "SELECT 1",
            ]
            if db_arg:
                cmd.append(db_arg)
            ok, out = await migrator._run_cmd(cmd, cwd=str(PASARGUARD_DIR), timeout=25)
            if ok and "1" in (out or ""):
                return True
    return False


async def resolve_live_admin_connection(
    migrator,
    db_type: str,
    env_text: str | None = None,
) -> dict:
    """Probe docker DB until admin credentials work; required before cross-DB ops."""
    text = env_text if env_text is not None else read_env_text()
    service = resolve_db_service(db_type)
    if not service:
        raise RuntimeError(f"No compose service for {db_type}")

    db_name = target_database_name(text, db_type)
    migrator.job.log(f"Resolving live admin credentials for {db_type} ({service}/{db_name})...")

    # Container init env often holds the password the volume was created with —
    # include it so restore/migrate can heal even when .env drifted.
    container_env: dict[str, str] = {}
    try:
        container_env = await read_db_container_init_env(migrator, service)
    except Exception as exc:
        migrator.job.log(f"Container init-env probe note: {exc}")

    if db_type in ("postgresql", "timescaledb"):
        # Ensure the DB service is up before any probe/heal.
        await migrator._run_cmd(
            ["docker", "compose", "up", "-d", service],
            cwd=str(PASARGUARD_DIR),
            timeout=180,
        )
        await asyncio.sleep(1)

        users = _unique_strings(
            *postgres_admin_users(text),
            container_env.get("POSTGRES_USER"),
            container_env.get("DB_USER"),
        ) or ["postgres"]
        passwords = _unique_strings(
            *postgres_password_candidates(text),
            container_env.get("POSTGRES_PASSWORD"),
            container_env.get("DB_PASSWORD"),
        )
        probe_dbs = _pg_probe_databases(text, db_type)
        if container_env.get("POSTGRES_DB"):
            probe_dbs = _unique_strings(container_env.get("POSTGRES_DB"), *probe_dbs)

        # Canonical install password (sqlite backups never supply one).
        preferred = passwords[0] if passwords else ""
        minted = False
        if not preferred:
            preferred = mint_install_db_password()
            minted = True
            migrator.job.log(
                "No install/container password candidates — minted POSTGRES_PASSWORD "
                "and will force-align live roles to it"
            )
            text = _persist_install_pg_password(preferred, text)
            passwords = _unique_strings(preferred, *passwords)

        # --- Fast path: local socket is NOT trust → password already proven ---
        trust_mode: bool | None = None
        for user in users:
            for pwd in passwords:
                hit_db = await _probe_pg_any_db(
                    migrator, service, user, pwd, probe_dbs,
                )
                if not hit_db:
                    continue
                trust_mode = await _pg_in_container_is_trust(
                    migrator, service, user, hit_db,
                )
                if trust_mode is False:
                    host, port = await _pick_pg_migration_endpoint(migrator, service)
                    migrator.job.log(
                        f"PostgreSQL auth OK as {user} on {hit_db} "
                        f"(local socket requires password; endpoint {host}:{port})"
                    )
                    return {
                        "db_type": db_type,
                        "user": user,
                        "password": pwd,
                        "database": db_name,
                        "host": host,
                        "port": port,
                    }
                # Local is trust — stop scanning; SCRAM/heal path below is authoritative.
                break
            if trust_mode is True:
                break

        # --- Trust / SCRAM-unknown path: prove password via eth0 SCRAM inside ---
        # Never hard-fail on "no published 5432" — that was the v4.6.13 production bug.
        migrator.job.log(
            "PostgreSQL local socket is trust (or unproven) — verifying install "
            "password via in-container eth0 SCRAM (no host publish required)..."
        )

        async def _scram_hit(pwd: str) -> tuple[str, str] | None:
            for user in users:
                hit = await _probe_pg_scram_any_db(
                    migrator, service, user, pwd, probe_dbs,
                )
                if hit:
                    return user, hit
            return None

        # 1) Already correct?
        hit = await _scram_hit(preferred)
        if hit:
            user, hit_db = hit
            host, port = await _pick_pg_migration_endpoint(migrator, service)
            migrator.job.log(
                f"PostgreSQL SCRAM OK as {user} via eth0 (db={hit_db}); "
                f"migration endpoint {host}:{port}"
            )
            return {
                "db_type": db_type,
                "user": user,
                "password": preferred,
                "database": db_name,
                "host": host,
                "port": port,
            }

        # 2) Another candidate already works on SCRAM — prefer install secret
        #    but accept working one and heal roles to preferred next.
        working_pwd = ""
        working_user = users[0]
        for pwd in passwords:
            if pwd == preferred:
                continue
            hit = await _scram_hit(pwd)
            if hit:
                working_user, _ = hit
                working_pwd = pwd
                break

        # 3) Always force-align to install/minted preferred (root heal).
        migrator.job.log(
            "PostgreSQL SCRAM not yet aligned to install password — force-aligning "
            f"roles (trust → live-HBA → single-user volumes-from → HBA"
            + (", minted=yes" if minted else "")
            + (", had-working-alt=yes" if working_pwd else "")
            + ")..."
        )
        recovered = await force_align_postgres_password(
            migrator,
            service,
            text,
            password=preferred,
            admin_users=users,
        )
        # Persist preferred into .env even when it came from container init.
        if preferred and not minted:
            text = _persist_install_pg_password(preferred, text)

        if recovered:
            for attempt in range(1, 6):
                await asyncio.sleep(1.2 * attempt)
                hit = await _scram_hit(preferred)
                if hit:
                    user, hit_db = hit
                    host, port = await _pick_pg_migration_endpoint(migrator, service)
                    migrator.job.log(
                        f"PostgreSQL SCRAM OK as {user} via eth0 after heal "
                        f"(db={hit_db}, attempt={attempt}); endpoint {host}:{port}"
                    )
                    return {
                        "db_type": db_type,
                        "user": user,
                        "password": preferred,
                        "database": db_name,
                        "host": host,
                        "port": port,
                    }

            # Eth0 might itself be trust (wide-open hba) — then SCRAM probe is
            # meaningless; accept preferred after successful ALTER + optional host TCP.
            eth0_trust = False
            for user in users:
                for db in probe_dbs:
                    if await _pg_eth0_is_trust(migrator, service, user, db):
                        eth0_trust = True
                        break
                if eth0_trust:
                    break
            if eth0_trust:
                host, port = await _pick_pg_migration_endpoint(migrator, service)
                migrator.job.log(
                    "PostgreSQL eth0 is also trust — accepting install password after "
                    f"successful role heal; endpoint {host}:{port}"
                )
                return {
                    "db_type": db_type,
                    "user": users[0],
                    "password": preferred,
                    "database": db_name,
                    "host": host,
                    "port": port,
                }

            # Secondary: host-side TCP (published / bridge / pgbouncer).
            endpoints = await _resolve_pg_tcp_endpoints(migrator, service)
            for image, host, port in endpoints:
                for user in users:
                    for tcp_db in probe_dbs:
                        if await _probe_pg_via_host_tcp(
                            migrator,
                            image=image,
                            host=host,
                            port=port,
                            user=user,
                            password=preferred,
                            database=tcp_db,
                        ):
                            migrator.job.log(
                                f"PostgreSQL auth OK as {user} via host TCP "
                                f"{host}:{port} after heal (db={tcp_db})"
                            )
                            return {
                                "db_type": db_type,
                                "user": user,
                                "password": preferred,
                                "database": db_name,
                                "host": host,
                                "port": port,
                            }

        # 4) Fall back to a password that already SCRAM-worked (rare).
        if working_pwd:
            host, port = await _pick_pg_migration_endpoint(migrator, service)
            migrator.job.log(
                f"PostgreSQL using alternate SCRAM-verified password as {working_user} "
                f"(heal to install secret failed or unverified); endpoint {host}:{port}"
            )
            return {
                "db_type": db_type,
                "user": working_user,
                "password": working_pwd,
                "database": db_name,
                "host": host,
                "port": port,
            }

        raise RuntimeError(
            "PostgreSQL/TimescaleDB authentication failed — could not prove the "
            "install password over in-container SCRAM (eth0) after auto-heal "
            f"(recovered={bool(recovered)}, minted={minted}). "
            "Update PGClockMG to v4.6.14+ and retry; if it persists, check that the "
            "timescaledb container is running and roles exist."
        )

    if db_type in ("mysql", "mariadb"):
        users = _unique_strings(
            *mysql_admin_users(text),
            container_env.get("MYSQL_USER"),
            container_env.get("MARIADB_USER"),
        )
        passwords = _unique_strings(
            *mysql_password_candidates(text),
            container_env.get("MYSQL_ROOT_PASSWORD"),
            container_env.get("MARIADB_ROOT_PASSWORD"),
            container_env.get("MYSQL_PASSWORD"),
            container_env.get("MARIADB_PASSWORD"),
            container_env.get("DB_PASSWORD"),
        )
        for user in users:
            for pwd in passwords:
                if await _probe_mysql(migrator, service, user, pwd, db_name):
                    conn = {
                        "db_type": db_type,
                        "user": user,
                        "password": pwd,
                        "database": db_name,
                        "host": "127.0.0.1",
                        "port": "3306",
                    }
                    migrator.job.log(f"MySQL/MariaDB auth OK as {user}")
                    return conn

        # Mirror PG trust recovery: when every .env candidate fails, force the
        # preferred password via temporary skip-grant on the same volume.
        preferred = passwords[0] if passwords else ""
        app_user = (
            read_env_var(text, "DB_USER")
            or read_env_var(text, "MYSQL_USER")
            or container_env.get("MYSQL_USER")
            or container_env.get("MARIADB_USER")
            or "pasarguard"
        )
        recovered = False
        if preferred:
            async def _run_list(cmd, cwd=None, timeout=600):
                return await migrator._run_cmd(cmd, cwd=cwd, timeout=timeout)

            recovered = await recover_mysql_passwords_via_skip_grants(
                _run_list,
                service=service,
                password=preferred,
                app_user=app_user,
                db_type=db_type,
                db_name=db_name,
                compose_cwd=str(PASARGUARD_DIR),
                log=migrator.job.log,
            )
        if recovered:
            migrator.job.log(
                "MySQL skip-grant recovery applied — re-checking root auth..."
            )
            await asyncio.sleep(2)
            for user in users:
                if await _probe_mysql(migrator, service, user, preferred, db_name):
                    conn = {
                        "db_type": db_type,
                        "user": user,
                        "password": preferred,
                        "database": db_name,
                        "host": "127.0.0.1",
                        "port": "3306",
                    }
                    migrator.job.log(
                        f"MySQL/MariaDB auth OK as {user} (after skip-grant recovery)"
                    )
                    return conn
        raise RuntimeError(
            "MySQL/MariaDB authentication failed — check MYSQL_ROOT_PASSWORD / DB_PASSWORD in .env"
            + (
                " (skip-grant recovery could not realign passwords)."
                if preferred and not recovered
                else " (skip-grant recovery ran but auth still failed)."
                if preferred and recovered
                else ""
            )
        )

    raise RuntimeError(f"Unsupported database for credential probe: {db_type}")


async def ensure_target_auth_ready(
    migrator,
    db_type: str,
    env_text: str | None = None,
    *,
    password: str | None = None,
    sync_roles: bool = True,
    refresh_pgbouncer: bool = True,
) -> dict:
    """Resolve admin auth, align roles, and refresh PgBouncer — shared by restore+migrate.

    Does not change convert/engine selection rules; only makes credential heal
    consistent across sqlite→server convert, same-engine restore, and Marzban/x-ui.
    """
    text = env_text if env_text is not None else read_env_text()
    admin = await resolve_live_admin_connection(migrator, db_type, env_text=text)
    canonical = (
        password
        or admin.get("password")
        or ""
    )
    if not sync_roles or not canonical:
        return admin

    if db_type in ("postgresql", "timescaledb"):
        await sync_postgres_roles_to_app_password(
            migrator,
            db_type,
            admin,
            env_text=text,
            password=canonical,
        )
        if refresh_pgbouncer:
            await refresh_pgbouncer_if_stale(
                migrator,
                db_type,
                env_text=text,
                password=canonical,
                force=True,
            )
    elif db_type in ("mysql", "mariadb"):
        await sync_mysql_roles_to_password(
            migrator,
            db_type,
            admin,
            password=canonical,
            env_text=text,
            db_name=admin.get("database") or target_database_name(text, db_type),
        )
    return admin


def migration_params_from_connection(
    source_db: str,
    target_db: str,
    target_conn: dict,
    source_conn: dict | None = None,
) -> dict:
    """Build wizard-style params dict with a verified target connection."""
    src = source_conn or {}
    tgt = target_conn
    params = {
        "source_db": source_db,
        "target_db": target_db,
        "_resolved_target_conn": dict(tgt),
        "source_db_user": src.get("user"),
        "source_db_password": src.get("password"),
        "source_db_name": src.get("database"),
        "source_db_host": src.get("host") or "127.0.0.1",
        "source_db_port": src.get("port"),
        "target_db_user": tgt.get("user"),
        "target_db_password": tgt.get("password"),
        "target_db_name": tgt.get("database"),
        "target_db_host": tgt.get("host") or "127.0.0.1",
        "target_db_port": migration_port(tgt, target_db),
    }
    return params


def _mysql_client_bins(db_type: str, service: str | None = None) -> list[str]:
    from app.services.pasarguard_ops import mysql_client_bins

    return mysql_client_bins(db_type, service)


def _mysql_sql_literal(password: str) -> str:
    return (password or "").replace("\\", "\\\\").replace("'", "\\'")


def _mysql_ident(name: str) -> str:
    """Quote a MySQL identifier (database / user) safely for generated SQL."""
    return "`" + (name or "").replace("`", "``") + "`"


def build_mysql_role_password_sql(
    password: str,
    app_user: str | None = None,
    db_name: str | None = None,
    *,
    include_flush_first: bool = False,
) -> str:
    """SQL to align root + app user passwords for %, localhost, and 127.0.0.1.

    Panel / alembic traffic often uses TCP to 127.0.0.1 (distinct from localhost
    socket). CREATE USER IF NOT EXISTS is safe when a host row is missing, but a
    newly created ``root@127.0.0.1`` is *not* a superuser — we must GRANT *.*
    WITH GRANT OPTION or alembic fails with error 1044 (Access denied to database).
    """
    lit = _mysql_sql_literal(password)
    hosts = ("%", "localhost", "127.0.0.1")
    statements: list[str] = []
    if include_flush_first:
        # Required before ALTER USER while mysqld runs with --skip-grant-tables.
        statements.append("FLUSH PRIVILEGES;")
    for host in hosts:
        statements.append(f"CREATE USER IF NOT EXISTS 'root'@'{host}' IDENTIFIED BY '{lit}';")
        statements.append(f"ALTER USER 'root'@'{host}' IDENTIFIED BY '{lit}';")
        # Critical: new root@host rows start with zero privileges.
        statements.append(
            f"GRANT ALL PRIVILEGES ON *.* TO 'root'@'{host}' WITH GRANT OPTION;"
        )
    user = (app_user or "").strip()
    if user and user != "root":
        for host in hosts:
            statements.append(
                f"CREATE USER IF NOT EXISTS '{user}'@'{host}' IDENTIFIED BY '{lit}';"
            )
            statements.append(f"ALTER USER '{user}'@'{host}' IDENTIFIED BY '{lit}';")
        db = (db_name or "").strip() or "pasarguard"
        db_q = _mysql_ident(db)
        for host in hosts:
            statements.append(
                f"GRANT ALL PRIVILEGES ON {db_q}.* TO '{user}'@'{host}';"
            )
    statements.append("FLUSH PRIVILEGES;")
    return " ".join(statements)


def mysql_sync_auth_candidates(
    *extra: str | None,
    env_text: str | None = None,
) -> list[str]:
    """Passwords to try when authenticating as root for a role sync."""
    text = env_text if env_text is not None else read_env_text()
    return _unique_strings(*extra, *mysql_password_candidates(text))


async def recover_mysql_passwords_via_skip_grants(
    run_cmd,
    *,
    service: str,
    password: str,
    app_user: str,
    db_type: str,
    db_name: str = "pasarguard",
    compose_cwd: str | None = None,
    log=None,
) -> bool:
    """Last-resort password reset when root auth is unknown.

    Stops the compose DB service, starts a temporary sibling container on the
    *same data volume* with ``--skip-grant-tables --skip-networking``, sets
    passwords, then brings the normal service back. Does not delete volumes.
    """
    import asyncio

    if not password or not service:
        return False

    cwd = compose_cwd or str(PASARGUARD_DIR)
    heal_name = f"pasarguard-{service}-pwd-heal"
    bins = _mysql_client_bins(db_type, service)
    sql = build_mysql_role_password_sql(
        password, app_user=app_user, db_name=db_name, include_flush_first=True,
    )

    def _log(msg: str) -> None:
        if log:
            log(msg)

    async def _run(cmd: list[str], timeout: int = 120) -> tuple[bool, str]:
        return await run_cmd(cmd, cwd=cwd, timeout=timeout)

    _log(
        f"MySQL root locked out on {service} — temporary skip-grant recovery "
        "(same volume, no data wipe)..."
    )

    # Drop leftover heal container from a previous interrupted run.
    await _run(["docker", "rm", "-f", heal_name], timeout=60)
    # Stop the normal service so the volume is free for the heal container.
    stop_ok, stop_out = await _run(
        ["docker", "compose", "stop", service], timeout=120
    )
    if not stop_ok:
        _log(f"MySQL recover: could not stop {service}: {(stop_out or '')[-200:]}")
        # Still try — volume may already be idle.
    started = False
    success = False
    try:
        # Keep docker-entrypoint.sh (do not override entrypoint) so existing
        # datadir startup stays identical to a normal boot; only add mysqld flags.
        run_ok, run_out = await _run(
            [
                "docker", "compose", "run", "-d", "--no-deps",
                "--name", heal_name,
                service,
                "--skip-grant-tables", "--skip-networking",
            ],
            timeout=180,
        )
        if not run_ok:
            _log(f"MySQL recover: compose run failed: {(run_out or '')[-300:]}")
            return False
        started = True

        ready = False
        ready_bin = bins[0]
        for _ in range(40):
            await asyncio.sleep(2)
            for bin_name in bins:
                ok, out = await _run(
                    [
                        "docker", "exec", heal_name, bin_name,
                        "-u", "root", "-N", "-e", "SELECT 1",
                    ],
                    timeout=20,
                )
                if ok and "1" in (out or ""):
                    ready = True
                    ready_bin = bin_name
                    break
            if ready:
                break
        if not ready:
            _log("MySQL recover: heal container never accepted root connections")
            return False

        ok, out = await _run(
            [
                "docker", "exec", heal_name, ready_bin,
                "-u", "root", "-e", sql,
            ],
            timeout=60,
        )
        if not ok:
            _log(f"MySQL recover: ALTER/CREATE failed: {(out or '')[-300:]}")
            return False
        _log(f"MySQL recover: passwords set via skip-grant ({ready_bin})")
        success = True
        return True
    finally:
        if started:
            await _run(["docker", "stop", heal_name], timeout=60)
        await _run(["docker", "rm", "-f", heal_name], timeout=60)
        # Always restore the normal DB service — even if ALTER failed.
        up_ok, up_out = await _run(
            ["docker", "compose", "up", "-d", service], timeout=180
        )
        if not up_ok:
            _log(f"MySQL recover: failed to restart {service}: {(up_out or '')[-300:]}")
        elif success:
            # Wait until normal mysqld accepts the new password before callers proceed.
            for _ in range(30):
                await asyncio.sleep(2)
                verified = False
                for bin_name in bins:
                    ok, out = await _run(
                        [
                            "docker", "compose", "exec", "-T",
                            "-e", f"MYSQL_PWD={password}",
                            service, bin_name,
                            "-u", "root", "-N", "-e", "SELECT 1",
                        ],
                        timeout=20,
                    )
                    if ok and "1" in (out or ""):
                        verified = True
                        break
                if verified:
                    _log(f"MySQL recover: {service} accepting new root password")
                    break


async def sync_mysql_roles_to_password(
    migrator,
    db_type: str,
    admin_conn: dict,
    *,
    app_user: str | None = None,
    password: str | None = None,
    env_text: str | None = None,
    db_name: str | None = None,
    allow_skip_grant_recovery: bool = True,
) -> bool:
    """Align MySQL/MariaDB root + app-user passwords so the panel URL can connect.

    Cross-DB copy authenticates as root; the panel uses DB_USER (often ``pasarguard``).
    Without this sync, ``Access denied for user 'pasarguard'@...`` is common after
    sqlite→mysql convert (same heal used by PasarGuard restore).

    Tries every known password candidate first. Only if root is fully unreachable
    does it fall back to temporary ``--skip-grant-tables`` recovery (same volume).
    """
    text = env_text if env_text is not None else read_env_text()
    service = resolve_db_service(db_type) or ("mariadb" if db_type == "mariadb" else "mysql")
    admin_pwd = (admin_conn or {}).get("password") or ""
    new_pwd = (
        password
        or read_env_var(text, "MYSQL_ROOT_PASSWORD")
        or read_env_var(text, "DB_PASSWORD")
        or read_env_var(text, "MYSQL_PASSWORD")
        or admin_pwd
    )
    if not new_pwd or not service:
        return False

    user = (
        app_user
        or read_env_var(text, "DB_USER")
        or read_env_var(text, "MYSQL_USER")
        or "pasarguard"
    )
    schema = (
        db_name
        or (admin_conn or {}).get("database")
        or target_database_name(text, db_type)
        or "pasarguard"
    )
    sql = build_mysql_role_password_sql(new_pwd, app_user=user, db_name=schema)
    cwd = str(PASARGUARD_DIR)
    auth_pwds = mysql_sync_auth_candidates(admin_pwd, new_pwd, env_text=text)
    migrator.job.log(
        f"Syncing MySQL passwords on {service} (app user={user}, "
        f"auth candidates={len(auth_pwds)})..."
    )
    last_out = ""
    for bin_name in _mysql_client_bins(db_type, service):
        for auth_pwd in auth_pwds:
            cmd = [
                "docker", "compose", "exec", "-T",
                "-e", f"MYSQL_PWD={auth_pwd}",
                service, bin_name, "-u", "root", "-e", sql,
            ]
            ok, out = await migrator._run_cmd(cmd, cwd=cwd, timeout=60)
            if ok:
                migrator.job.log(f"Synced MySQL passwords on {service} ({bin_name})")
                return True
            last_out = out or last_out
        cmd2 = [
            "docker", "compose", "exec", "-T",
            service, bin_name, "-u", "root", "-e", sql,
        ]
        ok2, out2 = await migrator._run_cmd(cmd2, cwd=cwd, timeout=60)
        if ok2:
            migrator.job.log(f"Synced MySQL passwords on {service} ({bin_name}, no-password)")
            return True
        last_out = out2 or last_out

    migrator.job.log(f"MySQL password sync note: {(last_out or '')[-300:]}")
    if not allow_skip_grant_recovery:
        return False

    async def _run_list(cmd, cwd=None, timeout=600):
        return await migrator._run_cmd(cmd, cwd=cwd, timeout=timeout)

    recovered = await recover_mysql_passwords_via_skip_grants(
        _run_list,
        service=service,
        password=new_pwd,
        app_user=user,
        db_type=db_type,
        db_name=schema,
        compose_cwd=cwd,
        log=migrator.job.log,
    )
    return bool(recovered)


def parse_container_env(text: str) -> dict[str, str]:
    """Parse ``docker inspect`` environment output; later values win."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key:
            out[key] = value
    return out


def pgbouncer_env_mismatch(
    container_env: dict[str, str],
    *,
    user: str,
    password: str,
    database: str,
) -> list[str]:
    """Keys where running PgBouncer disagrees with finalized panel credentials."""
    expected = {"DB_USER": user, "DB_PASSWORD": password, "DB_NAME": database}
    stale: list[str] = []
    for key, want in expected.items():
        if not want:
            continue
        have = container_env.get(key)
        if have is None or have == want:
            continue
        stale.append(key)
    return stale


async def refresh_pgbouncer_if_stale(
    migrator,
    db_type: str,
    *,
    env_text: str | None = None,
    user: str | None = None,
    password: str | None = None,
    database: str | None = None,
    force: bool = False,
) -> bool:
    """Recreate PgBouncer when its baked-in env disagrees with finalized .env.

    Pass ``force=True`` after trust password recovery — env strings can already
    match while SCRAM secrets / auth caches still reject the panel.
    """
    import asyncio

    from app.services.env_migration import parse_sqlalchemy_url, read_env_var
    from app.services.multiworker_stack import compose_has_service
    from app.services.pasarguard_ops import compose_file_prefix

    if db_type not in ("postgresql", "timescaledb"):
        return False
    if not compose_has_service("pgbouncer"):
        return False

    text = env_text if env_text is not None else read_env_text()
    url = read_env_var(text, "SQLALCHEMY_DATABASE_URL") or ""
    parsed = parse_sqlalchemy_url(url, text) if url else {}
    user = (
        user
        or parsed.get("user")
        or read_env_var(text, "DB_USER")
        or read_env_var(text, "POSTGRES_USER")
        or "pasarguard"
    )
    password = (
        password
        or read_env_var(text, "POSTGRES_PASSWORD")
        or read_env_var(text, "DB_PASSWORD")
        or parsed.get("password")
        or ""
    )
    database = (
        database
        or read_env_var(text, "DB_NAME")
        or parsed.get("database")
        or "pasarguard"
    )
    if not password:
        migrator.job.log("PgBouncer refresh skipped — no password in finalized .env")
        return False

    cwd = str(PASARGUARD_DIR)
    prefix = compose_file_prefix()
    ok, out = await migrator._run_cmd(
        ["docker", "compose", *prefix, "ps", "-q", "pgbouncer"],
        cwd=cwd,
        timeout=30,
        quiet=True,
    )
    cid = (out or "").strip().splitlines()
    if not ok or not cid or not cid[-1].strip():
        migrator.job.log("PgBouncer not running yet — will start with panel stack")
        return False

    container = cid[-1].strip()
    ok2, env_out = await migrator._run_cmd(
        [
            "docker", "inspect", "--format",
            "{{range .Config.Env}}{{println .}}{{end}}",
            container,
        ],
        timeout=30,
        quiet=True,
    )
    container_env = parse_container_env(env_out) if ok2 else {}
    stale = pgbouncer_env_mismatch(
        container_env, user=user, password=password, database=database,
    )
    if not stale and not force:
        migrator.job.log("PgBouncer credentials already match finalized .env")
        return False

    if force and not stale:
        migrator.job.log(
            "Forcing PgBouncer recreate after password recovery "
            "(env matches but auth cache may be stale)..."
        )
    else:
        migrator.job.log(
            f"PgBouncer holds stale credentials ({', '.join(stale)}) — "
            "recreating so panel auth on :6432 succeeds..."
        )
    ok_cfg, cfg_out = await migrator._run_cmd(
        ["docker", "compose", *prefix, "config", "-q"],
        cwd=cwd,
        timeout=60,
        quiet=True,
    )
    if not ok_cfg:
        raise RuntimeError(
            "docker compose config invalid — cannot recreate PgBouncer:\n"
            f"{(cfg_out or '')[-400:]}"
        )

    ok3, out3 = await migrator._run_cmd(
        [
            "docker", "compose", *prefix,
            "up", "-d", "--no-deps", "--force-recreate", "pgbouncer",
        ],
        cwd=cwd,
        timeout=180,
    )
    if not ok3:
        raise RuntimeError(f"PgBouncer recreate failed:\n{(out3 or '')[-800:]}")
    await asyncio.sleep(4)
    migrator.job.log("PgBouncer recreated with finalized credentials")
    return True


async def sync_postgres_roles_to_app_password(
    migrator,
    db_type: str,
    admin_conn: dict,
    env_text: str | None = None,
    *,
    password: str | None = None,
) -> bool:
    """Align app + superuser SCRAM secrets and refresh PgBouncer auth cache.

    Password priority must match ``finalize_pasarguard_env_after_restore`` /
    x-ui convert (``POSTGRES_PASSWORD`` then ``DB_PASSWORD``). If sync used
    ``DB_PASSWORD`` while finalize wrote ``POSTGRES_PASSWORD`` into the panel
    URL, PostgreSQL auth fails after sqlite→PG migration when those differ.
    """
    import asyncio

    from app.services.env_migration import parse_sqlalchemy_url

    text = env_text if env_text is not None else read_env_text()
    url_pwd = parse_sqlalchemy_url(
        read_env_var(text, "SQLALCHEMY_DATABASE_URL") or "", text,
    ).get("password")
    # Prefer explicit password from caller (convert/finalize sync_pwd), then
    # POSTGRES_PASSWORD before DB_PASSWORD — same order as x-ui sync_pwd.
    app_pwd = (
        password
        or read_env_var(text, "POSTGRES_PASSWORD")
        or read_env_var(text, "DB_PASSWORD")
        or read_compose_db_credentials(text).get("password")
        or url_pwd
        or (admin_conn or {}).get("password")
        or ""
    )
    if not app_pwd:
        return False

    service = resolve_db_service(db_type) or "timescaledb"
    db_name = target_database_name(text, db_type)
    admin_user = (admin_conn or {}).get("user") or "postgres"
    admin_pwd = (admin_conn or {}).get("password") or app_pwd

    def _lit(v: str) -> str:
        return "'" + (v or "").replace("'", "''") + "'"

    url_user = parse_sqlalchemy_url(
        read_env_var(text, "SQLALCHEMY_DATABASE_URL") or "", text,
    ).get("user")
    roles = _unique_strings(
        read_env_var(text, "POSTGRES_USER") or "postgres",
        read_env_var(text, "DB_USER"),
        url_user,
        db_name,
    )
    migrator.job.log(f"Syncing PostgreSQL role passwords ({len(roles)} roles)...")
    lit = _lit(app_pwd)
    cwd = str(PASARGUARD_DIR)
    from app.services.pasarguard_ops import compose_file_prefix

    prefix = compose_file_prefix()
    any_ok = False
    failed_roles: list[str] = []
    for role in roles:
        sql = f'ALTER ROLE "{role}" WITH PASSWORD {lit};'
        cmd = [
            "docker", "compose", *prefix, "exec", "-T",
            "-e", f"PGPASSWORD={admin_pwd}",
            service, "psql", "-U", admin_user, "-d", "postgres",
            "-v", "ON_ERROR_STOP=0", "-c", sql,
        ]
        ok, out = await migrator._run_cmd(cmd, cwd=cwd, timeout=30)
        if ok:
            any_ok = True
            continue
        # Password auth failed — try local socket trust (omit PGPASSWORD).
        if await _pg_alter_role_via_trust(
            migrator,
            service,
            as_user=admin_user,
            role=role,
            password=app_pwd,
            database="postgres",
        ):
            migrator.job.log(f"Synced PostgreSQL role {role} via local trust")
            any_ok = True
            continue
        migrator.job.log(
            f"PostgreSQL ALTER ROLE {role} note: {(out or '')[-200:]}"
        )
        failed_roles.append(role)

    if failed_roles or not any_ok:
        recovered = await force_align_postgres_password(
            migrator,
            service,
            text,
            password=app_pwd,
            admin_users=_unique_strings(admin_user, *postgres_admin_users(text)),
        )
        if recovered:
            any_ok = True
            # force_align already force-refreshes pgbouncer
            return True

    await refresh_pgbouncer_if_stale(
        migrator,
        db_type,
        env_text=text,
        password=app_pwd,
        force=bool(any_ok),
    )
    return any_ok
