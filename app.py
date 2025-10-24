# -*- coding: utf-8 -*-
"""
FastAPI video-worker (UTF-8 safe)
- /download?url=...&audio=1  => يحوّل لصوت (m4a)
- /download?url=...          => فيديو mp4
- يرجّع {"file_url": "/files/<filename>"}  (قد تكون نسبية)
- يقدّم /files/* كمسار ستاتيكي
"""

import os
import sys
import time
import uuid
import shutil
import logging
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# ========= إجبار UTF-8 =========
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "UTF-8")
os.environ.setdefault("LANG", "C.UTF-8")
os.environ.setdefault("LC_ALL", "C.UTF-8")
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

# ========= لوجينغ نظيف =========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
log = logging.getLogger("worker")

# ========= إنشاء مجلد الإخراج =========
FILES_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "files"))
os.makedirs(FILES_DIR, exist_ok=True)

# ========= إعداد FastAPI =========
app = FastAPI(title="video-worker", version="1.0.0")
app.mount("/files", StaticFiles(directory=FILES_DIR), name="files")

def _safe_name(base: str, ext: str) -> str:
    # اسم عشوائي “نظيف”
    return f"{int(time.time())}_{uuid.uuid4().hex[:12]}{ext}"

def _yt_dlp_download(url: str, audio: bool) -> str:
    """
    ينفذ yt-dlp بخيارات آمنة ويُرجع المسار النهائي للملف.
    - للفيديو: mp4
    - للصوت: m4a (من فيديو)
    """
    import subprocess

    # نعدّ اسمًا مؤقتًا؛ yt-dlp سيستبدله
    ext = ".m4a" if audio else ".mp4"
    out_tmpl = os.path.join(FILES_DIR, _safe_name("out", ext))
    # yt-dlp يتطلب template بدون الامتداد المسبق؛ لذلك نضبطه هكذا:
    # out_tmpl بدون الامتداد ونضيف .%(ext)s ليقررها yt-dlp
    if out_tmpl.endswith(ext):
        out_tmpl = out_tmpl[: -len(ext)]
    out_tmpl += ".%(ext)s"

    # خيارات شائعة
    common = [
        "yt-dlp",
        "--no-call-home",
        "--no-warnings",
        "--restrict-filenames",
        "--ignore-errors",
        "--force-overwrites",
        "-o", out_tmpl,
        url,
    ]

    if audio:
        # أفضل مسار للصوت M4A/AAC بدون إعادة ترميز ثقيلة إن أمكن
        opts = [
            "-f", "bestaudio/best",
            "--extract-audio",
            "--audio-format", "m4a",
            "--audio-quality", "0",
        ]
    else:
        # أفضل فيديو mp4
        # إن لم يتوفر mp4، سيحاول yt-dlp مع demux/merge مناسب
        opts = [
            "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
            "--merge-output-format", "mp4",
        ]

    env = os.environ.copy()
    env["LANG"] = env.get("LANG", "C.UTF-8")
    env["LC_ALL"] = env.get("LC_ALL", "C.UTF-8")

    # تشغيل
    res = subprocess.run(
        common + opts,
        text=True, encoding="utf-8", errors="replace",
        capture_output=True, env=env
    )
    log.info("yt-dlp exit=%s", res.returncode)
    if res.stdout:
        log.info("yt-dlp out: %s", res.stdout[-800:])
    if res.stderr:
        log.info("yt-dlp err: %s", res.stderr[-800:])

    if res.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: code={res.returncode}")

    # التقط اسم الملف الناتج (ابحث في المجلد عن أحدث ملف)
    latest_path: Optional[str] = None
    latest_mtime = -1.0
    for name in os.listdir(FILES_DIR):
        p = os.path.join(FILES_DIR, name)
        try:
            st = os.stat(p)
        except FileNotFoundError:
            continue
        if st.st_mtime > latest_mtime:
            latest_mtime = st.st_mtime
            latest_path = p

    if not latest_path or not os.path.isfile(latest_path):
        raise RuntimeError("No output file produced.")

    # تأكد من الامتداد النهائي
    if audio and not latest_path.lower().endswith((".m4a", ".mp3", ".opus", ".aac")):
        # لو خرج بصيغة غريبة—حوّل الاسم فقط
        target = os.path.splitext(latest_path)[0] + ".m4a"
        shutil.move(latest_path, target)
        latest_path = target
    if (not audio) and not latest_path.lower().endswith(".mp4"):
        target = os.path.splitext(latest_path)[0] + ".mp4"
        shutil.move(latest_path, target)
        latest_path = target

    return latest_path

@app.get("/health")
def health():
    return JSONResponse({"ok": True, "detail": "video-worker is healthy"}, media_type="application/json; charset=utf-8")

@app.get("/download")
def download(url: str = Query(..., description="Source URL to download"),
             audio: int = Query(0, description="1 to extract audio (m4a)")):
    try:
        audio_flag = bool(int(audio))
    except Exception:
        audio_flag = False

    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return JSONResponse({"detail": "Invalid url"}, status_code=400, media_type="application/json; charset=utf-8")

    log.info("download: url=%s audio=%s", url, audio_flag)
    try:
        path = _yt_dlp_download(url, audio=audio_flag)
        basename = os.path.basename(path)
        file_url = f"/files/{basename}"
        # ملاحظة: رابط نسبي. عميلك (البوت) عنده دالة تضمّه مع WORKER_URL
        return JSONResponse({"file_url": file_url}, media_type="application/json; charset=utf-8")
    except Exception as e:
        # أي خطأ يظهر بصيغة JSON واضحة (وبترميز UTF-8)
        return JSONResponse(
            {"detail": f"download failed: {e}"},
            status_code=500,
            media_type="application/json; charset=utf-8",
        )
