"""Check GitHub releases and apply in-place updates for the backup panel."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

from app.config import BACKUP_HOME

log = logging.getLogger("pgclockmg.backup_updater")

GITHUB_OWNER = "Mrclocks"
GITHUB_REPO = "PGClockMG"
GITHUB_API_LATEST = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
GITHUB_REPO_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}.git"
SERVICE_NAME = os.environ.get("PG_BACKUP_SERVICE", "pg-backup")

_LOCK = threading.RLock()
_CHECK_CACHE: dict[str, Any] = {"at": 0.0, "payload": None}
_CHECK_TTL_SEC = 300.0
_UPDATE_JOB: dict[str, Any] | None = None


def parse_version(raw: str | None) -> tuple[int, ...]:
    s = (raw or "").strip().lstrip("vV")
    parts: list[int] = []
    for piece in re.split(r"[.\-+_]", s):
        if not piece:
            continue
        m = re.match(r"^(\d+)", piece)
        if m:
            parts.append(int(m.group(1)))
        else:
            break
    return tuple(parts) if parts else (0,)


def version_lt(current: str, latest: str) -> bool:
    return parse_version(current) < parse_version(latest)


def _normalize_tag(tag: str | None) -> str:
    t = (tag or "").strip()
    if not t:
        return ""
    return t if t.startswith("v") else f"v{t.lstrip('vV')}"


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "User-Agent": "PGClockMG-Backup-Updater",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _release_payload(data: dict, *, current: str) -> dict:
    tag = _normalize_tag(data.get("tag_name") or data.get("name") or "")
    body = (data.get("body") or "").strip()
    return {
        "ok": True,
        "current": current,
        "latest": tag.lstrip("v") if tag else "",
        "latest_tag": tag,
        "available": bool(tag and version_lt(current, tag)),
        "name": data.get("name") or tag,
        "body": body,
        "html_url": data.get("html_url") or f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases",
        "published_at": data.get("published_at") or data.get("created_at"),
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def peek_cached_update(current: str) -> dict | None:
    """Return a fresh in-memory update cache entry, or None."""
    now = time.time()
    with _LOCK:
        cached = _CHECK_CACHE.get("payload")
        if (
            cached
            and (now - float(_CHECK_CACHE.get("at") or 0)) < _CHECK_TTL_SEC
            and (cached.get("current") == current)
        ):
            return dict(cached)
    return None


def schedule_background_update_check(current: str) -> None:
    """Refresh the update cache without blocking the request thread."""
    def _run() -> None:
        try:
            check_for_update(current=current, force=False, timeout=6.0)
        except Exception:
            log.exception("background update check failed")

    threading.Thread(target=_run, name="backup-update-check", daemon=True).start()


def check_for_update(*, current: str, force: bool = False, timeout: float = 20.0) -> dict:
    """Return latest GitHub release compared to the running backup panel version."""
    now = time.time()
    with _LOCK:
        cached = _CHECK_CACHE.get("payload")
        if (
            not force
            and cached
            and (now - float(_CHECK_CACHE.get("at") or 0)) < _CHECK_TTL_SEC
            and (cached.get("current") == current)
        ):
            return dict(cached)

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(GITHUB_API_LATEST, headers=_headers())
            if resp.status_code == 404:
                payload = {
                    "ok": True,
                    "current": current,
                    "latest": current,
                    "latest_tag": _normalize_tag(current),
                    "available": False,
                    "name": current,
                    "body": "",
                    "html_url": f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases",
                    "published_at": None,
                    "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "error": None,
                }
            else:
                resp.raise_for_status()
                payload = _release_payload(resp.json(), current=current)
    except Exception as exc:
        payload = {
            "ok": False,
            "current": current,
            "latest": None,
            "latest_tag": None,
            "available": False,
            "name": None,
            "body": "",
            "html_url": f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases",
            "published_at": None,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "error": str(exc),
        }

    with _LOCK:
        _CHECK_CACHE["at"] = now
        _CHECK_CACHE["payload"] = dict(payload)
    return payload


def get_update_job() -> dict | None:
    with _LOCK:
        return dict(_UPDATE_JOB) if _UPDATE_JOB else None


def _set_update_job(**fields: Any) -> dict:
    global _UPDATE_JOB
    with _LOCK:
        job = dict(_UPDATE_JOB or {})
        job.update(fields)
        job.setdefault("logs", [])
        _UPDATE_JOB = job
        return dict(job)


def _append_log(msg: str) -> None:
    global _UPDATE_JOB
    with _LOCK:
        job = dict(_UPDATE_JOB or {})
        logs = list(job.get("logs") or [])
        logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        job["logs"] = logs[-200:]
        _UPDATE_JOB = job


def _run(cmd: list[str], *, cwd: str | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _schedule_restart() -> bool:
    """Restart the backup systemd unit after the HTTP response can flush."""

    def _restart():
        time.sleep(1.8)
        try:
            r = _run(["systemctl", "restart", SERVICE_NAME], timeout=60)
            if r.returncode != 0:
                log.error("systemctl restart failed: %s", (r.stderr or r.stdout or "")[:500])
        except Exception:
            log.exception("failed to restart %s", SERVICE_NAME)

    if not shutil.which("systemctl"):
        _append_log("systemctl not found — restart the pg-backup service manually")
        return False
    threading.Thread(target=_restart, name="pg-backup-restart", daemon=True).start()
    return True


def _find_app_dir(extracted_root: Path) -> Path:
    """GitHub zipball extracts to a single top-level folder containing app/."""
    direct = extracted_root / "app"
    if direct.is_dir():
        return direct
    for child in sorted(extracted_root.iterdir()):
        if child.is_dir() and (child / "app").is_dir():
            return child / "app"
    raise RuntimeError("release archive missing app/")


def _download_release_archive(tag: str, dest_zip: Path, *, timeout: float = 120.0) -> None:
    """Download release sources as zip (preferred over git clone — works without git)."""
    urls = [
        f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/zipball/{tag}",
        f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/archive/refs/tags/{tag}.zip",
    ]
    last_err: Exception | None = None
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for url in urls:
            try:
                _append_log(f"Downloading {tag}…")
                with client.stream("GET", url, headers=_headers()) as resp:
                    if resp.status_code >= 400:
                        raise RuntimeError(f"HTTP {resp.status_code} for {url}")
                    with open(dest_zip, "wb") as fh:
                        for chunk in resp.iter_bytes(chunk_size=1024 * 64):
                            if chunk:
                                fh.write(chunk)
                if dest_zip.stat().st_size < 1000:
                    raise RuntimeError("downloaded archive too small")
                return
            except Exception as exc:
                last_err = exc
                _append_log(f"Download failed via {url.split('/')[2]} — retrying…")
                dest_zip.unlink(missing_ok=True)
    raise RuntimeError(f"download_failed: {last_err}")


def _extract_zip(archive: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "r") as zf:
        zf.extractall(dest_dir)
    return _find_app_dir(dest_dir)


def _clone_release_fallback(tag: str, src: Path) -> Path:
    """Last-resort git clone when zip download is unavailable."""
    if not shutil.which("git"):
        raise RuntimeError("git_not_found_and_zip_download_failed")
    _append_log("Falling back to git clone…")
    clone = _run(
        ["git", "clone", "--depth", "1", "--branch", tag, GITHUB_REPO_URL, str(src)],
        timeout=180,
    )
    if clone.returncode != 0:
        shutil.rmtree(src, ignore_errors=True)
        clone = _run(
            ["git", "clone", "--depth", "50", GITHUB_REPO_URL, str(src)],
            timeout=180,
        )
        if clone.returncode != 0:
            raise RuntimeError((clone.stderr or clone.stdout or "git_clone_failed")[:800])
        co = _run(["git", "checkout", tag], cwd=str(src), timeout=60)
        if co.returncode != 0:
            raise RuntimeError((co.stderr or co.stdout or "git_checkout_failed")[:800])
    app_dir = src / "app"
    if not app_dir.is_dir():
        raise RuntimeError("release missing app/")
    return app_dir


def apply_update(*, current: str, target_tag: str | None = None) -> dict:
    """
    Pull the target release into BACKUP_HOME, replace app/, refresh deps, restart service.
    Preserves backup_panel/, backups/, logs/, and venv/.

    Returns immediately with a running job; work continues in a background thread so the
    HTTP handler (and progress polling) are never blocked on GitHub I/O.
    """
    global _UPDATE_JOB
    with _LOCK:
        if _UPDATE_JOB and _UPDATE_JOB.get("status") in ("running", "queued"):
            return dict(_UPDATE_JOB)

        job = {
            "ok": True,
            "status": "running",
            "current": current,
            "target": _normalize_tag(target_tag) or "",
            "progress": 5,
            "logs": [f"[{time.strftime('%H:%M:%S')}] Update started…"],
            "restart_scheduled": False,
            "error": None,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "finished_at": None,
        }
        _UPDATE_JOB = dict(job)

    def _worker():
        tmp: Path | None = None
        try:
            home = Path(BACKUP_HOME)
            app_dst = home / "app"
            if not app_dst.is_dir():
                raise RuntimeError(f"app directory missing: {app_dst}")

            _set_update_job(progress=12)
            _append_log("Resolving target release…")
            info = check_for_update(current=current, force=True, timeout=15.0)
            if info.get("error") and not info.get("latest_tag") and not target_tag:
                raise RuntimeError(f"release_check_failed: {info.get('error')}")

            tag = _normalize_tag(target_tag or info.get("latest_tag") or "")
            if not tag:
                raise RuntimeError("no_release_found")
            _set_update_job(target=tag, progress=18)

            if not version_lt(current, tag) and not target_tag:
                _append_log("Already up to date")
                _set_update_job(
                    status="success",
                    progress=100,
                    updated=False,
                    message="already_up_to_date",
                    finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                )
                return

            tmp = Path(tempfile.mkdtemp(prefix="pg-backup-update-", dir="/tmp"))
            archive = tmp / "release.zip"
            extract_dir = tmp / "extracted"
            src_app: Path | None = None

            _set_update_job(progress=25)
            try:
                _download_release_archive(tag, archive)
                _set_update_job(progress=40)
                _append_log("Extracting archive…")
                src_app = _extract_zip(archive, extract_dir)
            except Exception as dl_exc:
                _append_log(f"Zip download failed ({dl_exc})")
                src = tmp / "src"
                src_app = _clone_release_fallback(tag, src)

            assert src_app is not None
            _set_update_job(progress=50)
            _append_log("Replacing application files…")

            staging = tmp / "app-new"
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            shutil.copytree(src_app, staging)

            req_src = src_app.parent / "requirements.txt"
            if not req_src.is_file() and extract_dir.exists():
                for cand in extract_dir.glob("*/requirements.txt"):
                    req_src = cand
                    break

            replaced = home / "app.next"
            if replaced.exists():
                shutil.rmtree(replaced, ignore_errors=True)
            shutil.move(str(staging), str(replaced))
            old = home / "app.old"
            if old.exists():
                shutil.rmtree(old, ignore_errors=True)
            shutil.move(str(app_dst), str(old))
            shutil.move(str(replaced), str(app_dst))
            shutil.rmtree(old, ignore_errors=True)

            if req_src.is_file():
                shutil.copy2(req_src, home / "requirements.txt")

            _set_update_job(progress=70)
            _append_log("Refreshing Python dependencies…")
            venv_pip = home / "venv" / "bin" / "pip"
            if venv_pip.is_file() and (home / "requirements.txt").is_file():
                pip = _run(
                    [str(venv_pip), "install", "-r", str(home / "requirements.txt"), "-q"],
                    cwd=str(home),
                    timeout=400,
                )
                if pip.returncode != 0:
                    raise RuntimeError((pip.stderr or pip.stdout or "pip_install_failed")[:800])
            else:
                _append_log("venv/pip not found — skipped dependency refresh")

            _set_update_job(progress=90)
            _append_log("Scheduling service restart…")
            restarted = _schedule_restart()
            _append_log("Update applied" + (" — restarting…" if restarted else ""))
            _set_update_job(
                status="success",
                progress=100,
                restart_scheduled=restarted,
                finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                updated=True,
            )
            with _LOCK:
                _CHECK_CACHE["payload"] = None
                _CHECK_CACHE["at"] = 0.0
        except Exception as exc:
            log.exception("backup panel update failed")
            _append_log(f"ERROR: {exc}")
            _set_update_job(
                ok=False,
                status="error",
                error=str(exc),
                progress=100,
                finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
        finally:
            if tmp and tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)

    threading.Thread(target=_worker, name="pg-backup-update", daemon=True).start()
    return get_update_job() or job
