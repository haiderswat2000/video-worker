# main.py
import os, uuid, shutil, aiohttp, asyncio
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI(title="Video Worker", version="1.0")

JOBS = {}

async def _download_video(job_id: str, url: str):
    """تحميل الفيديو فعلياً"""
    tmp_dir = f"/tmp/{job_id}"
    os.makedirs(tmp_dir, exist_ok=True)
    file_path = os.path.join(tmp_dir, f"{job_id}.mp4")
    try:
        cmd = [
            "yt-dlp", "-f", "best[ext=mp4]/best", url,
            "-o", file_path, "--quiet"
        ]
        proc = await asyncio.create_subprocess_exec(*cmd)
        await proc.wait()

        if os.path.exists(file_path):
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["file_url"] = f"{os.getenv('PUBLIC_BASE_URL')}/{job_id}.mp4"
        else:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = "file not created"
    except Exception as e:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["error"] = str(e)

@app.post("/jobs")
async def new_job(data: dict, bg: BackgroundTasks):
    url = data.get("url")
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

@app.get("/")
async def home():
    return JSONResponse({"ok": True, "message": "Worker running ✅"})
