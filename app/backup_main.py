"""PGClockMG Backup Panel — separate FastAPI app (own port + password)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import BACKUP_DIR, BACKUP_PORT, WEB_PORT
from app.services import backup_auth
from app.services.backup_engine import (
    apply_retention,
    delete_backup_file,
    get_backup_job,
    list_backup_files,
    resolve_backup_path,
    start_backup_async,
)
from app.services.backup_scheduler import start_scheduler, stop_scheduler
from app.services.backup_settings import (
    DEFAULT_TELEGRAM_CAPTION,
    load_settings,
    normalize_destinations,
    normalize_integrity,
    normalize_notify,
    normalize_retention_count,
    normalize_retention_days,
    normalize_schedule,
    normalize_thread_id,
    public_settings,
    update_settings,
)
from app.services.backup_net import UnsafeDestinationError
from app.services.backup_stream import get_push_job, start_push_async
from app.services.backup_telegram import (
    probe_telegram_connection,
    send_backup_to_telegram,
    test_telegram_connection,
)
from app.services.prerequisites import get_system_status, is_pasarguard_installed

APP_VERSION = "4.3.3"


def _dashboard_update_info() -> dict:
    """Non-blocking update banner: serve cache immediately, refresh in background."""
    try:
        from app.services.backup_updater import peek_cached_update, schedule_background_update_check

        cached = peek_cached_update(APP_VERSION)
        if cached is not None:
            return cached
        schedule_background_update_check(APP_VERSION)
        return {
            "ok": True,
            "current": APP_VERSION,
            "available": False,
            "pending": True,
            "error": None,
        }
    except Exception as exc:
        return {
            "ok": False,
            "current": APP_VERSION,
            "available": False,
            "error": str(exc),
        }


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    backup_auth.get_session_secret()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(
    title="PGClockMG Backup",
    version=APP_VERSION,
    lifespan=_lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

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
    setup_token: str | None = None


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=12, max_length=200)
    password_confirm: str = Field(min_length=12, max_length=200)


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class SettingsPatch(BaseModel):
    retention_count: int | None = None
    retention_days: int | None = None
    schedule: dict | None = None
    telegram: dict | None = None
    notify: dict | None = None
    integrity: dict | None = None
    stream: dict | None = None


class TelegramTestBody(BaseModel):
    send_sample: bool = True


class StreamSendBody(BaseModel):
    backup_id: str
    dest_url: str
    token: str


def _unauthorized() -> JSONResponse:
    return JSONResponse({"detail": "Unauthorized"}, status_code=401)


def _set_session_cookie(resp: JSONResponse, cookie: str, request: Request) -> None:
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    secure = request.url.scheme == "https" or forwarded == "https"
    resp.set_cookie(
        backup_auth.COOKIE_NAME,
        cookie,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=backup_auth.COOKIE_MAX_AGE,
        secure=secure,
    )


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
        "setup_token_required": backup_auth.setup_token_is_required(),
    }


@app.post("/api/setup/password")
async def setup_password(body: PasswordSetup, request: Request):
    if backup_auth.password_is_set():
        raise HTTPException(400, "password_already_set")
    if body.password != body.password_confirm:
        raise HTTPException(400, "password_mismatch")
    # Validate first (do not burn the one-time token on password write failures).
    if not backup_auth.verify_setup_token(body.setup_token):
        raise HTTPException(403, "setup_token_invalid")
    backup_auth.clear_empty_password_file()
    try:
        backup_auth.set_password(body.password, exclusive=True)
    except backup_auth.PasswordPolicyError as exc:
        raise HTTPException(400, f"weak_password:{exc}") from exc
    except OSError as exc:
        raise HTTPException(500, f"password_write_failed:{exc}") from exc
    # Only consume after the password is safely stored.
    backup_auth.consume_setup_token(body.setup_token)
    cookie = backup_auth.create_session_cookie()
    resp = JSONResponse({"ok": True})
    _set_session_cookie(resp, cookie, request)
    return resp


@app.post("/api/login")
async def login(body: LoginBody, request: Request):
    if not backup_auth.password_is_set():
        raise HTTPException(400, "setup_required")
    client_key = (request.client.host if request.client else None) or "unknown"
    if backup_auth.login_is_throttled(client_key):
        raise HTTPException(429, "too_many_attempts")
    if not backup_auth.check_password(body.password):
        backup_auth.record_login_failure(client_key)
        raise HTTPException(401, "invalid_password")
    backup_auth.clear_login_failures(client_key)
    cookie = backup_auth.create_session_cookie()
    resp = JSONResponse({"ok": True})
    _set_session_cookie(resp, cookie, request)
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
    from app.services.backup_engine import (
        _merge_counts,
        resolve_backup_manifest,
        resolve_backup_path,
    )
    from app.services.backup_settings import enrich_schedule

    system = get_system_status()
    cfg = load_settings()
    backups = list_backup_files()
    total_bytes = sum(int(b.get("size_bytes") or 0) for b in backups)
    resources = (system.get("resources") or {})
    storage = (resources.get("storage") or {})
    backup_disk = storage.get("backup") or {}
    memory = resources.get("memory") or {}
    tg = cfg.get("telegram") or {}
    sched = enrich_schedule(cfg.get("schedule") or {})
    access = {}
    try:
        access = get_panel_access_info() or {}
    except Exception:
        access = {}

    last = cfg.get("last_backup")
    if isinstance(last, dict) and last.get("backup_id"):
        path = resolve_backup_path(str(last.get("backup_id")))
        if path and path.is_file():
            meta = resolve_backup_manifest(path)
            last = {
                **last,
                "db_type": last.get("db_type") or meta.get("db_type"),
                "counts": _merge_counts(last.get("counts") or {}, meta.get("counts") or {}),
            }

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
            "url": access.get("login_url") or access.get("panel_url") or access.get("public_url"),
            "login_url": access.get("login_url") or access.get("panel_url"),
            "public_url": access.get("public_url"),
            "public_http_url": access.get("public_http_url"),
            "localhost_url": access.get("localhost_url"),
            "ssl": access.get("ssl"),
            "port": access.get("port"),
            "root_path": access.get("root_path") or "/",
            "host": access.get("host"),
            "domain": access.get("domain"),
            "ip": access.get("ip"),
            "db_type": access.get("db_type") or system.get("pasarguard_db"),
        },
        "update": _dashboard_update_info(),
        "last_backup": last,
        "last_error": cfg.get("last_error"),
        "backup_count": len(backups),
        "backup_total_bytes": total_bytes,
        "retention_count": cfg.get("retention_count") or 10,
        "retention_days": int(cfg.get("retention_days") or 0),
        "schedule": sched,
        "telegram": {
            "enabled": bool(tg.get("enabled")),
            "configured": bool(
                (tg.get("bot_token") or "").strip()
                and (
                    (tg.get("chat_id") or tg.get("admin_id") or "").strip()
                    or (tg.get("destinations") or [])
                )
            ),
            "proxy_enabled": bool(tg.get("proxy_enabled")),
            "destinations_count": len(tg.get("destinations") or []) + (
                1 if (tg.get("chat_id") or tg.get("admin_id") or "").strip() else 0
            ),
        },
        "notify": {
            "webhook_enabled": bool((cfg.get("notify") or {}).get("webhook_enabled")),
        },
        "integrity": {
            "verify_after_create": bool((cfg.get("integrity") or {}).get("verify_after_create", True)),
        },
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


@app.get("/api/session")
async def api_session():
    """Lightweight auth probe used during boot (avoids a full dashboard load)."""
    return {"ok": True, "version": APP_VERSION}


@app.get("/api/backups")
async def api_list_backups():
    return {"items": list_backup_files(), "backups_path": str(BACKUP_DIR)}


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
    try:
        # Validate destination early (same checks as push) before starting the job
        from app.services.backup_net import normalize_public_http_url
        normalize_public_http_url(body.dest_url.strip())
    except UnsafeDestinationError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not (body.token or "").strip():
        raise HTTPException(400, "token_missing")
    update_settings({"stream": {"default_dest_url": body.dest_url.strip()}})
    return start_push_async(
        path,
        dest_base_url=body.dest_url.strip(),
        token=body.token.strip(),
        sha256=sha,
    )


@app.get("/api/backups/stream/jobs/{job_id}")
async def api_stream_job(job_id: str):
    job = get_push_job(job_id)
    if not job:
        raise HTTPException(404, "job_not_found")
    return job


@app.get("/api/settings")
async def api_get_settings():
    return public_settings()


@app.put("/api/settings")
async def api_put_settings(body: SettingsPatch):
    patch: dict = {}
    if body.retention_count is not None:
        patch["retention_count"] = normalize_retention_count(body.retention_count)
    if body.retention_days is not None:
        patch["retention_days"] = normalize_retention_days(body.retention_days)
    if body.schedule is not None:
        patch["schedule"] = normalize_schedule(body.schedule)
    if body.notify is not None:
        notify = normalize_notify(body.notify)
        if notify.get("webhook_enabled") and notify.get("webhook_url"):
            try:
                from app.services.backup_net import normalize_public_http_url

                notify["webhook_url"] = normalize_public_http_url(notify["webhook_url"])
            except UnsafeDestinationError as exc:
                raise HTTPException(400, f"webhook_url_unsafe:{exc}") from exc
        patch["notify"] = notify
    if body.integrity is not None:
        patch["integrity"] = normalize_integrity(body.integrity)
    if body.telegram is not None:
        current = load_settings().get("telegram") or {}
        tg = dict(body.telegram)
        # UI/API alias: admin_id → chat_id
        admin_id = (tg.pop("admin_id", None) or "").strip()
        if admin_id and not (tg.get("chat_id") or "").strip():
            tg["chat_id"] = admin_id
        tg["message_thread_id"] = normalize_thread_id(tg.get("message_thread_id"))
        # Normalize destinations (extras); primary stays on chat_id / message_thread_id.
        primary = (tg.get("chat_id") or "").strip()
        extras = tg.get("destinations") if "destinations" in tg else current.get("destinations")
        tg["destinations"] = [
            d for d in normalize_destinations(extras or [])
            if not (primary and d.get("chat_id") == primary and d.get("message_thread_id") == tg.get("message_thread_id"))
        ]
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
    if "retention_count" in patch or "retention_days" in patch:
        apply_retention(
            keep_count=int(saved.get("retention_count") or 10),
            keep_days=int(saved.get("retention_days") or 0),
        )
    return public_settings(saved)


@app.get("/api/telegram/status")
async def api_telegram_status():
    cfg = load_settings()
    from app.services.backup_telegram import resolve_destinations, telegram_config

    tg = telegram_config(cfg)
    enabled = bool(tg.get("enabled"))
    configured = bool((tg.get("bot_token") or "").strip() and resolve_destinations(tg))
    if not enabled or not configured:
        return {
            "ok": False,
            "connected": False,
            "enabled": enabled,
            "configured": configured,
            "error": "disabled" if not enabled else "not_configured",
        }
    result = probe_telegram_connection(cfg)
    return {
        "ok": bool(result.get("ok")),
        "connected": bool(result.get("connected")),
        "enabled": enabled,
        "configured": configured,
        "bot": result.get("bot"),
        "error": result.get("error"),
    }


@app.post("/api/telegram/test")
async def api_telegram_test(body: TelegramTestBody | None = None):
    """Connect Telegram, create a real backup, and send the zip document to Admin ID."""
    import asyncio

    result = await asyncio.to_thread(test_telegram_connection)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "telegram_test_failed")
    result["connected"] = True
    return result


@app.delete("/api/dashboard/last-error")
async def api_clear_last_error():
    update_settings({"last_error": None})
    return {"ok": True}


@app.get("/api/update/status")
async def api_update_status(force: bool = False):
    from app.services.backup_updater import check_for_update, get_update_job

    info = check_for_update(current=APP_VERSION, force=force)
    job = get_update_job()
    return {**info, "job": job}


@app.post("/api/update/apply")
async def api_update_apply():
    import asyncio
    from app.services.backup_updater import apply_update, get_update_job

    # Do not block the event loop on GitHub I/O — apply_update returns a running
    # job immediately and does network/file work in a background thread.
    existing = get_update_job()
    if existing and existing.get("status") in ("running", "queued"):
        return existing
    job = await asyncio.to_thread(apply_update, current=APP_VERSION)
    return job


@app.get("/api/update/job")
async def api_update_job():
    from app.services.backup_updater import get_update_job

    job = get_update_job()
    if not job:
        raise HTTPException(404, "no_update_job")
    return job


@app.post("/api/password/change")
async def api_change_password(body: PasswordChange, request: Request):
    if body.password != body.password_confirm:
        raise HTTPException(400, "password_mismatch")
    if not backup_auth.check_password(body.current_password):
        raise HTTPException(401, "invalid_password")
    try:
        backup_auth.set_password(body.password)
    except backup_auth.PasswordPolicyError as exc:
        raise HTTPException(400, f"weak_password:{exc}") from exc
    backup_auth.rotate_session_secret()
    cookie = backup_auth.create_session_cookie()
    resp = JSONResponse({"ok": True})
    _set_session_cookie(resp, cookie, request)
    return resp
