"""Keep PasarGuard core_configs / inbounds aligned with on-disk xray_config.json.

Marzban migrations copy ``xray_config.json`` to ``/var/lib/pasarguard/`` and pin
``XRAY_JSON``, then panel boot seeds ``core_configs``. Several paths could leave
the *file* correct while the *database* still held the install default core
(inbounds UI stayed on the default inbound; hosts still appeared).

This module:
  - pins ``XRAY_JSON`` to the absolute data-dir path
  - forces ``core_configs.config`` from the on-disk JSON
  - ensures every tagged inbound in that JSON exists in ``inbounds``
  - hard-fails when the DB core/inbounds still do not match the file
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

from app.config import PASARGUARD_DATA, PASARGUARD_ENV
from app.services.env_migration import _set_env_var_simple
from app.services.marzban_inbound_certs import load_xray_config

LogFn = Callable[[str], None]

PINNED_XRAY_PATH = "/var/lib/pasarguard/xray_config.json"
_MIN_XRAY_BYTES = 64


def xray_config_path() -> Path:
    return PASARGUARD_DATA / "xray_config.json"


def pin_xray_json_in_env_text(env_text: str) -> str:
    """Force XRAY_JSON to the absolute PasarGuard data-dir path."""
    return _set_env_var_simple(env_text or "", "XRAY_JSON", PINNED_XRAY_PATH)


def pin_xray_json_env(*, log: LogFn | None = None) -> bool:
    """Write XRAY_JSON pin into live PasarGuard .env when the file exists."""
    xray = xray_config_path()
    if not xray.is_file() or not PASARGUARD_ENV.is_file():
        return False
    text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
    new_text = pin_xray_json_in_env_text(text)
    if new_text != text:
        PASARGUARD_ENV.write_text(new_text, encoding="utf-8")
        if log:
            log(f"Pinned XRAY_JSON → {PINNED_XRAY_PATH}")
    return True


def require_marzban_xray_on_disk(*, log: LogFn | None = None) -> Path:
    """Hard-require a usable Marzban/PasarGuard xray_config.json before seed/boot."""
    path = xray_config_path()
    if not path.is_file():
        raise RuntimeError(
            "Marzban xray_config.json is missing under /var/lib/pasarguard/. "
            "Core/inbounds cannot be seeded — include xray_config.json in the "
            "backup or ensure /var/lib/marzban/xray_config.json exists for live migrate."
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise RuntimeError(f"Cannot read xray_config.json: {exc}") from exc
    if size < _MIN_XRAY_BYTES:
        raise RuntimeError(
            f"xray_config.json is empty/too small ({size} bytes) — refusing to seed default core"
        )
    raw = path.read_text(encoding="utf-8", errors="ignore")
    try:
        data = load_xray_config(raw)
    except Exception as exc:
        raise RuntimeError(f"xray_config.json is not valid JSON: {exc}") from exc
    tags = inbound_tags_from_config(data)
    if not tags:
        raise RuntimeError(
            "xray_config.json has no tagged inbounds — refusing empty/default core seed"
        )
    if log:
        log(f"Xray ready for core seed: {len(tags)} inbound tag(s), {size} bytes")
    return path


def inbound_tags_from_config(data: dict[str, Any] | None) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for ib in (data or {}).get("inbounds") or []:
        if not isinstance(ib, dict):
            continue
        tag = str(ib.get("tag") or "").strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
    return tags


def inbound_tag_protocol_pairs(data: dict[str, Any] | None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for ib in (data or {}).get("inbounds") or []:
        if not isinstance(ib, dict):
            continue
        tag = str(ib.get("tag") or "").strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        proto = str(ib.get("protocol") or "").strip() or "vless"
        out.append((tag, proto))
    return out


def _load_xray(path: Path) -> tuple[str, dict[str, Any], list[tuple[str, str]]]:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    data = load_xray_config(raw)
    pairs = inbound_tag_protocol_pairs(data)
    if not pairs:
        raise RuntimeError("xray_config.json has no tagged inbounds")
    # Store canonical JSON (comments stripped) so DB matches parseable panel config.
    canonical = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return canonical, data, pairs


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return {str(r[1]) for r in rows}


def sync_core_from_xray_sqlite(
    sqlite_path: Path | str,
    xray_path: Path | str | None = None,
    *,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Force core_configs.config + inbounds tags from on-disk xray into SQLite."""
    db_path = Path(sqlite_path)
    xray = Path(xray_path) if xray_path else xray_config_path()
    if not db_path.is_file():
        raise RuntimeError(f"SQLite missing for core sync: {db_path}")
    canonical, _data, pairs = _load_xray(xray)
    file_tags = [t for t, _ in pairs]

    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "core_configs" not in tables:
            raise RuntimeError("core_configs table missing — panel upgrade did not finish")

        core_cols = _sqlite_columns(conn, "core_configs")
        if "config" not in core_cols:
            raise RuntimeError("core_configs.config column missing")

        rows = list(conn.execute("SELECT id FROM core_configs").fetchall())
        updated = 0
        inserted_cores = 0
        if rows:
            for (row_id,) in rows:
                conn.execute(
                    "UPDATE core_configs SET config=? WHERE id=?",
                    (canonical, row_id),
                )
                updated += 1
        else:
            if "name" in core_cols:
                conn.execute(
                    "INSERT INTO core_configs (name, config) VALUES (?, ?)",
                    ("Default", canonical),
                )
            else:
                conn.execute("INSERT INTO core_configs (config) VALUES (?)", (canonical,))
            inserted_cores = 1

        inbound_inserted = 0
        inbound_updated = 0
        if "inbounds" in tables:
            ib_cols = _sqlite_columns(conn, "inbounds")
            if "tag" in ib_cols:
                existing = {
                    str(r[0])
                    for r in conn.execute(
                        "SELECT tag FROM inbounds WHERE tag IS NOT NULL"
                    ).fetchall()
                    if r and r[0]
                }
                for tag, proto in pairs:
                    if tag in existing:
                        if "protocol" in ib_cols:
                            conn.execute(
                                "UPDATE inbounds SET protocol=? WHERE tag=?",
                                (proto, tag),
                            )
                            inbound_updated += 1
                        continue
                    cols = ["tag"]
                    vals: list[Any] = [tag]
                    if "protocol" in ib_cols:
                        cols.append("protocol")
                        vals.append(proto)
                    if "is_disabled" in ib_cols:
                        cols.append("is_disabled")
                        vals.append(0)
                    placeholders = ",".join("?" for _ in cols)
                    col_sql = ",".join(cols)
                    conn.execute(
                        f"INSERT INTO inbounds ({col_sql}) VALUES ({placeholders})",
                        vals,
                    )
                    inbound_inserted += 1
                    existing.add(tag)

        conn.commit()
        stats = {
            "cores_updated": updated,
            "cores_inserted": inserted_cores,
            "inbounds_inserted": inbound_inserted,
            "inbounds_updated": inbound_updated,
            "file_tags": file_tags,
        }
        if log:
            log(
                "Synced core/inbounds from xray_config.json — "
                f"cores_updated={updated}, cores_inserted={inserted_cores}, "
                f"inbounds_inserted={inbound_inserted}, tags={len(file_tags)}"
            )
        return stats
    finally:
        conn.close()


