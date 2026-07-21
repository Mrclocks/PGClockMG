"""Automated PasarGuard panel installation (answers official installer prompts)."""

from __future__ import annotations

import asyncio
import os
import re
import socket
import traceback
from pathlib import Path

from app.panels import PASARGUARD_INSTALL_DBS
from app.services.migrators.base import MigrationJob
from app.services.pg_access import get_panel_access_info
from app.services.prerequisites import is_pasarguard_installed

SCRIPT_URL = "https://github.com/PasarGuard/scripts/raw/main/pasarguard.sh"
_install_jobs: dict[str, MigrationJob] = {}


def get_install_job(job_id: str) -> MigrationJob | None:
    return _install_jobs.get(job_id)


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def _find_free_port(start: int = 8000) -> int:
    for p in range(start, min(start + 200, 65535)):
        if _port_free(p):
            return p
    return start


def validate_install_request(params: dict) -> list[str]:
    errors: list[str] = []
    db = (params.get("database") or "").strip().lower()
    if db not in PASARGUARD_INSTALL_DBS:
        errors.append(f"Unsupported database: {db}")
    ssl = params.get("ssl")
    if ssl not in (True, False, "yes", "no", "true", "false", 1, 0):
        errors.append("ssl must be yes/no")
    want_ssl = str(ssl).lower() in ("1", "true", "yes")
    domain = (params.get("domain") or "").strip()
    ip = (params.get("ip") or "").strip()
    if want_ssl and not domain and not ip:
        errors.append("Provide domain or IP when SSL is enabled")
    if domain and not re.match(
        r"^([A-Za-z0-9](-*[A-Za-z0-9])*\.)+[A-Za-z]{2,}$", domain
    ):
        errors.append(f"Invalid domain: {domain}")
    if ip and not re.match(r"^(\d{1,3}\.){3}\d{1,3}$", ip):
        errors.append(f"Invalid IPv4: {ip}")
    return errors


async def start_pasarguard_install(params: dict) -> MigrationJob:
    errors = validate_install_request(params)
    if errors:
        raise ValueError("; ".join(errors))
    if is_pasarguard_installed() and not params.get("force"):
        raise ValueError("PasarGuard is already installed")

    job = MigrationJob()
    _install_jobs[job.job_id] = job
    asyncio.create_task(_run_install(job, params))
    return job


async def _run_install(job: MigrationJob, params: dict) -> None:
    job.status = "running"
    try:
        result = await _install_pasarguard(job, params)
        job.result = result
        job.status = "success"
        job.set_progress(100, "PasarGuard installed")
    except Exception as e:
        job.status = "error"
        job.message = str(e)
        job.log(f"ERROR: {e}")
        job.log(traceback.format_exc())
        job.result = {"error": str(e)}


