"""Load and validate pg-redirect server configuration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SslConfig:
    enabled: bool = False
    cert: str = ""
    key: str = ""


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 2096
    redirect_base: str = ""
    panel: str = ""
    pasarguard_env: str = "/opt/pasarguard/.env"
    ssl: SslConfig | None = None

    @property
    def redirect_domain(self) -> str:
        """Alias used by older PasarGuard-style configs."""
        return self.redirect_base


def load_config(path: str | Path) -> ServerConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config root must be an object")

    port = int(data.get("port") or 2096)
    if port <= 0 or port > 65535:
        raise ValueError(f"invalid port: {port}")

    redirect_base = (
        (data.get("redirect_base") or data.get("redirect_domain") or "")
        .strip()
        .rstrip("/")
    )
    pasarguard_env = (
        (data.get("pasarguard_env") or "/opt/pasarguard/.env").strip()
        or "/opt/pasarguard/.env"
    )

    ssl_raw = data.get("ssl") or {}
    if not isinstance(ssl_raw, dict):
        ssl_raw = {}
    cert = (ssl_raw.get("cert") or "").strip()
    key = (ssl_raw.get("key") or "").strip()
    enabled = bool(ssl_raw.get("enabled")) and bool(cert and key)

    return ServerConfig(
        host=(data.get("host") or "0.0.0.0").strip() or "0.0.0.0",
        port=port,
        redirect_base=redirect_base,
        panel=str(data.get("panel") or ""),
        pasarguard_env=pasarguard_env,
        ssl=SslConfig(enabled=enabled, cert=cert, key=key),
    )


def build_config_dict(
    *,
    listen_port: int,
    redirect_base: str,
    panel: str = "x-ui",
    ssl_cert_pem: str = "",
    ssl_key_pem: str = "",
    host: str = "0.0.0.0",
    pasarguard_env: str = "/opt/pasarguard/.env",
) -> dict:
    ssl_enabled = bool(ssl_cert_pem and ssl_key_pem)
    base = (redirect_base or "").rstrip("/")
    return {
        "host": host,
        "port": int(listen_port),
        "redirect_base": base,
        "redirect_domain": base,
        "pasarguard_env": pasarguard_env or "/opt/pasarguard/.env",
        "panel": panel,
        "ssl": {
            "enabled": ssl_enabled,
            "cert": ssl_cert_pem if ssl_enabled else "",
            "key": ssl_key_pem if ssl_enabled else "",
        },
    }
