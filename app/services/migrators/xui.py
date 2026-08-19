"""3x-ui → PasarGuard migration using official PasarGuard/migrations tool.

Always converts to SQLite first. If target_db is not sqlite, runs two-phase
engine to copy head→head into the requested engine.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import socket
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from app.config import PASARGUARD_DIR, PASARGUARD_DATA, PASARGUARD_ENV, TOOLS_DIR, BACKUP_DIR
from app.services.migrators.base import BaseMigrator
from app.services.prerequisites import find_xui_db
from app.services.pasarguard_ops import safe_start_pasarguard, normalize_target_db
from app.services.native_migration import run_cross_db_migration
from app.services.env_migration import (
    env_points_to_db,
    finalize_pasarguard_env_after_restore,
    parse_sqlalchemy_url,
    read_env_var,
)

XUI_AUTH_HEAL_ENV = "PG_XUI_AUTH_HEAL"


def xui_auth_heal_enabled() -> bool:
    """Kill switch for the x-ui PostgreSQL auth repair (roles + PgBouncer)."""
    return os.environ.get(XUI_AUTH_HEAL_ENV, "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def parse_container_env(text: str) -> dict[str, str]:
    """Parse ``docker inspect`` environment output; later values win (runtime order)."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key:
            out[key] = value
    return out


def pgbouncer_env_mismatch(
    container_env: dict[str, str],
    *,
    user: str,
    password: str,
    database: str,
) -> list[str]:
    """Credential keys where a running PgBouncer disagrees with the panel URL.

    PgBouncer builds its userlist from ``DB_USER``/``DB_PASSWORD`` when the
    container is *created*, and ``docker compose restart`` never re-reads .env.
    A stale container then rejects the panel with ``password authentication
    failed`` on port 6432 even though PostgreSQL itself accepts the password.
    Keys the image does not define are ignored (custom PgBouncer setups).
    """
    expected = {"DB_USER": user, "DB_PASSWORD": password, "DB_NAME": database}
    stale: list[str] = []
    for key, want in expected.items():
        if not want:
            continue
        have = container_env.get(key)
        if have is None or have == want:
            continue
        stale.append(key)
    return stale


def pg_probe_result(ok: bool, output: str) -> str:
    """Classify a TCP login probe as ``ok``, ``auth-failed`` or ``unusable``.

    A probe that cannot reach PostgreSQL at all (port not published, image
    without psql, docker error) must never be read as a wrong password —
    otherwise the wizard would rewrite role passwords over a broken probe.
    """
    if ok:
        return "ok"
    low = (output or "").lower()
    if "password authentication failed" in low or "no password supplied" in low:
        return "auth-failed"
    return "unusable"


# Postmaster start time is readable by any role and identical for every session
# of one server, so it tells two PostgreSQL instances apart; epoch keeps the text
# free of DateStyle/TimeZone differences between the two clients we compare.
PG_FINGERPRINT_SQL = "SELECT extract(epoch from pg_postmaster_start_time())::bigint"


def pg_auth_context_sql(role: str) -> str:
    """One line of ``password_encryption|stored verifier|host hba method``."""
    literal = (role or "").replace("'", "''")
    return (
        "SELECT current_setting('password_encryption') || '|' || "
        "coalesce((SELECT CASE WHEN rolpassword LIKE 'SCRAM-SHA-256%' THEN 'scram-sha-256' "
        "WHEN rolpassword LIKE 'md5%' THEN 'md5' ELSE 'other' END "
        f"FROM pg_authid WHERE rolname = '{literal}'), 'unknown') || '|' || "
        "coalesce((SELECT auth_method FROM pg_hba_file_rules WHERE type = 'host' "
        "AND address = 'all' ORDER BY line_number DESC LIMIT 1), 'unknown')"
    )


def parse_pg_auth_context(text: str) -> dict[str, str]:
    """Parse the auth-context line; empty dict when PostgreSQL answered nothing."""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.count("|") >= 2:
            encryption, verifier, hba = (line.split("|") + ["", "", ""])[:3]
            return {
                "encryption": encryption.strip().lower(),
                "verifier": verifier.strip().lower(),
                "hba": hba.strip().lower(),
            }
    return {}


def required_password_encryption(ctx: dict[str, str]) -> str:
    """Encoding a role password must be stored in to satisfy the host hba rule."""
    hba = (ctx.get("hba") or "").lower()
    return hba if hba in ("scram-sha-256", "md5") else ""


def password_storage_mismatch(ctx: dict[str, str]) -> bool:
    """True when the stored verifier can never satisfy the host hba rule.

    A password stored as md5 cannot answer a ``scram-sha-256`` challenge (and
    vice versa), so PostgreSQL replies ``password authentication failed`` even
    though the password is correct. Nothing inside the container reveals this:
    the image's own ``host all all 127.0.0.1/32 trust`` line accepts everything.
    """
    required = required_password_encryption(ctx)
    verifier = (ctx.get("verifier") or "").lower()
    if not required or verifier in ("", "unknown"):
        return False
    return verifier != required


def parse_published_port(text: str, container_port: str = "5432/tcp") -> tuple[str, str]:
    """Host address docker actually publishes for a container port.

    The wizard must not assume 127.0.0.1:5432 is the panel's database: on a host
    where that port belongs to someone else, alembic authenticates against a
    stranger — and the schema reset would wipe it.
    """
    try:
        ports = json.loads(text or "{}") or {}
    except (ValueError, TypeError):
        return "", ""
    fallback = ("", "")
    for binding in ports.get(container_port) or []:
        if not isinstance(binding, dict):
            continue
        host_ip = str(binding.get("HostIp") or "").strip()
        host_port = str(binding.get("HostPort") or "").strip()
        if not host_port:
            continue
        if host_ip in ("", "0.0.0.0", "::", "[::]"):
            host_ip = "127.0.0.1"
        if host_ip == "127.0.0.1":
            return host_ip, host_port
        if not fallback[1]:
            fallback = (host_ip, host_port)
    return fallback


def _first_value(text: str) -> str:
    """First non-empty line of psql -tA output."""
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def pg_endpoint_candidates(host: str, port: str) -> list[tuple[str, str]]:
    """Addresses to try, loopback first because that is what alembic connects to."""
    endpoints: list[tuple[str, str]] = []
    for candidate in (("127.0.0.1", port), (host, port), ("127.0.0.1", "5432")):
        if candidate[0] and candidate[1] and candidate not in endpoints:
            endpoints.append(candidate)
    return endpoints


def containers_publishing_port(ps_output: str, port: str) -> list[str]:
    """Container names that publish ``port`` on the host, per ``docker ps``."""
    names: list[str] = []
    needle = f":{port}->"
    for line in (ps_output or "").splitlines():
        name, _, ports = line.partition("\t")
        if needle in ports and name.strip():
            names.append(name.strip())
    return names


def app_version() -> str:
    """Wizard version, so a job log alone shows which build produced it."""
    try:
        from app.main import APP_VERSION

        return f"PGClockMG v{APP_VERSION}"
    except Exception:
        return "PGClockMG (version unknown)"


def describe_password_source(env_text: str, password: str) -> str:
    """Name the .env key a password came from (never log the secret itself)."""
    for key in ("POSTGRES_PASSWORD", "DB_PASSWORD"):
        if password and read_env_var(env_text, key) == password:
            return key
    url_pwd = parse_sqlalchemy_url(
        read_env_var(env_text, "SQLALCHEMY_DATABASE_URL") or "", env_text,
    ).get("password")
    if password and password == url_pwd:
        return "SQLALCHEMY_DATABASE_URL"
    return "docker-compose"


def logs_show_pg_auth_failure(text: str) -> bool:
    """True when panel logs died on PostgreSQL credentials (not schema/data)."""
    low = (text or "").lower()
    return any(
        marker in low
        for marker in (
            "password authentication failed",
            "sasl authentication failed",
            "invalidpassworderror",
        )
    )


def find_xui_db_in_dir(root: Path) -> Path | None:
    """Locate an x-ui SQLite database inside an extracted backup / workspace."""
    if not root or not root.is_dir():
        return None

    preferred: list[Path] = []
    fallback: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        name = p.name.lower()
        if name.endswith(".db") or name.endswith(".sqlite3"):
            if "x-ui" in name:
                preferred.append(p)
            else:
                fallback.append(p)

    if preferred:
        for p in preferred:
            if p.name.lower() == "x-ui.db":
                return p
        return preferred[0]
    if fallback:
        return fallback[0]
    return None


def resolve_xui_db_source(
    upload_path: str | None = None,
    upload_work_dir: str | None = None,
) -> Path | None:
    """Resolve x-ui.db from bundle workspace, upload file/zip, or live install.

    Bundle uploads set upload_path to a *directory* (workspace). Older code
    treated every non-.zip path as a file and crashed with IsADirectoryError.
    """
    candidates: list[Path] = []
    if upload_work_dir:
        candidates.append(Path(upload_work_dir))
    if upload_path:
        p = Path(upload_path)
        if not candidates or p.resolve() != candidates[0].resolve():
            candidates.append(p)

    for src in candidates:
        if not src.exists():
            continue
        if src.is_dir():
            found = find_xui_db_in_dir(src)
            if found:
                return found
            continue
        if src.is_file():
            return src

    return find_xui_db()


def bundled_xui_schema_db(tools_dir: Path | None = None) -> Path | None:
    """Official empty PasarGuard SQLite schema shipped with PasarGuard/migrations."""
    base = Path(tools_dir) if tools_dir else TOOLS_DIR
    path = base / "migrations" / "x-ui" / "input-db-pg" / "db.sqlite3"
    return path if path.is_file() else None


def resolve_xui_schema_db(
    tools_dir: Path | None = None,
    pasarguard_data: Path | None = None,
) -> Path:
    """Pick a PasarGuard *SQLite* schema reference for the official x-ui migrator.

    Starting a MySQL/Postgres PasarGuard install never creates db.sqlite3, so we
    must not rely on safe_start for schema creation. Prefer the bundled empty
    schema from PasarGuard/migrations; fall back to a live sqlite file if present.
    """
    bundled = bundled_xui_schema_db(tools_dir)
    if bundled:
        return bundled

    data = Path(pasarguard_data) if pasarguard_data else PASARGUARD_DATA
    live = data / "db.sqlite3"
    if live.is_file() and live.stat().st_size > 0:
        return live

    raise RuntimeError(
        "PasarGuard SQLite schema reference not found. "
        "Expected tools/migrations/x-ui/input-db-pg/db.sqlite3 "
        "(re-run installer to fetch PasarGuard/migrations) "
        "or a local /var/lib/pasarguard/db.sqlite3."
    )


def _table_count(conn: sqlite3.Connection, table: str) -> int | None:
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if not row or row[0] == 0:
            return None
        return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    except sqlite3.Error:
        return None


def inspect_xui_sqlite(path: Path) -> dict[str, int]:
    """Count key x-ui source tables (client_traffics / clients / inbounds)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out: dict[str, int] = {}
        for table in ("client_traffics", "clients", "inbounds", "users"):
            n = _table_count(conn, table)
            if n is not None:
                out[table] = n
        return out
    finally:
        conn.close()


def inspect_pasarguard_sqlite(path: Path) -> dict[str, int]:
    """Count key PasarGuard tables in a migrated sqlite file."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out: dict[str, int] = {}
        for table in ("users", "admins", "hosts", "inbounds", "nodes", "groups"):
            n = _table_count(conn, table)
            if n is not None:
                out[table] = n
        return out
    finally:
        conn.close()


def assert_xui_source_has_data(path: Path) -> dict[str, int]:
    counts = inspect_xui_sqlite(path)
    if not counts:
        raise RuntimeError(
            "فایل x-ui.db جدول‌های مورد انتظار (inbounds / client_traffics) را ندارد — "
            "آیا فایل دیتابیس 3x-ui درست آپلود شده؟"
        )
    clients = counts.get("client_traffics", counts.get("clients", 0))
    inbounds = counts.get("inbounds", 0)
    if clients <= 0 and inbounds <= 0:
        raise RuntimeError(
            "دیتابیس x-ui.db خالی است (هیچ inbound/کاربری نیست) — مهاجرت لغو شد"
        )
    return counts


