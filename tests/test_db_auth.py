"""Tests for live DB credential resolution."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.db_auth import (
    migration_params_from_connection,
    mysql_password_candidates,
    postgres_password_candidates,
    postgres_admin_users,
    postgres_role_candidates,
    build_postgres_auth_attempts,
    summarize_pg_auth_errors,
    target_database_name,
)
from app.services.db_credentials import get_target_connection
from unittest.mock import MagicMock, patch


ENV_PG = """
DB_USER=pasarguard
DB_PASSWORD=app_secret
DB_NAME=pasarguard
POSTGRES_USER=postgres
POSTGRES_PASSWORD=super_secret
POSTGRES_DB=pasarguard
SQLALCHEMY_DATABASE_URL=postgresql+asyncpg://pasarguard:app_secret@127.0.0.1:6432/pasarguard
"""


def test_install_server_password_from_url_only():
    from app.services.db_auth import install_server_password

    env = (
        'SQLALCHEMY_DATABASE_URL="postgresql+asyncpg://pasarguard:urlOnly@127.0.0.1:6432/pasarguard"\n'
        "PASARGUARD_DB_ENGINE=timescaledb\n"
    )
    assert install_server_password(env, "timescaledb") == "urlOnly"
    assert install_server_password(
        "SQLALCHEMY_DATABASE_URL=sqlite+aiosqlite:////var/lib/pasarguard/db.sqlite3\n",
        "timescaledb",
    ) == ""
    print("OK: install_server_password from URL only")


def test_install_auth_env_for_sqlite_source_ignores_live_merge():
    from app.services.db_auth import install_auth_env_for_convert, install_server_password

    install = (
        "POSTGRES_PASSWORD=install-secret\n"
        "DB_PASSWORD=install-secret\n"
        'SQLALCHEMY_DATABASE_URL="postgresql+asyncpg://pasarguard:install-secret@127.0.0.1:6432/pasarguard"\n'
        "PASARGUARD_DB_ENGINE=timescaledb\n"
    )
    # Live after merge starts from sqlite backup — no POSTGRES_PASSWORD
    live = (
        'SQLALCHEMY_DATABASE_URL="sqlite+aiosqlite:////var/lib/pasarguard/db.sqlite3"\n'
        "PASARGUARD_DB_ENGINE=sqlite\n"
        "SUDO_PASSWORD=\n"
    )
    env = install_auth_env_for_convert(
        backup_db="sqlite",
        target_db="timescaledb",
        install_env_snapshot=install,
        live_env=live,
    )
    assert "install-secret" in env
    assert install_server_password(env, "timescaledb") == "install-secret"
    assert "sqlite+aiosqlite" not in env.lower() or "install-secret" in env
    # Must not pick empty live as auth source when install exists
    assert "POSTGRES_PASSWORD=install-secret" in env.replace(" ", "")
    print("OK: sqlite→timescale auth env uses install only")


def test_explain_auth_sqlite_to_timescale_mentions_no_backup_password():
    from app.services.pg_restore import explain_restore_error

    info = explain_restore_error(
        RuntimeError("SASL authentication failed"),
        "sqlite",
        "timescaledb",
    )
    joined = " ".join(info.get("causes_fa") or [])
    assert "sqlite" in joined.lower()
    assert "پسورد ندارد" in joined or "رمز نصب" in joined
    assert "globals.sql" not in joined
    assert "بکاپ=sqlite" in info["fa"]
    assert "4.6.14" in joined or "volumes-from" in joined.lower() or "live pg_hba" in joined.lower()
    print("OK: sqlite→timescale auth tips ignore backup password")


def test_pg_single_user_script_embeds_roles_and_braces_safe():
    from app.services.db_auth import _pg_single_user_alter_script

    script = _pg_single_user_alter_script(
        ["pasarguard", "postgres"],
        "sec{ret}'pass",
    )
    assert "ALTER ROLE \"pasarguard\"" in script
    assert "ALTER ROLE \"postgres\"" in script
    assert "postgres --single" in script
    assert "pgclockmg-heal: single-user ALTER done" in script
    # Password with braces must not break script generation
    assert "sec{ret}" in script or "sec{ret}" in script.replace("''", "'")
    print("OK: single-user script embeds roles / brace-safe")


def test_force_align_escalates_live_hba_then_single_user_then_hba():
    import asyncio
    from unittest.mock import AsyncMock, patch

    from app.services.db_auth import force_align_postgres_password
    from app.services.migrators.base import BaseMigrator, MigrationJob

    class Dummy(BaseMigrator):
        async def run(self, params):
            return {}

    async def _run():
        env = "POSTGRES_PASSWORD=live\nPOSTGRES_USER=pasarguard\nDB_NAME=pasarguard\n"

        # Path 1: live-HBA succeeds after trust fails → no single-user / stopped HBA
        job = MigrationJob(job_id="pg-live-hba")
        migrator = Dummy(job, {})
        with patch(
            "app.services.db_auth.recover_postgres_passwords_via_trust",
            new_callable=AsyncMock,
            return_value=False,
        ), patch(
            "app.services.db_auth.recover_postgres_passwords_via_live_hba",
            new_callable=AsyncMock,
            return_value=True,
        ) as live, patch(
            "app.services.db_auth.recover_postgres_passwords_via_single_user",
            new_callable=AsyncMock,
            return_value=False,
        ) as nuclear, patch(
            "app.services.db_auth.recover_postgres_passwords_via_hba_trust",
            new_callable=AsyncMock,
            return_value=False,
        ) as hba:
            ok = await force_align_postgres_password(
                migrator, "timescaledb", env, password="live",
            )
        assert ok is True
        assert live.await_count == 1
        assert nuclear.await_count == 0
        assert hba.await_count == 0
        assert any("live pg_hba" in line.lower() for line in job.logs)

        # Path 2: live-HBA fails → single-user succeeds
        job2 = MigrationJob(job_id="pg-nuclear")
        migrator2 = Dummy(job2, {})
        with patch(
            "app.services.db_auth.recover_postgres_passwords_via_trust",
            new_callable=AsyncMock,
            return_value=False,
        ), patch(
            "app.services.db_auth.recover_postgres_passwords_via_live_hba",
            new_callable=AsyncMock,
            return_value=False,
        ), patch(
            "app.services.db_auth.recover_postgres_passwords_via_single_user",
            new_callable=AsyncMock,
            return_value=True,
        ) as nuclear2, patch(
            "app.services.db_auth.recover_postgres_passwords_via_hba_trust",
            new_callable=AsyncMock,
            return_value=False,
        ) as hba2:
            ok2 = await force_align_postgres_password(
                migrator2, "timescaledb", env, password="live",
            )
        assert ok2 is True
        assert nuclear2.await_count == 1
        assert hba2.await_count == 0
        assert any("single-user" in line.lower() for line in job2.logs)

        # Path 3: all prior fail → stopped HBA-trust sidecar
        job3 = MigrationJob(job_id="pg-hba")
        migrator3 = Dummy(job3, {})
        with patch(
            "app.services.db_auth.recover_postgres_passwords_via_trust",
            new_callable=AsyncMock,
            return_value=False,
        ), patch(
            "app.services.db_auth.recover_postgres_passwords_via_live_hba",
            new_callable=AsyncMock,
            return_value=False,
        ), patch(
            "app.services.db_auth.recover_postgres_passwords_via_single_user",
            new_callable=AsyncMock,
            return_value=False,
        ), patch(
            "app.services.db_auth.recover_postgres_passwords_via_hba_trust",
            new_callable=AsyncMock,
            return_value=True,
        ) as hba3:
            ok3 = await force_align_postgres_password(
                migrator3, "timescaledb", env, password="live",
            )
        assert ok3 is True
        assert hba3.await_count == 1
        assert any("pg_hba" in line.lower() for line in job3.logs)

    asyncio.run(_run())
    print("OK: force_align escalates live-HBA → single-user → HBA")


def test_heal_sidecar_prefers_volumes_from_over_compose_run():
    """compose run can attach a throwaway VOLUME; volumes-from must win."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from app.services.db_auth import _start_heal_sidecar_volumes_from
    from app.services.migrators.base import BaseMigrator, MigrationJob

    class Dummy(BaseMigrator):
        async def run(self, params):
            return {}

    async def _run():
        job = MigrationJob(job_id="vf")
        migrator = Dummy(job, {})
        seen: list[list[str]] = []

        async def fake_run(cmd, cwd=None, timeout=600, *, quiet=False):
            argv = list(cmd) if isinstance(cmd, list) else [cmd]
            seen.append(argv)
            joined = " ".join(argv)
            if "compose ps" in joined:
                return True, "abc123deadbeef\n"
            if "{{.Config.Image}}" in joined:
                return True, "timescale/timescaledb-ha:pg16\n"
            if "docker" in argv and "run" in argv and "--volumes-from" in argv:
                return True, "sidecarcid\n"
            if "compose run" in joined:
                return True, "should-not-use\n"
            return True, ""

        with patch("app.services.db_auth.PASARGUARD_DIR", Path("/opt/pasarguard")), \
             patch.object(migrator, "_run_cmd", fake_run):
            ok, mode = await _start_heal_sidecar_volumes_from(
                migrator, service="timescaledb", heal_name="pasarguard-timescaledb-pwd-heal",
            )
        assert ok is True
        assert mode == "volumes-from"
        vf = [c for c in seen if "--volumes-from" in c]
        assert vf, "expected docker run --volumes-from"
        assert "abc123deadbeef" in vf[0]
        compose_runs = [
            c for c in seen
            if len(c) >= 3 and c[0] == "docker" and c[1] == "compose" and "run" in c
        ]
        assert not compose_runs, f"compose run should not run when volumes-from works: {compose_runs}"
        assert any("volumes-from=" in line for line in job.logs)

    asyncio.run(_run())
    print("OK: heal sidecar prefers volumes-from")


