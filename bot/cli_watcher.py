"""Watch CLI session JSONL files for direct responses and forward to Telegram.

When the user types directly in a CLI terminal (e.g. `claude -r SESSION`),
the response is written to the session JSONL file. This module tails that
file and forwards new assistant responses to Telegram — so the user can
see CLI output on their phone without opening a terminal.

Currently supports: Claude (reads ~/.claude/projects/*/SESSION.jsonl).
"""
import json
import os
import threading
import time

import i18n
from config import log
from state import state
from sessions import find_project_dirs

_stop = threading.Event()
_thread = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_session_file(session_id):
    """Locate the JSONL file for a Claude session."""
    for proj_dir in find_project_dirs():
        path = os.path.join(proj_dir, f"{session_id}.jsonl")
        if os.path.isfile(path):
            return path
    return None


def _extract_responses(data):
    """Yield complete assistant responses from raw JSONL data.

    Collects text from ``assistant`` events and yields the accumulated
    text each time a ``result`` event marks the end of a turn.
    """
    texts = []
    for line in data.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        etype = event.get("type", "")

        if etype == "assistant":
            for block in event.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text", "").strip()
                    if t:
                        texts.append(t)

        elif etype == "result":
            # Use result text as fallback when no assistant text was captured
            result_text = event.get("result", "").strip()
            if not texts and result_text:
                texts.append(result_text)
            if texts:
                yield "\n\n".join(texts)
            texts = []


# ---------------------------------------------------------------------------
# Watch loop
# ---------------------------------------------------------------------------

def _watch_loop():
    from telegram import send_long, send_html

    current_file = None
    file_pos = 0
    was_busy = False

    while not _stop.is_set():
        try:
            sid = state.session_id
            provider = state.provider or "claude"

            # Only Claude writes session JSONL files we can watch
            if not sid or provider != "claude":
                time.sleep(3)
                continue

            jsonl_path = _find_session_file(sid)
            if not jsonl_path:
                time.sleep(3)
                continue

            # Session file changed → seek to end (skip history)
            if jsonl_path != current_file:
                current_file = jsonl_path
                try:
                    file_pos = os.path.getsize(jsonl_path)
                except OSError:
                    file_pos = 0
                was_busy = False
                time.sleep(2)
                continue

            # Check for new data
            try:
                file_size = os.path.getsize(jsonl_path)
            except OSError:
                time.sleep(2)
                continue

            if file_size <= file_pos:
                time.sleep(2)
                continue

            # Bot is processing → advance past its own output
            if state.busy:
                was_busy = True
                file_pos = file_size
                time.sleep(2)
                continue

            # Just finished processing → skip one cycle to avoid
            # forwarding the bot's own final writes
            if was_busy:
                was_busy = False
                file_pos = file_size
                time.sleep(2)
                continue

            # ---- New data while bot is idle → direct CLI response ----
            with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(file_pos)
                new_data = f.read()

            # Only process up to the last complete line
            last_nl = new_data.rfind("\n")
            if last_nl == -1:
                # No complete line yet
                time.sleep(1)
                continue

            complete = new_data[:last_nl + 1]
            file_pos += len(complete.encode("utf-8"))

            for response in _extract_responses(complete):
                if not response.strip():
                    continue
                header = i18n.t("cli_watcher.header")
                msg = f"<b>{header}</b>\n\n{response}"
                try:
                    send_long(msg)
                    log.info("CLI watcher: forwarded %d chars", len(response))
                except Exception as e:
                    log.warning("CLI watcher send failed: %s", e)

        except Exception as e:
            log.warning("CLI watcher error: %s", e)

        time.sleep(2)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start():
    """Start the CLI watcher background thread."""
    global _thread
    _stop.clear()
    _thread = threading.Thread(target=_watch_loop, daemon=True)
    _thread.start()
    log.info("CLI watcher started")


def stop():
    """Stop the CLI watcher."""
    _stop.set()
    if _thread:
        _thread.join(timeout=5)
