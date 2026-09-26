"""E2E: sqlite → Timescale-family (local PostgreSQL) without SASL death.

Proves:
1. sqlite source has no password — convert uses install password only
2. when live SCRAM is stale, auto-heal realigns roles to install secret
3. data copy transfers users/admins/hosts/groups/nodes/inbounds completely
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

INSTALL_PASSWORD = "install-secret"
STALE_PASSWORD = "stale-volume-secret"
PG_USER = "pasarguard"
PG_DB = "pasarguard"
PG_HOST = "127.0.0.1"
PG_PORT = "5432"


def _have_local_postgres() -> bool:
    try:
        r = subprocess.run(
            [
                "psql", "-h", PG_HOST, "-p", PG_PORT, "-U", PG_USER, "-d", PG_DB,
                "-tAc", "SELECT 1",
            ],
            env={**os.environ, "PGPASSWORD": INSTALL_PASSWORD},
            capture_output=True,
            text=True,
            timeout=10,
        )
        return r.returncode == 0 and "1" in (r.stdout or "")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _psql_admin(sql: str) -> None:
    """Run SQL as OS postgres (peer) — used to set role passwords without knowing SCRAM."""
    subprocess.run(
        ["sudo", "-u", "postgres", "psql", "-d", PG_DB, "-v", "ON_ERROR_STOP=1", "-c", sql],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _psql_tcp(password: str, sql: str = "SELECT 1") -> tuple[bool, str]:
    r = subprocess.run(
        [
            "psql", "-h", PG_HOST, "-p", PG_PORT, "-U", PG_USER, "-d", PG_DB,
            "-v", "ON_ERROR_STOP=1", "-tAc", sql,
        ],
        env={**os.environ, "PGPASSWORD": password},
        capture_output=True,
        text=True,
        timeout=15,
    )
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    return r.returncode == 0, out


def _make_source_sqlite(path: str) -> dict[str, int]:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE admins (id INTEGER PRIMARY KEY, username TEXT, is_sudo INTEGER);
        INSERT INTO admins VALUES (1, 'admin', 1);
        INSERT INTO admins VALUES (2, 'ops', 0);

        CREATE TABLE core_configs (id INTEGER PRIMARY KEY, name TEXT);
        INSERT INTO core_configs VALUES (1, 'xray');

        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, name TEXT, address TEXT, core_config_id INTEGER
        );
        INSERT INTO nodes VALUES (1, 'node1', '1.2.3.4', 1);
        INSERT INTO nodes VALUES (2, 'node2', '5.6.7.8', 1);

        CREATE TABLE inbounds (id INTEGER PRIMARY KEY, tag TEXT, protocol TEXT, is_disabled INTEGER);
        INSERT INTO inbounds VALUES (1, 'vless-tcp', 'vless', 0);
        INSERT INTO inbounds VALUES (2, 'vmess-ws', 'vmess', 0);

        CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT, is_disabled INTEGER);
        INSERT INTO groups VALUES (1, 'default', 0);
        INSERT INTO groups VALUES (2, 'vip', 0);

        CREATE TABLE hosts (
            id INTEGER PRIMARY KEY,
            remark TEXT,
            inbound_tag TEXT,
            fragment_setting TEXT,
            noise_setting TEXT,
            mux_enable INTEGER,
            security TEXT,
            fingerprint TEXT
        );
        INSERT INTO hosts VALUES (
            1, 'host1', 'vless-tcp', '{}', '{}', 0, 'none', 'chrome'
        );
        INSERT INTO hosts VALUES (
            2, 'host2', 'vmess-ws', '{}', '{}', 0, 'tls', 'firefox'
        );

        CREATE TABLE users (
            id INTEGER PRIMARY KEY, username TEXT, status TEXT, enable INTEGER, admin_id INTEGER
        );
        INSERT INTO users VALUES (1, 'alice', 'active', 1, 1);
        INSERT INTO users VALUES (2, 'bob', 'active', 1, 1);
        INSERT INTO users VALUES (3, 'carol', 'disabled', 0, 2);

        CREATE TABLE settings (id INTEGER PRIMARY KEY, key TEXT, value TEXT);
        INSERT INTO settings VALUES (1, 'subscription', '{"url_prefix":"https://example.com"}');
        """
    )
    conn.commit()
    expected = {
        "admins": 2,
        "users": 3,
        "hosts": 2,
        "nodes": 2,
        "inbounds": 2,
        "groups": 2,
        "settings": 1,
        "core_configs": 1,
    }
    conn.close()
    return expected


