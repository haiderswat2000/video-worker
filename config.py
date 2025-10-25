from pathlib import Path
APP_NAME        = "video-worker"
DEBUG           = True
HOST            = "0.0.0.0"
PORT            = 10000          # Render سيستبدله تلقائياً بـ $PORT
MAX_CONCURRENCY = 2
STORAGE_DIR     = Path("./storage").absolute()
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
CLEANUP_AFTER_DONE = 3600        # ثواني
