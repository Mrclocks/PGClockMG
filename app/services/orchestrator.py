"""Migration job orchestrator."""

import asyncio
import traceback
from typing import Callable

from app.services.migrators.base import MigrationJob
from app.services.migrators.marzban import MarzbanMigrator
from app.services.migrators.xui import XuiMigrator
from app.services.migrators.hiddify import HiddifyMigrator
from app.services.migrators.pasarguard_db import PasarguardDbMigrator

from app.services.panel_job_lock import PanelJobAlreadyRunning, ensure_panel_idle

MIGRATORS = {
    "marzban": MarzbanMigrator,
    "3x-ui": XuiMigrator,
    "hiddify": HiddifyMigrator,
    "pasarguard": PasarguardDbMigrator,
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


# Backward-compatible alias used by API / older imports.
MigrationAlreadyRunning = PanelJobAlreadyRunning


async def start_migration(params: dict, on_log: Callable | None = None) -> MigrationJob:
    panel = str(params.get("source_panel") or "")
    migrator_cls = MIGRATORS.get(panel)
    if not migrator_cls:
        raise ValueError(f"Unsupported panel: {panel}")

    # Marzban / 3x-ui / Hiddify: soft-skip broken user rows by default.
    # Change-DB (pasarguard): keep critical tables hard-complete like restore convert.
    if "skip_bad_user_rows" not in params:
        params["skip_bad_user_rows"] = panel != "pasarguard"

    ensure_panel_idle()

    _prune_finished_jobs()
    job = MigrationJob()
    _active_jobs[job.job_id] = job

    if on_log:
        job.on_log(on_log)

    async def _run():
        try:
            job.status = "running"
            job.set_progress(0, "Starting migration...")
            if params.get("skip_bad_user_rows", True):
                job.log("Policy: skip broken user rows and continue (report at end)")
            migrator = migrator_cls(job, params)
            result = await migrator.run(params)
            result = dict(result or {})
            warn = getattr(job, "panel_boot_warning", None) or (
                (migrator.params or {}).get("_panel_boot_warning")
                if getattr(migrator, "params", None)
                else None
            )
            if isinstance(warn, dict):
                result["panel_boot_warning"] = warn
                result["panel_healthy"] = False
                job.log(
                    "Migration data finished with panel boot warning: "
                    f"{warn.get('kind') or 'panel_config'}"
                )
            elif "panel_healthy" not in result:
                result["panel_healthy"] = True
            job.result = result
            job.status = "success"
            job.set_progress(100, "Migration completed successfully!")
        except Exception as e:
            from app.services.pg_restore import explain_restore_error

            explain = explain_restore_error(
                e,
                params.get("source_db"),
                params.get("target_db"),
            )
            job.status = "error"
            job.message = explain.get("fa") or explain.get("en") or str(e)
            job.log(f"Error: {explain.get('detail') or e}")
            job.log(traceback.format_exc())
            job.result = {"error": str(e), "error_explain": explain}

    task = asyncio.create_task(_run())
    _job_tasks.add(task)
    task.add_done_callback(_job_tasks.discard)
    return job
