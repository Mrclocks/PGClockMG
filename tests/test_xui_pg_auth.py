"""x-ui → PostgreSQL auth repair: PgBouncer credential drift and panel retry."""

import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.migrators.base import MigrationJob
from app.services.migrators.xui import (
    PG_FINGERPRINT_SQL,
    XUI_AUTH_HEAL_ENV,
    XuiMigrator,
    containers_publishing_port,
    describe_password_source,
    logs_show_pg_auth_failure,
    parse_container_env,
    parse_pg_auth_context,
    parse_published_port,
    password_storage_mismatch,
    pg_fingerprint_value,
    pg_endpoint_candidates,
    pg_probe_result,
    pgbouncer_env_mismatch,
    required_password_encryption,
    xui_auth_heal_enabled,
)

PANEL_AUTH_TRACEBACK = """
  File "/code/.venv/lib/python3.14/site-packages/asyncpg/connect_utils.py", line 1102
    await connected
asyncpg.exceptions.InvalidPasswordError: password authentication failed for user "pasarguard"
"""


def _pgbouncer_env(password: str = "appsecret") -> str:
    return (
        "PATH=/usr/local/sbin:/usr/local/bin\n"
        "DB_HOST=timescaledb\n"
        "DB_PORT=5432\n"
        "DB_USER=pasarguard\n"
        f"DB_PASSWORD={password}\n"
        "DB_NAME=pasarguard\n"
        "AUTH_TYPE=scram-sha-256\n"
    )


def test_parse_container_env_keeps_last_value():
    parsed = parse_container_env(
        "DB_USER=pasarguard\nnoise line\nDB_PASSWORD=first\nDB_PASSWORD=second\n"
    )
    assert parsed["DB_USER"] == "pasarguard"
    assert parsed["DB_PASSWORD"] == "second"
    assert "noise line" not in parsed


def test_parse_container_env_keeps_equals_in_value():
    parsed = parse_container_env("DB_PASSWORD=a=b=c\n")
    assert parsed["DB_PASSWORD"] == "a=b=c"


def test_pgbouncer_matching_credentials_report_no_drift():
    env = parse_container_env(_pgbouncer_env())
    assert pgbouncer_env_mismatch(
        env, user="pasarguard", password="appsecret", database="pasarguard"
    ) == []


def test_pgbouncer_stale_password_is_detected():
    env = parse_container_env(_pgbouncer_env("old-secret"))
    stale = pgbouncer_env_mismatch(
        env, user="pasarguard", password="new-secret", database="pasarguard"
    )
    assert stale == ["DB_PASSWORD"]


def test_pgbouncer_unknown_or_empty_keys_are_ignored():
    # Custom images may not define DB_NAME; empty expectations prove nothing.
    env = parse_container_env("DB_USER=pasarguard\nDB_PASSWORD=appsecret\n")
    assert pgbouncer_env_mismatch(
        env, user="pasarguard", password="appsecret", database="pasarguard"
    ) == []
    assert pgbouncer_env_mismatch(
        env, user="", password="", database=""
    ) == []


def test_logs_show_pg_auth_failure():
    assert logs_show_pg_auth_failure(PANEL_AUTH_TRACEBACK)
    assert logs_show_pg_auth_failure("SASL authentication failed")
    assert not logs_show_pg_auth_failure("INFO: Application startup complete")
    assert not logs_show_pg_auth_failure("Can't locate revision identified by 'abc'")


def test_auth_heal_kill_switch():
    with patch.dict("os.environ", {}, clear=False):
        import os

        os.environ.pop(XUI_AUTH_HEAL_ENV, None)
        assert xui_auth_heal_enabled() is True
        for off in ("0", "false", "no", "off", "OFF"):
            os.environ[XUI_AUTH_HEAL_ENV] = off
            assert xui_auth_heal_enabled() is False
        os.environ[XUI_AUTH_HEAL_ENV] = "1"
        assert xui_auth_heal_enabled() is True
        os.environ.pop(XUI_AUTH_HEAL_ENV, None)


class _RecordingMigrator(XuiMigrator):
    """XuiMigrator with docker calls replaced by scripted responses."""

    def __init__(self, job, params, *, container_password="appsecret", config_ok=True):
        super().__init__(job, params)
        self.commands: list[list[str]] = []
        self.container_password = container_password
        self.config_ok = config_ok

    async def _run_cmd(self, cmd, cwd=None, timeout=600, *, quiet=False):
        argv = list(cmd) if isinstance(cmd, list) else [cmd]
        self.commands.append(argv)
        joined = " ".join(argv)
        if "ps" in argv and "-q" in argv:
            return True, "pgbouncer-container-id\n"
        if "inspect" in joined:
            return True, _pgbouncer_env(self.container_password)
        if "config" in argv and "-q" in argv:
            return self.config_ok, "" if self.config_ok else "invalid compose"
        return True, ""

    def ran(self, *needles: str) -> bool:
        return any(
            all(n in " ".join(cmd) for n in needles) for cmd in self.commands
        )


