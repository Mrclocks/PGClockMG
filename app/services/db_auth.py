"""Live database credential resolution — probe .env candidates until auth succeeds."""

from __future__ import annotations

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


def postgres_admin_users(env_text: str | None) -> list[str]:
    text = env_text or ""
    users = _unique_strings(
        read_env_var(text, "DB_USER"),
        read_env_var(text, "POSTGRES_USER"),
    )
    return users or ["postgres"]


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


async def _probe_pg(
    migrator,
    service: str,
    user: str,
    password: str,
    database: str,
) -> bool:
    if not password:
        return False
    cwd = str(PASARGUARD_DIR)
    cmd = (
        f'cd "{cwd}" && docker compose exec -T {service} '
        f'env PGPASSWORD="{password.replace(chr(34), "")}" '
        f'psql -U {user} -d {database} -tAc "SELECT 1" 2>/dev/null'
    )
    ok, out = await migrator._run_cmd(cmd, timeout=25)
    return ok and "1" in (out or "")


async def _probe_mysql(
    migrator,
    service: str,
    user: str,
    password: str,
    database: str,
) -> bool:
    if not password:
        return False
    cwd = str(PASARGUARD_DIR)
    pwd = password.replace('"', '\\"')
    # MariaDB images often ship `mariadb` only; MySQL ships `mysql`.
    # Prefer service-aware order (mariadb service → try mariadb client first).
    bins = ("mariadb", "mysql") if "maria" in (service or "").lower() else ("mysql", "mariadb")
    for bin_name in bins:
        # Prefer named DB; fall back to no-DB probe (avoids false auth fail on missing schema)
        for db_arg in (database, ""):
            db_part = f" {db_arg}" if db_arg else ""
            cmd = (
                f'cd "{cwd}" && docker compose exec -T {service} '
                f'{bin_name} -u {user} -p"{pwd}" -N -e "SELECT 1"{db_part} 2>/dev/null'
            )
            ok, out = await migrator._run_cmd(cmd, timeout=25)
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

    if db_type in ("postgresql", "timescaledb"):
        users = postgres_admin_users(text)
        passwords = postgres_password_candidates(text)
        probe_db = "postgres"
        for user in users:
            for pwd in passwords:
                if await _probe_pg(migrator, service, user, pwd, probe_db):
                    conn = {
                        "db_type": db_type,
                        "user": user,
                        "password": pwd,
                        "database": db_name,
                        "host": "127.0.0.1",
                        "port": "5432",
                    }
                    migrator.job.log(f"PostgreSQL auth OK as {user} (direct port 5432)")
                    return conn
        raise RuntimeError(
            "PostgreSQL/TimescaleDB authentication failed — "
            "POSTGRES_PASSWORD and DB_PASSWORD in /opt/pasarguard/.env do not match the running container"
        )

    if db_type in ("mysql", "mariadb"):
        users = mysql_admin_users(text)
        passwords = mysql_password_candidates(text)
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
        raise RuntimeError(
            "MySQL/MariaDB authentication failed — check MYSQL_ROOT_PASSWORD / DB_PASSWORD in .env"
        )

    raise RuntimeError(f"Unsupported database for credential probe: {db_type}")


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
    # Use single-quoted -e payload so SQL single-quotes stay intact; escape ' as '\''
    sql_q = sql.replace("'", "'\\''")
    for bin_name in _mysql_client_bins(db_type, service):
        for auth_pwd in auth_pwds:
            pwd_q = auth_pwd.replace('"', '\\"')
            cmd = (
                f'cd "{cwd}" && docker compose exec -T {service} '
                f"{bin_name} -u root -p\"{pwd_q}\" -e '{sql_q}'"
            )
            ok, out = await migrator._run_cmd(cmd, timeout=60)
            if ok:
                migrator.job.log(f"Synced MySQL passwords on {service} ({bin_name})")
                return True
            last_out = out or last_out
        cmd2 = (
            f'cd "{cwd}" && docker compose exec -T {service} '
            f"{bin_name} -u root -e '{sql_q}'"
        )
        ok2, out2 = await migrator._run_cmd(cmd2, timeout=60)
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
    any_ok = False
    for role in roles:
        sql = f'ALTER ROLE "{role}" WITH PASSWORD {lit};'
        cmd = (
            f'cd "{cwd}" && docker compose exec -T {service} '
            f'env PGPASSWORD="{admin_pwd.replace(chr(34), "")}" '
            f'psql -U {admin_user} -d postgres -v ON_ERROR_STOP=0 -c "{sql}"'
        )
        ok, out = await migrator._run_cmd(cmd, timeout=30)
        if ok:
            any_ok = True
        else:
            migrator.job.log(
                f"PostgreSQL ALTER ROLE {role} note: {(out or '')[-200:]}"
            )

    compose_path = PASARGUARD_DIR / "docker-compose.yml"
    if compose_path.is_file() and "pgbouncer" in compose_path.read_text(
        encoding="utf-8", errors="ignore",
    ):
        migrator.job.log("Restarting pgbouncer after role password sync...")
        await migrator._run_cmd(
            ["docker", "compose", "restart", "pgbouncer"],
            cwd=cwd,
            timeout=90,
        )
        await asyncio.sleep(4)
    return any_ok
