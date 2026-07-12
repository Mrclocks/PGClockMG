"""Marzban → PasarGuard migration (official docs — two methods)."""

import asyncio
import shutil
from pathlib import Path

from app.config import (
    MARZBAN_DIR, MARZBAN_DATA, PASARGUARD_DIR, PASARGUARD_DATA,
    PASARGUARD_ENV, BACKUP_DIR, TOOLS_DIR,
)
from app.services.migrators.base import BaseMigrator
from app.services.db_migration import run_db_migration, build_target_url
from app.services.env_migration import (
    transform_marzban_env,
    transform_compose_marzban_to_pasarguard,
    transform_xray_config,
    fix_mysql_dump_for_pasarguard,
    read_env_var,
    merge_marzban_env_into_pasarguard,
)
from app.services.backup_analyzer import resolve_extract_root, find_file_in_upload


class MarzbanMigrator(BaseMigrator):
    """
    Methods (per https://docs.pasarguard.org/en/migration/marzban/):
    - inplace: Marzban installed on THIS server → rename dirs in-place
    - fresh: Fresh PasarGuard on THIS server → import backup / live db
    """

    async def run(self, params: dict) -> dict:
        mode = params.get("marzban_mode") or "auto"
        source_db = params["source_db"]
        target_db = params["target_db"]
        password = params.get("target_db_password") or params.get("source_db_password")
        upload_path = params.get("upload_path")
        upload_work_dir = params.get("upload_work_dir")

        marzban_exists = MARZBAN_DIR.exists() or MARZBAN_DATA.exists()
        pg_exists = PASARGUARD_DIR.exists()

        if mode == "auto":
            if upload_path or upload_work_dir:
                mode = "fresh"
            elif marzban_exists and not pg_exists:
                mode = "inplace"
            elif marzban_exists and pg_exists:
                mode = "fresh"
            else:
                mode = "fresh"

        self.job.log(f"Marzban migration mode: {mode}")
        self.job.set_progress(5, f"Marzban migration ({mode})...")

        if mode == "inplace":
            return await self._migrate_inplace(source_db, target_db, password)
        return await self._migrate_fresh(
            source_db, target_db, password, upload_path, marzban_exists, upload_work_dir,
        )

    # ─── Method 1: In-place (Marzban on this server) ─────────────────

    async def _migrate_inplace(self, source_db: str, target_db: str, password: str | None) -> dict:
        if not MARZBAN_DIR.exists() and not MARZBAN_DATA.exists():
            raise RuntimeError(
                "In-place mode requires Marzban on this server (/opt/marzban). "
                "Use 'Fresh PasarGuard' mode or upload a backup."
            )
        if PASARGUARD_DIR.exists():
            raise RuntimeError(
                "PasarGuard is already installed. In-place mode requires ONLY Marzban. "
                "Choose 'Fresh PasarGuard' method instead."
            )

        self.job.set_progress(10, "Stopping Marzban...")
        if MARZBAN_DIR.exists():
            await self._run_cmd(["docker", "compose", "down"], cwd=str(MARZBAN_DIR))

        self.job.set_progress(15, "Removing old PasarGuard paths if any...")
        for p in [PASARGUARD_DIR, PASARGUARD_DATA, Path("/var/lib/mysql/pasarguard")]:
            if p.exists():
                shutil.rmtree(p)

        self.job.set_progress(25, "Renaming Marzban → PasarGuard directories...")
        if MARZBAN_DIR.exists():
            MARZBAN_DIR.rename(PASARGUARD_DIR)
            self.job.log(f"Renamed {MARZBAN_DIR} → {PASARGUARD_DIR}")

        if MARZBAN_DATA.exists():
            MARZBAN_DATA.rename(PASARGUARD_DATA)
            self.job.log(f"Renamed {MARZBAN_DATA} → {PASARGUARD_DATA}")

        mysql_marzban = Path("/var/lib/mysql/marzban")
        if mysql_marzban.exists():
            mysql_marzban.rename(Path("/var/lib/mysql/pasarguard"))
            self.job.log("Renamed MySQL data directory")
        else:
            await self._relocate_mysql_from_data_dir()

        self.job.set_progress(40, "Updating .env and docker-compose (official mapping)...")
        await self._update_env_paths(source_db, source_db)  # keep source driver first
        await self._update_compose()
        await self._update_xray_paths()

        sqlite_backup = None
        if source_db in ("mysql", "mariadb"):
            self.job.set_progress(50, "Migrating MySQL/MariaDB database marzban → pasarguard...")
            await self._rename_mysql_database(password, source_db)
        elif source_db == "sqlite":
            sqlite_backup = PASARGUARD_DATA / "db.sqlite3"
            if sqlite_backup.exists():
                self._backup_file(sqlite_backup, BACKUP_DIR)

        # Cross-DB migration (e.g. sqlite → timescaledb)
        if source_db != target_db:
            self.job.set_progress(60, f"Cross-database migration: {source_db} → {target_db}...")
            await self._ensure_target_database_stack(target_db, password)
            await self._update_env_paths(source_db, target_db, password)
            await self._wait_pasarguard_schema(target_db, password)
            source_path = await self._resolve_source_for_db_migration(source_db, sqlite_backup)
            await run_db_migration(self, source_path, source_db, target_db, password)
            await self._update_compose_for_target_db(target_db)
        else:
            await self._update_env_paths(source_db, target_db, password)

        self.job.set_progress(85, "Installing PasarGuard management script...")
        await self._install_pasarguard_script()

        self.job.set_progress(92, "Starting PasarGuard...")
        await self._start_pasarguard()

        self.job.set_progress(100, "In-place Marzban migration completed")
        return self._result("inplace", target_db)

    # ─── Method 2: Fresh PasarGuard (new installation) ───────────────

    async def _migrate_fresh(
        self, source_db: str, target_db: str, password: str | None,
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
            await self._apply_backup_env_and_assets(work_dir, source_db, target_db, password)
            self.job.log(f"Using upload bundle workspace ({len(list(work_dir.rglob('*')))} items)")
        elif upload_path:
            source_sqlite, source_sql, extra_data_dir = await self._extract_upload(
                upload_path, work_dir, source_db,
            )
            await self._apply_backup_env_and_assets(work_dir, source_db, target_db, password)
        elif marzban_exists and source_db == "sqlite":
            src = MARZBAN_DATA / "db.sqlite3"
            if not src.exists():
                raise RuntimeError("Marzban db.sqlite3 not found at /var/lib/marzban/")
            source_sqlite = work_dir / "db.sqlite3"
            shutil.copy2(src, source_sqlite)
            extra_data_dir = MARZBAN_DATA
            self.job.log(f"Using live Marzban database: {src}")
        elif marzban_exists and source_db in ("mysql", "mariadb"):
            source_sql = await self._dump_marzban_mysql(work_dir, password)
        else:
            raise RuntimeError(
                "Fresh mode: install PasarGuard first OR provide Marzban backup upload."
            )

        self.job.set_progress(25, "Preparing fresh PasarGuard migration...")
        if not PASARGUARD_DIR.exists():
            raise RuntimeError(
                "Fresh mode: PasarGuard must be installed manually on this server first. "
                "Run the PasarGuard installer, then return to this wizard."
            )

        self.job.set_progress(40, "Waiting for PasarGuard schema initialization...")
        await self._wait_pasarguard_schema(target_db, password)

        if source_db == target_db:
            self.job.set_progress(55, "Same DB type — direct import...")
            if source_db == "sqlite" and source_sqlite:
                dest = PASARGUARD_DATA / "db.sqlite3"
                PASARGUARD_DATA.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    self._backup_file(dest, BACKUP_DIR)
                shutil.copy2(source_sqlite, dest)
                self.job.log(f"Imported SQLite → {dest}")
            elif source_db in ("mysql", "mariadb") and source_sql:
                await self._import_mysql_dump(source_sql, password)
        else:
            self.job.set_progress(55, f"Cross-DB import: {source_db} → {target_db}...")
            await self._ensure_target_database_stack(target_db, password)
            await self._update_env_paths(source_db, target_db, password)
            if source_db == "sqlite":
                if not source_sqlite:
                    raise RuntimeError("SQLite source file missing")
                migration_source = str(source_sqlite)
            else:
                migration_source = str(source_sql) if source_sql else ""
                if not migration_source:
                    raise RuntimeError("SQL dump missing for cross-DB migration")
            await run_db_migration(self, migration_source, source_db, target_db, password)
            await self._update_env_paths(source_db, target_db, password)

        if extra_data_dir:
            await self._copy_marzban_assets(extra_data_dir)

        self.job.set_progress(90, "Starting PasarGuard...")
        await self._start_pasarguard()

        self.job.set_progress(100, "Fresh PasarGuard migration completed")
        return self._result("fresh", target_db)

    # ─── Helpers ─────────────────────────────────────────────────────

    async def _relocate_mysql_from_data_dir(self):
        """Docs: mv /var/lib/pasarguard/mysql/* -> /var/lib/mysql/pasarguard"""
        src = PASARGUARD_DATA / "mysql"
        dst = Path("/var/lib/mysql/pasarguard")
        if not src.exists():
            return
        dst.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            target = dst / item.name
            if target.exists():
                continue
            shutil.move(str(item), str(target))
        shutil.rmtree(src, ignore_errors=True)
        self.job.log("Relocated MySQL data from /var/lib/pasarguard/mysql")

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

    async def _apply_backup_env_and_assets(self, work_dir: Path, source_db: str, target_db: str, password: str | None):
        """Map Marzban backup settings to PasarGuard per official docs."""
        env_file = work_dir / ".env"
        if not env_file.exists():
            for p in work_dir.rglob(".env"):
                env_file = p
                break
        if env_file and env_file.exists() and PASARGUARD_ENV.exists():
            marzban_env = env_file.read_text(encoding="utf-8", errors="ignore")
            pg_env = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
            pwd = password or read_env_var(marzban_env, "MYSQL_ROOT_PASSWORD")
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

    async def _ensure_target_database_stack(self, target_db: str, password: str | None):
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
            self.job.log(f"{svc} not in docker-compose — adding via PasarGuard installer...")
            flags = {
                "mysql": "--database mysql",
                "mariadb": "--database mariadb",
                "postgresql": "--database postgresql",
                "timescaledb": "--database timescaledb",
            }
            flag = flags.get(target_db, "")
            await self._run_cmd([
                "bash", "-c",
                f"curl -fsSL https://github.com/PasarGuard/scripts/raw/main/pasarguard.sh | bash -s -- @ install {flag}".strip()
            ], timeout=900)

        self.job.log(f"Starting {svc} container...")
        await self._run_cmd(["docker", "compose", "up", "-d", svc], cwd=str(PASARGUARD_DIR))
        await asyncio.sleep(10)

    async def _resolve_source_for_db_migration(self, source_db: str, sqlite_path: Path | None) -> str:
        if source_db == "sqlite":
            p = sqlite_path or (PASARGUARD_DATA / "db.sqlite3")
            if not p.exists():
                raise RuntimeError("SQLite source missing after in-place rename")
            return str(p)
        raise RuntimeError(f"In-place cross-DB from {source_db} requires manual SQL export")

    async def _wait_pasarguard_schema(self, target_db: str, password: str | None):
        """Start PG once so Alembic migrations create schema on target DB."""
        await self._run_cmd(["docker", "compose", "up", "-d"], cwd=str(PASARGUARD_DIR))
        await asyncio.sleep(15)
        await self._run_cmd(["pasarguard", "restart"])
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

    async def _rename_mysql_database(self, password: str | None, db_engine: str = "mysql"):
        env_path = PASARGUARD_DIR / ".env"
        env_text = env_path.read_text(encoding="utf-8", errors="ignore") if env_path.exists() else ""
        pwd = password or read_env_var(env_text, "MYSQL_ROOT_PASSWORD") or read_env_var(env_text, "MYSQL_PASSWORD") or ""
        if not pwd:
            raise RuntimeError("MYSQL_ROOT_PASSWORD not found in .env — enter it in the wizard")

        svc = "mariadb" if db_engine == "mariadb" else "mysql"
        dump_cmd = "mariadb-dump" if db_engine == "mariadb" else "mysqldump"
        compose_dir = str(PASARGUARD_DIR)
        dump_path = PASARGUARD_DIR / "marzban_export.sql"

        await self._run_cmd(["docker", "compose", "up", "-d", svc], cwd=compose_dir)
        await asyncio.sleep(8)

        for user in ("root", "marzban"):
            proc = await asyncio.create_subprocess_shell(
                f'cd "{compose_dir}" && docker compose exec -T {svc} '
                f'{dump_cmd} -u {user} -p"{pwd}" -h 127.0.0.1 --databases marzban > "{dump_path}"',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            await proc.wait()
            if dump_path.exists() and dump_path.stat().st_size > 100:
                self.job.log(f"MySQL dump OK (user={user})")
                break

        if not dump_path.exists() or dump_path.stat().st_size < 100:
            raise RuntimeError("MySQL export failed — verify MYSQL_ROOT_PASSWORD in .env")

        text = fix_mysql_dump_for_pasarguard(dump_path.read_text(encoding="utf-8", errors="ignore"))
        dump_path.write_text(text, encoding="utf-8")

        import_cmd = "mariadb" if db_engine == "mariadb" else "mysql"
        proc2 = await asyncio.create_subprocess_shell(
            f'cd "{compose_dir}" && docker compose exec -T {svc} '
            f'{import_cmd} -u root -p"{pwd}" -h 127.0.0.1 < "{dump_path}"',
        )
        await proc2.wait()

        await self._run_cmd([
            "docker", "compose", "exec", "-T", svc,
            import_cmd, "-u", "root", f"-p{pwd}", "-h", "127.0.0.1",
            "-e", "DROP DATABASE IF EXISTS marzban;",
        ], cwd=compose_dir)
        dump_path.unlink(missing_ok=True)
        self.job.log("MySQL database migrated marzban → pasarguard")

    async def _dump_marzban_mysql(self, work_dir: Path, password: str | None) -> Path:
        pwd = password or ""
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

    async def _import_mysql_dump(self, dump_file: Path, password: str | None):
        pwd = password or ""
        fixed = dump_file.parent / "fixed_import.sql"
        text = fix_mysql_dump_for_pasarguard(dump_file.read_text(encoding="utf-8", errors="ignore"))
        fixed.write_text(text, encoding="utf-8")
        await self._run_cmd(["docker", "compose", "up", "-d", "mysql"], cwd=str(PASARGUARD_DIR))
        await asyncio.sleep(6)
        proc = await asyncio.create_subprocess_shell(
            f'cd "{PASARGUARD_DIR}" && docker compose exec -T mysql '
            f'mysql -u root -p"{pwd}" -h 127.0.0.1 < "{fixed}"',
        )
        await proc.wait()

    async def _update_env_paths(self, source_db: str, target_db: str, password: str | None = None):
        env_path = PASARGUARD_DIR / ".env"
        if not env_path.exists():
            raise RuntimeError(".env not found at /opt/pasarguard — cannot migrate settings")
        self._backup_file(env_path, BACKUP_DIR)
        original = env_path.read_text(encoding="utf-8", errors="ignore")
        text = transform_marzban_env(original, target_db, password)
        env_path.write_text(text, encoding="utf-8")
        self.job.log(".env migrated (paths, drivers, subscription template)")

    async def _update_compose(self):
        compose_path = PASARGUARD_DIR / "docker-compose.yml"
        if not compose_path.exists():
            return
        self._backup_file(compose_path, BACKUP_DIR)
        text = compose_path.read_text(encoding="utf-8", errors="ignore")
        compose_path.write_text(transform_compose_marzban_to_pasarguard(text), encoding="utf-8")
        self.job.log("docker-compose.yml updated")

    async def _update_compose_for_target_db(self, target_db: str):
        """Ensure compose uses correct DB service — user may need manual review."""
        self.job.log(f"Target DB stack: {target_db} — verify docker-compose.yml if needed")

    async def _update_xray_paths(self):
        v2 = PASARGUARD_DATA / "templates" / "v2ray"
        xray = PASARGUARD_DATA / "templates" / "xray"
        if v2.exists() and not xray.exists():
            v2.rename(xray)
        cfg = PASARGUARD_DATA / "xray_config.json"
        if cfg.exists():
            self._backup_file(cfg, BACKUP_DIR)
            t = transform_xray_config(cfg.read_text(encoding="utf-8", errors="ignore"))
            cfg.write_text(t, encoding="utf-8")

    async def _install_pasarguard_script(self):
        await self._run_cmd([
            "bash", "-c",
            "curl -sL https://github.com/PasarGuard/scripts/raw/main/pasarguard.sh | bash -s -- @ install-script"
        ])

    async def _start_pasarguard(self):
        ok, _ = await self._run_cmd(["pasarguard", "restart"])
        if not ok:
            await self._run_cmd(["docker", "compose", "up", "-d"], cwd=str(PASARGUARD_DIR))

    def _result(self, method: str, target_db: str) -> dict:
        return {
            "panel_url": self._get_panel_url(),
            "subscription_mode": "native",
            "method": method,
            "target_db": target_db,
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
