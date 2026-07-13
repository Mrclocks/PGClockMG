"""PasarGuard database migration between DB engines using official db-migrations tool."""

from pathlib import Path
import shutil

from app.config import PASARGUARD_DIR, PASARGUARD_DATA, PASARGUARD_ENV, BACKUP_DIR
from app.services.migrators.base import BaseMigrator
from app.services.env_migration import transform_pasarguard_env_for_target
from app.services.native_migration import run_cross_db_migration
from app.services.pasarguard_ops import safe_start_pasarguard, docker_compose_up, resolve_db_service


class PasarguardDbMigrator(BaseMigrator):
    async def run(self, params: dict) -> dict:
        source_db = params["source_db"]
        target_db = params["target_db"]
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

        if source_db != target_db:
            self.job.set_progress(30, f"Two-phase cross-DB: {source_db} → {target_db}...")
            await self._ensure_target_database_stack(target_db)
            await run_cross_db_migration(self, str(source_path), source_db, target_db)
        elif source_db == target_db and source_db == "sqlite":
            self.job.set_progress(40, "Replacing SQLite database...")
            dest = PASARGUARD_DATA / "db.sqlite3"
            PASARGUARD_DATA.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, dest)
        elif source_db == target_db:
            from app.services.db_migration import run_db_migration
            self.job.set_progress(40, f"Refreshing {source_db} database...")
            await run_db_migration(self, str(source_path), source_db, target_db)

        self.job.set_progress(75, "Updating PasarGuard .env...")
        await self._update_pasarguard_env(target_db)

        self.job.set_progress(90, "Starting PasarGuard...")
        await safe_start_pasarguard(self)

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

    async def _ensure_target_database_stack(self, target_db: str):
        if target_db == "sqlite":
            return
        svc = resolve_db_service(target_db)
        if svc:
            self.job.log(f"Starting {svc} container...")
            await docker_compose_up(self, [svc])
            import asyncio
            await asyncio.sleep(8)

    async def _update_pasarguard_env(self, target_db: str):
        if not PASARGUARD_ENV.exists():
            return
        from app.services.db_credentials import get_target_connection
        self._backup_file(PASARGUARD_ENV, BACKUP_DIR)
        text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
        conn = get_target_connection(self.params)
        pwd = conn.get("password")
        PASARGUARD_ENV.write_text(
            transform_pasarguard_env_for_target(text, target_db, pwd),
            encoding="utf-8",
        )
        self.job.log(".env updated for target database")

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
