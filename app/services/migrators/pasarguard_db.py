"""PasarGuard database migration between DB engines using official db-migrations tool."""

from pathlib import Path

from app.config import PASARGUARD_DIR, PASARGUARD_DATA, PASARGUARD_ENV, TOOLS_DIR, BACKUP_DIR
from app.services.migrators.base import BaseMigrator


class PasarguardDbMigrator(BaseMigrator):
    async def run(self, params: dict) -> dict:
        source_db = params["source_db"]
        target_db = params["target_db"]
        password = params.get("source_db_password") or params.get("target_db_password")
        upload_path = params.get("upload_path")

        self.job.set_progress(5, "بررسی PasarGuard...")

        if not PASARGUARD_DIR.exists():
            raise RuntimeError("PasarGuard نصب نیست")

        db_migrations = TOOLS_DIR / "db-migrations"
        if not db_migrations.exists():
            raise RuntimeError("ابزار db-migrations یافت نشد")

        self.job.set_progress(15, "تعیین مسیر دیتابیس مبدأ...")
        source_path = upload_path or self._detect_source_path(source_db)
        if not source_path or not Path(source_path).exists():
            raise RuntimeError(f"دیتابیس مبدأ ({source_db}) یافت نشد — بکاپ آپلود کنید")

        if source_db != "sqlite" and Path(source_path).suffix == ".sql":
            source_arg = str(source_path)
        else:
            source_arg = str(source_path)

        self.job.set_progress(25, "بکاپ‌گیری از دیتابیس فعلی...")
        self._backup_current_db(source_db)

        target_url = self._build_target_url(target_db, password)
        self.job.log(f"هدف: {target_db}")

        self.job.set_progress(40, "اجرای مهاجرت دیتابیس...")
        ok, out = await self._run_cmd(
            ["uv", "run", "migrations/universal.py",
             "--config", self._write_config(source_arg, source_db, target_url, target_db)],
            cwd=str(db_migrations),
            timeout=1800,
        )

        if not ok:
            # Try direct CLI mode
            ok, out = await self._run_cmd(
                ["./migrate.sh", source_db, "--to", target_db, "--db", target_url],
                cwd=str(db_migrations),
                timeout=1800,
            )

        if not ok:
            raise RuntimeError(f"مهاجرت دیتابیس ناموفق:\n{out}")

        self.job.set_progress(75, "به‌روزرسانی .env PasarGuard...")
        await self._update_pasarguard_env(target_db, password)

        self.job.set_progress(90, "راه‌اندازی مجدد PasarGuard...")
        await self._run_cmd(["pasarguard", "restart"])

        self.job.set_progress(100, "مهاجرت دیتابیس انجام شد!")
        return {
            "panel_url": self._get_panel_url(),
            "subscription_preserved": True,
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

    def _write_config(self, source_path: str, source_db: str, target_url: str, target_db: str) -> str:
        config_path = BACKUP_DIR / f"migrate-{self.job.job_id}.yml"
        source_type = "postgres" if source_db in ("postgresql", "timescaledb") else source_db
        if source_type == "timescaledb":
            source_type = "postgres"
        target_type = "postgres" if target_db in ("postgresql", "timescaledb") else target_db
        if target_type == "timescaledb":
            target_type = "postgres"

        is_file = Path(source_path).exists() and not source_path.startswith("mysql://")
        source_block = f'  path: "{source_path}"' if is_file else f'  url: "{source_path}"'

        config = f"""source:
  type: "{source_type}"
{source_block}

target:
  type: "{target_type}"
  url: "{target_url}"

exclude_tables:
  - admin_usage_logs
  - user_usage_logs
  - node_stats
"""
        config_path.write_text(config, encoding="utf-8")
        return str(config_path)

    def _build_target_url(self, db_type: str, password: str | None) -> str:
        pwd = password or "password"
        if db_type == "sqlite":
            return f"sqlite:///{PASARGUARD_DATA}/db.sqlite3"
        if db_type in ("mysql", "mariadb"):
            return f"mysql+pymysql://root:{pwd}@127.0.0.1:3306/pasarguard"
        return f"postgresql+asyncpg://postgres:{pwd}@localhost:5432/pasarguard"

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
        self.job.log(".env به‌روزرسانی شد")

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