def _migrator(**kwargs) -> _RecordingMigrator:
    job = MigrationJob(job_id="xuiauth")
    return _RecordingMigrator(job, {"target_db": "timescaledb"}, **kwargs)


async def _no_sleep(_seconds):
    return None


def _refresh(migrator, password="appsecret"):
    with patch("app.services.env_migration._compose_has_pgbouncer", lambda: True), \
         patch("app.services.migrators.xui.asyncio.sleep", _no_sleep):
        return asyncio.run(
            migrator._refresh_pgbouncer_credentials(
                "timescaledb",
                user="pasarguard",
                password=password,
                database="pasarguard",
            )
        )


def test_refresh_skips_when_pgbouncer_already_matches():
    migrator = _migrator()
    assert _refresh(migrator, password="appsecret") is False
    assert not migrator.ran("up", "-d", "pgbouncer")
    assert not migrator.ran("restart", "pgbouncer")


def test_refresh_recreates_when_credentials_are_stale():
    migrator = _migrator(container_password="old-secret")
    assert _refresh(migrator, password="new-secret") is True
    assert migrator.ran("up", "-d", "--no-deps", "--force-recreate", "pgbouncer")


def test_refresh_falls_back_to_restart_on_invalid_compose():
    migrator = _migrator(container_password="old-secret", config_ok=False)
    assert _refresh(migrator, password="new-secret") is False
    assert migrator.ran("restart", "pgbouncer")
    assert not migrator.ran("up", "-d", "--no-deps", "--force-recreate", "pgbouncer")


def test_refresh_disabled_by_kill_switch():
    import os

    migrator = _migrator(container_password="old-secret")
    os.environ[XUI_AUTH_HEAL_ENV] = "0"
    try:
        assert _refresh(migrator, password="new-secret") is False
    finally:
        os.environ.pop(XUI_AUTH_HEAL_ENV, None)
    assert migrator.commands == []


def test_refresh_noop_for_mysql_target():
    migrator = _migrator(container_password="old-secret")
    with patch("app.services.env_migration._compose_has_pgbouncer", lambda: True):
        result = asyncio.run(
            migrator._refresh_pgbouncer_credentials(
                "mariadb", user="pasarguard", password="x", database="pasarguard",
            )
        )
    assert result is False
    assert migrator.commands == []


def test_start_panel_does_not_repair_when_panel_is_healthy():
    calls = {"start": 0, "repair": 0}

    async def _ok_start(migrator, **kwargs):
        calls["start"] += 1

    async def _repair(self, target_db):
        calls["repair"] += 1
        return True

    async def _run():
        migrator = _migrator()
        with patch("app.services.migrators.xui.safe_start_pasarguard", _ok_start), \
             patch.object(XuiMigrator, "_repair_pg_panel_auth", _repair):
            await migrator._start_panel("timescaledb")

    asyncio.run(_run())
    assert calls == {"start": 1, "repair": 0}


def test_start_panel_repairs_and_retries_on_auth_failure():
    calls = {"start": 0, "repair": 0}

    async def _flaky_start(migrator, **kwargs):
        calls["start"] += 1
        if calls["start"] == 1:
            raise RuntimeError("PasarGuard failed to start — see container logs.")

    async def _repair(self, target_db):
        calls["repair"] += 1
        return True

    async def _logs(migrator, tail=150, **kwargs):
        return PANEL_AUTH_TRACEBACK

    async def _run():
        migrator = _migrator()
        with patch("app.services.migrators.xui.safe_start_pasarguard", _flaky_start), \
             patch("app.services.pasarguard_ops.fetch_pasarguard_logs", _logs), \
             patch.object(XuiMigrator, "_repair_pg_panel_auth", _repair):
            await migrator._start_panel("timescaledb")

    asyncio.run(_run())
    assert calls == {"start": 2, "repair": 1}