def assert_sqlite_core_matches_xray(
    sqlite_path: Path | str,
    xray_path: Path | str | None = None,
) -> None:
    """Hard-fail unless DB core JSON and inbounds tags cover the on-disk xray tags."""
    db_path = Path(sqlite_path)
    xray = Path(xray_path) if xray_path else xray_config_path()
    _canonical, data, pairs = _load_xray(xray)
    file_tags = {t for t, _ in pairs}
    if not file_tags:
        raise RuntimeError("xray_config.json has no tagged inbounds")

    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "core_configs" not in tables:
            raise RuntimeError("core_configs missing after Marzban migrate")
        core_cols = _sqlite_columns(conn, "core_configs")
        if "config" not in core_cols:
            raise RuntimeError("core_configs.config missing after Marzban migrate")
        rows = list(conn.execute("SELECT config FROM core_configs").fetchall())
        if not rows:
            raise RuntimeError("core_configs empty after Marzban migrate — default core was not replaced")

        core_tags: set[str] = set()
        for (raw,) in rows:
            cfg: dict[str, Any] = {}
            if isinstance(raw, dict):
                cfg = raw
            elif isinstance(raw, (bytes, bytearray)):
                try:
                    cfg = load_xray_config(raw.decode("utf-8", errors="ignore"))
                except Exception:
                    cfg = {}
            elif isinstance(raw, str) and raw.strip():
                try:
                    cfg = load_xray_config(raw)
                except Exception:
                    try:
                        parsed = json.loads(raw)
                        cfg = parsed if isinstance(parsed, dict) else {}
                    except Exception:
                        cfg = {}
            if isinstance(cfg, dict):
                core_tags.update(inbound_tags_from_config(cfg))

        missing_core = file_tags - core_tags
        if missing_core:
            raise RuntimeError(
                "PasarGuard core_configs still missing inbound tags from xray_config.json: "
                + ", ".join(sorted(missing_core)[:12])
                + ". File was on disk but core was not replaced."
            )

        if "inbounds" in tables and "tag" in _sqlite_columns(conn, "inbounds"):
            db_tags = {
                str(r[0])
                for r in conn.execute(
                    "SELECT tag FROM inbounds WHERE tag IS NOT NULL"
                ).fetchall()
                if r and r[0]
            }
            missing_ib = file_tags - db_tags
            if missing_ib:
                raise RuntimeError(
                    "PasarGuard inbounds table missing tags from xray_config.json: "
                    + ", ".join(sorted(missing_ib)[:12])
                    + ". Hosts may appear while the inbound list stays on the install default."
                )
    finally:
        conn.close()


