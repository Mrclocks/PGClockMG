"""Marzban → PasarGuard migration (fresh install only — PasarGuard must be pre-installed)."""

import asyncio
import re
import shutil
import sqlite3
from pathlib import Path

from app.config import (
    MARZBAN_DIR, MARZBAN_DATA, PASARGUARD_DIR, PASARGUARD_DATA,
    PASARGUARD_ENV, BACKUP_DIR, TOOLS_DIR,
)
from app.services.migrators.base import BaseMigrator
from app.services.native_migration import run_cross_db_migration
from app.services.env_migration import (
    transform_marzban_env,
    transform_compose_marzban_to_pasarguard,
    transform_xray_config,
    fix_mysql_dump_for_pasarguard,
    read_env_var,
    merge_marzban_env_into_pasarguard,
    get_panel_url_from_env,
    _set_sqlalchemy_url,
    _set_env_var_simple,
    build_sqlalchemy_url_for_target,
    finalize_pasarguard_env_after_restore,
    env_points_to_db,
)
from app.services.db_credentials import build_app_sqlalchemy_url, get_source_connection, get_target_connection
from app.services.pasarguard_ops import (
    safe_start_pasarguard,
    resolve_db_service,
)
from app.services.backup_analyzer import resolve_extract_root, find_file_in_upload
from app.services.pg_restore import soft_db_family
from app.services.pg_access import get_panel_access_info


