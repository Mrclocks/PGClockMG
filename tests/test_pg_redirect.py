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
    assert build_redirect_url(
        "https://other/sub/x", "https://1.2.3.4:8000"
    ) == "https://other/sub/x"


def test_load_path_index_from_pasarguard_style_mapping():
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
        assert index["/sub/oldid"] == "http://10.0.0.1:8000/sub/newtoken"


def test_config_load_accepts_redirect_domain_alias():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "c.json"
        cfg.write_text(
            json.dumps({
                "host": "0.0.0.0",
                "port": 2096,
                "redirect_domain": "https://pg.example:8000",
                "ssl": {"enabled": False, "cert": "", "key": ""},
            }),
            encoding="utf-8",
        )
        loaded = load_config(cfg)
        assert loaded.redirect_base == "https://pg.example:8000"
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


def test_redirect_app_lookup():
    from pg_redirect.config import ServerConfig

    app = RedirectApp(ServerConfig(), {"/sub/a": "http://pg/sub/b"})
    assert app.lookup("/sub/a") == "http://pg/sub/b"
    assert app.lookup("/sub/missing") is None


def test_http_server_301_and_healthz():
    async def _run():
        from pg_redirect.config import ServerConfig

        index = {"/sub/old": "http://10.0.0.5:8000/sub/new"}
        cfg = ServerConfig(host="127.0.0.1", port=0, redirect_base="http://10.0.0.5:8000")
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
