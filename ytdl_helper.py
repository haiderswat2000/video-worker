# -*- coding: utf-8 -*-
import os, re, asyncio, tempfile
from typing import Dict, Any, Tuple, Optional, List
from yt_dlp import YoutubeDL

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

def _common_headers() -> Dict[str, str]:
    return {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,ar;q=0.8"}

def _sanitize_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\n\r\t]+', "_", (s or "video")).strip() or "video"

def build_opts(outtmpl: str, progress_hook):
    return {
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
    }

def probe_info(url: str, base_opts: Dict[str, Any]) -> Dict[str, Any]:
    opts = dict(base_opts); opts.pop("format", None); opts.pop("merge_output_format", None)
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if info and "entries" in info and info["entries"]:
            info = info["entries"][0]
    return info

def download(url: str, out_dir: str) -> Tuple[str, Dict[str, Any]]:
    tmp_out = os.path.join(out_dir, "%(title).100s.%(ext)s")
    progress_hook = lambda _p: None
    opts = build_opts(tmp_out, progress_hook)
    info = probe_info(url, opts)

    with YoutubeDL(opts) as ydl:
        info2 = ydl.extract_info(url, download=True)
        final_path = ydl.prepare_filename(info2)
        base = os.path.basename(final_path)
        safe = _sanitize_name(os.path.splitext(base)[0]) + ".mp4"
        if final_path != os.path.join(out_dir, safe):
            os.replace(final_path, os.path.join(out_dir, safe))
        return os.path.join(out_dir, safe), info2
