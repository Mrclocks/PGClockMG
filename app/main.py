"""PG-Migrator FastAPI application."""

import socket
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException, Request, WebSocket, WebSocketDisconnect, Form
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from app.models import BackupCleanupRequest, MigrationRequest, PasarguardRestoreRequest
from app.panels import (
    PANELS, DATABASE_TYPES, SUBSCRIPTION_LABELS, PASARGUARD_INSTALL_DBS,
    PASARGUARD_INSTALL_COMMANDS, DOCS_INSTALL_URL, DOCS_NODE_URL, PANEL_GITHUB_URL,
    OWNER_TEMP_KEY_CMD, SSH_TUNNEL_CMD, can_convert_databases,
)
from app.services.prerequisites import check_prerequisites, get_recommended_target_dbs, get_system_status
from app.services.orchestrator import start_migration, get_job, MigrationAlreadyRunning
from app.services.validation import validate_migration
from app.services.upload import save_upload, get_upload_path, get_upload_analysis
from app.services.upload_bundle import (
    init_bundle, save_bundle_slot, get_bundle_status, prepare_bundle_workspace, bundle_has_upload,
)
from app.services.upload_requirements import get_upload_requirements
from app.services.archive_guard import (
    MAX_UPLOAD_BYTES, MAX_OVERRIDE_UPLOAD_BYTES, MAX_ZIP_ENTRY_BYTES, MAX_ZIP_FILES, MAX_ZIP_RATIO,
    MAX_ZIP_TOTAL_BYTES, allowed_upload_bytes, safe_upload_name,
)
from app.services.pg_access import get_panel_access_info
from app.services.pg_restore import (
    analyze_pasarguard_backup, start_pasarguard_restore, get_restore_job,
)
from app.services.self_uninstall import uninstall_preview, schedule_self_uninstall
from app.services.auth import COOKIE_NAME, COOKIE_MAX_AGE, ensure_token, token_matches
from app.config import WEB_PORT

APP_VERSION = "3.3.0"


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Create .access_token on boot so install/login recovery always has a file.
    ensure_token()
    yield


app = FastAPI(title="PGClockMG", version=APP_VERSION, lifespan=_lifespan)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

PUBLIC_PATHS = frozenset({"/login", "/favicon.ico"})


def _is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    # One-time stream receive tokens authenticate the request themselves.
    if path.startswith("/api/stream/receive/"):
        return True
    return False

_LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PGClockMG</title>
<style>
 body{background:#0f1420;color:#e6e9ef;font-family:system-ui,-apple-system,Segoe UI,sans-serif;
      display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
 .card{background:#182031;padding:32px;border-radius:14px;width:min(92vw,420px);
       box-shadow:0 18px 40px rgba(0,0,0,.45)}
 h1{margin:0 0 6px;font-size:1.25rem}
 p{margin:0 0 18px;color:#95a0b5;font-size:.9rem;line-height:1.6}
 input{width:100%%;padding:11px 12px;border-radius:8px;border:1px solid #2b3650;
       background:#0f1420;color:#e6e9ef;font-size:1rem;box-sizing:border-box}
 button{width:100%%;margin-top:12px;padding:11px;border:0;border-radius:8px;
        background:#3d7dff;color:#fff;font-size:1rem;cursor:pointer}
 code{background:#0f1420;padding:2px 6px;border-radius:5px;font-size:.82rem}
 .err{color:#ff8080;font-size:.85rem;margin-bottom:12px}
</style></head><body>
<form class="card" method="get" action="/login">
 <h1>PGClockMG</h1>
 %(error)s
 <p>Open the URL printed at the end of install<br>
    (<code>http://IP:PORT/?token=...</code>).<br><br>
    Or paste the token here. Recovery on the server:<br>
    <code>cat %(token_file)s</code></p>
 <input name="token" type="password" autofocus autocomplete="off" placeholder="access token">
 <button type="submit">Open wizard</button>
</form></body></html>
"""


def _login_page(error: bool = False) -> str:
    from app.services.auth import token_path

    ensure_token()
    return _LOGIN_PAGE % {
        "error": '<div class="err">Invalid token.</div>' if error else "",
        "token_file": token_path(),
    }


@app.middleware("http")
async def require_access_token(request: Request, call_next):
    path = request.url.path
    if _is_public_path(path):
        return await call_next(request)

    from_query = request.query_params.get("token")
    supplied = (
        request.cookies.get(COOKIE_NAME)
        or request.headers.get("X-Auth-Token")
        or from_query
    )
    if not token_matches(supplied):
        if path.startswith("/api/") or path.startswith("/ws/"):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return HTMLResponse(_login_page(), status_code=401)

    response = await call_next(request)
    if from_query and token_matches(from_query):
        response.set_cookie(
            COOKIE_NAME, from_query, httponly=True, samesite="lax",
            path="/", max_age=COOKIE_MAX_AGE,
        )
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/login")
async def login(token: str = ""):
    if not token_matches(token):
        return HTMLResponse(_login_page(error=bool(token)), status_code=401)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        COOKIE_NAME, token, httponly=True, samesite="lax",
        path="/", max_age=COOKIE_MAX_AGE,
    )
    return response


def _server_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@app.get("/api/info")
async def api_info():
    return {
        "version": APP_VERSION,
        "server_ip": _server_ip(),
        "web_port": WEB_PORT,
        "panels": [p.model_dump() for p in PANELS.values()],
        "database_types": DATABASE_TYPES,
        "pasarguard_install_dbs": PASARGUARD_INSTALL_DBS,
        "pasarguard_install_guide": {
            "docs_url": DOCS_INSTALL_URL,
            "github_url": PANEL_GITHUB_URL,
            "node_url": DOCS_NODE_URL,
            "owner_temp_key_cmd": OWNER_TEMP_KEY_CMD,
            "ssh_tunnel_cmd": SSH_TUNNEL_CMD,
            "commands": PASARGUARD_INSTALL_COMMANDS,
        },
        "subscription_labels": SUBSCRIPTION_LABELS,
        "system": get_system_status(),
        "panel_access": get_panel_access_info(),
        "upload_limits": {
            "max_upload_bytes": MAX_UPLOAD_BYTES,
            "max_override_upload_bytes": MAX_OVERRIDE_UPLOAD_BYTES,
            "max_zip_entry_bytes": MAX_ZIP_ENTRY_BYTES,
            "max_zip_total_bytes": MAX_ZIP_TOTAL_BYTES,
            "max_zip_files": MAX_ZIP_FILES,
            "max_zip_ratio": MAX_ZIP_RATIO,
        },
        "convert_rules": {
            "sqlite_to_any": True,
            "non_sqlite_to_sqlite": False,
            "cross_engine": True,
        },
    }


@app.get("/api/pasarguard/status")
async def api_pasarguard_status():
    return get_panel_access_info()


@app.get("/api/convert-check")
async def api_convert_check(source_db: str, target_db: str):
    ok = can_convert_databases(source_db, target_db)
    return {
        "ok": ok,
        "source_db": source_db,
        "target_db": target_db,
        "reason": None if ok else (
            "Cannot convert non-SQLite databases to SQLite. Install PasarGuard with the target engine, then restore."
            if target_db == "sqlite" and source_db != "sqlite"
            else "Unsupported conversion"
        ),
    }


@app.get("/api/pasarguard/restore/analyze/{upload_id}")
async def api_pasarguard_restore_analyze(upload_id: str):
    try:
        return analyze_pasarguard_backup(upload_id=upload_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/pasarguard/cleanup/analyze/{upload_id}")
async def api_backup_cleanup_analyze(upload_id: str):
    """What a cleanup would remove from this backup. Never blocks the restore."""
    from app.services.backup_cleanup import analyze_cleanup, cleanup_enabled
    from app.services.upload import get_upload_path

    if not cleanup_enabled():
        return {"available": False, "reason": "disabled", "rules": [], "default_rule_ids": []}

    path = get_upload_path(upload_id)
    if not path:
        raise HTTPException(404, "Upload not found")
    try:
        return analyze_cleanup(path)
    except Exception as e:
        # The wizard treats this as "no cleanup offered" and carries on.
        return {"available": False, "reason": str(e), "rules": [], "default_rule_ids": []}


@app.post("/api/pasarguard/cleanup")
async def api_backup_cleanup(req: BackupCleanupRequest):
    """Write a cleaned copy of an upload and return it as a new upload_id.

    On any problem the original upload_id comes back with applied=False, so the
    caller can always go straight to restore.
    """
    from app.services.backup_cleanup import clean_upload

    return await run_in_threadpool(clean_upload, req.upload_id, req.rule_ids)


@app.post("/api/pasarguard/restore")
async def api_pasarguard_restore(req: PasarguardRestoreRequest):
    if not req.confirmed:
        raise HTTPException(400, "Confirmation required")
    try:
        job = await start_pasarguard_restore(req.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"job_id": job.job_id, "status": job.status}


def _log_window(job, since: int, limit: int) -> dict:
    """Logs from `since` onward, capped at `limit`, plus offsets for the next poll."""
    total = len(job.logs)
    start = max(0, total - limit) if since < 0 else min(since, total)
    if total - start > limit:
        start = total - limit
    return {
        "logs": job.logs[start:],
        "log_start": start,
        "log_total": total,
    }


@app.get("/api/pasarguard/restore/{job_id}")
async def api_pasarguard_restore_status(job_id: str, since: int = -1):
    job = get_restore_job(job_id)
    if not job:
        raise HTTPException(404, "Restore job not found")
    return {
        "job_id": job.job_id,
        "status": job.status,
        "progress": job.progress,
        "message": job.message,
        **_log_window(job, since, 2000),
        "result": job.result,
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/api/panels")
async def api_panels():
    return [p.model_dump() for p in PANELS.values()]


@app.get("/api/system-check")
async def api_system_check():
    return get_system_status()


@app.get("/api/prerequisites/{panel_id}")
async def api_prerequisites(
    panel_id: str,
    marzban_mode: str | None = None,
    upload_id: str | None = None,
    upload_bundle_id: str | None = None,
):
    if panel_id not in PANELS:
        raise HTTPException(404, "پنل یافت نشد")
    return check_prerequisites(
        panel_id, marzban_mode=marzban_mode, upload_id=upload_id, upload_bundle_id=upload_bundle_id,
    )


@app.get("/api/upload/{upload_id}/analysis")
async def api_upload_analysis(upload_id: str):
    from app.services.upload import get_upload_analysis
    analysis = get_upload_analysis(upload_id)
    if not analysis:
        raise HTTPException(404, "Upload not found")
    return analysis


@app.get("/api/recommendations/{panel_id}/{source_db}")
async def api_recommendations(panel_id: str, source_db: str):
    return get_recommended_target_dbs(panel_id, source_db)


@app.get("/api/upload-requirements")
async def api_upload_requirements(
    panel_id: str,
    source_db: str | None = None,
    marzban_mode: str | None = None,
):
    if panel_id not in PANELS:
        raise HTTPException(404, "پنل یافت نشد")
    return get_upload_requirements(panel_id, source_db, marzban_mode)


@app.get("/api/upload-bundle/{bundle_id}")
async def api_upload_bundle(bundle_id: str):
    status = get_bundle_status(bundle_id)
    if status is None:
        raise HTTPException(404, "Bundle not found")
    return status


@app.post("/api/upload")
async def api_upload(
    file: UploadFile = File(...),
    bundle_id: str | None = Form(None),
    slot: str | None = Form(None),
    panel_id: str | None = Form(None),
    source_db: str | None = Form(None),
    marzban_mode: str | None = Form(None),
    allow_large_upload: str | None = Form(None),
):
    filename = safe_upload_name(file.filename)
    tmp_dir = Path(tempfile.mkdtemp(prefix="pg-upload-"))
    tmp_path = tmp_dir / filename
    size = 0
    use_large_upload_limit = str(allow_large_upload or "").strip().lower() in ("1", "true", "yes", "on")
    max_upload_bytes = allowed_upload_bytes(use_large_upload_limit)
    try:
        with open(tmp_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_upload_bytes:
                    limit_mb = max_upload_bytes // (1024 * 1024)
                    raise HTTPException(400, f"حداکثر حجم فایل {limit_mb} مگابایت است")
                out.write(chunk)

        if slot or bundle_id:
            bid = bundle_id or init_bundle()
            result = save_bundle_slot(
                bid, slot or "bundle_zip", tmp_path, filename,
                panel_id=panel_id, source_db=source_db, marzban_mode=marzban_mode,
            )
            if result.get("error"):
                raise HTTPException(400, result["error"])
            return result

        result = save_upload(tmp_path, filename)
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result
    finally:
        await file.close()
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            tmp_dir.rmdir()
        except Exception:
            pass


def _resolve_upload_params(params: dict) -> dict:
    if params.get("upload_bundle_id"):
        bid = params["upload_bundle_id"]
        if bundle_has_upload(bid):
            work = prepare_bundle_workspace(bid)
            params["upload_work_dir"] = str(work)
            params["upload_path"] = str(work)
            status = get_bundle_status(bid)
            if status:
                params["upload_analysis"] = status.get("analysis")
    elif params.get("upload_id"):
        path = get_upload_path(params["upload_id"])
        if path:
            params["upload_path"] = path
            analysis = get_upload_analysis(params["upload_id"])
            if analysis:
                params["upload_analysis"] = analysis
    return params


@app.post("/api/validate-migration")
async def api_validate_migration(req: MigrationRequest):
    params = req.model_dump()
    params = _resolve_upload_params(params)
    return validate_migration(params)


@app.post("/api/migrate")
async def api_migrate(req: MigrationRequest):
    params = req.model_dump()
    params = _resolve_upload_params(params)

    validation = validate_migration(params)
    if not validation["ok"]:
        raise HTTPException(400, {"errors": validation["errors"]})

    try:
        job = await start_migration(params)
    except MigrationAlreadyRunning as e:
        raise HTTPException(
            409,
            {
                "en": str(e),
                "fa": (
                    f"یک مهاجرت در حال اجرا است (job={e.job.job_id}، "
                    f"{e.job.progress}٪). تا پایان صبر کنید؛ کلیک دوباره نکنید."
                ),
                "ru": str(e),
                "job_id": e.job.job_id,
                "progress": e.job.progress,
            },
        )
    return {"job_id": job.job_id, "status": job.status}


@app.get("/api/migrate/{job_id}")
async def api_migrate_status(job_id: str, since: int = -1):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job یافت نشد")
    return {
        "job_id": job.job_id,
        "status": job.status,
        "progress": job.progress,
        "message": job.message,
        **_log_window(job, since, 2000),
        "result": job.result,
    }


@app.websocket("/ws/migrate/{job_id}")
async def ws_migrate(websocket: WebSocket, job_id: str):
    if not token_matches(
        websocket.cookies.get(COOKIE_NAME) or websocket.query_params.get("token")
    ):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    job = get_job(job_id)
    if not job:
        await websocket.close(code=4004)
        return

    import asyncio

    sent = 0
    try:
        while True:
            while sent < len(job.logs):
                await websocket.send_json({"type": "log", "message": job.logs[sent]})
                sent += 1
            await websocket.send_json({
                "type": "status",
                "status": job.status,
                "progress": job.progress,
                "message": job.message,
                "result": job.result,
            })
            if job.status in ("success", "error"):
                await websocket.send_json({
                    "type": "done",
                    "status": job.status,
                    "result": job.result,
                })
                break
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass


@app.post("/api/stream/listen")
async def api_stream_listen():
    """Destination: prepare a one-time token so a source can push a backup zip."""
    from app.services.backup_stream import create_listener
    info = create_listener(label="wizard")
    return {
        "token": info["token"],
        "expires_in_sec": int(info["expires_at"] - info["created_at"]),
        "receive_path": f"/api/stream/receive/{info['token']}",
        "hint": {
            "en": "On the source backup panel: Stream → paste this server URL and token.",
            "fa": "روی پنل بکاپ مبدأ: استریم → آدرس این سرور و توکن را وارد کنید.",
            "ru": "На панели бэкапа источника: Stream → URL этого сервера и токен.",
        },
    }


@app.get("/api/stream/status/{token}")
async def api_stream_status(token: str):
    from app.services.backup_stream import get_listener
    info = get_listener(token)
    if not info:
        raise HTTPException(404, "listener_not_found")
    return {
        "status": info.get("status"),
        "bytes_received": info.get("bytes_received") or 0,
        "upload_id": info.get("upload_id"),
        "filename": info.get("filename"),
        "error": info.get("error"),
        "sha256": info.get("sha256"),
    }


@app.put("/api/stream/receive/{token}")
@app.post("/api/stream/receive/{token}")
async def api_stream_receive(token: str, request: Request):
    """Source pushes the zip body here. Auth = one-time listener token only."""
    from app.services.backup_stream import receive_stream

    filename = request.headers.get("X-Backup-Filename")
    sha = request.headers.get("X-Backup-Sha256")
    size_raw = request.headers.get("X-Backup-Size")
    expected_size = int(size_raw) if size_raw and size_raw.isdigit() else None
    try:
        result = await receive_stream(
            token,
            request.stream(),
            filename=filename,
            expected_sha256=sha,
            expected_size=expected_size,
        )
        return result
    except FileNotFoundError:
        raise HTTPException(404, "listener_not_found")
    except TimeoutError:
        raise HTTPException(410, "listener_expired")
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/self-uninstall")
async def api_self_uninstall_preview():
    return uninstall_preview()


@app.post("/api/self-uninstall")
async def api_self_uninstall():
    """Remove PGClockMG service and /opt/pg-migrator after a short delay."""
    return await schedule_self_uninstall(delay_sec=2.0)