async def _download_script(job: MigrationJob) -> Path:
    dest = Path("/tmp/pgclockmg-pasarguard-install.sh")
    job.log(f"Downloading official installer: {SCRIPT_URL}")
    proc = await asyncio.create_subprocess_exec(
        "curl", "-fsSL", SCRIPT_URL, "-o", str(dest),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0 or not dest.exists():
        raise RuntimeError(f"Failed to download installer: {(out or b'').decode()[-500:]}")
    dest.chmod(0o700)
    return dest


def _build_cmd(script: Path, params: dict) -> list[str]:
    db = params["database"].strip().lower()
    want_ssl = str(params.get("ssl")).lower() in ("1", "true", "yes")
    domain = (params.get("domain") or "").strip()
    cmd = ["bash", str(script), "install"]
    if db != "sqlite":
        cmd += ["--database", db]
    if want_ssl and domain:
        cmd += ["--ssl-domain", domain]
    elif want_ssl:
        cmd += ["--ssl"]  # interactive SSL menu → we answer IP option
    else:
        cmd += ["--no-ssl"]
    return cmd


async def _install_pasarguard(job: MigrationJob, params: dict) -> dict:
    if os.geteuid() != 0 if hasattr(os, "geteuid") else False:
        job.log("Warning: not running as root — install may fail")

    want_ssl = str(params.get("ssl")).lower() in ("1", "true", "yes")
    domain = (params.get("domain") or "").strip()
    ip = (params.get("ip") or "").strip()
    wipe_volumes = bool(params.get("wipe_volumes"))

    script = await _download_script(job)
    cmd = _build_cmd(script, params)
    job.log(f"$ {' '.join(cmd)}")
    job.set_progress(5, "Starting official installer...")

    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    env["TERM"] = "xterm"

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )

    buf = ""
    answered_ssl = False
    answered_node = False
    answered_override = False
    answered_volumes = False
    answered_port = False
    finished_ok = False

    async def send(line: str, reason: str) -> None:
        job.log(f"→ auto-answer ({reason}): {line!r}")
        assert proc.stdin is not None
        proc.stdin.write((line + "\n").encode())
        await proc.stdin.drain()

    assert proc.stdout is not None
    while True:
        chunk = await proc.stdout.read(256)
        if not chunk:
            break
        text = chunk.decode("utf-8", errors="replace")
        buf += text
        while "\n" in buf or "\r" in buf:
            if "\n" in buf:
                line, buf = buf.split("\n", 1)
            else:
                line, buf = buf.split("\r", 1)
            line = line.strip("\r")
            if line.strip():
                job.log(line)
                pct = min(90, 10 + len(job.logs) // 3)
                job.set_progress(pct, line[:120])

            low = line.lower()

            if (not answered_override) and "override the previous installation" in low:
                await send("y" if params.get("force") else "n", "override install")
                answered_override = True
                if not params.get("force"):
                    raise RuntimeError("PasarGuard already installed — aborted override")

            if (not answered_volumes) and "delete volumes?" in low:
                await send("y" if wipe_volumes else "n", "delete volumes")
                answered_volumes = True

            if (not answered_ssl) and "select ssl option" in low:
                if want_ssl and domain:
                    await send("1", "SSL domain")
                elif want_ssl:
                    await send("2", "SSL IP certificate")
                else:
                    await send("4", "No SSL")
                answered_ssl = True

            if want_ssl and not domain and "enter ipv4 for ssl" in low:
                await send(ip, "SSL IPv4")

            if want_ssl and not domain and "enter ipv6 for ssl" in low:
                await send("", "skip IPv6")

            if want_ssl and domain and "enter domain for ssl" in low:
                await send(domain, "SSL domain value")

            if (not answered_port) and "enter a different port for pasarguard" in low:
                free = _find_free_port(8001)
                await send(str(free), f"port conflict → {free}")
                answered_port = True

            if (not answered_node) and "install pasarguard node" in low:
                await send("n", "skip node install")
                answered_node = True

            # Official installer ends by following logs — stop once healthy
            if any(
                m in line
                for m in (
                    "Application startup complete",
                    "Uvicorn running",
                    "Skipping node installation",
                )
            ):
                if answered_node or "Skipping node installation" in line:
                    finished_ok = True
                    job.log("Install finished — stopping log follow")
                    try:
                        proc.send_signal(__import__("signal").SIGINT)
                    except Exception:
                        proc.terminate()
                    break

    try:
        await asyncio.wait_for(proc.wait(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()

    if not is_pasarguard_installed():
        raise RuntimeError(
            "Installer exited but PasarGuard was not detected at /opt/pasarguard"
        )

    # Give containers a moment
    await asyncio.sleep(3)
    access = get_panel_access_info(prefer_host=domain or ip or None)
    access["database"] = params["database"]
    access["ssl_requested"] = want_ssl
    access["node_skipped"] = True
    access["finished_ok"] = finished_ok or is_pasarguard_installed()
    job.log("PasarGuard installation complete")
    return access
