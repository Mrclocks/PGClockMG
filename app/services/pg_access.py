"""PasarGuard panel access URL and no-SSL instructions."""

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
    return bool(cert and key and not cert.startswith("#") and not key.startswith("#"))


def _guess_domain(env_text: str) -> str | None:
    cert = read_env_var(env_text, "UVICORN_SSL_CERTFILE") or ""
    # /var/lib/pasarguard/certs/panel.example.com/fullchain.pem
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


def get_panel_access_info(prefer_host: str | None = None) -> dict:
    """Return login URL + access notes for installed PasarGuard."""
    installed = is_pasarguard_installed()
    env_text = _read_env() if installed else ""
    ip = prefer_host or _server_ip()
    port = read_env_var(env_text, "UVICORN_PORT") if env_text else None
    port = port or "8000"
    root_path = (read_env_var(env_text, "UVICORN_ROOT_PATH") or "").rstrip("/")
    ssl = _has_ssl(env_text) if env_text else False
    domain = _guess_domain(env_text) if env_text else None
    host = prefer_host or domain or ip
    scheme = "https" if ssl else "http"
    path = f"{root_path}/dashboard/".replace("//", "/")
    if not path.startswith("/"):
        path = "/" + path

    panel_url = f"{scheme}://{host}:{port}{path}"
    # Without SSL, official docs say panel binds localhost-only
    localhost_url = f"http://127.0.0.1:{port}{path}"
    ssh_tunnel = f"ssh -L {port}:localhost:{port} user@{ip}"

    no_ssl_notes = {
        "en": [
            "Without SSL, PasarGuard dashboard is only reachable on localhost.",
            f"From your PC run: {ssh_tunnel}",
            f"Then open: {localhost_url}",
            "You lose access when the SSH session closes (testing only).",
            "See: https://docs.pasarguard.org/en/panel/installation/",
        ],
        "fa": [
            "بدون SSL، داشبورد فقط روی localhost در دسترس است.",
            f"روی سیستم خودتان بزنید: {ssh_tunnel}",
            f"سپس باز کنید: {localhost_url}",
            "با بستن SSH دسترسی قطع می‌شود (فقط برای تست).",
            "مستندات: https://docs.pasarguard.org/en/panel/installation/",
        ],
        "ru": [
            "Без SSL панель доступна только на localhost.",
            f"На своём ПК выполните: {ssh_tunnel}",
            f"Затем откройте: {localhost_url}",
            "Доступ пропадёт при закрытии SSH (только для теста).",
            "Документация: https://docs.pasarguard.org/en/panel/installation/",
        ],
    }

    owner_notes = {
        "en": [
            "Create owner: on the server run  pasarguard cli generate-temp-key",
            "Open the panel login page → Owner access → Create owner → paste the key.",
            "Key is valid ~5 minutes and one-time use.",
            "Port and path can be changed in /opt/pasarguard/.env (UVICORN_PORT, UVICORN_ROOT_PATH).",
            "For master/proxy configs you need a node: https://github.com/PasarGuard/node",
        ],
        "fa": [
            "ساخت ادمین owner: روی سرور بزنید  pasarguard cli generate-temp-key",
            "صفحه ورود پنل → Owner access → Create owner → کلید را وارد کنید.",
            "کلید حدود ۵ دقیقه و یک‌بارمصرف است.",
            "پورت و path را می‌توانید در /opt/pasarguard/.env تغییر دهید (UVICORN_PORT، UVICORN_ROOT_PATH).",
            "برای ساخت مستر کانفیگ باید نود نصب شود: https://github.com/PasarGuard/node",
        ],
        "ru": [
            "Создать owner: на сервере выполните  pasarguard cli generate-temp-key",
            "Страница входа → Owner access → Create owner → вставьте ключ.",
            "Ключ действует ~5 минут и одноразовый.",
            "Порт и path можно менять в /opt/pasarguard/.env (UVICORN_PORT, UVICORN_ROOT_PATH).",
            "Для master-конфигов нужна нода: https://github.com/PasarGuard/node",
        ],
    }

    return {
        "installed": installed,
        "ssl": ssl,
        "domain": domain,
        "host": host,
        "ip": ip,
        "port": port,
        "root_path": root_path or "/",
        "panel_url": panel_url if ssl else localhost_url,
        "public_url": panel_url,
        "localhost_url": localhost_url,
        "ssh_tunnel": ssh_tunnel,
        "db_type": get_pasarguard_db_type() if installed else None,
        "env": get_pasarguard_env_summary() if installed else None,
        "no_ssl_notes": no_ssl_notes,
        "owner_notes": owner_notes,
        "node_url": "https://github.com/PasarGuard/node",
        "docs_url": "https://docs.pasarguard.org/en/panel/installation/",
        "env_path": "/opt/pasarguard/.env",
    }