def test_start_panel_reraises_when_failure_is_not_auth():
    calls = {"start": 0, "repair": 0}

    async def _failing_start(migrator, **kwargs):
        calls["start"] += 1
        raise RuntimeError("Can't locate revision identified by 'abc'")

    async def _repair(self, target_db):
        calls["repair"] += 1
        return True

    async def _logs(migrator, tail=150, **kwargs):
        return "alembic.util.exc.CommandError: Can't locate revision identified by 'abc'"

    async def _run():
        migrator = _migrator()
        with patch("app.services.migrators.xui.safe_start_pasarguard", _failing_start), \
             patch("app.services.pasarguard_ops.fetch_pasarguard_logs", _logs), \
             patch.object(XuiMigrator, "_repair_pg_panel_auth", _repair):
            await migrator._start_panel("timescaledb")

    try:
        asyncio.run(_run())
    except RuntimeError as e:
        assert "locate revision" in str(e)
    else:
        raise AssertionError("non-auth startup failure must propagate")
    assert calls == {"start": 1, "repair": 0}


def test_start_panel_keeps_original_error_when_repair_finds_nothing():
    async def _failing_start(migrator, **kwargs):
        raise RuntimeError("PasarGuard failed to start — see container logs.")

    async def _repair(self, target_db):
        return False

    async def _logs(migrator, tail=150, **kwargs):
        return PANEL_AUTH_TRACEBACK

    async def _run():
        migrator = _migrator()
        with patch("app.services.migrators.xui.safe_start_pasarguard", _failing_start), \
             patch("app.services.pasarguard_ops.fetch_pasarguard_logs", _logs), \
             patch.object(XuiMigrator, "_repair_pg_panel_auth", _repair):
            await migrator._start_panel("timescaledb")

    try:
        asyncio.run(_run())
    except RuntimeError as e:
        assert "failed to start" in str(e)
    else:
        raise AssertionError("startup failure must propagate when repair changed nothing")


def test_repair_uses_panel_url_password_for_roles():
    seen = {}

    async def _fake_resolve(migrator, db_type, env_text=None):
        return {"user": "pasarguard", "password": "live", "database": "pasarguard"}

    async def _fake_sync(migrator, db_type, admin, env_text=None, **kw):
        seen["password"] = kw.get("password")
        return True

    async def _run():
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                'SQLALCHEMY_DATABASE_URL="postgresql+asyncpg://'
                'pasarguard:url-secret@127.0.0.1:6432/pasarguard"\n'
                "DB_USER=pasarguard\n"
                "DB_PASSWORD=stale-secret\n"
                "POSTGRES_PASSWORD=other-secret\n",
                encoding="utf-8",
            )
            migrator = _migrator()
            with patch("app.services.migrators.xui.PASARGUARD_ENV", env_path), \
                 patch("app.services.db_auth.resolve_live_admin_connection", _fake_resolve), \
                 patch("app.services.db_auth.sync_postgres_roles_to_app_password", _fake_sync), \
                 patch("app.services.env_migration._compose_has_pgbouncer", lambda: False):
                assert await migrator._repair_pg_panel_auth("timescaledb") is True

    asyncio.run(_run())
    assert seen["password"] == "url-secret"


