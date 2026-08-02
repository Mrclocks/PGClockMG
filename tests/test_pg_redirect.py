"""Tests for native stdlib pg-redirect service."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from pg_redirect.base_url import (  # noqa: E402
    clear_base_cache,
    resolve_from_env_text,
    resolve_live_base,
)
from pg_redirect.mapping import (  # noqa: E402
    build_redirect_url,
    load_path_index,
    normalize_mapping_file,
    path_only,
)
from pg_redirect.config import build_config_dict, load_config  # noqa: E402
from pg_redirect.server import RedirectApp  # noqa: E402


def test_path_only_strips_query_and_domain():
    assert path_only("/sub/abc?name=abc") == "/sub/abc"
    assert path_only("https://x.example:2096/sub/zz?name=zz") == "/sub/zz"
    assert path_only("sub/tok") == "/sub/tok"


def test_build_redirect_url():
    assert build_redirect_url("/sub/new", "https://1.2.3.4:8000") == "https://1.2.3.4:8000/sub/new"
    # Absolute new URLs are rewritten onto live base (path only) so host changes apply
    assert build_redirect_url(
        "https://other/sub/x", "https://1.2.3.4:8000"
    ) == "https://1.2.3.4:8000/sub/x"
    assert build_redirect_url("https://other/sub/x", "") == "https://other/sub/x"


def test_load_path_index_keeps_relative_targets():
    with tempfile.TemporaryDirectory() as tmp:
        mapping = Path(tmp) / "m.json"
        mapping.write_text(
            json.dumps({
                "mappings": {
                    "u1": {
                        "old_subscription_url": "/sub/oldid?name=oldid",
                        "new_subscription_url": "/sub/newtoken",
                    }
                }
            }),
            encoding="utf-8",
        )
        normalize_mapping_file(mapping, redirect_base="http://10.0.0.1:8000")
        index = load_path_index(mapping)
        assert index["/sub/oldid"] == "/sub/newtoken"


def test_resolve_from_env_prefers_domain_prefix():
    clear_base_cache()
    env = (
        'UVICORN_PORT=8000\n'
        'UVICORN_SSL_CERTFILE=/var/lib/pasarguard/certs/domain.com/fullchain.pem\n'
        'UVICORN_SSL_KEYFILE=/var/lib/pasarguard/certs/domain.com/privkey.pem\n'
        'SUBSCRIPTION_URL_PREFIX="https://domain.com:8000"\n'
    )
    assert resolve_from_env_text(env) == "https://domain.com:8000"


def test_resolve_from_env_cert_domain_when_no_prefix():
    clear_base_cache()
    env = (
        "UVICORN_PORT=8000\n"
        "UVICORN_SSL_CERTFILE=/var/lib/pasarguard/certs/panel.example.com/fullchain.pem\n"
        "UVICORN_SSL_KEYFILE=/var/lib/pasarguard/certs/panel.example.com/privkey.pem\n"
    )
    assert resolve_from_env_text(env) == "https://panel.example.com:8000"


def test_live_base_tracks_env_file_change():
    clear_base_cache()
    with tempfile.TemporaryDirectory() as tmp:
        env = Path(tmp) / ".env"
        env.write_text(
            'SUBSCRIPTION_URL_PREFIX="https://old.example:8000"\nUVICORN_PORT=8000\n',
            encoding="utf-8",
        )
        base1 = resolve_live_base(env_path=env, fallback="http://10.0.0.1:8000", ttl_sec=0)
        assert base1 == "https://old.example:8000"
        env.write_text(
            'SUBSCRIPTION_URL_PREFIX="https://new.example:8000"\nUVICORN_PORT=8000\n',
            encoding="utf-8",
        )
        base2 = resolve_live_base(env_path=env, fallback="http://10.0.0.1:8000", ttl_sec=0)
        assert base2 == "https://new.example:8000"


def test_config_load_accepts_redirect_domain_alias():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "c.json"
        cfg.write_text(
            json.dumps({
                "host": "0.0.0.0",
                "port": 2096,
                "redirect_domain": "https://pg.example:8000",
                "pasarguard_env": "/opt/pasarguard/.env",
                "ssl": {"enabled": False, "cert": "", "key": ""},
            }),
            encoding="utf-8",
        )
        loaded = load_config(cfg)
        assert loaded.redirect_base == "https://pg.example:8000"
        assert loaded.pasarguard_env == "/opt/pasarguard/.env"
        assert loaded.port == 2096


def test_build_config_dict_ssl():
    cfg = build_config_dict(
        listen_port=2096,
        redirect_base="http://x",
        ssl_cert_pem="CERT",
        ssl_key_pem="KEY",
    )
    assert cfg["ssl"]["enabled"] is True
    assert cfg["port"] == 2096
    assert cfg["pasarguard_env"] == "/opt/pasarguard/.env"


def test_redirect_app_lookup_and_live_location():
    from pg_redirect.config import ServerConfig

    clear_base_cache()
    with tempfile.TemporaryDirectory() as tmp:
        env = Path(tmp) / ".env"
        env.write_text(
            'SUBSCRIPTION_URL_PREFIX="https://domain.com:8000"\n'
            "UVICORN_PORT=8000\n"
            "UVICORN_SSL_CERTFILE=/x\n"
            "UVICORN_SSL_KEYFILE=/y\n",
            encoding="utf-8",
        )
        app = RedirectApp(
            ServerConfig(
                redirect_base="http://10.0.0.5:8000",
                pasarguard_env=str(env),
            ),
            {"/sub/a": "/sub/b"},
        )
        assert app.lookup("/sub/a") == "/sub/b"
        assert app.resolve_location("/sub/b") == "https://domain.com:8000/sub/b"
        assert app.lookup("/sub/missing") is None


def test_http_server_301_and_healthz():
    async def _run():
        from pg_redirect.config import ServerConfig

        clear_base_cache()
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("", encoding="utf-8")  # force fallback to redirect_base
            index = {"/sub/old": "/sub/new"}
            cfg = ServerConfig(
                host="127.0.0.1",
                port=0,
                redirect_base="http://10.0.0.5:8000",
                pasarguard_env=str(env),
            )
            app = RedirectApp(cfg, index)

            server = await asyncio.start_server(app.handle, host="127.0.0.1", port=0)
            port = server.sockets[0].getsockname()[1]

            async def _http_get(path: str) -> tuple[int, dict[str, str]]:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(
                    f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
                    .encode("ascii")
                )
                await writer.drain()
                data = await reader.read(4096)
                writer.close()
                await writer.wait_closed()
                text = data.decode("iso-8859-1", "replace")
                head, _, _body = text.partition("\r\n\r\n")
                status_line = head.split("\r\n", 1)[0]
                code = int(status_line.split()[1])
                headers = {}
                for line in head.split("\r\n")[1:]:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
                return code, headers

            try:
                code, _ = await _http_get("/healthz")
                assert code == 200
                code, headers = await _http_get("/sub/old")
                assert code == 301
                assert headers.get("location") == "http://10.0.0.5:8000/sub/new"
                code, _ = await _http_get("/sub/nope")
                assert code == 404
            finally:
                server.close()
                await server.wait_closed()

    asyncio.run(_run())
