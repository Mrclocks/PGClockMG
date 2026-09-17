"""Safe auto-heal helpers for Marzban → PasarGuard migration.

Policy (do not weaken completeness checks):

AUTO-HEAL (retry once, then hard-fail)
  - Transient Docker/DB readiness, panel start blips, asset copy collisions
  - Known auth mis-sync between .env and DB roles (PostgreSQL/MySQL)

DEGRADE (warn, continue — data already preserved elsewhere)
  - Inbound TLS relocate (handled in MarzbanMigrator)
  - Optional disable-nodes after success

HARD-FAIL (never soft-skip — incomplete transfer risk)
  - Missing source backup/DB, empty convert, copy gaps
  - users/hosts without inbounds or core_configs
  - Wrong/unsupported engine pairing, missing PasarGuard .env

Happy-path migrations that already succeed are unchanged: these helpers only
run after a failure is observed.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Callable

LogFn = Callable[[str], None]

_TRANSIENT_INFRA_RE = re.compile(
    r"(connection refused|connect.*timed out|temporary failure|"
    r"could not connect|server closed the connection|broken pipe|"
    r"no such container|is restarting|not ready|did not become ready|"
    r"i/o timeout|network is unreachable|name or service not known|"
    r"database is starting|too many connections|lock wait timeout|"
    r"password authentication failed|access denied for user|"
    r"operationalerror|psycopg2|can't connect)",
    re.IGNORECASE,
)


def is_transient_infra_error(exc: BaseException | str) -> bool:
    """True when a retry / service restart may recover without skipping data."""
    text = str(exc or "")
    return bool(_TRANSIENT_INFRA_RE.search(text))


def merge_copy_tree(src: Path, dst: Path) -> int:
    """Copy ``src`` into ``dst`` file-by-file (dirs_exist_ok semantics).

    Used when ``rmtree`` + ``copytree`` fails mid-way (busy mounts, FileExists).
    Returns number of files copied/updated.
    """
    src = Path(src)
    dst = Path(dst)
    if not src.is_dir():
        raise NotADirectoryError(f"merge_copy_tree source is not a directory: {src}")
    dst.mkdir(parents=True, exist_ok=True)
    copied = 0
    for root, _dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        target_root = dst / rel
        target_root.mkdir(parents=True, exist_ok=True)
        for name in files:
            s = Path(root) / name
            d = target_root / name
            if d.exists() and d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
            shutil.copy2(s, d)
            copied += 1
    return copied


def normalize_templates_layout(templates_dir: Path, log: LogFn | None = None) -> str:
    """Ensure ``templates/xray`` exists; safely fold ``v2ray`` into it.

    Returns an action tag for tests/logs: renamed | merged | kept | missing.
    Never raises for FileExists races — falls back to merge copy.
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    templates_dir = Path(templates_dir)
    if not templates_dir.is_dir():
        return "missing"
    v2ray = templates_dir / "v2ray"
    xray = templates_dir / "xray"
    if not v2ray.exists():
        return "kept" if xray.exists() else "missing"
    if not xray.exists():
        try:
            v2ray.rename(xray)
            _log("Renamed templates/v2ray → templates/xray")
            return "renamed"
        except OSError as exc:
            _log(f"templates rename collided ({exc}) — merging v2ray into xray")
            xray.mkdir(parents=True, exist_ok=True)
            merge_copy_tree(v2ray, xray)
            shutil.rmtree(v2ray, ignore_errors=True)
            return "merged"
    # Both exist: prefer keeping xray, fold any missing files from v2ray.
    merge_copy_tree(v2ray, xray)
    shutil.rmtree(v2ray, ignore_errors=True)
    _log("Merged templates/v2ray into templates/xray")
    return "merged"


def safe_replace_tree(src: Path, dst: Path, *, log: LogFn | None = None) -> str:
    """Replace ``dst`` with a copy of ``src``.

    Tries rmtree+copytree first (current happy path). On failure, merge-copies
    without aborting so panel certs/templates still land.
    Returns: replaced | merged | skipped_empty
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    src = Path(src)
    dst = Path(dst)
    if not src.exists():
        return "missing"
    if src.is_dir() and not any(src.iterdir()):
        _log(f"Skip empty {src.name}/ (keeping existing destination)")
        return "skipped_empty"
    try:
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        return "replaced"
    except OSError as exc:
        _log(f"copytree {src.name} failed ({exc}) — merge-copying instead")
        if src.is_dir():
            merge_copy_tree(src, dst)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        return "merged"
