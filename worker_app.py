# worker_app.py
import os
import uuid
import base64
import asyncio
import tempfile
import contextlib
from typing import Dict, Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

app = FastAPI(title="Video Worker")

JOBS: Dict[str, Dict[str, Any]] = {}
FILES_DIR = os.path.abspath("./_worker_files")
os.makedirs(FILES_DIR, exist_ok=True)

# ===================== yt-dlp helper =====================
async def run_ytdlp(url: str, *, want_audio: bool, cookies_txt: str | None, job_id: str) -> Dict[str, Any]:
    out_basename = f"{job_id}.%(ext)s"
    out_template = os.path.join(FILES_DIR, out_basename)

    cookies_path = None
    if cookies_txt:
        cookies_path = os.path.join(FILES_DIR, f"{job_id}_cookies.txt")
        with open(cookies_path, "w", encoding="utf-8") as f:
            f.write(cookies_txt)

    cmd = [
        "yt-dlp",
        url,
        "-o", out_template,
        "--no-warnings",
        "--no-playlist",
        "--restrict-filenames",
        "--retries", "3",
        "--fragment-retries", "3",
        "--no-call-home",
    ]
    if cookies_path:
        cmd += ["--cookies", cookies_path]

    if want_audio:
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    else:
        cmd += ["-f", "bv*+ba/b", "--merge-output-format", "mp4"]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()

    if cookies_path:
        with contextlib.suppress(Exception):
            os.remove(cookies_path)

    if proc.returncode != 0:
        return {"ok": False, "error": (err.decode("utf-8", "ignore") or out.decode("utf-8", "ignore"))[:4000]}

    produced = None
    for name in os.listdir(FILES_DIR):
        if name.startswith(job_id + "."):
            produced = os.path.join(FILES_DIR, name)
            break

    if not produced or not os.path.isfile(produced):
        return {"ok": False, "error": "no output file produced"}

    return {"ok": True, "path": produced}


# ===================== API endpoints =====================
@app.post("/jobs")
async def create_job(payload: Dict[str, Any]):
    url = (payload.get("url") or "").strip()
    want_audio = bool(payload.get("audio", False))
    cookies_b64 = payload.get("cookies_b64")

    if not url:
        raise HTTPException(400, "url required")

    cookies_txt = None
    if cookies_b64:
        try:
            cookies_txt = base64.b64decode(cookies_b64).decode("utf-8")
        except Exception:
            raise HTTPException(400, "invalid cookies_b64")

    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "queued"}

    async def runner():
        JOBS[job_id]["status"] = "running"
        res = await run_ytdlp(url, want_audio=want_audio, cookies_txt=cookies_txt, job_id=job_id)
        if not res.get("ok"):
            JOBS[job_id] = {"status": "error", "error": res.get("error", "unknown")}
            return
        path = res["path"]
        JOBS[job_id] = {
            "status": "done",
            "filename": os.path.basename(path),
            "download_url": f"/files/{job_id}",
        }

    asyncio.create_task(runner())
    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "job not found")
    return j


@app.get("/files/{job_id}")
async def fetch_file(job_id: str):
    j = JOBS.get(job_id)
    if not j or j.get("status") != "done":
        raise HTTPException(404, "file not ready")

    filename = j.get("filename")
    path = os.path.join(FILES_DIR, filename)
    if not os.path.isfile(path):
        raise HTTPException(404, "file missing")

    return FileResponse(path, filename=filename)


@app.get("/")
async def home():
    return {"status": "ok", "endpoints": ["/jobs", "/jobs/{id}", "/files/{id}"]}
