"""Load .sql dump files into a live database for universal copy."""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path

from app.config import PASARGUARD_DIR
from app.services.pasarguard_ops import resolve_db_service, docker_compose_up, _compose_text


def _filter_timescaledb_extension_sql(sql: str) -> str:
    """Strip CREATE/DROP EXTENSION timescaledb lines (handled around import)."""
    return "\n".join(
        ln for ln in sql.splitlines()
        if not re.search(
            r"^\s*(DROP|CREATE)\s+EXTENSION\s+(IF\s+(EXISTS|NOT\s+EXISTS)\s+)?timescaledb\b",
            ln,
            re.I,
        )
    )


def _compose_has_service(name: str) -> bool:
    text = _compose_text()
    return bool(name and re.search(rf"^\s*{re.escape(name)}\s*:", text, re.MULTILINE))


async def _import_via_compose_service(
    migrator, dump_path: Path, source_db: str, service: str, conn: dict, staging_db: str,
) -> None:
    user = conn.get("user") or (
        "postgres" if source_db in ("postgresql", "timescaledb") else "root"
    )
    pwd = conn.get("password") or ""
    cwd = str(PASARGUARD_DIR)

    if source_db in ("mysql", "mariadb"):
        create_cmd = (
            f'cd "{cwd}" && docker compose exec -T {service} '
            f'mysql -u {user} -p"{pwd}" -e '
            f'"DROP DATABASE IF EXISTS `{staging_db}`; CREATE DATABASE `{staging_db}`;"'
        )
        import_cmd = (
            f'cd "{cwd}" && docker compose exec -T {service} '
            f'mysql -u {user} -p"{pwd}" {staging_db} < "{dump_path}"'
        )
        for cmd in (create_cmd, import_cmd):
            proc = await asyncio.create_subprocess_shell(cmd)
            await proc.wait()
            if proc.returncode != 0:
                raise RuntimeError(f"SQL staging failed (db={staging_db})")
        return

    # PostgreSQL / TimescaleDB via compose
    create_cmd = (
        f'cd "{cwd}" && docker compose exec -T {service} '
        f'env PGPASSWORD="{pwd}" psql -U {user} -d postgres -c '
        f'"DROP DATABASE IF EXISTS {staging_db}; CREATE DATABASE {staging_db};"'
    )
    proc = await asyncio.create_subprocess_shell(create_cmd)
    await proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"SQL staging failed creating db={staging_db}")

    head = ""
    try:
        head = dump_path.read_text(encoding="utf-8", errors="ignore")[:80_000]
    except Exception:
        pass
    use_ts = source_db == "timescaledb" or "timescaledb" in head.lower()
    filtered: Path | None = None
    import_path = dump_path
    if use_ts:
        for sql in (
            "CREATE EXTENSION IF NOT EXISTS timescaledb;",
            "SELECT timescaledb_pre_restore();",
        ):
            p = await asyncio.create_subprocess_shell(
                f'cd "{cwd}" && docker compose exec -T {service} '
                f'env PGPASSWORD="{pwd}" psql -U {user} -d {staging_db} -c "{sql}"'
            )
            await p.wait()
        filtered = dump_path.with_suffix(dump_path.suffix + ".staging-filtered")
        filtered.write_text(
            _filter_timescaledb_extension_sql(
                dump_path.read_text(encoding="utf-8", errors="ignore")
            ),
            encoding="utf-8",
        )
        import_path = filtered

    # Prefer stdin so host paths outside compose mounts still work
    with open(import_path, "rb") as fh:
        proc = await asyncio.create_subprocess_exec(
            "docker", "compose", "exec", "-T",
            "-e", f"PGPASSWORD={pwd}",
            service,
            "psql", "-U", user, "-d", staging_db, "-v", "ON_ERROR_STOP=0",
            cwd=cwd,
            stdin=fh,
        )
        await proc.wait()
    if filtered and filtered.exists():
        try:
            filtered.unlink()
        except OSError:
            pass
    if proc.returncode != 0:
        raise RuntimeError(f"SQL staging failed (db={staging_db})")

    if use_ts:
        p = await asyncio.create_subprocess_shell(
            f'cd "{cwd}" && docker compose exec -T {service} '
            f'env PGPASSWORD="{pwd}" psql -U {user} -d {staging_db} -c '
            f'"SELECT timescaledb_post_restore();"'
        )
        await p.wait()


async def _import_via_ephemeral_mysql(
    migrator, dump_path: Path, conn: dict, staging_db: str, container: str,
) -> dict:
    pwd = conn.get("password") or "pgmigrator"
    user = conn.get("user") or "root"
    port = "33060"

    await migrator._run_cmd([
        "docker", "run", "-d", "--rm", "--name", container,
        "-e", f"MYSQL_ROOT_PASSWORD={pwd}",
        "-p", f"{port}:3306",
        "mysql:8",
    ], timeout=120)
    await asyncio.sleep(15)

    init_cmd = (
        f'docker exec -i {container} mysql -u root -p"{pwd}" -e '
        f'"CREATE DATABASE `{staging_db}`;"'
    )
    proc = await asyncio.create_subprocess_shell(init_cmd)
    await proc.wait()

    with open(dump_path, "rb") as fh:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", container,
            "mysql", "-u", "root", f"-p{pwd}", staging_db,
            stdin=fh,
        )
        await proc.wait()
        if proc.returncode != 0:
            await migrator._run_cmd(["docker", "rm", "-f", container], timeout=30)
            raise RuntimeError("Failed to import MySQL dump into ephemeral container")

    return {
        "host": "127.0.0.1",
        "port": port,
        "database": staging_db,
        "user": "root",
        "password": pwd,
        "_ephemeral_container": container,
    }


