"""PasarGuard database migration between DB engines using official db-migrations tool."""

from pathlib import Path

from app.config import PASARGUARD_DIR, PASARGUARD_DATA, PASARGUARD_ENV, BACKUP_DIR
from app.services.migrators.base import BaseMigrator
from app.services.db_migration import run_db_migration


class PasarguardDbMigrator(BaseMigrator):
    async def run(self, params: dict) -> dict:
        source_db = params["source_db"]
        target_db = params["target_db"]
        password = params.get("source_db_password") or params.get("target_db_password")
        upload_path = params.get("upload_path")

        self.job.set_progress(5, "Checking PasarGuard installation...")

        if not PASARGUARD_DIR.exists():
            raise RuntimeError("PasarGuard is not installed on this server")

        self.job.set_progress(15, "Locating source database...")
        source_path = upload_path or self._detect_source_path(source_db)
        if not source_path or not Path(source_path).exists():
            raise RuntimeError(f"Source database ({source_db}) not found — upload a backup")

        self.job.set_progress(25, "Backing up current database...")
        self._backup_current_db(source_db)

        self.job.set_progress(40, f"Running db-migrations: {source_db} → {target_db}...")
        await run_db_migration(self, str(source_path), source_db, target_db, password)

        self.job.set_progress(75, "Updating PasarGuard .env...")
        await self._update_pasarguard_env(target_db, password)

        self.job.set_progress(90, "Restarting PasarGuard...")
        await self._run_cmd(["pasarguard", "restart"])

        self.job.set_progress(100, "Database migration completed")
        return {
            "panel_url": self._get_panel_url(),
            "subscription_mode": "native",
            "method": f"{source_db} → {target_db}",
        }

    def _detect_source_path(self, db_type: str) -> str | None:
        if db_type == "sqlite":
            p = PASARGUARD_DATA / "db.sqlite3"
            return str(p) if p.exists() else None
        return None

    def _backup_current_db(self, db_type: str):
        if db_type == "sqlite":
            p = PASARGUARD_DATA / "db.sqlite3"
            if p.exists():
                self._backup_file(p, BACKUP_DIR)

    async def _update_pasarguard_env(self, target_db: str, password: str | None):
        import re
        if not PASARGUARD_ENV.exists():
            return
        self._backup_file(PASARGUARD_ENV, BACKUP_DIR)
        text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
        pwd = password or "password"

        if target_db == "sqlite":
            new_url = 'SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite:////var/lib/pasarguard/db.sqlite3"'
        elif target_db in ("mysql", "mariadb"):
            new_url = f'SQLALCHEMY_DATABASE_URL = "mysql+asyncmy://root:{pwd}@127.0.0.1/pasarguard"'
        else:
            new_url = f'SQLALCHEMY_DATABASE_URL = "postgresql+asyncpg://postgres:{pwd}@localhost:5432/pasarguard"'

        if "SQLALCHEMY_DATABASE_URL" in text:
            text = re.sub(r'#?\s*SQLALCHEMY_DATABASE_URL\s*=\s*"[^"]*"', new_url, text)
        else:
            text += f"\n{new_url}\n"

        PASARGUARD_ENV.write_text(text, encoding="utf-8")
        self.job.log(".env updated")

    def _get_panel_url(self) -> str:
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            ip = "SERVER_IP"
        return f"https://{ip}:8000/dashboard/"
