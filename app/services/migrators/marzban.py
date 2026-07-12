"""Marzban → PasarGuard migration (official in-place method)."""

import asyncio
import re
import shutil
from pathlib import Path

from app.config import (
    MARZBAN_DIR, MARZBAN_DATA, PASARGUARD_DIR, PASARGUARD_DATA,
    PASARGUARD_ENV, BACKUP_DIR, TOOLS_DIR,
)
from app.services.migrators.base import BaseMigrator


class MarzbanMigrator(BaseMigrator):
    async def run(self, params: dict) -> dict:
        source_db = params["source_db"]
        target_db = params["target_db"]
        password = params.get("source_db_password") or params.get("target_db_password")
        upload_path = params.get("upload_path")
        install_pg = params.get("pasarguard_install", False)

        self.job.set_progress(5, "بررسی وضعیت سیستم...")

        marzban_exists = MARZBAN_DIR.exists() or MARZBAN_DATA.exists()
        pg_exists = PASARGUARD_DIR.exists()

        if upload_path:
            return await self._migrate_from_upload(upload_path, source_db, target_db, password, install_pg)
        elif marzban_exists and not pg_exists:
            return await self._migrate_inplace(source_db, target_db, password)
        elif marzban_exists and pg_exists:
            return await self._migrate_to_existing_pg(source_db, target_db, password)
        else:
            raise RuntimeError(
                "Marzban یافت نشد. لطفاً Marzban را نصب کنید یا فایل بکاپ (zip/sql/sqlite) آپلود کنید."
            )

    async def _migrate_inplace(self, source_db: str, target_db: str, password: str | None) -> dict:
        """Official in-place migration: rename marzban → pasarguard."""
        self.job.set_progress(10, "توقف سرویس Marzban...")
        await self._run_cmd(["docker", "compose", "down"], cwd=str(MARZBAN_DIR))

        self.job.set_progress(15, "پاکسازی مسیرهای قدیمی PasarGuard...")
        for p in [PASARGUARD_DIR, PASARGUARD_DATA, Path("/var/lib/mysql/pasarguard")]:
            if p.exists():
                shutil.rmtree(p)
                self.job.log(f"حذف شد: {p}")

        self.job.set_progress(25, "تغییر نام دایرکتوری‌ها...")
        if MARZBAN_DIR.exists():
            MARZBAN_DIR.rename(PASARGUARD_DIR)
            self.job.log(f"تغییر نام: {MARZBAN_DIR} → {PASARGUARD_DIR}")

        if MARZBAN_DATA.exists():
            MARZBAN_DATA.rename(PASARGUARD_DATA)
            self.job.log(f"تغییر نام: {MARZBAN_DATA} → {PASARGUARD_DATA}")

        mysql_marzban = Path("/var/lib/mysql/marzban")
        mysql_pg = Path("/var/lib/mysql/pasarguard")
        if mysql_marzban.exists():
            mysql_marzban.rename(mysql_pg)
            self.job.log("تغییر نام دیتابیس MySQL")

        self.job.set_progress(40, "به‌روزرسانی فایل .env...")
        await self._update_env(source_db, target_db, password)

        self.job.set_progress(55, "به‌روزرسانی docker-compose.yml...")
        await self._update_compose()

        self.job.set_progress(60, "به‌روزرسانی xray_config و templates...")
        await self._update_xray_paths()

        if source_db in ("mysql", "mariadb") and source_db != target_db:
            self.job.set_progress(65, "مهاجرت دیتابیس MySQL...")
            await self._migrate_mysql_db(password)
        elif source_db == "sqlite" and target_db != "sqlite":
            self.job.set_progress(65, "مهاجرت از SQLite به دیتابیس دیگر...")
            await self._migrate_sqlite_to_other(target_db, password)

        self.job.set_progress(80, "نصب اسکریپت PasarGuard...")
        await self._install_pasarguard_script()

        self.job.set_progress(90, "راه‌اندازی PasarGuard...")
        ok, _ = await self._run_cmd(["pasarguard", "restart"])
        if not ok:
            await self._run_cmd(["docker", "compose", "up", "-d"], cwd=str(PASARGUARD_DIR))

        self.job.set_progress(100, "مهاجرت Marzban با موفقیت انجام شد!")
        return {
            "panel_url": self._get_panel_url(),
            "subscription_preserved": True,
            "method": "in-place",
        }

    async def _migrate_from_upload(
        self, upload_path: str, source_db: str, target_db: str,
        password: str | None, install_pg: bool,
    ) -> dict:
        self.job.set_progress(10, "استخراج فایل بکاپ...")
        upload = Path(upload_path)
        work_dir = BACKUP_DIR / self.job.job_id
        work_dir.mkdir(parents=True, exist_ok=True)

        if upload.suffix == ".zip":
            ok, _ = await self._run_cmd(
                ["unzip", "-o", str(upload), "-d", str(work_dir)]
            )
            if not ok:
                raise RuntimeError("خطا در استخراج فایل zip")
        else:
            shutil.copy2(upload, work_dir / upload.name)

        # Find database file
        db_file = self._find_db_in_dir(work_dir, source_db)
        if not db_file:
            raise RuntimeError(f"فایل دیتابیس {source_db} در بکاپ یافت نشد")

        self.job.set_progress(30, "نصب PasarGuard...")
        if not PASARGUARD_DIR.exists() or install_pg:
            await self._install_pasarguard(target_db, password)

        self.job.set_progress(50, "وارد کردن دیتابیس...")
        if source_db == "sqlite":
            dest = PASARGUARD_DATA / "db.sqlite3"
            PASARGUARD_DATA.mkdir(parents=True, exist_ok=True)
            self._backup_file(dest, BACKUP_DIR)
            shutil.copy2(db_file, dest)
            await self._update_env("sqlite", target_db, password)
        elif source_db in ("mysql", "mariadb"):
            await self._import_mysql_dump(db_file, password)
            await self._update_env(source_db, target_db, password)

        self.job.set_progress(90, "راه‌اندازی PasarGuard...")
        await self._run_cmd(["pasarguard", "restart"])

        self.job.set_progress(100, "مهاجرت از بکاپ انجام شد!")
        return {
            "panel_url": self._get_panel_url(),
            "subscription_preserved": True,
            "method": "upload",
        }

    async def _migrate_to_existing_pg(self, source_db: str, target_db: str, password: str | None) -> dict:
        """When both Marzban and PG exist — use db-migrations tool."""
        self.job.set_progress(20, "هر دو پنل نصب هستند — استفاده از ابزار db-migrations...")
        db_migrations = TOOLS_DIR / "db-migrations"
        if not db_migrations.exists():
            raise RuntimeError("ابزار db-migrations یافت نشد — install.sh را دوباره اجرا کنید")

        source_path = self._get_marzban_db_path(source_db)
        if not source_path or not Path(source_path).exists():
            raise RuntimeError("دیتابیس Marzban یافت نشد")

        target_url = self._build_target_url(target_db, password)
        ok, out = await self._run_cmd(
            ["uv", "run", "migrations/universal.py",
             "--source", str(source_path),
             "--target-url", target_url],
            cwd=str(db_migrations),
            timeout=1200,
        )
        if not ok:
            raise RuntimeError(f"مهاجرت دیتابیس ناموفق: {out}")

        await self._run_cmd(["pasarguard", "restart"])
        self.job.set_progress(100, "مهاجرت دیتابیس انجام شد!")
        return {
            "panel_url": self._get_panel_url(),
            "subscription_preserved": True,
            "method": "db-migrations",
        }

    def _find_db_in_dir(self, directory: Path, db_type: str) -> Path | None:
        if db_type == "sqlite":
            for name in ("db.sqlite3", "marzban.db", "database.db"):
                for p in directory.rglob(name):
                    return p
        else:
            for p in directory.rglob("*.sql"):
                return p
        return None

    def _get_marzban_db_path(self, db_type: str) -> str | None:
        if db_type == "sqlite":
            p = MARZBAN_DATA / "db.sqlite3"
            return str(p) if p.exists() else None
        return None

    async def _update_env(self, source_db: str, target_db: str, password: str | None):
        env_path = PASARGUARD_DIR / ".env"
        if not env_path.exists():
            raise RuntimeError(".env یافت نشد")

        self._backup_file(env_path, BACKUP_DIR)
        text = env_path.read_text(encoding="utf-8", errors="ignore")

        text = text.replace("/var/lib/marzban", "/var/lib/pasarguard")
        text = text.replace("marzban", "pasarguard")

        if "V2RAY_SUBSCRIPTION_TEMPLATE" in text:
            text = text.replace("V2RAY_SUBSCRIPTION_TEMPLATE", "XRAY_SUBSCRIPTION_TEMPLATE")
            text = text.replace("v2ray/", "xray/")

        # Update SQL driver
        if target_db == "sqlite":
            text = re.sub(
                r'SQLALCHEMY_DATABASE_URL\s*=\s*"[^"]*"',
                'SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite:////var/lib/pasarguard/db.sqlite3"',
                text,
            )
        elif target_db in ("mysql", "mariadb"):
            pwd = password or "MYSQL_ROOT_PASSWORD"
            driver = "asyncmy"
            text = re.sub(
                r'SQLALCHEMY_DATABASE_URL\s*=\s*"[^"]*"',
                f'SQLALCHEMY_DATABASE_URL = "mysql+{driver}://root:{pwd}@127.0.0.1/pasarguard"',
                text,
            )
        elif target_db in ("postgresql", "timescaledb"):
            pwd = password or "DB_PASSWORD"
            text = re.sub(
                r'SQLALCHEMY_DATABASE_URL\s*=\s*"[^"]*"',
                f'SQLALCHEMY_DATABASE_URL = "postgresql+asyncpg://postgres:{pwd}@localhost:5432/pasarguard"',
                text,
            )

        env_path.write_text(text, encoding="utf-8")
        self.job.log(".env به‌روزرسانی شد")

    async def _update_compose(self):
        compose_path = PASARGUARD_DIR / "docker-compose.yml"
        if not compose_path.exists():
            return
        self._backup_file(compose_path, BACKUP_DIR)
        text = compose_path.read_text(encoding="utf-8", errors="ignore")
        text = text.replace("gozargah/marzban", "pasarguard/panel")
        text = text.replace("/var/lib/marzban", "/var/lib/pasarguard")
        text = text.replace("marzban:", "pasarguard:")
        text = text.replace("/var/lib/mysql/marzban", "/var/lib/mysql/pasarguard")
        text = text.replace("MYSQL_DATABASE: marzban", "MYSQL_DATABASE: pasarguard")
        compose_path.write_text(text, encoding="utf-8")
        self.job.log("docker-compose.yml به‌روزرسانی شد")

    async def _update_xray_paths(self):
        templates_v2ray = PASARGUARD_DATA / "templates" / "v2ray"
        templates_xray = PASARGUARD_DATA / "templates" / "xray"
        if templates_v2ray.exists() and not templates_xray.exists():
            templates_v2ray.rename(templates_xray)
            self.job.log("templates/v2ray → templates/xray")

        xray_config = PASARGUARD_DATA / "xray_config.json"
        if xray_config.exists():
            self._backup_file(xray_config, BACKUP_DIR)
            text = xray_config.read_text(encoding="utf-8", errors="ignore")
            text = text.replace("/var/lib/marzban", "/var/lib/pasarguard")
            xray_config.write_text(text, encoding="utf-8")

    async def _migrate_mysql_db(self, password: str | None):
        pwd = password or ""
        service = "mysql"
        compose_dir = str(PASARGUARD_DIR)

        await self._run_cmd(["docker", "compose", "up", "-d", service], cwd=compose_dir)
        await asyncio.sleep(5)

        dump_path = PASARGUARD_DIR / "marzban.sql"
        ok, _ = await self._run_cmd([
            "docker", "compose", "exec", "-T", service,
            "mysqldump", "-u", "root", f"-p{pwd}", "-h", "127.0.0.1",
            "--databases", "marzban",
        ], cwd=compose_dir)
        # Export via shell redirect
        proc = await asyncio.create_subprocess_shell(
            f'cd {compose_dir} && docker compose exec -T {service} '
            f'mysqldump -u root -p"{pwd}" -h 127.0.0.1 --databases marzban > "{dump_path}"',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        await proc.wait()

        if dump_path.exists():
            text = dump_path.read_text(encoding="utf-8", errors="ignore")
            lines = text.split("\n")
            new_lines = []
            for line in lines:
                if line.startswith("CREATE DATABASE"):
                    line = line.replace("marzban", "pasarguard")
                elif line.startswith("USE "):
                    line = line.replace("marzban", "pasarguard")
                new_lines.append(line)
            dump_path.write_text("\n".join(new_lines), encoding="utf-8")

            await asyncio.create_subprocess_shell(
                f'cd {compose_dir} && docker compose exec -T {service} '
                f'mysql -u root -p"{pwd}" -h 127.0.0.1 < "{dump_path}"',
            )
            dump_path.unlink(missing_ok=True)
            self.job.log("دیتابیس MySQL مهاجرت شد")

    async def _migrate_sqlite_to_other(self, target_db: str, password: str | None):
        db_migrations = TOOLS_DIR / "db-migrations"
        source = PASARGUARD_DATA / "db.sqlite3"
        target_url = self._build_target_url(target_db, password)
        await self._run_cmd(
            ["uv", "run", "migrations/universal.py",
             "--source", str(source), "--target-url", target_url],
            cwd=str(db_migrations), timeout=1200,
        )

    async def _import_mysql_dump(self, dump_file: Path, password: str | None):
        pwd = password or ""
        text = dump_file.read_text(encoding="utf-8", errors="ignore")
        for line in text.split("\n"):
            if line.startswith("CREATE DATABASE") or line.startswith("USE "):
                text = text.replace("marzban", "pasarguard")
                break
        fixed = dump_file.parent / "fixed_import.sql"
        fixed.write_text(text.replace("marzban", "pasarguard"), encoding="utf-8")

        await self._run_cmd(["docker", "compose", "up", "-d", "mysql"], cwd=str(PASARGUARD_DIR))
        await asyncio.sleep(5)
        await asyncio.create_subprocess_shell(
            f'cd {PASARGUARD_DIR} && docker compose exec -T mysql '
            f'mysql -u root -p"{pwd}" -h 127.0.0.1 < "{fixed}"',
        )

    async def _install_pasarguard(self, target_db: str, password: str | None):
        db_flag = ""
        if target_db == "mysql":
            db_flag = "--database mysql"
        elif target_db == "mariadb":
            db_flag = "--database mariadb"
        elif target_db in ("postgresql", "timescaledb"):
            db_flag = "--database timescaledb"

        await self._run_cmd([
            "bash", "-c",
            'curl -fsSL https://github.com/PasarGuard/scripts/raw/main/pasarguard.sh | bash -s -- @ install ' + db_flag
        ], timeout=900)

    async def _install_pasarguard_script(self):
        await self._run_cmd([
            "bash", "-c",
            "curl -sL https://github.com/PasarGuard/scripts/raw/main/pasarguard.sh | bash -s -- @ install-script"
        ])

    def _build_target_url(self, db_type: str, password: str | None) -> str:
        pwd = password or "password"
        if db_type == "sqlite":
            return f"sqlite:///{PASARGUARD_DATA}/db.sqlite3"
        if db_type in ("mysql", "mariadb"):
            return f"mysql+pymysql://root:{pwd}@127.0.0.1:3306/pasarguard"
        return f"postgresql+asyncpg://postgres:{pwd}@localhost:5432/pasarguard"

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
