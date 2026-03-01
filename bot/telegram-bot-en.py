#!/usr/bin/env python3
"""Migration script: downloads modular bot structure and switches to main.py.

This file replaces the old monolithic telegram-bot-en.py.
When executed (via /update_bot or directly), it downloads all modules
from GitHub and restarts using the new main.py entry point.
"""
import json
import os
import platform
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")

with open(CONFIG_FILE, encoding="utf-8") as f:
    _config = json.load(f)

BOT_TOKEN = _config["bot_token"]
CHAT_ID = str(_config["chat_id"])
GITHUB_REPO = _config.get("github_repo", "xmin-02/sumone")
GITHUB_RAW = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main"

FILES = [
    "bot/main.py",
    "bot/config.py",
    "bot/state.py",
    "bot/telegram.py",
    "bot/claude.py",
    "bot/tokens.py",
    "bot/sessions.py",
    "bot/downloader.py",
    "bot/i18n/__init__.py",
    "bot/i18n/ko.json",
    "bot/i18n/en.json",
    "bot/commands/__init__.py",
    "bot/commands/basic.py",
    "bot/commands/filesystem.py",
    "bot/commands/settings.py",
    "bot/commands/update.py",
    "bot/commands/total_tokens.py",
    "bot/commands/skills.py",
    "bot/commands/session_cmd.py",
]


def tg_send(text):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
        }).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15)
    except Exception:
        pass


def migrate():
    tg_send("<b>v2 Migration Started</b>\nUpdating to modular structure...")

    # Create subdirectories
    for sub in ["i18n", "commands"]:
        os.makedirs(os.path.join(SCRIPT_DIR, sub), exist_ok=True)

    # Download all files
    failed = []
    for remote_path in FILES:
        local_name = remote_path.replace("bot/", "", 1)
        local_path = os.path.join(SCRIPT_DIR, local_name)
        url = f"{GITHUB_RAW}/{remote_path}"
        try:
            urllib.request.urlretrieve(url, local_path)
        except Exception as e:
            failed.append(f"{local_name}: {e}")

    if failed:
        tg_send(
            f"<b>Migration Failed</b>\n"
            + "\n".join(f"- {f}" for f in failed)
        )
        sys.exit(1)

    tg_send(
        f"<b>v2 Migration Complete!</b>\n"
        f"{len(FILES)} files downloaded.\n"
        f"<i>Restarting with main.py...</i>"
    )
    time.sleep(1)

    # Restart with new entry point
    main_path = os.path.join(SCRIPT_DIR, "main.py")
    os.execv(sys.executable, [sys.executable, main_path])


if __name__ == "__main__":
    # Check if main.py already exists (already migrated)
    main_path = os.path.join(SCRIPT_DIR, "main.py")
    if os.path.exists(main_path):
        # Already migrated, just exec main.py
        os.execv(sys.executable, [sys.executable, main_path])
    else:
        migrate()
