"""Subscription URL mapping index for pg-redirect."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse


def path_only(url: str) -> str:
    """Extract request path used for lookup (no query string)."""
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
    # Drop trailing slash except root
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path or "/"


def build_redirect_url(new_url: str, redirect_base: str) -> str:
    """Build absolute Location target from mapping entry + redirect_base."""
    new = (new_url or "").strip()
    if not new:
        return ""
    if new.startswith("http://") or new.startswith("https://"):
        # Prefer path + live base so host changes in PasarGuard still apply.
        # Absolute URLs are treated as path carriers unless base is empty.
        if redirect_base:
            return (redirect_base.rstrip("/") + path_only(new))
        return new
    path = path_only(new)
    base = (redirect_base or "").rstrip("/")
    if not base:
        return path
    return base + path


def load_path_index(mapping_path: str | Path, redirect_base: str = "") -> dict[str, str]:
    """Build path → *relative* new path index (host applied at request time).

    ``redirect_base`` is accepted for API compatibility but not baked into
    values — the server resolves the live PasarGuard base on each request.
    """
    data = json.loads(Path(mapping_path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("mapping root must be an object")

    _ = redirect_base  # fallback base lives in ServerConfig / live .env
    mappings = data.get("mappings") or {}
    if not isinstance(mappings, dict):
        raise ValueError("mappings must be an object")

    index: dict[str, str] = {}
    for _key, entry in mappings.items():
        if not isinstance(entry, dict):
            continue
        old = entry.get("old_subscription_url") or ""
        new = entry.get("new_subscription_url") or ""
        old_path = path_only(old)
        new_path = path_only(new)
        if old_path and new_path:
            index[old_path] = new_path
            if old_path != "/" and not old_path.endswith("/"):
                index[old_path + "/"] = new_path
    return index


def normalize_mapping_file(mapping_path: str | Path, redirect_base: str = "") -> dict:
    """Normalize paths in-place and optionally stamp redirect_base (fallback only)."""
    path = Path(mapping_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    mappings = data.get("mappings") or {}
    fixed = 0
    for _key, entry in mappings.items():
        if not isinstance(entry, dict):
            continue
        old = entry.get("old_subscription_url") or ""
        new = entry.get("new_subscription_url") or ""
        old_n = path_only(old)
        if old_n != old:
            entry["old_subscription_url"] = old_n
            fixed += 1
        if new:
            new_n = path_only(new)
            if new_n != new:
                entry["new_subscription_url"] = new_n
                fixed += 1
    if redirect_base:
        data["redirect_base"] = redirect_base.rstrip("/")
    data.setdefault("version", 1)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    data["_normalized_entries"] = fixed
    return data
