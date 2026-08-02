"""3x-ui → PasarGuard migration using official PasarGuard/migrations tool.

Always converts to SQLite first. If target_db is not sqlite, runs two-phase
engine to copy head→head into the requested engine.
"""

from __future__ import annotations

import json
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
    read_env_var,
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


def is_modern_xui_multi_inbound_schema(path: Path) -> bool:
    """Newer 3x-ui (multi-inbound clients): ``clients`` + ``client_inbounds`` tables."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = _xui_table_names(conn)
        return "clients" in names and "client_inbounds" in names
    finally:
        conn.close()


def _client_entry_for_protocol(protocol: str, client: dict, flow_override: str = "") -> dict:
    """Build a classic ``settings.clients[]`` entry from a ``clients`` row."""
    email = (client.get("email") or "").strip()
    sub_id = (client.get("sub_id") or client.get("subId") or "").strip()
    flow = (flow_override or client.get("flow") or "").strip()
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
    uuid = (client.get("uuid") or client.get("id") or "").strip()
    password = (client.get("password") or "").strip()
    if proto in ("vless", "vmess", "tunnel"):
        if uuid:
            entry["id"] = uuid
    if proto in ("trojan", "shadowsocks", "shadowsocks2022", "socks", "http"):
        if password:
            entry["password"] = password
    if proto == "vless":
        entry["flow"] = flow
        # Keep auth if present (some panels store it); harmless for PG converter.
        auth = (client.get("auth") or "").strip()
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
    """Base URL where PasarGuard serves ``/sub/{token}`` (redirect target)."""
    text = env_text or ""
    if not text and PASARGUARD_ENV.exists():
        text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")

    # Explicit subscription / public URL wins when set
    for key in (
        "XRAY_SUBSCRIPTION_URL",
        "SUBSCRIPTION_URL",
        "PUBLIC_URL",
        "UVICORN_PUBLIC_URL",
    ):
        val = (read_env_var(text, key) or "").strip().rstrip("/")
        if val.startswith("http://") or val.startswith("https://"):
            # Strip trailing /sub if present
            if val.endswith("/sub"):
                val = val[:-4]
            return val

    port = read_env_var(text, "UVICORN_PORT") or "8000"
    has_ssl = bool(
        read_env_var(text, "UVICORN_SSL_CERTFILE")
        or read_env_var(text, "UVICORN_SSL_KEYFILE")
    )
    scheme = "https" if has_ssl else "http"
    return f"{scheme}://{detect_public_ip()}:{port}"


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
        # Newer 3x-ui multi-inbound clients schema → classic client_traffics shape
        norm = normalize_modern_xui_sqlite(input_db)
        if norm.get("normalized"):
            self.job.log(
                "Normalized modern 3x-ui multi-inbound schema: "
                f"clients={norm.get('clients')} memberships={norm.get('memberships')} "
                f"traffics={norm.get('traffics_written')} "
                f"settings_inbounds={norm.get('inbounds_settings_updated')}"
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
            norm = normalize_subscription_mapping(mapping_file)
            self.job.log(
                f"Normalized subscription mapping paths "
                f"(stripped ?name= query; {norm.get('_normalized_entries', 0)} entries touched)"
            )

        if target_db != "sqlite":
            self.job.set_progress(88, f"Two-phase: SQLite → {target_db}...")
            await self._convert_landed_sqlite_to_target(
                land_db, target_db, install_env_snapshot,
            )

        self.job.set_progress(92, "راه‌اندازی مجدد PasarGuard...")
        await safe_start_pasarguard(self)

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
        warn_en = [
            f"Old /{redirect_path}/{{subId}} links are redirected to PasarGuard via redirect-server.",
            f"Redirect listens {redir_scheme} on port {redirect_port} → {redirect_domain}/sub/…",
            "Create admin: pasarguard cli generate-temp-key",
            "Add Hosts in the panel for each inbound so new subscription configs resolve.",
        ]
        warn_fa = [
            f"لینک‌های قدیمی /{redirect_path}/{{subId}} با redirect-server به پاسارگارد هدایت می‌شوند.",
            f"ریدایرکت {redir_scheme} روی پورت {redirect_port} → {redirect_domain}/sub/…",
            "ادمین بسازید: pasarguard cli generate-temp-key",
            "برای هر inbound در پنل Host بسازید تا کانفیگ سابسکریپشن جدید کامل شود.",
        ]
        warn_ru = [
            f"Старые /{redirect_path}/{{subId}} перенаправляются на PasarGuard через redirect-server.",
            f"Redirect слушает {redir_scheme} на порту {redirect_port} → {redirect_domain}/sub/…",
            "Создайте админа: pasarguard cli generate-temp-key",
            "Создайте Hosts для inbound'ов в панели для новых подписок.",
        ]
        if not redirect_installed and install_redirect:
            detail = (redirect_error or "").strip()
            if len(detail) > 280:
                detail = "…" + detail[-280:]
            warn_en.insert(
                0,
                "pg-redirect did NOT install — old /sub links will not work until fixed. "
                "Users/inbounds already migrated. "
                + (f"Cause: {detail}" if detail else "Often: subscription port still busy, or python3/systemd missing."),
            )
            warn_fa.insert(
                0,
                "سرویس pg-redirect نصب نشد — لینک‌های قدیمی کار نمی‌کنند (یوزرها منتقل شده‌اند). "
                + (f"علت: {detail}" if detail else "معمولاً پورت ساب هنوز اشغال است یا python3/systemd نیست."),
            )
            warn_ru.insert(
                0,
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
            "target_db": target_db,
            "mapping_file": str(mapping_file) if mapping_file.exists() else None,
            "source_counts": src_counts,
            "migrated_counts": out_counts,
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

        await run_cross_db_migration(self, str(land_db), "sqlite", target_db)

        env = install_env_snapshot or (
            PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
            if PASARGUARD_ENV.exists()
            else ""
        )
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
        elif target_db in ("postgresql", "timescaledb") and admin:
            await sync_postgres_roles_to_app_password(
                self, target_db, admin, env_text=env,
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
        PASARGUARD_ENV.write_text(finalized, encoding="utf-8")
        self.job.log(f".env finalized for {target_db} (user={app_user})")

        # Panel must not keep reading the intermediate SQLite file
        if land_db.exists() and target_db != "sqlite":
            bak = PASARGUARD_DATA / f"db.sqlite3.pre-convert-{self.job.job_id}.bak"
            if bak.exists():
                bak.unlink()
            shutil.move(str(land_db), str(bak))
            self.job.log(f"Moved SQLite aside → {bak.name}")

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
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            ip = "SERVER_IP"
        return f"https://{ip}:8000/dashboard/"
