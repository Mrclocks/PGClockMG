"""E2E with real sample x-ui.db: normalize mapping + pg-redirect 301 (zero-touch path)."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DB = Path(r"c:\Users\hrtag\Downloads\x-ui (1).db")
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from app.services.migrators.xui import (  # noqa: E402
    normalize_subscription_mapping,
    read_xui_subscription_listen,
)
from app.services.redirect_ops import (  # noqa: E402
    build_runtime_config,
    bundled_pg_redirect_src,
)


def _extract_clients(db: Path) -> list[dict]:
    import sqlite3

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        out = []
        for inbound_id, settings_json in conn.execute("SELECT id, settings FROM inbounds"):
            data = json.loads(settings_json or "{}")
            for cl in data.get("clients") or []:
                sub = (cl.get("subId") or cl.get("subid") or "").strip()
                if sub:
                    out.append({
                        "email": cl.get("email"),
                        "subId": sub,
                        "inbound_id": inbound_id,
                    })
        return out
    finally:
        conn.close()


@pytest.mark.skipif(not SAMPLE_DB.is_file(), reason="sample x-ui.db not present")
def test_sample_db_listen_and_bundled_redirect():
    listen = read_xui_subscription_listen(SAMPLE_DB)
    assert listen["port"] == 2096
    assert listen["path"] == "sub"
    assert listen["ssl"] is False  # cert paths set but files not on this host
    assert listen["ssl_wanted"] is True  # https://IP:2096/sub/... was used
    assert "fullchain.pem" in (listen.get("cert_path") or "")
    assert bundled_pg_redirect_src() is not None

    clients = _extract_clients(SAMPLE_DB)
    assert len(clients) >= 3
    sub_ids = {c["subId"] for c in clients}
    assert "dkhsu9q5dowiu3l5" in sub_ids


@pytest.mark.skipif(not SAMPLE_DB.is_file(), reason="sample x-ui.db not present")
def test_sample_db_redirect_301_zero_touch():
    """Upstream-style ?name= mapping + client request without query → 301 to PG."""
    from pg_redirect.config import load_config
    from pg_redirect.mapping import load_path_index
    from pg_redirect.server import RedirectApp

    clients = _extract_clients(SAMPLE_DB)
    redirect_base = "http://203.0.113.10:8000"
    mappings = {}
    for cl in clients:
        sub = cl["subId"]
        mappings[cl["email"] or sub] = {
            # Official generator format (broken for path lookup until normalize)
            "old_subscription_url": f"/sub/{sub}?name={sub}",
            "new_subscription_url": f"/sub/token-{sub}",
        }

    with tempfile.TemporaryDirectory() as tmp:
        mapping_path = Path(tmp) / "subscription_url_mapping.json"
        mapping_path.write_text(
            json.dumps({"panel": "x-ui", "mappings": mappings}, indent=2),
            encoding="utf-8",
        )
        normalize_subscription_mapping(mapping_path)
        data = json.loads(mapping_path.read_text(encoding="utf-8"))
        for entry in data["mappings"].values():
            assert "?" not in entry["old_subscription_url"]

        cfg = build_runtime_config(
            listen_port=2096,
            redirect_base=redirect_base,
            panel="x-ui",
        )
        assert cfg["port"] == 2096
        assert cfg["redirect_base"] == redirect_base
        assert cfg["ssl"]["enabled"] is False

        cfg_path = Path(tmp) / "config.json"
        # Point pasarguard_env at a missing file so Location uses redirect_base fallback
        cfg_local = {
            **cfg,
            "host": "127.0.0.1",
            "port": 0,
            "pasarguard_env": str(Path(tmp) / "missing.env"),
        }
        cfg_path.write_text(json.dumps(cfg_local), encoding="utf-8")

        index = load_path_index(mapping_path, redirect_base=redirect_base)
        app = RedirectApp(load_config(cfg_path), index)

        async def _run():
            server = await asyncio.start_server(app.handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]

            async def get(path: str):
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(
                    f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
                    .encode("ascii")
                )
                await writer.drain()
                raw = await reader.read(4096)
                writer.close()
                await writer.wait_closed()
                text = raw.decode("iso-8859-1", "replace")
                head, _, _ = text.partition("\r\n\r\n")
                code = int(head.split()[1])
                headers = {}
                for line in head.split("\r\n")[1:]:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
                return code, headers

            try:
                code, _ = await get("/healthz")
                assert code == 200
                for cl in clients:
                    sub = cl["subId"]
                    # Real clients hit path only (no ?name=)
                    code, headers = await get(f"/sub/{sub}")
                    assert code == 301, sub
                    assert headers["location"] == f"{redirect_base}/sub/token-{sub}"
                    # Even if something appends query, still works
                    code, headers = await get(f"/sub/{sub}?name={sub}")
                    assert code == 301, sub
                code, _ = await get("/sub/not-a-real-id")
                assert code == 404
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(_run())
