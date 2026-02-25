"""Update command: /update_bot with profile photo and patch notes."""
import base64
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


def _git_blob_sha1(filepath):
    """Compute git blob SHA1 for a local file (same algorithm as git)."""
    with open(filepath, "rb") as f:
        data = f.read()
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()


def _fetch_bot_file_list():
    """Fetch list of all files under bot/ with git SHAs from GitHub tree API."""
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/git/trees/main?recursive=1"
    req = urllib.request.Request(api_url, headers={"Accept": "application/vnd.github.v3+json"})
    resp = urllib.request.urlopen(req, timeout=15)
    tree = json.loads(resp.read().decode())
    files = []
    for item in tree.get("tree", []):
        if item["type"] == "blob" and item["path"].startswith("bot/"):
            rel_path = item["path"][4:]  # strip "bot/" prefix
            files.append((rel_path, item["sha"]))
    return files


def _download_via_api(rel_path):
    """Download file content using GitHub Contents API (no CDN cache)."""
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/bot/{rel_path}?ref=main"
    req = urllib.request.Request(api_url, headers={"Accept": "application/vnd.github.v3+json"})
    resp = urllib.request.urlopen(req, timeout=15)
    data = json.loads(resp.read().decode())
    return base64.b64decode(data["content"])


def _update_all_files(bot_dir):
    """Download changed bot files via GitHub API, return (updated, added) lists."""
    files = _fetch_bot_file_list()
    updated = []
    added = []
    for rel_path, remote_sha in files:
        local_path = os.path.join(bot_dir, rel_path)
        is_existing = os.path.exists(local_path)
        # Compare git blob SHA — skip download if unchanged
        if is_existing:
            try:
                if _git_blob_sha1(local_path) == remote_sha:
                    continue
            except Exception:
                pass
        try:
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            content = _download_via_api(rel_path)
            with open(local_path, "wb") as f:
                f.write(content)
            if is_existing:
                updated.append(rel_path)
            else:
                added.append(rel_path)
        except Exception as e:
            log.warning("Failed to update %s: %s", rel_path, e)
    return updated, added


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
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/commits?path=bot&since={last_update}&per_page=20"
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
    try:
        photo_updated = _update_profile_photo()
        # Migrate github_repo unconditionally (runs even if no file changes)
        _new_repo = "xmin-02/sumone"
        if GITHUB_REPO != _new_repo:
            update_config("github_repo", _new_repo)
        updated, added = _update_all_files(bot_dir)
        if not updated and not added:
            if photo_updated:
                send_html(f"<b>{t('update.photo_updated')}</b>")
            else:
                send_html(f"<b>{t('update.up_to_date')}</b>")
            return
        patch_notes = _fetch_patch_notes()
        summary = []
        if updated:
            summary.append(f"Updated: {len(updated)}")
        if added:
            summary.append(f"New: {len(added)}")
        update_config("last_update", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        send_html(
            f"<b>{t('update.complete')}</b> ({', '.join(summary)} files)\n"
            f"{'━'*25}\n{escape_html(patch_notes)}\n{'━'*25}\n"
            f"<i>{t('update.restarting')}</i>"
        )
        time.sleep(1)
        main_path = os.path.join(bot_dir, "main.py")
        os.execv(sys.executable, [sys.executable, main_path])
    except Exception as e:
        send_html(f"<b>{t('update.failed')}:</b> {escape_html(str(e))}")
