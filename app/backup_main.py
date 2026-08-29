"""PGClockMG Backup Panel — separate FastAPI app (own port + password)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import BACKUP_PORT, WEB_PORT
from app.services import backup_auth
from app.services.backup_engine import (
    apply_retention,
    delete_backup_file,
    get_backup_job,
    list_backup_files,
    live_panel_stats,
    resolve_backup_path,
    start_backup_async,
)
from app.services.backup_scheduler import start_scheduler, stop_scheduler
from app.services.backup_settings import (
    DEFAULT_TELEGRAM_CAPTION,
    load_settings,
    public_settings,
    update_settings,
)
from app.services.backup_stream import push_backup_file
from app.services.backup_telegram import (
    format_caption,
    human_size,
    send_backup_to_telegram,
    test_telegram_connection,
)
from app.services.prerequisites import get_system_status, is_pasarguard_installed

APP_VERSION = "3.3.0"


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    backup_auth.get_session_secret()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="PGClockMG Backup", version=APP_VERSION, lifespan=_lifespan)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

PUBLIC_PATHS = frozenset({
    "/login",
    "/api/setup/status",
    "/api/setup/password",
    "/api/login",
    "/favicon.ico",
})


class PasswordSetup(BaseModel):
    password: str = Field(min_length=12, max_length=200)
    password_confirm: str = Field(min_length=12, max_length=200)


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class SettingsPatch(BaseModel):
    retention_count: int | None = None
    schedule: dict | None = None
    telegram: dict | None = None
    stream: dict | None = None


class TelegramTestBody(BaseModel):
    send_sample: bool = True


class StreamSendBody(BaseModel):
    backup_id: str
    dest_url: str
    token: str


def _unauthorized() -> JSONResponse:
    return JSONResponse({"detail": "Unauthorized"}, status_code=401)


@app.middleware("http")
async def require_backup_session(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)

    # First-run setup page is public HTML via /login
    if not backup_auth.password_is_set():
        if path.startswith("/api/") and path not in ("/api/setup/status", "/api/setup/password"):
            return JSONResponse({"detail": "setup_required"}, status_code=403)
        if not path.startswith("/api/"):
            return await call_next(request)

    cookie = request.cookies.get(backup_auth.COOKIE_NAME)
    if backup_auth.password_is_set() and not backup_auth.session_cookie_valid(cookie):
        if path.startswith("/api/"):
            return _unauthorized()
        return HTMLResponse(_login_redirect_hint(), status_code=401)

    return await call_next(request)


def _login_redirect_hint() -> str:
    return """<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="0;url=/login">
