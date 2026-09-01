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
    test_extract_failure_snippet_includes_exception_line()
    print("\nAll multiworker restore tests passed")
