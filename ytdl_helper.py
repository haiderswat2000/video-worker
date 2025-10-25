# ytdl_helper.py
# -*- coding: utf-8 -*-
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Any, Tuple, List, Optional
from yt_dlp import YoutubeDL

# وكلاء مستخدم: ويب + iOS (سنستخدم iOS ليوتيوب افتراضياً)
UA_WEB = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
UA_IOS = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)

CANDIDATE_YT_COOKIES: List[Path] = [Path("./cookies/youtube.txt"), Path("./youtube.txt")]

def _pick_cookiefile() -> Optional[Path]:
    for p in CANDIDATE_YT_COOKIES:
        if p.exists() and p.is_file():
            return p
    return None

def _is_youtube(url: str) -> bool:
    u = (url or "").lower()
    return ("youtube.com" in u) or ("youtu.be" in u)

def _sanitize(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\n\r\t]+', "_", (s or "video")).strip()
    return s or "video"

def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None

# ---------- توليد كوكيز Netscape موسّعة (يوتيوب + جوجل) ----------
def _parse_netscape_lines(text: str) -> List[tuple]:
    rows = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            domain, flag, path, secure, expires, name, value = parts[:7]
            rows.append((domain.lstrip("#"), flag, path, secure, expires, name, value))
    return rows

def _write_netscape_file(rows: List[tuple], dest: Path) -> None:
    header = ["# Netscape HTTP Cookie File", "# Generated/merged by worker", ""]
    body = ["\t".join([d, f, p, s, str(e), n, v]) for (d, f, p, s, e, n, v) in rows]
    dest.write_text("\n".join(header + body), encoding="utf-8")

def _ensure_consent_and_google_mirror(orig: Path) -> str:
    """
    يقرأ youtube.txt ويُنتج ملفًا مؤقتًا:
      - يزيل '#HttpOnly_' إن وُجدت
      - يضيف CONSENT=YES+ إن كانت مفقودة
      - ينسخ كل الكوكيز أيضًا إلى .google.com
    """
    txt = orig.read_text(encoding="utf-8", errors="ignore")
    rows = _parse_netscape_lines(txt)
    have_consent = any(r[5] == "CONSENT" for r in rows)

    merged: List[tuple] = []
    for (domain, flag, path, secure, expires, name, value) in rows:
        merged.append((domain, flag, path, secure, expires, name, value))
        if "youtube.com" in domain:
            merged.append((".google.com", "TRUE", "/", "TRUE", expires, name, value))

    if not have_consent:
        for dom in (".youtube.com", ".google.com"):
            merged.append((dom, "TRUE", "/", "TRUE", "4102444800", "CONSENT", "YES+"))

    tmp = Path(tempfile.gettempdir()) / f"yt_cookies_merged_{os.getpid()}.txt"
    _write_netscape_file(merged, tmp)
    return str(tmp)

def _http_headers(use_ios: bool) -> Dict[str, str]:
    return {
        "User-Agent": UA_IOS if use_ios else UA_WEB,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        "Sec-Fetch-Mode": "navigate",
        "Referer": "https://www.youtube.com/",
    }

# ---------- إعدادات yt-dlp ----------
def _base_opts(outtmpl: str, progress_hook, url_for_cookies: str = "") -> Dict[str, Any]:
    # نستخدم iOS ليوتيوب (مع UA iPhone)
    use_ios = _is_youtube(url_for_cookies)

    cookiefile_path: Optional[str] = None
    if use_ios:
        src = _pick_cookiefile()
        if src:
            cookiefile_path = _ensure_consent_and_google_mirror(src)

    base: Dict[str, Any] = {
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 10,
        "socket_timeout": 30,
        "http_headers": _http_headers(use_ios),
        "progress_hooks": [progress_hook],
        "concurrent_fragment_downloads": 4,
        "geo_bypass": True,
        "cachedir": False,
        # تفضيل mp4/h264 وتجنّب HLS قدر الإمكان
        "format_sort": [
            "+res", "+br",
            "codec:h264", "acodec:aac",
            "ext:mp4", "proto:https", "hasaud",
        ],
        # 🟢 جرّب iOS ثم Android ثم Web
        "extractor_args": {
            "youtube": {
                "player_client": ["ios", "android", "web"],
                # اجبر تضمين DASH manifest لأن بعض المقاطع لا تُظهر الصيغ إلا عبره
                "include_dash_manifest": ["True"],
            }
        },
        "verbose": True,
    }
    if cookiefile_path:
        base["cookiefile"] = cookiefile_path
    return base