class _FakePgStack(XuiMigrator):
    """XuiMigrator wired to a scripted PostgreSQL stack.

    It reproduces what hides this failure on a real server: the container's own
    loopback is ``trust``, so every password "works" from inside, while a host
    connection must pass the hba method with a verifier stored in that same
    encoding.
    """

    IMAGE = "timescale/timescaledb-ha:pg17"
    FINGERPRINT = "1755590000"

    def __init__(
        self,
        job,
        params,
        *,
        password="realpwd",
        verifier="scram-sha-256",
        encryption="scram-sha-256",
        hba="scram-sha-256",
        published=("127.0.0.1", "5432"),
        host_error="",
        alter_ok=True,
        impostor_on=(),
        noise=False,
    ):
        super().__init__(job, params)
        self.noise = noise
        self.password = password
        self.verifier = verifier
        self.encryption = encryption
        self.hba = hba
        self.published = published
        self.host_error = host_error
        self.alter_ok = alter_ok
        self.impostor_on = set(impostor_on)
        self.commands: list[list[str]] = []

    async def _run_cmd(self, cmd, cwd=None, timeout=600, *, quiet=False):
        argv = list(cmd) if isinstance(cmd, list) else [cmd]
        self.commands.append(argv)
        joined = " ".join(argv)
        if "pg_isready" in joined:
            return True, ""
        if "{{.Config.Image}}" in joined:
            return True, f"{self.IMAGE}\n"
        if "{{json .NetworkSettings.Ports}}" in joined:
            if not self.published:
                return True, "{}"
            host_ip, host_port = self.published
            return True, json.dumps(
                {"5432/tcp": [{"HostIp": host_ip, "HostPort": host_port}]}
            )
        if argv[:3] == ["docker", "compose", "ps"]:
            return True, "db-container-id\n"
        if argv[:2] == ["docker", "ps"]:
            return True, "some-other-stack-db\t127.0.0.1:5432->5432/tcp\n"
        if "run" in argv and "--network" in argv:
            return self._host_login(argv)
        if "exec" in argv:
            return self._in_container(argv)
        return True, ""

    @staticmethod
    def _arg(argv, flag):
        return argv[argv.index(flag) + 1] if flag in argv else ""

    @staticmethod
    def _pgpassword(argv):
        for arg in argv:
            if arg.startswith("PGPASSWORD="):
                return arg.split("=", 1)[1]
        return ""

    # stdout and stderr share one pipe, and the two paths emit different chatter
    NOISE = {
        "exec": 'WARN[0000] The "PG_WORK_MEM" variable is not set. Defaulting to ""',
        "run": "Unable to find image 'postgres:latest' locally",
    }

    def _answer(self, sql, source):
        if sql == PG_FINGERPRINT_SQL:
            value = self.FINGERPRINT
        elif "password_encryption" in sql:
            value = f"{self.encryption}|{self.verifier}|{self.hba}"
        else:
            value = "1"
        return f"{self.NOISE[source]}\n{value}" if self.noise else value

    def _in_container(self, argv):
        if "-tAc" in argv:  # local socket is trusted: the password is irrelevant
            return True, self._answer(self._arg(argv, "-tAc"), "exec") + "\n"
        for index, arg in enumerate(argv):
            if arg != "-c":
                continue
            sql = argv[index + 1]
            if "ALTER SYSTEM SET password_encryption" in sql:
                self.encryption = re.search(r"= '([^']+)'", sql).group(1)
            elif "ALTER ROLE" in sql:
                if not self.alter_ok:
                    return False, "ERROR:  permission denied to alter role"
                encryption = re.search(r"SET password_encryption = '([^']+)'", sql)
                self.verifier = encryption.group(1) if encryption else self.encryption
                self.password = (
                    re.search(r"PASSWORD '(.*)';", sql).group(1).replace("''", "'")
                )
        return True, "ALTER ROLE"

    def _host_login(self, argv):
        if self.host_error:
            return False, self.host_error
        port = self._arg(argv, "-p")
        user = self._arg(argv, "-U")
        if port in self.impostor_on:
            return True, "42\n"  # an unrelated PostgreSQL that happens to answer
        if not self.published or self.published[1] != port:
            return False, (
                "psql: error: connection to server at 127.0.0.1, port "
                f"{port} failed: Connection refused"
            )
        if (
            user != "pasarguard"
            or self._pgpassword(argv) != self.password
            or self.verifier != self.hba
        ):
            return False, (
                "psql: error: connection to server failed: FATAL:  "
                f'password authentication failed for user "{user}"'
            )
        return True, self._answer(self._arg(argv, "-tAc"), "run") + "\n"

    def altered_roles(self) -> int:
        return sum(1 for c in self.commands if "ALTER ROLE" in " ".join(c))

    def set_cluster_encryption_calls(self) -> int:
        return sum(1 for c in self.commands if "ALTER SYSTEM SET" in " ".join(c))


def _env(db_password: str, postgres_password: str | None = None) -> str:
    text = (
        'SQLALCHEMY_DATABASE_URL="postgresql+asyncpg://'
        f'pasarguard:{db_password}@127.0.0.1:6432/pasarguard"\n'
        "DB_USER=pasarguard\n"
        f"DB_PASSWORD={db_password}\n"
        "DB_NAME=pasarguard\n"
    )
    if postgres_password is not None:
        text += f"POSTGRES_PASSWORD={postgres_password}\n"
    return text


def _align(migrator, env_text, env_path):
    with patch("app.services.migrators.xui.PASARGUARD_ENV", env_path), \
         patch("app.services.migrators.xui.BACKUP_DIR", env_path.parent), \
         patch("app.services.pasarguard_ops.resolve_db_service", lambda _db: "timescaledb"), \
         patch("app.services.migrators.xui.asyncio.sleep", _no_sleep):
        return asyncio.run(
            migrator._align_pg_credentials_before_cross_db("timescaledb", env_text)
        )


