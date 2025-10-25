# ytdl_helper.py
# -*- coding: utf-8 -*-
import os
import re
from pathlib import Path
from typing import Dict, Any, Tuple
from yt_dlp import YoutubeDL

# ---------- ثوابت عامة ----------
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# مسارات محتملة للكوكيز
CANDIDATE_YT_COOKIES = [
    Path("./cookies/youtube.txt"),
    Path("./youtube.txt"),
]

def _pick_cookiefile() -> str | None:
    for p in CANDIDATE_YT_COOKIES:
        if p.exists() and p.is_file():
            return str(p)
    return None

# ---------- أدوات مساعدة ----------
def _common_headers() -> Dict[str, str]:
    return {
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        "Referer": "https://www.youtube.com/",
    }

def _sanitize(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\n\r\t]+', "_", (s or "video")).strip()
    return s or "video"

def _is_youtube(url: str) -> bool:
    u = (url or "").lower()
    return ("youtube.com" in u) or ("youtu.be" in u)

# ---------- بناء خيارات yt-dlp ----------
def build_opts(outtmpl: str, progress_hook, url_for_cookies: str = "") -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 10,
        "socket_timeout": 30,
        "http_headers": _common_headers(),
        "progress_hooks": [progress_hook],
        "concurrent_fragment_downloads": 4,
        "geo_bypass": True,
        "merge_output_format": "mp4",
        "format": "bestvideo*+bestaudio/best",
        "nopart": True,
        "cachedir": False,
        # حلول شائعة لمشاكل YouTube (player/client/age/consent)
        "extractor_args": {
            "youtube": {
                "player_client": ["web"],  # استخدم عميل الويب
                # يمكنك إضافة خيـارات أخرى هنا لو لزم لاحقاً
            }
        },
    }

    # ✅ تفعيل الكوكيز ليوتيوب فقط عند وجود الملف
    if _is_youtube(url_for_cookies):
        ck = _pick_cookiefile()
        if ck:
            base["cookiefile"] = ck

    return base

# ---------- الاستعلام بدون تنزيل ----------
def probe_info(url: str, base_opts: Dict[str, Any]) -> Dict[str, Any]:
    opts = dict(base_opts)
    opts.pop("format", None)
    opts.pop("merge_output_format", None)
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if info and "entries" in info and info["entries"]:
            info = info["entries"][0]
    return info

# ---------- التنزيل ----------
def download(url: str, out_dir: str) -> Tuple[str, Dict[str, Any]]:
    """
    ينزّل الوسائط إلى out_dir ويُعيد (المسار_النهائي, info)
    - يفرض الخروج mp4 ويعقّم الاسم.
    """
    os.makedirs(out_dir, exist_ok=True)
    tmp_out = os.path.join(out_dir, "%(title).100s.%(ext)s")

    opts = build_opts(tmp_out, lambda _p: None, url_for_cookies=url)

    with YoutubeDL(opts) as ydl:
        info2 = ydl.extract_info(url, download=True)
        final_path = ydl.prepare_filename(info2)

    # تعقيم الاسم وفرض .mp4
    base_name = _sanitize(os.path.splitext(os.path.basename(final_path))[0]) + ".mp4"
    dest = os.path.join(out_dir, base_name)

    if os.path.abspath(final_path) != os.path.abspath(dest):
        try:
            os.replace(final_path, dest)
        except Exception:
            # لو امتداد مختلف (mkv/ts) يظل صالحاً — نسمّيه mp4 لسهولة الإرسال
            try:
                if os.path.exists(final_path):
                    os.rename(final_path, dest)
            except Exception:
                dest = final_path  # كحل أخير

    return dest, info2
