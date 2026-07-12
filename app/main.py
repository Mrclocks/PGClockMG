"""PG-Migrator FastAPI application."""

import socket
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse

from app.models import MigrationRequest
from app.panels import PANELS, DATABASE_TYPES, SUBSCRIPTION_LABELS
from app.services.prerequisites import check_prerequisites, get_recommended_target_dbs
from app.services.orchestrator import start_migration, get_job
from app.services.upload import save_upload, get_upload_path
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


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/info")
async def api_info():
    return {
        "version": "1.2.0",
        "server_ip": _server_ip(),
        "web_port": WEB_PORT,
        "panels": [p.model_dump() for p in PANELS.values()],
        "database_types": DATABASE_TYPES,
        "subscription_labels": SUBSCRIPTION_LABELS,
    }


@app.get("/api/panels")
async def api_panels():
    return [p.model_dump() for p in PANELS.values()]


@app.get("/api/prerequisites/{panel_id}")
async def api_prerequisites(panel_id: str):
    if panel_id not in PANELS:
        raise HTTPException(404, "پنل یافت نشد")
    return check_prerequisites(panel_id)


@app.get("/api/recommendations/{panel_id}/{source_db}")
async def api_recommendations(panel_id: str, source_db: str):
    return get_recommended_target_dbs(panel_id, source_db)


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    content = await file.read()
    if len(content) > 500 * 1024 * 1024:
        raise HTTPException(400, "حداکثر حجم فایل ۵۰۰ مگابایت")
    result = save_upload(content, file.filename or "upload.bin")
    return result


@app.post("/api/migrate")
async def api_migrate(req: MigrationRequest):
    params = req.model_dump()
    if req.upload_id:
        path = get_upload_path(req.upload_id)
        if path:
            params["upload_path"] = path

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


@app.post("/api/install-pasarguard")
async def api_install_pasarguard(database: str = "sqlite"):
    import asyncio
    import subprocess

    db_flags = {
        "sqlite": "",
        "mysql": "--database mysql",
        "mariadb": "--database mariadb",
        "postgresql": "--database postgresql",
        "timescaledb": "--database timescaledb",
    }
    flag = db_flags.get(database, "")

    proc = await asyncio.create_subprocess_shell(
        f'bash -c \'curl -fsSL https://github.com/PasarGuard/scripts/raw/main/pasarguard.sh | bash -s -- @ install {flag}\'',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    return {
        "ok": proc.returncode == 0,
        "output": stdout.decode("utf-8", errors="replace")[-3000:],
    }
