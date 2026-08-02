"""3x-ui → PasarGuard migration using official PasarGuard/migrations tool.

Always converts to SQLite first. If target_db is not sqlite, runs two-phase
engine to copy head→head into the requested engine.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from app.config import PASARGUARD_DIR, PASARGUARD_DATA, PASARGUARD_ENV, TOOLS_DIR, BACKUP_DIR
from app.services.migrators.base import BaseMigrator
from app.services.prerequisites import find_xui_db
from app.services.pasarguard_ops import safe_start_pasarguard
from app.services.native_migration import run_cross_db_migration
from app.services.env_migration import (
    env_points_to_db,
    finalize_pasarguard_env_after_restore,
    read_env_var,
)


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
            return src

    return find_xui_db()


def bundled_xui_schema_db(tools_dir: Path | None = None) -> Path | None:
    """Official empty PasarGuard SQLite schema shipped with PasarGuard/migrations."""
    base = Path(tools_dir) if tools_dir else TOOLS_DIR
    path = base / "migrations" / "x-ui" / "input-db-pg" / "db.sqlite3"
    return path if path.is_file() else None


def resolve_xui_schema_db(
    tools_dir: Path | None = None,
    pasarguard_data: Path | None = None,
) -> Path:
    """Pick a PasarGuard *SQLite* schema reference for the official x-ui migrator.

    Starting a MySQL/Postgres PasarGuard install never creates db.sqlite3, so we
    must not rely on safe_start for schema creation. Prefer the bundled empty
    schema from PasarGuard/migrations; fall back to a live sqlite file if present.
    """
    bundled = bundled_xui_schema_db(tools_dir)
    if bundled:
        return bundled

    data = Path(pasarguard_data) if pasarguard_data else PASARGUARD_DATA
    live = data / "db.sqlite3"
    if live.is_file() and live.stat().st_size > 0:
        return live

    raise RuntimeError(
        "PasarGuard SQLite schema reference not found. "
        "Expected tools/migrations/x-ui/input-db-pg/db.sqlite3 "
        "(re-run installer to fetch PasarGuard/migrations) "
        "or a local /var/lib/pasarguard/db.sqlite3."
    )


def _table_count(conn: sqlite3.Connection, table: str) -> int | None:
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if not row or row[0] == 0:
            return None
        return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    except sqlite3.Error:
        return None


def inspect_xui_sqlite(path: Path) -> dict[str, int]:
    """Count key x-ui source tables (client_traffics / clients / inbounds)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out: dict[str, int] = {}
        for table in ("client_traffics", "clients", "inbounds", "users"):
            n = _table_count(conn, table)
            if n is not None:
                out[table] = n
        return out
    finally:
        conn.close()