def _mysql_sync(conn_info: dict, canonical: str, pairs: list[tuple[str, str]], log: LogFn | None) -> dict:
    import pymysql

    host = conn_info.get("host") or "127.0.0.1"
    port = int(conn_info.get("port") or 3306)
    user = conn_info.get("user") or "root"
    password = conn_info.get("password") or ""
    database = conn_info.get("database") or "pasarguard"
    stats = {"cores_updated": 0, "cores_inserted": 0, "inbounds_inserted": 0, "inbounds_updated": 0}

    with pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        autocommit=True,
    ) as db:
        with db.cursor() as cur:
            cur.execute("SHOW TABLES")
            tables = {str(r[0]).lower() for r in cur.fetchall() if r and r[0]}
            if "core_configs" not in tables:
                raise RuntimeError("core_configs missing on MySQL/MariaDB target")
            cur.execute("SHOW COLUMNS FROM `core_configs`")
            core_cols = {str(r[0]).lower() for r in cur.fetchall() if r and r[0]}
            if "config" not in core_cols:
                raise RuntimeError("core_configs.config missing on MySQL/MariaDB target")
            cur.execute("SELECT id FROM core_configs")
            ids = [r[0] for r in cur.fetchall()]
            if ids:
                for row_id in ids:
                    cur.execute("UPDATE core_configs SET config=%s WHERE id=%s", (canonical, row_id))
                    stats["cores_updated"] += 1
            else:
                if "name" in core_cols:
                    cur.execute(
                        "INSERT INTO core_configs (name, config) VALUES (%s, %s)",
                        ("Default", canonical),
                    )
                else:
                    cur.execute("INSERT INTO core_configs (config) VALUES (%s)", (canonical,))
                stats["cores_inserted"] = 1

            if "inbounds" in tables:
                cur.execute("SHOW COLUMNS FROM `inbounds`")
                ib_cols = {str(r[0]).lower() for r in cur.fetchall() if r and r[0]}
                if "tag" in ib_cols:
                    cur.execute("SELECT tag FROM inbounds WHERE tag IS NOT NULL")
                    existing = {str(r[0]) for r in cur.fetchall() if r and r[0]}
                    for tag, proto in pairs:
                        if tag in existing:
                            if "protocol" in ib_cols:
                                cur.execute(
                                    "UPDATE inbounds SET protocol=%s WHERE tag=%s",
                                    (proto, tag),
                                )
                                stats["inbounds_updated"] += 1
                            continue
                        cols = ["tag"]
                        vals: list[Any] = [tag]
                        if "protocol" in ib_cols:
                            cols.append("protocol")
                            vals.append(proto)
                        if "is_disabled" in ib_cols:
                            cols.append("is_disabled")
                            vals.append(0)
                        col_sql = ",".join(f"`{c}`" for c in cols)
                        ph = ",".join(["%s"] * len(cols))
                        cur.execute(f"INSERT INTO inbounds ({col_sql}) VALUES ({ph})", vals)
                        stats["inbounds_inserted"] += 1
                        existing.add(tag)
    if log:
        log(
            "Synced MySQL/MariaDB core/inbounds from xray_config.json — "
            f"cores_updated={stats['cores_updated']}, "
            f"inbounds_inserted={stats['inbounds_inserted']}"
        )
    return stats


