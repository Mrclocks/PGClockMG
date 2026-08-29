"""Strong-password auth for the backup management panel."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path

from app.config import BACKUP_PASSWORD_FILE, BACKUP_SECRET_FILE, BACKUP_SETUP_TOKEN_FILE

COOKIE_NAME = "pgclockmg_backup_session"
COOKIE_MAX_AGE = 12 * 60 * 60
MIN_PASSWORD_LEN = 12

_SPECIAL_RE = re.compile(r"[^A-Za-z0-9]")


class PasswordPolicyError(ValueError):
    pass


def password_policy_errors(password: str) -> list[str]:
    errors: list[str] = []
    if len(password) < MIN_PASSWORD_LEN:
        errors.append(f"min_length_{MIN_PASSWORD_LEN}")
    if not re.search(r"[a-z]", password):
        errors.append("need_lower")
    if not re.search(r"[A-Z]", password):
        errors.append("need_upper")
    if not re.search(r"[0-9]", password):
        errors.append("need_digit")
    if not _SPECIAL_RE.search(password):
        errors.append("need_special")
    return errors


def validate_password_strength(password: str) -> None:
    errs = password_policy_errors(password)
    if errs:
        raise PasswordPolicyError(",".join(errs))


def _hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )
    return "scrypt$" + base64.b64encode(salt).decode("ascii") + "$" + base64.b64encode(dk).decode("ascii")


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, salt_b64, hash_b64 = encoded.split("$", 2)
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    try:
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected = base64.b64decode(hash_b64.encode("ascii"))
    except Exception:
        return False
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )
    return hmac.compare_digest(dk, expected)


def password_is_set() -> bool:
    try:
        return BACKUP_PASSWORD_FILE.is_file() and bool(BACKUP_PASSWORD_FILE.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def set_password(password: str, *, exclusive: bool = False) -> None:
    """Write password hash. If exclusive=True, fail if a password file already exists."""
    validate_password_strength(password)
    BACKUP_PASSWORD_FILE.parent.mkdir(parents=True, exist_ok=True)
    encoded = _hash_password(password)
    if exclusive:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(BACKUP_PASSWORD_FILE, flags, 0o600)
        except FileExistsError as exc:
            raise PasswordPolicyError("password_already_set") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(encoded + "\n")
            os.chmod(BACKUP_PASSWORD_FILE, 0o600)
        except Exception:
            try:
                BACKUP_PASSWORD_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return
    tmp = BACKUP_PASSWORD_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(encoded + "\n")
        os.replace(tmp, BACKUP_PASSWORD_FILE)
        os.chmod(BACKUP_PASSWORD_FILE, 0o600)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def check_password(password: str) -> bool:
    if not password_is_set():
        return False
    try:
        encoded = BACKUP_PASSWORD_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return verify_password(password, encoded)


def get_session_secret() -> bytes:
    BACKUP_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    if BACKUP_SECRET_FILE.is_file():
        raw = BACKUP_SECRET_FILE.read_bytes().strip()
        if len(raw) >= 32:
            return raw
    return rotate_session_secret()


def rotate_session_secret() -> bytes:
    """Issue a new HMAC secret — invalidates all existing session cookies."""
    BACKUP_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    tmp = BACKUP_SECRET_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(secret)
    os.replace(tmp, BACKUP_SECRET_FILE)
    try:
        os.chmod(BACKUP_SECRET_FILE, 0o600)
    except OSError:
        pass
    return secret


def create_session_cookie(ttl: int = COOKIE_MAX_AGE) -> str:
    payload = {
        "exp": int(time.time()) + int(ttl),
        "nonce": secrets.token_hex(8),
    }
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    sig = hmac.new(get_session_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def session_cookie_valid(value: str | None) -> bool:
    if not value or "." not in value:
        return False
    body, sig = value.rsplit(".", 1)
    expected = hmac.new(get_session_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        raw = base64.urlsafe_b64decode(body.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
        exp = int(payload.get("exp") or 0)
    except Exception:
        return False
    return exp >= int(time.time())


def issue_setup_token() -> str:
    """One-time token required for first password setup (printed by installer)."""
    BACKUP_SETUP_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    fd = os.open(BACKUP_SETUP_TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    try:
        os.chmod(BACKUP_SETUP_TOKEN_FILE, 0o600)
    except OSError:
        pass
    return token


def setup_token_is_required() -> bool:
    return BACKUP_SETUP_TOKEN_FILE.is_file() and bool(
        BACKUP_SETUP_TOKEN_FILE.read_text(encoding="utf-8").strip()
    )


def _clean_token(value: str | None) -> str:
    """Normalize pasted tokens (strip whitespace / invisible mobile paste chars)."""
    if not value:
        return ""
    return "".join(ch for ch in value.strip() if ch.isprintable() and not ch.isspace())


def verify_setup_token(provided: str | None) -> bool:
    """Check setup token without consuming it. Never raises on length mismatch."""
    if not setup_token_is_required():
        return True
    expected = _clean_token(BACKUP_SETUP_TOKEN_FILE.read_text(encoding="utf-8"))
    got = _clean_token(provided)
    if not expected or not got or len(got) != len(expected):
        return False
    return hmac.compare_digest(got, expected)


def consume_setup_token(provided: str | None) -> bool:
    """Validate and delete the setup token. Returns False on mismatch/missing."""
    if not verify_setup_token(provided):
        return False
    if not setup_token_is_required():
        return True
    try:
        BACKUP_SETUP_TOKEN_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    return True


def password_file_path() -> Path:
    return BACKUP_PASSWORD_FILE


def clear_empty_password_file() -> None:
    """Remove a leftover empty .password so exclusive first-setup can proceed."""
    try:
        if BACKUP_PASSWORD_FILE.is_file() and not BACKUP_PASSWORD_FILE.read_text(encoding="utf-8").strip():
            BACKUP_PASSWORD_FILE.unlink(missing_ok=True)
    except OSError:
        pass


# Simple in-memory login throttle (per-process; good enough for single uvicorn worker).
_LOGIN_HITS: dict[str, list[float]] = {}
_LOGIN_LOCK_UNTIL: dict[str, float] = {}


def login_is_throttled(key: str, *, max_hits: int = 8, window_sec: int = 300, lock_sec: int = 600) -> bool:
    now = time.time()
    until = _LOGIN_LOCK_UNTIL.get(key) or 0
    if until > now:
        return True
    hits = [t for t in _LOGIN_HITS.get(key, []) if now - t < window_sec]
    _LOGIN_HITS[key] = hits
    return len(hits) >= max_hits


def record_login_failure(key: str, *, max_hits: int = 8, window_sec: int = 300, lock_sec: int = 600) -> None:
    now = time.time()
    hits = [t for t in _LOGIN_HITS.get(key, []) if now - t < window_sec]
    hits.append(now)
    _LOGIN_HITS[key] = hits
    if len(hits) >= max_hits:
        _LOGIN_LOCK_UNTIL[key] = now + lock_sec


def clear_login_failures(key: str) -> None:
    _LOGIN_HITS.pop(key, None)
    _LOGIN_LOCK_UNTIL.pop(key, None)