def _xui_table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(r[0])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def safe_copy_sqlite(src: Path, dst: Path) -> None:
    """Copy a SQLite DB including pending WAL data (avoids stale .db-only copies)."""
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    # URI read + backup API folds -wal into the destination
    src_conn = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
            dst_conn.commit()
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def detect_xui_db_schema(path: Path) -> dict:
    """Auto-detect legacy vs modern 3x-ui SQLite layout from uploaded DB tables."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = _xui_table_names(conn)
        modern = "clients" in names and "client_inbounds" in names
        clients = 0
        memberships = 0
        traffics = 0
        if "clients" in names:
            clients = int(conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0])
        if "client_inbounds" in names:
            memberships = int(conn.execute("SELECT COUNT(*) FROM client_inbounds").fetchone()[0])
        if "client_traffics" in names:
            traffics = int(conn.execute("SELECT COUNT(*) FROM client_traffics").fetchone()[0])
        return {
            "schema": "modern" if modern else "legacy",
            "modern": modern,
            "has_clients_table": "clients" in names,
            "has_client_inbounds": "client_inbounds" in names,
            "has_hosts_table": "hosts" in names,
            "clients": clients,
            "memberships": memberships,
            "traffics": traffics,
        }
    finally:
        conn.close()


def is_modern_xui_multi_inbound_schema(path: Path) -> bool:
    """Newer 3x-ui (multi-inbound clients): ``clients`` + ``client_inbounds`` tables."""
    return bool(detect_xui_db_schema(path).get("modern"))


def _client_text(value) -> str:
    """Coerce a clients-row field to stripped text.

    Modern 3x-ui ``clients.id`` is an integer PK. Never treat ints/bools as
    UUID/password/email strings — ``(… or id).strip()`` raises AttributeError.
    Classic ``settings.clients[].id`` remains a UUID string and still works.
    """
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return ""
    return str(value).strip()


def _client_entry_for_protocol(protocol: str, client: dict, flow_override: str = "") -> dict:
    """Build a classic ``settings.clients[]`` entry from a ``clients`` row."""
    email = _client_text(client.get("email"))
    sub_id = _client_text(client.get("sub_id")) or _client_text(client.get("subId"))
    flow = _client_text(flow_override) or _client_text(client.get("flow"))
    entry: dict = {
        "email": email,
        "enable": bool(client.get("enable", 1)),
        "expiryTime": int(client.get("expiry_time") or 0),
        "totalGB": int(client.get("total_gb") or 0),
        "subId": sub_id,
        "limitIp": int(client.get("limit_ip") or 0),
        "tgId": str(client.get("tg_id") or ""),
        "comment": str(client.get("comment") or ""),
        "reset": int(client.get("reset") or 0),
        "security": str(client.get("security") or "auto"),
    }
    proto = (protocol or "").lower()
    # Prefer uuid column; fall back to string id only (classic JSON), never int PK.
    uuid = _client_text(client.get("uuid")) or _client_text(client.get("id"))
    password = _client_text(client.get("password"))
    if proto in ("vless", "vmess", "tunnel"):
        if uuid:
            entry["id"] = uuid
    if proto in ("trojan", "shadowsocks", "shadowsocks2022", "socks", "http"):
        if password:
            entry["password"] = password
    if proto == "vless":
        entry["flow"] = flow
        # Keep auth if present (some panels store it); harmless for PG converter.
        auth = _client_text(client.get("auth"))
        if auth:
            entry["auth"] = auth
    if proto == "vmess" and password and "id" not in entry:
        entry["id"] = password
    # Trojan often also carries uuid in modern panels — keep password primary.
    if proto == "trojan" and uuid and "id" not in entry:
        entry["id"] = uuid
    return entry


def normalize_modern_xui_sqlite(db_path: Path) -> dict:
    """Rewrite modern multi-inbound 3x-ui DB into classic shape for PasarGuard migrator.

    Official ``migrate.py`` only reads ``client_traffics`` (+ ``inbounds.settings.clients``).
    New 3x-ui keeps membership in ``client_inbounds`` and may leave inbound
    ``settings.clients`` empty / incomplete → broken UUID map and /sub errors.

    Important: do **not** expand ``client_traffics`` to one row per inbound.
    PasarGuard maps ``email → username`` with UNIQUE username + INSERT OR REPLACE,
    so duplicate emails leave orphan ``users_groups_association`` rows and each
    user stuck on a single inbound group. Keep one traffic row per email; full
    multi-inbound group membership is repaired after migrate via
    ``sync_user_groups_from_xui_settings``.
    """
    path = Path(db_path)
    if not is_modern_xui_multi_inbound_schema(path):
        return {"normalized": False, "reason": "legacy-schema"}

    conn = sqlite3.connect(str(path))
    try:
        conn.row_factory = sqlite3.Row
        tables = _xui_table_names(conn)
        if "client_traffics" not in tables or "inbounds" not in tables:
            return {"normalized": False, "reason": "missing-core-tables"}

        clients = {
            int(r["id"]): dict(r)
            for r in conn.execute("SELECT * FROM clients")
        }
        links = [dict(r) for r in conn.execute("SELECT * FROM client_inbounds")]
        if not clients or not links:
            return {"normalized": False, "reason": "empty-clients"}

        # Preserve traffic counters when present
        traffic_cols = [r[1] for r in conn.execute("PRAGMA table_info(client_traffics)")]
        existing_by_email: dict[str, dict] = {}
        for r in conn.execute("SELECT * FROM client_traffics"):
            d = dict(r)
            email = (d.get("email") or "").strip()
            if email and email not in existing_by_email:
                existing_by_email[email] = d

        # email -> ordered inbound ids from membership
        email_inbounds: dict[str, list[int]] = {}
        for link in links:
            client = clients.get(int(link.get("client_id") or 0))
            if not client:
                continue
            email = (client.get("email") or "").strip()
            iid = int(link.get("inbound_id") or 0)
            if not email or iid <= 0:
                continue
            bucket = email_inbounds.setdefault(email, [])
            if iid not in bucket:
                bucket.append(iid)

        # Rebuild settings.clients on every inbound from relational membership
        inbounds = list(conn.execute("SELECT id, protocol, settings FROM inbounds"))
        settings_fixed = 0
        for inbound in inbounds:
            iid = int(inbound["id"])
            protocol = str(inbound["protocol"] or "")
            try:
                settings = json.loads(inbound["settings"] or "{}")
            except json.JSONDecodeError:
                settings = {}
            if not isinstance(settings, dict):
                settings = {}
            attached: list[dict] = []
            for link in links:
                if int(link.get("inbound_id") or 0) != iid:
                    continue
                client = clients.get(int(link.get("client_id") or 0))
                if not client:
                    continue
                attached.append(
                    _client_entry_for_protocol(
                        protocol, client, str(link.get("flow_override") or ""),
                    )
                )
            if attached:
                settings["clients"] = attached
            # Panel-only keys that can break PasarGuard subscription/config build
            if "encryption" in settings and protocol.lower() == "vless":
                settings.pop("encryption", None)
            if attached or "encryption" in (inbound["settings"] or ""):
                conn.execute(
                    "UPDATE inbounds SET settings=? WHERE id=?",
                    (json.dumps(settings, ensure_ascii=False), iid),
                )
                settings_fixed += 1

        # One traffic row per email (UNIQUE email). Official migrator uses
        # traffic.id as users.id and email as username — duplicates break groups.
        has_all_time = "all_time" in traffic_cols
        has_last_online = "last_online" in traffic_cols
        conn.execute("ALTER TABLE client_traffics RENAME TO client_traffics__legacy")
        col_defs = [
            "id integer PRIMARY KEY AUTOINCREMENT",
            "inbound_id integer",
            "enable numeric",
            "email text UNIQUE",
            "up integer",
            "down integer",
            "expiry_time integer",
            "total integer",
            "reset integer DEFAULT 0",
        ]
        if has_all_time:
            col_defs.append("all_time integer DEFAULT 0")
        if has_last_online:
            col_defs.append("last_online integer DEFAULT 0")
        conn.execute(f"CREATE TABLE client_traffics ({', '.join(col_defs)})")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_client_traffics_inbound "
            "ON client_traffics(inbound_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_client_traffics_renew "
            "ON client_traffics(expiry_time, reset)"
        )
        conn.execute("DROP TABLE client_traffics__legacy")

        inserted = 0
        next_id = 1
        for client in clients.values():
            email = (client.get("email") or "").strip()
            if not email:
                continue
            inbound_ids = email_inbounds.get(email) or []
            if not inbound_ids:
                continue
            prev = existing_by_email.get(email) or {}
            # Prefer existing inbound_id if still a member; else first membership
            preferred = int(prev.get("inbound_id") or 0)
            iid = preferred if preferred in inbound_ids else inbound_ids[0]
            cols = ["id", "inbound_id", "enable", "email", "up", "down", "expiry_time", "total", "reset"]
            vals: list = [
                next_id,
                iid,
                1 if client.get("enable", 1) else 0,
                email,
                int(prev.get("up") or 0),
                int(prev.get("down") or 0),
                int(client.get("expiry_time") or prev.get("expiry_time") or 0),
                int(client.get("total_gb") or prev.get("total") or 0),
                int(client.get("reset") or prev.get("reset") or 0),
            ]
            if has_all_time:
                cols.append("all_time")
                vals.append(int(prev.get("all_time") or 0))
            if has_last_online:
                cols.append("last_online")
                vals.append(int(prev.get("last_online") or 0))
            placeholders = ",".join("?" * len(cols))
            conn.execute(
                f"INSERT INTO client_traffics ({','.join(cols)}) VALUES ({placeholders})",
                vals,
            )
            next_id += 1
            inserted += 1

        conn.commit()
        return {
            "normalized": True,
            "clients": len(clients),
            "memberships": len(links),
            "traffics_written": inserted,
            "inbounds_settings_updated": settings_fixed,
            "one_traffic_per_email": True,
        }
    finally:
        conn.close()


def sync_user_groups_from_xui_settings(pg_db: Path, xui_db: Path) -> dict:
    """Ensure each PG user is in every inbound group their x-ui email belongs to.

    Official migrator associates a user with only the single ``inbound_id`` from
    their ``client_traffics`` row. After modern-schema normalize, that under-links
    multi-inbound clients. Also removes orphan association rows left by REPLACE.
    """
    pg_path = Path(pg_db)
    xui_path = Path(xui_db)
    email_to_inbounds: dict[str, set[int]] = {}

    xui = sqlite3.connect(f"file:{xui_path.as_posix()}?mode=ro", uri=True)
    try:
        xui.row_factory = sqlite3.Row
        for row in xui.execute("SELECT id, settings FROM inbounds"):
            iid = int(row["id"])
            try:
                settings = json.loads(row["settings"] or "{}")
            except json.JSONDecodeError:
                settings = {}
            for client in (settings.get("clients") or []) if isinstance(settings, dict) else []:
                if not isinstance(client, dict):
                    continue
                email = (client.get("email") or "").strip()
                if email:
                    email_to_inbounds.setdefault(email, set()).add(iid)
    finally:
        xui.close()

    if not email_to_inbounds:
        return {"synced": False, "reason": "no-clients-in-settings"}

    conn = sqlite3.connect(str(pg_path))
    try:
        tables = {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        needed = {"users", "groups", "users_groups_association", "inbounds_groups_association"}
        if not needed.issubset(tables):
            return {"synced": False, "reason": "missing-pg-tables"}

        inbound_to_group = {
            int(r[0]): int(r[1])
            for r in conn.execute(
                "SELECT inbound_id, group_id FROM inbounds_groups_association"
            )
        }
        users = {
            str(r[0]): int(r[1])
            for r in conn.execute("SELECT username, id FROM users")
            if r[0]
        }

        # Drop associations pointing at missing users
        orphan_deleted = conn.execute(
            """
            DELETE FROM users_groups_association
            WHERE user_id NOT IN (SELECT id FROM users)
            """
        ).rowcount

        existing = {
            (int(r[0]), int(r[1]))
            for r in conn.execute(
                "SELECT user_id, groups_id FROM users_groups_association"
            )
        }
        added = 0
        for email, inbound_ids in email_to_inbounds.items():
            user_id = users.get(email)
            if not user_id:
                continue
            for iid in inbound_ids:
                group_id = inbound_to_group.get(iid)
                if not group_id:
                    continue
                key = (user_id, group_id)
                if key in existing:
                    continue
                conn.execute(
                    "INSERT INTO users_groups_association (user_id, groups_id) VALUES (?, ?)",
                    key,
                )
                existing.add(key)
                added += 1

        conn.commit()
        return {
            "synced": True,
            "emails": len(email_to_inbounds),
            "links_added": added,
            "orphans_removed": int(orphan_deleted or 0),
        }
    finally:
        conn.close()


def _guess_share_address(xui_db: Path, listen: dict | None = None) -> str:
    """Best-effort public address for seeded PasarGuard hosts."""
    listen = listen or {}
    domain = str(listen.get("domain") or "").strip()
    if domain.startswith("http://") or domain.startswith("https://"):
        domain = urlparse(domain).hostname or ""
    if domain:
        return domain
    for key in ("cert_path", "key_path", "cert", "key"):
        raw = str(listen.get(key) or "").strip()
        if not raw:
            continue
        for part in reversed(Path(raw).parts):
            if "." in part and not part.lower().endswith(
                (".pem", ".crt", ".key", ".cer")
            ):
                return part
    # Fall back to cert paths directly from DB if listen was incomplete
    try:
        again = read_xui_subscription_listen(Path(xui_db))
        for key in ("cert_path", "key_path"):
            raw = str(again.get(key) or "").strip()
            for part in reversed(Path(raw).parts):
                if "." in part and not part.lower().endswith(
                    (".pem", ".crt", ".key", ".cer")
                ):
                    return part
    except Exception:
        pass
    return detect_public_ip()


def _host_overlay_from_stream(stream: dict) -> dict:
    """Map x-ui stream_settings → PasarGuard host column overlays."""
    if not isinstance(stream, dict):
        stream = {}
    network = str(stream.get("network") or "tcp").lower()
    security = str(stream.get("security") or "none").lower()
    if security == "tls":
        host_security = "tls"
    elif security == "none":
        host_security = "none"
    else:
        # reality / other → keep inbound defaults
        host_security = "inbound_default"

    path = ""
    host_header = ""
    if network == "ws":
        ws = stream.get("wsSettings") or {}
        path = str(ws.get("path") or "")
        host_header = str((ws.get("headers") or {}).get("Host") or "")
    elif network in ("httpupgrade", "http_upgrade"):
        hu = stream.get("httpupgradeSettings") or stream.get("httpUpgradeSettings") or {}
        path = str(hu.get("path") or "")
        host_header = str(hu.get("host") or "")
    elif network == "splithttp" or network == "xhttp":
        xh = stream.get("xhttpSettings") or stream.get("splithttpSettings") or {}
        path = str(xh.get("path") or "")
        host_header = str(xh.get("host") or "")
    elif network == "grpc":
        path = str((stream.get("grpcSettings") or {}).get("serviceName") or "")
    elif network == "tcp":
        header = ((stream.get("tcpSettings") or {}).get("header") or {})
        if str(header.get("type") or "") == "http":
            req = header.get("request") or {}
            paths = req.get("path") or []
            if isinstance(paths, list) and paths:
                path = str(paths[0] or "")
            headers = req.get("headers") or {}
            hosts = headers.get("Host") or headers.get("host") or []
            if isinstance(hosts, list) and hosts:
                host_header = str(hosts[0] or "")

    sni = ""
    fingerprint = "none"
    alpn = ""
    if security == "tls":
        tls = stream.get("tlsSettings") or {}
        sni = str(tls.get("serverName") or tls.get("server_name") or "")
        fingerprint = str(tls.get("fingerprint") or "none") or "none"
        alpn_list = tls.get("alpn") or []
        if isinstance(alpn_list, list):
            # EnumArray: comma-separated ProxyHostALPN values
            mapped = []
            for item in alpn_list:
                v = str(item or "").strip()
                if v in ("h2", "h3", "http/1.1"):
                    mapped.append(v)
            alpn = ",".join(mapped)
    elif security == "reality":
        reality = stream.get("realitySettings") or {}
        # serverNames may be list
        names = reality.get("serverNames") or reality.get("server_names") or []
        if isinstance(names, list) and names:
            sni = str(names[0] or "")
        elif isinstance(names, str):
            sni = names
        fingerprint = str(reality.get("fingerprint") or "chrome") or "chrome"

    return {
        "security": host_security,
        "path": path or None,
        "host": host_header or None,
        "sni": sni or None,
        "fingerprint": fingerprint or "none",
        "alpn": alpn or None,
    }


def ensure_sudo_admin_from_xui(pg_db: Path, xui_db: Path) -> dict:
    """Create sudo admin #1 from x-ui panel user when PG admins table is empty."""
    pg_path = Path(pg_db)
    xui_path = Path(xui_db)
    conn = sqlite3.connect(str(pg_path))
    try:
        tables = _xui_table_names(conn)
        if "admins" not in tables:
            return {"created": False, "reason": "no-admins-table"}
        if int(conn.execute("SELECT COUNT(*) FROM admins").fetchone()[0]) > 0:
            return {"created": False, "reason": "already-present"}

        username = "admin"
        hashed = "$2a$10$N9qo8uLOickgx2ZMRZoMyeIjZAgcfl7p92ldGxad68LJZdL17lhWy"  # "password"
        xui = sqlite3.connect(f"file:{xui_path.as_posix()}?mode=ro", uri=True)
        try:
            xui_tables = _xui_table_names(xui)
            if "users" in xui_tables:
                row = xui.execute(
                    "SELECT username, password FROM users ORDER BY id LIMIT 1"
                ).fetchone()
                if row and row[0] and row[1]:
                    username = str(row[0])[:34]
                    hashed = str(row[1])[:128]
        finally:
            xui.close()

        cols = [r[1] for r in conn.execute("PRAGMA table_info(admins)")]
        now = "1970-01-01 00:00:00"
        fields: list[str] = ["id", "username", "hashed_password"]
        values: list = [1, username, hashed]
        if "created_at" in cols:
            fields.append("created_at")
            values.append(now)
        if "is_sudo" in cols:
            fields.append("is_sudo")
            values.append(1)
        if "status" in cols:
            fields.append("status")
            values.append("active")
        if "used_traffic" in cols:
            fields.append("used_traffic")
            values.append(0)
        if "is_disabled" in cols:
            fields.append("is_disabled")
            values.append(0)
        # Newer PasarGuard: admin_roles (1=owner). Avoid NULL role_id → /sub 500.
        if "role_id" in cols:
            role_id = 1
            if "admin_roles" in tables:
                row = conn.execute(
                    "SELECT id FROM admin_roles ORDER BY id LIMIT 1"
                ).fetchone()
                if row:
                    role_id = int(row[0])
            fields.append("role_id")
            values.append(role_id)
        placeholders = ",".join("?" * len(fields))
        conn.execute(
            f"INSERT INTO admins ({','.join(fields)}) VALUES ({placeholders})",
            values,
        )
        # Point users at this admin when admin_id is missing/orphan
        if "users" in tables:
            user_cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
            if "admin_id" in user_cols:
                conn.execute(
                    "UPDATE users SET admin_id=1 WHERE admin_id IS NULL OR admin_id NOT IN (SELECT id FROM admins)"
                )
        conn.commit()
        return {"created": True, "username": username}
    finally:
        conn.close()


