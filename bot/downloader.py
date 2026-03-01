"""File download and prompt building."""
import os
import time
import urllib.request

from config import BOT_TOKEN, DATA_DIR, log
from telegram import tg_api

DOWNLOAD_DIR = os.path.join(DATA_DIR, "downloads")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
TEXT_EXTS = {
    ".txt", ".md", ".py", ".go", ".js", ".ts", ".c", ".h", ".cpp", ".java",
    ".rs", ".sh", ".bash", ".zsh", ".json", ".yaml", ".yml", ".toml", ".xml",
    ".html", ".css", ".sql", ".log", ".csv", ".ini", ".cfg", ".conf",
}


def download_tg_file(file_id, filename=None):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    result = tg_api("getFile", {"file_id": file_id})
    if not result or not result.get("ok"): return None
    tg_path = result["result"].get("file_path", "")
    if not tg_path: return None
    if not filename: filename = os.path.basename(tg_path)
    local_path = os.path.join(DOWNLOAD_DIR, f"{int(time.time())}_{filename}")
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{tg_path}"
    try:
        urllib.request.urlretrieve(url, local_path)
        log.info("Downloaded: %s -> %s", tg_path, local_path)
        return local_path
    except Exception as e:
        log.error("Download failed: %s", e); return None


def build_file_prompt(local_path, caption=""):
    from i18n import t
    ext = os.path.splitext(local_path)[1].lower()
    fname = os.path.basename(local_path)
    if ext in IMAGE_EXTS:
        prompt = f"{t('file_prompt.analyze_image')}: {local_path}"
        if caption: prompt = f"{caption}\n\n{t('file_prompt.file_label')}: {local_path}"
        return prompt
    if ext in TEXT_EXTS or ext == "":
        try:
            with open(local_path, "r", errors="replace") as f:
                content = f.read(50000)
            truncated = f" {t('file_prompt.truncated')}" if len(content) >= 50000 else ""
            return f"{caption or t('file_prompt.analyze_file')}\n\n--- {fname}{truncated} ---\n{content}"
        except Exception: pass
    return f"{caption or t('file_prompt.analyze_fallback')}\n\n{t('file_prompt.path_label')}: {local_path}"