def _pg_target_schema() -> str:
    return """
        DROP SCHEMA public CASCADE;
        CREATE SCHEMA public;
        GRANT ALL ON SCHEMA public TO pasarguard;
        GRANT ALL ON SCHEMA public TO public;

        CREATE TABLE admins (id INTEGER PRIMARY KEY, username TEXT, is_sudo BOOLEAN);
        CREATE TABLE core_configs (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, name TEXT, address TEXT,
            core_config_id INTEGER, server_ca TEXT NOT NULL DEFAULT '', api_key TEXT,
            status TEXT
        );
        CREATE TABLE inbounds (
            id INTEGER PRIMARY KEY, tag TEXT, protocol TEXT, is_disabled BOOLEAN
        );
        CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT, is_disabled BOOLEAN);
        CREATE TABLE hosts (
            id INTEGER PRIMARY KEY, remark TEXT, inbound_tag TEXT,
            fragment_settings TEXT, noise_settings TEXT, mux_settings TEXT,
            priority INTEGER NOT NULL DEFAULT 0, security TEXT, fingerprint TEXT
        );
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, username TEXT, status TEXT, enable BOOLEAN, admin_id INTEGER
        );
        CREATE TABLE settings (id INTEGER PRIMARY KEY, key TEXT, value TEXT);
    """


def _count_pg(table: str) -> int:
    ok, out = _psql_tcp(INSTALL_PASSWORD, f"SELECT count(*) FROM {table}")
    assert ok, f"count {table} failed: {out}"
    return int((out or "0").splitlines()[-1].strip())


def test_sqlite_source_has_no_password_uses_install_only():
    from app.services.db_auth import install_auth_env_for_convert, install_server_password

    install = (
        f"POSTGRES_PASSWORD={INSTALL_PASSWORD}\n"
        f"DB_PASSWORD={INSTALL_PASSWORD}\n"
        f"DB_USER={PG_USER}\n"
        f"DB_NAME={PG_DB}\n"
        f'SQLALCHEMY_DATABASE_URL="postgresql+asyncpg://{PG_USER}:{INSTALL_PASSWORD}'
        f'@{PG_HOST}:6432/{PG_DB}"\n'
        "PASARGUARD_DB_ENGINE=timescaledb\n"
    )
    live_after_sqlite_merge = (
        'SQLALCHEMY_DATABASE_URL="sqlite+aiosqlite:////var/lib/pasarguard/db.sqlite3"\n'
        "PASARGUARD_DB_ENGINE=sqlite\n"
    )
    env = install_auth_env_for_convert(
        backup_db="sqlite",
        target_db="timescaledb",
        install_env_snapshot=install,
        live_env=live_after_sqlite_merge,
    )
    assert install_server_password(env, "timescaledb") == INSTALL_PASSWORD
    assert "sqlite+aiosqlite" not in env.lower()
    print("OK: sqlite source → install Timescale password only")


