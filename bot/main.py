#!/usr/bin/env python3
"""Claude Code Telegram Bot - Entry point.

Polling loop, update routing, and message handling.
"""
import json
import os
import signal
import sys
import threading
import time

import i18n
import config
from config import BOT_TOKEN, CHAT_ID, POLL_TIMEOUT, IS_WINDOWS, settings, log
from state import state
from telegram import (
    escape_html, tg_api, send_html, delete_msg, send_long, send_typing,
)
from tokens import token_footer, get_monthly_tokens, publish_token_data, PUBLISH_INTERVAL
from downloader import download_tg_file, build_file_prompt
from claude import run_claude
from sessions import get_session_model

# Import command modules to trigger @command/@callback decorator registration
import commands.basic       # noqa: F401
import commands.filesystem  # noqa: F401
import commands.settings    # noqa: F401
import commands.update      # noqa: F401
import commands.total_tokens  # noqa: F401
import commands.skills      # noqa: F401
import commands.session_cmd  # noqa: F401

from commands import dispatch, dispatch_callback
from commands.session_cmd import (
    show_questions, handle_answer, handle_selection, _save_session_id,
)
from commands.total_tokens import handle_token_input


# ---------------------------------------------------------------------------
# Core message handler
# ---------------------------------------------------------------------------

def handle_message(text):
    """Send user text to Claude CLI and deliver the response."""
    with state.lock:
        if state.busy:
            send_html(f"<i>{i18n.t('busy')}</i>"); return
        state.busy = True

    # Animated typing indicator
    typing_id = [None]
    typing_stop = threading.Event()
    r = send_html(f"<b>{i18n.t('typing.label')} \u00b7</b>\n<i>{i18n.t('typing.cancel_hint')}</i>")
    try:
        typing_id[0] = r
    except Exception:
        pass

    def _typing_anim():
        dots = ["\u00b7", "\u00b7\u00b7", "\u00b7\u00b7\u00b7"]
        idx = 0
        while not typing_stop.is_set():
            typing_stop.wait(0.1)
            if typing_stop.is_set():
                break
            idx = (idx + 1) % len(dots)
            if typing_id[0]:
                tg_api("editMessageText", {
                    "chat_id": CHAT_ID, "message_id": typing_id[0],
                    "text": f"<b>{i18n.t('typing.label')} {dots[idx]}</b>\n<i>{i18n.t('typing.cancel_hint')}</i>",
                    "parse_mode": "HTML",
                })

    threading.Thread(target=_typing_anim, daemon=True).start()
    send_typing()
    sid = state.session_id

    def _run():
        try:
            log.info("Claude starting for: %s", text[:80])
            output, new_sid, questions = run_claude(text, session_id=sid)
            log.info("Claude finished, output=%d chars, new_sid=%s, questions=%s",
                     len(output) if output else 0, new_sid, bool(questions))

            if new_sid and not state.session_id:
                state.session_id = new_sid
                _save_session_id(new_sid)
                log.info("Auto-connected to session: %s", new_sid)

            active_sid = state.session_id or new_sid or sid
            footer = token_footer()

            typing_stop.set()
            delete_msg(typing_id[0])

            if questions:
                show_questions(questions, active_sid)
                if output and output not in ("",):
                    header = "Claude"
                    if active_sid:
                        header += f" [{active_sid[:8]}]"
                    send_long(header, output, footer=footer)
                return

            if not output:
                return

            header = "Claude"
            if active_sid:
                header += f" [{active_sid[:8]}]"
            send_long(header, output, footer=footer)
            log.info("Response sent to Telegram")
        except Exception as e:
            log.error("handle_message error: %s", e, exc_info=True)
            typing_stop.set()
            delete_msg(typing_id[0])
            send_html(f"<i>{i18n.t('error.generic', msg=str(e))}</i>")
        finally:
            with state.lock:
                state.busy = False

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# Update router
# ---------------------------------------------------------------------------