def _ensure_shadowsocks_password(password: str, *, seed: str = "") -> str:
    """PasarGuard requires shadowsocks password min_length=22 (Pydantic)."""
    pw = (password or "").strip()
    if len(pw) >= 22:
        return pw
    digest = hashlib.sha256(f"{pw}|{seed}".encode("utf-8")).hexdigest()
    if pw:
        return (pw + digest)[:32]
    return digest[:22]


def sanitize_user_proxy_settings(pg_db: Path) -> dict:
    """Fix proxy_settings that make PasarGuard ``/sub`` return HTTP 500.

    Official x-ui migrator copies short trojan passwords into shadowsocks.
    PasarGuard's ``ShadowsocksSettings.password`` requires ``min_length=22``, so
    ``UsersResponseWithInbounds.model_validate`` raises → Internal Server Error
    even when redirect mapping is correct.
    """
    path = Path(pg_db)
    conn = sqlite3.connect(str(path))
    try:
        if "users" not in _xui_table_names(conn):
            return {"fixed": 0, "reason": "no-users"}
        fixed = 0
        rows = list(conn.execute("SELECT id, username, proxy_settings FROM users"))
        for user_id, username, raw in rows:
            try:
                ps = json.loads(raw or "{}")
            except json.JSONDecodeError:
                continue
            if not isinstance(ps, dict):
                continue
            changed = False

            ss = ps.get("shadowsocks")
            if isinstance(ss, dict):
                old_pw = str(ss.get("password") or "")
                new_pw = _ensure_shadowsocks_password(old_pw, seed=str(username or user_id))
                if new_pw != old_pw:
                    ss["password"] = new_pw
                    changed = True
                method = str(ss.get("method") or "").strip()
                if not method:
                    ss["method"] = "chacha20-ietf-poly1305"
                    changed = True

            # Keep trojan password as-is (no min length); just ensure dict shape
            trojan = ps.get("trojan")
            if isinstance(trojan, dict) and not trojan.get("password"):
                # Prefer original ss/trojan material if somehow empty
                fallback = ""
                if isinstance(ss, dict):
                    fallback = str(ss.get("password") or "")
                if fallback:
                    trojan["password"] = fallback[:128]
                    changed = True

            vless = ps.get("vless")
            if isinstance(vless, dict):
                # Empty flow is fine for clients but drop unknown-empty noise
                if vless.get("flow") in ("", None):
                    if "flow" in vless:
                        vless.pop("flow", None)
                        changed = True

            if changed:
                conn.execute(
                    "UPDATE users SET proxy_settings=? WHERE id=?",
                    (json.dumps(ps, ensure_ascii=False), user_id),
                )
                fixed += 1

        conn.commit()
        return {"fixed": fixed, "users": len(rows)}
    finally:
        conn.close()


def sanitize_core_configs_encryption(pg_db: Path) -> dict:
    """Strip panel-only vless ``encryption`` keys that break Xray/PasarGuard."""
    path = Path(pg_db)
    conn = sqlite3.connect(str(path))
    try:
        if "core_configs" not in _xui_table_names(conn):
            return {"fixed": 0, "reason": "no-core_configs"}
        fixed = 0
        rows = list(conn.execute("SELECT id, config FROM core_configs"))
        for row_id, raw in rows:
            try:
                cfg = json.loads(raw or "{}")
            except json.JSONDecodeError:
                continue
            changed = False
            for ib in cfg.get("inbounds") or []:
                if not isinstance(ib, dict):
                    continue
                if str(ib.get("protocol") or "").lower() != "vless":
                    continue
                settings = ib.get("settings")
                if isinstance(settings, dict) and "encryption" in settings:
                    settings.pop("encryption", None)
                    changed = True
            if changed:
                conn.execute(
                    "UPDATE core_configs SET config=? WHERE id=?",
                    (json.dumps(cfg, ensure_ascii=False), row_id),
                )
                fixed += 1
        conn.commit()
        return {"fixed": fixed}
    finally:
        conn.close()


