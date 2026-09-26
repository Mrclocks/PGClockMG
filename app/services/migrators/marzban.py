"""Marzban → PasarGuard migration (fresh install only — PasarGuard must be pre-installed)."""

import asyncio
import re
import shutil
import sqlite3
import time
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
    rewrite_mysql_dump_file_for_pasarguard,
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
    mysql_admin_bins,
    mysql_client_bins,
    normalize_target_db,
    safe_start_pasarguard,
    resolve_db_service,
)
from app.services.backup_analyzer import resolve_extract_root, find_file_in_upload
from app.services.pg_restore import soft_db_family
from app.services.pg_access import get_panel_access_info
from app.services.marzban_inbound_certs import relocate_inbound_certs_in_xray_config
from app.services.marzban_core_sync import (
    assert_server_core_matches_xray,
    assert_sqlite_core_matches_xray,
    pin_xray_json_env,
    require_marzban_xray_on_disk,
    sync_core_from_xray_server_db,
    sync_core_from_xray_sqlite,
    xray_config_path,
)
from app.services.marzban_migrate_heal import (
    is_transient_infra_error,
    normalize_templates_layout,
    safe_replace_tree,
)


class MarzbanMigrator(BaseMigrator):
    """Marzban → PasarGuard (fresh install only — PasarGuard must be pre-installed)."""

    async def run(self, params: dict) -> dict:
        source_db = params["source_db"]
        target_db = params["target_db"]
        upload_path = params.get("upload_path")
        upload_work_dir = params.get("upload_work_dir")
        marzban_exists = MARZBAN_DIR.exists() or MARZBAN_DATA.exists()

        self.job.log("Marzban migration (fresh PasarGuard install)")
        # Default ON: skip broken user rows and continue (still abort if zero users land).
        if "skip_bad_user_rows" not in params:
            params["skip_bad_user_rows"] = True
            self.params["skip_bad_user_rows"] = True
        if params.get("skip_bad_user_rows"):
            self.job.log("Optimization: skip broken user rows and continue with report")
        if params.get("relocate_inbound_certs"):
            self.job.log("Optimization: relocate inbound TLS certs into PasarGuard certs/")
        # Default ON: leave nodes disabled so old Marzban/nodes cannot conflict.
        if "disable_nodes_after_migrate" not in params:
            params["disable_nodes_after_migrate"] = True
            self.params["disable_nodes_after_migrate"] = True
        if params.get("disable_nodes_after_migrate"):
            self.job.log("Optimization: disable nodes after Marzban migration (default on)")
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
            await self._apply_live_marzban_env(target_db)
        elif marzban_exists and source_db in ("mysql", "mariadb"):
            source_sql = await self._dump_marzban_mysql(work_dir)
            # Same as live SQLite: copy certs/xray_config and merge .env so panel
            # alembic can seed core_configs/inbounds (previously left extra_data_dir=None).
            if MARZBAN_DATA.exists():
                extra_data_dir = MARZBAN_DATA
                self.job.log(f"Using live Marzban data dir for assets: {MARZBAN_DATA}")
            await self._apply_live_marzban_env(target_db)
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
        await self._maybe_disable_nodes(target_db)
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

        self.job.set_progress(48, "Healing Marzban dump for PasarGuard constraints...")
        from app.services.marzban_preboot_heal import heal_marzban_preboot

        # Safe no-op on clean dumps; fixes case-dup names + orphan FKs on dirty/large ones.
        orig_target = self.params.get("target_db")
        self.params["target_db"] = "sqlite"
        await heal_marzban_preboot(self)
        self.job.set_progress(50, "Upgrading Marzban schema via PasarGuard panel boot...")
        # Long Marzban→PG alembic chains (bigint id, etc.) need a large health budget.
        self._maybe_relocate_inbound_certs()
        self._prepare_xray_for_panel_boot()
        await self._safe_start_with_heal(health_max_wait=1800)
        self.params["target_db"] = orig_target or target_db
        await self._stop_panel()
        self._assert_sqlite_pasarguard_ready(dest)

        if target_db == "sqlite":
            self.job.set_progress(90, "Starting PasarGuard on SQLite...")
            self._prepare_xray_for_panel_boot()
            await self._safe_start_with_heal()
            await self._assert_target_pasarguard_ready("sqlite")
            return

        self.job.set_progress(65, f"Converting PasarGuard SQLite → {target_db} (restore-grade)...")
        await self._convert_pg_sqlite_to_target(dest, target_db, install_env_snapshot)
        if extra_data_dir:
            await self._copy_marzban_assets(extra_data_dir)
        self.job.set_progress(90, "Starting PasarGuard...")
        self._prepare_xray_for_panel_boot()
        await self._safe_start_with_heal()
        await self._assert_target_pasarguard_ready(target_db)

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

        # Defense in depth: validation already blocks this, but fail early with a clear
        # message if called directly (migration_strategy would return unsupported late).
        if target_db == "sqlite" and source_db != "sqlite":
            raise RuntimeError(
                f"Cannot convert Marzban {source_db} → sqlite. "
                "Install PasarGuard with MySQL/MariaDB/PostgreSQL/TimescaleDB, then retry."
            )

        if same_family:
            self.job.set_progress(40, f"Importing Marzban dump into PasarGuard {target_db}...")
            await self._update_env_paths(source_db, target_db)
            await self._ensure_target_database_stack(target_db)
            await self._import_mysql_dump(source_sql)
            if extra_data_dir:
                await self._copy_marzban_assets(extra_data_dir)
            # Dump is in PasarGuard target DB (Marzban live untouched). Heal unknown stamp.
            from app.services.native_migration.cross_db import _heal_staging_alembic_if_unknown

            tconn = dict(get_target_connection(self.params))
            tconn["_allow_live_alembic_heal"] = True
            await _heal_staging_alembic_if_unknown(self, source_db, tconn)
            self.job.set_progress(68, "Healing Marzban dump for PasarGuard constraints...")
            from app.services.marzban_preboot_heal import heal_marzban_preboot

            await heal_marzban_preboot(self)
            self.job.set_progress(70, "Upgrading Marzban MySQL schema via panel boot...")
            # Large dumps: alembic may spend a long time on "use bigint for id column".
            self._maybe_relocate_inbound_certs()
            self._prepare_xray_for_panel_boot()
            await self._safe_start_with_heal(health_max_wait=1800)
            await self._assert_target_pasarguard_ready(target_db)
            return

        self.job.set_progress(40, "Preparing two-phase Marzban MySQL → target...")
        await self._update_env_paths(source_db, target_db)
        await self._ensure_target_database_stack(target_db)
        if extra_data_dir:
            await self._copy_marzban_assets(extra_data_dir)
        self.job.set_progress(50, f"Two-phase: {source_db} → {target_db} (panel-upgrade intermediate)...")
        self._maybe_relocate_inbound_certs()
        self._prepare_xray_for_panel_boot()
        await run_cross_db_migration(
            self, str(source_sql), source_db, target_db,
            upgrade_via_panel=True,
        )
        self._abort_if_copy_gaps()
        self._abort_if_inbounds_missing_from_stats(getattr(self, "copy_stats", None))
        await self._finalize_env_after_convert(target_db, install_env_snapshot)
        self.job.set_progress(90, "Starting PasarGuard...")
        self._prepare_xray_for_panel_boot()
        await self._safe_start_with_heal()
        await self._assert_target_pasarguard_ready(target_db)

    async def _convert_pg_sqlite_to_target(
        self, sqlite_path: Path, target_db: str, install_env_snapshot: str,
    ) -> None:
        """PasarGuard-shaped SQLite → installed server DB via the proven restore convert path.

        Keep .env on SQLite until convert finishes (same as change-DB / restore→convert),
        then finalize for the target engine.
        """
        from app.services.pg_restore import _maybe_cross_db_after_restore

        dest = PASARGUARD_DATA / "db.sqlite3"
        PASARGUARD_DATA.mkdir(parents=True, exist_ok=True)
        if Path(sqlite_path).resolve() != dest.resolve():
            shutil.copy2(sqlite_path, dest)

        # Keep a side copy of the upgraded PasarGuard SQLite before convert.
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        side = BACKUP_DIR / f"marzban-pg-ready-{self.job.job_id}.sqlite3"
        shutil.copy2(dest, side)
        self.job.log(f"Saved PasarGuard SQLite snapshot → {side.name}")

        # Critical: do NOT point .env at target before convert — that was emptying panels.
        await self._force_env_sqlite(install_env_snapshot)
        await self._ensure_target_database_stack(target_db)

        env = install_env_snapshot or ""
        user = (
            read_env_var(env, "DB_USER")
            or read_env_var(env, "POSTGRES_USER")
            or read_env_var(env, "MYSQL_USER")
            or "pasarguard"
        )
        password = (
            read_env_var(env, "DB_PASSWORD")
            or read_env_var(env, "POSTGRES_PASSWORD")
            or read_env_var(env, "MYSQL_ROOT_PASSWORD")
            or read_env_var(env, "MYSQL_PASSWORD")
            or ""
        )
        db_name = (
            read_env_var(env, "DB_NAME")
            or read_env_var(env, "POSTGRES_DB")
            or read_env_var(env, "MYSQL_DATABASE")
            or "pasarguard"
        )

        self.job.log(
            f"Converting PasarGuard SQLite → {target_db} (restore-grade path)..."
        )
        _final_db, stats, report = await _maybe_cross_db_after_restore(
            self.job,
            dict(self.params or {}),
            "sqlite",
            target_db,
            password,
            user,
            db_name,
            source_path=str(dest),
            install_env_snapshot=install_env_snapshot,
        )
        self.copy_stats = stats or {}
        self.copy_report = report or {}

        live_admin = (report or {}).get("live_admin") or {}
        if live_admin:
            self.params = {
                **(self.params or {}),
                "source_db": "sqlite",
                "target_db": target_db,
                "target_db_user": live_admin.get("user") or user,
                "target_db_password": live_admin.get("password") or password,
                "target_db_name": live_admin.get("database") or db_name,
                "_resolved_target_conn": {**live_admin, "db_type": target_db},
                "_auto_db_credentials": True,
            }

        self._abort_if_copy_gaps()
        self._abort_if_empty_convert(side if side.exists() else dest, stats)
        await self._finalize_env_after_convert(target_db, install_env_snapshot)
        self._relocate_sqlite_after_convert()

    def _abort_if_empty_convert(self, sqlite_path: Path, stats: dict | None) -> None:
        """Refuse success when source SQLite had rows but convert copied nothing."""
        stats = stats or {}
        copied = sum(int(stats.get(k, 0) or 0) for k in ("users", "admins", "hosts", "inbounds", "nodes", "groups"))
        users_copied = int(stats.get("users", 0) or 0)
        inbounds_copied = int(stats.get("inbounds", 0) or 0)
        if users_copied > 0 and inbounds_copied <= 0:
            raise RuntimeError(
                f"Convert copied users={users_copied} but inbounds=0. "
                "Marzban proxies→inbounds upgrade did not land before convert. Aborting."
            )
        if copied > 0:
            return
        src_users = 0
        try:
            if sqlite_path.exists():
                conn = sqlite3.connect(str(sqlite_path))
                try:
                    tables = {
                        r[0]
                        for r in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ).fetchall()
                    }
                    if "users" in tables:
                        src_users = int(conn.execute('SELECT COUNT(*) FROM "users"').fetchone()[0] or 0)
                finally:
                    conn.close()
        except Exception:
            src_users = -1
        if src_users > 0:
            raise RuntimeError(
                f"Convert produced empty target but SQLite source still has users={src_users}. "
                "Aborting so the panel is not left empty."
            )

    def _abort_if_inbounds_missing_from_stats(self, stats: dict | None) -> None:
        """Abort two-phase success when users landed without inbounds."""
        stats = stats or {}
        users = int(stats.get("users", 0) or 0)
        inbounds = int(stats.get("inbounds", 0) or 0)
        if users > 0 and inbounds <= 0:
            raise RuntimeError(
                f"Migration copied users={users} but inbounds=0. "
                "Panel-boot proxies→inbounds transform likely skipped. Aborting."
            )

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
        # finalize rewrites from install snapshot — never lose the absolute XRAY_JSON pin.
        pin_xray_json_env(log=self.job.log)
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
        # Snapshot rewrite must not drop a previously pinned absolute XRAY_JSON path.
        # Otherwise panel alembic seeds the install-default core while Marzban's
        # xray_config.json already sits under /var/lib/pasarguard/.
        pin_xray_json_env(log=self.job.log)
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
        critical = (
            "users", "admins", "hosts", "inbounds", "nodes", "groups", "core_configs",
        )
        found: dict[str, int] = {}
        tables: set[str] = set()
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

        self._assert_pasarguard_shape_ready(found, tables_present=tables, engine="sqlite")
        # File on disk is source of truth — force DB core/inbounds to match even if
        # panel boot seeded the install-default XRAY_JSON.
        self._sync_and_assert_core_from_xray("sqlite", sqlite_path=path)

    async def _assert_target_pasarguard_ready(self, target_db: str) -> None:
        """Post-boot readiness for live target engines (sqlite/mysql/pg/ts)."""
        if target_db == "sqlite":
            self._assert_sqlite_pasarguard_ready(PASARGUARD_DATA / "db.sqlite3")
            return
        if target_db in ("mysql", "mariadb"):
            found, tables = self._count_mysql_pasarguard_tables(target_db)
            self._assert_pasarguard_shape_ready(found, tables_present=tables, engine=target_db)
            self._sync_and_assert_core_from_xray(target_db)
            return
        if target_db in ("postgresql", "timescaledb"):
            found, tables = self._count_postgres_pasarguard_tables(target_db)
            self._assert_pasarguard_shape_ready(found, tables_present=tables, engine=target_db)
            self._sync_and_assert_core_from_xray(target_db)
            return

    def _prepare_xray_for_panel_boot(self) -> None:
        """Pin XRAY_JSON and refuse boot without a real Marzban xray_config.json."""
        pin_xray_json_env(log=self.job.log)
        require_marzban_xray_on_disk(log=self.job.log)

    def _sync_and_assert_core_from_xray(
        self,
        target_db: str,
        *,
        sqlite_path: Path | None = None,
    ) -> None:
        """Replace default core/inbounds with on-disk xray and hard-fail on mismatch."""
        pin_xray_json_env(log=self.job.log)
        require_marzban_xray_on_disk(log=self.job.log)
        if target_db == "sqlite":
            path = sqlite_path or (PASARGUARD_DATA / "db.sqlite3")
            sync_core_from_xray_sqlite(path, log=self.job.log)
            assert_sqlite_core_matches_xray(path)
            return
        from app.services.db_credentials import migration_port

        conn = dict(get_target_connection(self.params) or {})
        conn["host"] = conn.get("host") or "127.0.0.1"
        conn["port"] = migration_port(conn, target_db)
        sync_core_from_xray_server_db(target_db, conn, log=self.job.log)
        assert_server_core_matches_xray(target_db, conn)

    def _count_postgres_pasarguard_tables(
        self, target_db: str,
    ) -> tuple[dict[str, int], set[str]]:
        """Count critical PasarGuard tables on live PostgreSQL/Timescale."""
        import psycopg2

        from app.services.db_credentials import migration_port

        conn = get_target_connection(self.params)
        host = conn.get("host") or "127.0.0.1"
        port = int(migration_port(conn, target_db))
        user = conn.get("user") or "pasarguard"
        password = conn.get("password") or ""
        database = conn.get("database") or "pasarguard"
        critical = (
            "users", "admins", "hosts", "inbounds", "nodes", "groups", "core_configs",
        )
        found: dict[str, int] = {}
        tables: set[str] = set()
        try:
            with psycopg2.connect(
                host=host,
                port=port,
                user=user,
                password=password,
                dbname=database,
                connect_timeout=10,
            ) as db:
                db.autocommit = True
                with db.cursor() as cur:
                    cur.execute(
                        "SELECT tablename FROM pg_catalog.pg_tables "
                        "WHERE schemaname='public'"
                    )
                    tables = {str(r[0]).lower() for r in cur.fetchall() if r and r[0]}
                    for t in critical:
                        if t not in tables:
                            continue
                        cur.execute(f'SELECT COUNT(*) FROM "{t}"')
                        row = cur.fetchone()
                        n = int(row[0] or 0) if row else 0
                        if n > 0:
                            found[t] = n
        except Exception as e:
            raise RuntimeError(
                f"Could not verify upgraded {target_db} PasarGuard tables: {e}"
            ) from e
        return found, tables

    def _count_mysql_pasarguard_tables(
        self, target_db: str,
    ) -> tuple[dict[str, int], set[str]]:
        """Count critical PasarGuard tables on the live MySQL/MariaDB target."""
        import pymysql

        from app.services.db_credentials import migration_port

        conn = get_target_connection(self.params)
        host = conn.get("host") or "127.0.0.1"
        port = int(migration_port(conn, target_db))
        user = conn.get("user") or "root"
        password = conn.get("password") or ""
        database = conn.get("database") or "pasarguard"
        critical = (
            "users", "admins", "hosts", "inbounds", "nodes", "groups", "core_configs",
        )
        found: dict[str, int] = {}
        tables: set[str] = set()
        try:
            with pymysql.connect(
                host=host,
                port=port,
                user=user,
                password=password,
                database=database,
                charset="utf8mb4",
                connect_timeout=10,
                read_timeout=30,
            ) as db:
                with db.cursor() as cur:
                    cur.execute(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema=%s",
                        (database,),
                    )
                    tables = {str(r[0]).lower() for r in cur.fetchall() if r and r[0]}
                    for t in critical:
                        if t not in tables:
                            continue
                        cur.execute(f"SELECT COUNT(*) FROM `{t}`")
                        row = cur.fetchone()
                        n = int(row[0] or 0) if row else 0
                        if n > 0:
                            found[t] = n
        except Exception as e:
            raise RuntimeError(
                f"Could not verify upgraded {target_db} PasarGuard tables: {e}"
            ) from e
        return found, tables

    def _assert_pasarguard_shape_ready(
        self,
        found: dict[str, int],
        *,
        tables_present: set[str],
        engine: str,
    ) -> None:
        """Refuse success when users/hosts landed without inbounds/core_configs."""
        tables_l = {t.lower() for t in tables_present}
        users = int(found.get("users", 0) or 0)
        hosts = int(found.get("hosts", 0) or 0)
        inbounds = int(found.get("inbounds", 0) or 0)
        core_configs = int(found.get("core_configs", 0) or 0)

        if users <= 0 and hosts <= 0 and inbounds <= 0:
            raise RuntimeError(
                "Marzban → PasarGuard schema upgrade left critical tables empty "
                f"(users/hosts/inbounds on {engine}). Aborting."
            )
        if (users > 0 or hosts > 0) and inbounds <= 0:
            raise RuntimeError(
                f"Marzban → PasarGuard left inbounds empty while "
                f"users={users} hosts={hosts} ({engine}). "
                "proxies→inbounds / XRAY_JSON seeding likely failed. Aborting."
            )
        if (
            (users > 0 or hosts > 0)
            and "core_configs" in tables_l
            and core_configs <= 0
        ):
            raise RuntimeError(
                f"Marzban → PasarGuard left core_configs empty while "
                f"users={users} hosts={hosts} ({engine}). "
                "xray_config.json was missing or XRAY_JSON did not resolve. Aborting."
            )
        self.job.log(
            f"PasarGuard {engine} ready: "
            + (", ".join(f"{k}={v}" for k, v in found.items()) or "(empty)")
        )

    async def _apply_live_marzban_env(self, target_db: str) -> None:
        """Merge live Marzban .env keys (incl. XRAY_JSON) into PasarGuard .env."""
        if not PASARGUARD_ENV.exists():
            return
        env_file = MARZBAN_DIR / ".env"
        if not env_file.exists():
            self.job.log("Live Marzban .env not found — skipping env merge")
            return
        marzban_env = env_file.read_text(encoding="utf-8", errors="ignore")
        pg_env = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
        pwd = (
            get_source_connection(self.params).get("password")
            or read_env_var(marzban_env, "MYSQL_ROOT_PASSWORD")
        )
        merged = merge_marzban_env_into_pasarguard(pg_env, marzban_env, target_db, pwd)
        self._backup_file(PASARGUARD_ENV, BACKUP_DIR)
        PASARGUARD_ENV.write_text(merged, encoding="utf-8")
        self.job.log("Merged live Marzban .env settings into PasarGuard .env")

    def _pin_xray_json_env(self) -> None:
        """Point panel alembic at the copied xray_config (not relative ./xray_config.json)."""
        pin_xray_json_env(log=self.job.log)

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
        await asyncio.sleep(5)
        # Soft readiness nudge — never skips data; only restarts a stuck DB once.
        try:
            await self._run_cmd(
                ["docker", "compose", "ps", svc],
                cwd=str(PASARGUARD_DIR),
                timeout=30,
            )
        except Exception:
            pass

    async def _copy_marzban_assets(self, source_data: Path):
        """Copy certs, templates, xray_config from Marzban data dir.

        Asset layout glitches auto-heal (merge copy / template rename). Missing
        optional assets warn; xray_config pin still runs so inbounds seeding can
        proceed when the file is present.
        """
        PASARGUARD_DATA.mkdir(parents=True, exist_ok=True)
        for item in ("certs", "templates"):
            src = source_data / item
            if not src.exists():
                for p in source_data.rglob(item):
                    if p.is_dir():
                        src = p
                        break
            dst = PASARGUARD_DATA / item
            if not src.exists():
                continue
            try:
                how = safe_replace_tree(src, dst, log=self.job.log)
                if how in ("replaced", "merged"):
                    self.job.log(f"Copied {item}/ → /var/lib/pasarguard/{item}/ ({how})")
            except Exception as exc:
                self.job.log(
                    f"Warning: could not copy {item}/ ({exc}) — continuing with "
                    "whatever assets are already on disk"
                )
        templates = PASARGUARD_DATA / "templates"
        try:
            normalize_templates_layout(templates, log=self.job.log)
        except Exception as exc:
            self.job.log(f"Warning: templates layout heal skipped — {exc}")
        for p in source_data.rglob("xray_config.json"):
            try:
                text = transform_xray_config(p.read_text(encoding="utf-8", errors="ignore"))
                dst = PASARGUARD_DATA / "xray_config.json"
                dst.write_text(text, encoding="utf-8")
                self.job.log("Copied xray_config.json → /var/lib/pasarguard/")
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to copy Marzban xray_config.json — cannot seed core/inbounds: {exc}"
                ) from exc
            break
        else:
            if not xray_config_path().is_file():
                self.job.log(
                    "Warning: no xray_config.json found in Marzban assets "
                    "(will hard-fail before panel boot if still missing)"
                )
        self._pin_xray_json_env()
        self._maybe_relocate_inbound_certs()

    async def _safe_start_with_heal(self, *, health_max_wait: int | None = None) -> None:
        """Start PasarGuard; on transient/auth failure heal once and retry.

        Completeness asserts still run after a successful start. A second
        failure propagates unchanged so operators see the real error.
        """
        try:
            await safe_start_pasarguard(self, health_max_wait=health_max_wait)
            return
        except Exception as first:
            if not is_transient_infra_error(first):
                # Still allow one retry for generic start failures — many are infra.
                self.job.log(
                    f"Panel start failed — attempting one auto-heal retry "
                    f"({type(first).__name__}: {first})"
                )
            else:
                self.job.log(
                    f"Panel start hit transient error — auto-heal retry once: {first}"
                )

        target_db = (self.params or {}).get("target_db") or "sqlite"
        try:
            await self._stop_panel()
        except Exception as stop_exc:
            self.job.log(f"Auto-heal: panel stop note — {stop_exc}")

        if target_db in ("mysql", "mariadb", "postgresql", "timescaledb"):
            try:
                await self._ensure_target_database_stack(target_db)
            except Exception as db_exc:
                self.job.log(f"Auto-heal: DB stack note — {db_exc}")
            await self._try_sync_db_auth(target_db)

        await asyncio.sleep(3)
        await safe_start_pasarguard(self, health_max_wait=health_max_wait)

    async def _try_sync_db_auth(self, target_db: str) -> None:
        """Best-effort role/password sync so a retry can complete the transfer."""
        try:
            from app.services.db_auth import ensure_target_auth_ready
            from app.services.db_credentials import get_target_connection
            from app.services.pasarguard_ops import fetch_pasarguard_logs
            from app.services.pg_restore import is_auth_failure_text

            logs = ""
            try:
                logs = await fetch_pasarguard_logs(self, tail=120)
            except Exception:
                logs = ""
            if logs and not is_auth_failure_text(logs):
                self.job.log("Auto-heal: aligning DB credentials before panel retry")
            else:
                self.job.log("Auto-heal: DB auth failure detected — syncing roles")

            conn = get_target_connection(self.params) or {}
            password = conn.get("password") or ""
            if not password:
                return
            await ensure_target_auth_ready(
                self,
                target_db,
                password=password,
                sync_roles=True,
                refresh_pgbouncer=True,
            )
        except Exception as exc:
            self.job.log(f"Auto-heal: credential sync skipped — {exc}")

    def _maybe_relocate_inbound_certs(self) -> None:
        """Best-effort inbound TLS relocate — never abort migration.

        Panel certs/xray_config are already copied by ``_copy_marzban_assets``.
        Inbound relocate is an optional layout optimization; any failure is
        logged and migration continues with the copied panel assets.
        """
        if not self.params.get("relocate_inbound_certs"):
            return
        xray = PASARGUARD_DATA / "xray_config.json"
        try:
            summary = relocate_inbound_certs_in_xray_config(
                xray,
                certs_root=PASARGUARD_DATA / "certs",
                log=self.job.log,
            )
        except Exception as e:
            self.job.log(
                f"Warning: inbound TLS cert relocate skipped — {e}. "
                "Panel certificates already copied; migration continues."
            )
            return
        missing = summary.get("missing") or []
        errors = int(summary.get("errors") or 0)
        if missing and not summary.get("copied") and not summary.get("rewritten"):
            self.job.log(
                f"Warning: relocate found {len(missing)} cert pair(s) but no files on disk"
            )
        if errors:
            self.job.log(
                f"Warning: relocate skipped {errors} inbound cert pair(s); "
                "panel certs kept, migration continues."
            )

    async def _maybe_disable_nodes(self, target_db: str) -> None:
        """Optional: leave all nodes disabled after a successful Marzban migrate."""
        if not self.params.get("disable_nodes_after_migrate"):
            return
        try:
            from app.services.db_credentials import get_target_connection
            from app.services.pg_restore import _disable_nodes_after_restore

            conn = get_target_connection(self.params) or {}
            await _disable_nodes_after_restore(
                self.job,
                target_db,
                conn.get("password") or "",
                conn.get("user") or "pasarguard",
                conn.get("database") or "pasarguard",
            )
            self._nodes_disabled = True
        except Exception as e:
            self.job.log(f"Warning: could not disable nodes after migrate — {e}")
            self._nodes_disabled = False

    async def _dump_marzban_mysql(self, work_dir: Path) -> Path:
        conn = get_source_connection(self.params)
        pwd = conn.get("password") or ""
        dump_path = work_dir / "marzban.sql"
        last_err = ""
        for attempt in (1, 2):
            if MARZBAN_DIR.exists():
                proc = await asyncio.create_subprocess_shell(
                    f'cd "{MARZBAN_DIR}" && docker compose exec -T mysql '
                    f'mysqldump -u root -p"{pwd}" -h 127.0.0.1 --databases marzban > "{dump_path}"',
                )
                await proc.wait()
                if proc.returncode not in (0, None) and not dump_path.exists():
                    last_err = f"mysqldump exit={proc.returncode}"
            if dump_path.exists() and dump_path.stat().st_size > 0:
                break
            if attempt == 1:
                self.job.log(
                    "Auto-heal: Marzban MySQL dump missing/empty — retrying dump once..."
                )
                await asyncio.sleep(2)
                continue
            raise RuntimeError(
                "Failed to dump Marzban MySQL — check password and docker"
                + (f" ({last_err})" if last_err else "")
            )
        changed = rewrite_mysql_dump_file_for_pasarguard(dump_path, dump_path)
        size_mb = dump_path.stat().st_size / (1024 * 1024)
        self.job.log(
            f"Prepared Marzban MySQL dump ({size_mb:.1f} MB, {changed} lines rewritten)"
        )
        return dump_path

    async def _resolve_pasarguard_mysql_service(self) -> tuple[str, str]:
        """Return ``(compose_service, canonical_db_type)`` for the install target.

        Prefer the engine the user selected (mariadb vs mysql) so we look up the
        matching compose service and client binary order first.
        """
        target_db = normalize_target_db(self.params.get("target_db"))
        if target_db not in ("mysql", "mariadb"):
            # Import path is MySQL-family only; detect from live compose.
            target_db = "mariadb" if resolve_db_service("mariadb") else "mysql"
        if target_db == "mariadb":
            svc = resolve_db_service("mariadb") or resolve_db_service("mysql") or "mariadb"
        else:
            svc = resolve_db_service("mysql") or resolve_db_service("mariadb") or "mysql"
        return svc, target_db

    async def _pick_mysql_client_bin(
        self,
        svc: str,
        user: str,
        pwd: str,
        host: str,
        db_type: str,
    ) -> str:
        """Pick a working ``mysql``/``mariadb`` client inside the DB container.

        Official MariaDB images often ship only ``mariadb`` (no ``mysql`` symlink).
        Hardcoding ``mysql`` yields exit 127 / executable file not found.
        """
        last = ""
        for bin_name in mysql_client_bins(db_type, svc):
            proc = await asyncio.create_subprocess_exec(
                "docker", "compose", "exec", "-T",
                "-e", f"MYSQL_PWD={pwd}",
                svc, bin_name, "-u", user, "-h", host, "-e", "SELECT 1",
                cwd=str(PASARGUARD_DIR),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out_b, _ = await proc.communicate()
            last = (out_b or b"").decode("utf-8", errors="replace")
            if proc.returncode == 0:
                self.job.log(f"Using SQL client `{bin_name}` inside `{svc}`")
                return bin_name
            low = last.lower()
            if (
                "executable file not found" in low
                or "no such file" in low
                or "not found in $path" in low
            ):
                self.job.log(
                    f"`{bin_name}` not available in `{svc}` image — trying next client"
                )
                continue
            # Auth/ready glitches: still try the alternate client before giving up
            self.job.log(
                f"`{bin_name}` probe failed on `{svc}` "
                f"(exit {proc.returncode}) — trying next client"
            )
        raise RuntimeError(
            f"No working mysql/mariadb client inside `{svc}`.\n{(last or '')[-400:]}"
        )

    async def _wait_compose_mysql_ready(
        self,
        svc: str,
        user: str,
        pwd: str,
        host: str,
        *,
        db_type: str = "",
        attempts: int = 90,
    ) -> None:
        """Wait until compose MySQL/MariaDB accepts queries (not just container start)."""
        self.job.log(f"Waiting for {svc} to accept connections...")
        last = ""
        engine = db_type or normalize_target_db(self.params.get("target_db")) or "mysql"
        clients = tuple(mysql_client_bins(engine, svc))
        admins = tuple(mysql_admin_bins(engine, svc))
        for attempt in range(max(1, attempts)):
            ping_ok = False
            for admin in admins:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "compose", "exec", "-T",
                    "-e", f"MYSQL_PWD={pwd}",
                    svc, admin, "ping", "-h", host, f"-u{user}", "--silent",
                    cwd=str(PASARGUARD_DIR),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                out_b, _ = await proc.communicate()
                last = (out_b or b"").decode("utf-8", errors="replace")
                if proc.returncode == 0:
                    ping_ok = True
                    break
            if ping_ok:
                for client in clients:
                    proc = await asyncio.create_subprocess_exec(
                        "docker", "compose", "exec", "-T",
                        "-e", f"MYSQL_PWD={pwd}",
                        svc, client, "-u", user, "-h", host, "-e", "SELECT 1;",
                        cwd=str(PASARGUARD_DIR),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    out_b, _ = await proc.communicate()
                    last = (out_b or b"").decode("utf-8", errors="replace")
                    if proc.returncode == 0:
                        self.job.log(f"{svc} is ready (client={client})")
                        return
            if attempt == 0 or (attempt + 1) % 5 == 0:
                self.job.log(f"Still waiting for {svc}... ({attempt + 1}/{attempts})")
            await asyncio.sleep(2)

        # Auto-heal once: recreate the DB container then wait again (short).
        self.job.log(
            f"Auto-heal: {svc} not ready — force-recreating and waiting again..."
        )
        await self._run_cmd(
            ["docker", "compose", "up", "-d", "--force-recreate", svc],
            cwd=str(PASARGUARD_DIR),
            timeout=180,
        )
        await asyncio.sleep(5)
        retry_attempts = max(15, attempts // 3)
        for attempt in range(retry_attempts):
            for client in clients:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "compose", "exec", "-T",
                    "-e", f"MYSQL_PWD={pwd}",
                    svc, client, "-u", user, "-h", host, "-e", "SELECT 1;",
                    cwd=str(PASARGUARD_DIR),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                out_b, _ = await proc.communicate()
                last = (out_b or b"").decode("utf-8", errors="replace")
                if proc.returncode == 0:
                    self.job.log(
                        f"{svc} is ready after auto-heal recreate (client={client})"
                    )
                    return
            await asyncio.sleep(2)
        raise RuntimeError(
            f"{svc} did not become ready in time. Last output:\n{(last or '')[-400:]}"
        )

    async def _import_mysql_dump(self, dump_file: Path, *, _heal_attempted: bool = False):
        conn = get_target_connection(self.params)
        user = conn.get("user") or "root"
        pwd = conn.get("password") or ""
        db = conn.get("database") or "pasarguard"
        host = conn.get("host") or "127.0.0.1"
        dump_file = Path(dump_file)
        if not dump_file.exists():
            raise RuntimeError(f"Marzban MySQL dump not found: {dump_file}")

        size_mb = dump_file.stat().st_size / (1024 * 1024)
        fixed = dump_file.parent / "fixed_import.sql"
        self.job.log(f"Rewriting MySQL dump for PasarGuard ({size_mb:.1f} MB, streaming)...")
        changed = rewrite_mysql_dump_file_for_pasarguard(dump_file, fixed)
        self.job.log(f"Dump rewrite complete ({changed} lines changed)")

        from app.services.mysql_import_diagnostics import (
            assess_mysql_import_ram,
            classify_mysql_import_failure,
            compose_service_diagnostics,
            compose_service_oom_killed,
            compose_service_running,
            format_mysql_import_error,
            write_mysql_import_stdin_file,
        )

        # Advisory only — never aborts. Small dumps on healthy hosts stay quiet/ok.
        ram_advice = assess_mysql_import_ram(fixed.stat().st_size)
        if ram_advice.level == "warn":
            self.job.log(f"WARNING: {ram_advice.message}")
        else:
            self.job.log(ram_advice.message)

        svc, engine = await self._resolve_pasarguard_mysql_service()
        await self._run_cmd(["docker", "compose", "up", "-d", svc], cwd=str(PASARGUARD_DIR))
        await self._wait_compose_mysql_ready(svc, user, pwd, host, db_type=engine)
        client = await self._pick_mysql_client_bin(svc, user, pwd, host, engine)

        from app.services.native_migration.sql_staging import mysql_create_db_sql

        sql = mysql_create_db_sql(db, drop_first=True)
        self.job.log(f"Recreating target database `{db}` via `{client}`...")
        wipe = await asyncio.create_subprocess_exec(
            "docker", "compose", "exec", "-T",
            "-e", f"MYSQL_PWD={pwd}",
            svc, client, "-u", user, "-h", host, "-e", sql,
            cwd=str(PASARGUARD_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        wipe_out_b, _ = await wipe.communicate()
        if wipe.returncode != 0:
            wipe_out = (wipe_out_b or b"").decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Failed to recreate target database `{db}` "
                f"(exit {wipe.returncode}): {wipe_out[-400:]}"
            )

        self.job.set_progress(
            45,
            f"Importing Marzban dump into PasarGuard {engine} ({size_mb:.0f} MB)...",
        )
        self.job.log(
            f"Importing MySQL dump into `{db}` via docker compose exec stdin "
            f"({size_mb:.1f} MB, client={client} — large dumps can take a long time)..."
        )

        # SESSION preamble on the same connection as the dump (FK/unique checks off).
        # If preparing the combined file fails, fall back to the plain rewritten dump
        # so a disk glitch cannot block an otherwise healthy migration.
        import_path = fixed
        session_file = dump_file.parent / "fixed_import_session.sql"
        try:
            write_mysql_import_stdin_file(fixed, session_file)
            import_path = session_file
            self.job.log(
                "SESSION import preamble applied "
                "(FOREIGN_KEY_CHECKS=0, UNIQUE_CHECKS=0; connection-local only)"
            )
        except OSError as exc:
            self.job.log(
                f"SESSION import preamble skipped — using plain rewritten dump ({exc})"
            )

        # Prefer exec+stdin over shell redirect so host paths outside mounts work
        # and passwords/special chars are not re-parsed by a shell.
        container_died = False
        with import_path.open("rb") as fh:
            proc = await asyncio.create_subprocess_exec(
                "docker", "compose", "exec", "-T",
                "-e", f"MYSQL_PWD={pwd}",
                svc, client, "-u", user, "-h", host, db,
                cwd=str(PASARGUARD_DIR),
                stdin=fh,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            output_lines: list[str] = []

            async def _drain_stdout() -> None:
                assert proc.stdout is not None
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").rstrip()
                    if text:
                        output_lines.append(text)

            drain_task = asyncio.create_task(_drain_stdout())
            started = time.monotonic()
            # No short timeout: 400MB+ imports are legitimate and must finish.
            while True:
                try:
                    await asyncio.wait_for(asyncio.shield(drain_task), timeout=20)
                    break
                except (TimeoutError, asyncio.TimeoutError):
                    elapsed = int(time.monotonic() - started)
                    # Keep UI moving between 45% and 65% while import runs.
                    pct = min(65, 45 + (elapsed // 30))
                    self.job.set_progress(
                        pct,
                        f"Importing Marzban dump into PasarGuard {engine} "
                        f"({size_mb:.0f} MB, {elapsed}s)...",
                    )
                    self.job.log(f"Still importing MySQL dump... ({elapsed}s elapsed)")
                    # Best-effort liveness: only abort when the DB service is
                    # definitively down. Probe failures return None and are ignored
                    # so flaky docker CLI cannot break healthy imports.
                    running = await compose_service_running(str(PASARGUARD_DIR), svc)
                    if running is False:
                        container_died = True
                        self.job.log(
                            f"DB service `{svc}` is no longer running during import — "
                            f"stopping wait and collecting diagnostics"
                        )
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        break
            await proc.wait()
            if not drain_task.done():
                await drain_task
            elapsed = int(time.monotonic() - started)

        if proc.returncode != 0 or container_died:
            # If we did not catch death mid-loop, still detect a dead service now.
            if not container_died:
                running = await compose_service_running(str(PASARGUARD_DIR), svc)
                if running is False:
                    container_died = True
            oom = await compose_service_oom_killed(str(PASARGUARD_DIR), svc)
            failure = classify_mysql_import_failure(
                proc.returncode,
                "\n".join(output_lines),
                container_died=container_died,
                oom_killed=oom,
            )
            diag = await compose_service_diagnostics(str(PASARGUARD_DIR), svc)
            err = RuntimeError(
                format_mysql_import_error(
                    failure,
                    output_tail="\n".join(output_lines[-40:]),
                    diag_tail=diag,
                )
            )
            # One auto-heal retry after DB death / transient import failure.
            # Import always recreates the target DB first, so retry is safe.
            if (
                not _heal_attempted
                and (container_died or oom or is_transient_infra_error(err))
            ):
                self.job.log(
                    "Auto-heal: MySQL import failed — recreating DB service and "
                    "retrying import once so the transfer can complete..."
                )
                await self._run_cmd(
                    ["docker", "compose", "up", "-d", "--force-recreate", svc],
                    cwd=str(PASARGUARD_DIR),
                    timeout=180,
                )
                await self._wait_compose_mysql_ready(
                    svc, user, pwd, host, db_type=engine,
                )
                return await self._import_mysql_dump(dump_file, _heal_attempted=True)
            raise err
        self.job.log(f"MySQL dump import finished ({elapsed}s, client={client})")
        for path in (fixed, session_file):
            try:
                if path.exists() and path.resolve() != dump_file.resolve():
                    path.unlink()
            except OSError:
                pass

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
        from app.services.pg_access import resolve_dashboard_path

        dash = resolve_dashboard_path(env_text) if env_text else (access.get("dashboard_path") or "/dashboard/")
        root = (read_env_var(env_text, "UVICORN_ROOT_PATH") or "").rstrip("/") if env_text else ""
        out = {
            "panel_url": access.get("login_url") or get_panel_url_from_env(env_text),
            "panel_port": port or access.get("port") or "8000",
            "panel_root_path": root or access.get("root_path") or "/",
            "panel_dashboard_path": dash,
            "dashboard_path": dash,
            "login_url": access.get("login_url"),
            "root_path": access.get("root_path"),
            "subscription_mode": "native",
            "method": method,
            "target_db": target_db,
            "nodes_disabled": bool(getattr(self, "_nodes_disabled", False)),
        }
        if self.copy_report:
            out["copy_report"] = self.copy_report
            skips = (self.copy_report or {}).get("row_skips") or {}
            if skips:
                out["skip_report"] = skips
        return out

    def _get_panel_url(self) -> str:
        env_text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore") if PASARGUARD_ENV.exists() else None
        return get_panel_url_from_env(env_text)
