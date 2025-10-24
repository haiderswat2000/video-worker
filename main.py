from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
from yt_dlp import YoutubeDL
import os, uuid, asyncio, tempfile, shutil

app = FastAPI()
JOBS = {}

class JobIn(BaseModel):
    url: str

def _build_opts(tmp):
    return {
        "outtmpl": f"{tmp}/%(title).80s.%(ext)s",
        "quiet": True,
        "noplaylist": True,
        "max_filesize": 50 * 1024 * 1024,
        "format": "best[ext=mp4][acodec!=none][vcodec!=none]/best[acodec!=none]",
    }

async def _run_job(jid, url):
    JOBS[jid]["status"] = "running"
    tmp = tempfile.mkdtemp(prefix="ydl_")
    try:
        opts = _build_opts(tmp)
        def _dl():
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return ydl.prepare_filename(info)
        loop = asyncio.get_running_loop()
        path = await loop.run_in_executor(None, _dl)
        pub = os.path.join("/srv", f"{jid}.mp4")
        shutil.move(path, pub)
        base = os.getenv("PUBLIC_BASE_URL", "https://yourappname.onrender.com/files")
        JOBS[jid].update(status="done", file_url=f"{base}/{jid}.mp4")
    except Exception as e:
        JOBS[jid].update(status="error", error=str(e))

@app.post("/jobs")
async def new_job(j: JobIn, bg: BackgroundTasks):
    jid = uuid.uuid4().hex
    JOBS[jid] = {"status": "pending"}
    bg.add_task(_run_job, jid, j.url)
    return {"job_id": jid, "status": "pending"}

@app.get("/jobs/{jid}")
async def get_job(jid: str):
    if jid not in JOBS:
        raise HTTPException(404)
    return JOBS[jid]
