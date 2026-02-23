"""Token tracking and aggregation."""
import glob
import json
import os
import time

from config import BOT_TOKEN, REMOTE_BOTS, settings, log
from sessions import find_project_dirs
from state import state
from telegram import tg_api_raw

_token_cache = {}

PUBLISH_LANG = "zu"
PUBLISH_INTERVAL = 300


def scan_jsonl_tokens(fpath, seen=None):
    """Count tokens from assistant events with deduplication.

    Parent session files embed subagent events, causing the same
    messageId:requestId to appear in multiple files. Pass a shared
    ``seen`` set across calls to avoid double-counting.
    """
    total = 0
    try:
        with open(fpath, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"assistant"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("type") != "assistant":
                    continue
                if seen is not None:
                    msg_id = e.get("message", {}).get("id")
                    req_id = e.get("requestId")
                    if msg_id and req_id:
                        h = f"{msg_id}:{req_id}"
                        if h in seen:
                            continue
                        seen.add(h)
                u = e.get("message", {}).get("usage", {})
                total += u.get("input_tokens", 0) + u.get("output_tokens", 0) + \
                         u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
    except Exception:
        pass
    return total


def get_tokens(period):
    if period == "session":
        sid = state.session_id
        if not sid:
            return 0
        seen = set()
        for proj in find_project_dirs():
            fp = os.path.join(proj, f"{sid}.jsonl")
            if os.path.exists(fp):
                return scan_jsonl_tokens(fp, seen)
        return 0
    cache_key = f"{period}:{time.strftime('%Y-%m-%d')}"
    cached = _token_cache.get(cache_key)
    if cached and time.time() - cached[1] < 60:
        return cached[0]
    total = 0
    seen = set()
    today = time.strftime("%Y-%m-%d")
    month = time.strftime("%Y-%m")
    year = time.strftime("%Y")
    for proj in find_project_dirs():
        for fp in glob.glob(os.path.join(proj, "**", "*.jsonl"), recursive=True):
            try:
                mt = time.localtime(os.path.getmtime(fp))
                if period == "day" and time.strftime("%Y-%m-%d", mt) != today:
                    continue
                if period == "month" and time.strftime("%Y-%m", mt) != month:
                    continue
                if period == "year" and time.strftime("%Y", mt) != year:
                    continue
                total += scan_jsonl_tokens(fp, seen)
            except Exception:
                continue
    _token_cache[cache_key] = (total, time.time())
    return total


def get_monthly_tokens():
    return get_tokens("month")


def token_footer():
    from i18n import t
    period = settings.get("token_display", "month")
    count = get_tokens(period)
    labels = {"session": "session", "day": time.strftime("%Y-%m-%d"),
              "month": time.strftime("%Y-%m"), "year": time.strftime("%Y"), "total": "total"}
    if settings.get("show_remote_tokens") and REMOTE_BOTS and period != "session":
        period_key = {"day": "d", "month": "m", "year": "y", "total": "t"}.get(period)
        if period_key:
            for bot in REMOTE_BOTS:
                remote = fetch_remote_tokens(bot.get("token", ""))
                if remote:
                    count += remote.get(period_key, 0)
    return f"{labels[period]} tokens: {count:,}"


def compute_all_period_tokens():
    today = time.strftime("%Y-%m-%d")
    month = time.strftime("%Y-%m")
    year = time.strftime("%Y")
    period_totals = {"d": 0, "m": 0, "y": 0, "t": 0}
    session_count = 0
    seen = set()
    for proj in find_project_dirs():
        for fp in glob.glob(os.path.join(proj, "**", "*.jsonl"), recursive=True):
            try:
                mt = time.localtime(os.path.getmtime(fp))
                tokens = scan_jsonl_tokens(fp, seen)
                if tokens > 0:
                    session_count += 1
                    period_totals["t"] += tokens
                    if time.strftime("%Y", mt) == year:
                        period_totals["y"] += tokens
                    if time.strftime("%Y-%m", mt) == month:
                        period_totals["m"] += tokens
                    if time.strftime("%Y-%m-%d", mt) == today:
                        period_totals["d"] += tokens
            except Exception:
                continue
    return {"d": period_totals["d"], "m": period_totals["m"],
            "y": period_totals["y"], "t": period_totals["t"],
            "s": session_count, "ts": int(time.time())}


def publish_token_data():
    data = compute_all_period_tokens()
    desc = json.dumps(data, separators=(",", ":"))
    result = tg_api_raw(BOT_TOKEN, "setMyDescription",
                        {"description": desc, "language_code": PUBLISH_LANG})
    if result and result.get("ok"):
        log.info("Token data published: %d chars", len(desc))
    else:
        log.warning("Token data publish failed")


def fetch_remote_tokens(bot_token):
    result = tg_api_raw(bot_token, "getMyDescription",
                        {"language_code": PUBLISH_LANG})
    if not result or not result.get("ok"):
        return None
    desc = result.get("result", {}).get("description", "")
    if not desc:
        return None
    try:
        return json.loads(desc)
    except (json.JSONDecodeError, ValueError):
        return None


def get_remote_bot_info(bot_token):
    result = tg_api_raw(bot_token, "getMe")
    if not result or not result.get("ok"):
        return None
    bot = result.get("result", {})
    return {"name": bot.get("first_name", ""), "username": bot.get("username", "")}


def get_global_usage():
    total_cost = 0.0
    total_input = 0
    total_output = 0
    session_count = 0
    for proj_dir in find_project_dirs():
        try:
            entries = os.listdir(proj_dir)
        except Exception:
            continue
        for fname in entries:
            if not fname.endswith(".jsonl"):
                continue
            fpath = os.path.join(proj_dir, fname)
            session_counted = False
            try:
                with open(fpath, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                        except Exception:
                            continue
                        if e.get("type") != "result":
                            continue
                        if not session_counted:
                            session_count += 1
                            session_counted = True
                        cost = e.get("total_cost_usd", 0)
                        if cost:
                            total_cost += cost
                        usage = e.get("usage", {})
                        total_input += usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                        total_output += usage.get("output_tokens", 0)
            except Exception:
                continue
    return total_cost, total_input, total_output, session_count
