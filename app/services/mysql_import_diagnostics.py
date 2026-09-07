"""Diagnostics + safe helpers for MySQL/MariaDB dump import (compose exec path).

Design rules for migration safety:
- Advisory RAM preflight never blocks an import.
- SESSION preamble uses only widely-supported session toggles (same ones
  mysqldump itself emits); it dies with the import connection.
- Docker probes are best-effort and must not abort healthy imports.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MysqlImportKind = Literal["killed", "auth", "sql", "client", "unknown"]
MysqlImportRamLevel = Literal["ok", "info", "warn"]

# SESSION-only. No GLOBAL. No sql_log_bin / flush / sql_mode changes.
# These match what official mysqldump headers typically set for bulk load.
MYSQL_IMPORT_SESSION_PREAMBLE = (
    "-- PGClockMG MySQL import session preamble (SESSION scope only)\n"
    "SET SESSION FOREIGN_KEY_CHECKS=0;\n"
    "SET SESSION UNIQUE_CHECKS=0;\n"
)

_MI = 1024 * 1024
_GI = 1024 * _MI

# docker/mysql client often surfaces 128+signal; asyncio may also report -signal.
_SIGKILL_CODES = {137, -9, 128 + 9}
_SIGTERM_CODES = {143, -15, 128 + 15}

_AUTH_HINTS = (
    "access denied",
    "authentication failed",
    "password: no",
    "using password: yes",
    "error 1045",
    "error 1698",
    "error 1044",
)

_SQL_HINTS = (
    "error 1064",
    "error 1146",
    "error 1054",
    "error 1005",
    "error 1215",
    "error 1452",
    "syntax error",
)

_CONN_HINTS = (
    "can't connect",
    "can not connect",
    "error 2002",
    "error 2003",
    "connection refused",
    "gone away",
    "server has gone away",
    "not allowed to connect",
)


async def _communicate_timeout(
    proc: asyncio.subprocess.Process,
    timeout: float,
) -> tuple[bytes, bytes]:
    """communicate() with hard timeout; kill the probe so Docker CLI cannot leak."""
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except Exception:
            pass
        raise


@dataclass(frozen=True)
class MysqlImportFailure:
    kind: MysqlImportKind
    exit_code: int | None
    summary: str
    guidance: str


@dataclass(frozen=True)
class MysqlImportRamAdvice:
    """Advisory only — callers must log, never raise/abort on this."""

    level: MysqlImportRamLevel
    dump_bytes: int
    mem_available_bytes: int | None
    message: str


def mysql_import_session_preamble_sql() -> str:
    return MYSQL_IMPORT_SESSION_PREAMBLE


def write_mysql_import_stdin_file(dump_path: Path, dest: Path) -> Path:
    """Stream ``preamble + dump`` into ``dest`` without loading the dump in RAM.

    Keeps the caller's ``stdin=file`` import path identical to before.
    """
    dump_path = Path(dump_path)
    dest = Path(dest)
    if not dump_path.is_file():
        raise FileNotFoundError(f"MySQL dump not found: {dump_path}")
    preamble = mysql_import_session_preamble_sql().encode("utf-8")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as out, dump_path.open("rb") as inp:
        out.write(preamble)
        while True:
            chunk = inp.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    return dest


def read_mem_available_bytes() -> int | None:
    """Best-effort MemAvailable from /proc; None if unreadable."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
    except Exception:
        return None
    return None