def _pg_sync(conn_info: dict, canonical: str, pairs: list[tuple[str, str]], log: LogFn | None) -> dict:
    import psycopg2

    host = conn_info.get("host") or "127.0.0.1"
    port = int(conn_info.get("port") or 5432)
    user = conn_info.get("user") or "postgres"
    password = conn_info.get("password") or ""
    database = conn_info.get("database") or "pasarguard"
    stats = {"cores_updated": 0, "cores_inserted": 0, "inbounds_inserted": 0, "inbounds_updated": 0}

    with psycopg2.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        dbname=database,
        connect_timeout=15,
    ) as db:
        db.autocommit = True
        with db.cursor() as cur:
            cur.execute(
                "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public'"
            )
            tables = {str(r[0]).lower() for r in cur.fetchall() if r and r[0]}
            if "core_configs" not in tables:
                raise RuntimeError("core_configs missing on PostgreSQL/Timescale target")
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='core_configs'"
            )
            core_cols = {str(r[0]).lower() for r in cur.fetchall() if r and r[0]}
            if "config" not in core_cols:
                raise RuntimeError("core_configs.config missing on PostgreSQL/Timescale target")
            cur.execute("SELECT id FROM core_configs")
            ids = [r[0] for r in cur.fetchall()]
            if ids:
                for row_id in ids:
                    cur.execute(
                        "UPDATE core_configs SET config=%s WHERE id=%s",
                        (canonical, row_id),
                    )
                    stats["cores_updated"] += 1
            else:
                if "name" in core_cols:
                    cur.execute(
                        "INSERT INTO core_configs (name, config) VALUES (%s, %s)",
                        ("Default", canonical),
                    )
                else:
                    cur.execute("INSERT INTO core_configs (config) VALUES (%s)", (canonical,))
                stats["cores_inserted"] = 1

            if "inbounds" in tables:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='inbounds'"
                )
                ib_cols = {str(r[0]).lower() for r in cur.fetchall() if r and r[0]}
                if "tag" in ib_cols:
                    cur.execute("SELECT tag FROM inbounds WHERE tag IS NOT NULL")
                    existing = {str(r[0]) for r in cur.fetchall() if r and r[0]}
                    for tag, proto in pairs:
                        if tag in existing:
                            if "protocol" in ib_cols:
                                cur.execute(
                                    "UPDATE inbounds SET protocol=%s WHERE tag=%s",
                                    (proto, tag),
                                )
                                stats["inbounds_updated"] += 1
                            continue
                        cols = ["tag"]
                        vals: list[Any] = [tag]
                        if "protocol" in ib_cols:
                            cols.append("protocol")
                            vals.append(proto)
                        if "is_disabled" in ib_cols:
                            cols.append("is_disabled")
                            vals.append(False)
                        col_sql = ",".join(cols)
                        ph = ",".join(["%s"] * len(cols))
                        cur.execute(
                            f"INSERT INTO inbounds ({col_sql}) VALUES ({ph})",
                            vals,
                        )
                        stats["inbounds_inserted"] += 1
                        existing.add(tag)
    if log:
        log(
            "Synced PostgreSQL/Timescale core/inbounds from xray_config.json — "
            f"cores_updated={stats['cores_updated']}, "
            f"inbounds_inserted={stats['inbounds_inserted']}"
        )
    return stats


