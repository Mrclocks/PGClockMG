"""Universal any-to-any database data copy."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from app.config import PASARGUARD_DATA
from app.services.db_credentials import get_source_connection, get_target_connection
from app.services.native_migration.adapters import (
    copy_tables_universal,
    create_reader,
    create_writer,
)


async def copy_database_universal(
    migrator,
    source_path: str,
    source_db: str,
    target_db: str,
    source_version: str,
    staging_conn: dict | None = None,
) -> dict[str, int]:
    """Copy data from any supported source engine to any supported target engine."""
    source_conn = staging_conn or get_source_connection(migrator.params)
    target_conn = get_target_connection(migrator.params)

    reader_path = source_path if source_db == "sqlite" else None
    target_path = None
    if target_db == "sqlite":
        target_path = str(PASARGUARD_DATA / "db.sqlite3")

    reader = create_reader(source_db, reader_path, source_conn)
    writer = create_writer(target_db, target_conn, target_path)
    log = migrator.job.log

    try:
        migrator.job.log(f"Universal copy: {source_db} → {target_db}")
        stats = copy_tables_universal(reader, writer, log, source_version)
        total = sum(stats.values())
        migrator.job.log(
            f"Copy complete: {total} rows across {len(stats)} tables "
            f"(source file: {Path(source_path).name})"
        )
        if total == 0:
            migrator.job.log(
                "Warning: zero rows copied — verify source backup has user/admin data"
            )
        return stats
    finally:
        reader.close()
        writer.close()
