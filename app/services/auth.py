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


def _write_token_file(token: str) -> None:
    """Create or replace the token file with mode 0600."""
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token.strip() + "\n")
        os.replace(tmp, TOKEN_FILE)
        try:
            os.chmod(TOKEN_FILE, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _create_token_file() -> str:
    token = secrets.token_hex(24)
    try:
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = _read_token_file()
        if existing:
            return existing
        # Empty/racy file — overwrite safely.
        _write_token_file(token)
        return token
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    return token


def get_token() -> str:
    """Access token for this install.

    Prefer the on-disk file (so ``cat .access_token`` always works). Fall back to
    ``PG_MIGRATOR_TOKEN`` and persist it to disk when the file is missing. Generate
    a new token on first start when neither exists.
    """
    global _cached_token
    if _cached_token:
        return _cached_token

    file_token = _read_token_file()
    if file_token:
        _cached_token = file_token
        return _cached_token

    env_token = (os.environ.get("PG_MIGRATOR_TOKEN") or "").strip()
    if env_token:
        try:
            _write_token_file(env_token)
        except OSError:
            pass
        _cached_token = env_token
        return _cached_token

    _cached_token = _create_token_file()
    return _cached_token


def ensure_token() -> str:
    """Make sure the access-token file exists and return its value."""
    return get_token()


def token_matches(candidate: str | None) -> bool:
    if not candidate:
        return False
    return secrets.compare_digest(str(candidate), get_token())


def token_path() -> Path:
    return TOKEN_FILE
