"""Shared table copy helpers.

Policy: copy everything that exists in the source.
- Column intersection with target schema.
- Row-level skip on hard errors; report gaps at end (never abort except zero users).
- Obsolete Marzban enum tokens neutralized before PostgreSQL insert.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Callable

TABLE_ORDER = [
    "jwt",
    "system",
    "settings",
    "admins",
    "core_configs",
    "nodes",
    "inbounds",
    "groups",
    "inbounds_groups_association",
    "hosts",
    "client_templates",
    "user_templates",
    "template_group_association",
    "users",
    "users_groups_association",
    "exclude_inbounds_association",
    "template_inbounds_association",
    "next_plans",
    "notification_reminders",
    "node_user_usages",
    "node_usages",
]

SKIP_TABLES = {
    "alembic_version",
    "admin_usage_logs",
    "user_usage_logs",
    "node_stats",
    "proxies",
    "tls",
}

# Tables that must have rows for subscription links to work (report loudly if incomplete)
SUBSCRIPTION_TABLES = frozenset({
    "users",
    "hosts",
    "inbounds",
    "groups",
    "settings",
    "users_groups_association",
    "inbounds_groups_association",
    "exclude_inbounds_association",
})

# Only abort migration when users cannot be copied at all
MIGRATION_ABORT_IF_ZERO = frozenset({"users"})

_HOST_OBSOLETE = frozenset({"none", "None", "NONE", "null", "NULL", ""})

OBSOLETE_TO_EMPTY = {
    ("hosts", "alpn"): _HOST_OBSOLETE,
    ("hosts", "fingerprint"): _HOST_OBSOLETE,
    ("hosts", "security"): _HOST_OBSOLETE,
    ("hosts", "noise"): _HOST_OBSOLETE,
    ("hosts", "fragment"): _HOST_OBSOLETE,
}

BOOL_COLUMNS = frozenset({
    "enable",
    "is_sudo",
    "is_disabled",
    "allowinsecure",
    "random_user_agent",
    "use_sni_as_host",
    "mux_enable",
    "edit",
    "enabled",
    "is_disabled",
    "status",
})

# FK columns that may be nulled on retry when parent row missing
OPTIONAL_FK_COLUMNS: dict[str, tuple[str, ...]] = {
    "nodes": ("core_config_id",),
    "hosts": ("inbound_id", "group_id"),
}


def to_bool(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "t", "yes", "on")
    return bool(value)


def sqlite_table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def convert_value(table: str, column: str, value):
    """Coerce types; neutralize obsolete Marzban tokens."""
    if value is None:
        return None

    if column in BOOL_COLUMNS:
        return to_bool(value)

    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except Exception:
            return value

    if isinstance(value, str):
        stripped = value.strip()
        if stripped == "":
            return None
        obsolete = OBSOLETE_TO_EMPTY.get((table, column))
        if obsolete and stripped in obsolete:
            return None
        if table == "hosts" and column == "alpn" and "," in stripped:
            parts = [
                p.strip() for p in stripped.split(",")
                if p.strip() and p.strip() not in (obsolete or ())
            ]
            return ",".join(parts) if parts else None
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                json.loads(stripped)
                return stripped
            except Exception:
                pass
        return stripped

    return value


def copy_sqlite_tables(
    conn_sqlite: sqlite3.Connection,
    log: Callable[[str], None],
    *,
    target_columns_fn,
    truncate_fn,
    insert_fn,
    reset_sequence_fn=None,
) -> dict[str, int]:
    """Generic SQLite → target DB copy using callbacks."""
    stats: dict[str, int] = {}
    sqlite_tables = sqlite_table_names(conn_sqlite)

    for table in TABLE_ORDER:
        if table in SKIP_TABLES or table not in sqlite_tables:
            continue

        src_cols = sqlite_columns(conn_sqlite, table)
        if not src_cols:
            continue
        tgt_cols = target_columns_fn(table)
        if not tgt_cols:
            log(f"Skip {table}: not in target schema")
            continue

        common = [c for c in src_cols if c in tgt_cols]
        if not common:
            log(f"Skip {table}: no matching columns")
            continue

        truncate_fn(table)
        rows = conn_sqlite.execute(
            f"SELECT {', '.join(common)} FROM {table}"
        ).fetchall()

        count = 0
        for row in rows:
            values = tuple(convert_value(table, col, row[col]) for col in common)
            try:
                insert_fn(table, common, values)
                count += 1
            except Exception as exc:
                log(f"Row skip {table}: {str(exc)[:120]}")
        stats[table] = count
        log(f"Imported {table}: {count} rows ({len(common)} columns)")

        if reset_sequence_fn and "id" in tgt_cols:
            try:
                reset_sequence_fn(table)
            except Exception:
                pass

    return stats