def test_pg_resolve_uses_nuclear_when_trust_alter_fails():
    """Trust ALTER fails → nuclear single-user → TCP OK."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from app.services.db_auth import resolve_live_admin_connection
    from app.services.migrators.base import BaseMigrator, MigrationJob

    class Dummy(BaseMigrator):
        async def run(self, params):
            return {}

    async def _run():
        job = MigrationJob(job_id="pg-resolve-nuclear")
        migrator = Dummy(job, {})
        env = (
            "POSTGRES_PASSWORD=install-pw\n"
            "POSTGRES_USER=pasarguard\n"
            "POSTGRES_DB=pasarguard\n"
        )
        state = {"aligned": False, "tcp_ok": 0}

        async def fake_run(cmd, cwd=None, timeout=600, *, quiet=False):
            argv = list(cmd) if isinstance(cmd, list) else [cmd]
            joined = " ".join(argv)
            pwd = ""
            for a in argv:
                if a.startswith("PGPASSWORD="):
                    pwd = a.split("=", 1)[1]
            if "printenv" in joined:
                return True, ""
            if "compose ps" in joined or "compose stop" in joined or "compose up" in joined:
                return True, "dbcid\n" if "ps" in joined else ""
            if "{{.Config.Image}}" in joined:
                return True, "timescale/timescaledb:latest-pg16\n"
            if "NetworkSettings.Ports" in joined:
                return True, '{"5432/tcp":[{"HostIp":"127.0.0.1","HostPort":"5432"}]}'
            if "run" in argv and "--network" in argv:
                if state["aligned"] and pwd == "install-pw":
                    state["tcp_ok"] += 1
                    return True, "1\n"
                return False, "password authentication failed"
            if "exec" in argv and "psql" in argv:
                if "ALTER ROLE" in joined:
                    return False, "ERROR: permission denied"
                return False, "password authentication failed"
            return True, ""

        async def fake_nuclear(*_a, **_k):
            state["aligned"] = True
            return True

        with patch("app.services.db_auth.PASARGUARD_DIR", Path("/opt/pasarguard")), \
             patch("app.services.db_auth.resolve_db_service", return_value="timescaledb"), \
             patch.object(migrator, "_run_cmd", fake_run), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch(
                 "app.services.db_auth.recover_postgres_passwords_via_trust",
                 new_callable=AsyncMock,
                 return_value=False,
             ), \
             patch(
                 "app.services.db_auth.recover_postgres_passwords_via_live_hba",
                 new_callable=AsyncMock,
                 return_value=False,
             ), \
             patch(
                 "app.services.db_auth.recover_postgres_passwords_via_single_user",
                 side_effect=fake_nuclear,
             ), \
             patch(
                 "app.services.db_auth.refresh_pgbouncer_if_stale",
                 new_callable=AsyncMock,
                 return_value=True,
             ):
            conn = await resolve_live_admin_connection(
                migrator, "timescaledb", env_text=env,
            )
        assert conn["password"] == "install-pw"
        assert state["tcp_ok"] >= 1
        assert any("auto-heal" in line.lower() or "nuclear" in line.lower()
                    or "single-user" in line.lower() for line in job.logs)

    asyncio.run(_run())
    print("OK: PG resolve uses nuclear when trust fails")


def test_postgres_password_candidates_order():
    cands = postgres_password_candidates(ENV_PG)
    assert cands[0] == "super_secret"
    assert "app_secret" in cands
    print("OK: postgres password candidate order")


def test_postgres_admin_users():
    users = postgres_admin_users(ENV_PG)
    assert users[0] == "pasarguard"
    assert "postgres" in users
    print("OK: postgres admin users")


def test_postgres_role_candidates_prefers_app_over_postgres():
    users = postgres_role_candidates(
        "DB_USER=pasarguard\nPOSTGRES_USER=pasarguard\n",
        "pasarguard",
        container_user="pasarguard",
    )
    assert users[0] == "pasarguard"
    assert users.count("pasarguard") == 1
    assert users[-1] == "postgres"
    print("OK: role candidates order")


def test_build_postgres_auth_attempts_trust_then_password():
    attempts = build_postgres_auth_attempts(
        "DB_USER=pasarguard\nPOSTGRES_PASSWORD=pw\n",
        preferred_user="pasarguard",
        preferred_password="pw",
        include_trust=True,
    )
    assert ("pasarguard", None) in attempts
    assert ("pasarguard", "pw") in attempts
    # Trust for preferred user comes before its password attempt
    assert attempts.index(("pasarguard", None)) < attempts.index(("pasarguard", "pw"))
    print("OK: auth attempts trust-before-password")


def test_summarize_pg_auth_errors_deprioritizes_missing_postgres():
    summary = summarize_pg_auth_errors(
        [
            ("postgres", 'FATAL: role "postgres" does not exist'),
            ("pasarguard", "ERROR: permission denied for function timescaledb_post_restore"),
        ]
    )
    assert "permission denied" in summary.lower()
    assert summary.lower().index("permission") < summary.lower().index("does not exist")
    print("OK: summarize deprioritizes missing postgres role")


def test_target_database_name_pg():
    assert target_database_name(ENV_PG, "timescaledb") == "pasarguard"
    print("OK: target database name")


def test_migration_params_from_connection():
    admin = {
        "user": "postgres",
        "password": "super_secret",
        "database": "pasarguard",
        "host": "127.0.0.1",
        "port": "5432",
        "db_type": "timescaledb",
    }
    p = migration_params_from_connection("sqlite", "timescaledb", admin)
    assert p["_resolved_target_conn"]["user"] == "postgres"
    assert p["_resolved_target_conn"]["password"] == "super_secret"
    assert p["target_db"] == "timescaledb"
    print("OK: migration params from connection")


def test_get_target_uses_resolved_conn():
    params = {
        "target_db": "timescaledb",
        "_resolved_target_conn": {
            "user": "postgres",
            "password": "live_probe_ok",
            "database": "pasarguard",
            "host": "127.0.0.1",
            "port": "5432",
            "db_type": "timescaledb",
        },
        "target_db_password": "wrong",
    }
    conn = get_target_connection(params)
    assert conn["password"] == "live_probe_ok"
    assert conn["user"] == "postgres"
    print("OK: resolved conn bypasses wizard password")


def test_get_target_wizard_password_when_manual():
    fake_env = MagicMock()
    fake_env.exists.return_value = True

    def fake_admin(target_db, password_override=None, env_text=None):
        return {
            "user": "root",
            "password": "fromenv",
            "database": "pasarguard",
            "host": "127.0.0.1",
            "port": "3306",
            "db_type": target_db,
        }

    params = {
        "target_db": "mysql",
        "target_db_user": "pasarguard",
        "target_db_name": "pasarguard",
        "target_db_password": "wizardpwd",
    }
    with patch("app.services.db_credentials.PASARGUARD_ENV", fake_env), patch(
        "app.services.env_migration.get_pasarguard_admin_connection",
        fake_admin,
    ):
        conn = get_target_connection(params)
    assert conn["password"] == "wizardpwd"
    print("OK: manual wizard password preserved")


def test_mysql_password_candidates():
    env = "MYSQL_ROOT_PASSWORD=rootpw\nDB_PASSWORD=apppw\n"
    c = mysql_password_candidates(env)
    assert c[0] == "rootpw"
    assert "apppw" in c
    print("OK: mysql password candidates")


def test_mysql_password_candidates_from_sqlalchemy_url():
    env = (
        'SQLALCHEMY_DATABASE_URL="mysql+asyncmy://pasarguard:urlsecret@127.0.0.1:3306/pasarguard"\n'
        "DB_PASSWORD=apppw\n"
    )
    c = mysql_password_candidates(env)
    assert "urlsecret" in c
    assert "apppw" in c
    print("OK: mysql password from SQLAlchemy URL")


def test_explain_auth_mariadb_target_from_timescale():
    from app.services.pg_restore import explain_restore_error

    info = explain_restore_error(
        RuntimeError("MySQL/MariaDB authentication failed — check MYSQL_ROOT_PASSWORD"),
        "timescaledb",
        "mariadb",
    )
    blob = "\n".join(info.get("causes_fa") or [])
    assert "MYSQL" in blob or "MariaDB" in blob or "mariadb" in blob.lower()
    assert "PgBouncer" not in blob
    assert "POSTGRES_PASSWORD" not in blob
    print("OK: timescale→mariadb auth tips are MySQL-aware")


def test_sync_mysql_roles_runs_alter_user_shell():
    import asyncio
    from unittest.mock import patch

    from app.services.db_auth import sync_mysql_roles_to_password
    from app.services.migrators.base import BaseMigrator, MigrationJob

    class Dummy(BaseMigrator):
        async def run(self, params):
            return {}

    async def _run():
        job = MigrationJob(job_id="sync1")
        migrator = Dummy(job, {})
        seen = []

        async def fake_run(self, cmd, cwd=None, timeout=600):
            seen.append(cmd if isinstance(cmd, str) else " ".join(cmd))
            return True, "ok"

        with patch("app.services.db_auth.resolve_db_service", return_value="mysql"), \
             patch("app.services.db_auth.PASARGUARD_DIR", Path("/opt/pasarguard")), \
             patch.object(Dummy, "_run_cmd", fake_run):
            ok = await sync_mysql_roles_to_password(
                migrator,
                "mysql",
                {"user": "root", "password": "rootpw"},
                app_user="pasarguard",
                password="rootpw",
                env_text="DB_USER=pasarguard\nMYSQL_ROOT_PASSWORD=rootpw\n",
            )
        assert ok is True
        assert seen
        assert any("ALTER USER" in s and "pasarguard" in s for s in seen)
        assert any("127.0.0.1" in s for s in seen)
        assert not any("skip-grant-tables" in s for s in seen)

    asyncio.run(_run())
    print("OK: sync_mysql_roles ALTER USER")


def test_build_mysql_role_password_sql_hosts_and_grants():
    from app.services.db_auth import build_mysql_role_password_sql

    sql = build_mysql_role_password_sql("s3cret", app_user="pasarguard", db_name="pasarguard")
    assert "CREATE USER IF NOT EXISTS 'pasarguard'@'127.0.0.1'" in sql
    assert "ALTER USER 'pasarguard'@'127.0.0.1' IDENTIFIED BY 's3cret'" in sql
    assert "ALTER USER 'root'@'%'" in sql
    assert "GRANT ALL PRIVILEGES ON *.* TO 'root'@'127.0.0.1' WITH GRANT OPTION" in sql
    assert "GRANT ALL PRIVILEGES ON *.* TO 'root'@'%'" in sql
    assert "GRANT ALL PRIVILEGES ON `pasarguard`.* TO 'pasarguard'@'%'" in sql
    assert "FLUSH PRIVILEGES;" in sql
    skip = build_mysql_role_password_sql(
        "x", app_user="u", include_flush_first=True,
    )
    assert skip.startswith("FLUSH PRIVILEGES;")
    print("OK: build_mysql_role_password_sql hosts/grants")


def test_mysql_sync_auth_candidates_merges_env_and_extras():
    from app.services.db_auth import mysql_sync_auth_candidates

    env = "MYSQL_ROOT_PASSWORD=rootpw\nDB_PASSWORD=apppw\n"
    c = mysql_sync_auth_candidates("extra", "rootpw", env_text=env)
    assert c[0] == "extra"
    assert "rootpw" in c
    assert "apppw" in c
    print("OK: mysql_sync_auth_candidates merge")


def test_sync_mysql_roles_tries_old_password_then_succeeds():
    """Install root password still works even when .env target password differs."""
    import asyncio
    from unittest.mock import patch

    from app.services.db_auth import sync_mysql_roles_to_password
    from app.services.migrators.base import BaseMigrator, MigrationJob

    class Dummy(BaseMigrator):
        async def run(self, params):
            return {}

    async def _run():
        job = MigrationJob(job_id="sync2")
        migrator = Dummy(job, {})
        seen = []

        async def fake_run(self, cmd, cwd=None, timeout=600):
            text = cmd if isinstance(cmd, str) else " ".join(cmd)
            seen.append(text)
            # Fail until we authenticate with the old/install password.
            if (
                '-p"oldroot"' in text
                or "-poldroot" in text
                or "MYSQL_PWD=oldroot" in text
            ):
                return True, "ok"
            if "skip-grant-tables" in text:
                raise AssertionError("skip-grant must not run when a candidate works")
            return False, "Access denied"

        with patch("app.services.db_auth.resolve_db_service", return_value="mysql"), \
             patch("app.services.db_auth.PASARGUARD_DIR", Path("/opt/pasarguard")), \
             patch.object(Dummy, "_run_cmd", fake_run):
            ok = await sync_mysql_roles_to_password(
                migrator,
                "mysql",
                {"user": "root", "password": "oldroot"},
                app_user="pasarguard",
                password="newroot",
                env_text="DB_USER=pasarguard\nMYSQL_ROOT_PASSWORD=newroot\nDB_PASSWORD=newroot\n",
            )
        assert ok is True
        assert any("oldroot" in s for s in seen)
        assert not any("skip-grant-tables" in s for s in seen)

    asyncio.run(_run())
    print("OK: sync tries install password before skip-grant")


def test_sync_mysql_roles_skip_grant_recovery_when_locked_out():
    import asyncio
    from unittest.mock import patch

    from app.services.db_auth import sync_mysql_roles_to_password
    from app.services.migrators.base import BaseMigrator, MigrationJob

    class Dummy(BaseMigrator):
        async def run(self, params):
            return {}

    async def _run():
        job = MigrationJob(job_id="sync3")
        migrator = Dummy(job, {})
        seen = []
        heal_ready = {"n": 0}

        async def immediate_sleep(*_a, **_k):
            return None

        async def fake_run(self, cmd, cwd=None, timeout=600):
            if isinstance(cmd, list):
                text = " ".join(cmd)
            else:
                text = cmd
            seen.append(text)

            # Post-recovery verify on the normal service.
            if "compose exec" in text and "MYSQL_PWD=newroot" in text and "SELECT 1" in text:
                return True, "1\n"

            # Normal exec attempts fail (locked out) — shell strings from sync.
            if "compose exec" in text and "skip-grant" not in text:
                return False, (
                    "ERROR 1045 (28000): Access denied for user 'root'@'localhost'"
                )

            if isinstance(cmd, list) and len(cmd) >= 3 and cmd[0] == "docker" and cmd[1] == "rm":
                return True, ""
            if isinstance(cmd, list) and cmd[:3] == ["docker", "compose", "stop"]:
                return True, ""
            if isinstance(cmd, list) and "run" in cmd and "--skip-grant-tables" in cmd:
                return True, "healcid"
            if isinstance(cmd, list) and cmd[:2] == ["docker", "exec"] and "SELECT 1" in text:
                heal_ready["n"] += 1
                return True, "1\n"
            if isinstance(cmd, list) and cmd[:2] == ["docker", "exec"] and "ALTER USER" in text:
                assert "FLUSH PRIVILEGES;" in text
                assert "127.0.0.1" in text
                return True, "ok"
            if isinstance(cmd, list) and cmd[:2] == ["docker", "stop"]:
                return True, ""
            if isinstance(cmd, list) and cmd[:3] == ["docker", "compose", "up"]:
                return True, ""
            return False, "no"

        with patch("app.services.db_auth.resolve_db_service", return_value="mysql"), \
             patch("app.services.db_auth.PASARGUARD_DIR", Path("/opt/pasarguard")), \
             patch("asyncio.sleep", immediate_sleep), \
             patch.object(Dummy, "_run_cmd", fake_run):
            ok = await sync_mysql_roles_to_password(
                migrator,
                "mysql",
                {"user": "root", "password": "wrong"},
                app_user="pasarguard",
                password="newroot",
                env_text="DB_USER=pasarguard\nMYSQL_ROOT_PASSWORD=newroot\n",
            )
        assert ok is True
        assert any("skip-grant-tables" in s for s in seen)
        assert any("compose stop" in s for s in seen)
        assert any("compose up" in s and "-d" in s and "mysql" in s for s in seen)
        assert heal_ready["n"] >= 1

    asyncio.run(_run())
    print("OK: skip-grant recovery when root locked out")


def test_pg_restore_sync_mysql_uses_candidates_then_recovery():
    import asyncio
    from unittest.mock import patch

    from app.services.migrators.base import MigrationJob
    from app.services import pg_restore

    async def _run():
        job = MigrationJob(job_id="rsync1")
        calls = []

        async def fake_run(job_arg, cmd, cwd=None, timeout=600):
            text = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
            calls.append(text)
            if "compose exec" in text and "MYSQL_PWD=oldinstall" in text and "ALTER USER" in text:
                return True, "ok"
            if "compose exec" in text:
                return False, "Access denied"
            return True, "ok"

        with patch.object(pg_restore, "_run", fake_run), \
             patch.object(pg_restore, "_read_current_env", return_value=(
                 "DB_USER=pasarguard\nMYSQL_ROOT_PASSWORD=newbak\nDB_PASSWORD=newbak\n"
             )), \
             patch.object(pg_restore, "PASARGUARD_DIR", Path("/opt/pasarguard")):
            ok = await pg_restore._sync_mysql_passwords(
                job,
                "mysql",
                "newbak",
                user="pasarguard",
                db_type="mysql",
                db_name="pasarguard",
                auth_passwords=["oldinstall"],
            )
        assert ok is True
        assert any("MYSQL_PWD=oldinstall" in c for c in calls)
        assert not any("skip-grant-tables" in c for c in calls)

    asyncio.run(_run())
    print("OK: pg_restore sync uses auth_passwords before recovery")


def test_mysql_probe_uses_argv_without_password_in_args():
    """The probe must run as argv (never a shell string) and keep the password out of argv."""
    import asyncio
    from unittest.mock import patch

    from app.services.db_auth import resolve_live_admin_connection
    from app.services.migrators.base import BaseMigrator, MigrationJob

    class Dummy(BaseMigrator):
        async def run(self, params):
            return {}

    async def _run():
        job = MigrationJob(job_id="probe1")
        migrator = Dummy(job, {})
        env = "MYSQL_ROOT_PASSWORD=secret\nMYSQL_DATABASE=pasarguard\n"
        seen = {"exec": 0}

        class FakeProc:
            returncode = 0
            pid = 4242

            def __init__(self):
                class Out:
                    async def readline(self_inner):
                        if not getattr(self_inner, "_sent", False):
                            self_inner._sent = True
                            return b"1\n"
                        return b""

                self.stdout = Out()

            async def wait(self):
                return 0

            def kill(self):
                pass

        async def fake_exec(*args, **kwargs):
            seen["exec"] += 1
            argv = [str(a) for a in args]
            assert argv[:4] == ["docker", "compose", "exec", "-T"], argv
            assert any(a.startswith("MYSQL_PWD=") for a in argv), argv
            # password must never ride along as a -p<secret> argument
            assert not any(a.startswith("-p") and len(a) > 2 for a in argv), argv
            return FakeProc()

        async def fake_shell(cmd, **kwargs):
            raise AssertionError("probe must not build shell strings")

        with patch("app.services.db_auth.PASARGUARD_DIR", Path("/opt/pasarguard")), \
             patch("app.services.db_auth.resolve_db_service", return_value="mysql"), \
             patch("asyncio.create_subprocess_shell", fake_shell), \
             patch("asyncio.create_subprocess_exec", fake_exec):
            conn = await resolve_live_admin_connection(migrator, "mysql", env_text=env)

        assert conn["password"] == "secret"
        assert conn["user"] == "root"
        assert seen["exec"] >= 1
        # the echoed command must not leak the password into the job log
        assert not any("secret" in line for line in job.logs), job.logs

    asyncio.run(_run())
    print("OK: mysql probe uses argv and hides the password")


def test_pg_resolve_trust_recovers_stale_password():
    """Local trust + TCP reject → ALTER ROLE via trust → TCP accepts .env password."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from app.services.db_auth import resolve_live_admin_connection
    from app.services.migrators.base import BaseMigrator, MigrationJob

    class Dummy(BaseMigrator):
        async def run(self, params):
            return {}

    async def _run():
        job = MigrationJob(job_id="pg-trust1")
        migrator = Dummy(job, {})
        env = (
            "POSTGRES_PASSWORD=stale-secret\n"
            "POSTGRES_USER=pasarguard\n"
            "POSTGRES_DB=pasarguard\n"
        )
        state = {"aligned": False, "alters": 0, "tcp_before": 0, "tcp_after": 0}

        async def fake_run(cmd, cwd=None, timeout=600, *, quiet=False):
            argv = list(cmd) if isinstance(cmd, list) else [cmd]
            joined = " ".join(argv)
            pwd = ""
            for a in argv:
                if a.startswith("PGPASSWORD="):
                    pwd = a.split("=", 1)[1]
            if "compose ps" in joined:
                return True, "dbcid\n"
            if "{{.Config.Image}}" in joined:
                return True, "postgres:16\n"
            if "NetworkSettings.Ports" in joined:
                return True, '{"5432/tcp":[{"HostIp":"127.0.0.1","HostPort":"5432"}]}'
            if "run" in argv and "--network" in argv:
                if state["aligned"] and pwd == "stale-secret":
                    state["tcp_after"] += 1
                    return True, "1\n"
                state["tcp_before"] += 1
                return False, "password authentication failed"
            if "exec" in argv and "psql" in argv:
                if "ALTER ROLE" in joined:
                    state["alters"] += 1
                    state["aligned"] = True
                    return True, "ALTER ROLE\n"
                return True, "1\n"
            return True, ""

        with patch("app.services.db_auth.PASARGUARD_DIR", Path("/opt/pasarguard")), \
             patch("app.services.db_auth.resolve_db_service", return_value="postgresql"), \
             patch.object(migrator, "_run_cmd", fake_run), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            conn = await resolve_live_admin_connection(
                migrator, "postgresql", env_text=env,
            )
        assert conn["password"] == "stale-secret"
        assert conn["user"] == "pasarguard"
        assert state["alters"] >= 1
        assert state["tcp_before"] >= 1
        assert state["tcp_after"] >= 1
        assert any("trust recovery" in line.lower() for line in job.logs)

    asyncio.run(_run())
    print("OK: PG resolve recovers stale password via trust ALTER")


