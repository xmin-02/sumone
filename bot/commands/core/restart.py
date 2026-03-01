"""Restart command: /restart_bot."""
import os
import sys

from commands import command
from i18n import t
from config import IS_WINDOWS, log
from state import state
from telegram import send_html


@command("/restart_bot")
def handle_restart_bot(text):
    from telegram import tg_api
    send_html(f"<b>{t('restart.shutting_down')}</b>")
    log.info("Restart requested via /restart_bot")

    # Flush pending Telegram updates so /restart_bot won't be re-processed
    try:
        result = tg_api("getUpdates", {"timeout": 0})
        if result and result.get("ok"):
            updates = result.get("result", [])
            if updates:
                max_id = max(u["update_id"] for u in updates)
                tg_api("getUpdates", {"offset": max_id + 1, "timeout": 0})
                log.info("Flushed Telegram updates up to %d", max_id)
    except Exception as e:
        log.warning("Failed to flush updates: %s", e)

    # Stop file viewer
    try:
        from main import _stop_file_viewer
        _stop_file_viewer()
    except Exception as e:
        log.warning("Failed to stop file viewer: %s", e)

    # Kill AI process if running
    with state.lock:
        proc = state.ai_proc
        if proc and proc.poll() is None:
            try:
                if IS_WINDOWS:
                    proc.terminate()
                else:
                    proc.kill()
            except Exception:
                pass
            state.ai_proc = None
        state.busy = False

    # Re-exec the current process (replaces current process image)
    main_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
    if not os.path.isfile(main_py):
        main_py = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "main.py"))
    log.info("Re-executing: %s %s", sys.executable, main_py)
    os.execv(sys.executable, [sys.executable, main_py])
