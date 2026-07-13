"""3x-ui → PasarGuard migration using official PasarGuard/migrations tool.

Always converts to SQLite first. If target_db is not sqlite, runs two-phase
engine to copy head→head into the requested engine.
"""

import shutil
from pathlib import Path

from app.config import PASARGUARD_DIR, PASARGUARD_DATA, PASARGUARD_ENV, TOOLS_DIR, BACKUP_DIR
from app.services.migrators.base import BaseMigrator
from app.services.prerequisites import find_xui_db
from app.services.pasarguard_ops import safe_start_pasarguard
from app.services.native_migration import run_cross_db_migration
from app.services.env_migration import transform_pasarguard_env_for_target


class XuiMigrator(BaseMigrator):
    async def run(self, params: dict) -> dict:
        upload_path = params.get("upload_path")
        install_redirect = params.get("install_redirect", True)
        target_db = params.get("target_db") or "sqlite"

        self.job.set_progress(5, "یافتن دیتابیس 3x-ui...")

        xui_db = None
        if upload_path:
            src = Path(upload_path)
            work = BACKUP_DIR / self.job.job_id
            work.mkdir(parents=True, exist_ok=True)
            if src.suffix == ".zip":
                await self._run_cmd(["unzip", "-o", str(src), "-d", str(work)])
                for p in work.rglob("*.db"):
                    if "x-ui" in p.name.lower() or p.name == "x-ui.db":
                        xui_db = p
                        break
                if not xui_db:
                    for p in work.rglob("*.db"):
                        xui_db = p
                        break
            else:
                xui_db = work / "x-ui.db"
                shutil.copy2(src, xui_db)
        else:
            xui_db = find_xui_db()

        if not xui_db or not Path(xui_db).exists():
            raise RuntimeError("دیتابیس x-ui.db یافت نشد — لطفاً آپلود کنید")

        self.job.set_progress(15, "بررسی PasarGuard...")
        if not PASARGUARD_DIR.exists():
            raise RuntimeError("PasarGuard نصب نیست — ابتدا نصب کنید")

        schema_db = PASARGUARD_DATA / "db.sqlite3"
        if not schema_db.exists():
            self.job.log("راه‌اندازی PasarGuard برای ایجاد schema...")
            await safe_start_pasarguard(self)
            import asyncio
            await asyncio.sleep(10)

        self.job.set_progress(30, "آماده‌سازی ابزار مهاجرت x-ui...")
        xui_tool = TOOLS_DIR / "migrations" / "x-ui"
        if not xui_tool.exists():
            raise RuntimeError("ابزار x-ui migration یافت نشد")

        work_dir = BACKUP_DIR / f"xui-{self.job.job_id}"
        work_dir.mkdir(parents=True, exist_ok=True)
        input_db = work_dir / "x-ui.db"
        shutil.copy2(xui_db, input_db)
        output_dir = work_dir / "output-db"

        self.job.set_progress(45, "اجرای مهاجرت x-ui → PasarGuard SQLite...")
        ok, out = await self._run_cmd(
            ["uv", "run", "migrate.py",
             "--input-db", str(input_db),
             "--schema-db", str(schema_db),
             "--output-folder", str(output_dir),
             "--log-level", "INFO"],
            cwd=str(xui_tool),
            timeout=1200,
        )
        if not ok:
            raise RuntimeError(f"مهاجرت x-ui ناموفق: {out}")

        output_db = output_dir / "db.sqlite3"
        if not output_db.exists():
            raise RuntimeError("دیتابیس خروجی ایجاد نشد")

        self.job.set_progress(70, "جایگزینی دیتابیس PasarGuard (SQLite)...")
        self._backup_file(schema_db, BACKUP_DIR)
        shutil.copy2(output_db, schema_db)

        self.job.set_progress(80, "تولید mapping لینک‌های اشتراک...")
        mapping_file = work_dir / "subscription_url_mapping.json"
        await self._run_cmd(
            ["uv", "run", "migration/generate_subscription_url_mapping.py",
             "--xui-db", str(input_db),
             "--pasarguard-db", str(schema_db),
             "--output", str(mapping_file)],
            cwd=str(xui_tool),
        )

        redirect_installed = False
        if install_redirect and mapping_file.exists():
            self.job.set_progress(85, "نصب سرور ریدایرکت لینک‌های قدیمی...")
            ok, _ = await self._run_cmd([
                "bash", "-c",
                f"curl -fsSL https://raw.githubusercontent.com/PasarGuard/migrations/main/"
                f"redirect-server/install_redirect_server.sh | bash -s -- --mapping {mapping_file}"
            ], timeout=300)
            redirect_installed = ok

        if target_db != "sqlite":
            self.job.set_progress(88, f"Two-phase: SQLite → {target_db}...")
            await run_cross_db_migration(self, str(schema_db), "sqlite", target_db)
            if PASARGUARD_ENV.exists():
                from app.services.db_credentials import get_target_connection
                text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
                conn = get_target_connection(self.params)
                PASARGUARD_ENV.write_text(
                    transform_pasarguard_env_for_target(
                        text, target_db, conn.get("password"),
                    ),
                    encoding="utf-8",
                )

        self.job.set_progress(95, "راه‌اندازی مجدد PasarGuard...")
        await safe_start_pasarguard(self)

        self.job.set_progress(100, "3x-ui migration complete!")
        return {
            "panel_url": self._get_panel_url(),
            "subscription_mode": "redirect",
            "redirect_installed": redirect_installed,
            "target_db": target_db,
            "mapping_file": str(mapping_file) if mapping_file.exists() else None,
            "warnings": {
                "en": [
                    "Old /sub/{token} links work if redirect server is installed (enabled by default).",
                    "Create admin: pasarguard cli generate-temp-key",
                ],
                "fa": [
                    "لینک‌های قدیمی /sub/{token} با redirect server کار می‌کنند.",
                    "ادمین بسازید: pasarguard cli generate-temp-key",
                ],
                "ru": [
                    "Старые ссылки работают через redirect server.",
                    "Создайте админа: pasarguard cli generate-temp-key",
                ],
            },
        }

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
