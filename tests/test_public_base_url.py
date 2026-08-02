"""Domain-vs-IP public base URL resolution for PasarGuard links."""

from __future__ import annotations

from app.services.pg_access import resolve_pasarguard_public_base


def test_resolve_prefers_subscription_url_prefix_domain():
    env = (
        'SUBSCRIPTION_URL_PREFIX="https://domain.com:8000"\n'
        "UVICORN_PORT=8000\n"
        "UVICORN_SSL_CERTFILE=/var/lib/pasarguard/certs/domain.com/fullchain.pem\n"
        "UVICORN_SSL_KEYFILE=/var/lib/pasarguard/certs/domain.com/privkey.pem\n"
    )
    assert resolve_pasarguard_public_base(env) == "https://domain.com:8000"


def test_resolve_uses_cert_folder_domain_without_prefix():
    env = (
        "UVICORN_PORT=8000\n"
        "UVICORN_SSL_CERTFILE=/var/lib/pasarguard/certs/panel.example.com/fullchain.pem\n"
        "UVICORN_SSL_KEYFILE=/var/lib/pasarguard/certs/panel.example.com/privkey.pem\n"
    )
    assert resolve_pasarguard_public_base(env) == "https://panel.example.com:8000"


def test_resolve_keeps_ip_prefix_when_only_ip():
    env = (
        'SUBSCRIPTION_URL_PREFIX="https://203.0.113.10:8000"\n'
        "UVICORN_PORT=8000\n"
    )
    assert resolve_pasarguard_public_base(env) == "https://203.0.113.10:8000"


def test_resolve_allowed_origins_domain():
    env = (
        'ALLOWED_ORIGINS="https://cdn.example.com, https://panel.example.com:8000"\n'
        "UVICORN_PORT=8000\n"
        "UVICORN_SSL_CERTFILE=/x\n"
        "UVICORN_SSL_KEYFILE=/y\n"
    )
    assert resolve_pasarguard_public_base(env) == "https://cdn.example.com:8000"
