# main.py
import os, uuid, asyncio
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse, FileResponse

app = FastAPI(title="Video Worker", version="1.0")

JOBS = {}  # job_id -> {status, file_path?, file_url?, error?}

async def _download_video(job_id: str, url: str):
    """تحميل الفيديو فعلياً إلى /tmp/{job_id}.mp4"""
    tmp_dir = f"/tmp/{job_id}"
    os.makedirs(tmp_dir, exist_ok=True)
    file_path = os.path.join(tmp_dir, f"{job_id}.mp4")
    try:
        cmd = [
            "yt-dlp", "-f", "best[ext=mp4]/best",
            "-o", file_path,
            "--quiet", url
        ]
        proc = await asyncio.create_subprocess_exec(*cmd)
        await proc.wait()

        if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
            file_url = f"{base}/files/{job_id}.mp4" if base else f"/files/{job_id}.mp4"
            JOBS[job_id].update(status="done", file_path=file_path, file_url=file_url)
        else:
            JOBS[job_id].update(status="error", error="file not created")
    except Exception as e:
        JOBS[job_id].update(status="error", error=str(e))

@app.post("/jobs")
async def new_job(data: dict, bg: BackgroundTasks):
    url = (data or {}).get("url")
    if not url:
        raise HTTPException(status_code=400, detail="url required")
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "pending"}
    bg.add_task(_download_video, job_id, url)
    return {"job_id": job_id, "status": "pending"}

@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="job not found")
    return JOBS[job_id]

@app.get("/files/{name}.mp4")
async def get_file(name: str):
    job_id = name
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done" or not job.get("file_path"):
        raise HTTPException(status_code=404, detail="file not ready")
    return FileResponse(
        job["file_path"],
        filename=f"{job_id}.mp4",
        media_type="video/mp4"
    )

@app.get("/")
async def home():
    return JSONResponse({"ok": True, "message": "Worker running ✅"})