def sync_core_from_xray_server_db(
    db_type: str,
    conn_info: dict,
    xray_path: Path | str | None = None,
    *,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Force core/inbounds from on-disk xray into live MySQL/MariaDB/PG/Timescale."""
    xray = Path(xray_path) if xray_path else xray_config_path()
    canonical, _data, pairs = _load_xray(xray)
    engine = (db_type or "").strip().lower()
    if engine in ("mysql", "mariadb"):
        stats = _mysql_sync(conn_info, canonical, pairs, log)
    elif engine in ("postgresql", "timescaledb"):
        stats = _pg_sync(conn_info, canonical, pairs, log)
    else:
        raise RuntimeError(f"Unsupported engine for core sync: {db_type}")
    stats["file_tags"] = [t for t, _ in pairs]
    return stats


def assert_server_core_matches_xray(db_type: str, conn_info: dict, xray_path: Path | str | None = None) -> None:
    """Hard-fail unless live server DB core/inbounds cover on-disk xray tags."""
    xray = Path(xray_path) if xray_path else xray_config_path()
    _canonical, _data, pairs = _load_xray(xray)
    file_tags = {t for t, _ in pairs}
    engine = (db_type or "").strip().lower()

    if engine in ("mysql", "mariadb"):
        import pymysql

        with pymysql.connect(
            host=conn_info.get("host") or "127.0.0.1",
            port=int(conn_info.get("port") or 3306),
            user=conn_info.get("user") or "root",
            password=conn_info.get("password") or "",
            database=conn_info.get("database") or "pasarguard",
            charset="utf8mb4",
            autocommit=True,
        ) as db:
            with db.cursor() as cur:
                cur.execute("SELECT config FROM core_configs")
                rows = cur.fetchall()
                if not rows:
                    raise RuntimeError("core_configs empty on MySQL/MariaDB — default core not replaced")
                core_tags: set[str] = set()
                for (raw,) in rows:
                    try:
                        cfg = json.loads(raw or "{}") if isinstance(raw, str) else {}
                    except Exception:
                        cfg = {}
                    if isinstance(cfg, dict):
                        core_tags.update(inbound_tags_from_config(cfg))
                missing = file_tags - core_tags
                if missing:
                    raise RuntimeError(
                        "MySQL/MariaDB core_configs missing xray inbound tags: "
                        + ", ".join(sorted(missing)[:12])
                    )
                cur.execute("SHOW TABLES LIKE 'inbounds'")
                if cur.fetchone():
                    cur.execute("SELECT tag FROM inbounds WHERE tag IS NOT NULL")
                    db_tags = {str(r[0]) for r in cur.fetchall() if r and r[0]}
                    missing_ib = file_tags - db_tags
                    if missing_ib:
                        raise RuntimeError(
                            "MySQL/MariaDB inbounds missing xray tags: "
                            + ", ".join(sorted(missing_ib)[:12])
                        )
        return

    if engine in ("postgresql", "timescaledb"):
        import psycopg2

        with psycopg2.connect(
            host=conn_info.get("host") or "127.0.0.1",
            port=int(conn_info.get("port") or 5432),
            user=conn_info.get("user") or "postgres",
            password=conn_info.get("password") or "",
            dbname=conn_info.get("database") or "pasarguard",
            connect_timeout=15,
        ) as db:
            db.autocommit = True
            with db.cursor() as cur:
                cur.execute("SELECT config FROM core_configs")
                rows = cur.fetchall()
                if not rows:
                    raise RuntimeError("core_configs empty on PostgreSQL — default core not replaced")
                core_tags = set()
                for (raw,) in rows:
                    try:
                        if isinstance(raw, dict):
                            cfg = raw
                        else:
                            cfg = json.loads(raw or "{}")
                    except Exception:
                        cfg = {}
                    if isinstance(cfg, dict):
                        core_tags.update(inbound_tags_from_config(cfg))
                missing = file_tags - core_tags
                if missing:
                    raise RuntimeError(
                        "PostgreSQL core_configs missing xray inbound tags: "
                        + ", ".join(sorted(missing)[:12])
                    )
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name='inbounds'"
                )
                if cur.fetchone():
                    cur.execute("SELECT tag FROM inbounds WHERE tag IS NOT NULL")
                    db_tags = {str(r[0]) for r in cur.fetchall() if r and r[0]}
                    missing_ib = file_tags - db_tags
                    if missing_ib:
                        raise RuntimeError(
                            "PostgreSQL inbounds missing xray tags: "
                            + ", ".join(sorted(missing_ib)[:12])
                        )
        return

    raise RuntimeError(f"Unsupported engine for core assert: {db_type}")