async def _wait_ephemeral_pg_ready(container: str, pwd: str, attempts: int = 40) -> None:
    for _ in range(attempts):
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-e", f"PGPASSWORD={pwd}", container,
            "pg_isready", "-U", "postgres",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode == 0:
            return
        await asyncio.sleep(2)
    raise RuntimeError(f"Ephemeral PostgreSQL container {container} did not become ready")


async def _psql_ephemeral(
    container: str, pwd: str, db: str, sql: str | None = None, *, stdin_path: Path | None = None,
) -> int:
    if stdin_path is not None:
        with open(stdin_path, "rb") as fh:
            proc = await asyncio.create_subprocess_exec(
                "docker", "exec", "-i",
                "-e", f"PGPASSWORD={pwd}",
                container,
                "psql", "-U", "postgres", "-d", db, "-v", "ON_ERROR_STOP=0",
                stdin=fh,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            await proc.wait()
            return proc.returncode or 0
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec",
        "-e", f"PGPASSWORD={pwd}",
        container,
        "psql", "-U", "postgres", "-d", db, "-c", sql or "SELECT 1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    await proc.wait()
    return proc.returncode or 0


async def _import_via_ephemeral_postgres(
    migrator,
    dump_path: Path,
    source_db: str,
    conn: dict,
    staging_db: str,
    container: str,
) -> dict:
    """Stage PG/Timescale dump when the source engine is not in live compose (e.g. MySQL target)."""
    pwd = conn.get("password") or "pgmigrator"
    port = "54330"
    use_ts = source_db == "timescaledb"
    if not use_ts:
        try:
            head = dump_path.read_text(encoding="utf-8", errors="ignore")[:80_000]
            use_ts = "timescaledb" in head.lower()
        except Exception:
            use_ts = False

    image = "timescale/timescaledb:latest-pg17" if use_ts else "postgres:17"
    migrator.job.log(
        f"Staging {source_db} SQL dump into ephemeral container ({image})..."
    )
    ok, out = await migrator._run_cmd([
        "docker", "run", "-d", "--rm", "--name", container,
        "-e", f"POSTGRES_PASSWORD={pwd}",
        "-p", f"{port}:5432",
        image,
    ], timeout=180)
    if not ok:
        raise RuntimeError(f"Failed to start ephemeral PostgreSQL: {out[-500:]}")

    try:
        await _wait_ephemeral_pg_ready(container, pwd)
        rc = await _psql_ephemeral(
            container, pwd, "postgres",
            f'DROP DATABASE IF EXISTS "{staging_db}"; CREATE DATABASE "{staging_db}";',
        )
        if rc != 0:
            raise RuntimeError(f"Failed to create staging database {staging_db}")

        import_path = dump_path
        filtered: Path | None = None
        if use_ts:
            await _psql_ephemeral(
                container, pwd, staging_db, "CREATE EXTENSION IF NOT EXISTS timescaledb;",
            )
            await _psql_ephemeral(
                container, pwd, staging_db, "SELECT timescaledb_pre_restore();",
            )
            filtered = dump_path.with_suffix(dump_path.suffix + ".ephemeral-filtered")
            filtered.write_text(
                _filter_timescaledb_extension_sql(
                    dump_path.read_text(encoding="utf-8", errors="ignore")
                ),
                encoding="utf-8",
            )
            import_path = filtered

        rc = await _psql_ephemeral(container, pwd, staging_db, stdin_path=import_path)
        if filtered and filtered.exists():
            try:
                filtered.unlink()
            except OSError:
                pass
        if rc != 0:
            raise RuntimeError("Failed to import PostgreSQL dump into ephemeral container")

        if use_ts:
            await _psql_ephemeral(
                container, pwd, staging_db, "SELECT timescaledb_post_restore();",
            )
    except Exception:
        await migrator._run_cmd(["docker", "rm", "-f", container], timeout=30)
        raise

    return {
        "host": "127.0.0.1",
        "port": port,
        "database": staging_db,
        "user": "postgres",
        "password": pwd,
        "_ephemeral_container": container,
    }


async def import_sql_dump_to_live_db(
    migrator,
    dump_path: str,
    source_db: str,
    conn: dict,
) -> dict:
    """Import .sql dump into live DB and return DSN for reading."""
    path = Path(dump_path)
    if not path.exists():
        raise RuntimeError(f"SQL dump not found: {dump_path}")

    staging_db = f"pgmig_{uuid.uuid4().hex[:8]}"
    service = resolve_db_service(source_db)

    if service and _compose_has_service(service):
        migrator.job.log(f"Staging SQL dump into compose service {service}...")
        await docker_compose_up(migrator, [service])
        await asyncio.sleep(5)
        await _import_via_compose_service(migrator, path, source_db, service, conn, staging_db)
        from app.services.db_credentials import migration_port
        return {
            "host": conn.get("host") or "127.0.0.1",
            "port": migration_port(conn, source_db),
            "database": staging_db,
            "user": conn.get("user") or (
                "postgres" if source_db in ("postgresql", "timescaledb") else "root"
            ),
            "password": conn.get("password") or "",
        }

    if source_db in ("mysql", "mariadb"):
        container = f"pgmig-mysql-{uuid.uuid4().hex[:6]}"
        migrator.job.log("Staging SQL dump into ephemeral MySQL container...")
        return await _import_via_ephemeral_mysql(migrator, path, conn, staging_db, container)

    if source_db in ("postgresql", "timescaledb"):
        container = f"pgmig-pg-{uuid.uuid4().hex[:6]}"
        return await _import_via_ephemeral_postgres(
            migrator, path, source_db, conn, staging_db, container,
        )

    raise RuntimeError(
        f"Cannot stage {source_db} SQL dump — start the {source_db} service or use SQLite backup"
    )