def inspect_pasarguard_sqlite(path: Path) -> dict[str, int]:
    """Count key PasarGuard tables in a migrated sqlite file."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out: dict[str, int] = {}
        for table in ("users", "admins", "hosts", "inbounds", "nodes", "groups"):
            n = _table_count(conn, table)
            if n is not None:
                out[table] = n
        return out
    finally:
        conn.close()


def assert_xui_source_has_data(path: Path) -> dict[str, int]:
    counts = inspect_xui_sqlite(path)
    if not counts:
        raise RuntimeError(
            "فایل x-ui.db جدول‌های مورد انتظار (inbounds / client_traffics) را ندارد — "
            "آیا فایل دیتابیس 3x-ui درست آپلود شده؟"
        )
    clients = counts.get("client_traffics", counts.get("clients", 0))
    inbounds = counts.get("inbounds", 0)
    if clients <= 0 and inbounds <= 0:
        raise RuntimeError(
            "دیتابیس x-ui.db خالی است (هیچ inbound/کاربری نیست) — مهاجرت لغو شد"
        )
    return counts


def assert_migrated_pg_has_data(path: Path) -> dict[str, int]:
    counts = inspect_pasarguard_sqlite(path)
    data_tables = ("users", "inbounds", "groups")
    if any(counts.get(t, 0) > 0 for t in data_tables):
        return counts
    raise RuntimeError(
        "خروجی مهاجرت x-ui خالی است (users/inbounds/groups = 0). "
        "معمولاً به‌خاطر نبود schema مرجع SQLite یا دیتابیس مبدأ خالی است."
    )


# Upstream PasarGuard/migrations bug: debug logs reference `tag` before assignment
# whenever stream_settings has externalProxy / tlsSettings / security.
_XUI_TAG_BUG_NEEDLE = 'logger.debug(f"Removed externalProxy from inbound {tag}")'
_XUI_TAG_BUG_MARKER = (
    'tag = inbound_row.get("tag", f"inbound-{inbound_row.get(\'id\', \'unknown\')}")'
)


def patch_xui_converter_tag_bug(xui_tool: Path) -> bool:
    """Fix UnboundLocalError on `tag` in official x-ui converter (in-place).

    Returns True if the file was patched, False if already fixed / not present.
    """
    path = Path(xui_tool) / "migration" / "transformers" / "converter.py"
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    if _XUI_TAG_BUG_NEEDLE not in text:
        return False

    # Already fixed: tag assignment appears before the first debug that uses it.
    needle_pos = text.find(_XUI_TAG_BUG_NEEDLE)
    assign_pos = text.find(_XUI_TAG_BUG_MARKER)
    if assign_pos != -1 and assign_pos < needle_pos:
        return False

    buggy = (
        "            # Remove external proxy and TLS settings from streamSettings if present\n"
        "            if isinstance(stream_settings, dict):\n"
        "                # Remove proxy-related settings\n"
        '                stream_settings.pop("proxySettings", None)\n'
        '                stream_settings.pop("sockopt", None)\n'
        "                # Remove external proxy\n"
        '                if "externalProxy" in stream_settings:\n'
        '                    stream_settings.pop("externalProxy")\n'
        '                    logger.debug(f"Removed externalProxy from inbound {tag}")\n'
        "                # Remove TLS settings (certificate files won't exist on new system)\n"
        '                if "tlsSettings" in stream_settings:\n'
        '                    stream_settings.pop("tlsSettings")\n'
        '                    logger.debug(f"Removed tlsSettings from inbound {tag}")\n'
        "                # Remove security field (TLS indicator)\n"
        '                if "security" in stream_settings:\n'
        '                    stream_settings.pop("security")\n'
        '                    logger.debug(f"Removed security field from inbound {tag}")\n'
        "            \n"
        '            tag = inbound_row.get("tag", f"inbound-{inbound_row.get(\'id\', \'unknown\')}")\n'
    )
    fixed = (
        '            tag = inbound_row.get("tag", f"inbound-{inbound_row.get(\'id\', \'unknown\')}")\n'
        "            # Remove external proxy and TLS settings from streamSettings if present\n"
        "            if isinstance(stream_settings, dict):\n"
        "                # Remove proxy-related settings\n"
        '                stream_settings.pop("proxySettings", None)\n'
        '                stream_settings.pop("sockopt", None)\n'
        "                # Remove external proxy\n"
        '                if "externalProxy" in stream_settings:\n'
        '                    stream_settings.pop("externalProxy")\n'
        '                    logger.debug(f"Removed externalProxy from inbound {tag}")\n'
        "                # Remove TLS settings (certificate files won't exist on new system)\n"
        '                if "tlsSettings" in stream_settings:\n'
        '                    stream_settings.pop("tlsSettings")\n'
        '                    logger.debug(f"Removed tlsSettings from inbound {tag}")\n'
        "                # Remove security field (TLS indicator)\n"
        '                if "security" in stream_settings:\n'
        '                    stream_settings.pop("security")\n'
        '                    logger.debug(f"Removed security field from inbound {tag}")\n'
        "\n"
    )
    if buggy not in text:
        # Fallback: move assignment before stream_settings cleanup, drop the late assign.
        marker = "            # Remove external proxy and TLS settings from streamSettings if present\n"
        late = (
            '            tag = inbound_row.get("tag", f"inbound-{inbound_row.get(\'id\', \'unknown\')}")\n'
            '            protocol = inbound_row.get("protocol", "vless")\n'
        )
        if marker not in text or late not in text:
            return False
        text2 = text.replace(
            marker,
            '            tag = inbound_row.get("tag", f"inbound-{inbound_row.get(\'id\', \'unknown\')}")\n'
            + marker,
            1,
        )
        text2 = text2.replace(
            late,
            '            protocol = inbound_row.get("protocol", "vless")\n',
            1,
        )
        if text2 == text:
            return False
        path.write_text(text2, encoding="utf-8")
        return True

    path.write_text(text.replace(buggy, fixed, 1), encoding="utf-8")
    return True


def assert_migrated_core_config(path: Path) -> None:
    """Refuse to continue if official migrator skipped core_configs (broken xray config)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        n = _table_count(conn, "core_configs")
        if n is None:
            return  # schema without table — nothing to assert
        if n <= 0:
            raise RuntimeError(
                "مهاجرت x-ui جدول core_configs را خالی گذاشت "
                "(باگ tag در converter رسمی). دوباره تلاش کنید؛ "
                "بدون core_config پنل inboundها را درست لود نمی‌کند."
            )
    finally:
        conn.close()


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
        src_counts = assert_xui_source_has_data(Path(xui_db))
        self.job.log(
            "Source x-ui counts: "
            + ", ".join(f"{k}={v}" for k, v in src_counts.items())
        )

        self.job.set_progress(15, "بررسی PasarGuard...")
        if not PASARGUARD_DIR.exists():
            raise RuntimeError("PasarGuard نصب نیست — ابتدا نصب کنید")

        # Snapshot install .env BEFORE any rewrite (needed for MySQL app-user finalize)
        install_env_snapshot = ""
        if PASARGUARD_ENV.exists():
            install_env_snapshot = PASARGUARD_ENV.read_text(
                encoding="utf-8", errors="ignore",
            )

        self.job.set_progress(30, "آماده‌سازی ابزار مهاجرت x-ui...")
        xui_tool = TOOLS_DIR / "migrations" / "x-ui"
        if not xui_tool.exists():
            raise RuntimeError("ابزار x-ui migration یافت نشد")

        if patch_xui_converter_tag_bug(xui_tool):
            self.job.log(
                "Patched official x-ui converter: assign inbound tag before stream_settings cleanup"
            )

        schema_db = resolve_xui_schema_db()
        self.job.log(f"Using PasarGuard schema reference: {schema_db}")

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
        # Official tool can exit 0 even when core_configs failed (UnboundLocalError).
        if out and "Failed to migrate core_configs" in out:
            raise RuntimeError(
                "مهاجرت x-ui برای core_configs شکست خورد "
                f"(معمولاً باگ tag در converter): {out[-800:]}"
            )

        output_db = output_dir / "db.sqlite3"
        if not output_db.exists():
            raise RuntimeError("دیتابیس خروجی ایجاد نشد")

        out_counts = assert_migrated_pg_has_data(output_db)
        assert_migrated_core_config(output_db)
        self.job.log(
            "Migrated SQLite counts: "
            + ", ".join(f"{k}={v}" for k, v in out_counts.items())
        )

        # Land migrated SQLite under PasarGuard data (also intermediate for cross-db)
        land_db = PASARGUARD_DATA / "db.sqlite3"
        PASARGUARD_DATA.mkdir(parents=True, exist_ok=True)
        self.job.set_progress(70, "جایگزینی دیتابیس PasarGuard (SQLite)...")
        self._backup_file(land_db, BACKUP_DIR)
        shutil.copy2(output_db, land_db)

        self.job.set_progress(80, "تولید mapping لینک‌های اشتراک...")
        mapping_file = work_dir / "subscription_url_mapping.json"
        await self._run_cmd(
            ["uv", "run", "migration/generate_subscription_url_mapping.py",
             "--xui-db", str(input_db),
             "--pasarguard-db", str(land_db),
             "--output", str(mapping_file)],
            cwd=str(xui_tool),
        )

        redirect_installed = False
        if install_redirect and mapping_file.exists():
            self.job.set_progress(85, "نصب سرور ریدایرکت لینک‌های قدیمی...")
            redirect_installed = await self._install_redirect_server(mapping_file)

        if target_db != "sqlite":
            self.job.set_progress(88, f"Two-phase: SQLite → {target_db}...")
            await self._convert_landed_sqlite_to_target(
                land_db, target_db, install_env_snapshot,
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
            "source_counts": src_counts,
            "migrated_counts": out_counts,
            "warnings": {
                "en": [
                    "Old /sub/{token} links keep working via the redirect server (installed by default).",
                    "Create admin: pasarguard cli generate-temp-key",
                    "Add Hosts in the panel for each inbound so new subscription configs resolve.",
                ],
                "fa": [
                    "لینک‌های قدیمی /sub/{token} با redirect server بدون تغییر می‌مانند.",
                    "ادمین بسازید: pasarguard cli generate-temp-key",
                    "برای هر inbound در پنل Host بسازید تا کانفیگ سابسکریپشن جدید کامل شود.",
                ],
                "ru": [
                    "Старые ссылки /sub/{token} сохраняются через redirect server.",
                    "Создайте админа: pasarguard cli generate-temp-key",
                    "Создайте Hosts для inbound'ов в панели для новых подписок.",
                ],
            },
        }

    async def _install_redirect_server(self, mapping_file: Path) -> bool:
        """Install PasarGuard redirect-server so old x-ui /sub links keep working.

        Upstream installer expects ``MAP_FILE=/path`` (not ``--mapping``). Prefer the
        locally cloned script under tools/migrations/redirect-server.
        """
        mapping = Path(mapping_file)
        if not mapping.is_file():
            return False
        local = TOOLS_DIR / "migrations" / "redirect-server" / "install_redirect_server.sh"
        # Official API: MAP_FILE env + optional version arg (default latest)
        if local.is_file():
            cmd = f'MAP_FILE="{mapping}" bash "{local}" latest'
        else:
            url = (
                "https://raw.githubusercontent.com/PasarGuard/migrations/main/"
                "redirect-server/install_redirect_server.sh"
            )
            cmd = f'MAP_FILE="{mapping}" bash -c \'curl -fsSL "{url}" | bash -s -- latest\''
        ok, out = await self._run_cmd(["bash", "-c", cmd], timeout=600)
        if ok:
            self.job.log("Redirect server installed — old subscription URLs stay valid")
            return True
        self.job.log(
            "Redirect server install failed (old /sub links may break until fixed): "
            f"{(out or '')[-500:]}"
        )
        return False

    async def _convert_landed_sqlite_to_target(
        self,
        land_db: Path,
        target_db: str,
        install_env_snapshot: str,
    ) -> None:
        """sqlite → server DB with restore-grade MySQL password sync + .env finalize.

        Fixes Access denied for ``pasarguard`` after copy-as-root: align app user
        password and write SQLAlchemy URL from the install snapshot.
        """
        from app.services.db_auth import (
            resolve_live_admin_connection,
            sync_mysql_roles_to_password,
            sync_postgres_roles_to_app_password,
        )
        from app.services.db_credentials import get_target_connection

        await run_cross_db_migration(self, str(land_db), "sqlite", target_db)

        env = install_env_snapshot or (
            PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
            if PASARGUARD_ENV.exists()
            else ""
        )
        app_user = (
            read_env_var(env, "DB_USER")
            or read_env_var(env, "MYSQL_USER")
            or read_env_var(env, "POSTGRES_USER")
            or "pasarguard"
        )
        db_name = (
            read_env_var(env, "DB_NAME")
            or read_env_var(env, "MYSQL_DATABASE")
            or read_env_var(env, "POSTGRES_DB")
            or "pasarguard"
        )
        # Prefer MYSQL_ROOT_PASSWORD for sync (matches restore convert); URL keeps app user.
        sync_pwd = (
            read_env_var(env, "MYSQL_ROOT_PASSWORD")
            or read_env_var(env, "DB_PASSWORD")
            or read_env_var(env, "MYSQL_PASSWORD")
            or read_env_var(env, "POSTGRES_PASSWORD")
            or ""
        )
        admin = get_target_connection(self.params) or {}
        if not sync_pwd:
            sync_pwd = admin.get("password") or ""

        if target_db in ("mysql", "mariadb") and sync_pwd:
            try:
                live = await resolve_live_admin_connection(
                    self, target_db, env_text=env,
                )
            except Exception:
                live = {**admin, "db_type": target_db, "password": sync_pwd}
            await sync_mysql_roles_to_password(
                self,
                target_db,
                live,
                app_user=app_user,
                password=sync_pwd,
                env_text=env,
            )
            self.job.log(
                f"MySQL app user '{app_user}' aligned for panel SQLAlchemy URL"
            )
        elif target_db in ("postgresql", "timescaledb") and admin:
            await sync_postgres_roles_to_app_password(
                self, target_db, admin, env_text=env,
            )

        if not PASARGUARD_ENV.exists():
            raise RuntimeError(".env PasarGuard یافت نشد بعد از convert")

        text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
        finalized = finalize_pasarguard_env_after_restore(
            text,
            target_db,
            sync_pwd,
            env,
            db_user=app_user,
            db_name=db_name,
        )
        if not env_points_to_db(finalized, target_db):
            raise RuntimeError(
                f".env SQLALCHEMY_DATABASE_URL با موتور هدف {target_db} هم‌خوان نیست"
            )
        PASARGUARD_ENV.write_text(finalized, encoding="utf-8")
        self.job.log(f".env finalized for {target_db} (user={app_user})")

        # Panel must not keep reading the intermediate SQLite file
        if land_db.exists() and target_db != "sqlite":
            bak = PASARGUARD_DATA / f"db.sqlite3.pre-convert-{self.job.job_id}.bak"
            if bak.exists():
                bak.unlink()
            shutil.move(str(land_db), str(bak))
            self.job.log(f"Moved SQLite aside → {bak.name}")

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
