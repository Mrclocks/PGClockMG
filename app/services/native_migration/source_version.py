"""Read alembic_version from migration sources."""

from __future__ import annotations

import re
from pathlib import Path

from app.services.pasarguard_ops import read_sqlite_alembic_version


def read_alembic_version_from_sql_dump(sql_text: str) -> str | None:
    patterns = (
        r"INSERT\s+INTO\s+[`\"]?alembic_version[`\"]?\s+.*?VALUES\s*\(\s*['\"]?([0-9a-f]{12,})",
        r"INSERT\s+INTO\s+[`\"]?alembic_version[`\"]?\s*\([^)]*\)\s*VALUES\s*\(\s*['\"]?([0-9a-f]{12,})",
    )
    for pattern in patterns:
        m = re.search(pattern, sql_text, re.I | re.S)
        if m:
            return m.group(1)
    return None


async def resolve_source_alembic_version(
    migrator, source_db: str, source_path: str,
) -> str | None:
    path = Path(source_path)
    if not path.exists():
        return None

    if source_db == "sqlite":
        return read_sqlite_alembic_version(path)

    if path.suffix.lower() == ".sql":
        text = path.read_text(encoding="utf-8", errors="ignore")
        return read_alembic_version_from_sql_dump(text)

    if source_db in ("postgresql", "timescaledb"):
        from app.services.pasarguard_ops import read_target_alembic_version
        return await read_target_alembic_version(migrator, source_db)

    if source_db in ("mysql", "mariadb"):
        from app.services.pasarguard_ops import read_mysql_alembic_version
        return await read_mysql_alembic_version(migrator, source_db)

    return None
