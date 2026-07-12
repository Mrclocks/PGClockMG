"""Shared helpers for PasarGuard/db-migrations tool."""

from pathlib import Path

from app.config import PASARGUARD_DATA, BACKUP_DIR, TOOLS_DIR


def map_db_type(db_type: str) -> str:
    if db_type in ("postgresql", "timescaledb"):
        return "postgres"
    return db_type


def build_target_url(db_type: str, password: str | None) -> str:
    pwd = password or "password"
    if db_type == "sqlite":
        return f"sqlite:///{PASARGUARD_DATA}/db.sqlite3"
    if db_type in ("mysql", "mariadb"):
        return f"mysql+pymysql://root:{pwd}@127.0.0.1:3306/pasarguard"
    return f"postgresql+asyncpg://postgres:{pwd}@localhost:5432/pasarguard"


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


async def run_db_migration(migrator, source_path: str, source_db: str, target_db: str, password: str | None) -> None:
    """Run official db-migrations non-interactively."""
    db_migrations = TOOLS_DIR / "db-migrations"
    if not db_migrations.exists():
        raise RuntimeError("db-migrations tool not found — re-run install.sh")

    if not Path(source_path).exists():
        raise RuntimeError(f"Source database not found: {source_path}")

    target_url = build_target_url(target_db, password)
    config_path = write_migration_config(migrator.job.job_id, source_path, source_db, target_db, target_url)
    target_type = map_db_type(target_db)

    migrator.job.log(f"DB migration: {source_db} -> {target_db}")
    migrator.job.log(f"Source: {source_path}")

    shell_cmd = (
        f'cd "{db_migrations}" && '
        f'printf "yes\\n" | ./migrate.sh "{source_path}" --to {target_type} --db "{target_url}"'
    )
    ok, out = await migrator._run_cmd(["bash", "-c", shell_cmd], timeout=1800)

    if not ok:
        shell_cmd2 = (
            f'cd "{db_migrations}" && '
            f'printf "yes\\n" | uv run migrations/universal.py --config "{config_path}"'
        )
        ok, out = await migrator._run_cmd(["bash", "-c", shell_cmd2], timeout=1800)

    if not ok:
        raise RuntimeError(f"Database migration failed:\n{out[-4000:]}")