def assess_mysql_import_ram(
    dump_bytes: int,
    mem_available_bytes: int | None = None,
) -> MysqlImportRamAdvice:
    """Compare dump size to free RAM. Never means 'abort migration'."""
    dump_bytes = max(0, int(dump_bytes or 0))
    if mem_available_bytes is None:
        mem_available_bytes = read_mem_available_bytes()

    dump_mb = dump_bytes / _MI
    if mem_available_bytes is None:
        return MysqlImportRamAdvice(
            "info",
            dump_bytes,
            None,
            f"MySQL import preflight: dump {dump_mb:.1f} MiB; free RAM unknown "
            f"(continuing — advisory only).",
        )

    free_mb = mem_available_bytes / _MI
    # Soft thresholds only. Small dumps on healthy hosts stay at ok/info.
    if mem_available_bytes < 256 * _MI:
        return MysqlImportRamAdvice(
            "warn",
            dump_bytes,
            mem_available_bytes,
            f"MySQL import preflight: low free RAM ({free_mb:.0f} MiB) while importing "
            f"{dump_mb:.1f} MiB dump. Continuing anyway; if import exits 137, add RAM/swap "
            f"or check DB container OOM.",
        )
    # Large dump relative to free RAM (8x headroom heuristic).
    if dump_bytes >= 50 * _MI and mem_available_bytes < max(512 * _MI, 8 * dump_bytes):
        return MysqlImportRamAdvice(
            "warn",
            dump_bytes,
            mem_available_bytes,
            f"MySQL import preflight: dump {dump_mb:.1f} MiB is large vs free RAM "
            f"({free_mb:.0f} MiB). Continuing anyway; watch for exit 137 / OOMKilled.",
        )
    if dump_bytes > mem_available_bytes:
        return MysqlImportRamAdvice(
            "warn",
            dump_bytes,
            mem_available_bytes,
            f"MySQL import preflight: dump ({dump_mb:.1f} MiB) exceeds free RAM "
            f"({free_mb:.0f} MiB). Continuing anyway; failure is more likely under memory pressure.",
        )
    if mem_available_bytes < 1 * _GI and dump_bytes >= 100 * _MI:
        return MysqlImportRamAdvice(
            "info",
            dump_bytes,
            mem_available_bytes,
            f"MySQL import preflight: dump {dump_mb:.1f} MiB with {free_mb:.0f} MiB free RAM "
            f"(continuing).",
        )
    return MysqlImportRamAdvice(
        "ok",
        dump_bytes,
        mem_available_bytes,
        f"MySQL import preflight: dump {dump_mb:.1f} MiB, free RAM {free_mb:.0f} MiB — ok.",
    )


def classify_mysql_import_failure(
    exit_code: int | None,
    output: str = "",
    *,
    container_died: bool = False,
    oom_killed: bool | None = None,
) -> MysqlImportFailure:
    """Map exit code + client output to a stable failure kind (no I/O)."""
    text = (output or "").strip()
    low = text.lower()

    if container_died or exit_code in _SIGKILL_CODES or exit_code in _SIGTERM_CODES:
        if oom_killed is True:
            summary = (
                f"MySQL/MariaDB import was killed "
                f"(exit {exit_code if exit_code is not None else 'unknown'}; "
                f"container OOMKilled=true)."
            )
            guidance = (
                "The database container ran out of memory mid-import. "
                "This is not a DB password problem. Free RAM, add swap, or "
                "raise the container memory limit, then retry "
                "(the migrator recreates the target DB before each import)."
            )
        elif container_died:
            summary = (
                f"MySQL/MariaDB container stopped during dump import "
                f"(exit {exit_code if exit_code is not None else 'unknown'})."
            )
            guidance = (
                "The compose DB service exited while mysql was reading the dump. "
                "Check container logs and host disk space. "
                "Not a DB password problem — retry after the DB service stays up."
            )
        else:
            summary = (
                f"MySQL/MariaDB import process was killed "
                f"(exit {exit_code if exit_code is not None else 'unknown'}; SIGKILL/SIGTERM)."
            )
            guidance = (
                "Exit 137/143 usually means the mysql client or DB container was "
                "SIGKILL'd (OOM, docker restart, or host killer) — not bad credentials. "
                "Inspect compose DB logs and `dmesg` for OOM, then retry."
            )
        return MysqlImportFailure("killed", exit_code, summary, guidance)

    if any(h in low for h in _AUTH_HINTS):
        return MysqlImportFailure(
            "auth",
            exit_code,
            f"MySQL/MariaDB rejected credentials during dump import (exit {exit_code}).",
            "Verify MYSQL_ROOT_PASSWORD / DB user against /opt/pasarguard/.env "
            "and that the compose mysql/mariadb service is the one being targeted.",
        )

    if any(h in low for h in _CONN_HINTS):
        return MysqlImportFailure(
            "client",
            exit_code,
            f"Could not reach MySQL/MariaDB during dump import (exit {exit_code}).",
            "The DB service may have restarted or is not listening inside the container. "
            "Check compose ps/logs; this is usually not a wrong password.",
        )

    if any(h in low for h in _SQL_HINTS):
        return MysqlImportFailure(
            "sql",
            exit_code,
            f"MySQL/MariaDB reported an SQL error during dump import (exit {exit_code}).",
            "The dump may be truncated, incompatible, or partially rewritten. "
            "Check the SQL error in the output below; a clean retry recreates the target DB first.",
        )

    if exit_code not in (None, 0):
        return MysqlImportFailure(
            "client",
            exit_code,
            f"Failed to import MySQL dump into PasarGuard (exit {exit_code}).",
            "See mysql client output and DB container logs. "
            "If the service crashed mid-import, fix that before retrying.",
        )

    return MysqlImportFailure(
        "unknown",
        exit_code,
        "Failed to import MySQL dump into PasarGuard.",
        "See output and DB container logs.",
    )


