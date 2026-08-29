"""Self-uninstall PGClockMG (pg-migrator) from the server."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

INSTALL_DIR = Path("/opt/pg-migrator")
SERVICE_NAME = "pg-migrator"
BACKUP_SERVICE_NAME = "pg-backup"
UNIT_PATH = Path(f"/etc/systemd/system/{SERVICE_NAME}.service")
BACKUP_UNIT_PATH = Path(f"/etc/systemd/system/{BACKUP_SERVICE_NAME}.service")

_uninstall_tasks: set[asyncio.Task] = set()


def uninstall_preview() -> dict:
    return {
        "install_dir": str(INSTALL_DIR),
        "service": SERVICE_NAME,
        "backup_service": BACKUP_SERVICE_NAME,
        "exists": INSTALL_DIR.exists() or UNIT_PATH.exists() or BACKUP_UNIT_PATH.exists(),
        "commands": [
            f"systemctl stop {SERVICE_NAME} {BACKUP_SERVICE_NAME}",
            f"systemctl disable {SERVICE_NAME} {BACKUP_SERVICE_NAME}",
            f"rm -f {UNIT_PATH} {BACKUP_UNIT_PATH}",
            "systemctl daemon-reload",
            f"rm -rf {INSTALL_DIR}",
        ],
    }


async def schedule_self_uninstall(delay_sec: float = 2.0) -> dict:
    """Stop serving shortly after response, then remove service + files."""
    preview = uninstall_preview()
    task = asyncio.create_task(_do_uninstall(delay_sec))
    _uninstall_tasks.add(task)
    task.add_done_callback(_uninstall_tasks.discard)
    return {**preview, "scheduled": True, "delay_sec": delay_sec}


async def _do_uninstall(delay_sec: float) -> None:
    await asyncio.sleep(delay_sec)
    tmp_dir = Path(tempfile.mkdtemp(prefix="pgclockmg-uninstall-"))
    path = tmp_dir / "uninstall.sh"
    script = f"""#!/bin/bash
set -e
systemctl stop {SERVICE_NAME} 2>/dev/null || true
systemctl stop {BACKUP_SERVICE_NAME} 2>/dev/null || true
systemctl disable {SERVICE_NAME} 2>/dev/null || true
systemctl disable {BACKUP_SERVICE_NAME} 2>/dev/null || true
rm -f {UNIT_PATH} {BACKUP_UNIT_PATH}
systemctl daemon-reload 2>/dev/null || true
# Remove install dir last (this process may live under it)
nohup bash -c 'sleep 1; rm -rf {INSTALL_DIR}; rm -rf {tmp_dir}' >/dev/null 2>&1 &
"""
    path.write_text(script, encoding="utf-8")
    os.chmod(path, 0o700)
    # Detach so HTTP can finish
    await asyncio.create_subprocess_exec(
        "bash", str(path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
