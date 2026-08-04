"""Concurrent migration guard."""

import asyncio
from unittest.mock import MagicMock, patch

from app.services.orchestrator import (
    MigrationAlreadyRunning,
    _active_jobs,
    get_running_migration_job,
    start_migration,
)
from app.services.migrators.base import MigrationJob


def setup_function():
    _active_jobs.clear()


def teardown_function():
    _active_jobs.clear()


def test_get_running_migration_job():
    j = MigrationJob(job_id="abc")
    j.status = "running"
    _active_jobs[j.job_id] = j
    assert get_running_migration_job() is j
    j.status = "success"
    assert get_running_migration_job() is None
    print("OK: get_running_migration_job")


def test_start_migration_rejects_second_job():
    async def _run():
        class FakeMigrator:
            def __init__(self, job, params):
                self.job = job

            async def run(self, params):
                await asyncio.sleep(0.05)
                return {"ok": True}

        with patch.dict(
            "app.services.orchestrator.MIGRATORS",
            {"marzban": FakeMigrator},
            clear=False,
        ):
            job1 = await start_migration({"source_panel": "marzban"})
            try:
                await start_migration({"source_panel": "marzban"})
                raise AssertionError("expected MigrationAlreadyRunning")
            except MigrationAlreadyRunning as e:
                assert e.job.job_id == job1.job_id
            # Let first finish
            for _ in range(50):
                if job1.status in ("success", "error"):
                    break
                await asyncio.sleep(0.02)
            assert job1.status == "success"
            job2 = await start_migration({"source_panel": "marzban"})
            assert job2.job_id != job1.job_id
            for _ in range(50):
                if job2.status in ("success", "error"):
                    break
                await asyncio.sleep(0.02)

    asyncio.run(_run())
    print("OK: start_migration rejects concurrent job")


if __name__ == "__main__":
    setup_function()
    test_get_running_migration_job()
    teardown_function()
    setup_function()
    test_start_migration_rejects_second_job()
    teardown_function()
    print("\nAll orchestrator concurrency tests passed.")
