"""Migration job orchestrator."""

import asyncio
import traceback
from typing import Callable

from app.services.migrators.base import MigrationJob
from app.services.migrators.marzban import MarzbanMigrator
from app.services.migrators.xui import XuiMigrator
from app.services.migrators.hiddify import HiddifyMigrator
from app.services.migrators.pasarguard_db import PasarguardDbMigrator

from app.services.migrators.remnawave import RemnawaveMigrator

MIGRATORS = {
    "marzban": MarzbanMigrator,
    "3x-ui": XuiMigrator,
    "hiddify": HiddifyMigrator,
    "pasarguard": PasarguardDbMigrator,
    "remnawave": RemnawaveMigrator,
}

_active_jobs: dict[str, MigrationJob] = {}
_job_tasks: set[asyncio.Task] = set()
MAX_FINISHED_JOBS = 20


def _prune_finished_jobs() -> None:
    finished = [j for j in _active_jobs.values() if j.status in ("success", "error")]
    for job in finished[: max(0, len(finished) - MAX_FINISHED_JOBS)]:
        _active_jobs.pop(job.job_id, None)
        job.clear_log_callbacks()


def get_job(job_id: str) -> MigrationJob | None:
    return _active_jobs.get(job_id)


def get_running_migration_job() -> MigrationJob | None:
    """Return the in-flight job if any (pending/running)."""
    for job in _active_jobs.values():
        if job.status in ("pending", "running"):
            return job
    return None


class MigrationAlreadyRunning(RuntimeError):
    """Raised when a second migrate is requested while one is active."""

    def __init__(self, job: MigrationJob):
        self.job = job
        super().__init__(
            f"A migration is already running (job_id={job.job_id}, "
            f"progress={job.progress}%). Wait for it to finish before starting another."
        )


async def start_migration(params: dict, on_log: Callable | None = None) -> MigrationJob:
    panel = str(params.get("source_panel") or "")
    migrator_cls = MIGRATORS.get(panel)
    if not migrator_cls:
        raise ValueError(f"Unsupported panel: {panel}")

    existing = get_running_migration_job()
    if existing:
        raise MigrationAlreadyRunning(existing)

    _prune_finished_jobs()
    job = MigrationJob()
    _active_jobs[job.job_id] = job

    if on_log:
        job.on_log(on_log)

    async def _run():
        try:
            job.status = "running"
            job.set_progress(0, "Starting migration...")
            migrator = migrator_cls(job, params)
            result = await migrator.run(params)
            job.result = result
            job.status = "success"
            job.set_progress(100, "Migration completed successfully!")
        except Exception as e:
            job.status = "error"
            job.message = str(e)
            job.log(f"Error: {e}")
            job.log(traceback.format_exc())
            job.result = {"error": str(e)}

    task = asyncio.create_task(_run())
    _job_tasks.add(task)
    task.add_done_callback(_job_tasks.discard)
    return job