def process_update(update):
    # --- Callback queries (inline keyboards) ---
    cb = update.get("callback_query")
    if cb:
        cb_chat = str(cb.get("message", {}).get("chat", {}).get("id", ""))
        if cb_chat != CHAT_ID:
            return
        data = cb.get("data", "")
        handler = dispatch_callback(data)
        if handler:
            msg_id = cb.get("message", {}).get("message_id")
            handler(cb["id"], msg_id, data)
        return

    # --- Messages ---
    msg = update.get("message")
    if not msg:
        return
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if chat_id != CHAT_ID:
        log.warning("Unauthorized: %s", chat_id)
        return

    text = msg.get("text", "").strip()
    caption = msg.get("caption", "").strip()

    # Photo attachment
    photos = msg.get("photo")
    if photos:
        best = max(photos, key=lambda p: p.get("file_size", 0))
        local = download_tg_file(best["file_id"])
        if local:
            prompt = build_file_prompt(local, caption or i18n.t("file_prompt.photo_caption"))
            log.info("Photo received: %s", local)
            handle_message(prompt)
        else:
            send_html(f"<i>{i18n.t('error.photo_fail')}</i>")
        return

    # Document attachment
    doc = msg.get("document")
    if doc:
        fname = doc.get("file_name", "file")
        local = download_tg_file(doc["file_id"], fname)
        if local:
            prompt = build_file_prompt(local, caption or i18n.t("file_prompt.doc_caption"))
            log.info("Document received: %s -> %s", fname, local)
            handle_message(prompt)
        else:
            send_html(f"<i>{i18n.t('error.file_fail')}</i>")
        return

    if not text:
        return

    log.info("Received: %s", text[:100])
    lower = text.lower()

    # Special state-based handlers (before command dispatch)
    if lower == "/cancel_connect":
        state.waiting_token_input = False
        send_html(i18n.t("cancel.connect_cancelled"))
        return

    if state.waiting_token_input:
        handle_token_input(text)
        return

    if state.answering:
        handle_answer(text)
        return

    if state.selecting:
        handle_selection(text)
        return

    # Command dispatch via registry
    handler = dispatch(text)
    if handler:
        handler(text)
        return

    # Underscore → hyphen normalization for slash commands (e.g. /code_review → /code-review)
    if text.startswith("/") and "_" in text.split()[0]:
        parts = text.split(maxsplit=1)
        parts[0] = parts[0].replace("_", "-")
        text = " ".join(parts)

    # Default: send to Claude
    handle_message(text)


# ---------------------------------------------------------------------------
# Bot command sync + startup
# ---------------------------------------------------------------------------

def _sync_bot_commands():
    """Register bot commands with BotFather on startup."""
    try:
        bot_commands = i18n.t("bot_commands")
        if isinstance(bot_commands, list):
            commands_list = [{"command": c, "description": d} for c, d in bot_commands]
            tg_api("setMyCommands", {"commands": json.dumps(commands_list)})
            log.info("BotFather commands synced (%d commands)", len(commands_list))
    except Exception as e:
        log.warning("Failed to sync commands: %s", e)


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

def poll_loop():
    offset = 0
    log.info("Bot started.")
    _sync_bot_commands()

    # Token data publishing thread
    def _token_publish_loop():
        time.sleep(10)
        while True:
            try:
                publish_token_data()
            except Exception as e:
                log.error("Token publish error: %s", e)
            time.sleep(PUBLISH_INTERVAL)

    threading.Thread(target=_token_publish_loop, daemon=True).start()
    log.info("Token publish thread started (interval: %ds)", PUBLISH_INTERVAL)

    state.global_tokens = get_monthly_tokens()
    log.info("Monthly tokens loaded: %d", state.global_tokens)

    send_html(f"<b>{i18n.t('bot_started')}</b>")

    while True:
        try:
            result = tg_api("getUpdates", {
                "offset": offset,
                "timeout": POLL_TIMEOUT,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            })
            if not result or not result.get("ok"):
                log.warning("getUpdates failed")
                time.sleep(5)
                continue
            for upd in result.get("result", []):
                offset = upd["update_id"] + 1
                try:
                    process_update(upd)
                except Exception as e:
                    log.error("Update error: %s", e, exc_info=True)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error("Poll error: %s", e, exc_info=True)
            time.sleep(5)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    i18n.load(config.LANG)

    def sig_handler(signum, frame):
        log.info("Signal %s, exiting.", signum)
        with state.lock:
            if state.claude_proc and state.claude_proc.poll() is None:
                state.claude_proc.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    if not IS_WINDOWS:
        signal.signal(signal.SIGTERM, sig_handler)
    else:
        try:
            signal.signal(signal.SIGBREAK, sig_handler)
        except (AttributeError, OSError):
            pass

    poll_loop()


if __name__ == "__main__":
    main()
