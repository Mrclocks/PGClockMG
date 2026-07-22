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


def _guess_domain(env_text: str) -> str | None:
    cert = read_env_var(env_text, "UVICORN_SSL_CERTFILE") or ""
    m = re.search(r"/certs/([^/]+)/", cert.replace("\\", "/"))
    if m and "." in m.group(1) and m.group(1) != "ip":
        return m.group(1)
    origins = read_env_var(env_text, "ALLOWED_ORIGINS") or ""
    for part in re.split(r"[\s,]+", origins):
        part = part.strip().rstrip("/")
        m2 = re.match(r"https?://([^/:]+)", part)
        if m2 and "." in m2.group(1) and m2.group(1) not in ("localhost", "127.0.0.1"):
            return m2.group(1)
    return None


def _looks_like_ip(host: str | None) -> bool:
    if not host:
        return False
    return bool(re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", host.strip()))


def build_dashboard_url(host: str, port: str | int = "8000", *, https: bool = True, root_path: str = "") -> str:
    """Canonical panel login URL: https://host:8000/dashboard/"""
    host = (host or "").strip()
    port = str(port or "8000").strip() or "8000"
    rp = (root_path or "").rstrip("/")
    path = f"{rp}/dashboard/".replace("//", "/")
    if not path.startswith("/"):
        path = "/" + path
    scheme = "https" if https else "http"
    return f"{scheme}://{host}:{port}{path}"


def get_panel_access_info(prefer_host: str | None = None) -> dict:
    """Return login URL + categorized access guide for installed PasarGuard."""
    installed = is_pasarguard_installed()
    env_text = _read_env() if installed else ""
    detected_ip = _server_ip()
    port = (read_env_var(env_text, "UVICORN_PORT") if env_text else None) or "8000"
    root_path = (read_env_var(env_text, "UVICORN_ROOT_PATH") or "").rstrip("/")
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

    public_https = build_dashboard_url(host, port, https=True, root_path=root_path)
    public_http = build_dashboard_url(host, port, https=False, root_path=root_path)
    localhost_url = build_dashboard_url("127.0.0.1", port, https=False, root_path=root_path)
    ssh_tunnel = f"ssh -L {port}:localhost:{port} user@{ip}"
    owner_cmd = "pasarguard cli generate-temp-key"
    env_path = "/opt/pasarguard/.env"
    backup_path = "/opt/pasarguard/backup/"
    node_url = "https://github.com/PasarGuard/node"
    docs_url = "https://docs.pasarguard.org/en/panel/installation/"

    # Preferred open URL: if domain/IP known → https://host:port/dashboard/
    login_url = public_https if (domain or prefer or ssl or host) else localhost_url
    if not ssl and not domain and not prefer:
        login_url = localhost_url

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
                    {"text": "Dashboard URL:", "copy": login_url if ssl or domain or prefer else public_https},
                    {"text": "Config file:", "copy": env_path},
                    {"text": "Change port/path with UVICORN_PORT and UVICORN_ROOT_PATH in .env", "copy": None},
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
                    {"text": "لینک داشبورد:", "copy": login_url if ssl or domain or prefer else public_https},
                    {"text": "مسیر فایل تنظیمات:", "copy": env_path},
                    {"text": "پورت و path را با UVICORN_PORT و UVICORN_ROOT_PATH در .env عوض کنید.", "copy": None},
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
                    {"text": "URL дашборда:", "copy": login_url if ssl or domain or prefer else public_https},
                    {"text": "Файл настроек:", "copy": env_path},
                    {"text": "Порт/path: UVICORN_PORT и UVICORN_ROOT_PATH в .env", "copy": None},
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
