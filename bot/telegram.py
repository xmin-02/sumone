"""Telegram API helpers and message formatting."""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from config import BOT_TOKEN, CHAT_ID, POLL_TIMEOUT, MAX_MSG_LEN, MAX_PARTS, log

# ---------------------------------------------------------------------------
# Auto-dismiss: auto-delete inline keyboard messages after inactivity
# ---------------------------------------------------------------------------
_dismiss_timers = {}  # msg_id -> Timer


def schedule_auto_dismiss(msg_id, timeout=60):
    """Schedule a message for auto-deletion after timeout seconds."""
    cancel_auto_dismiss(msg_id)

    def _dismiss():
        _dismiss_timers.pop(msg_id, None)
        delete_msg(msg_id)

    timer = threading.Timer(timeout, _dismiss)
    timer.daemon = True
    timer.start()
    _dismiss_timers[msg_id] = timer


def cancel_auto_dismiss(msg_id):
    """Cancel auto-dismiss for a message."""
    timer = _dismiss_timers.pop(msg_id, None)
    if timer:
        timer.cancel()


def reset_auto_dismiss(msg_id, timeout=60):
    """Reset the auto-dismiss timer (call on each user interaction)."""
    schedule_auto_dismiss(msg_id, timeout)


def escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def md_to_telegram_html(text):
    lines = text.split("\n")
    out = []
    in_code = False
    in_table = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                out.append("</pre>"); in_code = False
            else:
                if in_table: out.append("</pre>"); in_table = False
                out.append("<pre>"); in_code = True
            continue
        if in_code:
            out.append(escape_html(line)); continue
        if re.match(r"^\s*\|", stripped):
            if not in_table: out.append("<pre>"); in_table = True
            if re.match(r"^\s*\|[\s\-:|]+\|\s*$", stripped): continue
            out.append(escape_html(line)); continue
        elif in_table:
            out.append("</pre>"); in_table = False
        line = escape_html(line)
        line = re.sub(r"^(#{1,6})\s+(.+)$", r"<b>\2</b>", line)
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
        line = re.sub(r"~~(.+?)~~", r"<s>\1</s>", line)
        out.append(line)
    if in_code: out.append("</pre>")
    if in_table: out.append("</pre>")
    return "\n".join(out)


def split_message(text, max_len=MAX_MSG_LEN):
    if len(text) <= max_len: return [text]
    chunks = []; remaining = text
    while remaining and len(chunks) < MAX_PARTS:
        if len(remaining) <= max_len: chunks.append(remaining); break
        segment = remaining[:max_len]; split_at = -1
        for finder, offset in [(lambda s: s.rfind("\n\n"), 2), (lambda s: s.rfind("\n"), 1)]:
            idx = finder(segment)
            if idx > max_len // 3: split_at = idx + offset; break
        if split_at == -1:
            for sep in [". ", "! ", "? ", ".\n"]:
                idx = segment.rfind(sep)
                if idx > max_len // 3: split_at = idx + len(sep); break
        if split_at == -1:
            idx = segment.rfind(" ")
            split_at = (idx + 1) if idx > max_len // 3 else max_len
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining: chunks[-1] += "\n\n... (truncated)"
    return chunks


def tg_api(method, params):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    try:
        req = urllib.request.Request(url, data=data)
        resp = urllib.request.urlopen(req, timeout=max(POLL_TIMEOUT + 10, 60))
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        log.error("TG API %s HTTP %s: %s", method, e.code, body[:200])
        return None
    except Exception as e:
        log.error("TG API %s error: %s", method, e)
        return None


def tg_api_raw(token, method, params=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        if params:
            data = urllib.parse.urlencode(params).encode()
            req = urllib.request.Request(url, data=data)
        else:
            req = urllib.request.Request(url)
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read().decode())
    except Exception as e:
        log.error("TG API raw %s error: %s", method, e)
        return None


def send_text(text, parse_mode=None):
    params = {"chat_id": CHAT_ID, "text": text}
    if parse_mode: params["parse_mode"] = parse_mode
    return tg_api("sendMessage", params)


def send_html(text):
    result = send_text(text, parse_mode="HTML")
    if not result or not result.get("ok"):
        result = send_text(re.sub(r"<[^>]+>", "", text))
    try:
        return result["result"]["message_id"]
    except Exception:
        return None


def delete_msg(msg_id):
    if msg_id:
        tg_api("deleteMessage", {"chat_id": CHAT_ID, "message_id": msg_id})


def send_long(header, body_md, footer=None):
    html_body = md_to_telegram_html(body_md)
    chunks = split_message(html_body)
    total = len(chunks)
    for i, chunk in enumerate(chunks):
        part = f" ({i+1}/{total})" if total > 1 else ""
        msg = f"<b>{escape_html(header)}{part}</b>\n{'━'*20}\n{chunk}"
        if footer and i == total - 1:
            msg += f"\n{'━'*20}\n<i>{footer}</i>"
        send_html(msg)
        if i < total - 1: time.sleep(0.3)


def send_typing():
    tg_api("sendChatAction", {"chat_id": CHAT_ID, "action": "typing"})
