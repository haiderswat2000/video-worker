# -*- coding: utf-8 -*-
import os, sys, time, uuid, shutil, logging, base64, tempfile, subprocess
from typing import Optional
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# ========= UTF-8 ثابت =========
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "UTF-8")
os.environ.setdefault("LANG", "C.UTF-8")
os.environ.setdefault("LC_ALL", "C.UTF-8")
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True)
log = logging.getLogger("worker")

FILES_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "files"))
os.makedirs(FILES_DIR, exist_ok=True)

app = FastAPI(title="video-worker", version="1.1.0")
app.mount("/files", StaticFiles(directory=FILES_DIR), name="files")

# ======== تحميل الكوكيز من البيئة (اختياري) ========
_COOKIES_FILE: Optional[str] = None
if os.getenv("YTDLP_COOKIES_FILE"):
    _COOKIES_FILE = os.getenv("YTDLP_COOKIES_FILE")
elif os.getenv("YTDLP_COOKIES_B64"):
    try:
        raw = base64.b64decode(os.environ["YTDLP_COOKIES_B64"]).decode("utf-8", "replace")
        fd, tmp_path = tempfile.mkstemp(prefix="cookies_", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as f:
            f.write(raw)
        _COOKIES_FILE = tmp_path
        log.info("Loaded cookies from YTDLP_COOKIES_B64 into %s", _COOKIES_FILE)
    except Exception as e:
        log.error("Failed to decode YTDLP_COOKIES_B64: %s", e)
        _COOKIES_FILE = None

# ======== UA وترويسات مناسبة ========
_UA = os.getenv("YTDLP_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
_REFERER = "https://www.youtube.com/"

def _safe_name(ext: str) -> str:
    return f"{int(time.time())}_{uuid.uuid4().hex[:12]}{ext}"

def _latest_in(dirpath: str) -> Optional[str]:
    latest_path, latest_mtime = None, -1.0
    for name in os.listdir(dirpath):
        p = os.path.join(dirpath, name)
        try:
            st = os.stat(p)
        except FileNotFoundError:
            continue
        if st.st_mtime > latest_mtime:
            latest_mtime, latest_path = st.st_mtime, p
    return latest_path

def _yt_dlp_download(url: str, audio: bool) -> str:
    # قالب الإخراج
    ext = ".m4a" if audio else ".mp4"
    out_tmpl = os.path.join(FILES_DIR, _safe_name(ext))
    if out_tmpl.endswith(ext):
        out_tmpl = out_tmpl[:-len(ext)]
    out_tmpl += ".%(ext)s"

    common = [
        "yt-dlp",
        "--no-call-home",
        "--no-warnings",
        "--restrict-filenames",
        "--ignore-errors",
        "--force-overwrites",
        "--force-ipv4",
        "--concurrent-fragments", "1",
        "--retries", "3",
        "--retry-sleep", "2",
        "--sleep-requests", "1",
        "--user-agent", _UA,
        "--add-header", f"Referer:{_REFERER}",
        "--add-header", "Origin:https://www.youtube.com",
        "-o", out_tmpl,
        url,
    ]

    # كوكيز (لو متوفرة)
    if _COOKIES_FILE and os.path.isfile(_COOKIES_FILE):
        common += ["--cookies", _COOKIES_FILE]

    # محاولة عميل أندرويد (تساعد ضد صفحة التحقق)
    extractor_args = ["--extractor-args", "youtube:player_client=android"]

    if audio:
        opts = [
            "-f", "bestaudio/best",
            "--extract-audio",
            "--audio-format", "m4a",
            "--audio-quality", "0",
        ] + extractor_args
    else:
        opts = [
            "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
            "--merge-output-format", "mp4",
        ] + extractor_args

    env = os.environ.copy()
    env.setdefault("LANG", "C.UTF-8"); env.setdefault("LC_ALL", "C.UTF-8")

    res = subprocess.run(
        common + opts,
        text=True, encoding="utf-8", errors="replace",
        capture_output=True, env=env
    )
    log.info("yt-dlp exit=%s", res.returncode)
    if res.stdout: log.info("yt-dlp out: %s", res.stdout[-800:])
    if res.stderr: log.info("yt-dlp err: %s", res.stderr[-800:])

    if res.returncode != 0:
        # جرّب محاولة ثانية بدون extractor-args كـ fallback
        res2 = subprocess.run(
            [*common, *opts[:-1*len(extractor_args)]],  # نحذف extractor-args
            text=True, encoding="utf-8", errors="replace",
            capture_output=True, env=env
        )
        log.info("yt-dlp (retry) exit=%s", res2.returncode)
        if res2.stdout: log.info("yt-dlp (retry) out: %s", res2.stdout[-800:])
        if res2.stderr: log.info("yt-dlp (retry) err: %s", res2.stderr[-800:])
        if res2.returncode != 0:
            raise RuntimeError(f"yt-dlp failed: {res2.stderr.strip() or res2.stdout.strip() or res2.returncode}")

    path = _latest_in(FILES_DIR)
    if not path or not os.path.isfile(path):
        raise RuntimeError("No output file produced.")

    # تأكيد الامتداد
    if audio and not path.lower().endswith((".m4a", ".mp3", ".aac", ".opus")):
        target = os.path.splitext(path)[0] + ".m4a"; shutil.move(path, target); path = target
    if (not audio) and not path.lower().endswith(".mp4"):
        target = os.path.splitext(path)[0] + ".mp4"; shutil.move(path, target); path = target

    return path

@app.get("/health")
def health():
    return JSONResponse({"ok": True}, media_type="application/json; charset=utf-8")

@app.get("/download")
def download(url: str = Query(...), audio: int = Query(0)):
    # 404 على "/" طبيعي؛ هذه هي نقطة الخدمة الفعلية
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
        return JSONResponse({"file_url": f"/files/{basename}"}, media_type="application/json; charset=utf-8")
    except Exception as e:
        return JSONResponse({"detail": f"download failed: {e}"}, status_code=500,
                            media_type="application/json; charset=utf-8")
