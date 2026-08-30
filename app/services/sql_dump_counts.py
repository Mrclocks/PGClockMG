"""Count rows in pg_dump / mysqldump text for PasarGuard panel tables.

Used by backup verification and restore analysis. Scans the full file in a
streaming fashion so large Timescale dumps (DDL-heavy prefix) are not
mis-read as empty when ``users`` data appears late in the file.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

# Panel tables we surface in backup manifests / empty-dump checks
STAT_TABLES = ("users", "nodes", "admins", "inbounds", "hosts", "groups")

# Broader set for restore expected-count estimates
RESTORE_COUNT_TABLES = STAT_TABLES + (
    "settings",
    "users_groups_association",
    "inbounds_groups_association",
    "core_configs",
)

_TABLE_ALT = (
    "users|nodes|admins|inbounds|hosts|groups|settings|"
    "users_groups_association|inbounds_groups_association|core_configs"
)

_COPY_HEAD = re.compile(
    rf"""(?ix)
    ^COPY\s+(?:ONLY\s+)?
    (?:(?P<schema>[A-Za-z_][\w]*)\.)?
    (?P<q1>["`]?)(?P<table>{_TABLE_ALT})(?P=q1)
    \s*(?:\([^;]*\))?\s+
    FROM\s+stdin\s*;
    \s*$
    """
)

_INSERT_HEAD = re.compile(
    rf"""(?ix)
    ^INSERT\s+INTO\s+
    (?:(?P<schema>[A-Za-z_][\w]*)\.)?
    (?P<q1>["`\[]?)(?P<table>{_TABLE_ALT})(?P=q1)
    \s*
    """
)

_CREATE_TABLE = re.compile(
    rf"""(?ix)
    CREATE\s+(?:UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?
    (?:(?P<schema>[A-Za-z_][\w]*)\.)?
    (?P<q1>["`\[]?)(?P<table>{_TABLE_ALT})(?P=q1)
    \s*[\(\s]
    """
)


def _count_insert_value_tuples(chunk: str) -> int:
    """Count top-level VALUE tuples in an INSERT … VALUES (…)[,…]; body."""
    upper = chunk.upper()
    idx = upper.find("VALUES")
    body = chunk[idx + 6 :] if idx >= 0 else chunk
    n = 0
    depth = 0
    for ch in body:
        if ch == "(":
            if depth == 0:
                n += 1
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
    return n


def estimate_sql_dump_counts_from_text(
    sql_text: str,
    tables: Iterable[str] = STAT_TABLES,
) -> dict[str, int | None]:
    """In-memory variant (tests / small blobs). Same semantics as file scan."""
    table_set = tuple(tables)
    if not sql_text:
        return {t: None for t in table_set}
    return _scan_sql_lines(sql_text.splitlines(keepends=True), table_set)


def scan_sql_dump_file(
    path: Path,
    tables: Iterable[str] = STAT_TABLES,
) -> dict[str, object]:
    """Stream-scan a dump file.

    ``counts[t] == 0`` means we saw a COPY/INSERT data section with zero rows
    (confirmed empty). ``counts[t] is None`` means we never entered that table's
    data section (unknown — do not treat as empty).
    """
    table_set = tuple(tables)
    with path.open("r", encoding="utf-8", errors="ignore", newline="") as fh:
        return _scan_sql_lines_meta(fh, table_set)


def _scan_sql_lines(lines, tables: tuple[str, ...]) -> dict[str, int | None]:
    meta = _scan_sql_lines_meta(lines, tables)
    return meta["counts"]  # type: ignore[return-value]


def _scan_sql_lines_meta(lines, tables: tuple[str, ...]) -> dict[str, object]:
    want = set(tables)
    counts: dict[str, int] = {}
    ddl_seen: dict[str, bool] = {t: False for t in tables}
    data_seen: dict[str, bool] = {t: False for t in tables}

    mode: str | None = None
    cur_table: str | None = None
    insert_buf: list[str] = []

    def _finish_insert() -> None:
        nonlocal mode, cur_table, insert_buf
        if mode != "insert" or not cur_table:
            insert_buf = []
            mode = None
            cur_table = None
            return
        blob = "".join(insert_buf)
        n = _count_insert_value_tuples(blob)
        counts[cur_table] = counts.get(cur_table, 0) + n
        insert_buf = []
        mode = None
        cur_table = None

    for raw in lines:
        line = raw if isinstance(raw, str) else str(raw)

        if mode == "copy" and cur_table:
            stripped = line.strip()
            if stripped == "\\." or stripped.startswith("\\."):
                mode = None
                cur_table = None
                continue
            if stripped:
                counts[cur_table] = counts.get(cur_table, 0) + 1
            continue

        if mode == "insert" and cur_table:
            insert_buf.append(line)
            if ";" in line:
                _finish_insert()
            continue

        cm = _CREATE_TABLE.search(line)
        if cm:
            t = (cm.group("table") or "").lower()
            if t in ddl_seen:
                ddl_seen[t] = True

        m = _COPY_HEAD.match(line.lstrip("\ufeff"))
        if m:
            t = (m.group("table") or "").lower()
            if t in want:
                data_seen[t] = True
                counts.setdefault(t, 0)
                mode = "copy"
                cur_table = t
            continue

        m = _INSERT_HEAD.match(line.lstrip("\ufeff").lstrip())
        if m:
            t = (m.group("table") or "").lower()
            if t in want:
                data_seen[t] = True
                counts.setdefault(t, 0)
                mode = "insert"
                cur_table = t
                insert_buf = [line]
                if ";" in line:
                    _finish_insert()
            continue

    if mode == "insert":
        _finish_insert()

    out_counts: dict[str, int | None] = {}
    for t in tables:
        if data_seen[t]:
            out_counts[t] = int(counts.get(t, 0))
        else:
            out_counts[t] = None

    return {
        "counts": out_counts,
        "ddl_seen": ddl_seen,
        "data_section_seen": data_seen,
    }


def assert_dump_compatible_with_live_users(
    *,
    db_type: str,
    dump_path: Path,
    live_users: int | None,
    dump_meta: dict | None = None,
    sqlite_user_count: int | None = None,
) -> str | None:
    """Return an error message if the dump is confirmed empty vs live panel.

    Returns None when it is safe to proceed (including unknown formats —
    never treat missing-from-sample as empty).
    """
    if not isinstance(live_users, int) or live_users <= 0:
        return None
    if not dump_path.is_file():
        return f"Backup dump missing but panel reports {live_users} users"

    if db_type == "sqlite":
        n = sqlite_user_count
        if n is None:
            return None
        if n == 0:
            return (
                f"Backup dump has 0 users but panel reports {live_users} — refusing empty dump"
            )
        return None

    meta = dump_meta or scan_sql_dump_file(dump_path)
    counts = meta.get("counts") or {}
    ddl_seen = meta.get("ddl_seen") or {}
    data_seen = meta.get("data_section_seen") or {}
    dump_users = counts.get("users")

    if dump_users == 0:
        return (
            f"Backup dump has 0 users but panel reports {live_users} — refusing empty dump"
        )

    if dump_users is None and ddl_seen.get("users") and not data_seen.get("users"):
        size = dump_path.stat().st_size
        return (
            f"Backup dump defines users table but contains no users data "
            f"(size={size} bytes) while panel reports {live_users} — refusing empty dump"
        )

    return None