def format_mysql_import_error(
    failure: MysqlImportFailure,
    *,
    output_tail: str = "",
    diag_tail: str = "",
) -> str:
    """Build a single RuntimeError message; keep it readable in the job UI."""
    parts = [failure.summary, failure.guidance]
    out = (output_tail or "").strip()
    if out:
        # Password-on-CLI warning alone is noise for killed/auth classification.
        useful = [
            ln for ln in out.splitlines()
            if ln.strip()
            and "using a password on the command line interface can be insecure" not in ln.lower()
        ]
        if useful:
            parts.append("mysql client output:\n" + "\n".join(useful[-40:]))
        elif failure.kind != "killed":
            parts.append("mysql client output:\n" + "\n".join(out.splitlines()[-10:]))
    diag = (diag_tail or "").strip()
    if diag:
        parts.append("container diagnostics:\n" + diag[-1200:])
    return "\n".join(parts)


async def compose_service_running(cwd: str, service: str) -> bool | None:
    """True if up, False if definitely down, None if probe failed (do not abort)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "compose", "ps", "-q", service,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out_b, _ = await _communicate_timeout(proc, 12)
    except Exception:
        return None
    cid = (out_b or b"").decode("utf-8", errors="replace").strip()
    if not cid:
        # Empty ps -q usually means not running; confirm with ps -a before False.
        try:
            proc_a = await asyncio.create_subprocess_exec(
                "docker", "compose", "ps", "-a", "--format", "{{.State}}", service,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            st_b, _ = await _communicate_timeout(proc_a, 12)
            state = (st_b or b"").decode("utf-8", errors="replace").strip().lower()
            if not state:
                return None
            if state.startswith("running"):
                return True
            return False
        except Exception:
            return None

    try:
        insp = await asyncio.create_subprocess_exec(
            "docker", "inspect", "-f", "{{.State.Running}}", cid.splitlines()[0],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        run_b, _ = await _communicate_timeout(insp, 12)
        val = (run_b or b"").decode("utf-8", errors="replace").strip().lower()
        if val == "true":
            return True
        if val == "false":
            return False
        return None
    except Exception:
        return None


async def compose_service_oom_killed(cwd: str, service: str) -> bool | None:
    """Best-effort OOMKilled flag for the compose service container."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "compose", "ps", "-a", "-q", service,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out_b, _ = await _communicate_timeout(proc, 12)
        cid = (out_b or b"").decode("utf-8", errors="replace").strip().splitlines()
        if not cid:
            return None
        insp = await asyncio.create_subprocess_exec(
            "docker", "inspect", "-f", "{{.State.OOMKilled}}", cid[0],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        flag_b, _ = await _communicate_timeout(insp, 12)
        flag = (flag_b or b"").decode("utf-8", errors="replace").strip().lower()
        if flag == "true":
            return True
        if flag == "false":
            return False
        return None
    except Exception:
        return None


async def compose_service_diagnostics(cwd: str, service: str, *, log_tail: int = 60) -> str:
    """Best-effort status + log snippet for error messages. Never raises."""
    chunks: list[str] = []
    try:
        ps = await asyncio.create_subprocess_exec(
            "docker", "compose", "ps", "-a", "--format",
            "{{.Name}} {{.Status}}", service,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        ps_b, _ = await _communicate_timeout(ps, 15)
        ps_text = (ps_b or b"").decode("utf-8", errors="replace").strip()
        if ps_text:
            chunks.append(f"compose ps: {ps_text}")
    except Exception as exc:
        chunks.append(f"compose ps: (unavailable: {type(exc).__name__})")

    oom = await compose_service_oom_killed(cwd, service)
    if oom is True:
        chunks.append("OOMKilled: true")
    elif oom is False:
        chunks.append("OOMKilled: false")

    try:
        logs = await asyncio.create_subprocess_exec(
            "docker", "compose", "logs", "--no-color", "--tail", str(log_tail), service,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        log_b, _ = await _communicate_timeout(logs, 20)
        log_text = (log_b or b"").decode("utf-8", errors="replace").strip()
        if log_text:
            chunks.append(log_text[-1000:])
    except Exception as exc:
        chunks.append(f"compose logs: (unavailable: {type(exc).__name__})")

    return "\n".join(chunks).strip()
