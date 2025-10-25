# -*- coding: utf-8 -*-
import os
from pathlib import Path

# ⚙️ إعدادات ثابتة (بدون متغيرات بيئة)
APP_NAME        = "video-worker"
DEBUG           = True
HOST            = "0.0.0.0"
PORT            = 10000  # Render يلتقط PORT تلقائياً؛ إن لزم غيّره
MAX_CONCURRENCY = 2      # عدد التحميلات المتوازية
STORAGE_DIR     = Path("./storage").absolute()
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

# مهلة قصوى للدَوران على الوظائف (ثوانٍ)
JOB_POLL_MAX_SECONDS = 1800  # 30 دقيقة
CLEANUP_AFTER_DONE   = 3600  # حذف الملفات بعد ساعة (بالثواني)
