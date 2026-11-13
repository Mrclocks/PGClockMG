"""Detect usernames that current PasarGuard panels reject (Marzban allowed them).

PasarGuard ``UserValidator.validate_username``: 3-128 chars, ``[a-zA-Z0-9-_@.]``
only, and no two consecutive special characters (``-_@.``). Marzban allowed
e.g. ``ali__vip``. Such rows migrate fine but fail panel-side validation, so we
only *report* them (renaming would break subscription links).
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

_ALLOWED = re.compile(r"^[a-zA-Z0-9\-_@.]+$")
_CONSECUTIVE = re.compile(r"[-_@.]{2,}")


def username_problem(name: str | None) -> str | None:
    """Return a short reason the panel would reject ``name``, else None."""
    n = name or ""
    if not (3 <= len(n) <= 128):
        return "length must be 3-128"
    if not _ALLOWED.match(n):
        return "illegal characters"
    if _CONSECUTIVE.search(n):
        return "consecutive special characters"
    return None


def find_invalid_usernames(names) -> list[tuple[str, str]]:
    out = []
    for n in names:
        reason = username_problem(n)
        if reason:
            out.append((n, reason))
    return out


def fetch_usernames(db_type: str, conn: dict, sqlite_path: Path | None = None) -> list[str]:
    if db_type == "sqlite":
        c = sqlite3.connect(str(sqlite_path))
        try:
            return [r[0] for r in c.execute("SELECT username FROM users")]
        finally:
            c.close()
    if db_type in ("mysql", "mariadb"):
        import pymysql

        with pymysql.connect(
            host=conn.get("host") or "127.0.0.1", port=int(conn.get("port") or 3306),
            user=conn.get("user") or "root", password=conn.get("password") or "",
            database=conn.get("database") or "pasarguard", charset="utf8mb4",
            connect_timeout=10,
        ) as db, db.cursor() as cur:
            cur.execute("SELECT username FROM users")
            return [r[0] for r in cur.fetchall()]
    import psycopg2

    with psycopg2.connect(
        host=conn.get("host") or "127.0.0.1", port=int(conn.get("port") or 5432),
        user=conn.get("user") or "pasarguard", password=conn.get("password") or "",
        dbname=conn.get("database") or "pasarguard", connect_timeout=10,
    ) as db, db.cursor() as cur:
        cur.execute("SELECT username FROM users")
        return [r[0] for r in cur.fetchall()]
