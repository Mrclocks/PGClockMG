"""Shared table copy helpers.

Policy: copy everything that exists in the source.
- Column intersection with target schema + legacy Marzban column aliases.
- Row-level skip on hard errors; abort when critical tables copy zero rows.
- Obsolete Marzban enum tokens neutralized before PostgreSQL/MySQL insert.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime
from decimal import Decimal
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

# Abort migration when any of these tables exist in source but copy zero rows
MIGRATION_ABORT_IF_ZERO = frozenset({
    "users",
    "hosts",
    "inbounds",
    "groups",
    "nodes",
})

_HOST_OBSOLETE = frozenset({"none", "None", "NONE", "null", "NULL", ""})

OBSOLETE_TO_EMPTY = {
    ("hosts", "alpn"): _HOST_OBSOLETE,
    ("hosts", "fingerprint"): _HOST_OBSOLETE,
    ("hosts", "security"): _HOST_OBSOLETE,
    ("hosts", "noise_settings"): _HOST_OBSOLETE,
    ("hosts", "fragment_settings"): _HOST_OBSOLETE,
    ("hosts", "noise"): _HOST_OBSOLETE,
    ("hosts", "fragment"): _HOST_OBSOLETE,
}

# Legacy Marzban source column -> PasarGuard target column
SOURCE_TO_TARGET_COLUMNS: dict[str, dict[str, str]] = {
    "hosts": {
        "fragment_setting": "fragment_settings",
        "noise_setting": "noise_settings",
        "mux_enable": "mux_settings",
    },
}

# Target-only NOT NULL columns missing from upgraded intermediate schema
TARGET_INSERT_DEFAULTS: dict[str, dict[str, object]] = {
    "nodes": {
        "server_ca": "",
        "api_key": "",
        "status": "healthy",
    },
    "hosts": {
        "priority": 0,
    },
    "groups": {
        "is_disabled": False,
    },
    "inbounds": {
        "is_disabled": False,
    },
}

JSON_COLUMNS = frozenset({
    "fragment_settings",
    "noise_settings",
    "mux_settings",
    "http_headers",
    "transport_settings",
    "value",
})

# PostgreSQL / MySQL engine families (same copy logic)
MYSQL_FAMILY = frozenset({"mysql", "mariadb"})
PG_FAMILY = frozenset({"postgresql", "timescaledb"})

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
})

# Marzban → PasarGuard user status (never coerce status as boolean)
USER_STATUS_ALIASES = {
    "onhold": "on_hold",
    "on-hold": "on_hold",
    "on hold": "on_hold",
}


def normalize_user_status(value):
    """Map Marzban/SQLite user status to a PasarGuard enum string."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "active" if value else "disabled"
    if isinstance(value, (int, float)):
        return "active" if value else "disabled"
    if not isinstance(value, str):
        return str(value)
    s = value.strip()
    if not s:
        return None
    low = s.lower()
    if low in USER_STATUS_ALIASES:
        return USER_STATUS_ALIASES[low]
    return low

# FK columns that may be nulled on retry when parent row missing
OPTIONAL_FK_COLUMNS: dict[str, tuple[str, ...]] = {
    "nodes": ("core_config_id",),
    "hosts": ("inbound_tag",),
}


def engine_family(db_type: str) -> str:
    """Normalize engine name for cross-DB compatibility checks."""
    if db_type in MYSQL_FAMILY:
        return "mysql"
    if db_type in PG_FAMILY:
        return "postgresql"
    return db_type


def normalize_raw_value(value):
    """Normalize DB driver return types before column-specific coercion."""
    if value is None:
        return None
    if isinstance(value, memoryview):
        value = bytes(value)
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except Exception:
            return value
    return value


def coerce_json_value(value):
    """Normalize a value for PostgreSQL/MySQL JSON columns."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return json.dumps({"enabled": value})
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            json.loads(stripped)
            return stripped
        except json.JSONDecodeError:
            return None
    return None


def coerce_mux_settings(value):
    """Map legacy mux_enable / string mux data to JSON mux_settings."""
    if value is None:
        return None
    if isinstance(value, bool):
        return json.dumps({"enabled": value})
    if isinstance(value, (int, float)):
        return json.dumps({"enabled": bool(value)})
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.startswith("{") or stripped.startswith("["):
            return coerce_json_value(stripped)
        return json.dumps({"enabled": to_bool(stripped)})
    return coerce_json_value(value)


def build_table_column_plan(
    table: str,
    src_cols: list[str],
    tgt_cols: list[str],
) -> tuple[list[str], list[str | None]]:
    """Build target insert columns and parallel source column selectors."""
    tgt_set = set(tgt_cols)
    src_set = set(src_cols)
    mappings = SOURCE_TO_TARGET_COLUMNS.get(table, {})
    defaults = TARGET_INSERT_DEFAULTS.get(table, {})

    insert_cols: list[str] = []
    select_cols: list[str | None] = []

    for sc in src_cols:
        if sc in tgt_set and sc not in insert_cols:
            insert_cols.append(sc)
            select_cols.append(sc)

    for sc, tc in mappings.items():
        if sc in src_set and tc in tgt_set and tc not in insert_cols:
            insert_cols.append(tc)
            select_cols.append(sc)

    for tc, _default in defaults.items():
        if tc in tgt_set and tc not in insert_cols:
            insert_cols.append(tc)
            select_cols.append(None)

    return insert_cols, select_cols


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
    value = normalize_raw_value(value)
    if value is None:
        return None

    if table == "users" and column == "status":
        return normalize_user_status(value)

    if table == "hosts" and column == "mux_settings":
        return coerce_mux_settings(value)

    if column in JSON_COLUMNS or (
        table == "settings" and column == "value"
    ):
        return coerce_json_value(value)

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
            if column in TARGET_INSERT_DEFAULTS.get(table, {}):
                return ""
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


def parse_not_null_column(error: str) -> str | None:
    """Extract column name from NOT NULL violation messages."""
    if not error:
        return None
    patterns = (
        r'null value in column "([^"]+)"',
        r"column '([^']+)' cannot be null",
        r"Column '([^']+)' cannot be null",
        r"field '([^']+)' doesn't have a default",
    )
    for pat in patterns:
        m = re.search(pat, error, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


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
