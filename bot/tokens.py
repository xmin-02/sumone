"""Token tracking and aggregation.

Uses ~/.sumone/token_log.jsonl as primary source (all providers),
with Claude JSONL fallback for historical data.
"""
import glob
import json
import os
import time

from config import BOT_TOKEN, REMOTE_BOTS, settings, log
from sessions import find_project_dirs
from state import state
from telegram import tg_api_raw

_token_cache = {}
_TOKEN_LOG = os.path.expanduser("~/.sumone/token_log.jsonl")

PUBLISH_LANG = "zu"
PUBLISH_INTERVAL = 300


# ---------------------------------------------------------------------------
# Token log reader (multi-provider)
# ---------------------------------------------------------------------------

def _read_token_log():
    """Read all entries from ~/.sumone/token_log.jsonl."""
    if not os.path.isfile(_TOKEN_LOG):
        return []
    entries = []
    try:
        with open(_TOKEN_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return entries


def _sum_tokens(entries):
    """Sum in+out token counts from token_log entries."""
    total = 0
    for e in entries:
        total += e.get("in", 0) + e.get("out", 0)
    return total


def _logged_sessions(entries):
    """Get set of session IDs present in token_log entries."""
    return {e.get("session") for e in entries if e.get("session")}


def _filter_period(entries, period):
    """Filter token_log entries by time period (local time prefix match)."""
    if period == "total":
        return entries
    today = time.strftime("%Y-%m-%d")
    month = time.strftime("%Y-%m")
    year = time.strftime("%Y")
    result = []
    for e in entries:
        ts = e.get("ts", "")
        if period == "day" and ts[:10] == today:
            result.append(e)
        elif period == "month" and ts[:7] == month:
            result.append(e)
        elif period == "year" and ts[:4] == year:
            result.append(e)
    return result


# ---------------------------------------------------------------------------
# Claude JSONL fallback (historical data before token_log)
# ---------------------------------------------------------------------------

def scan_jsonl_tokens(fpath, seen=None):
    """Count tokens from Claude JSONL assistant events with deduplication.

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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_tokens(period):
    """Get token count for a period. token_log.jsonl + Claude JSONL fallback."""
    if period == "session":
        sid = state.session_id
        if not sid:
            return 0
        entries = _read_token_log()
        session_entries = [e for e in entries if e.get("session") == sid]
        if session_entries:
            return _sum_tokens(session_entries)
        # Fallback: Claude JSONL
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

    today = time.strftime("%Y-%m-%d")
    month = time.strftime("%Y-%m")
    year = time.strftime("%Y")

    # 1. token_log.jsonl (all providers)
    all_entries = _read_token_log()
    filtered = _filter_period(all_entries, period)
    total = _sum_tokens(filtered)
    logged_sids = _logged_sessions(all_entries)

    # 2. Claude JSONL fallback (sessions not in token_log)
    seen = set()
    for proj in find_project_dirs():
        for fp in glob.glob(os.path.join(proj, "**", "*.jsonl"), recursive=True):
            try:
                sid = os.path.splitext(os.path.basename(fp))[0]
                if sid in logged_sids:
                    continue
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
    if period == "none":
        return ""
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
    """Compute token totals for all periods. Used for cross-bot publishing."""
    today = time.strftime("%Y-%m-%d")
    month = time.strftime("%Y-%m")
    year = time.strftime("%Y")
    period_totals = {"d": 0, "m": 0, "y": 0, "t": 0}
    sessions = set()

    # 1. token_log.jsonl (all providers)
    all_entries = _read_token_log()
    for e in all_entries:
        ts = e.get("ts", "")
        tokens = e.get("in", 0) + e.get("out", 0)
        if tokens <= 0:
            continue
        sid = e.get("session", "")
        if sid:
            sessions.add(sid)
        period_totals["t"] += tokens
        if ts[:4] == year:
            period_totals["y"] += tokens
        if ts[:7] == month:
            period_totals["m"] += tokens
        if ts[:10] == today:
            period_totals["d"] += tokens
    logged_sids = _logged_sessions(all_entries)

    # 2. Claude JSONL fallback
    seen = set()
    for proj in find_project_dirs():
        for fp in glob.glob(os.path.join(proj, "**", "*.jsonl"), recursive=True):
            try:
                sid = os.path.splitext(os.path.basename(fp))[0]
                if sid in logged_sids:
                    continue
                mt = time.localtime(os.path.getmtime(fp))
                tokens = scan_jsonl_tokens(fp, seen)
                if tokens > 0:
                    sessions.add(sid)
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
            "s": len(sessions), "ts": int(time.time())}


def get_global_usage():
    """Get global usage. Returns (total_cost, total_input, total_output, session_count)."""
    total_cost = 0.0
    total_input = 0
    total_output = 0
    sessions = set()

    # 1. token_log.jsonl (all providers)
    all_entries = _read_token_log()
    for e in all_entries:
        total_input += e.get("in", 0)
        total_output += e.get("out", 0)
        cost = e.get("cost")
        if cost:
            total_cost += cost
        sid = e.get("session", "")
        if sid:
            sessions.add(sid)
    logged_sids = _logged_sessions(all_entries)

    # 2. Claude JSONL fallback
    for proj_dir in find_project_dirs():
        try:
            fnames = os.listdir(proj_dir)
        except Exception:
            continue
        for fname in fnames:
            if not fname.endswith(".jsonl"):
                continue
            sid = fname[:-6]
            if sid in logged_sids:
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
                            sessions.add(sid)
                            session_counted = True
                        cost = e.get("total_cost_usd", 0)
                        if cost:
                            total_cost += cost
                        usage = e.get("usage", {})
                        total_input += usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                        total_output += usage.get("output_tokens", 0)
            except Exception:
                continue

    return total_cost, total_input, total_output, len(sessions)


def get_provider_usage():
    """Get per-provider usage from token_log.jsonl.

    Returns dict: {provider: {"cost": float, "in": int, "out": int, "sessions": int}}
    """
    result = {}
    entries = _read_token_log()
    for e in entries:
        prov = e.get("provider", "claude")
        if prov not in result:
            result[prov] = {"cost": 0.0, "in": 0, "out": 0, "sessions": set()}
        result[prov]["in"] += e.get("in", 0)
        result[prov]["out"] += e.get("out", 0)
        cost = e.get("cost")
        if cost:
            result[prov]["cost"] += cost
        sid = e.get("session", "")
        if sid:
            result[prov]["sessions"].add(sid)
    for prov in result:
        result[prov]["sessions"] = len(result[prov]["sessions"])
    return result


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
