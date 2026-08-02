"""3x-ui → PasarGuard migration using official PasarGuard/migrations tool.

Always converts to SQLite first. If target_db is not sqlite, runs two-phase
engine to copy head→head into the requested engine.
"""

from __future__ import annotations

import json
import shutil
import socket
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from app.config import PASARGUARD_DIR, PASARGUARD_DATA, PASARGUARD_ENV, TOOLS_DIR, BACKUP_DIR
from app.services.migrators.base import BaseMigrator
from app.services.prerequisites import find_xui_db
from app.services.pasarguard_ops import safe_start_pasarguard, normalize_target_db
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


def _subscription_path_only(url: str) -> str:
    """Path used by redirect-server lookup (must match ``r.URL.Path``, no query)."""
    raw = (url or "").strip()
    if not raw:
        return "/"
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlparse(raw)
        path = parsed.path or "/"
    else:
        path = raw.split("?", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    return path or "/"


def normalize_subscription_mapping(mapping_path: Path) -> dict:
    """Fix official mapping so redirect-server can match client requests.

    Upstream writes ``/sub/{subId}?name={subId}`` but browsers/clients request
    ``/sub/{subId}`` (query is not part of ``URL.Path``) → permanent 404.
    """
    path = Path(mapping_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    mappings = data.get("mappings") or {}
    fixed = 0
    for _key, entry in mappings.items():
        if not isinstance(entry, dict):
            continue
        old = entry.get("old_subscription_url") or ""
        new = entry.get("new_subscription_url") or ""
        old_n = _subscription_path_only(old)
        if old_n != old:
            entry["old_subscription_url"] = old_n
            fixed += 1
        if new and not new.startswith("http"):
            new_n = _subscription_path_only(new)
            if new_n != new:
                entry["new_subscription_url"] = new_n
                fixed += 1
    data["url_formats"] = {
        "old_format": "/sub/{subId}",
        "new_format": "/sub/{token}",
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    data["_normalized_entries"] = fixed
    return data


def read_xui_subscription_listen(xui_db: Path) -> dict:
    """Read 3x-ui subscription listener settings (port/path/certs)."""
    out = {
        "port": 2096,
        "path": "sub",
        "domain": "",
        "cert": "",
        "key": "",
        "ssl": False,
    }
    try:
        conn = sqlite3.connect(f"file:{xui_db}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return out

    settings = {str(k): ("" if v is None else str(v)) for k, v in rows}
    port_raw = settings.get("subPort") or settings.get("sub_port") or ""
    try:
        port = int(str(port_raw).strip()) if str(port_raw).strip() else 2096
    except ValueError:
        port = 2096
    if port <= 0 or port > 65535:
        port = 2096
    path = (settings.get("subPath") or settings.get("sub_path") or "sub").strip().strip("/") or "sub"
    domain = (
        settings.get("subURI")
        or settings.get("subDomain")
        or settings.get("sub_domain")
        or ""
    ).strip()
    cert = (settings.get("subCertFile") or settings.get("webCertFile") or "").strip()
    key = (settings.get("subKeyFile") or settings.get("webKeyFile") or "").strip()
    ssl = bool(cert and key and Path(cert).is_file() and Path(key).is_file())
    out.update({
        "port": port,
        "path": path,
        "domain": domain,
        "cert": cert if ssl else "",
        "key": key if ssl else "",
        "ssl": ssl,
    })
    return out


def detect_public_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip or "127.0.0.1"
    except Exception:
        return "127.0.0.1"


def pasarguard_subscription_base_url(env_text: str | None = None) -> str:
    """Base URL where PasarGuard serves ``/sub/{token}`` (redirect target)."""
    text = env_text or ""
    if not text and PASARGUARD_ENV.exists():
        text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")

    # Explicit subscription / public URL wins when set
    for key in (
        "XRAY_SUBSCRIPTION_URL",
        "SUBSCRIPTION_URL",
        "PUBLIC_URL",
        "UVICORN_PUBLIC_URL",
    ):
        val = (read_env_var(text, key) or "").strip().rstrip("/")
        if val.startswith("http://") or val.startswith("https://"):
            # Strip trailing /sub if present
            if val.endswith("/sub"):
                val = val[:-4]
            return val

    port = read_env_var(text, "UVICORN_PORT") or "8000"
    has_ssl = bool(
        read_env_var(text, "UVICORN_SSL_CERTFILE")
        or read_env_var(text, "UVICORN_SSL_KEYFILE")
    )
    scheme = "https" if has_ssl else "http"
    return f"{scheme}://{detect_public_ip()}:{port}"


def build_redirect_server_config(
    *,
    listen_port: int,
    redirect_domain: str,
    ssl_cert: str = "",
    ssl_key: str = "",
) -> dict:
    ssl_enabled = bool(ssl_cert and ssl_key)
    cert_pem = ""
    key_pem = ""
    if ssl_enabled:
        cert_pem = Path(ssl_cert).read_text(encoding="utf-8", errors="ignore")
        key_pem = Path(ssl_key).read_text(encoding="utf-8", errors="ignore")
    return {
        "host": "0.0.0.0",
        "port": int(listen_port),
        "redirect_domain": (redirect_domain or "").rstrip("/"),
        "panel": "x-ui",
        "ssl": {
            "enabled": ssl_enabled,
            "cert": cert_pem,
            "key": key_pem,
        },
    }


class XuiMigrator(BaseMigrator):
    async def run(self, params: dict) -> dict:
        upload_path = params.get("upload_path")
        upload_work_dir = params.get("upload_work_dir")
        install_redirect = params.get("install_redirect", True)
        target_db = normalize_target_db(params.get("target_db") or "sqlite")
        params["target_db"] = target_db
        self.params["target_db"] = target_db

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
        xui_listen = read_xui_subscription_listen(Path(xui_db))
        # Allow wizard overrides
        if params.get("xui_sub_port"):
            try:
                xui_listen["port"] = int(params["xui_sub_port"])
            except (TypeError, ValueError):
                pass
        if params.get("xui_sub_path"):
            xui_listen["path"] = str(params["xui_sub_path"]).strip().strip("/") or "sub"

        map_cmd = [
            "uv", "run", "migration/generate_subscription_url_mapping.py",
            "--xui-db", str(input_db),
            "--pasarguard-db", str(land_db),
            "--output", str(mapping_file),
            "--xui-path", xui_listen["path"],
        ]
        await self._run_cmd(map_cmd, cwd=str(xui_tool))
        if mapping_file.exists():
            norm = normalize_subscription_mapping(mapping_file)
            self.job.log(
                f"Normalized subscription mapping paths "
                f"(stripped ?name= query; {norm.get('_normalized_entries', 0)} entries touched)"
            )

        if target_db != "sqlite":
            self.job.set_progress(88, f"Two-phase: SQLite → {target_db}...")
            await self._convert_landed_sqlite_to_target(
                land_db, target_db, install_env_snapshot,
            )

        self.job.set_progress(92, "راه‌اندازی مجدد PasarGuard...")
        await safe_start_pasarguard(self)

        redirect_installed = False
        redirect_port = xui_listen["port"]
        redirect_domain = (
            (params.get("redirect_domain") or "").strip()
            or pasarguard_subscription_base_url(
                PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
                if PASARGUARD_ENV.exists()
                else install_env_snapshot
            )
        )
        if install_redirect and mapping_file.exists():
            self.job.set_progress(96, "نصب سرور ریدایرکت لینک‌های قدیمی...")
            redirect_installed = await self._install_redirect_server(
                mapping_file,
                listen_port=redirect_port,
                redirect_domain=redirect_domain,
                ssl_cert=xui_listen.get("cert") or "",
                ssl_key=xui_listen.get("key") or "",
            )

        self.job.set_progress(100, "3x-ui migration complete!")
        warn_en = [
            "Old /sub/{subId} links are redirected to PasarGuard via redirect-server.",
            f"Redirect listens on port {redirect_port} → {redirect_domain}/sub/…",
            "Create admin: pasarguard cli generate-temp-key",
            "Add Hosts in the panel for each inbound so new subscription configs resolve.",
        ]
        warn_fa = [
            "لینک‌های قدیمی /sub/{subId} با redirect-server به پاسارگارد هدایت می‌شوند.",
            f"ریدایرکت روی پورت {redirect_port} → {redirect_domain}/sub/…",
            "ادمین بسازید: pasarguard cli generate-temp-key",
            "برای هر inbound در پنل Host بسازید تا کانفیگ سابسکریپشن جدید کامل شود.",
        ]
        warn_ru = [
            "Старые /sub/{subId} перенаправляются на PasarGuard через redirect-server.",
            f"Redirect слушает порт {redirect_port} → {redirect_domain}/sub/…",
            "Создайте админа: pasarguard cli generate-temp-key",
            "Создайте Hosts для inbound'ов в панели для новых подписок.",
        ]
        if not redirect_installed and install_redirect:
            warn_en.insert(
                0,
                "Redirect server did NOT install — old /sub links will not work. "
                "Check migration logs for 'Direct redirect install failed' "
                "(often: GitHub download blocked from the server, or port still busy).",
            )
            warn_fa.insert(
                0,
                "سرور ریدایرکت نصب نشد — لینک‌های قدیمی کار نمی‌کنند. "
                "در لاگ مهاجرت دنبال «Direct redirect install failed» بگرد "
                "(معمولاً دانلود از GitHub بسته است یا پورت ساب هنوز اشغال است).",
            )
            warn_ru.insert(
                0,
                "Redirect-server не установился — старые /sub не работают. "
                "Смотрите лог: Direct redirect install failed "
                "(часто блок GitHub или порт занят).",
            )

        return {
            "panel_url": self._get_panel_url(),
            "subscription_mode": "redirect",
            "redirect_installed": redirect_installed,
            "redirect_port": redirect_port,
            "redirect_domain": redirect_domain,
            "target_db": target_db,
            "mapping_file": str(mapping_file) if mapping_file.exists() else None,
            "source_counts": src_counts,
            "migrated_counts": out_counts,
            "warnings": {
                "en": warn_en,
                "fa": warn_fa,
                "ru": warn_ru,
            },
        }

    async def _stop_xui_for_redirect_port(self, port: int) -> None:
        """Free the old 3x-ui subscription port so redirect-server can bind it."""
        # Best-effort: stop common x-ui unit / process without failing migration
        await self._run_cmd(
            ["bash", "-c", "systemctl stop x-ui 2>/dev/null || true"],
            timeout=60,
        )
        await self._run_cmd(
            ["bash", "-c", "systemctl stop x-ui.service 2>/dev/null || true"],
            timeout=60,
        )
        # Kill anything still listening on the subscription port (usually x-ui sub)
        await self._run_cmd(
            [
                "bash", "-c",
                f"fuser -k {int(port)}/tcp 2>/dev/null || "
                f"(command -v lsof >/dev/null && "
                f"lsof -ti tcp:{int(port)} | xargs -r kill -9) || true",
            ],
            timeout=30,
        )
        self.job.log(f"Freed port {port} for redirect-server (stopped x-ui if present)")

    async def _install_redirect_server(
        self,
        mapping_file: Path,
        *,
        listen_port: int = 2096,
        redirect_domain: str = "",
        ssl_cert: str = "",
        ssl_key: str = "",
    ) -> bool:
        """Install redirect-server so old x-ui /sub links keep working.

        Uses a direct (non-interactive) install instead of the upstream
        ``install_redirect_server.sh``, which often fails under the wizard because:
        - GitHub API / interactive prompts / ``set -e`` + ``systemctl status``
        - default port 8080 and empty ``redirect_domain`` when env vars are lost
        """
        mapping = Path(mapping_file)
        if not mapping.is_file():
            self.job.log("Redirect install skipped: mapping file missing")
            return False

        normalize_subscription_mapping(mapping)
        if not redirect_domain:
            redirect_domain = pasarguard_subscription_base_url()
        await self._stop_xui_for_redirect_port(listen_port)

        cfg = build_redirect_server_config(
            listen_port=listen_port,
            redirect_domain=redirect_domain,
            ssl_cert=ssl_cert,
            ssl_key=ssl_key,
        )
        cfg_path = mapping.parent / "redirect-server-config.json"
        cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        self.job.log(
            f"Redirect config: port={listen_port} redirect_domain={redirect_domain} "
            f"ssl={cfg['ssl']['enabled']}"
        )

        ok = await self._deploy_redirect_server_direct(mapping, cfg_path, listen_port)
        if ok:
            return True

        # Retry without SSL if cert/key made the service fail to start
        if cfg.get("ssl", {}).get("enabled"):
            self.job.log("Retrying redirect-server without SSL (plain HTTP on old sub port)...")
            cfg_no_ssl = build_redirect_server_config(
                listen_port=listen_port,
                redirect_domain=redirect_domain,
            )
            cfg_path.write_text(json.dumps(cfg_no_ssl, indent=2), encoding="utf-8")
            ok = await self._deploy_redirect_server_direct(mapping, cfg_path, listen_port)
            if ok:
                return True

        # Last resort: official installer (MAP_FILE/CONFIG_FILE), then force our files
        ok = await self._deploy_redirect_server_upstream(mapping, cfg_path)
        if ok or await self._redirect_server_is_active():
            await self._force_redirect_runtime_files(mapping, cfg_path)
            if await self._redirect_server_is_active():
                self.job.log(
                    "Redirect server installed — old /sub/{subId} → PasarGuard /sub/{token}"
                )
                return True

        self.job.log(
            "Redirect server install failed — check job logs above "
            "(common: GitHub download blocked, port still busy, missing curl/jq/tar)"
        )
        return False

    async def _redirect_server_is_active(self) -> bool:
        ok, out = await self._run_cmd(
            ["bash", "-c", "systemctl is-active redirect-server 2>/dev/null || true"],
            timeout=15,
        )
        return "active" in (out or "").split()

    async def _force_redirect_runtime_files(
        self, mapping: Path, cfg_path: Path,
    ) -> None:
        await self._run_cmd(
            [
                "bash", "-c",
                "mkdir -p /etc/redirect-server && "
                f'cp -f "{mapping}" /etc/redirect-server/subscription_url_mapping.json && '
                f'cp -f "{cfg_path}" /etc/redirect-server/config.json && '
                "id redirectsrv >/dev/null 2>&1 && "
                "chown -R redirectsrv:redirectsrv /etc/redirect-server || true && "
                "systemctl daemon-reload 2>/dev/null || true && "
                "systemctl enable redirect-server 2>/dev/null || true && "
                "systemctl restart redirect-server 2>/dev/null || true",
            ],
            timeout=60,
        )

    async def _deploy_redirect_server_direct(
        self,
        mapping: Path,
        cfg_path: Path,
        listen_port: int,
    ) -> bool:
        """Download binary + write systemd unit (no interactive upstream script)."""
        script = f'''set -euo pipefail
if [[ "$(id -u)" -ne 0 ]]; then
  echo "Redirect install requires root" >&2
  exit 1
fi
command -v curl >/dev/null || {{ echo "curl missing" >&2; exit 1; }}
command -v tar >/dev/null || {{ echo "tar missing" >&2; exit 1; }}
command -v systemctl >/dev/null || {{ echo "systemctl missing" >&2; exit 1; }}

case "$(uname -m)" in
  x86_64|amd64) ARCH_ASSET="Linux_x86_64" ;;
  aarch64|arm64) ARCH_ASSET="Linux_arm64" ;;
  armv7l|armv7hf) ARCH_ASSET="Linux_armv7" ;;
  i386|i686) ARCH_ASSET="Linux_i386" ;;
  *) echo "Unsupported arch: $(uname -m)" >&2; exit 1 ;;
esac

ASSET="redirect-server_0.0.1_${{ARCH_ASSET}}.tar.gz"
URL="https://github.com/PasarGuard/migrations/releases/download/v0.0.1/${{ASSET}}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "Downloading redirect-server: $URL"
if ! curl -fsSL --connect-timeout 20 --max-time 180 -o "$TMP/$ASSET" "$URL"; then
  echo "Direct download failed — trying GitHub API..."
  if command -v jq >/dev/null 2>&1; then
    API="https://api.github.com/repos/PasarGuard/migrations/releases/tags/v0.0.1"
    URL="$(curl -fsSL --connect-timeout 20 "$API" | jq -r --arg n "$ASSET" '.assets[]|select(.name==$n)|.browser_download_url')"
    [[ -n "$URL" && "$URL" != null ]] || {{ echo "Could not resolve asset URL" >&2; exit 1; }}
    curl -fsSL --connect-timeout 20 --max-time 180 -o "$TMP/$ASSET" "$URL"
  else
    exit 1
  fi
fi

tar -xzf "$TMP/$ASSET" -C "$TMP"
BIN="$(find "$TMP" -type f -name redirect-server | head -n 1)"
[[ -n "$BIN" ]] || {{ echo "redirect-server binary missing in archive" >&2; exit 1; }}
chmod +x "$BIN"

id redirectsrv >/dev/null 2>&1 || useradd --system --home /opt/redirect-server --shell /usr/sbin/nologin redirectsrv
install -d -m 0755 /opt/redirect-server
install -d -m 0750 /etc/redirect-server
install -D -m 0755 "$BIN" /usr/local/bin/redirect-server
cp -f "{mapping}" /etc/redirect-server/subscription_url_mapping.json
cp -f "{cfg_path}" /etc/redirect-server/config.json
chown -R redirectsrv:redirectsrv /opt/redirect-server /etc/redirect-server
chmod 0640 /etc/redirect-server/*

cat > /etc/systemd/system/redirect-server.service <<'UNIT'
[Unit]
Description=Subscription URL Redirect Server
After=network.target

[Service]
Type=simple
User=redirectsrv
Group=redirectsrv
WorkingDirectory=/opt/redirect-server
ExecStart=/usr/local/bin/redirect-server --config /etc/redirect-server/config.json --map /etc/redirect-server/subscription_url_mapping.json
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
UNIT

# Free listen port again right before start
fuser -k {int(listen_port)}/tcp 2>/dev/null || true
systemctl daemon-reload
systemctl enable redirect-server
systemctl restart redirect-server
sleep 1
if ! systemctl is-active --quiet redirect-server; then
  echo "redirect-server failed to start:" >&2
  systemctl status redirect-server --no-pager -l || true
  journalctl -u redirect-server -n 40 --no-pager || true
  ss -lntp "sport = :{int(listen_port)}" 2>/dev/null || true
  exit 1
fi
echo "redirect-server active on port {int(listen_port)}"
'''
        ok, out = await self._run_cmd(["bash", "-c", script], timeout=300)
        if ok:
            self.job.log(
                "Redirect server installed — old /sub/{subId} → PasarGuard /sub/{token}"
            )
            return True
        self.job.log(
            "Direct redirect install failed: "
            f"{(out or '')[-800:]}"
        )
        return False

    async def _deploy_redirect_server_upstream(
        self, mapping: Path, cfg_path: Path,
    ) -> bool:
        await self._run_cmd(
            ["bash", "-c", "rm -f /etc/redirect-server/config.json 2>/dev/null || true"],
            timeout=15,
        )
        local = TOOLS_DIR / "migrations" / "redirect-server" / "install_redirect_server.sh"
        if local.is_file():
            cmd = (
                f'MAP_FILE="{mapping}" CONFIG_FILE="{cfg_path}" '
                f'bash "{local}" latest'
            )
        else:
            url = (
                "https://raw.githubusercontent.com/PasarGuard/migrations/main/"
                "redirect-server/install_redirect_server.sh"
            )
            cmd = (
                f'MAP_FILE="{mapping}" CONFIG_FILE="{cfg_path}" '
                f"bash -c 'curl -fsSL \"{url}\" | bash -s -- latest'"
            )
        ok, out = await self._run_cmd(["bash", "-c", cmd], timeout=600)
        if not ok:
            self.job.log(
                "Upstream redirect installer failed: "
                f"{(out or '')[-500:]}"
            )
        return ok

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
        target_db = normalize_target_db(target_db)
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
        # Prefer engine-native root/admin secret for sync; URL keeps app user.
        if target_db in ("mysql", "mariadb"):
            sync_pwd = (
                read_env_var(env, "MYSQL_ROOT_PASSWORD")
                or read_env_var(env, "DB_PASSWORD")
                or read_env_var(env, "MYSQL_PASSWORD")
                or ""
            )
        elif target_db in ("postgresql", "timescaledb"):
            sync_pwd = (
                read_env_var(env, "POSTGRES_PASSWORD")
                or read_env_var(env, "DB_PASSWORD")
                or ""
            )
        else:
            sync_pwd = read_env_var(env, "DB_PASSWORD") or ""
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
