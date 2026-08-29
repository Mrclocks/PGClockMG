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

from app.config import BACKUP_PASSWORD_FILE, BACKUP_SECRET_FILE

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


def set_password(password: str) -> None:
    validate_password_strength(password)
    BACKUP_PASSWORD_FILE.parent.mkdir(parents=True, exist_ok=True)
    encoded = _hash_password(password)
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
    secret = secrets.token_bytes(32)
    fd = os.open(BACKUP_SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(secret)
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


def password_file_path() -> Path:
    return BACKUP_PASSWORD_FILE
