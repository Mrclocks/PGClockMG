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
    # CREATE DATABASE cannot share a transaction with DROP — run separately
    for sql in (
        f"DROP DATABASE IF EXISTS {staging_db};",
        f"CREATE DATABASE {staging_db};",
    ):
        create_cmd = (
            f'cd "{cwd}" && docker compose exec -T {service} '
            f'env PGPASSWORD="{pwd}" psql -U {user} -d postgres -v ON_ERROR_STOP=1 -c '
            f'"{sql}"'
        )
        proc = await asyncio.create_subprocess_shell(create_cmd)
        await proc.wait()
        if proc.returncode != 0 and "DROP DATABASE" not in sql:
            raise RuntimeError(f"SQL staging failed creating db={staging_db}")
        if proc.returncode != 0 and "DROP DATABASE" in sql:
            # ignore missing DB on drop
            pass

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


async def _container_running(name: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "docker", "inspect", "-f", "{{.State.Running}}", name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out_b, _ = await proc.communicate()
    return (out_b or b"").decode().strip().lower() == "true"


def _pick_free_host_port(preferred: int = 54330) -> str:
    """Bind an ephemeral host port so parallel/leftover mappings don't collide."""
    import socket

    for candidate in (preferred, preferred + 1, preferred + 2, 0):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", candidate))
                return str(s.getsockname()[1])
        except OSError:
            continue
    return str(preferred)


async def _wait_ephemeral_pg_ready(container: str, pwd: str, attempts: int = 60) -> None:
    """Wait until Postgres accepts connections (pg_isready + SELECT 1)."""
    last = ""
    for i in range(attempts):
        if not await _container_running(container):
            # Surface crash logs (OOM / image pull / bad arch)
            proc = await asyncio.create_subprocess_exec(
                "docker", "logs", "--tail", "40", container,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            logs_b, _ = await proc.communicate()
            raise RuntimeError(
                f"Ephemeral PostgreSQL container {container} exited early:\n"
                f"{(logs_b or b'').decode('utf-8', errors='replace')[-800:]}"
            )
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-e", f"PGPASSWORD={pwd}", container,
            "pg_isready", "-U", "postgres",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out_b, _ = await proc.communicate()
        if proc.returncode == 0:
            rc, out = await _psql_ephemeral(container, pwd, "postgres", "SELECT 1;")
            if rc == 0:
                return
            last = out
        else:
            last = (out_b or b"").decode("utf-8", errors="replace")
        await asyncio.sleep(2 if i < 10 else 3)
    raise RuntimeError(
        f"Ephemeral PostgreSQL container {container} did not become ready: {last[-400:]}"
    )


async def _psql_ephemeral(
    container: str,
    pwd: str,
    db: str,
    sql: str | None = None,
    *,
    stdin_path: Path | None = None,
    on_error_stop: bool = True,
) -> tuple[int, str]:
    stop_flag = "ON_ERROR_STOP=1" if on_error_stop else "ON_ERROR_STOP=0"
    if stdin_path is not None:
        with open(stdin_path, "rb") as fh:
            proc = await asyncio.create_subprocess_exec(
                "docker", "exec", "-i",
                "-e", f"PGPASSWORD={pwd}",
                container,
                "psql", "-U", "postgres", "-d", db, "-v", stop_flag,
                stdin=fh,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out_b, _ = await proc.communicate()
            return proc.returncode or 0, (out_b or b"").decode("utf-8", errors="replace")
    # One statement per -c — CREATE/DROP DATABASE cannot run inside a multi-statement transaction
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec",
        "-e", f"PGPASSWORD={pwd}",
        container,
        "psql", "-U", "postgres", "-d", db, "-v", stop_flag, "-c", sql or "SELECT 1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out_b, _ = await proc.communicate()
    return proc.returncode or 0, (out_b or b"").decode("utf-8", errors="replace")


async def _create_pg_staging_db(container: str, pwd: str, staging_db: str) -> None:
    """CREATE DATABASE must be its own psql -c (cannot share a transaction with DROP)."""
    safe = "".join(c for c in staging_db if c.isalnum() or c == "_")
    if safe != staging_db or not safe:
        raise RuntimeError(f"Invalid staging database name: {staging_db}")
    rc, out = await _psql_ephemeral(
        container, pwd, "postgres", f'DROP DATABASE IF EXISTS "{safe}";',
    )
    # DROP of missing DB is fine; still check for hard failures
    if rc != 0 and "does not exist" not in out.lower():
        raise RuntimeError(f"Failed to drop staging database {safe}: {out[-500:]}")
    rc, out = await _psql_ephemeral(
        container, pwd, "postgres", f'CREATE DATABASE "{safe}";',
    )
    if rc != 0:
        raise RuntimeError(f"Failed to create staging database {safe}: {out[-500:]}")


async def _import_via_ephemeral_postgres(
    migrator,
    dump_path: Path,
    source_db: str,
    conn: dict,
    staging_db: str,
    container: str,
) -> dict:
    """Stage PG/Timescale dump when the source engine is not in live compose (e.g. MySQL target)."""
    # Always use a dedicated password for the throwaway container — never the live MySQL secret.
    pwd = "pgmigrator"
    port = _pick_free_host_port(54330)
    use_ts = source_db == "timescaledb"
    if not use_ts:
        try:
            head = dump_path.read_text(encoding="utf-8", errors="ignore")[:80_000]
            use_ts = "timescaledb" in head.lower()
        except Exception:
            use_ts = False

    image = "timescale/timescaledb:latest-pg17" if use_ts else "postgres:17"
    migrator.job.log(
        f"Staging {source_db} SQL dump into ephemeral container ({image}, host port {port})..."
    )
    # Clean leftover name from a previous crash
    await migrator._run_cmd(["docker", "rm", "-f", container], timeout=30)
    ok, out = await migrator._run_cmd([
        "docker", "run", "-d", "--rm", "--name", container,
        "-e", f"POSTGRES_PASSWORD={pwd}",
        "-e", "POSTGRES_USER=postgres",
        "-p", f"127.0.0.1:{port}:5432",
        image,
    ], timeout=300)
    if not ok:
        raise RuntimeError(f"Failed to start ephemeral PostgreSQL: {out[-500:]}")

    try:
        await _wait_ephemeral_pg_ready(container, pwd)
        await _create_pg_staging_db(container, pwd, staging_db)
        migrator.job.log(f"Ephemeral staging DB ready: {staging_db}")

        import_path = dump_path
        filtered: Path | None = None
        if use_ts:
            rc, out = await _psql_ephemeral(
                container, pwd, staging_db, "CREATE EXTENSION IF NOT EXISTS timescaledb;",
            )
            if rc != 0:
                raise RuntimeError(f"Failed to enable timescaledb extension: {out[-500:]}")
            await _psql_ephemeral(
                container, pwd, staging_db, "SELECT timescaledb_pre_restore();",
                on_error_stop=False,
            )
            filtered = dump_path.with_suffix(dump_path.suffix + ".ephemeral-filtered")
            filtered.write_text(
                _filter_timescaledb_extension_sql(
                    dump_path.read_text(encoding="utf-8", errors="ignore")
                ),
                encoding="utf-8",
            )
            import_path = filtered

        # Dump restore: tolerate non-fatal object errors (roles/tablespaces) but require success overall
        rc, out = await _psql_ephemeral(
            container, pwd, staging_db, stdin_path=import_path, on_error_stop=False,
        )
        if filtered and filtered.exists():
            try:
                filtered.unlink()
            except OSError:
                pass
        if rc != 0:
            raise RuntimeError(
                "Failed to import PostgreSQL dump into ephemeral container:\n"
                f"{out[-800:]}"
            )
        # Confirm panel tables landed (detect empty/wrong dump early)
        rc, out = await _psql_ephemeral(
            container, pwd, staging_db,
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE';",
        )
        table_count = 0
        for line in (out or "").splitlines():
            if line.strip().isdigit():
                table_count = int(line.strip())
                break
        if table_count < 1:
            raise RuntimeError(
                "Ephemeral staging DB has no tables after dump import — "
                "backup dump may be empty or incompatible"
            )
        migrator.job.log(f"Ephemeral dump import finished ({table_count} tables)")

        if use_ts:
            await _psql_ephemeral(
                container, pwd, staging_db, "SELECT timescaledb_post_restore();",
                on_error_stop=False,
            )
    except Exception as e:
        migrator.job.log(f"Ephemeral staging failed — cleaning up ({e})")
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