def test_repair_fixes_password_encryption_before_syncing_roles():
    """The shared role sync would otherwise store a verifier the hba rejects."""
    async def _fake_resolve(migrator, db_type, env_text=None):
        return {"user": "pasarguard", "password": "url-secret", "database": "pasarguard"}

    async def _fake_sync(migrator, db_type, admin, env_text=None, **kw):
        return True

    async def _run():
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(_env("url-secret"), encoding="utf-8")
            migrator = _FakePgStack(
                MigrationJob(job_id="repairenc"),
                {"target_db": "timescaledb"},
                encryption="md5",
                hba="scram-sha-256",
            )
            with patch("app.services.migrators.xui.PASARGUARD_ENV", env_path), \
                 patch("app.services.pasarguard_ops.resolve_db_service", lambda _db: "timescaledb"), \
                 patch("app.services.db_auth.resolve_live_admin_connection", _fake_resolve), \
                 patch("app.services.db_auth.sync_postgres_roles_to_app_password", _fake_sync), \
                 patch("app.services.env_migration._compose_has_pgbouncer", lambda: False):
                assert await migrator._repair_pg_panel_auth("timescaledb") is True
            return migrator

    migrator = asyncio.run(_run())
    assert migrator.set_cluster_encryption_calls() == 1
    assert migrator.encryption == "scram-sha-256"


def test_precheck_accepts_working_credentials_untouched():
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        env_text = _env("realpwd")
        env_path.write_text(env_text, encoding="utf-8")
        migrator = _FakePgStack(MigrationJob(job_id="pre1"), {"target_db": "timescaledb"})
        result = _align(migrator, env_text, env_path)

    assert result == env_text
    assert migrator.altered_roles() == 0
    assert migrator.set_cluster_encryption_calls() == 0
    conn = migrator.params["_resolved_target_conn"]
    assert conn["user"] == "pasarguard"
    assert conn["password"] == "realpwd"
    assert conn["port"] == "5432"


def test_precheck_picks_the_password_that_survives_tcp_auth():
    """POSTGRES_PASSWORD is tried first but is stale; DB_PASSWORD is the live one."""
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        env_text = _env("realpwd", postgres_password="stale")
        env_path.write_text(env_text, encoding="utf-8")
        migrator = _FakePgStack(MigrationJob(job_id="pre2"), {"target_db": "timescaledb"})
        result = _align(migrator, env_text, env_path)
        on_disk = env_path.read_text(encoding="utf-8")

    assert migrator.altered_roles() == 0
    assert migrator.params["_resolved_target_conn"]["password"] == "realpwd"
    # .env must stop advertising the stale secret, or role sync re-applies it
    assert 'POSTGRES_PASSWORD="realpwd"' in result
    assert "POSTGRES_PASSWORD=stale" not in result
    assert 'POSTGRES_PASSWORD="realpwd"' in on_disk


def test_precheck_survives_docker_warnings_on_the_output_pipe():
    """Healthy stack, noisy docker: nothing may be altered or refused."""
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        env_text = _env("realpwd")
        env_path.write_text(env_text, encoding="utf-8")
        migrator = _FakePgStack(
            MigrationJob(job_id="noise"), {"target_db": "timescaledb"}, noise=True,
        )
        result = _align(migrator, env_text, env_path)
        on_disk = env_path.read_text(encoding="utf-8")

    assert result == env_text
    assert on_disk == env_text
    assert migrator.altered_roles() == 0
    assert migrator.set_cluster_encryption_calls() == 0
    assert migrator.params["_resolved_target_conn"]["password"] == "realpwd"
    assert not any("different postmaster" in line for line in migrator.job.logs)


def test_precheck_tries_the_credentials_the_copy_would_have_used_first():
    """Whatever worked before this pre-check existed must still work untouched."""
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        env_text = _env("env-secret")
        env_path.write_text(env_text, encoding="utf-8")
        migrator = _FakePgStack(
            MigrationJob(job_id="prev"),
            {"target_db": "timescaledb"},
            password="wizard-secret",
        )
        with patch(
            "app.services.db_credentials.get_target_connection",
            lambda _params: {"user": "pasarguard", "password": "wizard-secret"},
        ):
            result = _align(migrator, env_text, env_path)

    assert migrator.altered_roles() == 0
    assert migrator.params["_resolved_target_conn"]["password"] == "wizard-secret"
    assert 'DB_PASSWORD="wizard-secret"' in result