def test_pg_resolve_trust_recovery_raises_when_alter_fails():
    import asyncio
    from unittest.mock import AsyncMock, patch

    from app.services.db_auth import resolve_live_admin_connection
    from app.services.migrators.base import BaseMigrator, MigrationJob

    class Dummy(BaseMigrator):
        async def run(self, params):
            return {}

    async def _run():
        job = MigrationJob(job_id="pg-trust-fail")
        migrator = Dummy(job, {})
        env = "POSTGRES_PASSWORD=stale-secret\nPOSTGRES_DB=pasarguard\n"

        async def fake_run(cmd, cwd=None, timeout=600, *, quiet=False):
            argv = list(cmd) if isinstance(cmd, list) else [cmd]
            joined = " ".join(argv)
            if "compose ps" in joined:
                return True, "dbcid\n"
            if "{{.Config.Image}}" in joined:
                return True, "postgres:16\n"
            if "NetworkSettings.Ports" in joined:
                return True, '{"5432/tcp":[{"HostIp":"127.0.0.1","HostPort":"5432"}]}'
            if "run" in argv and "--network" in argv:
                return False, "password authentication failed"
            if "exec" in argv and "psql" in argv:
                if "ALTER ROLE" in joined:
                    return False, "ERROR: permission denied to alter role"
                return True, "1\n"
            return True, ""

        with patch("app.services.db_auth.PASARGUARD_DIR", Path("/opt/pasarguard")), \
             patch("app.services.db_auth.resolve_db_service", return_value="postgresql"), \
             patch.object(migrator, "_run_cmd", fake_run), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            with __import__("pytest").raises(
                RuntimeError, match="password auto-heal could not|trust recovery could not",
            ):
                await resolve_live_admin_connection(migrator, "postgresql", env_text=env)

    asyncio.run(_run())
    print("OK: PG resolve still raises when trust ALTER fails")


