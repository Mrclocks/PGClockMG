"""PG-Migrator FastAPI application."""

import socket
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException, WebSocket, WebSocketDisconnect, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse

from app.models import MigrationRequest
from app.panels import PANELS, DATABASE_TYPES, SUBSCRIPTION_LABELS, PASARGUARD_INSTALL_DBS
from app.services.prerequisites import check_prerequisites, get_recommended_target_dbs, get_system_status
from app.services.orchestrator import start_migration, get_job
from app.services.validation import validate_migration
from app.services.upload import save_upload, get_upload_path, get_upload_analysis
from app.services.upload_bundle import (
    init_bundle, save_bundle_slot, get_bundle_status, prepare_bundle_workspace, bundle_has_upload,
)
from app.services.upload_requirements import get_upload_requirements
from app.config import WEB_PORT

app = FastAPI(title="PG-Migrator", version="1.0.0")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


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
        "version": "1.7.1",
        "server_ip": _server_ip(),
        "web_port": WEB_PORT,
        "panels": [p.model_dump() for p in PANELS.values()],
        "database_types": DATABASE_TYPES,
        "pasarguard_install_dbs": PASARGUARD_INSTALL_DBS,
        "subscription_labels": SUBSCRIPTION_LABELS,
        "system": get_system_status(),
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
):
    content = await file.read()
    if len(content) > 500 * 1024 * 1024:
        raise HTTPException(400, "حداکثر حجم فایل ۵۰۰ مگابایت")

    filename = file.filename or "upload.bin"

    if slot or bundle_id:
        bid = bundle_id or init_bundle()
        result = save_bundle_slot(
            bid, slot or "bundle_zip", content, filename,
            panel_id=panel_id, source_db=source_db, marzban_mode=marzban_mode,
        )
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    result = save_upload(content, filename)
    return result


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

    job = await start_migration(params)
    return {"job_id": job.job_id, "status": job.status}


@app.get("/api/migrate/{job_id}")
async def api_migrate_status(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job یافت نشد")
    return {
        "job_id": job.job_id,
        "status": job.status,
        "progress": job.progress,
        "message": job.message,
        "logs": job.logs[-100:],
        "result": job.result,
    }


@app.websocket("/ws/migrate/{job_id}")
async def ws_migrate(websocket: WebSocket, job_id: str):
    await websocket.accept()
    job = get_job(job_id)
    if not job:
        await websocket.close(code=4004)
        return

    # Send existing logs
    for log in job.logs:
        await websocket.send_json({"type": "log", "message": log})

    def on_log(msg):
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(websocket.send_json({"type": "log", "message": msg}))
        except Exception:
            pass

    job.on_log(on_log)

    try:
        while True:
            import asyncio
            await asyncio.sleep(1)
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
    except WebSocketDisconnect:
        pass

