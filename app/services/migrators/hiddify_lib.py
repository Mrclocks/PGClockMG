"""Pure Hiddify backup parsing + redirect mapping helpers (no I/O side effects)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID


GB = 1024 * 1024 * 1024

# Common Hiddify client subscription suffixes under /{proxy_path}/{uuid}/
HIDDIFY_SUB_SUFFIXES = (
    "",
    "/",
    "/sub",
    "/sub/",
    "/all.txt",
    "/sub.txt",
    "/singbox",
    "/singbox/",
    "/clash",
    "/clash/",
    "/clashmeta",
    "/clashmeta/",
)


def is_hiddify_json_backup(data: Any) -> bool:
    """Detect Hiddify Manager JSON export (users + hconfigs)."""
    if not isinstance(data, dict):
        return False
    if not isinstance(data.get("users"), list):
        return False
    if not isinstance(data.get("hconfigs"), list):
        return False
    # Prefer strong signal: at least one user with uuid
    for u in data["users"][:5]:
        if isinstance(u, dict) and u.get("uuid"):
            return True
    return bool(data["users"]) or bool(data.get("proxies"))


def load_hiddify_json_file(path: Path | str) -> dict:
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    data = json.loads(text)
    if not is_hiddify_json_backup(data):
        raise ValueError("Not a Hiddify Manager JSON backup (missing users/hconfigs)")
    return data


def find_hiddify_json_in_dir(root: Path) -> Path | None:
    """Locate a Hiddify JSON backup under an upload/workspace directory."""
    if not root or not Path(root).exists():
        return None
    root = Path(root)
    candidates: list[Path] = []
    search_roots = [root]
    extracted = root / "extracted"
    if extracted.is_dir():
        search_roots.append(extracted)

    for base in search_roots:
        if base.is_file() and base.suffix.lower() == ".json":
            candidates.append(base)
            continue
        if not base.is_dir():
            continue
        for p in base.rglob("*.json"):
            if not p.is_file():
                continue
            # Skip huge unrelated configs if name hints xray
            name = p.name.lower()
            if name in ("xray_config.json", "package.json", "composer.json"):
                continue
            candidates.append(p)

    # Prefer smaller panel dumps first (xray configs can be large)
    candidates.sort(key=lambda p: (p.stat().st_size if p.exists() else 0, str(p)))
    for p in candidates:
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        if is_hiddify_json_backup(data):
            return p
    return None


def hconfig_map(hconfigs: list[dict] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for row in hconfigs or []:
        if not isinstance(row, dict):
            continue
        key = row.get("key")
        if key:
            out[str(key)] = row.get("value")
    return out


def extract_proxy_paths(hconfigs: list[dict] | None) -> dict[str, str]:
    cfg = hconfig_map(hconfigs)
    client = str(cfg.get("proxy_path_client") or "").strip().strip("/")
    root = str(cfg.get("proxy_path") or "").strip().strip("/")
    admin = str(cfg.get("proxy_path_admin") or "").strip().strip("/")
    return {
        "proxy_path_client": client,
        "proxy_path": root,
        "proxy_path_admin": admin,
    }


def _valid_uuid(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(UUID(str(value).strip()))
    except Exception:
        return None


def sanitize_username(name: str | None, uuid: str, used: set[str]) -> str:
    """PasarGuard: 3–128 chars, [a-zA-Z0-9-_@.]+, no consecutive specials."""
    raw = (name or "").strip()
    # Keep latin/digits; map spaces and others to underscore
    cleaned = re.sub(r"[^a-zA-Z0-9-_@.]+", "_", raw)
    cleaned = re.sub(r"[-_@.]{2,}", "_", cleaned).strip("._@-")
    if len(cleaned) < 3:
        short = (uuid or "user").replace("-", "")[:12]
        cleaned = f"u_{short}"
    if len(cleaned) > 128:
        cleaned = cleaned[:128].rstrip("._@-")
    base = cleaned
    n = 2
    while cleaned.lower() in used:
        suffix = f"_{n}"
        cleaned = (base[: max(3, 128 - len(suffix))] + suffix).rstrip("._@-")
        n += 1
        if n > 10_000:
            short = (uuid or "user").replace("-", "")[:12]
            cleaned = f"u_{short}_{n}"
            break
    used.add(cleaned.lower())
    return cleaned


def _parse_date(value: Any) -> datetime | None:
    if value in (None, "", "None", "null"):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(text[:19], fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def compute_expire_and_status(user: dict) -> dict:
    """Map Hiddify package timing → PasarGuard expire/status fields."""
    enabled = bool(user.get("enable", True))
    package_days = int(user.get("package_days") or 0)
    start = _parse_date(user.get("start_date"))
    now = datetime.now(timezone.utc)

    if not enabled:
        return {
            "status": "disabled",
            "expire": None,
            "on_hold_expire_duration": None,
            "on_hold_timeout": None,
        }

    if start is None and package_days > 0:
        # Hiddify: package not started until first use
        return {
            "status": "on_hold",
            "expire": None,
            "on_hold_expire_duration": package_days * 86400,
            "on_hold_timeout": None,
        }

    if start is not None and package_days > 0:
        expire_dt = start + timedelta(days=package_days)
        return {
            "status": "active" if expire_dt > now else "active",
            "expire": int(expire_dt.timestamp()),
            "on_hold_expire_duration": None,
            "on_hold_timeout": None,
        }

    return {
        "status": "active",
        "expire": None,
        "on_hold_expire_duration": None,
        "on_hold_timeout": None,
    }


def gb_to_bytes(value: Any) -> int:
    try:
        gb = float(value or 0)
    except (TypeError, ValueError):
        gb = 0.0
    if gb <= 0:
        return 0
    return int(gb * GB)


def normalize_hiddify_users(users: list[dict]) -> list[dict]:
    """Normalize backup users for PasarGuard import + redirect mapping."""
    used: set[str] = set()
    out: list[dict] = []
    for raw in users or []:
        if not isinstance(raw, dict):
            continue
        uuid = _valid_uuid(raw.get("uuid"))
        if not uuid:
            continue
        timing = compute_expire_and_status(raw)
        username = sanitize_username(raw.get("name"), uuid, used)
        note = (raw.get("comment") or "").strip()
        if len(note) > 500:
            note = note[:500]
        out.append({
            "username": username,
            "uuid": uuid,
            "original_name": raw.get("name") or "",
            "data_limit": gb_to_bytes(raw.get("usage_limit_GB")),
            "used_traffic": gb_to_bytes(raw.get("current_usage_GB")),
            "data_limit_reset_strategy": "no_reset",
            "enabled": bool(raw.get("enable", True)),
            "note": note or f"hiddify:{uuid}",
            "status": timing["status"] if raw.get("enable", True) else "disabled",
            "expire": timing["expire"],
            "on_hold_expire_duration": timing["on_hold_expire_duration"],
            "on_hold_timeout": timing["on_hold_timeout"],
            "package_days": int(raw.get("package_days") or 0),
            "start_date": raw.get("start_date"),
        })
    return out


def old_hiddify_paths(proxy_path_client: str, user_uuid: str, *, proxy_path: str = "") -> list[str]:
    """Build old Hiddify panel/subscription paths that clients may hit."""
    paths: list[str] = []
    bases: list[str] = []
    client = (proxy_path_client or "").strip().strip("/")
    root = (proxy_path or "").strip().strip("/")
    if client:
        bases.append(client)
    if root and root != client:
        bases.append(root)
    if not bases:
        # Fallback: UUID-only deep paths some setups expose
        bases.append("")

    uid = str(user_uuid).strip()
    for base in bases:
        prefix = f"/{base}/{uid}" if base else f"/{uid}"
        for suf in HIDDIFY_SUB_SUFFIXES:
            paths.append(prefix + suf)
    # Deduplicate preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for p in paths:
        # normalize // 
        while "//" in p:
            p = p.replace("//", "/")
        if not p.startswith("/"):
            p = "/" + p
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered


def subscription_path_from_url(url: str) -> str:
    """Extract /sub/{token} path from absolute or relative subscription URL."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        path = urlparse(raw).path or ""
    else:
        path = raw.split("?", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path


def build_subscription_mapping(
    migrated: list[dict],
    *,
    proxy_path_client: str,
    proxy_path: str = "",
) -> dict:
    """Build pg-redirect mapping JSON structure.

    Each migrated entry needs:
      username, uuid, new_subscription_url (or subscription_url), optional user_id
    """
    mappings: dict[str, dict] = {}
    for row in migrated:
        username = (row.get("username") or "").strip()
        uuid = (row.get("uuid") or "").strip()
        new_url = (
            row.get("new_subscription_url")
            or row.get("subscription_url")
            or ""
        )
        new_path = subscription_path_from_url(str(new_url))
        if not username or not uuid or not new_path:
            continue
        old_paths = old_hiddify_paths(proxy_path_client, uuid, proxy_path=proxy_path)
        # Primary key = username; also store first old path as canonical
        primary_old = old_paths[0] if old_paths else f"/{uuid}"
        mappings[username] = {
            "user_id": row.get("user_id"),
            "uuid": uuid,
            "old_subscription_url": primary_old,
            "new_subscription_url": new_path,
            "old_paths": old_paths,
        }
        # Extra exact-path keys so load_path_index can pick them up if we flatten
        for i, old in enumerate(old_paths):
            if i == 0:
                continue
            key = f"{username}#{i}"
            mappings[key] = {
                "user_id": row.get("user_id"),
                "uuid": uuid,
                "old_subscription_url": old,
                "new_subscription_url": new_path,
            }
    return {
        "version": 1,
        "panel": "hiddify",
        "proxy_path_client": proxy_path_client,
        "proxy_path": proxy_path,
        "mappings": mappings,
    }


def parse_users_from_backup(data: dict) -> tuple[list[dict], dict[str, str]]:
    """Return (normalized_users, proxy_paths) from a Hiddify JSON backup object."""
    paths = extract_proxy_paths(data.get("hconfigs") or [])
    users = normalize_hiddify_users(data.get("users") or [])
    return users, paths


def summarize_backup(data: dict) -> dict:
    users, paths = parse_users_from_backup(data)
    enabled = sum(1 for u in users if u.get("enabled"))
    return {
        "panel": "hiddify",
        "users_total": len(users),
        "users_enabled": enabled,
        "users_disabled": len(users) - enabled,
        "proxy_path_client": paths.get("proxy_path_client") or "",
        "proxy_path": paths.get("proxy_path") or "",
        "domains": len(data.get("domains") or []),
        "proxies": len(data.get("proxies") or []),
        "admin_users": len(data.get("admin_users") or []),
    }
