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
    # CREATE DATABASE cannot share a transaction with DROP — run separately.
    # App role (pasarguard) often lacks CREATEDB — try postgres / superuser too.
    users_to_try = []
    for u in (user, "postgres", "pasarguard"):
        if u and u not in users_to_try:
            users_to_try.append(u)
    created = False
    last_err = ""
    for u in users_to_try:
        for sql in (
            f'DROP DATABASE IF EXISTS "{staging_db}";',
            f'CREATE DATABASE "{staging_db}";',
        ):
            create_cmd = (
                f'cd "{cwd}" && docker compose exec -T {service} '
                f'env PGPASSWORD="{pwd}" psql -U {u} -d postgres -v ON_ERROR_STOP=1 -c '
                f"\"{sql}\""
            )
            proc = await asyncio.create_subprocess_shell(
                create_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out_b, _ = await proc.communicate()
            out = (out_b or b"").decode("utf-8", errors="replace")
            if proc.returncode != 0 and "DROP DATABASE" in sql:
                continue
            if proc.returncode != 0 and "CREATE DATABASE" in sql:
                last_err = out
                break
            if "CREATE DATABASE" in sql and proc.returncode == 0:
                created = True
                user = u  # use the user that could create DB for import too
        if created:
            break
    if not created:
        raise RuntimeError(
            f"SQL staging failed creating db={staging_db}: {last_err[-400:]}"
        )

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
    pwd = "pgmigrator"
    user = "root"
    port = _pick_free_host_port(33060)

    await migrator._run_cmd(["docker", "rm", "-f", container], timeout=30)
    await migrator._run_cmd([
        "docker", "run", "-d", "--name", container,
        "-e", f"MYSQL_ROOT_PASSWORD={pwd}",
        "-p", f"127.0.0.1:{port}:3306",
        "mysql:8",
    ], timeout=180)

    # Wait until mysql accepts connections (init can take a while)
    ready = False
    last = ""
    for i in range(60):
        if not await _container_running(container):
            raise RuntimeError(
                f"Ephemeral MySQL exited early:\n{(await _container_logs_tail(container))[-800:]}"
            )
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", container,
            "mysqladmin", "ping", "-h", "127.0.0.1", "-uroot", f"-p{pwd}", "--silent",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out_b, _ = await proc.communicate()
        last = (out_b or b"").decode("utf-8", errors="replace")
        if proc.returncode == 0:
            ready = True
            break
        await asyncio.sleep(2)
    if not ready:
        await migrator._run_cmd(["docker", "rm", "-f", container], timeout=30)
        raise RuntimeError(f"Ephemeral MySQL did not become ready: {last[-300:]}")

    init_cmd = (
        f'docker exec -i {container} mysql -h127.0.0.1 -u root -p"{pwd}" -e '
        f'"CREATE DATABASE `{staging_db}`;"'
    )
    proc = await asyncio.create_subprocess_shell(init_cmd)
    await proc.wait()
    if proc.returncode != 0:
        await migrator._run_cmd(["docker", "rm", "-f", container], timeout=30)
        raise RuntimeError(f"Failed to create ephemeral MySQL database {staging_db}")

    with open(dump_path, "rb") as fh:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", container,
            "mysql", "-h127.0.0.1", "-u", "root", f"-p{pwd}", staging_db,
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
        "user": user,
        "password": pwd,
        "_ephemeral_container": container,
    }


_PG_TRANSIENT = (
    "shutting down",
    "starting up",
    "connection refused",
    "could not connect",
    "server closed the connection",
    "the database system is",
    "connection reset",
    "too many connections",
)


def _is_transient_pg_error(text: str) -> bool:
    low = (text or "").lower()
    return any(t in low for t in _PG_TRANSIENT)