<title>PGClockMG Backup</title></head>
<body style="background:#0f1218;color:#e6e9ef;font-family:system-ui;display:flex;align-items:center;justify-content:center;min-height:100vh">
Redirecting to login…
</body></html>"""


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    path = STATIC_DIR / "backup.html"
    return FileResponse(path)


@app.get("/")
async def index():
    if not backup_auth.password_is_set():
        return FileResponse(STATIC_DIR / "backup.html")
    return FileResponse(STATIC_DIR / "backup.html")


@app.get("/api/setup/status")
async def setup_status():
    return {
        "password_set": backup_auth.password_is_set(),
        "backup_port": BACKUP_PORT,
        "wizard_port": WEB_PORT,
        "version": APP_VERSION,
        "min_password_length": backup_auth.MIN_PASSWORD_LEN,
    }


@app.post("/api/setup/password")
async def setup_password(body: PasswordSetup):
    if backup_auth.password_is_set():
        raise HTTPException(400, "password_already_set")
    if body.password != body.password_confirm:
        raise HTTPException(400, "password_mismatch")
    try:
        backup_auth.set_password(body.password)
    except backup_auth.PasswordPolicyError as exc:
        raise HTTPException(400, f"weak_password:{exc}") from exc
    cookie = backup_auth.create_session_cookie()
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        backup_auth.COOKIE_NAME,
        cookie,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=backup_auth.COOKIE_MAX_AGE,
    )
    return resp


@app.post("/api/login")
async def login(body: LoginBody):
    if not backup_auth.password_is_set():
        raise HTTPException(400, "setup_required")
    if not backup_auth.check_password(body.password):
        raise HTTPException(401, "invalid_password")
    cookie = backup_auth.create_session_cookie()
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        backup_auth.COOKIE_NAME,
        cookie,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=backup_auth.COOKIE_MAX_AGE,
    )
    return resp


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(backup_auth.COOKIE_NAME, path="/")
    return resp


@app.get("/api/dashboard")
async def dashboard():
    from app.config import BACKUP_PORT, WEB_PORT
    from app.services.pg_access import get_panel_access_info

    system = get_system_status()
    stats = live_panel_stats()
    cfg = load_settings()
    backups = list_backup_files()
    total_bytes = sum(int(b.get("size_bytes") or 0) for b in backups)
    resources = (system.get("resources") or {})
    storage = (resources.get("storage") or {})
    backup_disk = storage.get("backup") or {}
    memory = resources.get("memory") or {}
    tg = cfg.get("telegram") or {}
    sched = cfg.get("schedule") or {}
    access = {}
    try:
        access = get_panel_access_info() or {}
    except Exception:
        access = {}

    return {
        "version": APP_VERSION,
        "pasarguard_installed": is_pasarguard_installed(),
        "ports": {"wizard": WEB_PORT, "backup": BACKUP_PORT},
        "system": {
            "pasarguard": system.get("pasarguard"),
            "pasarguard_db": system.get("pasarguard_db"),
            "pasarguard_env": system.get("pasarguard_env"),
            "docker": system.get("docker"),
            "root": system.get("root"),
            "resources": resources,
        },
        "panel_access": {
            "url": access.get("url") or access.get("login_url") or access.get("dashboard_url"),
            "ssl": access.get("ssl"),
            "port": access.get("port"),
            "db_type": access.get("db_type") or system.get("pasarguard_db"),
        },
        "live_stats": stats,
        "last_backup": cfg.get("last_backup"),
        "last_error": cfg.get("last_error"),
        "backup_count": len(backups),
        "backup_total_bytes": total_bytes,
        "retention_count": cfg.get("retention_count") or 10,
        "schedule": sched,
        "telegram": {
            "enabled": bool(tg.get("enabled")),
            "configured": bool((tg.get("bot_token") or "").strip() and (tg.get("chat_id") or "").strip()),
            "proxy_enabled": bool(tg.get("proxy_enabled")),
        },
        "stream_dest": ((cfg.get("stream") or {}).get("default_dest_url") or ""),
        "health": {
            "backup_disk_free_bytes": backup_disk.get("free_bytes"),
            "backup_disk_total_bytes": backup_disk.get("total_bytes"),
            "memory_available_bytes": memory.get("available_bytes"),
            "memory_total_bytes": memory.get("total_bytes"),
            "cpu_count": resources.get("cpu_count"),
            "load_ratio_1m": resources.get("load_ratio_1m"),
            "profile": resources.get("profile"),
        },
    }


@app.get("/api/backups")
async def api_list_backups():
    return {"items": list_backup_files()}


@app.post("/api/backups/create")
async def api_create_backup():
    job = start_backup_async(trigger="manual")
    return job


@app.get("/api/backups/jobs/{job_id}")
async def api_backup_job(job_id: str):
    job = get_backup_job(job_id)
    if not job:
        raise HTTPException(404, "job_not_found")
    return job


@app.get("/api/backups/{backup_id}/download")
async def api_download_backup(backup_id: str):
    path = resolve_backup_path(backup_id)
    if not path:
        raise HTTPException(404, "backup_not_found")
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/zip",
    )


@app.delete("/api/backups/{backup_id}")
async def api_delete_backup(backup_id: str):
    if not delete_backup_file(backup_id):
        raise HTTPException(404, "backup_not_found")
    return {"ok": True}


@app.post("/api/backups/{backup_id}/telegram")
async def api_send_telegram(backup_id: str):
    path = resolve_backup_path(backup_id)
    if not path:
        raise HTTPException(404, "backup_not_found")
    meta = None
    sidecar = path.with_suffix(path.suffix + ".json")
    if sidecar.is_file():
        import json
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:
            meta = None
    result = send_backup_to_telegram(path, manifest=meta)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "telegram_failed")
    return result


@app.post("/api/backups/stream/send")
async def api_stream_send(body: StreamSendBody):
    path = resolve_backup_path(body.backup_id)
    if not path:
        raise HTTPException(404, "backup_not_found")
    meta = None
    sidecar = path.with_suffix(path.suffix + ".json")
    sha = None
    if sidecar.is_file():
        import json
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
            sha = (meta or {}).get("sha256")
        except Exception:
            pass
    result = push_backup_file(
        path,
        dest_base_url=body.dest_url.strip(),
        token=body.token.strip(),
        sha256=sha,
    )
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "stream_failed")
    # remember last dest
    update_settings({"stream": {"default_dest_url": body.dest_url.strip()}})
    return result


@app.get("/api/settings")
async def api_get_settings():
    return public_settings()


@app.put("/api/settings")
async def api_put_settings(body: SettingsPatch):
    patch: dict = {}
    if body.retention_count is not None:
        patch["retention_count"] = max(1, min(100, int(body.retention_count)))
    if body.schedule is not None:
        patch["schedule"] = body.schedule
    if body.stream is not None:
        patch["stream"] = body.stream
    if body.telegram is not None:
        current = load_settings().get("telegram") or {}
        tg = dict(body.telegram)
        # keep previous secrets when blank
        if not (tg.get("bot_token") or "").strip():
            tg["bot_token"] = current.get("bot_token") or ""
        if tg.get("proxy_password") in (None, ""):
            if tg.pop("proxy_password_clear", None):
                tg["proxy_password"] = ""
            else:
                tg["proxy_password"] = current.get("proxy_password") or ""
        if "caption_template" in tg and not (tg.get("caption_template") or "").strip():
            tg["caption_template"] = DEFAULT_TELEGRAM_CAPTION
        patch["telegram"] = tg
    saved = update_settings(patch)
    if "retention_count" in patch:
        apply_retention(patch["retention_count"])
    return public_settings(saved)


@app.post("/api/telegram/test")
async def api_telegram_test(body: TelegramTestBody | None = None):
    result = test_telegram_connection()
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "telegram_test_failed")
    return result


@app.post("/api/telegram/preview")
async def api_telegram_preview(request: Request):
    data = await request.json()
    template = data.get("caption_template") or DEFAULT_TELEGRAM_CAPTION
    sample = {
        "date": "2026-01-15T03:00:00Z",
        "size": human_size(125 * 1024 * 1024),
        "db_type": "timescaledb",
        "users": 1200,
        "nodes": 8,
        "status": "ok",
        "filename": "pgclockmg-timescaledb-sample.zip",
        "parts": "3 part(s)",
    }
    return {"text": format_caption(template, sample)}


@app.post("/api/password/change")
async def api_change_password(body: PasswordSetup):
    if body.password != body.password_confirm:
        raise HTTPException(400, "password_mismatch")
    try:
        backup_auth.set_password(body.password)
    except backup_auth.PasswordPolicyError as exc:
        raise HTTPException(400, f"weak_password:{exc}") from exc
    cookie = backup_auth.create_session_cookie()
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        backup_auth.COOKIE_NAME,
        cookie,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=backup_auth.COOKIE_MAX_AGE,
    )
    return resp