def seed_hosts_from_xui_inbounds(
    pg_db: Path,
    xui_db: Path,
    *,
    share_address: str = "",
) -> dict:
    """Create one PasarGuard host per inbound so ``/sub`` can render configs.

    Official x-ui migrator never populates ``hosts``. Without them PasarGuard
    subscriptions stay empty / error for clients. Prefer copying modern x-ui
    ``hosts`` rows when present; otherwise synthesize from inbound stream.
    """
    pg_path = Path(pg_db)
    xui_path = Path(xui_db)
    address = (share_address or "").strip() or _guess_share_address(xui_path)

    xui = sqlite3.connect(f"file:{xui_path.as_posix()}?mode=ro", uri=True)
    try:
        xui.row_factory = sqlite3.Row
        xui_tables = _xui_table_names(xui)
        inbound_meta: dict[int, dict] = {}
        for row in xui.execute(
            "SELECT id, remark, port, protocol, tag, stream_settings FROM inbounds"
        ):
            try:
                stream = json.loads(row["stream_settings"] or "{}")
            except json.JSONDecodeError:
                stream = {}
            tag = (row["tag"] or "").strip() or f"inbound-{row['id']}"
            inbound_meta[int(row["id"])] = {
                "tag": tag,
                "remark": (row["remark"] or tag or f"inbound-{row['id']}").strip(),
                "port": int(row["port"] or 0) or None,
                "protocol": str(row["protocol"] or ""),
                "stream": stream if isinstance(stream, dict) else {},
            }

        xui_hosts: list[dict] = []
        if "hosts" in xui_tables:
            for row in xui.execute("SELECT * FROM hosts WHERE COALESCE(is_disabled,0)=0"):
                xui_hosts.append(dict(row))
    finally:
        xui.close()

    conn = sqlite3.connect(str(pg_path))
    try:
        tables = _xui_table_names(conn)
        if "hosts" not in tables or "inbounds" not in tables:
            return {"seeded": 0, "reason": "missing-tables"}

        existing = int(conn.execute("SELECT COUNT(*) FROM hosts").fetchone()[0])
        if existing > 0:
            return {"seeded": 0, "reason": "already-present", "hosts": existing}

        pg_tags = {
            str(r[0])
            for r in conn.execute("SELECT tag FROM inbounds")
            if r[0]
        }
        # Ensure tags from x-ui exist (official migrator keeps same tags)
        host_cols = {r[1] for r in conn.execute("PRAGMA table_info(hosts)")}
        inserted = 0
        priority = 1

        def _insert_host(
            *,
            remark: str,
            inbound_tag: str,
            port: int | None,
            addr: str,
            overlay: dict,
        ) -> None:
            nonlocal inserted, priority
            if inbound_tag not in pg_tags:
                return
            fields = {
                "remark": (remark or inbound_tag)[:256],
                "address": (addr or "127.0.0.1")[:256],
                "port": port,
                "inbound_tag": inbound_tag,
                "security": overlay.get("security") or "inbound_default",
                "fingerprint": overlay.get("fingerprint") or "none",
                "priority": priority,
                "is_disabled": 0,
                "random_user_agent": 0,
                "use_sni_as_host": 0,
                "status": "",
            }
            if overlay.get("path") and "path" in host_cols:
                fields["path"] = str(overlay["path"])[:256]
            if overlay.get("sni") and "sni" in host_cols:
                fields["sni"] = str(overlay["sni"])[:1000]
            if overlay.get("host") and "host" in host_cols:
                fields["host"] = str(overlay["host"])[:1000]
            # Never write alpn='none' — EnumArray cannot parse it (→ /sub 500)
            if overlay.get("alpn") and "alpn" in host_cols:
                fields["alpn"] = str(overlay["alpn"])[:14]
            elif "alpn" in host_cols:
                fields["alpn"] = ""

            use = {k: v for k, v in fields.items() if k in host_cols}
            cols = list(use.keys())
            conn.execute(
                f"INSERT INTO hosts ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                [use[c] for c in cols],
            )
            inserted += 1
            priority += 1

        # 1) Prefer modern x-ui managed hosts
        for h in xui_hosts:
            meta = inbound_meta.get(int(h.get("inbound_id") or 0))
            if not meta:
                continue
            addr = (h.get("address") or "").strip() or address
            port = int(h.get("port") or 0) or meta["port"]
            overlay = _host_overlay_from_stream(meta["stream"])
            # Host-level overrides from x-ui hosts table
            if h.get("security") and str(h.get("security")) != "same":
                sec = str(h.get("security")).lower()
                if sec in ("tls", "none", "inbound_default"):
                    overlay["security"] = sec
            if h.get("path"):
                overlay["path"] = str(h.get("path"))
            if h.get("sni"):
                overlay["sni"] = str(h.get("sni"))
            if h.get("host_header"):
                overlay["host"] = str(h.get("host_header"))
            if h.get("fingerprint"):
                overlay["fingerprint"] = str(h.get("fingerprint")) or "none"
            if h.get("alpn"):
                raw_alpn = h.get("alpn")
                if isinstance(raw_alpn, str) and raw_alpn not in ("", "none", "same"):
                    try:
                        parsed = json.loads(raw_alpn)
                        if isinstance(parsed, list):
                            overlay["alpn"] = ",".join(str(x) for x in parsed if x)
                        else:
                            overlay["alpn"] = raw_alpn
                    except json.JSONDecodeError:
                        overlay["alpn"] = raw_alpn
            _insert_host(
                remark=str(h.get("remark") or meta["remark"]),
                inbound_tag=meta["tag"],
                port=port,
                addr=addr,
                overlay=overlay,
            )

        # 2) Fallback: one host per inbound from stream_settings
        if inserted == 0:
            for meta in inbound_meta.values():
                _insert_host(
                    remark=meta["remark"],
                    inbound_tag=meta["tag"],
                    port=meta["port"],
                    addr=address,
                    overlay=_host_overlay_from_stream(meta["stream"]),
                )

        conn.commit()
        return {
            "seeded": inserted,
            "address": address,
            "from_xui_hosts": bool(xui_hosts) and inserted > 0,
        }
    finally:
        conn.close()


def enrich_subscription_mapping_from_clients(
    mapping_file: Path,
    xui_db: Path,
    pg_db: Path,
    *,
    xui_path: str = "sub",
) -> dict:
    """Fill missing redirect mappings using modern ``clients.sub_id`` / settings."""
    path = Path(mapping_file)
    if not path.is_file():
        return {"added": 0, "reason": "no-mapping-file"}

    data = json.loads(path.read_text(encoding="utf-8"))
    mappings = data.setdefault("mappings", {})
    if not isinstance(mappings, dict):
        mappings = {}
        data["mappings"] = mappings

    email_to_sub: dict[str, str] = {}
    xui = sqlite3.connect(f"file:{Path(xui_db).as_posix()}?mode=ro", uri=True)
    try:
        xui.row_factory = sqlite3.Row
        tables = _xui_table_names(xui)
        if "clients" in tables:
            for row in xui.execute("SELECT email, sub_id FROM clients"):
                email = (row["email"] or "").strip()
                sub = (row["sub_id"] or "").strip()
                if email and sub:
                    email_to_sub.setdefault(email, sub)
        for row in xui.execute("SELECT settings FROM inbounds"):
            try:
                settings = json.loads(row["settings"] or "{}")
            except json.JSONDecodeError:
                continue
            for client in (settings.get("clients") or []) if isinstance(settings, dict) else []:
                if not isinstance(client, dict):
                    continue
                email = (client.get("email") or "").strip()
                sub = (
                    client.get("subId")
                    or client.get("sub_id")
                    or client.get("sub_token")
                    or ""
                )
                sub = str(sub).strip()
                if email and sub:
                    email_to_sub.setdefault(email, sub)
    finally:
        xui.close()

    pg = sqlite3.connect(f"file:{Path(pg_db).as_posix()}?mode=ro", uri=True)
    try:
        pg.row_factory = sqlite3.Row
        jwt_row = pg.execute("SELECT secret_key FROM jwt LIMIT 1").fetchone()
        jwt_secret = (jwt_row[0] if jwt_row else "") or ""
        users = list(pg.execute("SELECT id, username FROM users"))
    finally:
        pg.close()

    if not jwt_secret:
        return {"added": 0, "reason": "no-jwt"}

    # Same algorithm as PasarGuard/migrations generate_subscription_url_mapping.py
    from base64 import b64encode
    from hashlib import sha256
    from math import ceil
    import time

    def _pg_token(username: str) -> str:
        data = f"{username},{ceil(time.time())}"
        data_b64 = (
            b64encode(data.encode("utf-8"), altchars=b"-_")
            .decode("utf-8")
            .rstrip("=")
        )
        sign = b64encode(
            sha256((data_b64 + jwt_secret).encode("utf-8")).digest(),
            altchars=b"-_",
        ).decode("utf-8")[:10]
        return data_b64 + sign

    sub_path = (xui_path or "sub").strip().strip("/") or "sub"
    added = 0
    for row in users:
        email = (row["username"] or "").strip()
        if not email or email in mappings:
            continue
        sub_id = email_to_sub.get(email)
        if not sub_id:
            continue
        mappings[email] = {
            "user_id": int(row["id"]),
            "old_subscription_url": f"/{sub_path}/{sub_id}",
            "new_subscription_url": f"/sub/{_pg_token(email)}",
        }
        added += 1

    if added:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        normalize_subscription_mapping(path)
    return {"added": added, "mapped_total": len(mappings)}


def assert_migrated_pg_has_data(path: Path) -> dict[str, int]:
    counts = inspect_pasarguard_sqlite(path)
    data_tables = ("users", "inbounds", "groups")
    if any(counts.get(t, 0) > 0 for t in data_tables):
        return counts
    raise RuntimeError(
        "خروجی مهاجرت x-ui خالی است (users/inbounds/groups = 0). "
        "معمولاً به‌خاطر نبود schema مرجع SQLite یا دیتابیس مبدأ خالی است."
    )


# Upstream PasarGuard/migrations bug: debug logs reference `tag` before assignment
# whenever stream_settings has externalProxy / tlsSettings / security.
_XUI_TAG_BUG_NEEDLE = 'logger.debug(f"Removed externalProxy from inbound {tag}")'
_XUI_TAG_BUG_MARKER = (
    'tag = inbound_row.get("tag", f"inbound-{inbound_row.get(\'id\', \'unknown\')}")'
)


def patch_xui_converter_tag_bug(xui_tool: Path) -> bool:
    """Fix UnboundLocalError on `tag` in official x-ui converter (in-place).

    Returns True if the file was patched, False if already fixed / not present.
    """
    path = Path(xui_tool) / "migration" / "transformers" / "converter.py"
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    if _XUI_TAG_BUG_NEEDLE not in text:
        return False

    # Already fixed: tag assignment appears before the first debug that uses it.
    needle_pos = text.find(_XUI_TAG_BUG_NEEDLE)
    assign_pos = text.find(_XUI_TAG_BUG_MARKER)
    if assign_pos != -1 and assign_pos < needle_pos:
        return False

    buggy = (
        "            # Remove external proxy and TLS settings from streamSettings if present\n"
        "            if isinstance(stream_settings, dict):\n"
        "                # Remove proxy-related settings\n"
        '                stream_settings.pop("proxySettings", None)\n'
        '                stream_settings.pop("sockopt", None)\n'
        "                # Remove external proxy\n"
        '                if "externalProxy" in stream_settings:\n'
        '                    stream_settings.pop("externalProxy")\n'
        '                    logger.debug(f"Removed externalProxy from inbound {tag}")\n'
        "                # Remove TLS settings (certificate files won't exist on new system)\n"
        '                if "tlsSettings" in stream_settings:\n'
        '                    stream_settings.pop("tlsSettings")\n'
        '                    logger.debug(f"Removed tlsSettings from inbound {tag}")\n'
        "                # Remove security field (TLS indicator)\n"
        '                if "security" in stream_settings:\n'
        '                    stream_settings.pop("security")\n'
        '                    logger.debug(f"Removed security field from inbound {tag}")\n'
        "            \n"
        '            tag = inbound_row.get("tag", f"inbound-{inbound_row.get(\'id\', \'unknown\')}")\n'
    )
    fixed = (
        '            tag = inbound_row.get("tag", f"inbound-{inbound_row.get(\'id\', \'unknown\')}")\n'
        "            # Remove external proxy and TLS settings from streamSettings if present\n"
        "            if isinstance(stream_settings, dict):\n"
        "                # Remove proxy-related settings\n"
        '                stream_settings.pop("proxySettings", None)\n'
        '                stream_settings.pop("sockopt", None)\n'
        "                # Remove external proxy\n"
        '                if "externalProxy" in stream_settings:\n'
        '                    stream_settings.pop("externalProxy")\n'
        '                    logger.debug(f"Removed externalProxy from inbound {tag}")\n'
        "                # Remove TLS settings (certificate files won't exist on new system)\n"
        '                if "tlsSettings" in stream_settings:\n'
        '                    stream_settings.pop("tlsSettings")\n'
        '                    logger.debug(f"Removed tlsSettings from inbound {tag}")\n'
        "                # Remove security field (TLS indicator)\n"
        '                if "security" in stream_settings:\n'
        '                    stream_settings.pop("security")\n'
        '                    logger.debug(f"Removed security field from inbound {tag}")\n'
        "\n"
    )
    if buggy not in text:
        # Fallback: move assignment before stream_settings cleanup, drop the late assign.
        marker = "            # Remove external proxy and TLS settings from streamSettings if present\n"
        late = (
            '            tag = inbound_row.get("tag", f"inbound-{inbound_row.get(\'id\', \'unknown\')}")\n'
            '            protocol = inbound_row.get("protocol", "vless")\n'
        )
        if marker not in text or late not in text:
            return False
        text2 = text.replace(
            marker,
            '            tag = inbound_row.get("tag", f"inbound-{inbound_row.get(\'id\', \'unknown\')}")\n'
            + marker,
            1,
        )
        text2 = text2.replace(
            late,
            '            protocol = inbound_row.get("protocol", "vless")\n',
            1,
        )
        if text2 == text:
            return False
        path.write_text(text2, encoding="utf-8")
        return True

    path.write_text(text.replace(buggy, fixed, 1), encoding="utf-8")
    return True


