# main.py
# -*- coding: utf-8 -*-

import os
import uuid
import asyncio
import glob
import time
from typing import Dict, Any, Optional

from fastapi import FastAPI, BackgroundTasks, HTTPException, Query
from fastapi.responses import JSONResponse, FileResponse

app = FastAPI(title="Video Worker", version="1.2")

# ==== مسارات ثابتة (بدون متغيرات) ====
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FILES_DIR = os.path.join(BASE_DIR, "files")
os.makedirs(FILES_DIR, exist_ok=True)

# ملف كوكيز محلي داخل المستودع لدعم يوتيوب
COOKIES_FILE = os.path.join(BASE_DIR, "cookies.txt")
HAS_COOKIES = os.path.isfile(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0

# روابط عامة (اختياري) — إن لم توفّر، سنعيد روابط نسبية
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")

# إدارة مهام بسيطة داخل الذاكرة
# job_id -> dict(status, file_path?, file_url?, error?, created_at)
JOBS: Dict[str, Dict[str, Any]] = {}

# إعدادات تنظيف دوري
JOB_TTL_SECONDS = 60 * 60  # ساعة
CLEAN_INTERVAL = 15 * 60   # كل 15 دقيقة


def _now() -> float:
    return time.time()


def _job_url_for(job_id: str, ext: str) -> str:
    rel = f"/files/{job_id}.{ext}"
    return f"{PUBLIC_BASE_URL}{rel}" if PUBLIC_BASE_URL else rel


async def _exec(cmd: list) -> tuple[int, str]:
    """
    تشغيل أمر async وجمع stderr (بدون إغراق اللوغز).
    نعيد (returncode, stderr_tail).
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE
    )
    _, err = await proc.communicate()
    tail = (err or b"")[-2000:].decode("utf-8", "ignore")
    return proc.returncode, tail


def _pick_output(job_id: str) -> Optional[str]:
    """
    yt-dlp قد يخرج mp4/webm/mkv/m4a… نبحث عن أي امتداد ونرجعه.
    نعطي أولوية mp4 إن وُجد.
    """
    base = os.path.join(FILES_DIR, job_id)
    prefs = ("mp4", "mkv", "webm", "m4a", "mp3")
    for ext in prefs:
        p = f"{base}.{ext}"
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
    # إن لم نجد من اللائحة المفضلة، نلتقط أي ملف يبدأ بـ job_id.
    matches = glob.glob(f"{base}.*")
    for p in matches:
        try:
            if os.path.getsize(p) > 0:
                return p
        except Exception:
            continue
    return None


async def _download_video(job_id: str, url: str):
    """
    تنزيل فعلي إلى ./files/{job_id}.<ext>
    نضبط yt-dlp لاستخدام cookies.txt (إن وجد) + player_client للأندرويد ليوتيوب.
    """
    # نستخدم قالب اسم الإخراج فوق مجلد files
    outtmpl = os.path.join(FILES_DIR, f"{job_id}.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-progress",
        "--no-warnings",
        "-o", outtmpl,
        # اختيارات جودة جيدة/متوافقة
        "-S", "codec:h264,res,ext",           # فضّل h264 ثم الدقة ثم الامتداد
        "-f", "bv*+ba/best",                  # فيديو+صوت أو أفضل متاح
        "--retries", "3",
        "--fragment-retries", "10",
        "--socket-timeout", "30",
        "--extractor-args", "youtube:player_client=android",
        url,
    ]
    if HAS_COOKIES:
        cmd[0:0] = []  # لا شيء — للوضوح فقط
        cmd.extend(["--cookies", COOKIES_FILE])

    rc, err_tail = await _exec(cmd)

    if rc != 0:
        JOBS[job_id].update(status="error", error=(err_tail or "yt-dlp failed"))
        return

    # التقط الملف الناتج الحقيقي
    out_path = _pick_output(job_id)
    if not out_path:
        JOBS[job_id].update(status="error", error="file not created")
        return

    ext = os.path.splitext(out_path)[1].lstrip(".").lower()
    JOBS[job_id].update(
        status="done",
        file_path=out_path,
        file_url=_job_url_for(job_id, ext),
    )


async def _cleaner_loop():
    """حذف المهام/الملفات الأقدم من JOB_TTL_SECONDS كل CLEAN_INTERVAL."""
    while True:
        try:
            cutoff = _now() - JOB_TTL_SECONDS
            to_delete = []
            for jid, info in list(JOBS.items()):
                if info.get("created_at", 0) < cutoff:
                    to_delete.append(jid)
            for jid in to_delete:
                p = JOBS[jid].get("file_path")
                if p and os.path.exists(p):
                    with contextlib.suppress(Exception):
                        os.remove(p)
                JOBS.pop(jid, None)
        except Exception:
            pass
        await asyncio.sleep(CLEAN_INTERVAL)


@app.on_event("startup")
async def _on_start():
    # شغّل منظِّف الخلفية
    asyncio.create_task(_cleaner_loop())


@app.get("/")
async def home():
    return JSONResponse({"ok": True, "message": "Worker running ✅"})


@app.post("/jobs")
async def new_job(data: Dict[str, Any], bg: BackgroundTasks):
    url = (data or {}).get("url")
    if not url:
        raise HTTPException(status_code=400, detail="url required")

    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "pending", "created_at": _now()}
    bg.add_task(_download_video, job_id, url)
    return {"job_id": job_id, "status": "pending"}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    # لا نعرض المسار الداخلي في الـAPI العام
    public = {k: v for k, v in job.items() if k != "file_path"}
    return public


@app.get("/download")
async def direct_download(url: str = Query(..., description="Media URL")):
    """
    تنزيل متزامن (يُفيد زبونك عندما يحاول أولاً /download ثم يسقط إلى /jobs).
    """
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "running", "created_at": _now()}
    await _download_video(job_id, url)
    job = JOBS[job_id]
    if job.get("status") != "done":
        raise HTTPException(status_code=500, detail=job.get("error") or "download failed")
    return {"file_url": job["file_url"]}


@app.get("/files/{name}")
async def get_file_by_name(name: str):
    """
    مرونة: نسمح بطلب /files/xxxx.mp4 أو /files/xxxx.webm
    """
    path = os.path.join(FILES_DIR, name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="file not found")
    media = "application/octet-stream"
    ext = os.path.splitext(name)[1].lower()
    if ext in (".mp4", ".m4v"):
        media = "video/mp4"
    elif ext in (".webm",):
        media = "video/webm"
    elif ext in (".mkv",):
        media = "video/x-matroska"
    elif ext in (".m4a", ".mp3"):
        media = "audio/mpeg"
    return FileResponse(path, filename=name, media_type=media)


# تَوافق مع مسارك السابق: /files/{job_id}.mp4
@app.get("/files/{job_id}.mp4")
async def get_mp4(job_id: str):
    # إن لم يكن mp4 موجودًا، حاول أي امتداد ثم أعده كتحميل
    p = os.path.join(FILES_DIR, f"{job_id}.mp4")
    if os.path.isfile(p):
        return FileResponse(p, filename=f"{job_id}.mp4", media_type="video/mp4")

    alt = _pick_output(job_id)
    if not alt:
        raise HTTPException(status_code=404, detail="file not ready")
    name = os.path.basename(alt)
    return await get_file_by_name(name)
