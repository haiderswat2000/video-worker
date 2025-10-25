# worker_app.py
import os
import uuid
import base64
import asyncio
import contextlib
from typing import Dict, Any, Optional, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

app = FastAPI(title="Video Worker")

JOBS: Dict[str, Dict[str, Any]] = {}
FILES_DIR = os.path.abspath("./_worker_files")
os.makedirs(FILES_DIR, exist_ok=True)

# =============== سياسة التحويل ===============
# عندما تكون True: أي فيديو (audio=False) سيتم تحويله قسرًا إلى H.264+yuv420p+AAC+faststart
ALWAYS_TRANSCODE = True

# يمكن تعطيلها عالميًا عبر متغير بيئة إن رغبت:
if os.getenv("ALWAYS_TRANSCODE", "").lower() in ("0", "false", "no"):
    ALWAYS_TRANSCODE = False

# =============== subprocess helpers ===============
async def _run(*cmd: str) -> Tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out_b, err_b = await proc.communicate()
    out = out_b.decode("utf-8", "ignore")
    err = err_b.decode("utf-8", "ignore")
    return proc.returncode, out, err

async def _probe_video(path: str) -> Tuple[Optional[str], Optional[str]]:
    if not os.path.isfile(path):
        return None, None
    rc1, out1, _ = await _run(
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name",
        "-of", "default=nw=1:nk=1", path
    )
    rc2, out2, _ = await _run(
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=pix_fmt",
        "-of", "default=nw=1:nk=1", path
    )
    vcodec = out1.strip() if rc1 == 0 and out1.strip() else None
    pixfmt = out2.strip() if rc2 == 0 and out2.strip() else None
    return vcodec, pixfmt

async def _make_faststart_copy(src: str, dst: str) -> None:
    rc, _, err = await _run(
        "ffmpeg", "-y", "-i", src, "-movflags", "+faststart",
        "-c", "copy", dst
    )
    if rc != 0:
        raise RuntimeError(f"ffmpeg remux failed: {err[:4000]}")

async def _make_h264_transcode(src: str, dst: str) -> None:
    rc, _, err = await _run(
        "ffmpeg", "-y", "-i", src,
        "-movflags", "+faststart",
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        dst
    )
    if rc != 0:
        raise RuntimeError(f"ffmpeg transcode failed: {err[:4000]}")

async def ensure_mobile_compatible(input_path: str, job_id: str, force_transcode: bool) -> str:
    """
    إن كان force_transcode=True ⇒ تحويل قسري إلى H.264+yuv420p+AAC
    غير ذلك ⇒ إن كان H.264+yuv420p نعمل faststart فقط، وإلا نُحوّل.
    """
    out_path = os.path.join(FILES_DIR, f"{job_id}.mp4")
    if force_transcode:
        await _make_h264_transcode(input_path, out_path)
        return out_path

    vcodec, pixfmt = await _probe_video(input_path)
    needs_transcode = False
    if not vcodec or not pixfmt:
        needs_transcode = True
    else:
        if vcodec.lower() != "h264" or pixfmt.lower() != "yuv420p":
            needs_transcode = True

    if needs_transcode:
        await _make_h264_transcode(input_path, out_path)
    else:
        await _make_faststart_copy(input_path, out_path)

    return out_path

# =============== yt-dlp ===============
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
        # نفضّل MP4، وسنُطَبّع لاحقاً عبر ffmpeg مهما كانت النتيجة
        cmd += ["-f", "bv*+ba/b", "--merge-output-format", "mp4"]

    rc, out, err = await _run(*cmd)

    if cookies_path:
        with contextlib.suppress(Exception):
            os.remove(cookies_path)

    if rc != 0:
        return {"ok": False, "error": (err or out)[:4000]}

    produced = None
    for name in os.listdir(FILES_DIR):
        if name.startswith(job_id + "."):
            produced = os.path.join(FILES_DIR, name)
            break

    if not produced or not os.path.isfile(produced):
        return {"ok": False, "error": "no output file produced"}

    return {"ok": True, "path": produced}

# =============== API ===============
@app.post("/jobs")
async def create_job(payload: Dict[str, Any]):
    url = (payload.get("url") or "").strip()
    want_audio = bool(payload.get("audio", False))
    cookies_b64 = payload.get("cookies_b64")
    # خيار لتمرير التحويلة من الواجهة (اختياري): force_transcode=True/False
    force_transcode = bool(payload.get("force_transcode", ALWAYS_TRANSCODE))

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
        try:
            JOBS[job_id]["status"] = "running"
            res = await run_ytdlp(url, want_audio=want_audio, cookies_txt=cookies_txt, job_id=job_id)
            if not res.get("ok"):
                JOBS[job_id] = {"status": "error", "error": res.get("error", "unknown")}
                return

            src_path = res["path"]

            if want_audio:
                # مسار الصوت — لا تحويل فيديو
                filename = os.path.basename(src_path)
                JOBS[job_id] = {
                    "status": "done",
                    "filename": filename,
                    "download_url": f"/files/{job_id}",
                }
            else:
                # ✅ تمرير التحويلة دائمًا (أو حسب force_transcode)
                try:
                    norm_path = await ensure_mobile_compatible(src_path, job_id=job_id, force_transcode=force_transcode)
                    filename = os.path.basename(norm_path)
                    JOBS[job_id] = {
                        "status": "done",
                        "filename": filename,
                        "download_url": f"/files/{job_id}",
                    }
                    if os.path.abspath(src_path) != os.path.abspath(norm_path):
                        with contextlib.suppress(Exception):
                            os.remove(src_path)
                except Exception as ex:
                    # في حال فشل التحويل، نقدّم الملف الأصلي (كحل أخير)
                    filename = os.path.basename(src_path)
                    JOBS[job_id] = {
                        "status": "done",
                        "filename": filename,
                        "download_url": f"/files/{job_id}",
                        "note": f"normalize_failed:{str(ex)[:120]}",
                    }
        except Exception as e:
            JOBS[job_id] = {"status": "error", "error": str(e)[:4000]}

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
    return {"status": "ok", "endpoints": ["/jobs", "/jobs/{id}", "/files/{id}", "/healthz"]}

@app.get("/healthz")
async def healthz():
    rc1, _, _ = await _run("ffmpeg", "-version")
    rc2, _, _ = await _run("ffprobe", "-version")
    return {"ffmpeg": rc1 == 0, "ffprobe": rc2 == 0, "ok": True}
