"""Shared helpers for PasarGuard/db-migrations tool."""

from pathlib import Path

from app.config import BACKUP_DIR, TOOLS_DIR
from app.services.db_credentials import build_migration_url, get_target_connection


def map_db_type(db_type: str) -> str:
    if db_type in ("postgresql", "timescaledb"):
        return "postgres"
    return db_type


def build_target_url(params: dict) -> str:
    """Build target URL from user-provided credentials."""
    return build_migration_url(params)


def write_migration_config(
    job_id: str,
    source_path: str,
    source_db: str,
    target_db: str,
    target_url: str,
) -> Path:
    source_type = map_db_type(source_db)
    target_type = map_db_type(target_db)
    is_file = Path(source_path).exists() and "://" not in source_path
    source_block = f'  path: "{source_path}"' if is_file else f'  url: "{source_path}"'

    config_path = BACKUP_DIR / f"migrate-{job_id}.yml"
    config_path.write_text(
        f"""source:
  type: "{source_type}"
{source_block}

target:
  type: "{target_type}"
  url: "{target_url}"

exclude_tables:
  - admin_usage_logs
  - user_usage_logs
  - node_stats
""",
        encoding="utf-8",
    )
    return config_path


async def run_db_migration(migrator, source_path: str, source_db: str, target_db: str) -> None:
    """Run official db-migrations non-interactively."""
    db_migrations = TOOLS_DIR / "db-migrations"
    if not db_migrations.exists():
        raise RuntimeError("db-migrations tool not found — re-run install.sh")

    if not Path(source_path).exists():
        raise RuntimeError(f"Source database not found: {source_path}")

    params = migrator.params
    target_url = build_target_url(params)
    conn = get_target_connection(params)
    migrator.job.log(
        f"Target connection: user={conn.get('user')}, database={conn.get('database')}, "
        f"host={conn.get('host')}, port={conn.get('port') or 'default'}"
    )
    config_path = write_migration_config(migrator.job.job_id, source_path, source_db, target_db, target_url)
    target_type = map_db_type(target_db)

    migrator.job.log(f"DB migration: {source_db} -> {target_db}")
    migrator.job.log(f"Source: {source_path}")

    if source_db == "sqlite":
        from app.services.pasarguard_ops import read_sqlite_alembic_version
        src_ver = read_sqlite_alembic_version(source_path)
        if src_ver:
            migrator.job.log(f"Source Alembic version: {src_ver}")

    shell_cmd = (
        f'cd "{db_migrations}" && '
        f'export DEBIAN_FRONTEND=noninteractive CI=1 && '
        f'yes | ./migrate.sh "{source_path}" --to {target_type} --db "{target_url}"'
    )
    ok, out = await migrator._run_cmd(["bash", "-c", shell_cmd], timeout=1800)

    if not ok:
        shell_cmd2 = (
            f'cd "{db_migrations}" && '
            f'export DEBIAN_FRONTEND=noninteractive CI=1 && '
            f'yes | uv run migrations/universal.py --config "{config_path}"'
        )
        ok, out = await migrator._run_cmd(["bash", "-c", shell_cmd2], timeout=1800)

    if not ok:
        raise RuntimeError(f"Database migration failed:\n{out[-4000:]}")
