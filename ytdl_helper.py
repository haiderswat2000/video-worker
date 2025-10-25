# ytdl_helper.py
# -*- coding: utf-8 -*-
import os, re, shutil, tempfile
from pathlib import Path
from typing import Dict, Any, Tuple, List
from yt_dlp import YoutubeDL

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# نبحث عن الكوكيز في هذه المسارات
CANDIDATE_YT_COOKIES = [Path("./cookies/youtube.txt"), Path("./youtube.txt")]

def _pick_cookiefile() -> Path | None:
    for p in CANDIDATE_YT_COOKIES:
        if p.exists() and p.is_file():
            return p
    return None

def _is_youtube(url: str) -> bool:
    u = (url or "").lower()
    return ("youtube.com" in u) or ("youtu.be" in u)

def _common_headers() -> Dict[str, str]:
    return {
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        "Referer": "https://www.youtube.com/",
    }

def _sanitize(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\n\r\t]+', "_", (s or "video")).strip()
    return s or "video"

def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None

# --------- توليد كوكيز Netscape موسّعة (يوتيوب + جوجل) ----------
NEEDED_NAMES = {
    "__Secure-1PSID", "__Secure-3PSID",
    "__Secure-1PSIDTS", "__Secure-3PSIDTS",
    "__Secure-1PSIDCC", "__Secure-3PSIDCC",
    "SAPISID", "APISID", "HSID", "SSID", "SID",
    "__Secure-1PAPISID", "__Secure-3PAPISID",
    "LOGIN_INFO", "YSC", "VISITOR_INFO1_LIVE", "PREF", "GPS",
    "CONSENT",
}

def _parse_netscape_lines(text: str) -> List[tuple]:
    rows = []
    for line in text.splitlines():
        if not line or line.startswith("#"):  # تجاهل التعليقات
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            domain, flag, path, secure, expires, name, value = parts[:7]
            rows.append((domain, flag, path, secure, expires, name, value))
    return rows

def _write_netscape_file(rows: List[tuple], dest: Path):
    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated/merged by worker",
        ""
    ]
    for (domain, flag, path, secure, expires, name, value) in rows:
        lines.append("\t".join([domain, flag, path, secure, str(expires), name, value]))
    dest.write_text("\n".join(lines), encoding="utf-8")

def _ensure_consent_and_google_mirror(orig: Path) -> str:
    """
    يقرأ youtube.txt ويُنتج ملف مؤقت:
      - يضيف CONSENT=YES+ إن لم توجد
      - ينسخ كل الكوكيز لكلٍ من .youtube.com و .google.com
    """
    txt = orig.read_text(encoding="utf-8", errors="ignore")
    rows = _parse_netscape_lines(txt)

    # هل لدينا CONSENT؟
    have_consent = any(name == "CONSENT" for *_rest, name, _ in rows)

    merged: List[tuple] = []
    for (domain, flag, path, secure, expires, name, value) in rows:
        # تنظيف التعليقات التي كان فيها "#HttpOnly_"
        domain = domain.lstrip("#")
        # نسخ السطر كما هو
        merged.append((domain, flag, path, secure, expires, name, value))
        # مضاعفة لأي كوكي إلى google.com إن كان أصله youtube.com
        if domain.endswith("youtube.com"):
            merged.append((".google.com", "TRUE", "/", "TRUE", expires, name, value))

    if not have_consent:
        # أضف CONSENT=YES+ إلى كلا النطاقين (يدوم حتى 2099)
        for dom in [".youtube.com", ".google.com"]:
            merged.append((dom, "TRUE", "/", "TRUE", "4102444800", "CONSENT", "YES+"))

    tmp = Path(tempfile.gettempdir()) / f"yt_cookies_merged_{os.getpid()}.txt"
    _write_netscape_file(merged, tmp)
    return str(tmp)

# --------- yt-dlp options ----------
def _base_opts(outtmpl: str, progress_hook, url_for_cookies: str = "") -> Dict[str, Any]:
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
        "cachedir": False,
        # تفضيلات للمخرجات
        "format_sort": [
            "+res", "+br",
            "codec:h264", "acodec:aac",
            "ext:mp4", "proto:https", "hasaud",
        ],
        "extractor_args": {"youtube": {"player_client": ["web"]}},
    }

    # تفعيل الكوكيز ليوتيوب فقط
    if _is_youtube(url_for_cookies):
        src = _pick_cookiefile()
        if src:
            base["cookiefile"] = _ensure_consent_and_google_mirror(src)

    return base

def _probe_info(url: str, base_opts: Dict[str, Any]) -> Dict[str, Any]:
    opts = dict(base_opts)
    opts.pop("format", None)
    opts.pop("merge_output_format", None)
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if info and "entries" in info and info["entries"]:
            info = info["entries"][0]
    return info or {}

def _pick_best_muxed(info: Dict[str, Any]) -> str | None:
    fmts: List[Dict[str, Any]] = info.get("formats") or []
    def is_muxed(f: Dict[str, Any]) -> bool:
        ac = (f.get("acodec") or "").lower()
        vc = (f.get("vcodec") or "").lower()
        return ac not in ("", "none") and vc not in ("", "none")
    cand = []
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

def download(url: str, out_dir: str) -> Tuple[str, Dict[str, Any]]:
    """
    يحاول أولاً صيغة مدمجة جاهزة، ثم سلاسل fallback، ثم الدمج عبر FFmpeg إن كان متوفراً.
    كما يدمج/يكمّل الكوكيز تلقائياً لتخطّي رسالة "Sign in to confirm".
    """
    os.makedirs(out_dir, exist_ok=True)
    tmp_out = os.path.join(out_dir, "%(title).100s.%(ext)s")
    base_opts = _base_opts(tmp_out, lambda _p: None, url_for_cookies=url)
    ff_ok = _ffmpeg_available()

    info = _probe_info(url, base_opts)
    fmt_id = _pick_best_muxed(info)

    try_order: List[Dict[str, Any]] = []

    # 1) format_id المدمج إن وجد
    if fmt_id:
        o1 = dict(base_opts); o1["format"] = fmt_id
        try_order.append(o1)

    # 2) بدون دمج: أي صيغة فيها صوت
    o2 = dict(base_opts); o2["format"] = "best[hasaudio=true][ext=mp4]/best[hasaudio=true]/best"
    try_order.append(o2)

    # 3) مع FFmpeg: دمج فيديو+صوت وإخراج mp4
    if ff_ok:
        o3 = dict(base_opts)
        o3["format"] = "bestvideo*+bestaudio/best"
        o3["merge_output_format"] = "mp4"
        try_order.append(o3)

    # 4) أخيرًا best
    o4 = dict(base_opts); o4["format"] = "best"
    try_order.append(o4)

    last_exc = None
    final_path = None
    final_info = None

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

    # إعادة تسمية إلى .mp4 (حتى لو الأصل webm)
    base_name = _sanitize(os.path.splitext(os.path.basename(final_path))[0]) + ".mp4"
    dest = os.path.join(out_dir, base_name)
    if os.path.abspath(final_path) != os.path.abspath(dest):
        try:
            os.replace(final_path, dest)
        except Exception:
            dest = final_path

    return dest, final_info
