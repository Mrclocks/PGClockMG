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

# Wizard only — never touches the pg-backup service. Backups stored under the wizard dir
# are MOVED (not copied) out first; the install dir is only deleted when that is certain.
_UNINSTALL_SCRIPT = r"""#!/bin/bash
SRC="__INSTALL_DIR__/backups"
if grep -Eqs "^WorkingDirectory=[\"']?__INSTALL_DIR__/?[\"']?[[:space:]]*$" __BACKUP_UNIT__; then
  # Legacy combined install: pg-backup still runs from the wizard dir and reads its
  # backups from there — do not move them and do not delete the dir.
  LEGACY=1
else
  LEGACY=0
  if [ -d "$SRC" ] && [ -n "$(ls -A "$SRC" 2>/dev/null)" ]; then
    KEEP="__KEEP_ROOT__/pgclockmg-backups-$(date +%Y%m%d-%H%M%S)"
    if ! { mkdir -p "$KEEP" && mv "$SRC" "$KEEP/backups" && [ -d "$KEEP/backups" ] && [ ! -e "$SRC" ]; }; then
      # Could not move the backups to a safe place — abort before touching anything.
      rm -rf __TMP__
      exit 1
    fi
  fi
fi
systemctl stop __SERVICE__ 2>/dev/null || true
systemctl disable __SERVICE__ 2>/dev/null || true
rm -f __UNIT__
systemctl daemon-reload 2>/dev/null || true
if [ "$LEGACY" = "1" ]; then
  nohup bash -c 'sleep 1; rm -rf __TMP__' >/dev/null 2>&1 &
else
  # Remove install dir last (this process may live under it)
  nohup bash -c 'sleep 1; rm -rf __INSTALL_DIR__; rm -rf __TMP__' >/dev/null 2>&1 &
fi
"""


def uninstall_preview() -> dict:
    return {
        "install_dir": str(INSTALL_DIR),
        "service": SERVICE_NAME,
        "backup_service": BACKUP_SERVICE_NAME,
        "backup_service_kept": True,
        "exists": INSTALL_DIR.exists() or UNIT_PATH.exists(),
        "commands": [
            f"mv {INSTALL_DIR}/backups {KEEP_ROOT}/pgclockmg-backups-<timestamp>/backups  (if not empty and the backup panel does not run from this dir; uninstall is aborted if this fails)",
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
    script = _UNINSTALL_SCRIPT
    for placeholder, value in (
        ("__INSTALL_DIR__", str(INSTALL_DIR)),
        ("__KEEP_ROOT__", str(KEEP_ROOT)),
        ("__SERVICE__", SERVICE_NAME),
        ("__UNIT__", str(UNIT_PATH)),
        ("__BACKUP_UNIT__", f"/etc/systemd/system/{BACKUP_SERVICE_NAME}.service"),
        ("__TMP__", str(tmp_dir)),
    ):
        script = script.replace(placeholder, value)
    path.write_text(script, encoding="utf-8")
    os.chmod(path, 0o700)
    # Detach so HTTP can finish
    await asyncio.create_subprocess_exec(
        "bash", str(path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