def test_pg_resolve_accepts_password_verified_over_tcp():
    import asyncio
    from unittest.mock import patch

    from app.services.db_auth import resolve_live_admin_connection
    from app.services.migrators.base import BaseMigrator, MigrationJob

    class Dummy(BaseMigrator):
        async def run(self, params):
            return {}

    async def _run():
        job = MigrationJob(job_id="pg-trust2")
        migrator = Dummy(job, {})
        env = "POSTGRES_PASSWORD=real-secret\nPOSTGRES_DB=pasarguard\n"

        async def fake_run(cmd, cwd=None, timeout=600, *, quiet=False):
            argv = list(cmd) if isinstance(cmd, list) else [cmd]
            joined = " ".join(argv)
            pwd = ""
            for a in argv:
                if a.startswith("PGPASSWORD="):
                    pwd = a.split("=", 1)[1]
            if "compose ps" in joined:
                return True, "dbcid\n"
            if "{{.Config.Image}}" in joined:
                return True, "timescale/timescaledb:latest-pg16\n"
            if "NetworkSettings.Ports" in joined:
                return True, '{"5432/tcp":[{"HostIp":"0.0.0.0","HostPort":"5432"}]}'
            if "run" in argv and "--network" in argv:
                if pwd == "real-secret":
                    return True, "1\n"
                return False, "password authentication failed"
            if "exec" in argv and "psql" in argv:
                return True, "1\n"  # trust
            return True, ""

        with patch("app.services.db_auth.PASARGUARD_DIR", Path("/opt/pasarguard")), \
             patch("app.services.db_auth.resolve_db_service", return_value="timescaledb"), \
             patch.object(migrator, "_run_cmd", fake_run):
            conn = await resolve_live_admin_connection(
                migrator, "timescaledb", env_text=env,
            )
        assert conn["password"] == "real-secret"
        assert conn["port"] == "5432"
        assert conn["host"] == "127.0.0.1"

    asyncio.run(_run())
    print("OK: PG resolve accepts TCP-verified password under trust")


