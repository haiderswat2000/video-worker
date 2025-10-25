# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import time
import uuid
import asyncio
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, BackgroundTasks, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware

from config import (
    APP_NAME,
    DEBUG,
    HOST,
    PORT,
    STORAGE_DIR,
    MAX_CONCURRENCY,
    CLEANUP_AFTER_DONE,
)
from ytdl_helper import download as ydl_download, probe_info as ydl_probe

# =========================
# تطبيق FastAPI + CORS
# =========================
app = FastAPI(title=APP_NAME, debug=DEBUG)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# =========================
# نموذج المهمة والذاكرة المؤقتة
# =========================
class Job(BaseModel):
    id: str
    url: str
    status: str = "queued"         # queued | running | done | error
    file_path: Optional[str] = None
    error: Optional[str] = None
    created_at: float = time.time()
    done_at: Optional[float] = None

JOBS: Dict[str, Job] = {}
SEM = asyncio.Semaphore(MAX_CONCURRENCY)

# =========================
# صفحات مساعدة
# =========================
@app.get("/", response_class=PlainTextResponse)
async def root():
    return f"{APP_NAME} running ✓"

@app.get("/healthz")
async def healthz():
    active = sum(1 for j in JOBS.values() if j.status in ("queued", "running"))
    return {"ok": True, "active_jobs": active}

# =========================
# تنفيذ مهمة تنزيل داخل الـWorker
# =========================
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

# =========================
# تنزيل فوري (متزامن)
# =========================
@app.get("/download")
async def sync_download(url: str = Query(..., description="رابط الفيديو/الصوت")):
    jid = uuid.uuid4().hex
    JOBS[jid] = Job(id=jid, url=url, status="running")
    try:
        out_dir = str(STORAGE_DIR / jid)
        os.makedirs(out_dir, exist_ok=True)
        file_path, _ = await asyncio.to_thread(ydl_download, url, out_dir)
        JOBS[jid].status = "done"
        JOBS[jid].file_path = file_path
        JOBS[jid].done_at = time.time()
        return {"job_id": jid, "status": "done", "file": f"/files/{jid}"}
    except Exception as e:
        JOBS[jid].status = "error"
        JOBS[jid].error = str(e)
        JOBS[jid].done_at = time.time()
        raise HTTPException(400, str(e))

# =========================
# نظام المهام (غير متزامن)
# =========================
@app.post("/jobs")
async def create_job(payload: dict, bg: BackgroundTasks):
    url = payload.get("url")
    if not url:
        raise HTTPException(400, "url required")
    jid = uuid.uuid4().hex
    JOBS[jid] = Job(id=jid, url=url)
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

# =========================
# تسليم الملف النهائي
# =========================
@app.get("/files/{job_id}")
async def get_file(job_id: str):
    job = JOBS.get(job_id)
    if not job or job.status != "done" or not job.file_path:
        raise HTTPException(404, "file not ready")
    path = Path(job.file_path)
    if not path.exists():
        raise HTTPException(404, "file missing")
    return FileResponse(
        str(path),
        filename=path.name,
        media_type="video/mp4",   # مناسب أيضًا لـ webm/mp4؛ تيليجرام يقبله
    )

# =========================
# أدوات تشخيصية مفيدة
# =========================
@app.get("/cookies/debug")
def cookies_debug():
    """
    يفحص وجود ملف الكوكيز وعدد صفوفه وبعض المفاتيح الشائعة.
    يساعد على حل مشكلة 'Sign in to confirm you're not a bot'
    """
    candidates = [Path("./cookies/youtube.txt"), Path("./youtube.txt")]
    found = None
    for p in candidates:
        if p.exists():
            found = p
            break
    if not found:
        raise HTTPException(404, "cookies file not found")

    txt = found.read_text(encoding="utf-8", errors="ignore")
    names = []
    for line in txt.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            names.append(parts[5])

    has_sid = any(n in names for n in [
        "__Secure-1PSID", "__Secure-3PSID", "SID", "SAPISID", "APISID", "HSID", "SSID"
    ])

    return {
        "file": str(found),
        "rows": sum(1 for _ in names),
        "has_CONSENT": ("CONSENT" in names),
        "has_SID_like": has_sid,
    }

@app.get("/formats")
async def list_formats(url: str = Query(..., description="رابط للفحص فقط")):
    """
    يُرجع الـformats المتاحة بدون تنزيل (مفيد لتشخيص 'Requested format is not available')
    """
    try:
        # نبني نفس خيارات ytdl داخل ytdl_helper عبر probe_info
        info = await asyncio.to_thread(ydl_probe, url, {"dummy": True})
        fmts = info.get("formats") or []
        out = []
        for f in fmts:
            out.append({
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "height": f.get("height"),
                "tbr": f.get("tbr"),
                "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"),
                "proto": f.get("protocol"),
                "filesize": f.get("filesize") or f.get("filesize_approx"),
            })
        # ترتيب بسيط للأعلى جودة
        out.sort(key=lambda x: (x.get("height") or 0, x.get("tbr") or 0), reverse=True)
        return {"ok": True, "uploader": info.get("uploader"), "title": info.get("title"), "formats": out[:80]}
    except Exception as e:
        raise HTTPException(400, f"probe failed: {e}")

# =========================
# منظّف دوري يحذف الملفات بعد مدة
# =========================
async def _janitor():
    while True:
        now = time.time()
        for j in list(JOBS.values()):
            if j.done_at and (now - j.done_at) > CLEANUP_AFTER_DONE:
                p = Path(j.file_path or "")
                if p.exists():
                    with contextlib.suppress(Exception):
                        p.unlink()
                d = Path(STORAGE_DIR / j.id)
                if d.exists():
                    for x in d.iterdir():
                        with contextlib.suppress(Exception):
                            x.unlink()
                    with contextlib.suppress(Exception):
                        d.rmdir()
                JOBS.pop(j.id, None)
        await asyncio.sleep(60)

@app.on_event("startup")
async def _startup():
    # إنشاء مجلد التخزين إن لم يكن موجوداً
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    asyncio.create_task(_janitor())

# =========================
# تشغيل محلي (للاختبار)
# =========================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST or "0.0.0.0", port=int(os.environ.get("PORT", PORT)), reload=False)
