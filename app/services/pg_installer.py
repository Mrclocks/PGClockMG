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
    """Always avoid interactive SSL menu when possible.

    - domain SSL → --ssl-domain (no menu)
    - no SSL → --no-ssl (no menu)
    - IP SSL → --no-ssl first, then configure IP cert after install
      (interactive IP menu is unreliable over pipes; post-setup is deterministic)
    """
    db = params["database"].strip().lower()
    want_ssl = str(params.get("ssl")).lower() in ("1", "true", "yes")
    domain = (params.get("domain") or "").strip()
    http_port = str(params.get("ssl_http_port") or "80").strip() or "80"

    cmd = ["bash", str(script), "install"]
    if db != "sqlite":
        cmd += ["--database", db]

    if want_ssl and domain:
        cmd += ["--ssl-domain", domain, "--ssl-http-port", http_port]
    else:
        # IP SSL handled after install; avoid hanging SSL menu entirely
        cmd += ["--no-ssl"]
    return cmd


def _strip_ansi(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
    text = re.sub(r"\x1b\].*?\x07", "", text)
    return text


def _norm(text: str) -> str:
    """Lowercase + normalize fancy apostrophes for matching."""
    t = _strip_ansi(text).lower()
    return t.replace("\u2019", "'").replace("\u2018", "'")


async def _configure_ip_ssl(job: MigrationJob, ip: str, http_port: str) -> None:
    """Issue Let's Encrypt IP cert after a --no-ssl install (non-interactive)."""
    job.set_progress(92, f"Configuring SSL for IP {ip}...")
    job.log(f"Post-install IP SSL setup for {ip} (challenge port {http_port})")

    # Use official script functions by sourcing after bootstrap, via a small driver.
    # Falls back to calling `pasarguard` env edits + acme if available.
    driver = Path("/tmp/pgclockmg-ip-ssl.sh")
    driver.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
IPV4="{ip}"
HTTP_PORT="{http_port}"
APP_DIR="/opt/pasarguard"
ENV_FILE="$APP_DIR/.env"
DATA_DIR="/var/lib/pasarguard"
CERT_DIR="$DATA_DIR/certs/ip"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "PasarGuard .env not found" >&2
  exit 1
fi

# Prefer official CLI helper if present after install
if command -v pasarguard >/dev/null 2>&1; then
  # Pull shared libs path used by installed script
  true
fi

# Install/ensure acme.sh
ACME_HOME="${{HOME:-/root}}/.acme.sh"
ACME_BIN=""
if [[ -x "$ACME_HOME/acme.sh" ]]; then
  ACME_BIN="$ACME_HOME/acme.sh"
elif [[ -x /root/.acme.sh/acme.sh ]]; then
  ACME_BIN="/root/.acme.sh/acme.sh"
fi

if [[ -z "$ACME_BIN" ]]; then
  curl -fsSL https://get.acme.sh | sh -s email=ssl@pasarguard.local || true
  if [[ -x /root/.acme.sh/acme.sh ]]; then
    ACME_BIN="/root/.acme.sh/acme.sh"
  elif [[ -x "$ACME_HOME/acme.sh" ]]; then
    ACME_BIN="$ACME_HOME/acme.sh"
  fi
fi

if [[ -z "$ACME_BIN" ]]; then
  echo "acme.sh not available — skipping IP SSL (panel installed without SSL)" >&2
  exit 0
fi

mkdir -p "$CERT_DIR"
"$ACME_BIN" --issue -d "$IPV4" --standalone --httpport "$HTTP_PORT" --force || {{
  echo "IP certificate issuance failed — panel remains without SSL" >&2
  exit 0
}}

"$ACME_BIN" --installcert -d "$IPV4" \\
  --fullchain-file "$CERT_DIR/fullchain.pem" \\
  --key-file "$CERT_DIR/privkey.pem" || true

if [[ ! -s "$CERT_DIR/fullchain.pem" || ! -s "$CERT_DIR/privkey.pem" ]]; then
  echo "Certificate files missing — continuing without SSL" >&2
  exit 0
fi

# Enable SSL in .env
set_env() {{
  local key="$1" val="$2"
  if grep -qE "^[[:space:]]*#?[[:space:]]*${{key}}=" "$ENV_FILE"; then
    sed -i -E "s|^[[:space:]]*#?[[:space:]]*${{key}}=.*|$key=\\"$val\\"|" "$ENV_FILE"
  else
    printf '\\n%s="%s"\\n' "$key" "$val" >> "$ENV_FILE"
  fi
}}

set_env UVICORN_SSL_CERTFILE "$CERT_DIR/fullchain.pem"
set_env UVICORN_SSL_KEYFILE "$CERT_DIR/privkey.pem"
set_env UVICORN_SSL_CA_TYPE "public"

cd "$APP_DIR"
docker compose up -d || true
echo "IP SSL configured for https://$IPV4"
""",
        encoding="utf-8",
    )
    driver.chmod(0o700)

    proc = await asyncio.create_subprocess_exec(
        "bash", str(driver),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out_b, _ = await proc.communicate()
    out = (out_b or b"").decode("utf-8", errors="replace")
    for line in out.splitlines():
        if line.strip():
            job.log(line)
    if proc.returncode not in (0, None):
        job.log(f"IP SSL setup exited {proc.returncode} — panel installed; SSL optional")


async def _install_pasarguard(job: MigrationJob, params: dict) -> dict:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        job.log("Warning: not running as root — install may fail")

    want_ssl = str(params.get("ssl")).lower() in ("1", "true", "yes")
    domain = (params.get("domain") or "").strip()
    ip = (params.get("ip") or "").strip()
    wipe_volumes = bool(params.get("wipe_volumes"))
    http_port = str(params.get("ssl_http_port") or "80").strip() or "80"
    want_ip_ssl = want_ssl and not domain and bool(ip)

    script = await _download_script(job)
    cmd = _build_cmd(script, params)
    job.log(f"$ {' '.join(cmd)}")
    if want_ip_ssl:
        job.log("IP SSL: install with --no-ssl first, then configure certificate (avoids interactive SSL menu hang)")
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
    rolling = ""  # last ~2KB for prompt detection (including no-newline prompts)

    async def send(line: str, reason: str) -> None:
        job.log(f"→ auto-answer ({reason}): {line!r}")
        assert proc.stdin is not None
        proc.stdin.write((line + "\n").encode())
        await proc.stdin.drain()

    async def maybe_answer() -> None:
        nonlocal finished_ok
        low = _norm(rolling[-1200:])

        # With --no-ssl / --ssl-domain we should NOT see SSL menu.
        # Keep handlers anyway for safety if script still asks.
        if "ssl_menu" not in answered and (
            "select ssl option" in low
            or "choose ssl setup method" in low
            or "must be reachable for let" in low
        ):
            # Should not happen on our preferred paths; pick safe answer
            choice = "1" if (want_ssl and domain) else ("2" if want_ip_ssl else "4")
            await send(choice, f"SSL menu fallback → {choice}")
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

        if "application startup complete" in low or "uvicorn running" in low:
            finished_ok = True

    assert proc.stdout is not None
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(512), timeout=1.0)
            except asyncio.TimeoutError:
                chunk = b""

            if chunk:
                text = chunk.decode("utf-8", errors="replace")
                pending += text
                rolling = (rolling + text)[-4000:]
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
                        job.set_progress(min(88, 10 + len(job.logs) // 3), line_clean[:120])

                await maybe_answer()
            else:
                await maybe_answer()
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

    # IP SSL after successful non-interactive install
    if want_ip_ssl:
        try:
            await _configure_ip_ssl(job, ip, http_port)
        except Exception as e:
            job.log(f"IP SSL setup warning: {e}")

    access = get_panel_access_info(prefer_host=domain or ip or None)
    access["database"] = params["database"]
    access["ssl_requested"] = want_ssl
    access["ssl_http_port"] = http_port
    access["node_skipped"] = True
    access["ip_ssl_deferred"] = want_ip_ssl
    job.log("PasarGuard installation complete")
    return access
