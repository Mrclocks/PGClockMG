"""x-ui → PostgreSQL auth repair: PgBouncer credential drift and panel retry."""

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.migrators.base import MigrationJob
from app.services.migrators.xui import (
    XUI_AUTH_HEAL_ENV,
    XuiMigrator,
    logs_show_pg_auth_failure,
    parse_container_env,
    pgbouncer_env_mismatch,
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
    print("OK: x-ui PostgreSQL auth repair")
