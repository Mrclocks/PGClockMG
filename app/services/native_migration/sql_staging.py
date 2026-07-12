"""Load .sql dump files into a live database for universal copy."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from app.config import PASARGUARD_DIR
from app.services.pasarguard_ops import resolve_db_service, docker_compose_up, _compose_text


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
    else:
        create_cmd = (
            f'cd "{cwd}" && docker compose exec -T {service} '
            f'env PGPASSWORD="{pwd}" psql -U {user} -d postgres -c '
            f'"DROP DATABASE IF EXISTS {staging_db}; CREATE DATABASE {staging_db};"'
        )
        import_cmd = (
            f'cd "{cwd}" && docker compose exec -T {service} '
            f'env PGPASSWORD="{pwd}" psql -U {user} -d {staging_db} -f "{dump_path}"'
        )

    for cmd in (create_cmd, import_cmd):
        proc = await asyncio.create_subprocess_shell(cmd)
        await proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"SQL staging failed (db={staging_db})")


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

    if service and service in _compose_text():
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

    raise RuntimeError(
        f"Cannot stage {source_db} SQL dump — start the {source_db} service or use SQLite backup"
    )
