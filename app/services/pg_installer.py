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
    db = params["database"].strip().lower()
    want_ssl = str(params.get("ssl")).lower() in ("1", "true", "yes")
    domain = (params.get("domain") or "").strip()
    http_port = str(params.get("ssl_http_port") or "80").strip() or "80"

    cmd = ["bash", str(script), "install"]
    if db != "sqlite":
        cmd += ["--database", db]

    if want_ssl and domain:
        # Non-interactive SSL path in official script (no menu)
        cmd += ["--ssl-domain", domain, "--ssl-http-port", http_port]
    elif want_ssl:
        # Hits SSL menu — we pre-answer option 2 as soon as Port 80 note appears
        cmd += ["--ssl", "--ssl-http-port", http_port]
    else:
        cmd += ["--no-ssl"]
    return cmd


def _strip_ansi(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
    text = re.sub(r"\x1b\].*?\x07", "", text)
    return text


def _ssl_choice(want_ssl: bool, domain: str) -> str:
    if want_ssl and domain:
        return "1"
    if want_ssl:
        return "2"
    return "4"


def _looks_like_ssl_menu(text: str) -> bool:
    low = _strip_ansi(text).lower()
    return any(
        s in low
        for s in (
            "select ssl option",
            "choose ssl setup method",
            "must be reachable for let's encrypt",
            "must be reachable for let",
            "1) let's encrypt domain",
            "2) let's encrypt ip",
            "4) no ssl",
        )
    )


async def _install_pasarguard(job: MigrationJob, params: dict) -> dict:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        job.log("Warning: not running as root — install may fail")

    want_ssl = str(params.get("ssl")).lower() in ("1", "true", "yes")
    domain = (params.get("domain") or "").strip()
    ip = (params.get("ip") or "").strip()
    wipe_volumes = bool(params.get("wipe_volumes"))
    ssl_pick = _ssl_choice(want_ssl, domain)

    script = await _download_script(job)
    cmd = _build_cmd(script, params)
    job.log(f"$ {' '.join(cmd)}")
    job.set_progress(5, "Starting official installer...")

    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    env["TERM"] = "xterm"
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
    pending = ""
    idle_rounds = 0

    async def send(line: str, reason: str) -> None:
        job.log(f"→ auto-answer ({reason}): {line!r}")
        assert proc.stdin is not None
        proc.stdin.write((line + "\n").encode())
        await proc.stdin.drain()

    async def maybe_answer(text: str) -> None:
        nonlocal finished_ok
        low = _strip_ansi(text).lower()

        # CRITICAL: answer SSL as soon as Port-80 / menu text appears.
        # Official order: print "Port 80 ... Let's Encrypt." THEN read -p.
        # Sending now fills stdin so `read` never blocks.
        if "ssl_menu" not in answered and _looks_like_ssl_menu(text):
            await send(ssl_pick, f"SSL menu → option {ssl_pick}")
            answered.add("ssl_menu")

        if "override" not in answered and "override the previous installation" in low:
            if params.get("force"):
                await send("y", "override install")
                answered.add("override")
            else:
                await send("n", "abort override")
                answered.add("override")
                raise RuntimeError("PasarGuard is already installed")

        if "volumes" not in answered and "delete volumes?" in low:
            await send("y" if wipe_volumes else "n", "delete volumes")
            answered.add("volumes")

        if "ssl_domain" not in answered and "enter domain for ssl" in low:
            await send(domain or "", "SSL domain")
            answered.add("ssl_domain")

        if "ssl_ipv4" not in answered and "enter ipv4 for ssl" in low:
            await send(ip if ip else "", "SSL IPv4")
            answered.add("ssl_ipv4")

        if "ssl_ipv6" not in answered and "enter ipv6 for ssl" in low:
            await send("", "skip IPv6")
            answered.add("ssl_ipv6")

        if "panel_port" not in answered and "enter a different port for pasarguard" in low:
            free = _find_free_port(8001)
            await send(str(free), f"panel port → {free}")
            answered.add("panel_port")

        if "install_node" not in answered and "install pasarguard node" in low:
            await send("n", "skip node install")
            answered.add("install_node")
            finished_ok = True

        if "skipping node installation" in low:
            finished_ok = True
            answered.add("install_node")

        if any(m in text for m in ("Application startup complete", "Uvicorn running")):
            finished_ok = True

    assert proc.stdout is not None
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(512), timeout=1.0)
            except asyncio.TimeoutError:
                chunk = b""

            if chunk:
                idle_rounds = 0
                text = chunk.decode("utf-8", errors="replace")
                pending += text
                all_output.append(text)

                while "\n" in pending or "\r" in pending:
                    if "\r\n" in pending:
                        line, pending = pending.split("\r\n", 1)
                    elif "\n" in pending:
                        line, pending = pending.split("\n", 1)
                    else:
                        line, pending = pending.split("\r", 1)
                    line_clean = _strip_ansi(line).strip()
                    if line_clean:
                        job.log(line_clean)
                        job.set_progress(min(90, 10 + len(job.logs) // 3), line_clean[:120])
                        await maybe_answer(line_clean)

                if pending.strip():
                    await maybe_answer(pending)
            else:
                idle_rounds += 1
                if pending.strip():
                    await maybe_answer(pending)

                # Force SSL answer if stuck near the known hang point
                recent = _strip_ansi("".join(all_output[-30:]) + pending).lower()
                if (
                    "ssl_menu" not in answered
                    and idle_rounds >= 2
                    and (
                        "port 80" in recent
                        or "let's encrypt" in recent
                        or "ssl option" in recent
                        or "ssl setup method" in recent
                    )
                ):
                    await send(ssl_pick, f"SSL menu (force) → option {ssl_pick}")
                    answered.add("ssl_menu")

                if proc.returncode is not None:
                    break
                if finished_ok and "install_node" in answered:
                    break

            if finished_ok and "install_node" in answered:
                break
            if not chunk and proc.returncode is not None:
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

    await asyncio.sleep(2)

    if not is_pasarguard_installed():
        blob = _strip_ansi("".join(all_output))
        err_lines = [
            ln.strip()
            for ln in blob.splitlines()
            if re.search(r"(error|failed|fatal|aborted)", ln, re.I)
        ]
        detail = err_lines[-3:] if err_lines else blob.strip().splitlines()[-5:]
        msg = "\n".join(detail).strip() or "PasarGuard install failed (panel not found at /opt/pasarguard)"
        raise RuntimeError(msg)

    if proc.returncode not in (0, None, -signal.SIGINT, -2, 130) and not finished_ok:
        if not is_pasarguard_installed():
            raise RuntimeError(f"Installer exited with code {proc.returncode}")

    access = get_panel_access_info(prefer_host=domain or ip or None)
    access["database"] = params["database"]
    access["ssl_requested"] = want_ssl
    access["ssl_http_port"] = str(params.get("ssl_http_port") or "80")
    access["node_skipped"] = True
    access["ssl_menu_option"] = ssl_pick
    job.log("PasarGuard installation complete")
    return access