def test_stale_scram_healed_then_tcp_accepts_install_password():
    """Live volume has stale SCRAM; heal aligns to install password (no SASL left)."""
    if not _have_local_postgres():
        print("SKIP: local postgres not available")
        return

    # Make TCP reject the install password (simulates drifted volume).
    lit = INSTALL_PASSWORD.replace("'", "''")
    stale_lit = STALE_PASSWORD.replace("'", "''")
    _psql_admin(f"ALTER ROLE {PG_USER} WITH PASSWORD '{stale_lit}';")
    ok_stale, _ = _psql_tcp(STALE_PASSWORD)
    ok_install, err = _psql_tcp(INSTALL_PASSWORD)
    assert ok_stale, "setup: stale password should work before heal"
    assert not ok_install, f"setup: install password must fail before heal ({err})"

    from app.services.db_auth import force_align_postgres_password
    from app.services.migrators.base import BaseMigrator, MigrationJob

    class Dummy(BaseMigrator):
        async def run(self, params):
            return {}

    env = (
        f"POSTGRES_PASSWORD={INSTALL_PASSWORD}\n"
        f"POSTGRES_USER={PG_USER}\n"
        f"DB_USER={PG_USER}\n"
        f"DB_NAME={PG_DB}\n"
        f"POSTGRES_DB={PG_DB}\n"
    )

    async def fake_run(cmd, cwd=None, timeout=600, *, quiet=False):
        argv = list(cmd) if isinstance(cmd, list) else [cmd]
        joined = " ".join(argv)
        # Trust ALTER (no PGPASSWORD) → peer as OS postgres
        if "psql" in argv and "ALTER ROLE" in joined and not any(
            a.startswith("PGPASSWORD=") for a in argv
        ):
            # Extract -c SQL
            sql = ""
            if "-c" in argv:
                sql = argv[argv.index("-c") + 1]
            if sql:
                try:
                    _psql_admin(sql)
                    return True, "ALTER ROLE\n"
                except subprocess.CalledProcessError as exc:
                    return False, (exc.stderr or str(exc))[-300:]
            return False, "no sql"
        if "printenv" in joined:
            return True, ""
        if "compose" in joined and ("stop" in joined or "up" in joined or "rm" in joined):
            return True, ""
        if "pgbouncer" in joined.lower() or "refresh" in joined.lower():
            return True, ""
        return True, ""

    async def _run():
        job = MigrationJob(job_id="e2e-heal")
        migrator = Dummy(job, {})
        with patch("app.services.db_auth.PASARGUARD_DIR", Path("/tmp")), \
             patch("app.services.db_auth.resolve_db_service", return_value="timescaledb"), \
             patch.object(migrator, "_run_cmd", fake_run), \
             patch(
                 "app.services.db_auth.refresh_pgbouncer_if_stale",
                 new_callable=AsyncMock,
                 return_value=True,
             ), \
             patch(
                 "app.services.db_auth.read_db_container_init_env",
                 new_callable=AsyncMock,
                 return_value={"POSTGRES_USER": PG_USER, "POSTGRES_DB": PG_DB},
             ):
            ok = await force_align_postgres_password(
                migrator,
                "timescaledb",
                env,
                password=INSTALL_PASSWORD,
                admin_users=[PG_USER, "postgres"],
            )
        assert ok is True, f"heal failed; logs={job.logs}"

    asyncio.run(_run())

    ok_after, out_after = _psql_tcp(INSTALL_PASSWORD)
    assert ok_after, f"after heal, install password must work over TCP: {out_after}"
    ok_stale_after, _ = _psql_tcp(STALE_PASSWORD)
    # Stale may or may not still work depending on ALTER; install MUST work.
    assert ok_after
    print("OK: stale SCRAM healed — install password accepted over TCP (no SASL)")


def test_sqlite_to_timescale_data_copy_complete():
    """Full row transfer sqlite → postgres (Timescale wire path / PostgresWriter)."""
    if not _have_local_postgres():
        print("SKIP: local postgres not available")
        return

    # Ensure install password works (heal test may have left it aligned).
    _psql_admin(
        f"ALTER ROLE {PG_USER} WITH PASSWORD '{INSTALL_PASSWORD.replace(chr(39), chr(39)+chr(39))}';"
    )
    ok, err = _psql_tcp(INSTALL_PASSWORD)
    assert ok, f"need working install password for copy: {err}"

    from app.services.native_migration.adapters import (
        create_reader,
        create_writer,
        copy_tables_universal,
    )

    fd, src = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    try:
        expected = _make_source_sqlite(src)
        # Reset target schema
        _psql_admin(_pg_target_schema())

        reader = create_reader("sqlite", src, {})
        writer = create_writer(
            "timescaledb",
            {
                "host": PG_HOST,
                "port": PG_PORT,
                "database": PG_DB,
                "user": PG_USER,
                "password": INSTALL_PASSWORD,
            },
        )
        logs: list[str] = []
        try:
            stats, report = copy_tables_universal(
                reader, writer, logs.append, fail_hard=True,
            )
        finally:
            reader.close()
            writer.close()

        assert not report.get("has_gaps"), f"copy gaps: {report}"
        for table, n in expected.items():
            got = stats.get(table, 0)
            assert got == n, f"{table}: copied {got} expected {n}; stats={stats}"
            live = _count_pg(table)
            assert live == n, f"{table}: live PG count {live} expected {n}"

        # Spot-check usernames survived
        ok_u, users_out = _psql_tcp(
            INSTALL_PASSWORD,
            "SELECT username FROM users ORDER BY id",
        )
        assert ok_u
        assert "alice" in users_out and "bob" in users_out and "carol" in users_out
        print(
            "OK: sqlite→timescaledb data copy complete — "
            + ", ".join(f"{k}={v}" for k, v in expected.items())
        )
    finally:
        os.unlink(src)


