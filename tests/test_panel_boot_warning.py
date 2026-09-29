"""Panel-config boot failures soft-succeed as warning after data is ready."""


def test_is_panel_config_boot_failure_ssl_key_mismatch():
    from app.services.pasarguard_ops import is_panel_config_boot_failure

    log = (
        "PasarGuard did not reach ready state\n"
        "ssl.SSLError: [X509: KEY_VALUES_MISMATCH] key values mismatch (_ssl.c:4184)\n"
        "ValueError: SSL Error: [X509: KEY_VALUES_MISMATCH] key values mismatch"
    )
    assert is_panel_config_boot_failure(log)
    assert not is_panel_config_boot_failure(
        "password authentication failed for user pasarguard"
    )
    assert not is_panel_config_boot_failure("panel database is empty")
    print("OK: SSL key mismatch classified as panel-config boot failure")


def test_build_panel_boot_warning_key_mismatch():
    from app.services.pasarguard_ops import build_panel_boot_warning

    warn = build_panel_boot_warning(
        "ValueError: SSL Error: [X509: KEY_VALUES_MISMATCH] key values mismatch"
    )
    assert warn["kind"] == "ssl_key_mismatch"
    assert "SSL" in warn["en"] or "certificate" in warn["en"].lower()
    assert "گواهی" in warn["fa"] or "SSL" in warn["fa"]
    assert any("KEY_VALUES_MISMATCH" in c for c in warn["causes_fa"])
    assert warn.get("detail")
    print("OK: panel boot warning payload for key mismatch")


def test_safe_start_soft_warns_on_ssl_without_failing_job():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.services import pasarguard_ops as ops
    from app.services.migrators.base import MigrationJob

    class _Mig:
        def __init__(self):
            self.job = MigrationJob(job_id="ssl-warn")
            self.params = {"target_db": "timescaledb"}

    mig = _Mig()
    ssl_log = (
        "pasarguard-1 exited with code 1\n"
        "ssl.SSLError: [X509: KEY_VALUES_MISMATCH] key values mismatch\n"
        "ValueError: SSL Error: KEY_VALUES_MISMATCH"
    )

    async def _run():
        with (
            patch.object(ops, "_compose_text", return_value="services:\n  pasarguard:\n    image: x\n"),
            patch(
                "app.services.multiworker_stack.start_panel_stack",
                new_callable=AsyncMock,
                return_value=(True, "ok"),
            ),
            patch.object(
                ops,
                "verify_pasarguard_healthy",
                new_callable=AsyncMock,
                side_effect=RuntimeError(
                    "PasarGuard did not reach ready state\n" + ssl_log
                ),
            ),
        ):
            await ops.safe_start_pasarguard(mig, health_max_wait=5)

    asyncio.run(_run())
    warn = getattr(mig.job, "panel_boot_warning", None) or mig.params.get("_panel_boot_warning")
    assert isinstance(warn, dict)
    assert warn.get("kind") == "ssl_key_mismatch"
    print("OK: safe_start_pasarguard soft-warns on SSL mismatch")


def test_orchestrator_merges_panel_boot_warning_into_success():
    import asyncio
    from unittest.mock import patch

    from app.services import orchestrator as orch

    class _FakeMig:
        def __init__(self, job, params):
            self.job = job
            self.params = params

        async def run(self, params):
            self.job.panel_boot_warning = {
                "kind": "ssl_key_mismatch",
                "fa": "هشدار SSL",
                "en": "SSL warn",
                "causes_fa": ["a"],
                "causes_en": ["a"],
            }
            return {"users_migrated": 3}

    async def _run():
        with (
            patch.object(orch, "ensure_panel_idle"),
            patch.object(orch, "_prune_finished_jobs"),
            patch.dict(orch.MIGRATORS, {"3x-ui": _FakeMig}, clear=False),
        ):
            job = await orch.start_migration(
                {"source_panel": "3x-ui", "target_db": "timescaledb"}
            )
            for _ in range(50):
                if job.status in ("success", "error"):
                    break
                await asyncio.sleep(0.02)
            return job

    job = asyncio.run(_run())
    assert job.status == "success"
    assert job.result
    assert job.result.get("panel_boot_warning", {}).get("kind") == "ssl_key_mismatch"
    assert job.result.get("panel_healthy") is False
    print("OK: orchestrator keeps success + panel_boot_warning")


if __name__ == "__main__":
    test_is_panel_config_boot_failure_ssl_key_mismatch()
    test_build_panel_boot_warning_key_mismatch()
    test_safe_start_soft_warns_on_ssl_without_failing_job()
    test_orchestrator_merges_panel_boot_warning_into_success()
