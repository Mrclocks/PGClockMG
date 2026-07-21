"""Automated PasarGuard panel installation (answers official installer prompts)."""

from __future__ import annotations

import asyncio
import os
import re
import signal
import socket
import traceback
from pathlib import Path

from app.panels import PASARGUARD_INSTALL_DBS
from app.services.migrators.base import MigrationJob
from app.services.pg_access import get_panel_access_info
from app.services.prerequisites import is_pasarguard_installed

SCRIPT_URL = "https://github.com/PasarGuard/scripts/raw/main/pasarguard.sh"
_install_jobs: dict[str, MigrationJob] = {}

# Prompts from official pasarguard.sh install (matched on rolling buffer, even without newline)
PROMPT_RULES = [
    ("override", re.compile(r"override the previous installation\?\s*\(y/n\)\s*$", re.I)),
    ("volumes", re.compile(r"Delete volumes\?\s*\[y/N\]:\s*$", re.I)),
    ("ssl_menu", re.compile(r"Select SSL option\s*\[1-4\]\s*\(default:\s*1\):\s*$", re.I)),
    ("ssl_domain", re.compile(r"Enter domain for SSL certificate[^:]*:\s*$", re.I)),
    ("ssl_ipv4_default", re.compile(r"Enter IPv4 for SSL certificate\s*\(default:[^)]*\):\s*$", re.I)),
    ("ssl_ipv4", re.compile(r"Enter IPv4 for SSL certificate:\s*$", re.I)),
    ("ssl_ipv6", re.compile(r"Enter IPv6 for SSL certificate[^:]*:\s*$", re.I)),
    ("panel_port", re.compile(r"Enter a different port for PasarGuard[^:]*:\s*$", re.I)),
    ("install_node", re.compile(r"Do you want to install PasarGuard node\?\s*\(y/n\)\s*$", re.I)),
]


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
    http_port = str(params.get("ssl_http_port") or "80").strip()
    if want_ssl and (not http_port.isdigit() or not (1 <= int(http_port) <= 65535)):
        errors.append("Invalid SSL HTTP challenge port")
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
        err = str(e).strip() or "Installation failed"
        # Keep only a clean user-facing error (no traceback in message)
        job.status = "error"
        job.message = err
        job.log(f"ERROR: {err}")
        job.log(traceback.format_exc())
        job.result = {"error": err}


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
    """Build CLI matching official docs: --database + --ssl-domain / --ssl / --no-ssl."""
    db = params["database"].strip().lower()
    want_ssl = str(params.get("ssl")).lower() in ("1", "true", "yes")
    domain = (params.get("domain") or "").strip()
    http_port = str(params.get("ssl_http_port") or "80").strip() or "80"

    cmd = ["bash", str(script), "install"]
    if db != "sqlite":
        cmd += ["--database", db]

    if want_ssl and domain:
        # Fully non-interactive SSL path in official script
        cmd += ["--ssl-domain", domain, "--ssl-http-port", http_port]
    elif want_ssl:
        # IP SSL still needs menu option 2 — we auto-answer
        cmd += ["--ssl", "--ssl-http-port", http_port]
    else:
        cmd += ["--no-ssl"]
    return cmd


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)


