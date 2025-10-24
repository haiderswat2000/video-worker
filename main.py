# main.py
# -*- coding: utf-8 -*-

import os
import uuid
import asyncio
import glob
import time
import contextlib
from typing import Dict, Any, Optional, Tuple

from fastapi import FastAPI, BackgroundTasks, HTTPException, Query
from fastapi.responses import JSONResponse, FileResponse

app = FastAPI(title="Video Worker", version="1.5")

# ===== مسارات ثابتة =====
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FILES_DIR = os.path.join(BASE_DIR, "files")
os.makedirs(FILES_DIR, exist_ok=True)

# cookies.txt (Netscape) بجانب main.py
COOKIES_FILE = os.path.join(BASE_DIR, "cookies.txt")
HAS_COOKIES = os.path.isfile(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0
if HAS_COOKIES:
    print(f"[worker] cookies.txt detected at: {COOKIES_FILE}")
else:
    print("[worker] cookies.txt not found or empty — YouTube may require sign-in")

# رابط عام اختياري لإرجاع روابط مطلقة
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")

# إدارة مهام داخل الذاكرة
JOBS: Dict[str, Dict[str, Any]] = {}

# تنظيف
JOB_TTL_SECONDS = 60 * 60   # 1h
CLEAN_INTERVAL = 15 * 60    # 15m

# رؤوس افتراضية
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
ACCEPT_LANG = "en-US,en;q=0.9,ar;q=0.8"


def _now() -> float:
    return time.time()


def _job_url_for(job_id: str, ext: str) -> str:
    rel = f"/files/{job_id}.{ext}"
    return f"{PUBLIC_BASE_URL}{rel}" if PUBLIC_BASE_URL else rel


async def _exec(cmd: list) -> Tuple[int, str]:
    """تشغيل أمر async وإرجاع (rc, tail_of_stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, err = await proc.communicate()
    tail = (err or b"")[-4000:].decode("utf-8", "ignore")
    return proc.returncode, tail


def _pick_output(job_id: str) -> Optional[str]:
    """اختيار أول ملف ناتج صالح، مع تفضيل mp4."""
    base = os.path.join(FILES_DIR, job_id)
    prefs = ("mp4", "mkv", "webm", "m4a", "mp3")
    for ext in prefs:
        p = f"{base}.{ext}"
        if os.path.exists(p):
            try:
                if os.path.getsize(p) > 0:
                    return p
            except Exception:
                pass
    for p in glob.glob(f"{base}.*"):
        try:
            if os.path.getsize(p) > 0:
                return p
        except Exception:
            continue
    return None


def _base_ydl_cmd(outtmpl: str, url: str, player_clients: str) -> list:
    """أوامر yt-dlp المشتركة + اختيار عميل يوتيوب."""
    cmd = [
        "yt-dlp",
        "--no-progress", "--no-warnings",
        "-o", outtmpl,

        # الثبات
        "--retries", "3",
        "--fragment-retries", "10",
        "--retry-sleep", "3",
        "--sleep-requests", "1",
        "--socket-timeout", "30",
        "--force-ipv4",

        # رؤوس
        "--user-agent", UA,
        "--add-header", f"Accept-Language: {ACCEPT_LANG}",

        # عميل يوتيوب
        "--extractor-args", f"youtube:player_client={player_clients}",

        url,
    ]
    if HAS_COOKIES:
        cmd += ["--cookies", COOKIES_FILE]
    return cmd


async def _download_video(job_id: str, url: str):
    """
    تنزيل فعلي إلى ./files/{job_id}.<ext>
    - مع الكوكيز: نستخدم web/web_embedded لكي تُقبل الكوكيز من YouTube.
    - بدون كوكيز: نستخدم android,tv_embedded,ios,web.
    - fallback متعدد للمشاكل الشائعة (format/sign-in/429).
    """
    outtmpl = os.path.join(FILES_DIR, f"{job_id}.%(ext)s")

    # اختر العملاء حسب وجود الكوكيز
    clients_primary = "web,web_embedded" if HAS_COOKIES else "android,tv_embedded,ios,web"

    # محاولة 1: تفضيل h264، مع دمج mp4
    cmd1 = _base_ydl_cmd(outtmpl, url, clients_primary) + [
        "-S", "codec:h264,res,ext",
        "-f", "bestvideo*+bestaudio/best",
        "--merge-output-format", "mp4",
    ]
    rc1, err1 = await _exec(cmd1)

    if rc1 != 0:
        lower1 = (err1 or "").lower()

        # محاولة 2: إزالة -S والاعتماد على bestvideo+bestaudio
        cmd2 = _base_ydl_cmd(outtmpl, url, clients_primary) + [
            "-f", "bestvideo*+bestaudio/best",
            "--merge-output-format", "mp4",
        ]
        rc2, err2 = await _exec(cmd2)

        if rc2 != 0:
            lower2 = (err2 or "").lower()

            # مع الكوكيز: جرّب web فقط (بعض المرات web_embedded يرفض الكوكيز)
            if HAS_COOKIES:
                cmd3 = _base_ydl_cmd(outtmpl, url, "web") + [
                    "-f", "bestvideo*+bestaudio/best",
                    "--merge-output-format", "mp4",
                ]
                rc3, err3 = await _exec(cmd3)
            else:
                rc3, err3 = 0, ""

            if rc3 != 0:
                lower3 = (err3 or "").lower()

                # محاولة 4: أبسط صيغة ممكنة
                cmd4 = _base_ydl_cmd(outtmpl, url, clients_primary) + [
                    "-f", "best",
                ]
                rc4, err4 = await _exec(cmd4)

                if rc4 != 0:
                    # محاولة 5: صوت فقط
                    cmd5 = _base_ydl_cmd(outtmpl, url, clients_primary) + [
                        "-f", "bestaudio/best",
                    ]
                    rc5, err5 = await _exec(cmd5)

                    if rc5 != 0:
                        msg = (err5 or err4 or err3 or err2 or err1 or "yt-dlp failed").strip()
                        low = msg.lower()
                        if "sign in to confirm" in low:
                            msg = "YouTube needs WEB cookies (signin). Verify cookies.txt is fresh & applied.\n" + msg
                        elif "requested format is not available" in low:
                            msg = "Requested format not available — all fallbacks tried.\n" + msg
                        elif "429" in low or "too many requests" in low:
                            msg = "Rate limited (429). Try again later.\n" + msg
                        JOBS[job_id].update(status="error", error=msg)
                        return

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
    asyncio.create_task(_cleaner_loop())


@app.get("/")
async def home():
    return JSONResponse({"ok": True, "message": "Worker running ✅"})


# ====== إنشاء مهمة ======
@app.post("/jobs")
async def new_job(data: Dict[str, Any], bg: BackgroundTasks):
    url = (data or {}).get("url")
    if not url:
        raise HTTPException(status_code=400, detail="url required")
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "pending", "created_at": _now()}
    bg.add_task(_download_video, job_id, url)
    return {"job_id": job_id, "status": "pending"}


# ====== الاستعلام عن حالة المهمة ======
@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return {k: v for k, v in job.items() if k != "file_path"}

@app.get("/api/jobs/{job_id}")
async def get_job_api(job_id: str):
    return await get_job(job_id)

@app.get("/status/{job_id}")
async def get_job_status(job_id: str):
    return await get_job(job_id)


# ====== تنزيل مباشر ======
@app.get("/download")
async def direct_download(url: str = Query(..., description="Media URL")):
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "running", "created_at": _now()}
    await _download_video(job_id, url)
    job = JOBS[job_id]
    if job.get("status") != "done":
        raise HTTPException(status_code=500, detail=job.get("error") or "download failed")
    return {"file_url": job["file_url"]}


# ====== تقديم الملفات ======
@app.get("/files/{name}")
async def get_file_by_name(name: str):
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


# توافق: /files/{job_id}.mp4 حتى لو الامتداد الحقيقي ليس mp4
@app.get("/files/{job_id}.mp4")
async def get_mp4(job_id: str):
    p = os.path.join(FILES_DIR, f"{job_id}.mp4")
    if os.path.isfile(p):
        return FileResponse(p, filename=f"{job_id}.mp4", media_type="video/mp4")
    alt = _pick_output(job_id)
    if not alt:
        raise HTTPException(status_code=404, detail="file not ready")
    name = os.path.basename(alt)
    return await get_file_by_name(name)