def test_mysql_resolve_skip_grant_recovers_stale_root():
    """Every .env root candidate fails → skip-grant → probe succeeds."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from app.services.db_auth import resolve_live_admin_connection
    from app.services.migrators.base import BaseMigrator, MigrationJob

    class Dummy(BaseMigrator):
        async def run(self, params):
            return {}

    async def _run():
        job = MigrationJob(job_id="mysql-skip1")
        migrator = Dummy(job, {})
        env = "MYSQL_ROOT_PASSWORD=env-secret\nDB_USER=pasarguard\nDB_NAME=pasarguard\n"
        state = {"recovered": False, "probes": 0}

        async def fake_probe(migrator_, service, user, password, database):
            state["probes"] += 1
            return state["recovered"] and password == "env-secret"

        async def fake_recover(*_a, **_k):
            state["recovered"] = True
            return True

        with patch("app.services.db_auth.PASARGUARD_DIR", Path("/opt/pasarguard")), \
             patch("app.services.db_auth.resolve_db_service", return_value="mysql"), \
             patch("app.services.db_auth._probe_mysql", side_effect=fake_probe), \
             patch(
                 "app.services.db_auth.recover_mysql_passwords_via_skip_grants",
                 side_effect=fake_recover,
             ), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            conn = await resolve_live_admin_connection(
                migrator, "mysql", env_text=env,
            )
        assert conn["password"] == "env-secret"
        assert state["recovered"] is True
        assert state["probes"] >= 2
        assert any("skip-grant" in line.lower() for line in job.logs)

    asyncio.run(_run())
    print("OK: MySQL resolve recovers via skip-grant")


def test_sync_postgres_falls_back_to_trust_alter():
    """Password ALTER fails → trust ALTER succeeds → force PgBouncer refresh."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from app.services.db_auth import sync_postgres_roles_to_app_password
    from app.services.migrators.base import BaseMigrator, MigrationJob

    class Dummy(BaseMigrator):
        async def run(self, params):
            return {}

    async def _run():
        job = MigrationJob(job_id="pg-sync-trust")
        migrator = Dummy(job, {})
        env = (
            "POSTGRES_PASSWORD=live\n"
            "POSTGRES_USER=pasarguard\n"
            "DB_USER=pasarguard\n"
            "DB_NAME=pasarguard\n"
            'SQLALCHEMY_DATABASE_URL="postgresql+asyncpg://pasarguard:live@127.0.0.1:6432/pasarguard"\n'
        )
        state = {"trust_alters": 0, "forced": False}

        async def fake_run(cmd, cwd=None, timeout=600, *, quiet=False):
            argv = list(cmd) if isinstance(cmd, list) else [cmd]
            joined = " ".join(argv)
            if "ALTER ROLE" in joined and "PGPASSWORD=" in joined:
                return False, "password authentication failed"
            if "ALTER ROLE" in joined:
                state["trust_alters"] += 1
                return True, "ALTER ROLE\n"
            return True, ""

        async def fake_refresh(*_a, **kwargs):
            if kwargs.get("force"):
                state["forced"] = True
            return True

        with patch("app.services.db_auth.PASARGUARD_DIR", Path("/opt/pasarguard")), \
             patch("app.services.db_auth.resolve_db_service", return_value="timescaledb"), \
             patch.object(migrator, "_run_cmd", fake_run), \
             patch(
                 "app.services.db_auth.refresh_pgbouncer_if_stale",
                 side_effect=fake_refresh,
             ), \
             patch("app.services.pasarguard_ops.compose_file_prefix", return_value=()):
            ok = await sync_postgres_roles_to_app_password(
                migrator,
                "timescaledb",
                {"user": "pasarguard", "password": "wrong", "database": "pasarguard"},
                env_text=env,
                password="live",
            )
        assert ok is True
        assert state["trust_alters"] >= 1
        assert state["forced"] is True

    asyncio.run(_run())
    print("OK: sync_postgres falls back to trust ALTER")