def probe_info(url: str, base_opts: Dict[str, Any]) -> Dict[str, Any]:
    opts = dict(base_opts)
    # نزيل أي format لنسمح لليدلة أن تُرجع جميع الصيغ
    opts.pop("format", None)
    opts.pop("merge_output_format", None)
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if info and "entries" in info and info["entries"]:
            info = info["entries"][0]
    return info or {}

def _pick_best_muxed(info: Dict[str, Any]) -> Optional[str]:
    fmts: List[Dict[str, Any]] = info.get("formats") or []

    def is_muxed(f: Dict[str, Any]) -> bool:
        ac = (f.get("acodec") or "").lower()
        vc = (f.get("vcodec") or "").lower()
        return ac not in ("", "none") and vc not in ("", "none")

    cand: List[tuple] = []
    for f in fmts:
        if not is_muxed(f):
            continue
        proto = (f.get("protocol") or "").lower()
        is_hls = proto.startswith("m3u8") or "hls" in proto
        cand.append((f, is_hls))

    if not cand:
        return None

    def score(pair):
        f, is_hls = pair
        ext = (f.get("ext") or "").lower()
        h = f.get("height") or 0
        tbr = f.get("tbr") or 0
        mp4_bonus = 100000 if ext == "mp4" else 0
        hls_penalty = -50000 if is_hls else 0
        return (mp4_bonus + hls_penalty, h, tbr)

    best_f, _ = sorted(cand, key=score, reverse=True)[0]
    return best_f.get("format_id")

# ---------- التنزيل ----------
def download(url: str, out_dir: str) -> Tuple[str, Dict[str, Any]]:
    """
    يحاول أولاً ترك yt-dlp يختار تلقائياً (بدون format)، ثم:
    format_id المدمج -> سلسلة مرنة -> دمج عبر FFmpeg -> best.
    """
    os.makedirs(out_dir, exist_ok=True)
    tmp_out = os.path.join(out_dir, "%(title).100s.%(ext)s")
    base_opts = _base_opts(tmp_out, lambda _p: None, url_for_cookies=url)
    ff_ok = _ffmpeg_available()

    info = probe_info(url, base_opts)
    fmt_id = _pick_best_muxed(info)

    try_order: List[Dict[str, Any]] = []

    # 0) اترك yt-dlp يختار تلقائياً (أحياناً يحلّ "Requested format is not available")
    o0 = dict(base_opts)
    o0.pop("format", None)
    o0.pop("merge_output_format", None)
    try_order.append(o0)

    # 1) format_id المدمج إن وُجد
    if fmt_id:
        o1 = dict(base_opts); o1["format"] = fmt_id
        try_order.append(o1)

    # 2) سلسلة بدون دمج: أي صيغة فيها صوت
    o2 = dict(base_opts); o2["format"] = "best[hasaudio=true][ext=mp4]/best[hasaudio=true]/best"
    try_order.append(o2)

    # 3) مع FFmpeg: دمج أفضل فيديو+صوت وإخراج mp4
    if ff_ok:
        o3 = dict(base_opts)
        o3["format"] = "bestvideo*+bestaudio/best"
        o3["merge_output_format"] = "mp4"
        try_order.append(o3)

    # 4) أخيرًا: best
    o4 = dict(base_opts); o4["format"] = "best"
    try_order.append(o4)

    last_exc: Optional[Exception] = None
    final_path: Optional[str] = None
    final_info: Optional[Dict[str, Any]] = None

    for opts in try_order:
        try:
            with YoutubeDL(opts) as ydl:
                final_info = ydl.extract_info(url, download=True)
                final_path = ydl.prepare_filename(final_info)
            break
        except Exception as e:
            last_exc = e
            continue

    if not final_path:
        raise last_exc or RuntimeError("failed to download")

    base_name = _sanitize(os.path.splitext(os.path.basename(final_path))[0]) + ".mp4"
    dest = os.path.join(out_dir, base_name)
    if os.path.abspath(final_path) != os.path.abspath(dest):
        try:
            os.replace(final_path, dest)
        except Exception:
            dest = final_path

    return dest, final_info or {}
