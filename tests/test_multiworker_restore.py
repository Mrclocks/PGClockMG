"""Tests for multi-worker / NATS restore stack helpers."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import multiworker_stack as mws
from app.services.migrators.base import MigrationJob


MULTI_COMPOSE = """
services:
  nats:
    image: nats:2.10-alpine
  panel:
    container_name: pasarguard
    image: pasarguard/panel:latest
  node-worker:
    image: pasarguard/panel:latest
  scheduler:
    image: pasarguard/panel:latest
  timescaledb:
    image: timescale/timescaledb:latest-pg17
"""

SIMPLE_COMPOSE = """
services:
  pasarguard:
    image: pasarguard/panel:latest
  timescaledb:
    image: timescale/timescaledb:latest-pg17
"""


def test_detect_single_worker_stack():
    with patch.object(mws, "_compose_text", return_value=SIMPLE_COMPOSE):
        info = mws.detect_multiworker_stack('UVICORN_WORKERS=1\nNATS_ENABLED=0\n')
    assert info["orchestrate"] is False
    assert info["uses_nats"] is False
    print("OK: single-worker not orchestrated")


def test_detect_multiworker_with_nats():
    env = '\n'.join([
        "UVICORN_WORKERS=4",
        "NATS_ENABLED=1",
        'NATS_URL="nats://localhost:4222"',
    ])
    with (
        patch.object(mws, "_compose_text", return_value=MULTI_COMPOSE),
        patch.object(mws, "resolve_pasarguard_service", return_value="panel"),
    ):
        info = mws.detect_multiworker_stack(env)
    assert info["orchestrate"] is True
    assert info["uses_nats"] is True
    assert info["panel_service"] == "panel"
    assert info["uvicorn_workers"] == 4
    print("OK: multi-worker stack detected")


def test_detect_nats_disabled_skips_orchestration():
    env = "UVICORN_WORKERS=1\nNATS_ENABLED=0\n"
    with (
        patch.object(mws, "_compose_text", return_value=MULTI_COMPOSE),
        patch.object(mws, "resolve_pasarguard_service", return_value="panel"),
    ):
        info = mws.detect_multiworker_stack(env)
    assert info["orchestrate"] is False
    assert info["uses_nats"] is False
    print("OK: NATS disabled + single worker → no orchestration")


def test_align_nats_env_fixes_localhost():
    env = 'UVICORN_WORKERS=4\nNATS_URL="nats://localhost:4222"\n'
    with patch.object(mws, "_compose_text", return_value=MULTI_COMPOSE):
        out = mws.align_nats_env_for_compose(env)
    assert "nats://nats:4222" in out
    assert "localhost" not in out
    assert 'NATS_ENABLED="1"' in out or "NATS_ENABLED=1" in out.replace(" ", "")
    print("OK: align NATS URL + enable flag")


def test_align_nats_env_noop_without_nats_service():
    env = 'NATS_URL="nats://localhost:4222"\n'
    with patch.object(mws, "_compose_text", return_value=SIMPLE_COMPOSE):
        out = mws.align_nats_env_for_compose(env)
    assert out == env
    print("OK: no nats service → env unchanged")


def test_panel_stack_stop_order():
    with patch.object(mws, "_compose_text", return_value=MULTI_COMPOSE):
        with patch.object(mws, "panel_compose_service", return_value="panel"):
            services = mws.panel_stack_stop_services()
    assert services == ["node-worker", "scheduler", "panel"]
    print("OK: stop order satellites before panel")


def test_start_panel_stack_single_worker():
    calls: list[tuple] = []

    async def fake_compose(job, *args, **kwargs):
        calls.append(args)
        return True, ""

    job = MigrationJob(job_id="mw1")
    with (
        patch.object(mws, "_compose_text", return_value=SIMPLE_COMPOSE),
        patch.object(mws, "detect_multiworker_stack", return_value={
            "orchestrate": False,
            "uses_nats": False,
            "panel_service": "pasarguard",
            "uvicorn_workers": 1,
            "satellite_services": [],
        }),
        patch.object(mws, "_compose_job", side_effect=fake_compose),
    ):
        ok, _ = asyncio.run(mws.start_panel_stack(job, force_recreate=True))
    assert ok is True
    assert calls[0] == ("up", "-d", "--force-recreate", "pasarguard")
    print("OK: single-worker start unchanged")


def test_compose_file_prefix_uses_both_main_and_multi():
    import tempfile
    import shutil
    from app.services import pasarguard_ops as po

    td = Path(tempfile.mkdtemp(prefix="pg-compose-"))
    old = po.PASARGUARD_DIR
    po.PASARGUARD_DIR = td
    try:
        (td / "docker-compose.yml").write_text(
            "services:\n  timescaledb:\n    image: x\n  pasarguard:\n    image: p\n",
            encoding="utf-8",
        )
        (td / "docker-compose.multi.yml").write_text(
            "services:\n  nats:\n    image: nats\n  panel:\n    image: p\n",
            encoding="utf-8",
        )
        prefix = po.compose_file_prefix()
        assert prefix == [
            "-f", str(td / "docker-compose.yml"),
            "-f", str(td / "docker-compose.multi.yml"),
        ]
        assert mws.compose_has_service("timescaledb") is True
        assert mws.compose_has_service("nats") is True
        old_env = po.PASARGUARD_ENV
        (td / ".env").write_text("UVICORN_WORKERS=4\nNATS_ENABLED=1\n", encoding="utf-8")
        po.PASARGUARD_ENV = td / ".env"
        try:
            assert po.resolve_pasarguard_service() == "panel"
            (td / ".env").write_text("UVICORN_WORKERS=1\nNATS_ENABLED=0\n", encoding="utf-8")
            assert po.resolve_pasarguard_service() == "pasarguard"
        finally:
            po.PASARGUARD_ENV = old_env
    finally:
        po.PASARGUARD_DIR = old
        shutil.rmtree(td, ignore_errors=True)
    print("OK: compose prefix merges main + multi")


def test_start_panel_stack_multi_worker():
    calls: list[tuple] = []

    async def fake_compose(job, *args, **kwargs):
        calls.append(args)
        if args and args[0] == "logs":
            if args[-1] == "panel":
                return True, "Application startup complete\n"
            return True, "Server is ready\n"
        return True, ""

    job = MigrationJob(job_id="mw2")
    with (
        patch.object(mws, "detect_multiworker_stack", return_value={
            "orchestrate": True,
            "uses_nats": True,
            "panel_service": "panel",
            "uvicorn_workers": 4,
            "satellite_services": ["node-worker", "scheduler"],
        }),
        patch.object(mws, "compose_has_service", return_value=True),
        patch.object(mws, "_compose_job", side_effect=fake_compose),
    ):
        ok, _ = asyncio.run(mws.start_panel_stack(job, force_recreate=True))
    assert ok is True
    assert ("up", "-d", "--force-recreate", "nats") in calls
    assert ("up", "-d", "--force-recreate", "panel") in calls
    assert ("up", "-d", "--force-recreate", "node-worker", "scheduler") in calls
    print("OK: multi-worker start order nats → panel → satellites")


def test_bare_traceback_not_treated_as_failure():
    from app.services.pasarguard_ops import _check_logs_for_failure

    blob = "\n".join([
        "panel-1 | Traceback (most recent call last):",
        "panel-1 |   File \"main.py\", line 1, in <module>",
        "panel-1 |     raise x",
    ])
    assert _check_logs_for_failure(blob) is None
    blob2 = blob + "\npanel-1 | RuntimeError: NATS is required when running more than 1 worker."
    assert _check_logs_for_failure(blob2) is not None
    print("OK: bare traceback ignored until exception line")


def test_transient_connect_and_bare_valueerror_not_hard_fail():
    from app.services.pasarguard_ops import (
        _check_logs_for_failure,
        _line_indicates_failure,
        _logs_only_transient_connect_noise,
    )

    assert not _line_indicates_failure(
        'pasarguard-1 | asyncpg.exceptions.CannotConnectNowError: could not connect'
    )
    assert not _line_indicates_failure(
        "pasarguard-1 | ConnectionRefusedError: [Errno 111] Connection refused"
    )
    assert not _line_indicates_failure("pasarguard-1 | ValueError: temporary retry")
    assert _line_indicates_failure(
        "pasarguard-1 | ERROR: Application startup failed. Exiting."
    )
    assert _check_logs_for_failure(
        "pasarguard-1 | password authentication failed for user \"x\""
    )
    assert _logs_only_transient_connect_noise(
        "pasarguard-1 | could not connect to server: Connection refused\n"
        "pasarguard-1 | retrying…"
    )
    print("OK: transient connect / bare ValueError are soft; auth/startup still hard")


def test_pgbouncer_env_mismatch_detects_stale_credentials():
    from app.services.db_auth import pgbouncer_env_mismatch

    stale = pgbouncer_env_mismatch(
        {"DB_USER": "pasarguard", "DB_PASSWORD": "old", "DB_NAME": "pasarguard"},
        user="pasarguard",
        password="new-secret",
        database="pasarguard",
    )
    assert stale == ["DB_PASSWORD"]
    assert pgbouncer_env_mismatch(
        {"DB_USER": "pasarguard", "DB_PASSWORD": "same", "DB_NAME": "pasarguard"},
        user="pasarguard",
        password="same",
        database="pasarguard",
    ) == []
    print("OK: pgbouncer env mismatch detection")


def test_extract_failure_snippet_includes_root_before_startup_failed():
    from app.services.pasarguard_ops import _extract_failure_snippet

    blob = "\n".join([
        "pasarguard-1 | asyncpg.exceptions.InvalidPasswordError: password authentication failed for user \"pasarguard\"",
        "pasarguard-1 | ERROR:    2026-09-01 18:49:57,022 - Application startup failed. Exiting.",
        "pasarguard-1 | ERROR:    2026-09-01 18:49:57,032 - Application startup failed. Exiting.",
    ])
    out = _extract_failure_snippet(blob)
    assert "InvalidPasswordError" in out
    assert "Application startup failed" in out
    print("OK: root cause before startup failed included")


def test_extract_failure_snippet_includes_exception_line():
    from app.services.pasarguard_ops import _extract_failure_snippet

    blob = "\n".join([
        "pasarguard-1 | Traceback (most recent call last):",
        "pasarguard-1 |   File \"main.py\", line 1",
        "pasarguard-1 | RuntimeError: NATS is required when running more than 1 worker.",
    ])
    out = _extract_failure_snippet(blob)
    assert "RuntimeError" in out
    assert "NATS is required" in out
    print("OK: failure snippet includes exception line")


def test_telegram_conflict_is_noise_not_root_cause():
    """Telegram getUpdates conflict must not dominate restore failure details."""
    from app.services.pasarguard_ops import (
        _extract_failure_snippet,
        _line_indicates_failure,
        _logs_dominated_by_telegram_noise,
        _logs_show_panel_startup,
    )

    spam_line = (
        "pasarguard-1 | Failed to fetch updates - TelegramConflictError: "
        "Telegram server says - Conflict: terminated by other getUpdates request; "
        "make sure that only one bot instance is running"
    )
    spam = "\n".join([spam_line] * 12)
    assert _logs_dominated_by_telegram_noise(spam)
    assert not _line_indicates_failure(spam_line)
    snip = _extract_failure_snippet(spam)
    assert "TelegramConflictError" not in snip or "ignored" in snip.lower()
    assert "not a panel boot failure" in snip

    # Real auth failure must still surface even when Telegram spam follows.
    mixed = "\n".join([
        "pasarguard-1 | asyncpg.exceptions.InvalidPasswordError: password authentication failed",
        "pasarguard-1 | ERROR:    Application startup failed. Exiting.",
        spam_line,
        spam_line,
        spam_line,
    ])
    mixed_snip = _extract_failure_snippet(mixed)
    assert "InvalidPasswordError" in mixed_snip
    assert "Application startup failed" in mixed_snip

    # Startup marker still visible when spam has not fully scrolled it out.
    with_boot = "\n".join([
        "pasarguard-1 | Application startup complete",
        spam_line,
        spam_line,
    ])
    assert _logs_show_panel_startup(with_boot)
    print("OK: TelegramConflictError treated as noise, real failures kept")


def test_explain_restore_telegram_noise_on_panel_not_up():
    from app.services.pg_restore import (
        _app_version_before,
        explain_restore_error,
    )

    exc = RuntimeError(
        "PasarGuard did not reach ready state (no 'Application startup complete' in logs).\n"
        "pasarguard-1 | Failed to fetch updates - TelegramConflictError: "
        "Telegram server says - Conflict: terminated by other getUpdates request"
    )
    info = explain_restore_error(exc, "sqlite", "timescaledb")
    assert "تلگرام" in info["fa"] or "Telegram" in info["en"]
    blob = "\n".join(info["causes_fa"])
    assert "getUpdates" in blob or "bot token" in blob or "ربات" in blob
    # On current builds the update tip is obsolete and must not appear.
    if not _app_version_before("4.6.17"):
        assert "4.6.17" not in blob
        assert "آپدیت" not in blob
    else:
        assert any("4.6.17" in c for c in info["causes_fa"])
    print("OK: explain_restore maps TelegramConflict panel-not-up")


def test_verify_healthy_accepts_telegram_noise_when_port_up():
    """sqlite→timescaledb false-fail: Telegram spam + listening port = healthy."""
    import app.services.pasarguard_ops as ops

    spam_line = (
        "pasarguard-1 | Failed to fetch updates - TelegramConflictError: "
        "Telegram server says - Conflict: terminated by other getUpdates request; "
        "make sure that only one bot instance is running"
    )
    spam = "\n".join([spam_line] * 10)

    class _Mig:
        def __init__(self):
            self.job = MigrationJob(job_id="tg-noise")
            self.params = {"target_db": "timescaledb"}

    mig = _Mig()

    async def _run():
        with (
            patch(
                "app.services.multiworker_stack.detect_multiworker_stack",
                return_value={
                    "uvicorn_workers": 1,
                    "uses_nats": False,
                    "orchestrate": False,
                },
            ),
            patch.object(ops, "fetch_pasarguard_logs", new_callable=AsyncMock, return_value=spam),
            patch.object(
                ops, "fetch_extended_panel_logs", new_callable=AsyncMock, return_value=spam
            ),
            patch.object(
                ops, "_pasarguard_container_state", new_callable=AsyncMock, return_value="running"
            ),
            patch.object(
                ops, "_panel_port_is_listening", new_callable=AsyncMock, return_value=True
            ),
            patch.object(ops.asyncio, "sleep", new_callable=AsyncMock),
        ):
            await ops.verify_pasarguard_healthy(mig, max_wait=60)

    asyncio.run(_run())
    blob = "\n".join(mig.job.logs)
    assert "healthy" in blob.lower()
    assert "TelegramConflictError" in blob or "telegram" in blob.lower()
    print("OK: verify_pasarguard_healthy accepts Telegram noise when port is up")


def test_node_control_conflict_is_noise_not_root_cause():
    """node controlled by another client must not fail restore health details."""
    from app.services.pasarguard_ops import (
        _extract_failure_snippet,
        _line_indicates_failure,
        _logs_dominated_by_node_control_noise,
        _logs_show_panel_startup,
    )

    spam_line = (
        "pasarguard-1 | ERROR:    - Node-operation - Failed to connect node nl, al "
        "with id 7, Error: node is controlled by another client"
    )
    spam = "\n".join([spam_line] * 10)
    assert _logs_dominated_by_node_control_noise(spam)
    assert not _line_indicates_failure(spam_line)
    snip = _extract_failure_snippet(spam)
    assert "controlled by another client" not in snip or "ignored" in snip.lower()
    assert "not a panel boot failure" in snip

    with_boot = "\n".join([
        "pasarguard-1 | Application startup complete",
        spam_line,
        spam_line,
    ])
    assert _logs_show_panel_startup(with_boot)
    print("OK: node controlled-by-another-client treated as noise")


def test_explain_restore_node_control_noise_on_panel_not_up():
    from app.services.pg_restore import (
        _app_version_before,
        explain_restore_error,
    )

    exc = RuntimeError(
        "PasarGuard did not reach ready state (no 'Application startup complete' in logs).\n"
        "pasarguard-1 | ERROR: - Node-operation - Failed to connect node nl, al "
        "with id 7, Error: node is controlled by another client"
    )
    info = explain_restore_error(exc, "sqlite", "postgresql")
    assert "نود" in info["fa"] or "node" in info["en"].lower()
    blob = "\n".join(info["causes_fa"])
    assert "کنترل" in blob or "controller" in blob.lower() or "controlled" in blob.lower()
    if not _app_version_before("4.6.27"):
        assert "4.6.27" not in blob
        assert "آپدیت" not in blob
    else:
        assert any("4.6.27" in c for c in info["causes_fa"])
    print("OK: explain_restore maps node-control panel-not-up")


def test_verify_healthy_accepts_node_control_noise_when_port_up():
    """sqlite→postgresql false-fail: node-session spam + listening port = healthy."""
    import app.services.pasarguard_ops as ops

    spam_line = (
        "pasarguard-1 | ERROR:    - Record-usages - Failed to get users stats "
        "from node 7, error: node is controlled by another client"
    )
    spam = "\n".join([spam_line] * 10)

    class _Mig:
        def __init__(self):
            self.job = MigrationJob(job_id="node-noise")
            self.params = {"target_db": "postgresql"}

    mig = _Mig()

    async def _run():
        with (
            patch(
                "app.services.multiworker_stack.detect_multiworker_stack",
                return_value={
                    "uvicorn_workers": 1,
                    "uses_nats": False,
                    "orchestrate": False,
                },
            ),
            patch.object(ops, "fetch_pasarguard_logs", new_callable=AsyncMock, return_value=spam),
            patch.object(
                ops, "fetch_extended_panel_logs", new_callable=AsyncMock, return_value=spam
            ),
            patch.object(
                ops, "_pasarguard_container_state", new_callable=AsyncMock, return_value="running"
            ),
            patch.object(
                ops, "_panel_port_is_listening", new_callable=AsyncMock, return_value=True
            ),
            patch.object(ops.asyncio, "sleep", new_callable=AsyncMock),
        ):
            await ops.verify_pasarguard_healthy(mig, max_wait=60)

    asyncio.run(_run())
    blob = "\n".join(mig.job.logs)
    assert "healthy" in blob.lower()
    assert "node" in blob.lower() or "controlled" in blob.lower()
    print("OK: verify_pasarguard_healthy accepts node-control noise when port is up")


def test_panel_port_probe_uses_published_and_in_container():
    """Host 127.0.0.1:8000 unpublished must not mean panel is down."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    import app.services.pasarguard_ops as ops

    mig = MagicMock()
    mig.job = MagicMock()
    mig.job.log = MagicMock()
    mig.params = {"target_db": "postgresql"}

    async def _run_cmd(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if "ps" in cmd and "-q" in cmd:
            return True, "abc123deadbeef"
        if "NetworkSettings.Ports" in joined:
            return True, '{"8000/tcp":[{"HostIp":"0.0.0.0","HostPort":"2087"}]}'
        if "IPAddress" in joined:
            return True, "172.18.0.5"
        if "python" in joined or "/dev/tcp" in joined:
            return True, ""
        return True, ""

    mig._run_cmd = AsyncMock(side_effect=_run_cmd)

    with (
        patch.object(ops, "_panel_uvicorn_port", new_callable=AsyncMock, return_value=8000),
        patch.object(ops, "_tcp_connect_ok", side_effect=lambda host, port, **k: (
            (host == "127.0.0.1" and int(port) == 2087)
            or (host == "172.18.0.5" and int(port) == 8000)
        )),
        patch.object(ops, "panel_compose_service", return_value="pasarguard"),
        patch.object(ops, "compose_file_prefix", return_value=[]),
        patch.object(ops, "extract_docker_container_id", return_value="abc123deadbeef"),
    ):
        # First: published 2087 should work even if loopback 8000 fails
        def _open(host, port, **k):
            return host == "127.0.0.1" and int(port) == 2087

        with patch.object(ops, "_tcp_connect_ok", side_effect=_open):
            assert asyncio.run(ops._panel_port_is_listening(mig)) is True

    # In-container fallback when nothing on host
    async def _run_cmd2(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if "ps" in cmd and "-q" in cmd:
            return True, "abc123deadbeef"
        if "NetworkSettings.Ports" in joined:
            return True, "{}"
        if "IPAddress" in joined:
            return True, ""
        if "python" in joined:
            return True, ""
        return False, ""

    mig2 = MagicMock()
    mig2.job = MagicMock()
    mig2.job.log = MagicMock()
    mig2.params = {"target_db": "postgresql"}
    mig2._run_cmd = AsyncMock(side_effect=_run_cmd2)

    with (
        patch.object(ops, "_panel_uvicorn_port", new_callable=AsyncMock, return_value=8000),
        patch.object(ops, "_tcp_connect_ok", return_value=False),
        patch.object(ops, "panel_compose_service", return_value="pasarguard"),
        patch.object(ops, "compose_file_prefix", return_value=[]),
        patch.object(ops, "extract_docker_container_id", return_value="abc123deadbeef"),
    ):
        assert asyncio.run(ops._panel_port_is_listening(mig2)) is True
    logged = " ".join(str(c) for c in mig2.job.log.call_args_list)
    assert "inside container" in logged.lower() or "listening inside" in logged.lower()
    print("OK: panel port probe uses published / in-container")


def test_verify_healthy_soft_accepts_startup_when_host_port_unpublished():
    """97% hang: Application startup complete + running, but host :8000 unpublished."""
    import app.services.pasarguard_ops as ops

    boot = "pasarguard-1 | INFO:     Application startup complete."

    class _Mig:
        def __init__(self):
            self.job = MigrationJob(job_id="port-soft")
            self.params = {"target_db": "postgresql"}

    mig = _Mig()

    async def _run():
        with (
            patch(
                "app.services.multiworker_stack.detect_multiworker_stack",
                return_value={
                    "uvicorn_workers": 1,
                    "uses_nats": False,
                    "orchestrate": False,
                },
            ),
            patch.object(ops, "fetch_pasarguard_logs", new_callable=AsyncMock, return_value=boot),
            patch.object(
                ops, "fetch_extended_panel_logs", new_callable=AsyncMock, return_value=boot
            ),
            patch.object(
                ops, "_pasarguard_container_state", new_callable=AsyncMock, return_value="running"
            ),
            patch.object(
                ops, "_panel_port_is_listening", new_callable=AsyncMock, return_value=False
            ),
            patch.object(ops.asyncio, "sleep", new_callable=AsyncMock),
        ):
            await ops.verify_pasarguard_healthy(mig, max_wait=60)

    asyncio.run(_run())
    blob = "\n".join(mig.job.logs)
    assert "healthy" in blob.lower()
    assert "startup complete" in blob.lower() or "host port" in blob.lower()
    print("OK: verify soft-accepts Application startup complete when host port unpublished")


def test_try_heal_nats_imports_read_env_text_from_db_auth():
    """Regression: read_env_text lives in db_auth, not env_migration."""
    import asyncio
    import inspect

    from app.services.pasarguard_ops import _try_heal_nats_multiworker
    from app.services.migrators.base import MigrationJob

    src = inspect.getsource(_try_heal_nats_multiworker)
    assert "from app.services.db_auth import read_env_text" in src
    assert "from app.services.env_migration import read_env_text" not in src

    class _Mig:
        def __init__(self):
            self.job = MigrationJob(job_id="nats-import")
            self.params = {"target_db": "mysql"}

    with patch.object(mws, "detect_multiworker_stack", return_value={
        "uvicorn_workers": 1,
        "uses_nats": False,
        "orchestrate": False,
    }):
        result = asyncio.run(
            _try_heal_nats_multiworker(_Mig(), "Database migrations failed")
        )
    assert result is False
    print("OK: NATS heal imports read_env_text from db_auth (no ImportError)")


if __name__ == "__main__":
    test_detect_single_worker_stack()
    test_detect_multiworker_with_nats()
    test_detect_nats_disabled_skips_orchestration()
    test_align_nats_env_fixes_localhost()
    test_align_nats_env_noop_without_nats_service()
    test_panel_stack_stop_order()
    test_start_panel_stack_single_worker()
    test_compose_file_prefix_uses_both_main_and_multi()
    test_start_panel_stack_multi_worker()
    test_bare_traceback_not_treated_as_failure()
    test_transient_connect_and_bare_valueerror_not_hard_fail()
    test_pgbouncer_env_mismatch_detects_stale_credentials()
    test_extract_failure_snippet_includes_root_before_startup_failed()
    test_extract_failure_snippet_includes_exception_line()
    test_telegram_conflict_is_noise_not_root_cause()
    test_explain_restore_telegram_noise_on_panel_not_up()
    test_verify_healthy_accepts_telegram_noise_when_port_up()
    test_node_control_conflict_is_noise_not_root_cause()
    test_explain_restore_node_control_noise_on_panel_not_up()
    test_verify_healthy_accepts_node_control_noise_when_port_up()
    test_panel_port_probe_uses_published_and_in_container()
    test_verify_healthy_soft_accepts_startup_when_host_port_unpublished()
    test_try_heal_nats_imports_read_env_text_from_db_auth()
    print("\nAll multiworker restore tests passed")