def test_pg_resolve_recovers_when_local_probes_fail():
    """Local probes never succeed → still attempt trust recovery (root gap fix)."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from app.services.db_auth import resolve_live_admin_connection
    from app.services.migrators.base import BaseMigrator, MigrationJob

    class Dummy(BaseMigrator):
        async def run(self, params):
            return {}

    async def _run():
        job = MigrationJob(job_id="pg-local-fail")
        migrator = Dummy(job, {})
        env = (
            "POSTGRES_PASSWORD=live-secret\n"
            "POSTGRES_USER=pasarguard\n"
            "POSTGRES_DB=pasarguard\n"
        )
        state = {"aligned": False, "tcp_ok": 0}

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
                return True, "dbcid\n"
            if "{{.Config.Image}}" in joined:
                return True, "timescale/timescaledb:latest-pg16\n"
            if "NetworkSettings.Ports" in joined:
                return True, '{"5432/tcp":[{"HostIp":"127.0.0.1","HostPort":"5432"}]}'
            if "run" in argv and "--network" in argv:
                if state["aligned"] and pwd == "live-secret":
                    state["tcp_ok"] += 1
                    return True, "1\n"
                return False, "password authentication failed"
            if "exec" in argv and "psql" in argv:
                if "ALTER ROLE" in joined:
                    state["aligned"] = True
                    return True, "ALTER ROLE\n"
                # Local probes always fail (simulates scram-local + wrong secret)
                return False, "password authentication failed"
            return True, ""

        with patch("app.services.db_auth.PASARGUARD_DIR", Path("/opt/pasarguard")), \
             patch("app.services.db_auth.resolve_db_service", return_value="timescaledb"), \
             patch.object(migrator, "_run_cmd", fake_run), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch(
                 "app.services.db_auth.refresh_pgbouncer_if_stale",
                 new_callable=AsyncMock,
                 return_value=True,
             ):
            conn = await resolve_live_admin_connection(
                migrator, "timescaledb", env_text=env,
            )
        assert conn["password"] == "live-secret"
        assert state["aligned"] is True
        assert state["tcp_ok"] >= 1
        assert any(
            any(s in line.lower() for s in (
                "trust password recovery",
                "password auto-heal",
                "auto-aligning",
                "single-user",
            ))
            for line in job.logs
        )

    asyncio.run(_run())
    print("OK: PG resolve recovers when local probes fail")


def test_pg_resolve_tries_app_db_when_postgres_db_missing():
    """Probe pasarguard DB when the default ``postgres`` database is absent."""
    import asyncio
    from unittest.mock import patch

    from app.services.db_auth import resolve_live_admin_connection
    from app.services.migrators.base import BaseMigrator, MigrationJob

    class Dummy(BaseMigrator):
        async def run(self, params):
            return {}

    async def _run():
        job = MigrationJob(job_id="pg-app-db")
        migrator = Dummy(job, {})
        env = (
            "POSTGRES_PASSWORD=ok\n"
            "POSTGRES_USER=pasarguard\n"
            "POSTGRES_DB=pasarguard\n"
            "DB_NAME=pasarguard\n"
        )

        async def fake_run(cmd, cwd=None, timeout=600, *, quiet=False):
            argv = list(cmd) if isinstance(cmd, list) else [cmd]
            joined = " ".join(argv)
            if "printenv" in joined:
                return True, ""
            if "exec" in argv and "psql" in argv and "-d" in argv:
                # Find -d argument
                try:
                    di = argv.index("-d")
                    db = argv[di + 1]
                except (ValueError, IndexError):
                    db = ""
                if db == "postgres":
                    return False, 'FATAL: database "postgres" does not exist'
                if db == "pasarguard" and "PGPASSWORD=ok" in joined:
                    return True, "1\n"
                return False, "password authentication failed"
            return True, ""

        with patch("app.services.db_auth.PASARGUARD_DIR", Path("/opt/pasarguard")), \
             patch("app.services.db_auth.resolve_db_service", return_value="timescaledb"), \
             patch.object(migrator, "_run_cmd", fake_run), \
             patch(
                 "app.services.db_auth._pg_in_container_is_trust",
                 return_value=False,
             ):
            conn = await resolve_live_admin_connection(
                migrator, "timescaledb", env_text=env,
            )
        assert conn["password"] == "ok"
        assert conn["user"] == "pasarguard"
        assert any("pasarguard" in line for line in job.logs)

    asyncio.run(_run())
    print("OK: PG resolve tries app DB when postgres missing")


def test_pg_resolve_uses_container_init_password():
    """Empty .env password list → recover using container POSTGRES_PASSWORD."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from app.services.db_auth import resolve_live_admin_connection
    from app.services.migrators.base import BaseMigrator, MigrationJob

    class Dummy(BaseMigrator):
        async def run(self, params):
            return {}

    async def _run():
        job = MigrationJob(job_id="pg-ctr-pwd")
        migrator = Dummy(job, {})
        # No password keys in .env — only container init env has the secret
        env = "POSTGRES_USER=pasarguard\nPOSTGRES_DB=pasarguard\nDB_NAME=pasarguard\n"
        state = {"aligned": False}

        async def fake_run(cmd, cwd=None, timeout=600, *, quiet=False):
            argv = list(cmd) if isinstance(cmd, list) else [cmd]
            joined = " ".join(argv)
            pwd = ""
            for a in argv:
                if a.startswith("PGPASSWORD="):
                    pwd = a.split("=", 1)[1]
            if "printenv" in joined and "POSTGRES_PASSWORD" in joined:
                return True, "container-secret\n"
            if "printenv" in joined and "POSTGRES_USER" in joined:
                return True, "pasarguard\n"
            if "printenv" in joined:
                return True, ""
            if "compose ps" in joined:
                return True, "dbcid\n"
            if "{{.Config.Image}}" in joined:
                return True, "postgres:16\n"
            if "NetworkSettings.Ports" in joined:
                return True, '{"5432/tcp":[{"HostIp":"0.0.0.0","HostPort":"5432"}]}'
            if "run" in argv and "--network" in argv:
                if state["aligned"] and pwd == "container-secret":
                    return True, "1\n"
                if pwd == "container-secret" and not state["aligned"]:
                    # Before recovery TCP rejects (SCRAM drifted from init)
                    return False, "password authentication failed"
                return False, "password authentication failed"
            if "exec" in argv and "psql" in argv:
                if "ALTER ROLE" in joined:
                    state["aligned"] = True
                    return True, "ALTER ROLE\n"
                if pwd == "container-secret":
                    return True, "1\n"
                return False, "password authentication failed"
            return True, ""

        with patch("app.services.db_auth.PASARGUARD_DIR", Path("/opt/pasarguard")), \
             patch("app.services.db_auth.resolve_db_service", return_value="postgresql"), \
             patch.object(migrator, "_run_cmd", fake_run), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch(
                 "app.services.db_auth.refresh_pgbouncer_if_stale",
                 new_callable=AsyncMock,
                 return_value=True,
             ):
            conn = await resolve_live_admin_connection(
                migrator, "postgresql", env_text=env,
            )
        assert conn["password"] == "container-secret"

    asyncio.run(_run())
    print("OK: PG resolve uses container init password")