def test_precheck_does_not_trust_the_containers_own_loopback():
    """The password is right, but stored md5 against a scram-sha-256 host rule.

    This is the shape of the reported failure: everything inside the container
    accepts the password, alembic connects from the host and is rejected.
    """
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        env_text = _env("realpwd")
        env_path.write_text(env_text, encoding="utf-8")
        migrator = _FakePgStack(
            MigrationJob(job_id="pre3"),
            {"target_db": "timescaledb"},
            verifier="md5",
            encryption="md5",
            hba="scram-sha-256",
        )
        result = _align(migrator, env_text, env_path)

    assert migrator.verifier == "scram-sha-256"
    assert migrator.password == "realpwd"
    assert migrator.altered_roles() == 1
    assert migrator.params["_resolved_target_conn"]["password"] == "realpwd"
    assert result == env_text


def test_precheck_fixes_the_cluster_default_even_when_login_works():
    """PasarGuard's own role sync runs an ALTER ROLE right before alembic.

    With password_encryption still on md5 that sync would rewrite a working
    password into a verifier no host connection can use.
    """
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        env_text = _env("realpwd")
        env_path.write_text(env_text, encoding="utf-8")
        migrator = _FakePgStack(
            MigrationJob(job_id="pre4"),
            {"target_db": "timescaledb"},
            encryption="md5",
            verifier="scram-sha-256",
            hba="scram-sha-256",
        )
        _align(migrator, env_text, env_path)

    assert migrator.set_cluster_encryption_calls() == 1
    assert migrator.encryption == "scram-sha-256"
    assert migrator.altered_roles() == 0
    assert migrator.params["_resolved_target_conn"]["password"] == "realpwd"


def test_precheck_repairs_role_when_no_password_authenticates():
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        env_text = _env("newpwd")
        env_path.write_text(env_text, encoding="utf-8")
        migrator = _FakePgStack(
            MigrationJob(job_id="pre5"), {"target_db": "timescaledb"}, password="forgotten",
        )
        result = _align(migrator, env_text, env_path)

    assert migrator.altered_roles() == 1
    assert migrator.password == "newpwd"
    assert migrator.params["_resolved_target_conn"]["password"] == "newpwd"
    assert result == env_text


def test_precheck_uses_the_port_docker_actually_publishes():
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        env_text = _env("realpwd")
        env_path.write_text(env_text, encoding="utf-8")
        migrator = _FakePgStack(
            MigrationJob(job_id="pre6"),
            {"target_db": "timescaledb"},
            published=("127.0.0.1", "5433"),
        )
        _align(migrator, env_text, env_path)

    assert migrator.params["_resolved_target_conn"]["port"] == "5433"


def test_precheck_refuses_a_foreign_postgres_on_the_assumed_port():
    """A stranger on 5432 must never be handed to the copy — it drops the schema."""
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        env_text = _env("realpwd")
        env_path.write_text(env_text, encoding="utf-8")
        migrator = _FakePgStack(
            MigrationJob(job_id="pre7"),
            {"target_db": "timescaledb"},
            published=None,
            impostor_on=("5432",),
        )
        result = _align(migrator, env_text, env_path)

    assert result == env_text
    assert "_resolved_target_conn" not in migrator.params
    assert any("different postmaster" in line for line in migrator.job.logs)


def test_precheck_explains_the_failure_without_causing_one():
    """A hopeless case is described in the log; the migration still runs its course."""
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        env_text = _env("newpwd")
        env_path.write_text(env_text, encoding="utf-8")
        migrator = _FakePgStack(
            MigrationJob(job_id="pre8"),
            {"target_db": "timescaledb"},
            password="forgotten",
            alter_ok=False,
        )
        result = _align(migrator, env_text, env_path)
        on_disk = env_path.read_text(encoding="utf-8")

    assert result == env_text
    assert "_resolved_target_conn" not in migrator.params
    assert on_disk == env_text
    assert any("ALTER ROLE pasarguard failed" in line for line in migrator.job.logs)
    assert any(
        "some-other-stack-db" in line and "127.0.0.1:5432" in line
        for line in migrator.job.logs
    )


def test_precheck_skipped_for_mysql_and_by_kill_switch():
    import os

    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        env_text = _env("realpwd")
        env_path.write_text(env_text, encoding="utf-8")

        migrator = _FakePgStack(MigrationJob(job_id="pre5"), {"target_db": "mariadb"})
        with patch("app.services.migrators.xui.PASARGUARD_ENV", env_path):
            assert asyncio.run(
                migrator._align_pg_credentials_before_cross_db("mariadb", env_text)
            ) == env_text
        assert migrator.commands == []

        migrator = _FakePgStack(MigrationJob(job_id="pre6"), {"target_db": "timescaledb"})
        os.environ[XUI_AUTH_HEAL_ENV] = "0"
        try:
            assert _align(migrator, env_text, env_path) == env_text
        finally:
            os.environ.pop(XUI_AUTH_HEAL_ENV, None)
        assert migrator.commands == []