async def _container_running(name: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "docker", "inspect", "-f", "{{.State.Running}}", name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out_b, _ = await proc.communicate()
    return (out_b or b"").decode().strip().lower() == "true"


async def _container_logs_tail(name: str, n: int = 60) -> str:
    proc = await asyncio.create_subprocess_exec(
        "docker", "logs", "--tail", str(n), name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out_b, _ = await proc.communicate()
    return (out_b or b"").decode("utf-8", errors="replace")


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
    # Force TCP inside the container so we don't race unix-socket reopen during init restart
    base = [
        "docker", "exec",
        "-e", f"PGPASSWORD={pwd}",
        container,
        "psql", "-h", "127.0.0.1", "-U", "postgres", "-d", db, "-v", stop_flag,
    ]
    if stdin_path is not None:
        with open(stdin_path, "rb") as fh:
            proc = await asyncio.create_subprocess_exec(
                "docker", "exec", "-i",
                "-e", f"PGPASSWORD={pwd}",
                container,
                "psql", "-h", "127.0.0.1", "-U", "postgres", "-d", db, "-v", stop_flag,
                stdin=fh,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out_b, _ = await proc.communicate()
            return proc.returncode or 0, (out_b or b"").decode("utf-8", errors="replace")
    # One statement per -c — CREATE/DROP DATABASE cannot share a transaction
    proc = await asyncio.create_subprocess_exec(
        *base, "-c", sql or "SELECT 1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out_b, _ = await proc.communicate()
    return proc.returncode or 0, (out_b or b"").decode("utf-8", errors="replace")


async def _wait_ephemeral_pg_ready(container: str, pwd: str, attempts: int = 90) -> None:
    """
    Wait until Postgres is *stably* ready.

    Official Postgres/Timescale images start a temporary postmaster for initdb,
    shut it down, then start the real server. A single pg_isready/SELECT 1 success
    during that window causes CREATE DATABASE to fail with "shutting down".
    Require several consecutive successes spanning multiple seconds.
    """
    last = ""
    streak = 0
    need_streak = 5  # ~10s of continuous readiness at 2s interval
    for i in range(attempts):
        if not await _container_running(container):
            logs = await _container_logs_tail(container)
            raise RuntimeError(
                f"Ephemeral PostgreSQL container {container} exited early:\n{logs[-1000:]}"
            )
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-e", f"PGPASSWORD={pwd}", container,
            "pg_isready", "-h", "127.0.0.1", "-U", "postgres",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out_b, _ = await proc.communicate()
        if proc.returncode == 0:
            rc, out = await _psql_ephemeral(container, pwd, "postgres", "SELECT 1;")
            if rc == 0 and not _is_transient_pg_error(out):
                streak += 1
                if streak >= need_streak:
                    return
            else:
                streak = 0
                last = out or (out_b or b"").decode("utf-8", errors="replace")
        else:
            streak = 0
            last = (out_b or b"").decode("utf-8", errors="replace")
        await asyncio.sleep(2)
    logs = await _container_logs_tail(container)
    raise RuntimeError(
        f"Ephemeral PostgreSQL container {container} did not become ready: {last[-400:]}\n"
        f"--- docker logs ---\n{logs[-800:]}"
    )


async def _psql_ephemeral_retry(
    container: str,
    pwd: str,
    db: str,
    sql: str,
    *,
    attempts: int = 20,
    on_error_stop: bool = True,
    allow_missing_drop: bool = False,
) -> str:
    """Run one SQL statement with retries across Postgres init restarts."""
    last = ""
    for i in range(attempts):
        if not await _container_running(container):
            raise RuntimeError(
                f"Container {container} stopped during SQL:\n"
                f"{(await _container_logs_tail(container))[-800:]}"
            )
        rc, out = await _psql_ephemeral(
            container, pwd, db, sql, on_error_stop=on_error_stop,
        )
        last = out
        if rc == 0:
            return out
        if allow_missing_drop and "does not exist" in (out or "").lower():
            return out
        if _is_transient_pg_error(out):
            await asyncio.sleep(2 if i < 8 else 3)
            continue
        raise RuntimeError(f"psql failed: {sql[:120]} → {out[-500:]}")
    raise RuntimeError(
        f"psql still failing after retries ({sql[:80]}): {last[-500:]}"
    )


async def _create_pg_staging_db(container: str, pwd: str, staging_db: str) -> None:
    """CREATE DATABASE must be its own psql -c (cannot share a transaction with DROP)."""
    safe = "".join(c for c in staging_db if c.isalnum() or c == "_")
    if safe != staging_db or not safe:
        raise RuntimeError(f"Invalid staging database name: {staging_db}")
    await _psql_ephemeral_retry(
        container, pwd, "postgres",
        f'DROP DATABASE IF EXISTS "{safe}";',
        allow_missing_drop=True,
    )
    await _psql_ephemeral_retry(
        container, pwd, "postgres",
        f'CREATE DATABASE "{safe}";',
    )
    # Prove the new DB accepts connections (catches post-create restart races)
    await _psql_ephemeral_retry(container, pwd, safe, "SELECT 1;")


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
    # No --rm until we finish: keeps logs if init crashes; we remove explicitly in finally paths
    ok, out = await migrator._run_cmd([
        "docker", "run", "-d", "--name", container,
        "-e", f"POSTGRES_PASSWORD={pwd}",
        "-e", "POSTGRES_USER=postgres",
        "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
        "-p", f"127.0.0.1:{port}:5432",
        image,
    ], timeout=300)
    if not ok:
        raise RuntimeError(f"Failed to start ephemeral PostgreSQL: {out[-500:]}")

    try:
        migrator.job.log("Waiting for ephemeral Postgres to finish init (stable ready)...")
        await _wait_ephemeral_pg_ready(container, pwd)
        await _create_pg_staging_db(container, pwd, staging_db)
        migrator.job.log(f"Ephemeral staging DB ready: {staging_db}")

        import_path = dump_path
        filtered: Path | None = None
        if use_ts:
            await _psql_ephemeral_retry(
                container, pwd, staging_db,
                "CREATE EXTENSION IF NOT EXISTS timescaledb;",
            )
            await _psql_ephemeral_retry(
                container, pwd, staging_db,
                "SELECT timescaledb_pre_restore();",
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

        # Dump restore: tolerate non-fatal object errors (roles/tablespaces)
        last_import_err = ""
        imported = False
        for attempt in range(5):
            rc, out = await _psql_ephemeral(
                container, pwd, staging_db, stdin_path=import_path, on_error_stop=False,
            )
            last_import_err = out
            if rc == 0 or not _is_transient_pg_error(out):
                # rc!=0 with non-transient may still have loaded most objects — check tables below
                imported = True
                if rc != 0 and not _is_transient_pg_error(out):
                    migrator.job.log(
                        f"Dump import finished with warnings/errors (will verify tables): "
                        f"{out[-300:]}"
                    )
                break
            migrator.job.log(f"Transient import failure — retry {attempt + 1}/5")
            await asyncio.sleep(3)
        if filtered and filtered.exists():
            try:
                filtered.unlink()
            except OSError:
                pass
        if not imported:
            raise RuntimeError(
                "Failed to import PostgreSQL dump into ephemeral container:\n"
                f"{last_import_err[-800:]}"
            )

        # Confirm panel tables landed (detect empty/wrong dump early)
        table_count = 0
        for _ in range(10):
            rc, out = await _psql_ephemeral(
                container, pwd, staging_db,
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE';",
            )
            if rc == 0:
                for line in (out or "").splitlines():
                    if line.strip().isdigit():
                        table_count = int(line.strip())
                        break
                if table_count >= 1:
                    break
            if _is_transient_pg_error(out):
                await asyncio.sleep(2)
                continue
            break
        if table_count < 1:
            raise RuntimeError(
                "Ephemeral staging DB has no tables after dump import — "
                f"backup dump may be empty or incompatible.\n{last_import_err[-500:]}"
            )
        migrator.job.log(f"Ephemeral dump import finished ({table_count} tables)")

        if use_ts:
            await _psql_ephemeral_retry(
                container, pwd, staging_db,
                "SELECT timescaledb_post_restore();",
                on_error_stop=False,
            )
    except Exception as e:
        migrator.job.log(f"Ephemeral staging failed — cleaning up ({e})")
        try:
            logs = await _container_logs_tail(container)
            if logs.strip():
                migrator.job.log(f"Ephemeral container logs:\n{logs[-1200:]}")
        except Exception:
            pass
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
    """Import .sql dump into a staging DB and return DSN for reading.

    Timescale dumps are NEVER staged into plain PostgreSQL (resolve_db_service
    may fall back to the postgresql compose service — that path always breaks).
    """
    path = Path(dump_path)
    if not path.exists():
        raise RuntimeError(f"SQL dump not found: {dump_path}")

    staging_db = f"pgmig_{uuid.uuid4().hex[:8]}"

    dump_is_timescale = source_db == "timescaledb"
    if source_db in ("postgresql", "timescaledb") and not dump_is_timescale:
        try:
            head = path.read_text(encoding="utf-8", errors="ignore")[:80_000]
            dump_is_timescale = "timescaledb" in head.lower()
        except Exception:
            pass

    # ── Timescale source dumps ──────────────────────────────────────────
    if dump_is_timescale or source_db == "timescaledb":
        # Only a real timescaledb compose service can accept this dump.
        if _compose_has_service("timescaledb"):
            migrator.job.log("Staging Timescale dump into compose service timescaledb...")
            await docker_compose_up(migrator, ["timescaledb"])
            await asyncio.sleep(5)
            await _import_via_compose_service(
                migrator, path, "timescaledb", "timescaledb", conn, staging_db,
            )
            from app.services.db_credentials import migration_port
            return {
                "host": conn.get("host") or "127.0.0.1",
                "port": migration_port(conn, "timescaledb"),
                "database": staging_db,
                "user": conn.get("user") or "postgres",
                "password": conn.get("password") or "",
            }
        # No Timescale in compose (e.g. target is mysql/postgresql/mariadb) → ephemeral
        container = f"pgmig-pg-{uuid.uuid4().hex[:6]}"
        return await _import_via_ephemeral_postgres(
            migrator, path, "timescaledb", conn, staging_db, container,
        )

    # ── MySQL / MariaDB source dumps ────────────────────────────────────
    if source_db in ("mysql", "mariadb"):
        service = resolve_db_service(source_db)
        # Only stage into a matching engine family — never into the wrong service
        if service and service in ("mysql", "mariadb") and _compose_has_service(service):
            migrator.job.log(f"Staging SQL dump into compose service {service}...")
            await docker_compose_up(migrator, [service])
            await asyncio.sleep(5)
            await _import_via_compose_service(
                migrator, path, source_db, service, conn, staging_db,
            )
            from app.services.db_credentials import migration_port
            return {
                "host": conn.get("host") or "127.0.0.1",
                "port": migration_port(conn, source_db),
                "database": staging_db,
                "user": conn.get("user") or "root",
                "password": conn.get("password") or "",
            }
        container = f"pgmig-mysql-{uuid.uuid4().hex[:6]}"
        migrator.job.log("Staging SQL dump into ephemeral MySQL container...")
        return await _import_via_ephemeral_mysql(migrator, path, conn, staging_db, container)

    # ── Plain PostgreSQL source dumps ───────────────────────────────────
    if source_db == "postgresql":
        service = None
        if _compose_has_service("postgresql"):
            service = "postgresql"
        elif _compose_has_service("timescaledb"):
            # Timescale accepts plain PG dumps
            service = "timescaledb"
        if service:
            migrator.job.log(f"Staging PostgreSQL dump into compose service {service}...")
            await docker_compose_up(migrator, [service])
            await asyncio.sleep(5)
            await _import_via_compose_service(
                migrator, path, source_db, service, conn, staging_db,
            )
            from app.services.db_credentials import migration_port
            return {
                "host": conn.get("host") or "127.0.0.1",
                "port": migration_port(conn, source_db),
                "database": staging_db,
                "user": conn.get("user") or "postgres",
                "password": conn.get("password") or "",
            }
        container = f"pgmig-pg-{uuid.uuid4().hex[:6]}"
        return await _import_via_ephemeral_postgres(
            migrator, path, source_db, conn, staging_db, container,
        )

    raise RuntimeError(
        f"Cannot stage {source_db} SQL dump — start the {source_db} service or use SQLite backup"
    )
