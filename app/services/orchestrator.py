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


def get_job(job_id: str) -> MigrationJob | None:
    return _active_jobs.get(job_id)


async def start_migration(params: dict, on_log: Callable | None = None) -> MigrationJob:
    panel = params.get("source_panel")
    migrator_cls = MIGRATORS.get(panel)
    if not migrator_cls:
        raise ValueError(f"Unsupported panel: {panel}")

    job = MigrationJob()
    _active_jobs[job.job_id] = job

    if on_log:
        job.on_log(on_log)

    async def _run():
        try:
            job.status = "running"
            job.set_progress(0, "Starting migration...")
            migrator = migrator_cls(job)
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

    asyncio.create_task(_run())
    return job
