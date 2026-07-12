"""3x-ui → PasarGuard migration using official PasarGuard/migrations tool."""

import shutil
from pathlib import Path

from app.config import PASARGUARD_DIR, PASARGUARD_DATA, TOOLS_DIR, BACKUP_DIR
from app.services.migrators.base import BaseMigrator
from app.services.prerequisites import find_xui_db


class XuiMigrator(BaseMigrator):
    async def run(self, params: dict) -> dict:
        upload_path = params.get("upload_path")
        install_redirect = params.get("install_redirect", False)

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
            await self._run_cmd(["pasarguard", "restart"])
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

        self.job.set_progress(45, "اجرای مهاجرت x-ui → PasarGuard...")
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

        self.job.set_progress(70, "جایگزینی دیتابیس PasarGuard...")
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

        self.job.set_progress(95, "راه‌اندازی مجدد PasarGuard...")
        await self._run_cmd(["pasarguard", "restart"])

        self.job.set_progress(100, "مهاجرت 3x-ui انجام شد!")
        return {
            "panel_url": self._get_panel_url(),
            "subscription_preserved": False,
            "redirect_installed": redirect_installed,
            "mapping_file": str(mapping_file) if mapping_file.exists() else None,
            "warnings_fa": [
                "لینک‌های اشتراک کاربران تغییر کرده‌اند.",
                "اکانت ادمین باید دستی ساخته شود: pasarguard cli generate-temp-key",
            ],
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