def assert_migrated_core_config(path: Path) -> None:
    """Refuse to continue if official migrator skipped core_configs (broken xray config)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        n = _table_count(conn, "core_configs")
        if n is None:
            return  # schema without table — nothing to assert
        if n <= 0:
            raise RuntimeError(
                "مهاجرت x-ui جدول core_configs را خالی گذاشت "
                "(باگ tag در converter رسمی). دوباره تلاش کنید؛ "
                "بدون core_config پنل inboundها را درست لود نمی‌کند."
            )
    finally:
        conn.close()


def _subscription_path_only(url: str) -> str:
    """Path used by redirect-server lookup (must match ``r.URL.Path``, no query)."""
    raw = (url or "").strip()
    if not raw:
        return "/"
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlparse(raw)
        path = parsed.path or "/"
    else:
        path = raw.split("?", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    return path or "/"


def normalize_subscription_mapping(mapping_path: Path) -> dict:
    """Fix official mapping so redirect-server can match client requests.

    Upstream writes ``/sub/{subId}?name={subId}`` but browsers/clients request
    ``/sub/{subId}`` (query is not part of ``URL.Path``) → permanent 404.
    """
    path = Path(mapping_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    mappings = data.get("mappings") or {}
    fixed = 0
    for _key, entry in mappings.items():
        if not isinstance(entry, dict):
            continue
        old = entry.get("old_subscription_url") or ""
        new = entry.get("new_subscription_url") or ""
        old_n = _subscription_path_only(old)
        if old_n != old:
            entry["old_subscription_url"] = old_n
            fixed += 1
        if new and not new.startswith("http"):
            new_n = _subscription_path_only(new)
            if new_n != new:
                entry["new_subscription_url"] = new_n
                fixed += 1
    data["url_formats"] = {
        "old_format": "/sub/{subId}",
        "new_format": "/sub/{token}",
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    data["_normalized_entries"] = fixed
    return data


def read_xui_subscription_listen(xui_db: Path) -> dict:
    """Read 3x-ui subscription listener settings (port/path/certs).

    ``ssl_wanted`` is True when the panel had cert/key paths configured (typical
    ``https://IP:2096/sub/...`` links), even if those files are missing on this host.
    """
    out = {
        "port": 2096,
        "path": "sub",
        "domain": "",
        "cert": "",
        "key": "",
        "cert_path": "",
        "key_path": "",
        "ssl": False,
        "ssl_wanted": False,
    }
    try:
        conn = sqlite3.connect(f"file:{xui_db}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return out

    settings = {str(k): ("" if v is None else str(v)) for k, v in rows}
    port_raw = settings.get("subPort") or settings.get("sub_port") or ""
    try:
        port = int(str(port_raw).strip()) if str(port_raw).strip() else 2096
    except ValueError:
        port = 2096
    if port <= 0 or port > 65535:
        port = 2096
    path = (settings.get("subPath") or settings.get("sub_path") or "sub").strip().strip("/") or "sub"
    domain = (
        settings.get("subURI")
        or settings.get("subDomain")
        or settings.get("sub_domain")
        or ""
    ).strip()
    cert = (settings.get("subCertFile") or settings.get("webCertFile") or "").strip()
    key = (settings.get("subKeyFile") or settings.get("webKeyFile") or "").strip()
    ssl_wanted = bool(cert and key)
    ssl = bool(ssl_wanted and Path(cert).is_file() and Path(key).is_file())
    out.update({
        "port": port,
        "path": path,
        "domain": domain,
        "cert_path": cert,
        "key_path": key,
        "cert": cert if ssl else "",
        "key": key if ssl else "",
        "ssl": ssl,
        "ssl_wanted": ssl_wanted,
    })
    return out


def detect_public_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip or "127.0.0.1"
    except Exception:
        return "127.0.0.1"


def pasarguard_subscription_base_url(env_text: str | None = None) -> str:
    """Base URL where PasarGuard serves ``/sub/{token}`` (redirect target).

    Prefers domain from PasarGuard .env (SUBSCRIPTION_URL_PREFIX / certs);
    falls back to server IP. pg-redirect also re-reads .env at request time.
    """
    from app.services.pg_access import resolve_pasarguard_public_base

    text = env_text
    if text is None and PASARGUARD_ENV.exists():
        text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
    return resolve_pasarguard_public_base(text)


def build_redirect_server_config(
    *,
    listen_port: int,
    redirect_domain: str,
    ssl_cert: str = "",
    ssl_key: str = "",
) -> dict:
    ssl_enabled = bool(ssl_cert and ssl_key)
    cert_pem = ""
    key_pem = ""
    if ssl_enabled:
        cert_pem = Path(ssl_cert).read_text(encoding="utf-8", errors="ignore")
        key_pem = Path(ssl_key).read_text(encoding="utf-8", errors="ignore")
    return {
        "host": "0.0.0.0",
        "port": int(listen_port),
        "redirect_domain": (redirect_domain or "").rstrip("/"),
        "panel": "x-ui",
        "ssl": {
            "enabled": ssl_enabled,
            "cert": cert_pem,
            "key": key_pem,
        },
    }


class XuiMigrator(BaseMigrator):
    async def run(self, params: dict) -> dict:
        upload_path = params.get("upload_path")
        upload_work_dir = params.get("upload_work_dir")
        install_redirect = params.get("install_redirect", True)
        target_db = normalize_target_db(params.get("target_db") or "sqlite")
        params["target_db"] = target_db
        self.params["target_db"] = target_db
        self.job.log(f"x-ui → PasarGuard ({target_db}) on {app_version()}")

        self.job.set_progress(5, "یافتن دیتابیس 3x-ui...")

        xui_db = await self._locate_xui_db(upload_path, upload_work_dir)

        if not xui_db or not Path(xui_db).exists():
            raise RuntimeError("دیتابیس x-ui.db یافت نشد — لطفاً آپلود کنید")

        self.job.log(f"Using 3x-ui database: {xui_db}")
        src_counts = assert_xui_source_has_data(Path(xui_db))
        self.job.log(
            "Source x-ui counts: "
            + ", ".join(f"{k}={v}" for k, v in src_counts.items())
        )

        self.job.set_progress(15, "بررسی PasarGuard...")
        if not PASARGUARD_DIR.exists():
            raise RuntimeError("PasarGuard نصب نیست — ابتدا نصب کنید")

        # Snapshot install .env BEFORE any rewrite (needed for MySQL app-user finalize)
        install_env_snapshot = ""
        if PASARGUARD_ENV.exists():
            install_env_snapshot = PASARGUARD_ENV.read_text(
                encoding="utf-8", errors="ignore",
            )

        self.job.set_progress(30, "آماده‌سازی ابزار مهاجرت x-ui...")
        xui_tool = TOOLS_DIR / "migrations" / "x-ui"
        if not xui_tool.exists():
            raise RuntimeError("ابزار x-ui migration یافت نشد")

        if patch_xui_converter_tag_bug(xui_tool):
            self.job.log(
                "Patched official x-ui converter: assign inbound tag before stream_settings cleanup"
            )

        schema_db = resolve_xui_schema_db()
        self.job.log(f"Using PasarGuard schema reference: {schema_db}")

        work_dir = BACKUP_DIR / f"xui-{self.job.job_id}"
        work_dir.mkdir(parents=True, exist_ok=True)
        input_db = work_dir / "x-ui.db"
        # WAL-safe copy so uploads next to .db-wal/.db-shm stay consistent
        safe_copy_sqlite(Path(xui_db), input_db)

        # Auto-detect legacy vs modern 3x-ui schema from uploaded tables (no user choice)
        schema_info = detect_xui_db_schema(input_db)
        if schema_info.get("modern"):
            self.job.log(
                "Detected 3x-ui schema: MODERN "
                "(clients + client_inbounds) — "
                f"clients={schema_info.get('clients')} "
                f"memberships={schema_info.get('memberships')} "
                f"traffics={schema_info.get('traffics')}"
            )
        else:
            self.job.log(
                "Detected 3x-ui schema: LEGACY "
                "(classic client_traffics / settings.clients) — "
                f"traffics={schema_info.get('traffics')}"
            )

        # Newer 3x-ui multi-inbound clients schema → classic shape for official migrator
        norm = normalize_modern_xui_sqlite(input_db)
        if norm.get("normalized"):
            self.job.log(
                "Normalized modern 3x-ui multi-inbound schema: "
                f"clients={norm.get('clients')} memberships={norm.get('memberships')} "
                f"traffics={norm.get('traffics_written')} "
                f"settings_inbounds={norm.get('inbounds_settings_updated')}"
            )
        else:
            self.job.log(
                f"Normalize skipped ({norm.get('reason', 'legacy-schema')}) — "
                "using DB as-is for official migrator"
            )
        output_dir = work_dir / "output-db"

        self.job.set_progress(45, "اجرای مهاجرت x-ui → PasarGuard SQLite...")
        ok, out = await self._run_cmd(
            ["uv", "run", "migrate.py",
             "--input-db", str(input_db),
             "--schema-db", str(schema_db),
             "--output-folder", str(output_dir),
             "--log-level", "INFO"],
            cwd=str(xui_tool),
            timeout=1200,
        )
        if not ok:
            raise RuntimeError(f"مهاجرت x-ui ناموفق: {out}")
        # Official tool can exit 0 even when core_configs failed (UnboundLocalError).
        if out and "Failed to migrate core_configs" in out:
            raise RuntimeError(
                "مهاجرت x-ui برای core_configs شکست خورد "
                f"(معمولاً باگ tag در converter): {out[-800:]}"
            )

        output_db = output_dir / "db.sqlite3"
        if not output_db.exists():
            raise RuntimeError("دیتابیس خروجی ایجاد نشد")

        # Multi-inbound clients: official tool links each user to one inbound only
        group_sync = sync_user_groups_from_xui_settings(output_db, input_db)
        if group_sync.get("synced"):
            self.job.log(
                "Synced user↔inbound groups from x-ui settings: "
                f"emails={group_sync.get('emails')} "
                f"links_added={group_sync.get('links_added')} "
                f"orphans_removed={group_sync.get('orphans_removed')}"
            )

        admin_info = ensure_sudo_admin_from_xui(output_db, input_db)
        if admin_info.get("created"):
            self.job.log(
                f"Created sudo admin from x-ui panel user: {admin_info.get('username')}"
            )

        enc_fix = sanitize_core_configs_encryption(output_db)
        if enc_fix.get("fixed"):
            self.job.log(
                f"Stripped vless encryption from core_configs ({enc_fix.get('fixed')} row(s))"
            )

        proxy_fix = sanitize_user_proxy_settings(output_db)
        if proxy_fix.get("fixed"):
            self.job.log(
                "Fixed user proxy_settings for PasarGuard /sub validation: "
                f"{proxy_fix.get('fixed')}/{proxy_fix.get('users')} users "
                "(shadowsocks password min 22 chars)"
            )

        # PasarGuard /sub needs hosts; official migrator leaves hosts empty
        xui_listen_early = read_xui_subscription_listen(Path(xui_db))
        share_addr = (
            (params.get("redirect_domain") or "").strip()
            or _guess_share_address(input_db, xui_listen_early)
        )
        # redirect_domain may be a full URL
        if share_addr.startswith("http://") or share_addr.startswith("https://"):
            share_addr = urlparse(share_addr).hostname or share_addr
        hosts_info = seed_hosts_from_xui_inbounds(
            output_db, input_db, share_address=share_addr,
        )
        if hosts_info.get("seeded"):
            self.job.log(
                f"Seeded {hosts_info.get('seeded')} PasarGuard host(s) "
                f"address={hosts_info.get('address')} "
                f"(from_xui_hosts={hosts_info.get('from_xui_hosts')})"
            )
        else:
            self.job.log(
                f"Host seed skipped: {hosts_info.get('reason')} "
                f"(hosts={hosts_info.get('hosts', 0)})"
            )

        out_counts = assert_migrated_pg_has_data(output_db)
        assert_migrated_core_config(output_db)
        self.job.log(
            "Migrated SQLite counts: "
            + ", ".join(f"{k}={v}" for k, v in out_counts.items())
        )

        # Land migrated SQLite under PasarGuard data (also intermediate for cross-db)
        land_db = PASARGUARD_DATA / "db.sqlite3"
        PASARGUARD_DATA.mkdir(parents=True, exist_ok=True)
        self.job.set_progress(70, "جایگزینی دیتابیس PasarGuard (SQLite)...")
        self._backup_file(land_db, BACKUP_DIR)
        shutil.copy2(output_db, land_db)

        self.job.set_progress(80, "تولید mapping لینک‌های اشتراک...")
        mapping_file = work_dir / "subscription_url_mapping.json"
        xui_listen = read_xui_subscription_listen(Path(xui_db))
        # Allow wizard overrides
        if params.get("xui_sub_port"):
            try:
                xui_listen["port"] = int(params["xui_sub_port"])
            except (TypeError, ValueError):
                pass
        if params.get("xui_sub_path"):
            xui_listen["path"] = str(params["xui_sub_path"]).strip().strip("/") or "sub"

        map_cmd = [
            "uv", "run", "migration/generate_subscription_url_mapping.py",
            "--xui-db", str(input_db),
            "--pasarguard-db", str(land_db),
            "--output", str(mapping_file),
            "--xui-path", xui_listen["path"],
        ]
        await self._run_cmd(map_cmd, cwd=str(xui_tool))
        if mapping_file.exists():
            map_norm = normalize_subscription_mapping(mapping_file)
            self.job.log(
                f"Normalized subscription mapping paths "
                f"(stripped ?name= query; {map_norm.get('_normalized_entries', 0)} entries touched)"
            )
            enrich = enrich_subscription_mapping_from_clients(
                mapping_file,
                input_db,
                land_db,
                xui_path=xui_listen["path"],
            )
            if enrich.get("added"):
                self.job.log(
                    f"Enriched subscription mapping from clients/subId: "
                    f"added={enrich.get('added')} total={enrich.get('mapped_total')}"
                )

        if target_db != "sqlite":
            self.job.set_progress(88, f"Two-phase: SQLite → {target_db}...")
            await self._convert_landed_sqlite_to_target(
                land_db, target_db, install_env_snapshot,
            )

        self.job.set_progress(92, "راه‌اندازی مجدد PasarGuard...")
        await self._start_panel(target_db)

        redirect_installed = False
        redirect_port = xui_listen["port"]
        redirect_domain = (
            (params.get("redirect_domain") or "").strip()
            or pasarguard_subscription_base_url(
                PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
                if PASARGUARD_ENV.exists()
                else install_env_snapshot
            )
        )
        redirect_error = ""
        if install_redirect and mapping_file.exists():
            self.job.set_progress(96, "نصب سرور ریدایرکت لینک‌های قدیمی...")
            redirect_installed, redirect_error = await self._install_redirect_server(
                mapping_file,
                listen_port=redirect_port,
                redirect_domain=redirect_domain,
                ssl_cert=xui_listen.get("cert_path") or xui_listen.get("cert") or "",
                ssl_key=xui_listen.get("key_path") or xui_listen.get("key") or "",
                ssl_wanted=bool(xui_listen.get("ssl_wanted")),
                work_dir=work_dir,
            )

        self.job.set_progress(100, "3x-ui migration complete!")
        redir_scheme = "https" if xui_listen.get("ssl_wanted") else "http"
        redirect_path = (xui_listen.get("path") or "sub").strip().strip("/") or "sub"
        warn_en: list[str] = []
        warn_fa: list[str] = []
        warn_ru: list[str] = []
        if not redirect_installed and install_redirect:
            detail = (redirect_error or "").strip()
            if len(detail) > 280:
                detail = "…" + detail[-280:]
            warn_en.append(
                "pg-redirect did NOT install — old /sub links will not work until fixed. "
                "Users/inbounds already migrated. "
                + (f"Cause: {detail}" if detail else "Often: subscription port still busy, or python3/systemd missing."),
            )
            warn_fa.append(
                "سرویس pg-redirect نصب نشد — لینک‌های قدیمی کار نمی‌کنند (یوزرها منتقل شده‌اند). "
                + (f"علت: {detail}" if detail else "معمولاً پورت ساب هنوز اشغال است یا python3/systemd نیست."),
            )
            warn_ru.append(
                "pg-redirect не установился — старые /sub не работают (пользователи уже перенесены). "
                + (f"Причина: {detail}" if detail else "Часто порт занят или нет python3/systemd."),
            )

        return {
            "panel_url": self._get_panel_url(),
            "subscription_mode": "redirect",
            "redirect_installed": redirect_installed,
            "redirect_port": redirect_port,
            "redirect_path": redirect_path,
            "redirect_domain": redirect_domain,
            "redirect_scheme": redir_scheme,
            "target_db": target_db,
            "mapping_file": str(mapping_file) if mapping_file.exists() else None,
            "source_counts": src_counts,
            "migrated_counts": out_counts,
            "xui_schema": schema_info.get("schema"),
            "xui_schema_modern": bool(schema_info.get("modern")),
            "admin_username": admin_info.get("username") if admin_info.get("created") else None,
            "hosts_seeded": int(hosts_info.get("seeded") or 0),
            "warnings": {
                "en": warn_en,
                "fa": warn_fa,
                "ru": warn_ru,
            },
        }

    async def _install_redirect_server(
        self,
        mapping_file: Path,
        *,
        listen_port: int = 2096,
        redirect_domain: str = "",
        ssl_cert: str = "",
        ssl_key: str = "",
        ssl_wanted: bool = False,
        work_dir: Path | None = None,
    ) -> tuple[bool, str]:
        """Install native pg-redirect so old x-ui /sub links keep working.

        When 3x-ui had subscription TLS configured, old client links are almost
        always ``https://…:subPort/sub/…``. In that case we must speak HTTPS —
        falling back to plain HTTP silently breaks every old link.

        Returns ``(ok, error_detail)``. Uses bundled stdlib service — no GitHub download.
        """
        from app.services.redirect_ops import (
            generate_self_signed_pem,
            install_pg_redirect,
            pg_redirect_is_active,
            resolve_redirect_tls,
        )

        mapping = Path(mapping_file)
        if not mapping.is_file():
            self.job.log("Redirect install skipped: mapping file missing")
            return False, "mapping file missing"

        normalize_subscription_mapping(mapping)
        if not redirect_domain:
            redirect_domain = pasarguard_subscription_base_url()

        env_text = ""
        if PASARGUARD_ENV.exists():
            env_text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")

        # Listener scheme must match OLD client links, not PasarGuard's URL:
        # - x-ui had sub certs → https://IP:subPort/sub/... → HTTPS redirect
        # - x-ui had no sub certs → http://IP:subPort/sub/... → plain HTTP
        # (PG being https://…:8000 must NOT force TLS on the old sub port.)
        want_tls = bool(ssl_wanted)

        cert_pem, key_pem, tls_src = ("", "", "")
        if want_tls:
            cert_pem, key_pem, tls_src = resolve_redirect_tls(
                cert_path=ssl_cert,
                key_path=ssl_key,
                env_text=env_text,
                common_name=detect_public_ip(),
                work_dir=work_dir or mapping.parent,
                want_ssl=True,
            )
            if cert_pem and key_pem:
                self.job.log(f"pg-redirect TLS material: {tls_src}")
            else:
                self.job.log(
                    "pg-redirect: old links need HTTPS but no cert found yet — "
                    "will try self-signed after first failure"
                )
        else:
            self.job.log(
                "pg-redirect: x-ui had no subscription TLS — "
                "listening plain HTTP for old http://sub links"
            )

        ok, err = await install_pg_redirect(
            self,
            mapping,
            listen_port=listen_port,
            redirect_base=redirect_domain,
            panel="x-ui",
            ssl_cert=cert_pem,
            ssl_key=key_pem,
        )
        if ok:
            return True, ""

        # If TLS material was bad/unreadable, retry with a fresh self-signed cert.
        if want_tls:
            self.job.log("Retrying pg-redirect with self-signed HTTPS (for old https://sub links)...")
            generated = generate_self_signed_pem(
                detect_public_ip(),
                Path(work_dir or mapping.parent),
            )
            if generated:
                ok2, err2 = await install_pg_redirect(
                    self,
                    mapping,
                    listen_port=listen_port,
                    redirect_base=redirect_domain,
                    panel="x-ui",
                    ssl_cert=generated[0],
                    ssl_key=generated[1],
                )
                if ok2 or await pg_redirect_is_active(self):
                    self.job.log("pg-redirect active with self-signed TLS")
                    return True, ""
                err = err2 or err
            else:
                self.job.log("self-signed cert generation failed (openssl missing?)")

            # Do NOT fall back to plain HTTP when clients expect https:// — that
            # "succeeds" install while every old subscription link stays broken.
            self.job.log(
                "pg-redirect HTTPS install failed — refusing HTTP fallback "
                "(old https://IP:subPort/sub/… links would not work)"
            )
            return False, err or "HTTPS redirect required but TLS install failed"

        self.job.log(
            "pg-redirect install failed — check logs above "
            "(common: port still busy, python3/systemd missing, or package not bundled)"
        )
        return False, err or "pg-redirect install failed"

    async def _convert_landed_sqlite_to_target(
        self,
        land_db: Path,
        target_db: str,
        install_env_snapshot: str,
    ) -> None:
        """sqlite → server DB with restore-grade MySQL password sync + .env finalize.

        Fixes Access denied for ``pasarguard`` after copy-as-root: align app user
        password and write SQLAlchemy URL from the install snapshot.
        """
        from app.services.db_auth import (
            resolve_live_admin_connection,
            sync_mysql_roles_to_password,
            sync_postgres_roles_to_app_password,
        )
        from app.services.db_credentials import get_target_connection

        env = install_env_snapshot or (
            PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
            if PASARGUARD_ENV.exists()
            else ""
        )
        try:
            env = await self._align_pg_credentials_before_cross_db(
                normalize_target_db(target_db), env,
            )
        except Exception as e:
            self.job.log(f"Credential pre-check note: {e}")

        await run_cross_db_migration(self, str(land_db), "sqlite", target_db)

        target_db = normalize_target_db(target_db)
        app_user = (
            read_env_var(env, "DB_USER")
            or read_env_var(env, "MYSQL_USER")
            or read_env_var(env, "POSTGRES_USER")
            or "pasarguard"
        )
        db_name = (
            read_env_var(env, "DB_NAME")
            or read_env_var(env, "MYSQL_DATABASE")
            or read_env_var(env, "POSTGRES_DB")
            or "pasarguard"
        )
        # Prefer engine-native root/admin secret for sync; URL keeps app user.
        # CRITICAL: the same sync_pwd must be used for role ALTER and .env
        # finalize — otherwise panel URL and live SCRAM secrets diverge.
        if target_db in ("mysql", "mariadb"):
            sync_pwd = (
                read_env_var(env, "MYSQL_ROOT_PASSWORD")
                or read_env_var(env, "DB_PASSWORD")
                or read_env_var(env, "MYSQL_PASSWORD")
                or ""
            )
        elif target_db in ("postgresql", "timescaledb"):
            sync_pwd = (
                read_env_var(env, "POSTGRES_PASSWORD")
                or read_env_var(env, "DB_PASSWORD")
                or ""
            )
        else:
            sync_pwd = read_env_var(env, "DB_PASSWORD") or ""
        admin = get_target_connection(self.params) or {}
        if not sync_pwd:
            sync_pwd = admin.get("password") or ""

        if target_db in ("postgresql", "timescaledb"):
            env_pg_pwd = read_env_var(env, "POSTGRES_PASSWORD")
            env_db_pwd = read_env_var(env, "DB_PASSWORD")
            if env_pg_pwd and env_db_pwd and env_pg_pwd != env_db_pwd:
                chosen = "POSTGRES_PASSWORD" if sync_pwd == env_pg_pwd else "DB_PASSWORD"
                self.job.log(
                    "Note: POSTGRES_PASSWORD and DB_PASSWORD differ in .env — "
                    "the official compose only reads DB_PASSWORD; "
                    f"roles and panel URL will be aligned to {chosen}"
                )

        if target_db in ("mysql", "mariadb") and sync_pwd:
            try:
                live = await resolve_live_admin_connection(
                    self, target_db, env_text=env,
                )
            except Exception:
                live = {**admin, "db_type": target_db, "password": sync_pwd}
            await sync_mysql_roles_to_password(
                self,
                target_db,
                live,
                app_user=app_user,
                password=sync_pwd,
                env_text=env,
            )
            self.job.log(
                f"MySQL app user '{app_user}' aligned for panel SQLAlchemy URL"
            )
        elif target_db in ("postgresql", "timescaledb") and sync_pwd:
            try:
                live = await resolve_live_admin_connection(
                    self, target_db, env_text=env,
                )
            except Exception:
                live = {**admin, "db_type": target_db, "password": sync_pwd}
            await sync_postgres_roles_to_app_password(
                self,
                target_db,
                live,
                env_text=env,
                password=sync_pwd,
            )
            self.job.log(
                f"PostgreSQL roles aligned to finalize password "
                f"(user={app_user})"
            )

        if not PASARGUARD_ENV.exists():
            raise RuntimeError(".env PasarGuard یافت نشد بعد از convert")

        text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
        finalized = finalize_pasarguard_env_after_restore(
            text,
            target_db,
            sync_pwd,
            env,
            db_user=app_user,
            db_name=db_name,
        )
        if not env_points_to_db(finalized, target_db):
            raise RuntimeError(
                f".env SQLALCHEMY_DATABASE_URL با موتور هدف {target_db} هم‌خوان نیست"
            )
        # Restore point: keep the pre-migration .env next to the DB backups.
        self._backup_env_once()
        PASARGUARD_ENV.write_text(finalized, encoding="utf-8")
        self.job.log(f".env finalized for {target_db} (user={app_user})")

        if target_db in ("postgresql", "timescaledb"):
            panel_conn = parse_sqlalchemy_url(
                read_env_var(finalized, "SQLALCHEMY_DATABASE_URL") or "", finalized,
            )
            await self._refresh_pgbouncer_credentials(
                target_db,
                user=panel_conn.get("user") or app_user,
                password=panel_conn.get("password") or sync_pwd,
                database=panel_conn.get("database") or db_name,
            )

        # Panel must not keep reading the intermediate SQLite file
        if land_db.exists() and target_db != "sqlite":
            bak = PASARGUARD_DATA / f"db.sqlite3.pre-convert-{self.job.job_id}.bak"
            if bak.exists():
                bak.unlink()
            shutil.move(str(land_db), str(bak))
            self.job.log(f"Moved SQLite aside → {bak.name}")

    def _backup_env_once(self) -> None:
        """Keep the first .env of this job; later rewrites must not clobber it."""
        dest = BACKUP_DIR / f"{PASARGUARD_ENV.name}.bak.{self.job.job_id}"
        if dest.exists():
            return
        try:
            self._backup_file(PASARGUARD_ENV, BACKUP_DIR)
        except OSError as e:
            self.job.log(f".env backup note: {e}")

    async def _wait_pg_ready(self, service: str, attempts: int = 12) -> bool:
        await self._run_cmd(
            ["docker", "compose", "up", "-d", service],
            cwd=str(PASARGUARD_DIR),
            timeout=180,
        )
        for _ in range(attempts):
            ok, _out = await self._run_cmd(
                ["docker", "compose", "exec", "-T", service, "pg_isready", "-q"],
                cwd=str(PASARGUARD_DIR),
                timeout=30,
                quiet=True,
            )
            if ok:
                return True
            await asyncio.sleep(5)
        return False

    async def _service_image(self, service: str) -> str:
        container = await self._compose_container_id(service)
        if not container:
            return ""
        ok, out = await self._run_cmd(
            ["docker", "inspect", "--format", "{{.Config.Image}}", container],
            timeout=30,
            quiet=True,
        )
        if not ok:
            return ""
        for line in reversed((out or "").splitlines()):
            if line.strip():
                return line.strip()
        return ""

    async def _pg_published_endpoint(self, service: str) -> tuple[str, str]:
        """Host address docker publishes for the database container's 5432."""
        container = await self._compose_container_id(service)
        if not container:
            return "", ""
        ok, out = await self._run_cmd(
            [
                "docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}",
                container,
            ],
            timeout=30,
            quiet=True,
        )
        return parse_published_port(out or "") if ok else ("", "")

    async def _port_publishers(self, port: str) -> list[str]:
        ok, out = await self._run_cmd(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"],
            timeout=30,
            quiet=True,
        )
        return containers_publishing_port(out or "", port) if ok else []

    async def _pg_query_in_container(
        self,
        service: str,
        *,
        user: str,
        password: str,
        sql: str,
        database: str = "postgres",
    ) -> tuple[bool, str]:
        """Run one query through the container's local socket (always trusted)."""
        ok, out = await self._run_cmd(
            [
                "docker", "compose", "exec", "-T",
                "-e", f"PGPASSWORD={password}",
                service, "psql", "-U", user, "-d", database, "-tAc", sql,
            ],
            cwd=str(PASARGUARD_DIR),
            timeout=60,
            quiet=True,
        )
        return ok, (out or "")

    async def _pg_host_login(
        self,
        image: str,
        *,
        host: str,
        port: str,
        user: str,
        password: str,
        database: str = "postgres",
        sql: str = PG_FINGERPRINT_SQL,
    ) -> tuple[str, str]:
        """Log in exactly the way alembic and the data copy do: host → TCP.

        Every probe that runs *inside* the container is worthless for this
        question. Official PostgreSQL images ship a pg_hba whose first host rule
        is ``host all all 127.0.0.1/32 trust``, so a loopback connection there
        accepts any password, while the wizard's connections arrive through
        docker's NAT from the bridge gateway and must pass scram-sha-256.
        """
        if not image:
            return "unusable", "no image resolved for the database service"
        ok, out = await self._run_cmd(
            [
                "docker", "run", "--rm", "--network", "host",
                "--entrypoint", "psql",
                "-e", f"PGPASSWORD={password}",
                image,
                "-h", host, "-p", port, "-U", user, "-d", database, "-tAc", sql,
            ],
            timeout=90,
            quiet=True,
        )
        text = out or ""
        return pg_probe_result(ok and bool(_first_value(text)), text), text

    async def _pg_auth_context(
        self, service: str, *, user: str, password: str,
    ) -> dict[str, str]:
        """What PostgreSQL stores for a role and what its host rule demands."""
        ok, out = await self._pg_query_in_container(
            service, user=user, password=password, sql=pg_auth_context_sql(user),
        )
        return parse_pg_auth_context(out) if ok else {}

    async def _pg_set_cluster_password_encryption(
        self, service: str, *, user: str, password: str, encryption: str,
    ) -> tuple[bool, str]:
        """Make the cluster store *future* passwords the way its hba rule reads them.

        Session-level ``SET`` would only protect this one statement: PasarGuard's
        own role sync runs its ``ALTER ROLE`` again right before alembic, so a
        wrong cluster default silently re-breaks the password we just repaired.
        """
        ok, out = await self._run_cmd(
            [
                "docker", "compose", "exec", "-T",
                "-e", f"PGPASSWORD={password}",
                service, "psql", "-U", user, "-d", "postgres", "-v", "ON_ERROR_STOP=1",
                "-c", f"ALTER SYSTEM SET password_encryption = '{encryption}'",
                "-c", "SELECT pg_reload_conf()",
            ],
            cwd=str(PASARGUARD_DIR),
            timeout=60,
            quiet=True,
        )
        return ok, (out or "")

    async def _pg_force_role_password(
        self,
        service: str,
        *,
        admin_user: str,
        admin_password: str,
        role: str,
        password: str,
        encryption: str = "",
    ) -> tuple[bool, str]:
        """ALTER ROLE through the trusted socket, without a shell in between.

        The shared sync builds one long ``sh -c`` string and runs psql with
        ``ON_ERROR_STOP=0``, so a password containing shell metacharacters — or a
        permission error — is applied wrong or reported as success. ``encryption``
        pins how the verifier is stored, because a password re-applied in the
        wrong encoding still fails every TCP login.
        """
        role_ident = (role or "").replace('"', '""')
        pwd_literal = (password or "").replace("'", "''")
        sql = ""
        if encryption:
            sql += f"SET password_encryption = '{encryption}'; "
        sql += f"ALTER ROLE \"{role_ident}\" WITH LOGIN PASSWORD '{pwd_literal}';"
        ok, out = await self._run_cmd(
            [
                "docker", "compose", "exec", "-T",
                "-e", f"PGPASSWORD={admin_password}",
                service, "psql", "-U", admin_user, "-d", "postgres",
                "-v", "ON_ERROR_STOP=1", "-c", sql,
            ],
            cwd=str(PASARGUARD_DIR),
            timeout=60,
            quiet=True,
        )
        return ok, (out or "")

    def _normalize_pg_env_passwords(self, env_text: str, password: str) -> str:
        """Point existing POSTGRES_PASSWORD / DB_PASSWORD at the working password.

        Role sync, `.env` finalize and PgBouncer all read these keys; while they
        disagree the panel ends up with a URL no role accepts.
        """
        from app.services.env_migration import _set_env_var_simple

        updated = env_text
        changed: list[str] = []
        for key in ("POSTGRES_PASSWORD", "DB_PASSWORD"):
            current = read_env_var(updated, key)
            if not current or current == password:
                continue
            updated = _set_env_var_simple(updated, key, password)
            changed.append(key)
        if not changed:
            return env_text
        self._backup_env_once()
        PASARGUARD_ENV.write_text(updated, encoding="utf-8")
        self.job.log(
            f"Aligned {', '.join(changed)} in .env with the password the database actually accepts"
        )
        return updated

    async def _align_cluster_password_encryption(
        self, service: str, *, user: str, password: str,
    ) -> dict[str, str]:
        """Report the server's password rules and fix a default that cannot work.

        Returns the parsed auth context so callers can also decide whether the
        role's stored verifier itself has to be re-applied.
        """
        ctx = await self._pg_auth_context(service, user=user, password=password)
        if not ctx:
            return {}
        self.job.log(
            f"PostgreSQL auth setup: password_encryption={ctx.get('encryption')}, "
            f"{user} stored as {ctx.get('verifier')}, host rule requires "
            f"{ctx.get('hba')}"
        )
        encryption = required_password_encryption(ctx)
        if not encryption or ctx.get("encryption") == encryption:
            return ctx
        self.job.log(
            f"New passwords would be stored as {ctx.get('encryption')} while "
            f"connections must pass {encryption} — every later ALTER ROLE, "
            "including PasarGuard's own role sync, would write a password that "
            f"cannot authenticate. Switching the cluster default to {encryption}..."
        )
        ok, out = await self._pg_set_cluster_password_encryption(
            service, user=user, password=password, encryption=encryption,
        )
        if not ok:
            self.job.log(f"Could not set password_encryption: {out.strip()[-300:]}")
        return ctx

    async def _probe_pg_candidates(
        self,
        image: str,
        env_text: str,
        *,
        host: str,
        port: str,
        users: list[str],
        passwords: list[str],
        fingerprint: str,
    ) -> tuple[tuple[str, str] | None, str, str]:
        """Try every .env credential from the host; return (working, unusable, error)."""
        unusable = ""
        last_error = ""
        for user in users:
            for pwd in passwords:
                result, out = await self._pg_host_login(
                    image, host=host, port=port, user=user, password=pwd,
                )
                source = describe_password_source(env_text, pwd)
                if result == "ok" and fingerprint and _first_value(out) != fingerprint:
                    self.job.log(
                        f"  {user} / {source} → accepted by {host}:{port}, but that "
                        "server is not the PasarGuard database container (different "
                        "postmaster) — refusing to migrate into it"
                    )
                    last_error = (
                        f"{host}:{port} is a different PostgreSQL server than the "
                        "PasarGuard database container"
                    )
                    continue
                self.job.log(f"  {user} / {source} → {result}")
                if result == "ok":
                    return (user, pwd), "", ""
                if result == "unusable":
                    unusable = out or unusable
                last_error = out or last_error
        return None, unusable, last_error

    async def _align_pg_credentials_before_cross_db(
        self, target_db: str, env_text: str,
    ) -> str:
        """Verify — and if needed repair — the credentials the copy will use.

        Returns the effective .env text so finalize writes the panel URL with the
        same password the roles and PgBouncer end up on.
        """
        if target_db not in ("postgresql", "timescaledb") or not xui_auth_heal_enabled():
            return env_text
        from app.services.db_auth import (
            postgres_admin_users,
            postgres_password_candidates,
            target_database_name,
        )
        from app.services.pasarguard_ops import resolve_db_service

        self.job.log(
            "Verifying PostgreSQL credentials from the host over TCP "
            "(the exact path alembic and the data copy use)..."
        )
        service = resolve_db_service(target_db)
        if not service:
            self.job.log(
                f"docker-compose has no {target_db} service — skipping credential pre-check"
            )
            return env_text
        if not await self._wait_pg_ready(service):
            self.job.log(f"{service} is not accepting connections — skipping credential pre-check")
            return env_text
        image = await self._service_image(service)

        users = postgres_admin_users(env_text)
        passwords = [p for p in postgres_password_candidates(env_text) if p]
        if not users or not passwords:
            self.job.log("No PostgreSQL credentials found in .env — skipping credential pre-check")
            return env_text
        database = target_database_name(env_text, target_db)

        published_host, published_port = await self._pg_published_endpoint(service)
        if published_port:
            self.job.log(
                f"{service} publishes PostgreSQL on {published_host}:{published_port}"
            )
        else:
            self.job.log(
                f"{service} publishes no host port for 5432 — falling back to 127.0.0.1:5432"
            )
        endpoints = pg_endpoint_candidates(published_host, published_port)

        ok_fp, fp_out = await self._pg_query_in_container(
            service, user=users[0], password=passwords[0], sql=PG_FINGERPRINT_SQL,
        )
        fingerprint = _first_value(fp_out) if ok_fp else ""

        ctx = await self._align_cluster_password_encryption(
            service, user=users[0], password=passwords[0],
        )
        encryption = required_password_encryption(ctx)

        working: tuple[str, str] | None = None
        unusable = ""
        last_error = ""
        host, port = "127.0.0.1", "5432"
        for host, port in endpoints:
            self.job.log(
                f"Credential pre-check on {host}:{port}: {len(users)} user(s) × "
                f"{len(passwords)} password(s) from .env"
            )
            working, unusable, last_error = await self._probe_pg_candidates(
                image, env_text,
                host=host, port=port, users=users, passwords=passwords,
                fingerprint=fingerprint,
            )
            # An unreachable endpoint says nothing about the password; a rejected
            # one is the answer, and trying further addresses would only risk
            # landing on some other machine's PostgreSQL.
            if working or not unusable:
                break

        if working is None and unusable:
            # Probe could not reach PostgreSQL — never treat that as a bad password.
            self.job.log(
                f"Could not verify PostgreSQL credentials over {host}:{port} — "
                f"continuing without changes ({unusable.strip()[-300:]})"
            )
            return env_text

        if working is None:
            user, pwd = users[0], passwords[0]
            if password_storage_mismatch(ctx):
                self.job.log(
                    f"That combination can never authenticate: a {ctx.get('verifier')} "
                    f"verifier cannot answer a {encryption} challenge, which is why "
                    "the password looks correct inside the container and is rejected "
                    f"from the host — re-applying it as {encryption}..."
                )
            else:
                self.job.log(
                    f"No .env password authenticates as {user} over {host}:{port} — "
                    f"aligning the role password before the copy ({last_error.strip()[-200:]})"
                )
            ok, out = await self._pg_force_role_password(
                service,
                admin_user=user,
                admin_password=pwd,
                role=user,
                password=pwd,
                encryption=encryption,
            )
            if not ok:
                self.job.log(f"ALTER ROLE {user} failed: {out.strip()[-300:]}")
            else:
                # Re-verify through the same guard: a server that answers on this
                # port but is not our container must not pass on the second try.
                working, unusable_after, error_after = await self._probe_pg_candidates(
                    image, env_text,
                    host=host, port=port, users=[user], passwords=[pwd],
                    fingerprint=fingerprint,
                )
                if working:
                    self.job.log(f"Role {user} aligned — TCP authentication now succeeds")
                else:
                    last_error = error_after or unusable_after or last_error

        if working is None:
            await self._raise_pg_auth_diagnosis(
                service, host=host, port=port, user=users[0], detail=last_error,
            )

        user, pwd = working
        self.job.log(
            f"PostgreSQL TCP auth verified as {user} using "
            f"{describe_password_source(env_text, pwd)} on {host}:{port}"
        )
        self.params["_resolved_target_conn"] = {
            "db_type": target_db,
            "user": user,
            "password": pwd,
            "database": database,
            "host": host,
            "port": port,
        }
        return self._normalize_pg_env_passwords(env_text, pwd)

    async def _raise_pg_auth_diagnosis(
        self, service: str, *, host: str, port: str, user: str, detail: str,
    ) -> None:
        """Stop with the reason, instead of an asyncpg traceback ten seconds later.

        Everything alembic needs has already been tried here with the same
        credentials, so continuing can only reproduce the same failure without
        saying why.
        """
        publishers = await self._port_publishers(port)
        lines = [
            f"PostgreSQL at {host}:{port} rejects every credential in "
            f"{PASARGUARD_ENV} for role {user}, so alembic and the data copy "
            "cannot connect either."
        ]
        if publishers:
            lines.append(f"Host port {port} is published by: {', '.join(publishers)}.")
        else:
            lines.append(
                f"No container publishes host port {port}; the {service} service "
                'needs ports: ["127.0.0.1:5432:5432"] for the wizard to reach it.'
            )
        if detail.strip():
            lines.append(detail.strip()[-400:])
        message = " ".join(lines)
        self.job.log(f"Error: {message}")
        raise RuntimeError(message)

    async def _compose_container_id(self, service: str) -> str:
        ok, out = await self._run_cmd(
            ["docker", "compose", "ps", "-q", service],
            cwd=str(PASARGUARD_DIR),
            timeout=30,
            quiet=True,
        )
        if not ok:
            return ""
        for line in (out or "").splitlines():
            container = line.strip()
            if container:
                return container
        return ""

    async def _container_env(self, container: str) -> dict[str, str]:
        ok, out = await self._run_cmd(
            [
                "docker", "inspect", "--format",
                "{{range .Config.Env}}{{println .}}{{end}}", container,
            ],
            timeout=30,
            quiet=True,
        )
        return parse_container_env(out) if ok else {}

    async def _refresh_pgbouncer_credentials(
        self,
        target_db: str,
        *,
        user: str,
        password: str,
        database: str,
    ) -> bool:
        """Recreate PgBouncer only when its baked-in credentials went stale.

        Installs where the container already matches .env keep the existing
        restart-only behaviour — nothing is recreated and nothing is restarted.
        """
        from app.services.env_migration import _compose_has_pgbouncer

        if target_db not in ("postgresql", "timescaledb"):
            return False
        if not xui_auth_heal_enabled():
            self.job.log(
                f"PgBouncer credential refresh disabled ({XUI_AUTH_HEAL_ENV}=0)"
            )
            return False
        if not _compose_has_pgbouncer():
            return False

        container = await self._compose_container_id("pgbouncer")
        if not container:
            self.job.log("PgBouncer container not found — skipping credential refresh")
            return False
        container_env = await self._container_env(container)
        if not container_env:
            self.job.log("Could not read PgBouncer container env — skipping refresh")
            return False

        stale = pgbouncer_env_mismatch(
            container_env, user=user, password=password, database=database,
        )
        if not stale:
            self.job.log("PgBouncer credentials match .env — no recreate needed")
            return False

        self.job.log(
            f"PgBouncer still holds pre-migration credentials ({', '.join(stale)}) — "
            "restart does not re-read .env, recreating it so the panel can authenticate..."
        )
        cwd = str(PASARGUARD_DIR)
        ok_cfg, cfg_out = await self._run_cmd(
            ["docker", "compose", "config", "-q"], cwd=cwd, timeout=60, quiet=True,
        )
        if not ok_cfg:
            self.job.log(
                "docker compose config is not valid — keeping restart-only behaviour: "
                f"{(cfg_out or '')[-200:]}"
            )
            await self._run_cmd(
                ["docker", "compose", "restart", "pgbouncer"], cwd=cwd, timeout=120,
            )
            return False

        ok, out = await self._run_cmd(
            ["docker", "compose", "up", "-d", "--no-deps", "--force-recreate", "pgbouncer"],
            cwd=cwd,
            timeout=180,
        )
        if not ok:
            self.job.log(
                f"PgBouncer recreate failed — falling back to restart: {(out or '')[-200:]}"
            )
            await self._run_cmd(
                ["docker", "compose", "restart", "pgbouncer"], cwd=cwd, timeout=120,
            )
            return False

        await asyncio.sleep(5)
        self.job.log("PgBouncer recreated with the credentials now in .env")
        return True

    async def _repair_pg_panel_auth(self, target_db: str) -> bool:
        """Align the PostgreSQL role and PgBouncer with the panel's own URL."""
        from app.services.db_auth import (
            resolve_live_admin_connection,
            sync_postgres_roles_to_app_password,
        )
        from app.services.pasarguard_ops import resolve_db_service

        env = (
            PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
            if PASARGUARD_ENV.exists()
            else ""
        )
        panel_conn = parse_sqlalchemy_url(
            read_env_var(env, "SQLALCHEMY_DATABASE_URL") or "", env,
        )
        password = panel_conn.get("password") or ""
        user = panel_conn.get("user") or read_env_var(env, "DB_USER") or "pasarguard"
        database = (
            panel_conn.get("database") or read_env_var(env, "DB_NAME") or "pasarguard"
        )
        if not password:
            self.job.log("No password in SQLALCHEMY_DATABASE_URL — cannot repair auth")
            return False

        try:
            admin = await resolve_live_admin_connection(self, target_db, env_text=env)
        except Exception as probe_error:
            self.job.log(
                f"Live admin probe failed ({probe_error}) — repairing with panel credentials"
            )
            admin = {
                "db_type": target_db,
                "user": user,
                "password": password,
                "database": database,
                "host": "127.0.0.1",
                "port": "5432",
            }

        service = resolve_db_service(target_db)
        if service:
            # Without this the role sync below re-stores the password in an
            # encoding the server's own host rule cannot accept.
            await self._align_cluster_password_encryption(
                service, user=user, password=password,
            )
        synced = await sync_postgres_roles_to_app_password(
            self, target_db, admin, env_text=env, password=password,
        )
        refreshed = await self._refresh_pgbouncer_credentials(
            target_db, user=user, password=password, database=database,
        )
        return bool(synced or refreshed)

    async def _start_panel(self, target_db: str) -> None:
        """Start the panel; repair PostgreSQL auth once if that is what killed it."""
        try:
            await safe_start_pasarguard(self)
            return
        except Exception as start_error:
            if target_db not in ("postgresql", "timescaledb"):
                raise
            if not xui_auth_heal_enabled():
                raise
            from app.services.pasarguard_ops import fetch_pasarguard_logs

            try:
                logs = await fetch_pasarguard_logs(self, tail=120)
            except Exception as log_error:
                self.job.log(f"Could not read panel logs for auth check: {log_error}")
                raise start_error
            if not logs_show_pg_auth_failure(logs):
                raise
            self.job.log(
                "Panel could not authenticate to PostgreSQL — aligning role password "
                "and PgBouncer credentials, then retrying once..."
            )
            try:
                repaired = await self._repair_pg_panel_auth(target_db)
            except Exception as repair_error:
                self.job.log(f"Auth repair note: {repair_error}")
                raise start_error
            if not repaired:
                raise start_error

        await safe_start_pasarguard(self)

    async def _locate_xui_db(
        self,
        upload_path: str | None,
        upload_work_dir: str | None,
    ) -> Path | None:
        """Find x-ui.db, extracting zip uploads into a job work dir when needed."""
        resolved = resolve_xui_db_source(upload_path, upload_work_dir)
        if resolved is None:
            return None

        if resolved.is_dir():
            return find_xui_db_in_dir(resolved)

        if resolved.is_file() and resolved.suffix.lower() == ".zip":
            work = BACKUP_DIR / self.job.job_id
            work.mkdir(parents=True, exist_ok=True)
            ok, out = await self._run_cmd(["unzip", "-o", str(resolved), "-d", str(work)])
            if not ok:
                raise RuntimeError(f"استخراج zip ناموفق: {out}")
            found = find_xui_db_in_dir(work)
            if not found:
                raise RuntimeError("دیتابیس x-ui.db داخل zip یافت نشد")
            return found

        return resolved

    def _get_panel_url(self) -> str:
        from app.services.pg_access import get_panel_access_info

        return (
            get_panel_access_info().get("login_url")
            or f"https://{detect_public_ip()}:8000/dashboard/"
        )