async def _install_pasarguard(job: MigrationJob, params: dict) -> dict:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        job.log("Warning: not running as root — install may fail")

    want_ssl = str(params.get("ssl")).lower() in ("1", "true", "yes")
    domain = (params.get("domain") or "").strip()
    ip = (params.get("ip") or "").strip()
    wipe_volumes = bool(params.get("wipe_volumes"))
    # Domain SSL uses flags only — no SSL menu. IP SSL still needs menu.
    needs_ssl_menu = want_ssl and not domain

    script = await _download_script(job)
    cmd = _build_cmd(script, params)
    job.log(f"$ {' '.join(cmd)}")
    job.set_progress(5, "Starting official installer...")

    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    env["TERM"] = "dumb"
    env["PYTHONUNBUFFERED"] = "1"

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )

    answered: set[str] = set()
    finished_ok = False
    all_output: list[str] = []
    pending = ""  # incomplete line (read -p prompts live here)

    async def send(line: str, reason: str) -> None:
        job.log(f"→ auto-answer ({reason}): {line!r}")
        assert proc.stdin is not None
        proc.stdin.write((line + "\n").encode())
        await proc.stdin.drain()

    async def handle_prompts(window: str) -> None:
        nonlocal finished_ok
        clean = _strip_ansi(window)
        # Match against end of buffer (prompt usually at the end)
        tail = clean[-400:]

        for key, pattern in PROMPT_RULES:
            if key in answered:
                continue
            if not pattern.search(tail):
                continue

            if key == "override":
                if params.get("force"):
                    await send("y", "override install")
                    answered.add(key)
                else:
                    await send("n", "abort override")
                    answered.add(key)
                    raise RuntimeError("PasarGuard is already installed")

            elif key == "volumes":
                await send("y" if wipe_volumes else "n", "delete volumes")
                answered.add(key)

            elif key == "ssl_menu":
                if want_ssl and domain:
                    await send("1", "SSL domain")
                elif want_ssl:
                    await send("2", "SSL IP certificate")
                else:
                    await send("4", "No SSL")
                answered.add(key)

            elif key == "ssl_domain":
                await send(domain or "", "SSL domain value")
                answered.add(key)

            elif key in ("ssl_ipv4", "ssl_ipv4_default"):
                # Empty → accept detected default when prompt has default
                await send(ip if ip else "", "SSL IPv4")
                answered.add("ssl_ipv4")
                answered.add("ssl_ipv4_default")

            elif key == "ssl_ipv6":
                await send("", "skip IPv6")
                answered.add(key)

            elif key == "panel_port":
                free = _find_free_port(8001)
                await send(str(free), f"panel port → {free}")
                answered.add(key)

            elif key == "install_node":
                await send("n", "skip node install")
                answered.add(key)
                finished_ok = True

    assert proc.stdout is not None
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(256), timeout=1.0)
            except asyncio.TimeoutError:
                # Still check pending prompt while installer is idle waiting for input
                if pending:
                    await handle_prompts(pending)
                if proc.returncode is not None:
                    break
                if finished_ok:
                    break
                continue

            if not chunk:
                break

            text = chunk.decode("utf-8", errors="replace")
            pending += text
            all_output.append(text)

            # Flush complete lines to log
            while "\n" in pending or "\r" in pending:
                if "\r\n" in pending:
                    line, pending = pending.split("\r\n", 1)
                elif "\n" in pending:
                    line, pending = pending.split("\n", 1)
                else:
                    line, pending = pending.split("\r", 1)
                line = _strip_ansi(line).strip()
                if line:
                    job.log(line)
                    job.set_progress(min(90, 10 + len(job.logs) // 3), line[:120])
                    low = line.lower()
                    if "skipping node installation" in low:
                        finished_ok = True
                    if any(m in line for m in ("Application startup complete", "Uvicorn running")):
                        finished_ok = True

            # Critical: answer prompts that have no trailing newline yet
            await handle_prompts(pending)

            if finished_ok and "install_node" in answered:
                break
    finally:
        if finished_ok and proc.returncode is None:
            job.log("Install finished — stopping log follow")
            try:
                proc.send_signal(signal.SIGINT)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass

    try:
        await asyncio.wait_for(proc.wait(), timeout=45)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        await proc.wait()

    # Allow containers to settle
    await asyncio.sleep(2)

    if not is_pasarguard_installed():
        # Extract last meaningful error lines for the user
        blob = _strip_ansi("".join(all_output))
        err_lines = [
            ln.strip()
            for ln in blob.splitlines()
            if re.search(r"(error|failed|fatal|aborted)", ln, re.I)
        ]
        detail = err_lines[-3:] if err_lines else blob.strip().splitlines()[-5:]
        msg = "\n".join(detail).strip() or "PasarGuard install failed (panel not found at /opt/pasarguard)"
        raise RuntimeError(msg)

    # If process died early with error code and we never finished node prompt
    if proc.returncode not in (0, None, -signal.SIGINT, -2, 130) and not finished_ok:
        # 130 = 128+SIGINT; still OK if panel exists
        if not is_pasarguard_installed():
            raise RuntimeError(f"Installer exited with code {proc.returncode}")

    access = get_panel_access_info(prefer_host=domain or ip or None)
    access["database"] = params["database"]
    access["ssl_requested"] = want_ssl
    access["ssl_http_port"] = str(params.get("ssl_http_port") or "80")
    access["node_skipped"] = True
    access["needs_ssl_menu"] = needs_ssl_menu
    job.log("PasarGuard installation complete")
    return access
