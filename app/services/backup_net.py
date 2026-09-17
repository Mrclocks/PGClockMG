"""Network safety helpers for backup panel (SSRF / proxy guards)."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeDestinationError(ValueError):
    pass


_BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata",
})

# Stream destinations may be LAN / loopback (same-DC restore). Webhooks stay public-only.
_STREAM_ALLOWED_LOCAL_HOSTNAMES = frozenset({
    "localhost",
    "localhost.localdomain",
})


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (ip.version == 4 and ip in ipaddress.ip_network("169.254.0.0/16"))
        or (ip.version == 6 and ip in ipaddress.ip_network("fc00::/7"))
    )


def _ip_is_stream_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Block only dangerous targets for backup streaming.

    Private RFC1918 and loopback are allowed so VPS↔VPS LAN and same-host
    restores work. Cloud metadata link-local stays blocked.
    """
    if ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return True
    if ip.version == 4 and ip in ipaddress.ip_network("169.254.0.0/16"):
        return True
    if ip.version == 6 and ip.is_link_local:
        return True
    return False


def assert_public_hostname(host: str) -> None:
    """Reject hosts that resolve only to private/metadata/loopback addresses."""
    raw = (host or "").strip().lower().rstrip(".")
    if not raw:
        raise UnsafeDestinationError("host_empty")
    if raw in _BLOCKED_HOSTNAMES or raw.endswith(".localhost") or raw.endswith(".local"):
        raise UnsafeDestinationError("host_blocked")
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    # Strip optional zone id
    if "%" in raw:
        raw = raw.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(raw)
        if _ip_is_blocked(ip):
            raise UnsafeDestinationError("ip_blocked")
        return
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(raw, None)
    except socket.gaierror as exc:
        raise UnsafeDestinationError("dns_failed") from exc
    if not infos:
        raise UnsafeDestinationError("dns_failed")
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _ip_is_blocked(ip):
            raise UnsafeDestinationError("resolved_ip_blocked")


def assert_stream_hostname(host: str) -> None:
    """Allow private/LAN/loopback for backup→wizard stream; still block metadata."""
    raw = (host or "").strip().lower().rstrip(".")
    if not raw:
        raise UnsafeDestinationError("host_empty")
    if raw in ("metadata.google.internal", "metadata"):
        raise UnsafeDestinationError("host_blocked")
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    if "%" in raw:
        raw = raw.split("%", 1)[0]
    if raw in _STREAM_ALLOWED_LOCAL_HOSTNAMES or raw.endswith(".localhost"):
        return
    try:
        ip = ipaddress.ip_address(raw)
        if _ip_is_stream_blocked(ip):
            raise UnsafeDestinationError("ip_blocked")
        return
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(raw, None)
    except socket.gaierror as exc:
        raise UnsafeDestinationError("dns_failed") from exc
    if not infos:
        raise UnsafeDestinationError("dns_failed")
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _ip_is_stream_blocked(ip):
            raise UnsafeDestinationError("resolved_ip_blocked")


def _normalize_http_url(raw: str, *, hostname_checker) -> str:
    text = (raw or "").strip().rstrip("/")
    if not text:
        raise UnsafeDestinationError("url_empty")
    if "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeDestinationError("url_scheme")
    host = parsed.hostname
    if not host:
        raise UnsafeDestinationError("url_host")
    hostname_checker(host)
    netloc = host
    if parsed.port:
        netloc = f"{host}:{parsed.port}"
    path = parsed.path or ""
    return f"{parsed.scheme}://{netloc}{path}".rstrip("/")


def normalize_public_http_url(raw: str) -> str:
    """Public-only URL (webhooks / notify). Private destinations rejected."""
    return _normalize_http_url(raw, hostname_checker=assert_public_hostname)


def normalize_stream_dest_url(raw: str) -> str:
    """URL for backup→wizard streaming. Private LAN / localhost allowed."""
    return _normalize_http_url(raw, hostname_checker=assert_stream_hostname)


def assert_proxy_host(host: str) -> None:
    """Proxy may be private (intentional) but must be a plain hostname/IP — no weird schemes."""
    raw = (host or "").strip()
    if not raw:
        raise UnsafeDestinationError("proxy_host_empty")
    if "://" in raw or "/" in raw or "@" in raw or " " in raw:
        raise UnsafeDestinationError("proxy_host_invalid")
    # Allow private IPs for LAN proxies, but reject metadata link-local abuse patterns
    try:
        ip = ipaddress.ip_address(raw.strip("[]"))
        if ip.is_unspecified or ip.is_multicast:
            raise UnsafeDestinationError("proxy_ip_blocked")
        if ip.version == 4 and ip in ipaddress.ip_network("169.254.0.0/16"):
            raise UnsafeDestinationError("proxy_ip_blocked")
    except ValueError:
        # hostname — block obvious metadata names
        low = raw.lower().rstrip(".")
        if low in ("metadata.google.internal", "metadata") or low.endswith(".localhost"):
            raise UnsafeDestinationError("proxy_host_blocked")