def test_ensure_target_auth_ready_syncs_mysql_and_pg():
    import asyncio
    from unittest.mock import AsyncMock, patch

    from app.services.db_auth import ensure_target_auth_ready
    from app.services.migrators.base import BaseMigrator, MigrationJob

    class Dummy(BaseMigrator):
        async def run(self, params):
            return {}

    async def _run():
        job = MigrationJob(job_id="ensure-1")
        migrator = Dummy(job, {})
        admin = {
            "db_type": "mysql",
            "user": "root",
            "password": "secret",
            "database": "pasarguard",
            "host": "127.0.0.1",
            "port": "3306",
        }
        sync_mysql = AsyncMock(return_value=True)
        sync_pg = AsyncMock(return_value=True)
        refresh = AsyncMock(return_value=True)
        with patch(
            "app.services.db_auth.resolve_live_admin_connection",
            new_callable=AsyncMock,
            return_value=admin,
        ), patch(
            "app.services.db_auth.sync_mysql_roles_to_password", sync_mysql,
        ), patch(
            "app.services.db_auth.sync_postgres_roles_to_app_password", sync_pg,
        ), patch(
            "app.services.db_auth.refresh_pgbouncer_if_stale", refresh,
        ):
            out = await ensure_target_auth_ready(
                migrator, "mysql", env_text="MYSQL_ROOT_PASSWORD=secret\n",
                password="secret",
            )
            assert out["password"] == "secret"
            assert sync_mysql.await_count == 1
            assert sync_pg.await_count == 0

            pg_admin = dict(admin, db_type="timescaledb", port="5432")
            with patch(
                "app.services.db_auth.resolve_live_admin_connection",
                new_callable=AsyncMock,
                return_value=pg_admin,
            ):
                await ensure_target_auth_ready(
                    migrator, "timescaledb",
                    env_text="POSTGRES_PASSWORD=secret\n",
                    password="secret",
                )
            assert sync_pg.await_count == 1
            assert refresh.await_count == 1
            assert refresh.await_args.kwargs.get("force") is True

    asyncio.run(_run())
    print("OK: ensure_target_auth_ready syncs mysql and pg")


