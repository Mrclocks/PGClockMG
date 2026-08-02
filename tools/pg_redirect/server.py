"""Async HTTP(S) redirect server — stdlib only."""

from __future__ import annotations

import asyncio
import logging
import signal
import ssl
import tempfile
from pathlib import Path

from .config import ServerConfig, load_config
from .mapping import load_path_index

log = logging.getLogger("pg_redirect")


def _http_response(status: int, reason: str, headers: dict[str, str], body: bytes = b"") -> bytes:
    lines = [f"HTTP/1.1 {status} {reason}"]
    hdrs = dict(headers)
    hdrs.setdefault("Content-Length", str(len(body)))
    hdrs.setdefault("Connection", "close")
    hdrs.setdefault("Server", "pg-redirect")
    for k, v in hdrs.items():
        lines.append(f"{k}: {v}")
    lines.append("")
    lines.append("")
    return ("\r\n".join(lines)).encode("utf-8") + body


async def _read_request(reader: asyncio.StreamReader, limit: int = 65536) -> tuple[str, str]:
    """Return (method, path) from the first request line; path excludes query."""
    data = await reader.read(limit)
    if not data:
        return "", ""
    try:
        text = data.decode("iso-8859-1", errors="replace")
    except Exception:
        return "", ""
    first = text.split("\r\n", 1)[0]
    parts = first.split()
    if len(parts) < 2:
        return "", ""
    method, target = parts[0], parts[1]
    # Strip query; also accept absolute-form targets (http://host/path)
    path = target.split("?", 1)[0] or "/"
    if path.startswith("http://") or path.startswith("https://"):
        # authority + path — keep only the path portion
        rest = path.split("://", 1)[1]
        slash = rest.find("/")
        path = rest[slash:] if slash >= 0 else "/"
    if not path.startswith("/"):
        path = "/" + path
    return method.upper(), path


class RedirectApp:
    def __init__(self, config: ServerConfig, path_index: dict[str, str]):
        self.config = config
        self.path_index = path_index

    def lookup(self, path: str) -> str | None:
        if path in self.path_index:
            return self.path_index[path]
        if path != "/" and path.endswith("/"):
            return self.path_index.get(path.rstrip("/"))
        return self.path_index.get(path + "/")

    async def handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        try:
            method, path = await _read_request(reader)
            if not method:
                writer.close()
                await writer.wait_closed()
                return

            if path in ("/healthz", "/health", "/ping"):
                body = b"ok\n"
                resp = _http_response(
                    200, "OK",
                    {"Content-Type": "text/plain; charset=utf-8"},
                    body,
                )
                writer.write(resp)
                await writer.drain()
                return

            if method not in ("GET", "HEAD", "OPTIONS"):
                resp = _http_response(
                    405, "Method Not Allowed",
                    {"Content-Type": "text/plain; charset=utf-8", "Allow": "GET, HEAD"},
                    b"method not allowed\n",
                )
                writer.write(resp)
                await writer.drain()
                return

            target = self.lookup(path)
            if not target:
                log.info("404 %s %s peer=%s", method, path, peer)
                resp = _http_response(
                    404, "Not Found",
                    {"Content-Type": "text/plain; charset=utf-8"},
                    b"not found\n",
                )
                writer.write(resp)
                await writer.drain()
                return

            log.info("301 %s %s -> %s peer=%s", method, path, target, peer)
            resp = _http_response(
                301, "Moved Permanently",
                {
                    "Location": target,
                    "Content-Type": "text/plain; charset=utf-8",
                },
                b"",
            )
            writer.write(resp)
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        except Exception:
            log.exception("handler error peer=%s", peer)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


def _make_ssl_context(config: ServerConfig) -> ssl.SSLContext | None:
    ssl_cfg = config.ssl
    if not ssl_cfg or not ssl_cfg.enabled:
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    # cert/key may be PEM text embedded in config
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".crt") as cf:
        cf.write(ssl_cfg.cert)
        cert_path = cf.name
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".key") as kf:
        kf.write(ssl_cfg.key)
        key_path = kf.name
    try:
        ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    finally:
        Path(cert_path).unlink(missing_ok=True)
        Path(key_path).unlink(missing_ok=True)
    return ctx


async def serve(config: ServerConfig, path_index: dict[str, str]) -> None:
    app = RedirectApp(config, path_index)
    ssl_ctx = _make_ssl_context(config)
    server = await asyncio.start_server(
        app.handle,
        host=config.host,
        port=config.port,
        ssl=ssl_ctx,
    )
    socks = ", ".join(str(s.getsockname()) for s in server.sockets or [])
    mode = "HTTPS" if ssl_ctx else "HTTP"
    log.info(
        "pg-redirect listening %s on %s (%d mappings, base=%s)",
        mode, socks, len(path_index), config.redirect_base or "(none)",
    )

    stop = asyncio.Event()

    def _stop(*_args):
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            # Windows
            signal.signal(sig, lambda *_: _stop())

    async with server:
        await stop.wait()
        log.info("pg-redirect shutting down")
        server.close()
        await server.wait_closed()


def run_from_files(config_path: str | Path, map_path: str | Path) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config(config_path)
    index = load_path_index(map_path, redirect_base=config.redirect_base)
    if not index:
        log.warning("mapping index is empty — all subscription paths will 404")
    asyncio.run(serve(config, index))
