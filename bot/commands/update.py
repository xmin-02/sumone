"""Update command: /update_bot with profile photo and patch notes."""
import hashlib
import json
import os
import sys
import time
import uuid
import urllib.request

from commands import command
from i18n import t
from config import BOT_TOKEN, GITHUB_REPO, LANG, _config, update_config, log
from telegram import escape_html, send_html


def _update_profile_photo():
    """Download logo from GitHub and set as bot profile photo if changed."""
    install_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # install_dir should be the bot/ directory
    install_dir = os.path.dirname(os.path.abspath(__file__))
    # Actually, we need the bot/ dir. __file__ is commands/update.py, so:
    install_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cached_logo = os.path.join(install_dir, ".logo_cache.png")
    tmp_logo = os.path.join(install_dir, ".logo_new.png")
    try:
        logo_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/assets/logo.png"
        urllib.request.urlretrieve(logo_url, tmp_logo)
        with open(tmp_logo, "rb") as f:
            new_data = f.read()
        new_hash = hashlib.sha256(new_data).hexdigest()
        old_hash = ""
        if os.path.exists(cached_logo):
            with open(cached_logo, "rb") as f:
                old_hash = hashlib.sha256(f.read()).hexdigest()
        if new_hash == old_hash:
            os.remove(tmp_logo)
            log.info("Profile photo unchanged, skipping")
            return False
        boundary = uuid.uuid4().hex
        photo_json = json.dumps({"type": "static", "photo": "attach://photo_file"})
        parts = []
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"\r\n\r\n{photo_json}\r\n".encode())
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo_file\"; filename=\"logo.png\"\r\nContent-Type: image/png\r\n\r\n".encode() + new_data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/setMyProfilePhoto", data=body)
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        resp = urllib.request.urlopen(req, timeout=30)
        result = json.loads(resp.read())
        if result.get("ok"):
            os.replace(tmp_logo, cached_logo)
            log.info("Profile photo updated")
            return True
    except Exception as e:
        log.warning("Profile photo update failed: %s", e)
    finally:
        try: os.remove(tmp_logo)
        except Exception: pass
    return False


def _fetch_patch_notes():
    last_update = _config.get("last_update", "")
    if not last_update:
        bot_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        main_path = os.path.join(bot_dir, "main.py")
        if os.path.exists(main_path):
            local_mtime = os.path.getmtime(main_path)
        else:
            local_mtime = time.time()
        last_update = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(local_mtime))
    file_path = "bot/main.py"
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/commits?path={file_path}&since={last_update}&per_page=20"
    try:
        req = urllib.request.Request(api_url, headers={"Accept": "application/vnd.github.v3+json"})
        resp = urllib.request.urlopen(req, timeout=10)
        commits = json.loads(resp.read().decode())
        if not commits:
            return t("update.changes_detected")
        notes = []
        for c in commits:
            msg = c.get("commit", {}).get("message", "").split("\n")[0].strip()
            if msg and msg not in notes:
                notes.append(msg)
        if not notes:
            return t("update.changes_detected")
        return "\n".join(f"- {n}" for n in notes[:10])
    except Exception:
        return t("update.changes_detected")


@command("/update_bot", aliases=["/update"])
def handle_update_bot(text):
    send_html(f"<i>{t('update.checking')}</i>")
    bot_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bot_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/bot/main.py"
    current_path = os.path.join(bot_dir, "main.py")
    new_path = current_path + ".new"
    try:
        photo_updated = _update_profile_photo()
        urllib.request.urlretrieve(bot_url, new_path)
        with open(current_path, encoding="utf-8") as f:
            old_content = f.read()
        with open(new_path, encoding="utf-8") as f:
            new_content = f.read()
        if old_content == new_content:
            os.remove(new_path)
            if photo_updated:
                send_html(f"<b>{t('update.photo_updated')}</b>")
            else:
                send_html(f"<b>{t('update.up_to_date')}</b>")
            return
        patch_notes = _fetch_patch_notes()
        os.replace(new_path, current_path)
        update_config("last_update", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        send_html(f"<b>{t('update.complete')}</b>\n{'━'*25}\n{escape_html(patch_notes)}\n{'━'*25}\n<i>{t('update.restarting')}</i>")
        time.sleep(1)
        os.execv(sys.executable, [sys.executable, current_path])
    except Exception as e:
        if os.path.exists(new_path):
            try: os.remove(new_path)
            except Exception: pass
        send_html(f"<b>{t('update.failed')}:</b> {escape_html(str(e))}")
