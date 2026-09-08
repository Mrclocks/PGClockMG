"""Shared busy-gate for panel-mutating jobs (migrate + PasarGuard restore)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.services.migrators.base import MigrationJob

JobKind = Literal["migrate", "restore"]


@dataclass
class RunningPanelJob:
    kind: JobKind
    job: MigrationJob


class PanelJobAlreadyRunning(RuntimeError):
    """Raised when migrate/restore would overlap another panel-mutating job."""

    def __init__(self, running: RunningPanelJob):
        self.running = running
        self.job = running.job
        self.kind = running.kind
        super().__init__(
            f"A {running.kind} job is already running "
            f"(job_id={running.job.job_id}, progress={running.job.progress}%). "
            "Wait for it to finish before starting another migrate/restore."
        )


def get_running_panel_job() -> RunningPanelJob | None:
    """Return the active migrate or restore job, if any."""
    from app.services.orchestrator import get_running_migration_job
    from app.services.pg_restore import get_running_restore_job

    mig = get_running_migration_job()
    if mig:
        return RunningPanelJob(kind="migrate", job=mig)
    rest = get_running_restore_job()
    if rest:
        return RunningPanelJob(kind="restore", job=rest)
    return None


def ensure_panel_idle() -> None:
    """Raise PanelJobAlreadyRunning if migrate or restore is in flight."""
    running = get_running_panel_job()
    if running:
        raise PanelJobAlreadyRunning(running)
