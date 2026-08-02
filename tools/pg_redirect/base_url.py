"""Resolve live PasarGuard public base URL for redirect targets.

Reads ``/opt/pasarguard/.env`` (configurable) so when the panel domain or IP
changes later, redirects follow without reinstalling pg-redirect.
"""

from __future__ import annotations

import re
import socket
import time
from pathlib import Path
from urllib.parse import urlparse

_CACHE: dict[str, tuple[float, str]] = {}
_CACHE_TTL_SEC = 30.0

_PREFIX_KEYS = (
    "SUBSCRIPTION_URL_PREFIX",
    "XRAY_SUBSCRIPTION_URL_PREFIX",
    "XRAY_SUBSCRIPTION_URL",
    "SUBSCRIPTION_URL",
    "PUBLIC_URL",
    "UVICORN_PUBLIC_URL",
)


def _read_env_var(text: str, key: str) -> str:
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*)$", re.I | re.M)
    m = pat.search(text or "")
    if not m:
        return ""
    raw = m.group(1).strip()
    if not raw or raw.startswith("#"):
        return ""
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        raw = raw[1:-1]
    return raw.strip()


def _server_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _guess_domain(env_text: str) -> str:
    for key in _PREFIX_KEYS:
        val = _read_env_var(env_text, key)
        if val.startswith("http://") or val.startswith("https://"):
            host = urlparse(val).hostname or ""
            if host and "." in host and host not in ("localhost", "127.0.0.1"):
                return host
    cert = _read_env_var(env_text, "UVICORN_SSL_CERTFILE") or ""
    m = re.search(r"/certs/([^/]+)/", cert.replace("\\", "/"))
    if m and "." in m.group(1) and m.group(1) != "ip":
        return m.group(1)
    origins = _read_env_var(env_text, "ALLOWED_ORIGINS") or ""
    for part in re.split(r"[\s,]+", origins):
        part = part.strip().rstrip("/")
        m2 = re.match(r"https?://([^/:]+)", part)
        if m2 and "." in m2.group(1) and m2.group(1) not in ("localhost", "127.0.0.1"):
            return m2.group(1)
    return ""


def _normalize_base(url: str) -> str:
    val = (url or "").strip().rstrip("/")
    if not val:
        return ""
    if val.endswith("/sub"):
        val = val[:-4]
    return val.rstrip("/")


def resolve_from_env_text(env_text: str) -> str:
    """Return public base (scheme://host:port) or empty if env is blank."""
    text = env_text or ""
    if not text.strip():
        return ""

    for key in _PREFIX_KEYS:
        val = _normalize_base(_read_env_var(text, key))
        if val.startswith("http://") or val.startswith("https://"):
            return val

    port = _read_env_var(text, "UVICORN_PORT") or "8000"
    has_ssl = bool(
        _read_env_var(text, "UVICORN_SSL_CERTFILE")
        and _read_env_var(text, "UVICORN_SSL_KEYFILE")
    )
    scheme = "https" if has_ssl else "http"
    host = _guess_domain(text) or _server_ip()
    return f"{scheme}://{host}:{port}"


def resolve_from_env_file(env_path: str | Path | None) -> str:
    if not env_path:
        return ""
    path = Path(env_path)
    if not path.is_file():
        return ""
    try:
        return resolve_from_env_text(path.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return ""


def resolve_live_base(
    *,
    env_path: str | Path | None = "/opt/pasarguard/.env",
    fallback: str = "",
    cache_key: str | None = None,
    ttl_sec: float = _CACHE_TTL_SEC,
) -> str:
    """Prefer live PasarGuard .env (domain over IP); else fallback config base."""
    key = cache_key or str(env_path or "")
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached and (now - cached[0]) < ttl_sec:
        return cached[1]

    live = resolve_from_env_file(env_path)
    base = _normalize_base(live) or _normalize_base(fallback)
    if not base:
        port = "8000"
        base = f"https://{_server_ip()}:{port}"
    _CACHE[key] = (now, base)
    return base


def clear_base_cache() -> None:
    _CACHE.clear()