def test_probe_result_classification():
    assert pg_probe_result(True, "1") == "ok"
    assert pg_probe_result(
        False, 'FATAL:  password authentication failed for user "pasarguard"'
    ) == "auth-failed"
    assert pg_probe_result(False, "fe_sendauth: no password supplied") == "auth-failed"
    assert pg_probe_result(
        False, "psql: error: connection to server at 127.0.0.1, port 5432 failed: Connection refused"
    ) == "unusable"
    assert pg_probe_result(False, 'docker: Error response from daemon: exec: "psql"') == "unusable"


def test_precheck_never_rewrites_roles_when_the_probe_cannot_reach_postgres():
    """Connection refused is not a wrong password — leave the database alone."""
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        env_text = _env("realpwd", postgres_password="stale")
        env_path.write_text(env_text, encoding="utf-8")
        migrator = _FakePgStack(
            MigrationJob(job_id="pre9"),
            {"target_db": "timescaledb"},
            host_error=(
                'docker: Error response from daemon: exec: "psql": executable file not found'
            ),
        )
        result = _align(migrator, env_text, env_path)
        on_disk = env_path.read_text(encoding="utf-8")

    assert result == env_text
    assert on_disk == env_text
    assert migrator.altered_roles() == 0
    assert "_resolved_target_conn" not in migrator.params
    assert any("executable file not found" in line for line in migrator.job.logs)


def test_precheck_probes_the_way_alembic_connects():
    """Every login attempt must come from the host, never from inside the container."""
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        env_text = _env("realpwd")
        env_path.write_text(env_text, encoding="utf-8")
        migrator = _FakePgStack(MigrationJob(job_id="pre10"), {"target_db": "timescaledb"})
        _align(migrator, env_text, env_path)

    logins = [c for c in migrator.commands if "PGPASSWORD=realpwd" in " ".join(c)]
    assert logins, "the pre-check must attempt a login"
    assert all(
        "run" in cmd and "--network" in cmd and "host" in cmd
        for cmd in logins
        if PG_FINGERPRINT_SQL in " ".join(cmd) and "exec" not in cmd
    )
    assert any("--entrypoint" in cmd for cmd in logins)


def test_endpoint_candidates_prefer_loopback_and_the_published_port():
    assert pg_endpoint_candidates("127.0.0.1", "5433") == [
        ("127.0.0.1", "5433"), ("127.0.0.1", "5432"),
    ]
    assert pg_endpoint_candidates("10.0.0.5", "5432") == [
        ("127.0.0.1", "5432"), ("10.0.0.5", "5432"),
    ]
    assert pg_endpoint_candidates("", "") == [("127.0.0.1", "5432")]


def test_parse_published_port_prefers_loopback():
    text = json.dumps({
        "5432/tcp": [
            {"HostIp": "10.0.0.5", "HostPort": "5555"},
            {"HostIp": "127.0.0.1", "HostPort": "5433"},
        ]
    })
    assert parse_published_port(text) == ("127.0.0.1", "5433")
    assert parse_published_port(
        json.dumps({"5432/tcp": [{"HostIp": "0.0.0.0", "HostPort": "5432"}]})
    ) == ("127.0.0.1", "5432")
    assert parse_published_port(json.dumps({"5432/tcp": None})) == ("", "")
    assert parse_published_port("not json") == ("", "")


def test_containers_publishing_port():
    ps_output = (
        "pasarguard-timescaledb-1\t127.0.0.1:5432->5432/tcp\n"
        "other-panel-db\t0.0.0.0:6432->6432/tcp\n"
    )
    assert containers_publishing_port(ps_output, "5432") == ["pasarguard-timescaledb-1"]
    assert containers_publishing_port(ps_output, "6432") == ["other-panel-db"]
    assert containers_publishing_port("", "5432") == []


def test_query_answers_are_read_out_of_docker_noise():
    noisy = (
        'WARN[0000] The "PG_WORK_MEM" variable is not set. Defaulting to ""\n'
        "Unable to find image 'postgres:latest' locally\n"
        "1755590000\n"
    )
    assert pg_fingerprint_value(noisy) == "1755590000"
    assert pg_fingerprint_value("WARN[0000] nothing here") == ""
    assert pg_fingerprint_value("") == ""
    assert parse_pg_auth_context(
        "time=\"2026-08-19\" level=warning msg=\"a|b|c warning\"\n"
        "md5|md5|scram-sha-256\n"
    ) == {"encryption": "md5", "verifier": "md5", "hba": "scram-sha-256"}


