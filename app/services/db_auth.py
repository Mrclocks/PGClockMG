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
    return _unique_strings(
        read_env_var(text, "POSTGRES_PASSWORD"),
        compose.get("password"),
        read_env_var(text, "DB_PASSWORD"),
    )


def mysql_password_candidates(env_text: str | None) -> list[str]:
    text = env_text or ""
    compose = read_compose_db_credentials(text)
    return _unique_strings(
        read_env_var(text, "MYSQL_ROOT_PASSWORD"),
        read_env_var(text, "MYSQL_PASSWORD"),
        compose.get("password"),
        read_env_var(text, "DB_PASSWORD"),
    )


def postgres_admin_users(env_text: str | None) -> list[str]:
    text = env_text or ""
    return _unique_strings(
        read_env_var(text, "POSTGRES_USER"),
        "postgres",
        read_env_var(text, "DB_USER"),
    )


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
    cmd = (
        f'cd "{cwd}" && docker compose exec -T {service} '
        f'mysql -u {user} -p"{pwd}" -N -e "SELECT 1" {database} 2>/dev/null'
    )
    ok, out = await migrator._run_cmd(cmd, timeout=25)
    return ok and "1" in (out or "")


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


async def sync_postgres_roles_to_app_password(
    migrator,
    db_type: str,
    admin_conn: dict,
    env_text: str | None = None,
) -> None:
    """Align app + superuser SCRAM secrets and refresh PgBouncer auth cache."""
    import asyncio

    text = env_text if env_text is not None else read_env_text()
    app_pwd = (
        read_env_var(text, "DB_PASSWORD")
        or read_compose_db_credentials(text).get("password")
        or admin_conn.get("password")
        or ""
    )
    if not app_pwd:
        return

    service = resolve_db_service(db_type) or "timescaledb"
    db_name = target_database_name(text, db_type)
    admin_user = admin_conn.get("user") or "postgres"
    admin_pwd = admin_conn.get("password") or app_pwd

    def _lit(v: str) -> str:
        return "'" + (v or "").replace("'", "''") + "'"

    roles = _unique_strings(
        read_env_var(text, "DB_USER"),
        read_env_var(text, "POSTGRES_USER"),
        "postgres",
        "pasarguard",
        db_name,
    )
    migrator.job.log(f"Syncing PostgreSQL role passwords ({len(roles)} roles)...")
    lit = _lit(app_pwd)
    cwd = str(PASARGUARD_DIR)
    for role in roles:
        sql = f'ALTER ROLE "{role}" WITH PASSWORD {lit};'
        cmd = (
            f'cd "{cwd}" && docker compose exec -T {service} '
            f'env PGPASSWORD="{admin_pwd.replace(chr(34), "")}" '
            f'psql -U {admin_user} -d postgres -v ON_ERROR_STOP=0 -c "{sql}"'
        )
        await migrator._run_cmd(cmd, timeout=30)

    if "pgbouncer" in (PASARGUARD_DIR / "docker-compose.yml").read_text(encoding="utf-8", errors="ignore"):
        migrator.job.log("Restarting pgbouncer after role password sync...")
        await migrator._run_cmd(
            ["docker", "compose", "restart", "pgbouncer"],
            cwd=cwd,
            timeout=90,
        )
        await asyncio.sleep(4)
