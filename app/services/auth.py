"""Single-token access control for the wizard."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from app.config import BASE_DIR

TOKEN_FILE = BASE_DIR / ".access_token"
COOKIE_NAME = "pgclockmg_session"
COOKIE_MAX_AGE = 12 * 60 * 60

_cached_token: str | None = None


def _read_token_file() -> str | None:
    try:
        value = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _create_token_file() -> str:
    token = secrets.token_hex(24)
    try:
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read_token_file() or token
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    return token


def get_token() -> str:
    """Access token for this install, generated on first start."""
    global _cached_token
    if _cached_token:
        return _cached_token
    env_token = (os.environ.get("PG_MIGRATOR_TOKEN") or "").strip()
    _cached_token = env_token or _read_token_file() or _create_token_file()
    return _cached_token


def token_matches(candidate: str | None) -> bool:
    if not candidate:
        return False
    return secrets.compare_digest(str(candidate), get_token())


def token_path() -> Path:
    return TOKEN_FILE
