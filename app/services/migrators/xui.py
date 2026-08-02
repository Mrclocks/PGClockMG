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


def find_xui_db_in_dir(root: Path) -> Path | None:
    """Locate an x-ui SQLite database inside an extracted backup / workspace."""
    if not root or not root.is_dir():
        return None

    preferred: list[Path] = []
    fallback: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        name = p.name.lower()
        if name.endswith(".db") or name.endswith(".sqlite3"):
            if "x-ui" in name:
                preferred.append(p)
            else:
                fallback.append(p)

    if preferred:
        # Prefer exact x-ui.db, then any *x-ui* name
        for p in preferred:
            if p.name.lower() == "x-ui.db":
                return p
        return preferred[0]
    if fallback:
        return fallback[0]
    return None


def resolve_xui_db_source(
    upload_path: str | None = None,
    upload_work_dir: str | None = None,
) -> Path | None:
    """Resolve x-ui.db from bundle workspace, upload file/zip, or live install.

    Bundle uploads set upload_path to a *directory* (workspace). Older code
    treated every non-.zip path as a file and crashed with IsADirectoryError.
    """
    candidates: list[Path] = []
    if upload_work_dir:
        candidates.append(Path(upload_work_dir))
    if upload_path:
        p = Path(upload_path)
        # Avoid scanning the same directory twice when both params point at workspace
        if not candidates or p.resolve() != candidates[0].resolve():
            candidates.append(p)

    for src in candidates:
        if not src.exists():
            continue
        if src.is_dir():
            found = find_xui_db_in_dir(src)
            if found:
                return found
            continue
        if src.is_file():
            suffix = src.suffix.lower()
            if suffix == ".zip":
                # Caller extracts zip; signal zip by returning the zip path.
                # Actual extraction stays in the migrator (needs async unzip).
                return src
            if suffix in (".db", ".sqlite3") or "x-ui" in src.name.lower():
                return src
            # Unknown single file — still try (legacy upload of bare db)
            return src

    return find_xui_db()


class XuiMigrator(BaseMigrator):
    async def run(self, params: dict) -> dict:
        upload_path = params.get("upload_path")
        upload_work_dir = params.get("upload_work_dir")
        install_redirect = params.get("install_redirect", True)
        target_db = params.get("target_db") or "sqlite"

        self.job.set_progress(5, "یافتن دیتابیس 3x-ui...")

        xui_db = await self._locate_xui_db(upload_path, upload_work_dir)

        if not xui_db or not Path(xui_db).exists():
            raise RuntimeError("دیتابیس x-ui.db یافت نشد — لطفاً آپلود کنید")

        self.job.log(f"Using 3x-ui database: {xui_db}")

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

    async def _locate_xui_db(
        self,
        upload_path: str | None,
        upload_work_dir: str | None,
    ) -> Path | None:
        """Find x-ui.db, extracting zip uploads into a job work dir when needed."""
        resolved = resolve_xui_db_source(upload_path, upload_work_dir)
        if resolved is None:
            return None

        if resolved.is_dir():
            return find_xui_db_in_dir(resolved)

        if resolved.is_file() and resolved.suffix.lower() == ".zip":
            work = BACKUP_DIR / self.job.job_id
            work.mkdir(parents=True, exist_ok=True)
            ok, out = await self._run_cmd(["unzip", "-o", str(resolved), "-d", str(work)])
            if not ok:
                raise RuntimeError(f"استخراج zip ناموفق: {out}")
            found = find_xui_db_in_dir(work)
            if not found:
                raise RuntimeError("دیتابیس x-ui.db داخل zip یافت نشد")
            return found

        return resolved

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
