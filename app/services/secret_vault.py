"""In-memory server-side secret vault for wizard autofill / autopass.

Broad APIs (``/api/info``, system status) must keep returning scrubbed metadata only.
Full password values live here and are exposed only via the scoped credentials
endpoint, or injected server-side when a migrate/restore job starts without an
explicit password from the client.
"""

from __future__ import annotations

import copy
import threading
import time
from typing import Any

from app.services.env_migration import pick_primary_env_password

_LOCK = threading.RLock()
# scope -> {candidates, primary, db_type, updated_at}
_STORE: dict[str, dict[str, Any]] = {}

LIVE_PASARGUARD = "live:pasarguard"
LIVE_MARZBAN = "live:marzban"


def upload_scope(upload_id: str | None) -> str | None:
    if not upload_id:
        return None
    return f"upload:{upload_id}"


def bundle_scope(bundle_id: str | None) -> str | None:
    if not bundle_id:
        return None
    return f"bundle:{bundle_id}"


def put_candidates(
    scope: str,
    candidates: list[dict] | None,
    *,
    db_type: str | None = None,
) -> None:
    """Store full password candidates (including plaintext ``value``) for a scope."""
    if not scope:
        return
    rows = copy.deepcopy(candidates or [])
    primary = pick_primary_env_password(rows, db_type)
    with _LOCK:
        _STORE[scope] = {
            "candidates": rows,
            "primary": primary,
            "db_type": db_type,
            "updated_at": time.time(),
        }


def get_candidates(scope: str) -> list[dict]:
    with _LOCK:
        entry = _STORE.get(scope) or {}
        return copy.deepcopy(entry.get("candidates") or [])


def get_primary(scope: str) -> str | None:
    with _LOCK:
        entry = _STORE.get(scope) or {}
        primary = entry.get("primary")
        return str(primary) if primary else None


def clear_scope(scope: str) -> None:
    with _LOCK:
        _STORE.pop(scope, None)


def resolve_source_scope(params: dict) -> str | None:
    """Pick the best vault scope for migration source credentials."""
    upload_id = params.get("upload_id")
    bundle_id = params.get("upload_bundle_id")
    if upload_id:
        return upload_scope(str(upload_id))
    if bundle_id:
        return bundle_scope(str(bundle_id))
    panel = (params.get("source_panel") or "").lower()
    if panel in ("marzban", "marzneshin"):
        return LIVE_MARZBAN
    return None


def apply_vault_passwords(params: dict) -> dict:
    """Fill missing source/target DB passwords from the vault (autopass)."""
    out = dict(params)
    source_scope = resolve_source_scope(out)
    if not (out.get("source_db_password") or "").strip() and source_scope:
        held = get_primary(source_scope)
        if held:
            out["source_db_password"] = held
    if not (out.get("target_db_password") or "").strip():
        held = get_primary(LIVE_PASARGUARD)
        if held:
            out["target_db_password"] = held
    return out