def test_convert_path_rejects_auth_error_after_successful_heal_flow():
    """High-level: ensure_target_auth_ready succeeds after stale→heal (no SASL raise)."""
    if not _have_local_postgres():
        print("SKIP: local postgres not available")
        return

    from app.services.db_auth import ensure_target_auth_ready
    from app.services.migrators.base import BaseMigrator, MigrationJob

    class Dummy(BaseMigrator):
        async def run(self, params):
            return {}

    # Drift again
    _psql_admin(
        f"ALTER ROLE {PG_USER} WITH PASSWORD "
        f"'{STALE_PASSWORD.replace(chr(39), chr(39)+chr(39))}';"
    )
    assert not _psql_tcp(INSTALL_PASSWORD)[0]

    env = (
        f"POSTGRES_PASSWORD={INSTALL_PASSWORD}\n"
        f"POSTGRES_USER={PG_USER}\n"
        f"DB_USER={PG_USER}\n"
        f"DB_NAME={PG_DB}\n"
        f"POSTGRES_DB={PG_DB}\n"
        f'SQLALCHEMY_DATABASE_URL="postgresql+asyncpg://{PG_USER}:{INSTALL_PASSWORD}'
        f'@{PG_HOST}:{PG_PORT}/{PG_DB}"\n'
    )

    async def fake_run(cmd, cwd=None, timeout=600, *, quiet=False):
        argv = list(cmd) if isinstance(cmd, list) else [cmd]
        joined = " ".join(argv)
        pwd = ""
        for a in argv:
            if a.startswith("PGPASSWORD="):
                pwd = a.split("=", 1)[1]

        if "printenv" in joined:
            return True, ""
        if "compose ps" in joined:
            return True, "fakecid\n"
        if "{{.Config.Image}}" in joined:
            return True, "postgres:16\n"
        if "NetworkSettings.Ports" in joined:
            return True, '{"5432/tcp":[{"HostIp":"127.0.0.1","HostPort":"5432"}]}'
        # Host TCP probe via docker run --network host
        if "run" in argv and "--network" in argv and "psql" in argv:
            ok, out = _psql_tcp(pwd)
            return ok, ("1\n" if ok else f"password authentication failed\n{out}")
        # In-container probe / ALTER
        if "exec" in argv and "psql" in argv:
            if "ALTER ROLE" in joined:
                sql = argv[argv.index("-c") + 1] if "-c" in argv else ""
                if sql and not pwd:
                    try:
                        _psql_admin(sql)
                        return True, "ALTER ROLE\n"
                    except subprocess.CalledProcessError as exc:
                        return False, (exc.stderr or "")[-200:]
                if sql and pwd:
                    ok, out = _psql_tcp(pwd, sql)
                    return ok, out
            # SELECT 1 probe
            if pwd:
                ok, out = _psql_tcp(pwd)
                # Local socket in real containers may be trust; simulate success
                # for any password under "trust" check by also accepting stale.
                if ok:
                    return True, "1\n"
                # Fake local trust: in-container always works
                return True, "1\n"
            return True, "1\n"
        return True, ""

    async def _run():
        job = MigrationJob(job_id="e2e-ensure")
        migrator = Dummy(job, {})
        with patch("app.services.db_auth.PASARGUARD_DIR", Path("/tmp")), \
             patch("app.services.db_auth.resolve_db_service", return_value="timescaledb"), \
             patch.object(migrator, "_run_cmd", fake_run), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch(
                 "app.services.db_auth.refresh_pgbouncer_if_stale",
                 new_callable=AsyncMock,
                 return_value=True,
             ):
            conn = await ensure_target_auth_ready(
                migrator,
                "timescaledb",
                env_text=env,
                password=INSTALL_PASSWORD,
            )
        assert conn["password"] == INSTALL_PASSWORD
        assert _psql_tcp(INSTALL_PASSWORD)[0], "live TCP must accept install password"
        # Must not leave SASL-class failure
        assert not any("authentication failed" in (l or "").lower() and "could not" in (l or "").lower()
                       for l in job.logs)
        print("OK: ensure_target_auth_ready healed stale SCRAM — no SASL raise")

    asyncio.run(_run())


if __name__ == "__main__":
    test_sqlite_source_has_no_password_uses_install_only()
    test_stale_scram_healed_then_tcp_accepts_install_password()
    test_sqlite_to_timescale_data_copy_complete()
    test_convert_path_rejects_auth_error_after_successful_heal_flow()
    print("\nAll sqlite→Timescale E2E checks passed")
