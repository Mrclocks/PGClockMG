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
            if '-p"oldroot"' in text or "-poldroot" in text:
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


def test_mysql_probe_shell_string_via_base_migrator():
    """Regression: x-ui→MySQL died because BaseMigrator exec'd shell strings char-by-char."""
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
        seen = {"shell": 0}

        class FakeProc:
            returncode = 0

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

        async def fake_shell(cmd, **kwargs):
            seen["shell"] += 1
            assert isinstance(cmd, str)
            assert "docker compose exec" in cmd
            assert "mysql" in cmd or "mariadb" in cmd
            return FakeProc()

        async def fake_exec(*_a, **_k):
            raise AssertionError("probe must use create_subprocess_shell for shell strings")

        with patch("app.services.db_auth.PASARGUARD_DIR", Path("/opt/pasarguard")), \
             patch("app.services.db_auth.resolve_db_service", return_value="mysql"), \
             patch("asyncio.create_subprocess_shell", fake_shell), \
             patch("asyncio.create_subprocess_exec", fake_exec):
            conn = await resolve_live_admin_connection(migrator, "mysql", env_text=env)

        assert conn["password"] == "secret"
        assert conn["user"] == "root"
        assert seen["shell"] >= 1
        assert not any("$ c d" in line for line in job.logs)

    asyncio.run(_run())
    print("OK: mysql probe uses shell via BaseMigrator")


if __name__ == "__main__":
    test_postgres_password_candidates_order()
    test_postgres_admin_users()
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
    test_mysql_probe_shell_string_via_base_migrator()
    print("\nAll db_auth tests passed")