def test_password_storage_rules():
    scram = parse_pg_auth_context("md5|md5|scram-sha-256\n")
    assert scram == {
        "encryption": "md5", "verifier": "md5", "hba": "scram-sha-256",
    }
    assert required_password_encryption(scram) == "scram-sha-256"
    assert password_storage_mismatch(scram) is True
    assert password_storage_mismatch(
        parse_pg_auth_context("scram-sha-256|scram-sha-256|scram-sha-256")
    ) is False
    # A trust or unknown host rule proves nothing: never rewrite a password on it.
    assert required_password_encryption(parse_pg_auth_context("md5|md5|trust")) == ""
    assert password_storage_mismatch(parse_pg_auth_context("md5|unknown|md5")) is False
    assert parse_pg_auth_context("") == {}
    assert password_storage_mismatch({}) is False


def test_describe_password_source():
    env_text = _env("live", postgres_password="stale")
    assert describe_password_source(env_text, "stale") == "POSTGRES_PASSWORD"
    assert describe_password_source(env_text, "live") == "DB_PASSWORD"
    assert describe_password_source(_env("live"), "live") == "DB_PASSWORD"
    assert describe_password_source("", "anything") == "docker-compose"


def test_normalize_pg_env_passwords_only_touches_existing_keys():
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        env_text = _env("old", postgres_password="old")
        env_path.write_text(env_text, encoding="utf-8")
        migrator = _FakePgStack(MigrationJob(job_id="norm"), {})
        with patch("app.services.migrators.xui.PASARGUARD_ENV", env_path), \
             patch("app.services.migrators.xui.BACKUP_DIR", env_path.parent):
            updated = migrator._normalize_pg_env_passwords(env_text, "new")
            unchanged = migrator._normalize_pg_env_passwords(updated, "new")

    assert 'DB_PASSWORD="new"' in updated
    assert 'POSTGRES_PASSWORD="new"' in updated
    assert "MYSQL_ROOT_PASSWORD" not in updated
    assert unchanged == updated


if __name__ == "__main__":
    test_parse_container_env_keeps_last_value()
    test_parse_container_env_keeps_equals_in_value()
    test_pgbouncer_matching_credentials_report_no_drift()
    test_pgbouncer_stale_password_is_detected()
    test_pgbouncer_unknown_or_empty_keys_are_ignored()
    test_logs_show_pg_auth_failure()
    test_auth_heal_kill_switch()
    test_refresh_skips_when_pgbouncer_already_matches()
    test_refresh_recreates_when_credentials_are_stale()
    test_refresh_falls_back_to_restart_on_invalid_compose()
    test_refresh_disabled_by_kill_switch()
    test_refresh_noop_for_mysql_target()
    test_start_panel_does_not_repair_when_panel_is_healthy()
    test_start_panel_repairs_and_retries_on_auth_failure()
    test_start_panel_reraises_when_failure_is_not_auth()
    test_start_panel_keeps_original_error_when_repair_finds_nothing()
    test_repair_uses_panel_url_password_for_roles()
    test_repair_fixes_password_encryption_before_syncing_roles()
    test_precheck_accepts_working_credentials_untouched()
    test_precheck_picks_the_password_that_survives_tcp_auth()
    test_precheck_survives_docker_warnings_on_the_output_pipe()
    test_precheck_tries_the_credentials_the_copy_would_have_used_first()
    test_precheck_does_not_trust_the_containers_own_loopback()
    test_precheck_fixes_the_cluster_default_even_when_login_works()
    test_precheck_repairs_role_when_no_password_authenticates()
    test_precheck_uses_the_port_docker_actually_publishes()
    test_precheck_refuses_a_foreign_postgres_on_the_assumed_port()
    test_precheck_explains_the_failure_without_causing_one()
    test_precheck_skipped_for_mysql_and_by_kill_switch()
    test_probe_result_classification()
    test_precheck_never_rewrites_roles_when_the_probe_cannot_reach_postgres()
    test_precheck_probes_the_way_alembic_connects()
    test_endpoint_candidates_prefer_loopback_and_the_published_port()
    test_parse_published_port_prefers_loopback()
    test_containers_publishing_port()
    test_query_answers_are_read_out_of_docker_noise()
    test_password_storage_rules()
    test_describe_password_source()
    test_normalize_pg_env_passwords_only_touches_existing_keys()
    print("OK: x-ui PostgreSQL auth repair")