class MarzbanMigrator(BaseMigrator):
    """Marzban → PasarGuard (fresh install only — PasarGuard must be pre-installed)."""

    async def run(self, params: dict) -> dict:
        source_db = params["source_db"]
        target_db = params["target_db"]
        upload_path = params.get("upload_path")
        upload_work_dir = params.get("upload_work_dir")
        marzban_exists = MARZBAN_DIR.exists() or MARZBAN_DATA.exists()

        self.job.log("Marzban migration (fresh PasarGuard install)")
        self.job.set_progress(5, "Starting Marzban → PasarGuard migration...")

        return await self._migrate(
            source_db, target_db, upload_path, marzban_exists, upload_work_dir,
        )

    async def _migrate(
        self, source_db: str, target_db: str,
        upload_path: str | None, marzban_exists: bool, upload_work_dir: str | None = None,
    ) -> dict:
        self.job.set_progress(10, "Preparing fresh PasarGuard installation...")

        work_dir = BACKUP_DIR / f"marzban-{self.job.job_id}"
        work_dir.mkdir(parents=True, exist_ok=True)
        source_sqlite = None
        source_sql = None
        extra_data_dir = None

        if upload_work_dir:
            bundled = Path(upload_work_dir)
            shutil.copytree(bundled, work_dir, dirs_exist_ok=True)
            source_sqlite, source_sql, extra_data_dir = self._parse_work_dir(work_dir, source_db)
            await self._apply_backup_env_and_assets(work_dir, source_db, target_db)
            self.job.log(f"Using upload bundle workspace ({len(list(work_dir.rglob('*')))} items)")
        elif upload_path:
            source_sqlite, source_sql, extra_data_dir = await self._extract_upload(
                upload_path, work_dir, source_db,
            )
            await self._apply_backup_env_and_assets(work_dir, source_db, target_db)
        elif marzban_exists and source_db == "sqlite":
            src = MARZBAN_DATA / "db.sqlite3"
            if not src.exists():
                raise RuntimeError("Marzban db.sqlite3 not found at /var/lib/marzban/")
            source_sqlite = work_dir / "db.sqlite3"
            shutil.copy2(src, source_sqlite)
            extra_data_dir = MARZBAN_DATA
            self.job.log(f"Using live Marzban database: {src}")
        elif marzban_exists and source_db in ("mysql", "mariadb"):
            source_sql = await self._dump_marzban_mysql(work_dir)
        else:
            raise RuntimeError(
                "Marzban backup required — upload ZIP or separate files in the wizard."
            )

        if not PASARGUARD_DIR.exists():
            raise RuntimeError(
                "PasarGuard must be installed manually before migration. "
                "Run the PasarGuard installer first."
            )

        install_env_snapshot = (
            PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
            if PASARGUARD_ENV.exists() else ""
        )

        if source_db != target_db:
            self.job.log(f"Cross-database migration: Marzban {source_db} → PasarGuard {target_db}")

        # Proven path: land Marzban → PasarGuard-shaped via real panel boot (same as
        # sqlite→sqlite), then convert like restore when target is not sqlite.
        if source_db == "sqlite" and source_sqlite:
            await self._migrate_sqlite_like_restore(
                source_sqlite, target_db, extra_data_dir, install_env_snapshot,
            )
        elif source_db in ("mysql", "mariadb") and source_sql:
            await self._migrate_mysql_like_restore(
                source_sql, source_db, target_db, extra_data_dir, install_env_snapshot,
            )
        else:
            raise RuntimeError("Source database file missing for Marzban migration")

        self.job.set_progress(100, "Marzban migration completed")
        return self._result("fresh", target_db)

    async def _migrate_sqlite_like_restore(
        self,
        source_sqlite: Path,
        target_db: str,
        extra_data_dir: Path | None,
        install_env_snapshot: str,
    ) -> None:
        """Marzban SQLite → any target via PasarGuard SQLite upgrade + restore-grade convert."""
        self.job.set_progress(35, "Landing Marzban SQLite into PasarGuard...")
        await self._force_env_sqlite(install_env_snapshot)
        dest = PASARGUARD_DATA / "db.sqlite3"
        PASARGUARD_DATA.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            self._backup_file(dest, BACKUP_DIR)
        shutil.copy2(source_sqlite, dest)
        self.job.log(f"Imported Marzban SQLite → {dest}")
        if extra_data_dir:
            await self._copy_marzban_assets(extra_data_dir)

        self.job.set_progress(50, "Upgrading Marzban schema via PasarGuard panel boot...")
        orig_target = self.params.get("target_db")
        self.params["target_db"] = "sqlite"
        await safe_start_pasarguard(self)
        self.params["target_db"] = orig_target or target_db
        await self._stop_panel()
        self._assert_sqlite_pasarguard_ready(dest)

        if target_db == "sqlite":
            self.job.set_progress(90, "Starting PasarGuard on SQLite...")
            await safe_start_pasarguard(self)
            return

        self.job.set_progress(65, f"Converting PasarGuard SQLite → {target_db} (restore-grade)...")
        await self._convert_pg_sqlite_to_target(dest, target_db, install_env_snapshot)
        if extra_data_dir:
            await self._copy_marzban_assets(extra_data_dir)
        self.job.set_progress(90, "Starting PasarGuard...")
        await safe_start_pasarguard(self)

    async def _migrate_mysql_like_restore(
        self,
        source_sql: Path,
        source_db: str,
        target_db: str,
        extra_data_dir: Path | None,
        install_env_snapshot: str,
    ) -> None:
        """Marzban MySQL/MariaDB → target with panel-boot upgrade + restore-grade convert."""
        same_family = soft_db_family(source_db, target_db) or source_db == target_db

        if same_family:
            self.job.set_progress(40, f"Importing Marzban dump into PasarGuard {target_db}...")
            await self._update_env_paths(source_db, target_db)
            await self._ensure_target_database_stack(target_db)
            await self._import_mysql_dump(source_sql)
            if extra_data_dir:
                await self._copy_marzban_assets(extra_data_dir)
            self.job.set_progress(70, "Upgrading Marzban MySQL schema via panel boot...")
            await safe_start_pasarguard(self)
            return

        self.job.set_progress(40, "Preparing two-phase Marzban MySQL → target...")
        await self._update_env_paths(source_db, target_db)
        await self._ensure_target_database_stack(target_db)
        if extra_data_dir:
            await self._copy_marzban_assets(extra_data_dir)
        self.job.set_progress(50, f"Two-phase: {source_db} → {target_db} (panel-upgrade intermediate)...")
        await run_cross_db_migration(
            self, str(source_sql), source_db, target_db,
            upgrade_via_panel=True,
        )
        self._abort_if_copy_gaps()
        await self._finalize_env_after_convert(target_db, install_env_snapshot)
        self.job.set_progress(90, "Starting PasarGuard...")
        await safe_start_pasarguard(self)

    async def _convert_pg_sqlite_to_target(
        self, sqlite_path: Path, target_db: str, install_env_snapshot: str,
    ) -> None:
        """Restore-grade convert: PasarGuard-shaped SQLite → installed server DB."""
        from app.services.db_auth import (
            migration_params_from_connection,
            resolve_live_admin_connection,
        )

        await self._update_env_paths("sqlite", target_db)
        await self._ensure_target_database_stack(target_db)

        admin = await resolve_live_admin_connection(
            self, target_db, env_text=install_env_snapshot or None,
        )
        self.params = migration_params_from_connection("sqlite", target_db, admin)
        self.params["_resolved_target_conn"] = admin
        self.params["_auto_db_credentials"] = True

        await run_cross_db_migration(self, str(sqlite_path), "sqlite", target_db)
        self._abort_if_copy_gaps()
        await self._finalize_env_after_convert(target_db, install_env_snapshot)
        self._relocate_sqlite_after_convert()

    async def _finalize_env_after_convert(self, target_db: str, install_env_snapshot: str) -> None:
        if not PASARGUARD_ENV.exists():
            return
        text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
        conn = get_target_connection(self.params)
        finalized = finalize_pasarguard_env_after_restore(
            text,
            target_db,
            conn.get("password"),
            install_env_snapshot or text,
            db_user=conn.get("user"),
            db_name=conn.get("database"),
        )
        if not env_points_to_db(finalized, target_db):
            raise RuntimeError(
                f".env SQLALCHEMY_DATABASE_URL does not match target engine {target_db}"
            )
        PASARGUARD_ENV.write_text(finalized, encoding="utf-8")
        self.job.log(f".env finalized for {target_db}")

    def _relocate_sqlite_after_convert(self) -> None:
        sqlite_path = PASARGUARD_DATA / "db.sqlite3"
        if not sqlite_path.exists():
            return
        bak = PASARGUARD_DATA / f"db.sqlite3.pre-convert-{self.job.job_id}.bak"
        if bak.exists():
            bak.unlink()
        shutil.move(str(sqlite_path), str(bak))
        self.job.log(f"Moved SQLite aside → {bak.name} (panel uses server DB)")

    def _abort_if_copy_gaps(self) -> None:
        report = self.copy_report or {}
        if not report.get("has_gaps"):
            return
        crit = report.get("critical_incomplete") or report.get("incomplete") or []
        raise RuntimeError(
            "Migration incomplete — critical tables were not fully copied:\n"
            + ", ".join(
                f"{i.get('table')} {i.get('copied')}/{i.get('source')}" for i in crit
            )
        )

    async def _force_env_sqlite(self, install_env_snapshot: str) -> None:
        """Point live .env at SQLite so panel boot runs Marzban→PasarGuard alembic."""
        if not PASARGUARD_ENV.exists():
            raise RuntimeError(".env not found at /opt/pasarguard — cannot migrate")
        self._backup_file(PASARGUARD_ENV, BACKUP_DIR)
        base = install_env_snapshot or PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
        url = build_sqlalchemy_url_for_target("sqlite", None, base)
        text = _set_sqlalchemy_url(base, url)
        text = _set_env_var_simple(text, "PASARGUARD_DB_ENGINE", "sqlite")
        PASARGUARD_ENV.write_text(text, encoding="utf-8")
        self.job.log(".env temporarily pointed at SQLite for schema upgrade")

    async def _stop_panel(self) -> None:
        await self._run_cmd(
            ["docker", "compose", "stop", "pasarguard"],
            cwd=str(PASARGUARD_DIR),
            timeout=120,
        )

    def _assert_sqlite_pasarguard_ready(self, path: Path) -> None:
        """Ensure panel-boot upgrade produced PasarGuard-shaped data."""
        if not path.exists():
            raise RuntimeError("SQLite intermediate missing after schema upgrade")
        critical = ("users", "admins", "hosts", "inbounds", "nodes", "groups")
        found: dict[str, int] = {}
        try:
            conn = sqlite3.connect(str(path))
            try:
                tables = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                for t in critical:
                    if t not in tables:
                        continue
                    n = int(conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] or 0)
                    if n > 0:
                        found[t] = n
            finally:
                conn.close()
        except Exception as e:
            raise RuntimeError(f"Could not verify upgraded SQLite: {e}") from e

        if "inbounds" not in found and "hosts" not in found and "users" not in found:
            raise RuntimeError(
                "Marzban → PasarGuard schema upgrade left critical tables empty "
                "(users/hosts/inbounds). Aborting before convert."
            )
        self.job.log(
            "PasarGuard SQLite ready: "
            + ", ".join(f"{k}={v}" for k, v in found.items())
        )

    # ─── Helpers ─────────────────────────────────────────────────────

    def _parse_work_dir(self, work_dir: Path, source_db: str):
        source_sqlite = None
        source_sql = None
        extra = None

        for name in ("db.sqlite3", "marzban.db", "x-ui.db"):
            for p in work_dir.rglob(name):
                source_sqlite = p
                break
            if source_sqlite:
                break

        for p in sorted(work_dir.rglob("*.sql")):
            source_sql = p
            break

        for p in work_dir.rglob("xray_config.json"):
            extra = p.parent
            break
        if not extra:
            for name in ("certs", "templates"):
                for p in work_dir.rglob(name):
                    if p.is_dir():
                        extra = p.parent
                        break
                if extra:
                    break

        if source_db == "sqlite" and not source_sqlite:
            raise RuntimeError("No SQLite database found in backup (db.sqlite3)")
        if source_db in ("mysql", "mariadb") and not source_sql:
            raise RuntimeError("No .sql dump found in backup")

        return source_sqlite, source_sql, extra

    async def _extract_upload(self, upload_path: str, work_dir: Path, source_db: str):
        upload = Path(upload_path)
        upload_dir = upload.parent

        if upload.suffix.lower() == ".zip":
            extract_root = resolve_extract_root(upload_dir)
            if extract_root.exists():
                for p in extract_root.rglob("*"):
                    if p.is_file():
                        rel = p.relative_to(extract_root)
                        dest = work_dir / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(p, dest)
                self.job.log(f"Using pre-extracted backup ({len(list(work_dir.rglob('*')))} items)")
            else:
                ok, _ = await self._run_cmd(["unzip", "-o", str(upload), "-d", str(work_dir)])
                if not ok:
                    raise RuntimeError("Failed to extract zip backup")
        else:
            shutil.copy2(upload, work_dir / upload.name)

        source_sqlite = find_file_in_upload(upload_dir, ("db.sqlite3", "marzban.db"))
        if source_sqlite and source_sqlite.parent != work_dir:
            dest = work_dir / source_sqlite.name
            if not dest.exists():
                shutil.copy2(source_sqlite, dest)
            source_sqlite = dest
        else:
            source_sqlite = None
            for name in ("db.sqlite3", "marzban.db"):
                for p in work_dir.rglob(name):
                    source_sqlite = p
                    break
                if source_sqlite:
                    break

        source_sql = find_file_in_upload(upload_dir, ("marzban.sql",))
        if not source_sql:
            for p in sorted(work_dir.rglob("*.sql")):
                source_sql = p
                break
        if not source_sql and upload.suffix.lower() == ".sql":
            source_sql = work_dir / upload.name

        extra = None
        for p in work_dir.rglob("xray_config.json"):
            extra = p.parent
            break
        if not extra:
            for name in ("certs", "templates"):
                for p in work_dir.rglob(name):
                    if p.is_dir():
                        extra = p.parent
                        break
                if extra:
                    break

        source_sqlite, source_sql, extra_parsed = self._parse_work_dir(work_dir, source_db)
        extra = extra or extra_parsed
        self.job.log(f"Backup parsed: sqlite={source_sqlite}, sql={source_sql}, assets={extra}")
        return source_sqlite, source_sql, extra

    async def _apply_backup_env_and_assets(self, work_dir: Path, source_db: str, target_db: str):
        """Map Marzban backup settings to PasarGuard per official docs."""
        env_file = work_dir / ".env"
        if not env_file.exists():
            for p in work_dir.rglob(".env"):
                env_file = p
                break
        if env_file and env_file.exists() and PASARGUARD_ENV.exists():
            marzban_env = env_file.read_text(encoding="utf-8", errors="ignore")
            pg_env = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
            pwd = get_source_connection(self.params).get("password") or read_env_var(marzban_env, "MYSQL_ROOT_PASSWORD")
            merged = merge_marzban_env_into_pasarguard(pg_env, marzban_env, target_db, pwd)
            self._backup_file(PASARGUARD_ENV, BACKUP_DIR)
            PASARGUARD_ENV.write_text(merged, encoding="utf-8")
            self.job.log("Merged Marzban .env settings into PasarGuard .env")

        compose_file = None
        for name in ("docker-compose.yml", "docker-compose.yaml"):
            for p in work_dir.rglob(name):
                compose_file = p
                break
        pg_compose = PASARGUARD_DIR / "docker-compose.yml"
        if pg_compose.exists():
            text = pg_compose.read_text(encoding="utf-8", errors="ignore")
            if "marzban" in text.lower():
                self._backup_file(pg_compose, BACKUP_DIR)
                pg_compose.write_text(transform_compose_marzban_to_pasarguard(text), encoding="utf-8")
                self.job.log("Fixed marzban paths in PasarGuard docker-compose.yml")
        elif compose_file:
            text = transform_compose_marzban_to_pasarguard(compose_file.read_text(encoding="utf-8", errors="ignore"))
            pg_compose.write_text(text, encoding="utf-8")
            self.job.log("Wrote docker-compose.yml from backup mapping")

        data_src = work_dir
        for candidate in work_dir.rglob("xray_config.json"):
            data_src = candidate.parent
            break
        await self._copy_marzban_assets(data_src)

    async def _ensure_target_database_stack(self, target_db: str):
        """Start target DB services before cross-DB migration."""
        if target_db == "sqlite":
            return

        compose_path = PASARGUARD_DIR / "docker-compose.yml"
        text = compose_path.read_text(encoding="utf-8", errors="ignore") if compose_path.exists() else ""

        svc_map = {
            "timescaledb": "timescaledb",
            "postgresql": "postgresql",
            "mysql": "mysql",
            "mariadb": "mariadb",
        }
        svc = svc_map.get(target_db)
        if not svc:
            return

        if svc not in text:
            raise RuntimeError(
                f"Database service `{svc}` is not in /opt/pasarguard/docker-compose.yml. "
                f"Install PasarGuard yourself with --database {target_db} first "
                "(see the Guide tab), then retry migration."
            )

        self.job.log(f"Starting {svc} container...")
        await self._run_cmd(["docker", "compose", "up", "-d", svc], cwd=str(PASARGUARD_DIR))
        await asyncio.sleep(10)

    async def _copy_marzban_assets(self, source_data: Path):
        """Copy certs, templates, xray_config from Marzban data dir."""
        PASARGUARD_DATA.mkdir(parents=True, exist_ok=True)
        for item in ("certs", "templates"):
            src = source_data / item
            if not src.exists():
                for p in source_data.rglob(item):
                    if p.is_dir():
                        src = p
                        break
            dst = PASARGUARD_DATA / item
            if src.exists():
                if dst.exists():
                    shutil.rmtree(dst, ignore_errors=True)
                shutil.copytree(src, dst)
                self.job.log(f"Copied {item}/ → /var/lib/pasarguard/{item}/")
        v2ray = PASARGUARD_DATA / "templates" / "v2ray"
        xray = PASARGUARD_DATA / "templates" / "xray"
        if v2ray.exists() and not xray.exists():
            v2ray.rename(xray)
            self.job.log("Renamed templates/v2ray → templates/xray")
        for p in source_data.rglob("xray_config.json"):
            text = transform_xray_config(p.read_text(encoding="utf-8", errors="ignore"))
            dst = PASARGUARD_DATA / "xray_config.json"
            dst.write_text(text, encoding="utf-8")
            self.job.log("Copied xray_config.json → /var/lib/pasarguard/")
            break

    async def _dump_marzban_mysql(self, work_dir: Path) -> Path:
        conn = get_source_connection(self.params)
        pwd = conn.get("password") or ""
        dump_path = work_dir / "marzban.sql"
        if MARZBAN_DIR.exists():
            proc = await asyncio.create_subprocess_shell(
                f'cd "{MARZBAN_DIR}" && docker compose exec -T mysql '
                f'mysqldump -u root -p"{pwd}" -h 127.0.0.1 --databases marzban > "{dump_path}"',
            )
            await proc.wait()
        if not dump_path.exists():
            raise RuntimeError("Failed to dump Marzban MySQL — check password and docker")
        text = fix_mysql_dump_for_pasarguard(dump_path.read_text(encoding="utf-8", errors="ignore"))
        dump_path.write_text(text, encoding="utf-8")
        return dump_path

    async def _import_mysql_dump(self, dump_file: Path):
        conn = get_target_connection(self.params)
        user = conn.get("user") or "root"
        pwd = conn.get("password") or ""
        db = conn.get("database") or "pasarguard"
        host = conn.get("host") or "127.0.0.1"
        fixed = dump_file.parent / "fixed_import.sql"
        text = fix_mysql_dump_for_pasarguard(dump_file.read_text(encoding="utf-8", errors="ignore"))
        fixed.write_text(text, encoding="utf-8")
        svc = resolve_db_service("mysql") or resolve_db_service("mariadb") or "mysql"
        await self._run_cmd(["docker", "compose", "up", "-d", svc], cwd=str(PASARGUARD_DIR))
        await asyncio.sleep(6)
        wipe = await asyncio.create_subprocess_shell(
            f'cd "{PASARGUARD_DIR}" && docker compose exec -T {svc} '
            f'mysql -u {user} -p"{pwd}" -h {host} -e '
            f'"DROP DATABASE IF EXISTS `{db}`; CREATE DATABASE `{db}`; "'
        )
        await wipe.wait()
        proc = await asyncio.create_subprocess_shell(
            f'cd "{PASARGUARD_DIR}" && docker compose exec -T {svc} '
            f'mysql -u {user} -p"{pwd}" -h {host} {db} < "{fixed}"',
        )
        await proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(
                f"Failed to import Marzban MySQL dump into PasarGuard "
                f"(exit {proc.returncode}). Check DB credentials and container logs."
            )

    async def _update_env_paths(self, source_db: str, target_db: str):
        env_path = PASARGUARD_DIR / ".env"
        if not env_path.exists():
            raise RuntimeError(".env not found at /opt/pasarguard — cannot migrate settings")
        self._backup_file(env_path, BACKUP_DIR)
        original = env_path.read_text(encoding="utf-8", errors="ignore")
        sqlalchemy_url = build_app_sqlalchemy_url(self.params)
        text = _set_sqlalchemy_url(original, sqlalchemy_url)
        text = _set_env_var_simple(text, "PASARGUARD_DB_ENGINE", target_db)
        env_path.write_text(text, encoding="utf-8")
        self.job.log(".env updated for target database")

    def _result(self, method: str, target_db: str) -> dict:
        access = get_panel_access_info()
        env_text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore") if PASARGUARD_ENV.exists() else None
        port = read_env_var(env_text, "UVICORN_PORT") if env_text else None
        root = (read_env_var(env_text, "UVICORN_ROOT_PATH") or "").rstrip("/") if env_text else ""
        out = {
            "panel_url": access.get("login_url") or get_panel_url_from_env(env_text),
            "panel_port": port or access.get("port") or "8000",
            "panel_root_path": root or access.get("root_path") or "/",
            "login_url": access.get("login_url"),
            "root_path": access.get("root_path"),
            "subscription_mode": "native",
            "method": method,
            "target_db": target_db,
        }
        if self.copy_report:
            out["copy_report"] = self.copy_report
        return out

    def _get_panel_url(self) -> str:
        env_text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore") if PASARGUARD_ENV.exists() else None
        return get_panel_url_from_env(env_text)
