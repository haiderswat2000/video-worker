# -*- coding: utf-8 -*-
# cookie_manager.py — إدارة واكتشاف ملفات الكوكيز لكل موقع + معالجة خاصة ليوتيوب

import os
import re
import tempfile
from typing import Dict, List, Optional, Tuple

# مسارات البحث لكل دومين (ضع ملفاتك في ./cookies/*.txt مرة واحدة)
COOKIE_PATHS: Dict[str, List[str]] = {
    "youtube":   ["./cookies/youtube.txt",   "./youtube.txt"],
    "instagram": ["./cookies/instagram.txt", "./instagram.txt"],
    "tiktok":    ["./cookies/tiktok.txt",    "./tiktok.txt"],
    "pinterest": ["./cookies/pinterest.txt", "./pinterest.txt"],
    "twitter":   ["./cookies/twitter.txt",   "./twitter.txt", "./cookies/x.txt", "./x.txt"],
}

SID_LIKE = ("__Secure-1PSID", "__Secure-3PSID", "SID", "HSID", "SSID", "SAPISID", "APISID")

def _first_existing(paths: List[str]) -> Optional[str]:
    for p in paths:
        try:
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                return p
        except Exception:
            pass
    return None

def _read_lines(path: str) -> List[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().splitlines()
    except Exception:
        return []

def _fix_netscape_line(line: str) -> str:
    # إصلاح سطور #HttpOnly_.domain => .domain \t TRUE \t ...
    if line.startswith("#HttpOnly_"):
        return line.replace("#HttpOnly_", "", 1)
    return line

def _ensure_consent(lines: List[str], host: str) -> List[str]:
    # يوتيوب/جوجل أحيانًا يحتاج CONSENT=YES+
    has_consent = any("\tCONSENT\t" in l for l in lines)
    if not has_consent:
        lines.append(f".{host}\tTRUE\t/\tTRUE\t1735689600\tCONSENT\tYES+")
    return lines

def _duplicate_youtube_to_google(lines: List[str]) -> List[str]:
    # ننسخ كوكيز youtube إلى .google.com لأن بعض طلبات يوتيوب تمر عبر google.com
    out = []
    for l in lines:
        if not l or l.startswith("#"):
            out.append(l)
            continue
        # أعمدة Netscape: domain, flag, path, secure, expiry, name, value
        parts = l.split("\t")
        if len(parts) < 7:
            out.append(l); continue
        domain = parts[0]
        out.append(l)
        if "youtube.com" in domain:
            clone = l.replace("youtube.com", "google.com")
            out.append(clone)
    return out

def _write_temp_merged(lines: List[str]) -> str:
    fd, path = tempfile.mkstemp(prefix="yt_cookies_merged_", suffix=".txt")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as f:
            for l in lines:
                f.write(l + "\n")
    except Exception:
        pass
    return path

def _merge_youtube_cookiefile(path: str) -> Tuple[str, Dict[str, bool]]:
    """يرجع (path_merged, debug_flags)"""
    raw = _read_lines(path)
    fixed = [_fix_netscape_line(l) for l in raw if l.strip() != ""]
    has_sid_like = any(any(sid in l for sid in SID_LIKE) for l in fixed)
    fixed = _ensure_consent(fixed, "youtube.com")
    fixed = _duplicate_youtube_to_google(fixed)
    merged = _write_temp_merged(fixed)
    dbg = {
        "source": path,
        "rows": len(fixed),
        "has_SID_like": bool(has_sid_like),
        "has_CONSENT": any("\tCONSENT\t" in l for l in fixed),
        "merged": merged,
    }
    return merged, dbg

def detect_cookie_for_url(url: str) -> Tuple[Optional[str], Dict[str, any]]:
    """
    يحدد ملف الكوكيز المناسب للرابط.
    - YouTube: يدمج + يضيف CONSENT + يكرّر لـ .google.com (يرجع ملف مؤقت)
    - مواقع أخرى: يعيد المسار الأصلي كما هو إن وجد
    """
    url_l = url.lower()
    dbg: Dict[str, any] = {"url": url, "selected": None, "domain": None, "details": {}}

    def pick(key: str) -> Optional[str]:
        p = _first_existing(COOKIE_PATHS.get(key, []))
        if p:
            dbg["details"][key] = {"path": p}
        return p

    if "youtube.com" in url_l or "youtu.be" in url_l:
        dbg["domain"] = "youtube"
        p = pick("youtube")
        if not p:
            return None, dbg
        merged, info = _merge_youtube_cookiefile(p)
        dbg["details"]["youtube"].update(info)
        dbg["selected"] = merged
        return merged, dbg

    if "instagram.com" in url_l:
        dbg["domain"] = "instagram"
        p = pick("instagram")
        dbg["selected"] = p
        return p, dbg

    if "tiktok.com" in url_l:
        dbg["domain"] = "tiktok"
        p = pick("tiktok")
        dbg["selected"] = p
        return p, dbg

    if "pinterest." in url_l:
        dbg["domain"] = "pinterest"
        p = pick("pinterest")
        # Pinterest غالبًا يحتاج csrftoken داخل الملف
        if p:
            lines = _read_lines(p)
            dbg["details"]["pinterest"]["rows"] = len(lines)
            dbg["details"]["pinterest"]["has_csrftoken"] = any("\tcsrftoken\t" in l for l in lines)
        dbg["selected"] = p
        return p, dbg

    if "twitter.com" in url_l or "//x.com" in url_l:
        dbg["domain"] = "twitter"
        p = pick("twitter")
        dbg["selected"] = p
        return p, dbg

    # غير ذلك: لا كوكيز
    dbg["domain"] = "generic"
    return None, dbg

def cookies_overview() -> Dict[str, any]:
    """
    ملخص لكل الدومينات: هل وُجد ملف؟ هل به قيم مهمة؟ (للـ /cookies/debug)
    """
    out: Dict[str, any] = {}
    for key, paths in COOKIE_PATHS.items():
        p = _first_existing(paths)
        info = {"found": bool(p), "path": p or None}
        if p:
            lines = _read_lines(p)
            info["rows"] = len(lines)
            if key == "youtube":
                info["has_SID_like"] = any(any(sid in l for sid in SID_LIKE) for l in lines)
                info["has_CONSENT"] = any("\tCONSENT\t" in l for l in lines)
            if key == "pinterest":
                info["has_csrftoken"] = any("\tcsrftoken\t" in l for l in lines)
        out[key] = info
    return out
