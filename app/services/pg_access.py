"""PasarGuard panel access URL and structured post-install guide."""

from __future__ import annotations

import re
import socket
from pathlib import Path

from app.config import PASARGUARD_ENV
from app.services.env_migration import read_env_var
from app.services.prerequisites import is_pasarguard_installed, get_pasarguard_db_type, get_pasarguard_env_summary


def _server_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _read_env() -> str:
    if PASARGUARD_ENV.exists():
        return PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
    return ""


def _has_ssl(env_text: str) -> bool:
    cert = read_env_var(env_text, "UVICORN_SSL_CERTFILE")
    key = read_env_var(env_text, "UVICORN_SSL_KEYFILE")
    return bool(cert and key and not str(cert).startswith("#") and not str(key).startswith("#"))


def _looks_like_ip(host: str | None) -> bool:
    if not host:
        return False
    return bool(re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", host.strip()))


def _hostname_from_url(val: str) -> str | None:
    from urllib.parse import urlparse

    raw = (val or "").strip()
    if not (raw.startswith("http://") or raw.startswith("https://")):
        return None
    host = urlparse(raw).hostname or ""
    if host and "." in host and host not in ("localhost", "127.0.0.1") and not _looks_like_ip(host):
        return host
    return None


def _guess_domain(env_text: str) -> str | None:
    for key in (
        "SUBSCRIPTION_URL_PREFIX",
        "XRAY_SUBSCRIPTION_URL_PREFIX",
        "XRAY_SUBSCRIPTION_URL",
        "SUBSCRIPTION_URL",
        "PUBLIC_URL",
        "UVICORN_PUBLIC_URL",
        "ALLOWED_ORIGINS",
    ):
        raw = read_env_var(env_text, key) or ""
        if key == "ALLOWED_ORIGINS":
            for part in re.split(r"[\s,]+", raw):
                host = _hostname_from_url(part.strip().rstrip("/"))
                if host:
                    return host
            continue
        host = _hostname_from_url(raw.strip().rstrip("/"))
        if host:
            return host
    cert = read_env_var(env_text, "UVICORN_SSL_CERTFILE") or ""
    m = re.search(r"/certs/([^/]+)/", cert.replace("\\", "/"))
    if m and "." in m.group(1) and m.group(1) != "ip" and not _looks_like_ip(m.group(1)):
        return m.group(1)
    return None


def resolve_pasarguard_public_base(env_text: str | None = None) -> str:
    """Public PasarGuard base URL (no trailing slash): domain preferred, else IP.

    Used for subscription redirect targets and related links. Prefer explicit
    subscription/public URL keys, then cert/ALLOWED_ORIGINS domain, then IP.
    """
    text = env_text if env_text is not None else _read_env()
    for key in (
        "SUBSCRIPTION_URL_PREFIX",
        "XRAY_SUBSCRIPTION_URL_PREFIX",
        "XRAY_SUBSCRIPTION_URL",
        "SUBSCRIPTION_URL",
        "PUBLIC_URL",
        "UVICORN_PUBLIC_URL",
    ):
        val = (read_env_var(text, key) or "").strip().rstrip("/")
        if val.startswith("http://") or val.startswith("https://"):
            if val.endswith("/sub"):
                val = val[:-4]
            return val.rstrip("/")

    port = (read_env_var(text, "UVICORN_PORT") or "8000").strip() or "8000"
    scheme = "https" if _has_ssl(text) else "http"
    host = _guess_domain(text) or _server_ip()
    return f"{scheme}://{host}:{port}"


def normalize_dashboard_path(path: str | None, *, default: str = "/dashboard/") -> str:
    """Normalize DASHBOARD_PATH to a leading+trailing-slash form."""
    raw = (path or "").strip().strip('"').strip("'")
    if not raw:
        return default
    if not raw.startswith("/"):
        raw = "/" + raw
    if not raw.endswith("/"):
        raw = raw + "/"
    # Collapse duplicate slashes without touching the leading one
    while "//" in raw:
        raw = raw.replace("//", "/")
    return raw or default


def resolve_dashboard_path(env_text: str | None = None) -> str:
    """Panel UI path from .env: DASHBOARD_PATH (PasarGuard/Marzban), else legacy ROOT_PATH.

    PasarGuard and Marzban both use DASHBOARD_PATH (default ``/dashboard/``).
    Older setups sometimes set UVICORN_ROOT_PATH as a prefix before /dashboard/.
    """
    text = env_text if env_text is not None else ""
    dash = read_env_var(text, "DASHBOARD_PATH") if text else None
    if dash and str(dash).strip():
        return normalize_dashboard_path(str(dash))
    root = (read_env_var(text, "UVICORN_ROOT_PATH") or "").strip().rstrip("/") if text else ""
    if root:
        return normalize_dashboard_path(f"{root}/dashboard/")
    return "/dashboard/"


def build_dashboard_url(
    host: str,
    port: str | int = "8000",
    *,
    https: bool = True,
    root_path: str = "",
    dashboard_path: str | None = None,
) -> str:
    """Canonical panel login URL from host/port + DASHBOARD_PATH.

    Prefer ``dashboard_path`` (from DASHBOARD_PATH in .env). Legacy callers may
    still pass ``root_path`` (UVICORN_ROOT_PATH) which prefixes ``/dashboard/``.
    """
    host = (host or "").strip()
    port = str(port or "8000").strip() or "8000"
    if dashboard_path is not None and str(dashboard_path).strip():
        path = normalize_dashboard_path(dashboard_path)
    elif root_path and str(root_path).strip() and str(root_path).strip() != "/":
        path = normalize_dashboard_path(f"{str(root_path).rstrip('/')}/dashboard/")
    else:
        path = "/dashboard/"
    scheme = "https" if https else "http"
    return f"{scheme}://{host}:{port}{path}"


def get_panel_access_info(prefer_host: str | None = None) -> dict:
    """Return login URL + categorized access guide for installed PasarGuard."""
    installed = is_pasarguard_installed()
    env_text = _read_env() if installed else ""
    detected_ip = _server_ip()
    port = (read_env_var(env_text, "UVICORN_PORT") if env_text else None) or "8000"
    root_path = (read_env_var(env_text, "UVICORN_ROOT_PATH") or "").rstrip("/")
    dashboard_path = resolve_dashboard_path(env_text) if env_text else "/dashboard/"
    ssl = _has_ssl(env_text) if env_text else False
    domain = _guess_domain(env_text) if env_text else None

    prefer = (prefer_host or "").strip() or None
    if prefer and _looks_like_ip(prefer):
        ip = prefer
        host = prefer
    elif prefer:
        domain = prefer
        ip = detected_ip
        host = prefer
    else:
        ip = detected_ip
        host = domain or ip

    public_https = build_dashboard_url(host, port, https=True, dashboard_path=dashboard_path)
    public_http = build_dashboard_url(host, port, https=False, dashboard_path=dashboard_path)
    localhost_url = build_dashboard_url("127.0.0.1", port, https=False, dashboard_path=dashboard_path)
    ssh_tunnel = f"ssh -L {port}:localhost:{port} user@{ip}"
    owner_cmd = "pasarguard cli generate-temp-key"
    env_path = "/opt/pasarguard/.env"
    backup_path = "/opt/pasarguard/backup/"
    node_url = "https://github.com/PasarGuard/node"
    docs_url = "https://docs.pasarguard.org/en/panel/installation/"

    login_url = public_https if (ssl or domain or prefer) else localhost_url

    guide = {
        "en": [
            {
                "title": "1) Create owner account",
                "items": [
                    {"text": "On the server generate a one-time key:", "copy": owner_cmd},
                    {"text": "Open the panel → Owner access → Create owner → paste the key.", "copy": None},
                    {"text": "The key expires in about 5 minutes and works once.", "copy": None},
                ],
            },
            {
                "title": "2) Panel address",
                "items": [
                    {"text": "Dashboard URL:", "copy": public_https},
                    {"text": "Config file:", "copy": env_path},
                    {"text": "Change port/path with UVICORN_PORT and DASHBOARD_PATH in .env", "copy": None},
                ],
            },
            {
                "title": "3) Without SSL (SSH tunnel)",
                "items": [
                    {"text": "Dashboard is localhost-only without SSL.", "copy": None},
                    {"text": "Tunnel from your PC:", "copy": ssh_tunnel},
                    {"text": "Then open:", "copy": localhost_url},
                ],
            } if not ssl else None,
            {
                "title": "4) Node (optional)",
                "items": [
                    {"text": "For master/proxy configs install a node:", "copy": node_url},
                    {"text": "Docs:", "copy": docs_url},
                ],
            },
        ],
        "fa": [
            {
                "title": "۱) ساخت حساب Owner",
                "items": [
                    {"text": "روی سرور این دستور را بزنید:", "copy": owner_cmd},
                    {"text": "پنل را باز کنید → Owner access → Create owner → کلید را وارد کنید.", "copy": None},
                    {"text": "کلید حدود ۵ دقیقه اعتبار دارد و یک‌بارمصرف است.", "copy": None},
                ],
            },
            {
                "title": "۲) آدرس پنل",
                "items": [
                    {"text": "لینک داشبورد:", "copy": public_https},
                    {"text": "مسیر فایل تنظیمات:", "copy": env_path},
                    {"text": "پورت و path را با UVICORN_PORT و DASHBOARD_PATH در .env عوض کنید.", "copy": None},
                ],
            },
            {
                "title": "۳) بدون SSL (تونل SSH)",
                "items": [
                    {"text": "بدون SSL داشبورد فقط روی localhost در دسترس است.", "copy": None},
                    {"text": "از سیستم خودتان تونل بزنید:", "copy": ssh_tunnel},
                    {"text": "بعد این آدرس را باز کنید:", "copy": localhost_url},
                ],
            } if not ssl else None,
            {
                "title": "۴) نود (اختیاری)",
                "items": [
                    {"text": "برای مستر کانفیگ به نود نیاز دارید:", "copy": node_url},
                    {"text": "مستندات نصب:", "copy": docs_url},
                ],
            },
        ],
        "ru": [
            {
                "title": "1) Создать Owner",
                "items": [
                    {"text": "На сервере выполните:", "copy": owner_cmd},
                    {"text": "Панель → Owner access → Create owner → вставьте ключ.", "copy": None},
                    {"text": "Ключ действует ~5 минут и одноразовый.", "copy": None},
                ],
            },
            {
                "title": "2) Адрес панели",
                "items": [
                    {"text": "URL дашборда:", "copy": public_https},
                    {"text": "Файл настроек:", "copy": env_path},
                    {"text": "Порт/path: UVICORN_PORT и DASHBOARD_PATH в .env", "copy": None},
                ],
            },
            {
                "title": "3) Без SSL (SSH-туннель)",
                "items": [
                    {"text": "Без SSL панель только на localhost.", "copy": None},
                    {"text": "Туннель с вашего ПК:", "copy": ssh_tunnel},
                    {"text": "Затем откройте:", "copy": localhost_url},
                ],
            } if not ssl else None,
            {
                "title": "4) Node (опционально)",
                "items": [
                    {"text": "Для master-конфигов нужна нода:", "copy": node_url},
                    {"text": "Документация:", "copy": docs_url},
                ],
            },
        ],
    }
    # Drop None sections (SSL case)
    for lang in guide:
        guide[lang] = [s for s in guide[lang] if s]

    # Legacy flat notes (compat)
    no_ssl_notes = {
        "en": [i["text"] + (f" {i['copy']}" if i.get("copy") else "") for s in guide["en"] if "SSL" in s["title"] or "SSH" in s["title"] for i in s["items"]],
        "fa": [i["text"] + (f" {i['copy']}" if i.get("copy") else "") for s in guide["fa"] if "SSL" in s["title"] or "SSH" in s["title"] for i in s["items"]],
        "ru": [i["text"] + (f" {i['copy']}" if i.get("copy") else "") for s in guide["ru"] if "SSL" in s["title"] or "SSH" in s["title"] for i in s["items"]],
    }
    owner_notes = {
        "en": [i["text"] + (f" {i['copy']}" if i.get("copy") else "") for s in guide["en"][:2] for i in s["items"]],
        "fa": [i["text"] + (f" {i['copy']}" if i.get("copy") else "") for s in guide["fa"][:2] for i in s["items"]],
        "ru": [i["text"] + (f" {i['copy']}" if i.get("copy") else "") for s in guide["ru"][:2] for i in s["items"]],
    }

    return {
        "installed": installed,
        "ssl": ssl,
        "domain": domain,
        "host": host,
        "ip": ip,
        "port": port,
        "root_path": root_path or "/",
        "dashboard_path": dashboard_path,
        "panel_url": login_url,
        "public_url": public_https,
        "public_http_url": public_http,
        "localhost_url": localhost_url,
        "login_url": login_url,
        "ssh_tunnel": ssh_tunnel,
        "owner_cmd": owner_cmd,
        "db_type": get_pasarguard_db_type() if installed else None,
        "env": get_pasarguard_env_summary() if installed else None,
        "guide": guide,
        "no_ssl_notes": no_ssl_notes,
        "owner_notes": owner_notes,
        "node_url": node_url,
        "docs_url": docs_url,
        "env_path": env_path,
        "backup_path": backup_path,
    }
