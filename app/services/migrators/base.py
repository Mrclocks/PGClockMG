"""Base migration runner with logging."""

import asyncio
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Callable


class MigrationJob:
    def __init__(self, job_id: str | None = None):
        self.job_id = job_id or str(uuid.uuid4())[:8]
        self.status = "pending"
        self.progress = 0
        self.message = ""
        self.logs: list[str] = []
        self.result: dict | None = None
        self._callbacks: list[Callable] = []

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self.logs.append(line)
        for cb in self._callbacks:
            try:
                cb(line)
            except Exception:
                pass

    def set_progress(self, pct: int, msg: str = ""):
        """Update progress; never move backwards (avoids UI jumping 85→45→75)."""
        try:
            pct_i = int(pct)
        except (TypeError, ValueError):
            pct_i = self.progress
        pct_i = min(100, max(0, pct_i))
        if pct_i >= self.progress:
            self.progress = pct_i
        if msg:
            self.message = msg

    def on_log(self, callback: Callable):
        self._callbacks.append(callback)


class BaseMigrator(ABC):
    def __init__(self, job: MigrationJob, params: dict | None = None):
        self.job = job
        self.params = params or {}
        self.copy_report: dict | None = None

    @abstractmethod
    async def run(self, params: dict) -> dict:
        pass

    async def _run_cmd(
        self,
        cmd: list[str] | str,
        cwd: str | None = None,
        timeout: int = 600,
    ) -> tuple[bool, str]:
        """Run a command as argv list (exec) or shell string.

        ``db_auth`` probes pass shell strings (``cd ... && docker compose exec...``).
        Passing those to ``create_subprocess_exec`` iterates the string character-by-
        character and fails with FileNotFoundError — same shell support as restore.
        """
        if isinstance(cmd, str):
            self.job.log(f"$ {cmd}")
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
            )
        else:
            self.job.log(f"$ {' '.join(cmd)}")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
            )
        output_lines = []

        async def _drain_stdout() -> None:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                output_lines.append(text)
                self.job.log(text)

        try:
            # wait_for works on Python 3.9+; asyncio.timeout needs 3.11+
            await asyncio.wait_for(_drain_stdout(), timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
            return False, "Timeout"
        # stdout EOF does not always mean the process exited (e.g. hung fuser).
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (TimeoutError, asyncio.TimeoutError):
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except Exception:
                pass
            self.job.log("command hung after stdout closed — killed")
            return False, "Timeout"
        return proc.returncode == 0, "\n".join(output_lines)

    def _backup_file(self, path, backup_dir) -> str | None:
        from pathlib import Path
        import shutil
        p = Path(path)
        if not p.exists():
            return None
        dest = Path(backup_dir) / f"{p.name}.bak.{self.job.job_id}"
        shutil.copy2(p, dest)
        self.job.log(f"Backup: {p} -> {dest}")
        return str(dest)
