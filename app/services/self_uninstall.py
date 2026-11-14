"""Self-uninstall PGClockMG (pg-migrator) from the server.

Only the wizard is removed. The backup panel (``pg-backup``) keeps running and
any backups stored under the wizard install dir are copied to a safe location first.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

INSTALL_DIR = Path("/opt/pg-migrator")
SERVICE_NAME = "pg-migrator"
BACKUP_SERVICE_NAME = "pg-backup"
UNIT_PATH = Path(f"/etc/systemd/system/{SERVICE_NAME}.service")
KEEP_ROOT = Path("/var/lib/pgclockbackup")

_uninstall_tasks: set[asyncio.Task] = set()


def uninstall_preview() -> dict:
    return {
        "install_dir": str(INSTALL_DIR),
        "service": SERVICE_NAME,
        "backup_service": BACKUP_SERVICE_NAME,
        "backup_service_kept": True,
        "exists": INSTALL_DIR.exists() or UNIT_PATH.exists(),
        "commands": [
            f"cp -a {INSTALL_DIR}/backups/. {KEEP_ROOT}/pgclockmg-backups-<timestamp>/  (if not empty)",
            f"systemctl stop {SERVICE_NAME}",
            f"systemctl disable {SERVICE_NAME}",
            f"rm -f {UNIT_PATH}",
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
# Wizard only — never touch the pg-backup service or its install dir.
# Keep any backups stored under the wizard dir (e.g. pre-migration safety backup).
if [ -d "{INSTALL_DIR}/backups" ] && [ -n "$(ls -A "{INSTALL_DIR}/backups" 2>/dev/null)" ]; then
  keep="{KEEP_ROOT}/pgclockmg-backups-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$keep" 2>/dev/null && cp -a "{INSTALL_DIR}/backups/." "$keep/" 2>/dev/null || true
fi
systemctl stop {SERVICE_NAME} 2>/dev/null || true
systemctl disable {SERVICE_NAME} 2>/dev/null || true
rm -f {UNIT_PATH}
systemctl daemon-reload 2>/dev/null || true
# Legacy combined install: pg-backup still runs from the wizard dir — leave it in place.
if grep -qs '^WorkingDirectory={INSTALL_DIR}$' /etc/systemd/system/{BACKUP_SERVICE_NAME}.service; then
  nohup bash -c 'sleep 1; rm -rf {tmp_dir}' >/dev/null 2>&1 &
else
  # Remove install dir last (this process may live under it)
  nohup bash -c 'sleep 1; rm -rf {INSTALL_DIR}; rm -rf {tmp_dir}' >/dev/null 2>&1 &
fi
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