def test_parse_published_port_prefers_loopback():
    from app.services.db_auth import _parse_published_port

    # 0.0.0.0 is normalized to loopback and returned immediately
    host, port = _parse_published_port(
        '{"5432/tcp":[{"HostIp":"0.0.0.0","HostPort":"5432"}]}'
    )
    assert (host, port) == ("127.0.0.1", "5432")

    # Explicit 127.0.0.1 wins over a non-loopback fallback later in the list
    host, port = _parse_published_port(
        '{"5432/tcp":[{"HostIp":"10.0.0.5","HostPort":"15432"},'
        '{"HostIp":"127.0.0.1","HostPort":"5432"}]}'
    )
    assert (host, port) == ("127.0.0.1", "5432")
    print("OK: published port prefers loopback")


if __name__ == "__main__":
    test_pg_single_user_script_embeds_roles_and_braces_safe()
    test_force_align_escalates_live_hba_then_single_user_then_hba()
    test_heal_sidecar_prefers_volumes_from_over_compose_run()
    test_pg_resolve_uses_nuclear_when_trust_alter_fails()
    test_install_server_password_from_url_only()
    test_install_auth_env_for_sqlite_source_ignores_live_merge()
    test_explain_auth_sqlite_to_timescale_mentions_no_backup_password()
    test_postgres_password_candidates_order()
    test_postgres_admin_users()
    test_postgres_role_candidates_prefers_app_over_postgres()
    test_build_postgres_auth_attempts_trust_then_password()
    test_summarize_pg_auth_errors_deprioritizes_missing_postgres()
    test_target_database_name_pg()
    test_migration_params_from_connection()
    test_get_target_uses_resolved_conn()
    test_get_target_wizard_password_when_manual()
    test_mysql_password_candidates()
    test_mysql_password_candidates_from_sqlalchemy_url()
    test_explain_auth_mariadb_target_from_timescale()
    test_build_mysql_role_password_sql_hosts_and_grants()
    test_mysql_sync_auth_candidates_merges_env_and_extras()
    test_sync_mysql_roles_runs_alter_user_shell()
    test_sync_mysql_roles_tries_old_password_then_succeeds()
    test_sync_mysql_roles_skip_grant_recovery_when_locked_out()
    test_pg_restore_sync_mysql_uses_candidates_then_recovery()
    test_mysql_probe_uses_argv_without_password_in_args()
    test_pg_resolve_trust_recovers_stale_password()
    test_pg_resolve_trust_recovery_raises_when_alter_fails()
    test_pg_resolve_accepts_password_verified_over_tcp()
    test_mysql_resolve_skip_grant_recovers_stale_root()
    test_sync_postgres_falls_back_to_trust_alter()
    test_pg_resolve_recovers_when_local_probes_fail()
    test_pg_resolve_tries_app_db_when_postgres_db_missing()
    test_pg_resolve_uses_container_init_password()
    test_ensure_target_auth_ready_syncs_mysql_and_pg()
    test_parse_published_port_prefers_loopback()
    print("\nAll db_auth tests passed")
