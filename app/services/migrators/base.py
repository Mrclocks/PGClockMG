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
        self.progress = min(100, max(0, pct))
        if msg:
            self.message = msg

    def on_log(self, callback: Callable):
        self._callbacks.append(callback)


class BaseMigrator(ABC):
    def __init__(self, job: MigrationJob):
        self.job = job

    @abstractmethod
    async def run(self, params: dict) -> dict:
        pass

    async def _run_cmd(self, cmd: list[str], cwd: str | None = None, timeout: int = 600) -> tuple[bool, str]:
        self.job.log(f"$ {' '.join(cmd)}")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
        )
        output_lines = []
        try:
            async with asyncio.timeout(timeout):
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").rstrip()
                    output_lines.append(text)
                    self.job.log(text)
        except TimeoutError:
            proc.kill()
            return False, "Timeout"
        await proc.wait()
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
