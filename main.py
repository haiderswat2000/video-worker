# -*- coding: utf-8 -*-
from __future__ import annotations
import uuid, time, asyncio, os
from pathlib import Path
from typing import Dict, Optional
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware

from config import APP_NAME, DEBUG, HOST, PORT, STORAGE_DIR, MAX_CONCURRENCY, CLEANUP_AFTER_DONE
from ytdl_helper import download as ydl_download

app = FastAPI(title=APP_NAME, debug=DEBUG)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], allow_credentials=True
)

# حالة الوظائف
class Job(BaseModel):
    id: str
    url: str
    status: str = "queued"      # queued | running | done | error
    file_path: Optional[str] = None
    error: Optional[str] = None
    created_at: float = time.time()
    done_at: Optional[float] = None

JOBS: Dict[str, Job] = {}
SEM = asyncio.Semaphore(MAX_CONCURRENCY)

async def _run_job(job_id: str):
    job = JOBS[job_id]
    job.status = "running"
    try:
        out_dir = str(STORAGE_DIR / job.id)
        os.makedirs(out_dir, exist_ok=True)
        async with SEM:
            file_path, _info = await asyncio.to_thread(ydl_download, job.url, out_dir)
        job.file_path = file_path
        job.status = "done"
        job.done_at = time.time()
    except Exception as e:
        job.status = "error"
        job.error = str(e)
        job.done_at = time.time()

@app.get("/healthz")
async def healthz():
    return {"ok": True, "active_jobs": sum(1 for j in JOBS.values() if j.status in ("queued","running"))}

@app.get("/download")
async def sync_download(url: str):
    # تنزيل متزامن سريع (للروابط السهلة)
    jid = str(uuid.uuid4().hex)
    JOBS[jid] = Job(id=jid, url=url, status="running")
    try:
        out_dir = str(STORAGE_DIR / jid)
        os.makedirs(out_dir, exist_ok=True)
        file_path, _ = await asyncio.to_thread(ydl_download, url, out_dir)
        JOBS[jid].status = "done"; JOBS[jid].file_path = file_path; JOBS[jid].done_at = time.time()
        return {"job_id": jid, "status": "done", "file": f"/files/{jid}"}
    except Exception as e:
        JOBS[jid].status = "error"; JOBS[jid].error = str(e); JOBS[jid].done_at = time.time()
        raise HTTPException(400, str(e))

class CreateJob(BaseModel):
    url: str

@app.post("/jobs")
async def create_job(req: CreateJob, bg: BackgroundTasks):
    jid = str(uuid.uuid4().hex)
    JOBS[jid] = Job(id=jid, url=req.url)
    bg.add_task(_run_job, jid)
    return {"job_id": jid, "status": JOBS[jid].status}

@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    data = job.dict()
    if job.status == "done":
        data["file"] = f"/files/{job.id}"
    return JSONResponse(data)

@app.get("/files/{job_id}")
async def get_file(job_id: str):
    job = JOBS.get(job_id)
    if not job or job.status != "done" or not job.file_path:
        raise HTTPException(404, "file not ready")
    return FileResponse(job.file_path, filename=Path(job.file_path).name, media_type="video/mp4")

# تنظيف تلقائي قديم
async def _janitor():
    while True:
        now = time.time()
        for j in list(JOBS.values()):
            if j.done_at and (now - j.done_at) > CLEANUP_AFTER_DONE:
                try:
                    p = Path(j.file_path or "")
                    if p.exists():
                        p.unlink(missing_ok=True)
                    d = Path(STORAGE_DIR / j.id)
                    if d.exists():
                        for x in d.iterdir():
                            x.unlink(missing_ok=True)
                        d.rmdir()
                except Exception:
                    pass
                JOBS.pop(j.id, None)
        await asyncio.sleep(60)

@app.on_event("startup")
async def _startup():
    asyncio.create_task(_janitor())

# تشغيل محلي (اختياري)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)
