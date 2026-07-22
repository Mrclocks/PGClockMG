"""Universal any-to-any database data copy."""

from __future__ import annotations

from pathlib import Path

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
    fail_hard: bool = True,
    stamp_alembic: bool = True,
) -> dict[str, int]:
    """Copy data from any supported source engine to any supported target engine."""
    source_conn = staging_conn or get_source_connection(migrator.params)
    target_conn = get_target_connection(migrator.params)

    reader_path = source_path if source_db == "sqlite" else None
    if source_db == "sqlite" and not reader_path:
        reader_path = str(PASARGUARD_DATA / "db.sqlite3")

    target_path = None
    if target_db == "sqlite":
        target_path = str(PASARGUARD_DATA / "db.sqlite3")

    reader = create_reader(source_db, reader_path, source_conn)
    writer = create_writer(target_db, target_conn, target_path)
    log = migrator.job.log

    try:
        migrator.job.log(f"Universal copy: {source_db} → {target_db}")
        # Diagnostics: show exactly where we read from and write to, so a
        # "success but empty panel" can be traced to a wrong DB/host/user.
        try:
            if source_db == "sqlite":
                migrator.job.log(f"  source: sqlite file {reader_path}")
            else:
                migrator.job.log(
                    "  source: {db} {u}@{h}:{p}/{n}".format(
                        db=source_db,
                        u=source_conn.get("user"),
                        h=source_conn.get("host"),
                        p=source_conn.get("port"),
                        n=source_conn.get("database"),
                    )
                )
            if target_db == "sqlite":
                migrator.job.log(f"  target: sqlite file {target_path}")
            else:
                migrator.job.log(
                    "  target: {db} {u}@{h}:{p}/{n}".format(
                        db=target_db,
                        u=target_conn.get("user"),
                        h=target_conn.get("host"),
                        p=target_conn.get("port"),
                        n=target_conn.get("database"),
                    )
                )
        except Exception:
            pass
        stats, report = copy_tables_universal(
            reader, writer, log, source_version, fail_hard=fail_hard,
            stamp_alembic=stamp_alembic,
        )
        migrator.copy_report = report
        total = sum(v for v in stats.values() if isinstance(v, int) and v >= 0)
        migrator.job.log(
            f"Copy complete: {total} rows across {len(stats)} tables "
            f"(source: {Path(source_path).name})"
        )
        # Highlight the entities the operator actually cares about.
        key_tables = [
            "admins", "users", "hosts", "inbounds", "nodes",
            "core_configs", "groups",
        ]
        summary = ", ".join(
            f"{name}={stats[name]}" for name in key_tables if name in stats
        )
        if summary:
            migrator.job.log(f"Key entities copied: {summary}")
        if report.get("has_gaps"):
            migrator.job.log(
                "WARNING: copy report has gaps — "
                + ", ".join(
                    f"{i['table']} {i['copied']}/{i['source']}"
                    for i in report.get("incomplete", [])[:8]
                )
            )
        return stats
    finally:
        reader.close()
        writer.close()
