#!/usr/bin/env python3
"""Telegram bot for bidirectional Claude Code interaction (Windows + Linux/macOS).

Commands:
  /session    - List recent sessions, enter selection mode
  /clear      - Clear session, start fresh
  /model      - Change or show current model
  /cost       - Show cost info
  /status     - Show bot status
  /builtin    - List CLI built-in commands
  /skills     - List OMC skills
  /help       - Usage guide
  /cancel     - Cancel running claude process
  /update_bot - Auto-update bot from GitHub
  <number>    - (During selection/answering) Select that option
  <text>      - Send message to Claude via CLI
"""
import json
import logging
import os
import platform
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

IS_WINDOWS = platform.system() == "Windows"

# --- Config (loaded from config.json) ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")

def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)

_config = load_config()
BOT_TOKEN = _config["bot_token"]
CHAT_ID = str(_config["chat_id"])
WORK_DIR = _config.get("work_dir", os.path.expanduser("~"))
LANG = _config.get("lang", "ko")
GITHUB_REPO = _config.get("github_repo", "xmin-02/Claude-telegram-bot")
REMOTE_BOTS = _config.get("remote_bots", [])

DEFAULT_SETTINGS = {
    "show_cost": False,
    "show_status": True,
    "show_global_cost": True,
    "token_display": "month",
    "show_remote_tokens": False,
}
TOKEN_PERIODS = ["session", "day", "month", "year", "total"]
TOKEN_LABELS = {"session": "세션", "day": "일", "month": "월", "year": "년", "total": "전체"}
settings = {**DEFAULT_SETTINGS, **_config.get("settings", {})}

MAX_MSG_LEN = 3900
MAX_PARTS = 20
POLL_TIMEOUT = 30

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")
logging.basicConfig(
    filename=LOG_FILE, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
)
log = logging.getLogger("tg-bot")

# --- State ---
class State:
    session_id = _config.get("session_id")
    selecting = False
    answering = False
    session_list = []
    pending_question = None
    claude_proc = None
    busy = False
    model = None
    total_cost = 0.0
    last_cost = 0.0
    global_tokens = 0
    waiting_token_input = False
    lock = threading.Lock()

def _save_session_id(sid):
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["session_id"] = sid
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
    except Exception:
        pass

MODEL_ALIASES = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
    "o4": "claude-opus-4-6",
    "s4": "claude-sonnet-4-6",
    "h4": "claude-haiku-4-5-20251001",
}

state = State()

# --- Telegram helpers ---
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

def _tg_api_raw(token, method, params=None):
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

# --- File download from Telegram ---
DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")

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

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
TEXT_EXTS = {
    ".txt", ".md", ".py", ".go", ".js", ".ts", ".c", ".h", ".cpp", ".java",
    ".rs", ".sh", ".bash", ".zsh", ".json", ".yaml", ".yml", ".toml", ".xml",
    ".html", ".css", ".sql", ".log", ".csv", ".ini", ".cfg", ".conf",
}

def build_file_prompt(local_path, caption=""):
    ext = os.path.splitext(local_path)[1].lower()
    fname = os.path.basename(local_path)
    if ext in IMAGE_EXTS:
        prompt = f"이 이미지 파일을 분석해줘: {local_path}"
        if caption: prompt = f"{caption}\n\n파일: {local_path}"
        return prompt
    if ext in TEXT_EXTS or ext == "":
        try:
            with open(local_path, "r", errors="replace") as f:
                content = f.read(50000)
            truncated = " (일부만 포함됨)" if len(content) >= 50000 else ""
            return f"{caption or '이 파일 내용을 분석해줘'}\n\n--- {fname}{truncated} ---\n{content}"
        except Exception: pass
    return f"{caption or '이 파일을 분석해줘'}\n\n파일 경로: {local_path}"

# --- Session listing ---
def _find_project_dirs():
    if IS_WINDOWS:
        claude_proj = os.path.join(os.environ.get("APPDATA", ""), "claude", "projects")
        if not os.path.isdir(claude_proj):
            claude_proj = os.path.expanduser("~/.claude/projects")
    else:
        claude_proj = os.path.expanduser("~/.claude/projects")
    if not os.path.isdir(claude_proj): return []
    dirs = []
    for name in os.listdir(claude_proj):
        full = os.path.join(claude_proj, name)
        if os.path.isdir(full): dirs.append(full)
    return dirs

def get_sessions(limit=10):
    import glob as g
    all_files = []
    for proj_dir in _find_project_dirs():
        all_files.extend(g.glob(os.path.join(proj_dir, "*.jsonl")))
    files = sorted(all_files, key=os.path.getmtime, reverse=True)[:limit]
    sessions = []
    for fpath in files:
        sid = os.path.basename(fpath).replace(".jsonl", "")
        mtime = os.path.getmtime(fpath)
        ts = time.strftime("%m/%d %H:%M", time.localtime(mtime))
        preview = _get_first_user_message(fpath)
        sessions.append((sid, ts, preview))
    return sessions

def _get_first_user_message(fpath):
    try:
        with open(fpath, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try: e = json.loads(line)
                except Exception: continue
                if e.get("type") != "user": continue
                content = e.get("message", {}).get("content", "")
                text = _extract_text(content)
                if text:
                    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL).strip()
                    if text: return text[:80]
    except Exception: pass
    return "(미리보기 없음)"

def _extract_text(content):
    if isinstance(content, str) and content.strip(): return content.strip()
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                t = c.get("text", "").strip()
                if t: return t
    return ""

def _get_session_model(session_id):
    for proj_dir in _find_project_dirs():
        fpath = os.path.join(proj_dir, f"{session_id}.jsonl")
        if not os.path.exists(fpath): continue
        try:
            with open(fpath, "rb") as f:
                f.seek(0, 2); size = f.tell()
                f.seek(max(0, size - 50000))
                tail = f.read().decode("utf-8", errors="replace")
            for line in reversed(tail.strip().split("\n")):
                line = line.strip()
                if not line: continue
                try: e = json.loads(line)
                except Exception: continue
                if e.get("type") == "assistant":
                    m = e.get("message", {}).get("model", "")
                    if m: return m
        except Exception: pass
    return None

# --- Claude CLI ---
def _find_claude_cmd():
    """Find the claude CLI executable (handles Windows .cmd wrapper)."""
    for cmd in ["claude", "claude.cmd"]:
        try:
            result = subprocess.run(
                [cmd, "--version"], capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
            )
            if result.returncode == 0: return cmd
        except Exception: continue
    return "claude"  # fallback

CLAUDE_CMD = _find_claude_cmd()

TOOL_LABELS = {
    "Read": "파일 읽는 중", "Edit": "파일 수정 중", "Write": "파일 생성 중",
    "Bash": "명령어 실행 중", "Grep": "코드 검색 중", "Glob": "파일 탐색 중",
    "Task": "에이전트 실행 중", "WebFetch": "웹 조회 중", "WebSearch": "웹 검색 중",
    "AskUserQuestion": "질문 생성 중", "TodoWrite": "작업 목록 업데이트",
}

def _send_intermediate(text):
    html = md_to_telegram_html(text)
    chunks = split_message(html)
    for i, chunk in enumerate(chunks):
        send_html(f"\U0001f4ad {chunk}")
        if i < len(chunks) - 1: time.sleep(0.3)

def _describe_tool(event):
    content = event.get("message", {}).get("content", [])
    if not isinstance(content, list): return None
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use": continue
        name = block.get("name", ""); inp = block.get("input", {})
        label = TOOL_LABELS.get(name, name)
        if name in ("Read", "Edit", "Write"):
            fp = inp.get("file_path", "")
            if fp: label += f": {os.path.basename(fp)}"
        elif name == "Bash":
            cmd = inp.get("command", "")
            if cmd: label += f": {cmd[:40]}"
        elif name in ("Grep", "Glob"):
            pat = inp.get("pattern", "")
            if pat: label += f": {pat[:30]}"
        elif name == "TodoWrite":
            todos = inp.get("todos", [])
            in_prog = [t for t in todos if t.get("status") == "in_progress"]
            if in_prog: label += f": {in_prog[0].get('activeForm', '')[:30]}"
            else: label += f" ({len(todos)}개)"
        return label
    return None

def run_claude(message, session_id=None):
    cmd = [CLAUDE_CMD]
    if session_id: cmd += ["-r", session_id]
    cmd += ["-p", message, "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]
    if state.model: cmd += ["--model", state.model]
    log.info("Running: %s", " ".join(cmd[:6]) + "...")
    try:
        popen_kwargs = dict(
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=WORK_DIR, env=_claude_env(),
        )
        if IS_WINDOWS:
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        with state.lock:
            proc = subprocess.Popen(cmd, **popen_kwargs)
            state.claude_proc = proc
        final_text = []; sent_text_count = 0
        captured_session_id = None; pending_questions = None
        last_status_time = 0; start_time = time.time()
        def _typing_loop():
            while proc.poll() is None: send_typing(); time.sleep(5)
        threading.Thread(target=_typing_loop, daemon=True).start()
        for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line: continue
            try: event = json.loads(line)
            except json.JSONDecodeError: continue
            etype = event.get("type", "")
            if etype == "assistant":
                content = event.get("message", {}).get("content", [])
                has_tool_use = False
                for block in content:
                    if not isinstance(block, dict): continue
                    btype = block.get("type")
                    if btype == "text":
                        t = block.get("text", "").strip()
                        if t: final_text.append(t)
                    elif btype == "tool_use":
                        has_tool_use = True
                        if block.get("name") == "AskUserQuestion":
                            qs = block.get("input", {}).get("questions", [])
                            if qs:
                                pending_questions = qs
                                log.info("AskUserQuestion detected: %d questions, killing proc", len(qs))
                                proc.kill()
                                break
                    if pending_questions:
                        break
                if has_tool_use:
                    unsent = final_text[sent_text_count:]
                    if unsent:
                        combined = "\n\n".join(unsent)
                        if len(combined) > 30:
                            _send_intermediate(combined)
                            log.info("Intermediate text: %d chars", len(combined))
                        sent_text_count = len(final_text)
                now = time.time()
                if settings["show_status"] and now - last_status_time >= 5:
                    desc = _describe_tool(event)
                    if desc:
                        elapsed = int(now - start_time); mins, secs = divmod(elapsed, 60)
                        t = f"{mins}분 {secs}초" if mins > 0 else f"{secs}초"
                        send_html(f"<i>{escape_html(desc)} ({t})</i>")
                        last_status_time = now; log.info("Status: %s", desc)
            if not captured_session_id:
                sid = event.get("session_id")
                if sid: captured_session_id = sid; log.info("Captured session_id: %s", sid)
            if etype == "result":
                result_text = event.get("result", "")
                if result_text and not final_text: final_text.append(result_text)
                if not captured_session_id:
                    sid = event.get("session_id")
                    if sid: captured_session_id = sid
                cost = event.get("total_cost_usd", 0)
                duration = event.get("duration_ms", 0)
                turns = event.get("num_turns", 0)
                usage = event.get("usage", {})
                in_tok = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                out_tok = usage.get("output_tokens", 0)
                if cost:
                    state.last_cost = cost; state.total_cost += cost
                    if settings["show_cost"]:
                        dur_s = duration / 1000 if duration else 0
                        mins, secs = divmod(int(dur_s), 60)
                        dur_str = f"{mins}분 {secs}초" if mins > 0 else f"{secs}초"
                        cost_line = f"\U0001f4b0 ${cost:.4f} | \u23f1 {dur_str} | \U0001f504 {turns}턴 | \U0001f4ca {in_tok:,}+{out_tok:,} 토큰"
                        send_html(f"<i>{cost_line}</i>")
        try: proc.wait(timeout=10)
        except Exception: pass
        try: stderr_out = proc.stderr.read().decode("utf-8", errors="replace").strip()
        except Exception: stderr_out = ""
        with state.lock: state.claude_proc = None
        unsent = final_text[sent_text_count:]
        output = "\n\n".join(unsent).strip()
        if pending_questions:
            return output or "", captured_session_id, pending_questions
        if proc.returncode != 0 and not output and sent_text_count == 0:
            err_msg = f"오류 (코드 {proc.returncode}):\n{stderr_out[:500]}" if stderr_out else f"오류 (코드 {proc.returncode})"
            return err_msg, captured_session_id, None
        return output or "", captured_session_id, pending_questions
    except subprocess.TimeoutExpired:
        with state.lock:
            if state.claude_proc: state.claude_proc.kill(); state.claude_proc = None
        return "시간 초과 (1시간 제한)", None, None
    except Exception as e:
        with state.lock: state.claude_proc = None
        return f"오류: {e}", None, None

def _claude_env():
    env = os.environ.copy()
    env["CLAUDE_TELEGRAM_BOT"] = "1"
    env.pop("CLAUDECODE", None)
    if IS_WINDOWS:
        # Windows: ensure npm global bin is in PATH
        npm_prefix = os.path.join(env.get("APPDATA", ""), "npm")
        if os.path.isdir(npm_prefix):
            env["PATH"] = npm_prefix + ";" + env.get("PATH", "")
        # Add local Python scripts
        py_scripts = os.path.join(os.path.expanduser("~"), "AppData", "Local", "Programs", "Python", "Scripts")
        if os.path.isdir(py_scripts):
            env["PATH"] = py_scripts + ";" + env.get("PATH", "")
    else:
        env["HOME"] = os.path.expanduser("~")
        env["PATH"] = os.path.expanduser("~/.local/bin") + ":/usr/local/bin:/usr/bin:/bin"
        goroot = os.path.join(WORK_DIR, "goroot")
        gopath = os.path.join(WORK_DIR, "gopath")
        if os.path.isdir(goroot):
            env["GOROOT"] = goroot
            env["PATH"] = f"{gopath}/bin:{goroot}/bin:{env['PATH']}"
        if os.path.isdir(gopath):
            env["GOPATH"] = gopath
    return env

def _get_monthly_tokens():
    return _get_tokens("month")

_token_cache = {}

def _scan_jsonl_tokens(fpath):
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
                u = e.get("message", {}).get("usage", {})
                total += u.get("input_tokens", 0) + u.get("output_tokens", 0) + \
                         u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
    except Exception:
        pass
    return total

def _get_tokens(period):
    import glob
    if period == "session":
        sid = state.session_id
        if not sid:
            return 0
        for proj in _find_project_dirs():
            fp = os.path.join(proj, f"{sid}.jsonl")
            if os.path.exists(fp):
                return _scan_jsonl_tokens(fp)
        return 0
    cache_key = f"{period}:{time.strftime('%Y-%m-%d')}"
    cached = _token_cache.get(cache_key)
    if cached and time.time() - cached[1] < 60:
        return cached[0]
    total = 0
    today = time.strftime("%Y-%m-%d")
    month = time.strftime("%Y-%m")
    year = time.strftime("%Y")
    for proj in _find_project_dirs():
        for fp in glob.glob(os.path.join(proj, "*.jsonl")):
            try:
                mt = time.localtime(os.path.getmtime(fp))
                if period == "day" and time.strftime("%Y-%m-%d", mt) != today:
                    continue
                if period == "month" and time.strftime("%Y-%m", mt) != month:
                    continue
                if period == "year" and time.strftime("%Y", mt) != year:
                    continue
                total += _scan_jsonl_tokens(fp)
            except Exception:
                continue
    _token_cache[cache_key] = (total, time.time())
    return total

def _token_footer():
    period = settings.get("token_display", "month")
    count = _get_tokens(period)
    labels = {"session": "session", "day": time.strftime("%Y-%m-%d"),
              "month": time.strftime("%Y-%m"), "year": time.strftime("%Y"), "total": "total"}
    # Add remote tokens if enabled
    if settings.get("show_remote_tokens") and REMOTE_BOTS and period != "session":
        period_key = {"day": "d", "month": "m", "year": "y", "total": "t"}.get(period)
        if period_key:
            for bot in REMOTE_BOTS:
                remote = _fetch_remote_tokens(bot.get("token", ""))
                if remote:
                    count += remote.get(period_key, 0)
    return f"{labels[period]} tokens: {count:,}"

PUBLISH_LANG = "zu"  # Language code used as data channel (Zulu - won't affect Korean users)
PUBLISH_INTERVAL = 300  # seconds (5 minutes)

def _compute_all_period_tokens():
    """Compute token totals for all periods in a single structure."""
    import glob
    result = {}
    today = time.strftime("%Y-%m-%d")
    month = time.strftime("%Y-%m")
    year = time.strftime("%Y")
    period_totals = {"d": 0, "m": 0, "y": 0, "t": 0}
    session_count = 0
    for proj in _find_project_dirs():
        for fp in glob.glob(os.path.join(proj, "*.jsonl")):
            try:
                mt = time.localtime(os.path.getmtime(fp))
                tokens = _scan_jsonl_tokens(fp)
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

def _publish_token_data():
    """Store aggregated token data in bot's own description (language_code='zu')."""
    data = _compute_all_period_tokens()
    desc = json.dumps(data, separators=(",", ":"))
    result = _tg_api_raw(BOT_TOKEN, "setMyDescription",
                         {"description": desc, "language_code": PUBLISH_LANG})
    if result and result.get("ok"):
        log.info("Token data published: %d chars", len(desc))
    else:
        log.warning("Token data publish failed")

def _fetch_remote_tokens(bot_token):
    """Read token data from another bot's description."""
    result = _tg_api_raw(bot_token, "getMyDescription",
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

def _get_remote_bot_info(bot_token):
    """Get bot name and username from token via getMe."""
    result = _tg_api_raw(bot_token, "getMe")
    if not result or not result.get("ok"):
        return None
    bot = result.get("result", {})
    return {"name": bot.get("first_name", ""), "username": bot.get("username", "")}

def get_global_usage():
    total_cost = 0.0
    total_input = 0
    total_output = 0
    session_count = 0
    for proj_dir in _find_project_dirs():
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

# --- Command handlers ---
def handle_session():
    sessions = get_sessions(10)
    state.session_list = sessions
    state.selecting = True
    if not sessions:
        send_html("<b>세션이 없습니다.</b>"); return
    lines = []
    for i, (sid, ts, preview) in enumerate(sessions, 1):
        p = preview[:50] + "..." if len(preview) > 50 else preview
        lines.append(f"<b>{i}.</b> <code>{sid[:8]}</code> {escape_html(ts)}\n    {escape_html(p)}")
    current = ""
    if state.session_id: current = f"\n현재: <code>{state.session_id[:8]}</code>"
    msg = (f"<b>최근 세션</b>{current}\n{'━'*25}\n"
           + "\n".join(lines)
           + f"\n{'━'*25}\n번호(1-10) 또는 세션 UUID를 입력하세요.\n/clear 새 세션 시작")
    send_html(msg)

def _show_questions(questions, sid):
    lines = []; all_options = []
    for qi, q in enumerate(questions):
        header = q.get("header", ""); question = q.get("question", "")
        options = q.get("options", []); multi = q.get("multiSelect", False)
        icon = "\U0001f4cb" if multi else "\u2753"
        line = f"{icon} {escape_html(question)}"
        if header: line = f"<b>[{escape_html(header)}]</b> {line}"
        lines.append(line)
        for oi, opt in enumerate(options):
            num = len(all_options) + 1
            label = opt.get("label", ""); desc = opt.get("description", "")
            entry = f"  <b>{num}.</b> {escape_html(label)}"
            if desc: entry += f" — {escape_html(desc)}"
            lines.append(entry)
            all_options.append({"label": label, "q_idx": qi, "opt_idx": oi})
    body = "\n".join(lines)
    msg = (f"<b>Claude — 선택 필요</b>\n{'━'*25}\n{body}\n{'━'*25}\n"
           "번호를 입력해서 선택하세요.")
    send_html(msg)
    state.pending_question = {"session_id": sid, "questions": questions, "options_map": all_options}
    state.answering = True
    log.info("Entered answering mode: %d options", len(all_options))

def handle_answer(text):
    text = text.strip()
    pq = state.pending_question
    if not pq:
        state.answering = False; handle_message(text); return
    options_map = pq["options_map"]; sid = pq["session_id"]
    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(options_map):
            chosen = options_map[idx]; label = chosen["label"]
            state.answering = False; state.pending_question = None
            if sid: state.session_id = sid; _save_session_id(sid)
            answer_text = f'"{label}" 을 선택합니다.'
            log.info("Answer: %s (option %d)", label, idx + 1)
            handle_message(answer_text); return
        else:
            send_html(f"잘못된 번호입니다. 1-{len(options_map)} 사이를 입력하세요."); return
    state.answering = False; state.pending_question = None
    if sid: state.session_id = sid; _save_session_id(sid)
    handle_message(text)

def handle_clear():
    state.session_id = None; state.selecting = False
    state.answering = False; state.pending_question = None
    _save_session_id(None)
    send_html("<b>대화 초기화</b>\n이전 맥락 없이 새 대화를 시작합니다.")

def handle_cost():
    msg = (f"<b>비용 정보</b>\n{'━'*25}\n"
           f"마지막 요청: ${state.last_cost:.4f}\n"
           f"봇 세션 누적: ${state.total_cost:.4f}\n")
    if settings["show_global_cost"]:
        try:
            g_cost, g_in, g_out, g_sessions = get_global_usage()
            msg += (f"\n<b>전체 사용량 (모든 세션)</b>\n{'━'*25}\n"
                    f"총 비용: ${g_cost:.4f}\n"
                    f"총 세션: {g_sessions}개\n"
                    f"입력 토큰: {g_in:,}\n"
                    f"출력 토큰: {g_out:,}\n"
                    f"총 토큰: {g_in + g_out:,}\n")
        except Exception:
            pass
    send_html(msg)

# --- /total_tokens ---
def _save_remote_bots():
    """Save remote_bots list to config.json."""
    global REMOTE_BOTS
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["remote_bots"] = REMOTE_BOTS
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
    except Exception:
        pass

def handle_total_tokens():
    """Show total tokens menu with inline keyboard."""
    my_info = _tg_api_raw(BOT_TOKEN, "getMe")
    my_name = ""
    if my_info and my_info.get("ok"):
        my_name = f"@{my_info['result'].get('username', 'unknown')}"
    remote_count = len(REMOTE_BOTS)
    remote_info = f"\n연결된 PC: {remote_count}개" if remote_count > 0 else "\n연결된 PC: 없음"
    msg = (f"<b>토큰 사용량 관리</b>\n{'━'*25}\n"
           f"현재 봇: <code>{escape_html(my_name)}</code>"
           f"{remote_info}\n{'━'*25}")
    buttons = [
        [{"text": "사용 집계", "callback_data": "tt:aggregate"}],
        [{"text": "다른 PC 연결", "callback_data": "tt:connect"},
         {"text": "연결된 PC 관리", "callback_data": "tt:manage"}],
        [{"text": "닫기", "callback_data": "tt:close"}],
    ]
    tg_api("sendMessage", {
        "chat_id": CHAT_ID,
        "text": msg,
        "parse_mode": "HTML",
        "reply_markup": json.dumps({"inline_keyboard": buttons}),
    })

def _handle_aggregate():
    """Aggregate tokens from local + all remote bots."""
    send_html("<i>집계 중...</i>")
    _publish_token_data()
    local_data = _compute_all_period_tokens()
    my_info = _tg_api_raw(BOT_TOKEN, "getMe")
    my_name = f"@{my_info['result'].get('username', '')}" if my_info and my_info.get("ok") else "현재 PC"
    period_labels = {"d": "일", "m": "월", "y": "년", "t": "전체"}
    lines = [f"<b>토큰 사용량 (전체 PC 합산)</b>\n{'━'*25}"]
    lines.append(f"\n<b>{escape_html(my_name)}</b> (현재 PC)")
    for p, label in period_labels.items():
        lines.append(f"  {label}: {local_data.get(p, 0):,}")
    lines.append(f"  세션: {local_data.get('s', 0)}개")
    totals = {p: local_data.get(p, 0) for p in period_labels}
    total_sessions = local_data.get("s", 0)
    for bot in REMOTE_BOTS:
        token = bot.get("token", "")
        name = bot.get("username", bot.get("name", "알 수 없음"))
        remote_data = _fetch_remote_tokens(token)
        if remote_data:
            lines.append(f"\n<b>@{escape_html(name)}</b>")
            for p, label in period_labels.items():
                val = remote_data.get(p, 0)
                lines.append(f"  {label}: {val:,}")
                totals[p] += val
            rs = remote_data.get("s", 0)
            lines.append(f"  세션: {rs}개")
            total_sessions += rs
            ts = remote_data.get("ts", 0)
            if ts:
                updated = time.strftime("%m/%d %H:%M", time.localtime(ts))
                lines.append(f"  <i>마지막 갱신: {updated}</i>")
        else:
            lines.append(f"\n<b>@{escape_html(name)}</b>")
            lines.append("  <i>데이터 없음 (봇이 실행 중인지 확인하세요)</i>")
    if REMOTE_BOTS:
        lines.append(f"\n{'━'*25}\n<b>합계</b>")
        for p, label in period_labels.items():
            lines.append(f"  {label}: {totals[p]:,}")
        lines.append(f"  세션: {total_sessions}개")
    send_html("\n".join(lines))

def _handle_connect():
    """Start token input mode for connecting another PC."""
    state.waiting_token_input = True
    send_html(
        f"<b>다른 PC 연결</b>\n{'━'*25}\n"
        "다른 PC 봇의 토큰을 입력해주세요.\n"
        "(@BotFather → 봇 선택 → API Token)\n\n"
        "<i>취소하려면 /cancel_connect 를 입력하세요.</i>")

def _handle_token_input(text):
    """Process bot token input for remote PC connection."""
    state.waiting_token_input = False
    token = text.strip()
    if not re.match(r'^\d+:[A-Za-z0-9_-]+$', token):
        send_html("<b>오류:</b> 올바른 봇 토큰 형식이 아닙니다.")
        return
    for bot in REMOTE_BOTS:
        if bot.get("token") == token:
            send_html(f"<b>이미 연결된 봇입니다:</b> @{escape_html(bot.get('username', ''))}")
            return
    if token == BOT_TOKEN:
        send_html("<b>오류:</b> 현재 봇의 토큰은 연결할 수 없습니다.")
        return
    info = _get_remote_bot_info(token)
    if not info:
        send_html("<b>오류:</b> 유효하지 않은 토큰입니다. 토큰을 확인해주세요.")
        return
    new_bot = {"token": token, "name": info["name"], "username": info["username"]}
    REMOTE_BOTS.append(new_bot)
    _save_remote_bots()
    send_html(
        f"<b>연결 완료!</b>\n{'━'*25}\n"
        f"봇 이름: {escape_html(info['name'])}\n"
        f"유저네임: @{escape_html(info['username'])}\n"
        f"{'━'*25}\n"
        f"<i>해당 PC에서도 봇이 실행 중이어야 데이터를 가져올 수 있습니다.</i>")
    log.info("Remote bot connected: @%s", info["username"])

def _handle_manage():
    """Show connected PCs management UI."""
    if not REMOTE_BOTS:
        send_html("<b>연결된 PC가 없습니다.</b>\n/total_tokens → 다른 PC 연결 로 추가하세요.")
        return
    lines = [f"<b>연결된 PC 관리</b>\n{'━'*25}"]
    buttons = []
    for i, bot in enumerate(REMOTE_BOTS):
        name = bot.get("username", bot.get("name", "알 수 없음"))
        lines.append(f"  <b>{i+1}.</b> @{escape_html(name)}")
        buttons.append([{"text": f"{i+1}. @{name} 삭제", "callback_data": f"tt:del:{i}"}])
    buttons.append([{"text": "닫기", "callback_data": "tt:close"}])
    tg_api("sendMessage", {
        "chat_id": CHAT_ID,
        "text": "\n".join(lines),
        "parse_mode": "HTML",
        "reply_markup": json.dumps({"inline_keyboard": buttons}),
    })

def _handle_delete_remote(index):
    """Delete a remote bot connection."""
    global REMOTE_BOTS
    if 0 <= index < len(REMOTE_BOTS):
        removed = REMOTE_BOTS.pop(index)
        _save_remote_bots()
        name = removed.get("username", removed.get("name", ""))
        return f"@{name} 연결 해제됨"
    return "잘못된 인덱스"

def handle_total_tokens_callback(callback_id, msg_id, data):
    """Handle inline keyboard callbacks for /total_tokens."""
    action = data.split(":", 1)[1] if ":" in data else ""
    if action == "close":
        tg_api("deleteMessage", {"chat_id": CHAT_ID, "message_id": msg_id})
        tg_api("answerCallbackQuery", {"callback_query_id": callback_id})
        return
    if action == "aggregate":
        tg_api("answerCallbackQuery", {"callback_query_id": callback_id, "text": "집계 중..."})
        threading.Thread(target=_handle_aggregate, daemon=True).start()
        return
    if action == "connect":
        tg_api("answerCallbackQuery", {"callback_query_id": callback_id})
        _handle_connect()
        return
    if action == "manage":
        tg_api("answerCallbackQuery", {"callback_query_id": callback_id})
        _handle_manage()
        return
    if action.startswith("del:"):
        try:
            index = int(action.split(":")[1])
            result_text = _handle_delete_remote(index)
            tg_api("answerCallbackQuery", {"callback_query_id": callback_id, "text": result_text})
            tg_api("deleteMessage", {"chat_id": CHAT_ID, "message_id": msg_id})
            _handle_manage()
        except (ValueError, IndexError):
            tg_api("answerCallbackQuery", {"callback_query_id": callback_id, "text": "오류"})
        return
    tg_api("answerCallbackQuery", {"callback_query_id": callback_id})

def handle_model(text):
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or parts[1].strip() == "":
        current = state.model or "기본값 (sonnet)"
        aliases = ", ".join(sorted(MODEL_ALIASES.keys()))
        send_html(
            f"<b>현재 모델:</b> <code>{escape_html(current)}</code>\n{'━'*25}\n"
            f"<b>사용법:</b> /model [이름]\n<b>단축어:</b> {escape_html(aliases)}\n"
            f"<b>예시:</b>\n  /model opus\n  /model sonnet\n  /model haiku\n"
            f"  /model default — 기본값으로 복원")
        return
    name = parts[1].strip().lower()
    if name in ("default", "reset", "기본", "기본값"):
        state.model = None
        send_html("<b>모델 초기화:</b> 기본값 (sonnet)"); return
    resolved = MODEL_ALIASES.get(name)
    if not resolved:
        if name.startswith("claude-"): resolved = name
        else:
            aliases = ", ".join(sorted(MODEL_ALIASES.keys()))
            send_html(f"알 수 없는 모델: <code>{escape_html(name)}</code>\n사용 가능: {escape_html(aliases)}"); return
    state.model = resolved
    send_html(f"<b>모델 변경됨:</b> <code>{escape_html(resolved)}</code>")

def handle_status():
    session_info = f"<code>{state.session_id[:8]}</code>" if state.session_id else "없음 (새 세션 모드)"
    model_info = f"<code>{escape_html(state.model)}</code>" if state.model else "기본값 (sonnet)"
    busy_info = "처리 중" if state.busy else "대기"
    os_info = f"Windows ({platform.version()})" if IS_WINDOWS else platform.platform()
    msg = (f"<b>Bot 상태</b>\n{'━'*25}\n"
           f"세션: {session_info}\n모델: {model_info}\n상태: {busy_info}\nOS: {escape_html(os_info)}\n")
    send_html(msg)

def _fetch_patch_notes():
    last_update = _config.get("last_update", "")
    if not last_update:
        local_mtime = os.path.getmtime(os.path.abspath(__file__))
        last_update = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(local_mtime))
    file_path = f"bot/telegram-bot-{LANG}.py"
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/commits?path={file_path}&since={last_update}&per_page=20"
    try:
        req = urllib.request.Request(api_url, headers={"Accept": "application/vnd.github.v3+json"})
        resp = urllib.request.urlopen(req, timeout=10)
        commits = json.loads(resp.read().decode())
        if not commits:
            return "변경 사항 있음"
        notes = []
        for c in commits:
            msg = c.get("commit", {}).get("message", "").split("\n")[0].strip()
            if msg and msg not in notes:
                notes.append(msg)
        if not notes:
            return "변경 사항 있음"
        return "\n".join(f"- {n}" for n in notes[:10])
    except Exception:
        return "변경 사항 있음"

def _save_update_time():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["last_update"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
    except Exception:
        pass

def _update_profile_photo():
    """GitHub에서 로고를 다운로드하고, 변경된 경우 봇 프로필 사진으로 설정."""
    import hashlib, uuid
    install_dir = os.path.dirname(os.path.abspath(__file__))
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

def handle_update_bot():
    send_html("<i>업데이트 확인 중...</i>")
    bot_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/bot/telegram-bot-{LANG}.py"
    current_path = os.path.abspath(__file__)
    new_path = current_path + ".new"
    try:
        # 프로필 사진 체크 (코드 변경과 무관하게 항상 실행)
        photo_updated = _update_profile_photo()
        urllib.request.urlretrieve(bot_url, new_path)
        with open(current_path, encoding="utf-8") as f:
            old_content = f.read()
        with open(new_path, encoding="utf-8") as f:
            new_content = f.read()
        if old_content == new_content:
            os.remove(new_path)
            if photo_updated:
                send_html("<b>프로필 사진이 업데이트되었습니다.</b>")
            else:
                send_html("<b>이미 최신 버전입니다.</b>")
            return
        patch_notes = _fetch_patch_notes()
        os.replace(new_path, current_path)
        _save_update_time()
        send_html(f"<b>업데이트 완료!</b>\n{'━'*25}\n{escape_html(patch_notes)}\n{'━'*25}\n<i>재시작 중...</i>")
        time.sleep(1)
        os.execv(sys.executable, [sys.executable, current_path])
    except Exception as e:
        if os.path.exists(new_path):
            try: os.remove(new_path)
            except Exception: pass
        send_html(f"<b>업데이트 실패:</b> {escape_html(str(e))}")

def handle_builtin():
    msg = (
        "<b>빌트인 명령어 (CLI 내장)</b>\n" + '━'*25 + "\n"
        "<b>봇에서 동작</b>\n"
        "  /clear — 대화 초기화\n  /cost — 비용 확인\n  /model — 모델 변경/확인\n"
        "  /session — 세션 선택\n  /status — 상태 확인\n  /cancel — 작업 취소\n"
        "  /pwd — 현재 작업 디렉토리\n  /cd — 디렉토리 이동\n  /ls — 파일 목록\n"
        "  /settings — 봇 설정 (비용 표시, 상태 메시지 등)\n"
        "  /update_bot — 봇 자동 업데이트 (GitHub에서 최신 코드 다운로드)\n"
        "\n<b>Claude에 전달됨</b>\n"
        "  /compact — 컨텍스트 압축\n  /context — 컨텍스트 사용량\n  /init — 프로젝트 초기화\n"
        "  /review — 코드 리뷰\n  /security-review — 보안 리뷰\n  /pr-comments — PR 코멘트\n"
        "  /release-notes — 릴리스 노트\n  /insights — 인사이트\n  /extra-usage — 추가 사용량\n"
        "\n<b>CLI 전용 (봇 미지원)</b>\n"
        "  /config — 설정 변경\n  /permissions — 권한 설정\n  /doctor — 진단\n"
        "  /login, /logout — 인증\n  /add-dir — 디렉토리 추가\n  /agents — 에이전트 설정\n")
    send_html(msg)

def handle_skills():
    msg = (
        "<b>사용 가능한 스킬 (OMC)</b>\n" + '━'*25 + "\n"
        "<b>실행 모드</b>\n"
        "  /autopilot — 자율 실행\n  /ralph — 완료까지 반복\n  /ultrawork — 최대 병렬\n"
        "  /ultrapilot — 병렬 자율\n  /ultraqa — QA 반복 사이클\n  /team — 다중 에이전트 협업\n"
        "  /pipeline — 에이전트 체이닝\n  /ccg — Claude+Codex+Gemini\n"
        "\n<b>계획/분석</b>\n"
        "  /plan — 전략적 계획\n  /ralplan — 합의 기반 계획\n  /review — 계획 리뷰\n"
        "  /analyze — 심층 분석\n  /sciomc — 병렬 연구\n  /deepinit — 코드베이스 초기화\n"
        "\n<b>코드 품질</b>\n"
        "  /code-review — 코드 리뷰\n  /security-review — 보안 리뷰\n"
        "  /tdd — 테스트 주도 개발\n  /build-fix — 빌드 오류 수정\n"
        "\n<b>유틸리티</b>\n"
        "  /note — 메모 저장\n  /learner — 스킬 추출\n  /skill — 스킬 관리\n"
        "  /trace — 에이전트 추적\n  /hud — HUD 설정\n  /external-context — 외부 문서 검색\n"
        "  /writer-memory — 작가 메모리\n"
        "\n<b>설정/관리</b>\n"
        "  /omc-setup — OMC 설정\n  /omc-doctor — OMC 진단\n  /mcp-setup — MCP 설정\n"
        "  /ralph-init — PRD 초기화\n  /configure-notifications — 알림 설정\n"
        "  /learn-about-omc — 사용 패턴 분석\n  /cancel — 실행 모드 취소\n"
        "\n<b>봇 관리</b>\n"
        "  /update_bot — 봇 자동 업데이트 (GitHub에서 최신 코드 다운로드)\n"
        + '━'*25 + "\n<i>예: /autopilot 로그인 기능 만들어줘</i>")
    send_html(msg)

def handle_help():
    session_info = f"<code>{state.session_id[:8]}</code>" if state.session_id else "없음"
    model_info = escape_html(state.model) if state.model else "기본값 (sonnet)"
    msg = (
        "<b>Claude Code Telegram Bot</b>\n" + '━'*25 + "\n\n"
        "<b>사용법</b>\n"
        "텍스트를 보내면 Claude와 대화합니다.\n"
        "사진이나 파일을 첨부하면 자동으로 분석합니다.\n"
        "스킬 명령어(/autopilot 등)를 보내면 Claude가 해당 스킬을 실행합니다.\n\n"
        "<b>세션</b>\n"
        "봇은 세션 단위로 대화 맥락을 유지합니다.\n"
        "첫 메시지를 보내면 자동으로 세션이 생성되고,\n"
        "이후 메시지는 같은 세션에서 이어집니다.\n"
        "/session 으로 이전 세션에 다시 연결하거나\n"
        "/clear 로 새 세션을 시작할 수 있습니다.\n\n"
        "<b>모델</b>\n"
        "/model opus, /model sonnet, /model haiku\n"
        "/model default 로 기본값 복원\n\n"
        "<b>명령어 안내</b>\n"
        "/builtin — CLI 빌트인 명령어 목록\n"
        "/skills — OMC 스킬 목록\n"
        "/update_bot — 봇 자동 업데이트\n\n"
        "<b>예시</b>\n"
        "<code>mutation.go 파일 분석해줘</code>\n"
        "<code>/autopilot 로그인 기능 만들어줘</code>\n"
        "<code>/plan 리팩토링 전략 세워줘</code>\n"
        "<code>/code_review prog/rand.go</code>\n\n"
        + '━'*25 + f"\n세션: {session_info} | 모델: <code>{model_info}</code>\n")
    send_html(msg)

def _save_settings():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["settings"] = settings
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
    except Exception:
        pass

SETTINGS_KEYS = [
    ("show_cost", "요청별 비용 표시", "응답 후 비용/토큰 정보"),
    ("show_status", "작업 상태 메시지", "처리 중 도구 사용 상태"),
    ("show_global_cost", "전체 비용 표시", "/cost 전체 세션 누적"),
    ("show_remote_tokens", "다른 PC 토큰 합산", "응답 footer에 연결된 PC 토큰 합산"),
]

def _settings_keyboard():
    rows = []
    for key, label, desc in SETTINGS_KEYS:
        mark = "ON" if settings[key] else "OFF"
        rows.append([{"text": f"[{mark}]  {label}", "callback_data": f"stg:{key}"}])
    cur = settings.get("token_display", "month")
    token_row = []
    for k in TOKEN_PERIODS:
        label = f"[{TOKEN_LABELS[k]}]" if k == cur else TOKEN_LABELS[k]
        token_row.append({"text": label, "callback_data": f"stg:td:{k}"})
    rows.append(token_row)
    rows.append([{"text": "닫기", "callback_data": "stg:close"}])
    return json.dumps({"inline_keyboard": rows})

def _settings_text():
    lines = []
    for key, label, desc in SETTINGS_KEYS:
        mark = "ON " if settings[key] else "OFF"
        lines.append(f"  <code>[{mark}]</code> <b>{escape_html(label)}</b>\n          <i>{escape_html(desc)}</i>")
    cur = settings.get("token_display", "month")
    period_str = " / ".join(f"<b>{v}</b>" if k == cur else v for k, v in TOKEN_LABELS.items())
    lines.append(f"  <code>[{TOKEN_LABELS[cur]:^3}]</code> <b>토큰 표시 범위</b>\n          <i>{period_str}</i>")
    body = "\n\n".join(lines)
    return f"<b>Settings</b>\n{'━'*25}\n\n{body}\n\n{'━'*25}\n<i>항목을 눌러 전환</i>"

def handle_settings(text):
    params = {
        "chat_id": CHAT_ID,
        "text": _settings_text(),
        "parse_mode": "HTML",
        "reply_markup": _settings_keyboard(),
    }
    tg_api("sendMessage", params)

def handle_settings_callback(callback_id, msg_id, data):
    key = data.split(":", 1)[1]
    if key == "close":
        tg_api("deleteMessage", {"chat_id": CHAT_ID, "message_id": msg_id})
        tg_api("answerCallbackQuery", {"callback_query_id": callback_id})
        return
    if key.startswith("td:"):
        new_period = key.split(":", 1)[1]
        if new_period in TOKEN_PERIODS:
            settings["token_display"] = new_period
            _save_settings()
            tg_api("answerCallbackQuery", {"callback_query_id": callback_id, "text": f"토큰: {TOKEN_LABELS[new_period]}"})
    elif key in settings:
        settings[key] = not settings[key]
        _save_settings()
        status = "ON" if settings[key] else "OFF"
        label = next((l for k, l, d in SETTINGS_KEYS if k == key), key)
        tg_api("answerCallbackQuery", {"callback_query_id": callback_id, "text": f"{label}: {status}"})
    else:
        return
    tg_api("editMessageText", {
        "chat_id": CHAT_ID,
        "message_id": msg_id,
        "text": _settings_text(),
        "parse_mode": "HTML",
        "reply_markup": _settings_keyboard(),
    })

def handle_pwd():
    send_html(f"<b>작업 디렉토리</b>\n<code>{escape_html(WORK_DIR)}</code>")

def handle_cd(text):
    global WORK_DIR
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        send_html(f"<b>현재:</b> <code>{escape_html(WORK_DIR)}</code>\n<b>사용법:</b> /cd 경로")
        return
    target = parts[1].strip()
    if target == "~":
        target = os.path.expanduser("~")
    elif target == "-":
        target = getattr(state, "prev_dir", WORK_DIR)
    elif target == "..":
        target = os.path.dirname(WORK_DIR)
    elif not os.path.isabs(target):
        target = os.path.join(WORK_DIR, target)
    target = os.path.normpath(target)
    if not os.path.isdir(target):
        send_html(f"<b>오류:</b> 디렉토리를 찾을 수 없습니다\n<code>{escape_html(target)}</code>")
        return
    state.prev_dir = WORK_DIR
    WORK_DIR = target
    send_html(f"<b>이동 완료</b>\n<code>{escape_html(WORK_DIR)}</code>")

def handle_ls(text):
    args = text.split()[1:]
    show_all = False; target = WORK_DIR
    for arg in args:
        if arg.startswith("-"):
            if "a" in arg: show_all = True
        else:
            target = arg
    if not os.path.isabs(target):
        target = os.path.join(WORK_DIR, target)
    target = os.path.normpath(target)
    if not os.path.isdir(target):
        send_html(f"<b>오류:</b> 디렉토리를 찾을 수 없습니다\n<code>{escape_html(target)}</code>")
        return
    try:
        entries = os.listdir(target)
    except PermissionError:
        send_html(f"<b>오류:</b> 접근 권한이 없습니다\n<code>{escape_html(target)}</code>")
        return
    if not show_all:
        entries = [e for e in entries if not e.startswith(".")]
    dirs = []; files = []
    for name in sorted(entries, key=str.lower):
        full = os.path.join(target, name)
        if os.path.isdir(full):
            dirs.append(f"📁 {name}/")
        else:
            try:
                size = os.path.getsize(full)
                if size < 1024: s = f"{size}B"
                elif size < 1048576: s = f"{size/1024:.1f}K"
                else: s = f"{size/1048576:.1f}M"
                files.append(f"📄 {name}  ({s})")
            except Exception:
                files.append(f"📄 {name}")
    if not dirs and not files:
        send_html(f"<b>{escape_html(os.path.basename(target))}/</b>\n(빈 디렉토리)")
        return
    lines = dirs + files
    total = len(lines)
    if total > 50:
        lines = lines[:50]
        lines.append(f"... 외 {total - 50}개")
    body = "\n".join(escape_html(l) for l in lines)
    send_html(f"<b>{escape_html(target)}</b>\n<pre>{body}</pre>\n<i>{len(dirs)}개 폴더, {len(files)}개 파일</i>")

def handle_cancel():
    with state.lock: proc = state.claude_proc; was_busy = state.busy
    if proc and proc.poll() is None:
        if IS_WINDOWS:
            proc.terminate()  # Windows: terminate instead of kill for cleaner shutdown
        else:
            proc.kill()
        with state.lock: state.claude_proc = None; state.busy = False
        send_html("<b>취소됨.</b> 실행 중인 프로세스를 종료했습니다.")
    elif was_busy:
        with state.lock: state.busy = False
        send_html("<b>초기화.</b> 대기 상태를 해제했습니다.")
    else:
        send_html("실행 중인 작업이 없습니다.")

def handle_selection(text):
    text = text.strip()
    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(state.session_list):
            sid, ts, preview = state.session_list[idx]
            state.session_id = sid; state.selecting = False
            _save_session_id(sid)
            sess_model = _get_session_model(sid)
            if sess_model: state.model = sess_model
            p = preview[:60] + "..." if len(preview) > 60 else preview
            model_line = f"\n모델: <code>{escape_html(state.model or 'default')}</code>" if sess_model else ""
            send_html(
                f"<b>세션 연결됨</b>\nID: <code>{sid[:8]}</code>\n"
                f"시간: {escape_html(ts)}\n미리보기: {escape_html(p)}{model_line}\n"
                f"{'━'*25}\n메시지를 보내면 이 세션에서 대화를 이어갑니다.")
        else:
            send_html(f"잘못된 번호입니다. 1-{len(state.session_list)} 사이를 입력하세요.")
        return
    uuid_pat = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
    if uuid_pat.match(text):
        found = False
        for proj_dir in _find_project_dirs():
            if os.path.exists(os.path.join(proj_dir, f"{text}.jsonl")):
                found = True; break
        if found:
            state.session_id = text; state.selecting = False
            _save_session_id(text)
            sess_model = _get_session_model(text)
            if sess_model: state.model = sess_model
            model_info = f" | 모델: {escape_html(sess_model)}" if sess_model else ""
            send_html(f"<b>세션 연결됨</b> <code>{text[:8]}</code>{model_info}")
        else:
            send_html("세션을 찾을 수 없습니다. UUID를 확인하세요.")
        return
    state.selecting = False
    handle_message(text)

def handle_message(text):
    with state.lock:
        if state.busy:
            send_html("<i>Claude가 처리 중입니다. /cancel 로 취소할 수 있습니다.</i>"); return
        state.busy = True
    # Animated typing indicator (with cancel hint)
    typing_id = [None]
    typing_stop = threading.Event()
    r = send_html("<b>입력중 ·</b>\n<i>/cancel 로 취소</i>")
    try: typing_id[0] = r
    except Exception: pass
    def _typing_anim():
        dots = ["·", "··", "···"]
        i = 0
        while not typing_stop.is_set():
            typing_stop.wait(0.1)
            if typing_stop.is_set(): break
            i = (i + 1) % len(dots)
            if typing_id[0]:
                tg_api("editMessageText", {
                    "chat_id": CHAT_ID, "message_id": typing_id[0],
                    "text": f"<b>입력중 {dots[i]}</b>\n<i>/cancel 로 취소</i>",
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
            token_footer = _token_footer()
            typing_stop.set()
            delete_msg(typing_id[0])
            if questions:
                _show_questions(questions, active_sid)
                if output and output not in ("(빈 응답)",):
                    header = "Claude"
                    if active_sid: header += f" [{active_sid[:8]}]"
                    send_long(header, output, footer=token_footer)
                return
            if not output: return
            header = "Claude"
            if active_sid: header += f" [{active_sid[:8]}]"
            send_long(header, output, footer=token_footer)
            log.info("Response sent to Telegram")
        except Exception as e:
            log.error("handle_message error: %s", e, exc_info=True)
            typing_stop.set()
            delete_msg(typing_id[0])
            send_html(f"<i>오류: {escape_html(str(e))}</i>")
        finally:
            with state.lock: state.busy = False
    threading.Thread(target=_run, daemon=True).start()

# --- Main loop ---
def process_update(update):
    cb = update.get("callback_query")
    if cb:
        cb_chat = str(cb.get("message", {}).get("chat", {}).get("id", ""))
        if cb_chat != CHAT_ID: return
        data = cb.get("data", "")
        if data.startswith("stg:"):
            msg_id = cb.get("message", {}).get("message_id")
            handle_settings_callback(cb["id"], msg_id, data)
        if data.startswith("tt:"):
            msg_id = cb.get("message", {}).get("message_id")
            handle_total_tokens_callback(cb["id"], msg_id, data)
        return
    msg = update.get("message")
    if not msg: return
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if chat_id != CHAT_ID:
        log.warning("Unauthorized: %s", chat_id); return
    text = msg.get("text", "").strip()
    caption = msg.get("caption", "").strip()
    photos = msg.get("photo")
    if photos:
        best = max(photos, key=lambda p: p.get("file_size", 0))
        local = download_tg_file(best["file_id"])
        if local:
            prompt = build_file_prompt(local, caption or "이 이미지를 분석해줘")
            log.info("Photo received: %s", local); handle_message(prompt)
        else: send_html("<i>사진 다운로드 실패</i>")
        return
    doc = msg.get("document")
    if doc:
        fname = doc.get("file_name", "file")
        local = download_tg_file(doc["file_id"], fname)
        if local:
            prompt = build_file_prompt(local, caption or "이 파일 내용을 분석해줘")
            log.info("Document received: %s -> %s", fname, local); handle_message(prompt)
        else: send_html("<i>파일 다운로드 실패</i>")
        return
    if not text: return
    log.info("Received: %s", text[:100])
    lower = text.lower()
    if lower in ("/session", "/sessions"): handle_session()
    elif lower in ("/clear", "/new"): handle_clear()
    elif lower.startswith("/model"): handle_model(text)
    elif lower == "/cost": handle_cost()
    elif lower == "/status": handle_status()
    elif lower == "/builtin": handle_builtin()
    elif lower == "/skills": handle_skills()
    elif lower in ("/help", "/start"): handle_help()
    elif lower == "/cancel": handle_cancel()
    elif lower.startswith("/settings"): handle_settings(text)
    elif lower == "/pwd": handle_pwd()
    elif lower.startswith("/cd"): handle_cd(text)
    elif lower.startswith("/ls"): handle_ls(text)
    elif lower in ("/update_bot", "/update"): handle_update_bot()
    elif lower in ("/total_tokens", "/totaltokens"): handle_total_tokens()
    elif lower == "/cancel_connect":
        state.waiting_token_input = False
        send_html("연결 취소됨.")
    elif state.waiting_token_input: _handle_token_input(text)
    elif state.answering: handle_answer(text)
    elif state.selecting: handle_selection(text)
    else:
        if text.startswith("/") and "_" in text.split()[0]:
            parts = text.split(maxsplit=1)
            parts[0] = parts[0].replace("_", "-")
            text = " ".join(parts)
        handle_message(text)

BOT_COMMANDS = [
    ("help", "도움말"),
    ("session", "세션 목록 및 선택"),
    ("clear", "새 세션 시작"),
    ("model", "모델 변경 (opus/sonnet/haiku)"),
    ("cost", "비용 정보"),
    ("status", "현재 상태 확인"),
    ("settings", "봇 설정"),
    ("builtin", "CLI 빌트인 명령어 목록"),
    ("skills", "OMC 스킬 목록"),
    ("cancel", "실행 중인 작업 취소"),
    ("pwd", "현재 작업 디렉토리"),
    ("cd", "디렉토리 이동"),
    ("ls", "파일/폴더 목록"),
    ("update_bot", "봇 업데이트"),
    ("total_tokens", "전체 PC 토큰 사용량 집계"),
    ("analyze", "심층 분석 및 디버깅"),
    ("autopilot", "자율 실행 (아이디어에서 코드까지)"),
    ("build_fix", "빌드 오류 수정"),
    ("ccg", "Claude-Codex-Gemini 트리모델 오케스트레이션"),
    ("code_review", "코드 리뷰"),
    ("configure_notifications", "알림 설정 (Telegram/Discord/Slack)"),
    ("deepinit", "코드베이스 초기화"),
    ("external_context", "외부 문서 검색"),
    ("hud", "HUD 디스플레이 설정"),
    ("learn_about_omc", "OMC 사용 패턴 분석"),
    ("learner", "스킬 추출"),
    ("mcp_setup", "MCP 서버 설정"),
    ("note", "노트패드 메모 저장"),
    ("omc_doctor", "OMC 진단"),
    ("omc_setup", "OMC 설정"),
    ("pipeline", "에이전트 체이닝"),
    ("plan", "전략적 계획 수립"),
    ("project_session_manager", "프로젝트 세션 관리"),
    ("ralph", "완료까지 반복 실행"),
    ("ralph_init", "PRD 초기화"),
    ("ralplan", "합의 기반 계획"),
    ("release", "OMC 릴리스"),
    ("review", "계획 리뷰"),
    ("sciomc", "병렬 분석"),
    ("security_review", "보안 리뷰"),
    ("skill", "스킬 관리"),
    ("tdd", "테스트 주도 개발"),
    ("team", "다중 에이전트 협업"),
    ("trace", "에이전트 추적"),
    ("ultraqa", "QA 반복 사이클"),
    ("ultrapilot", "병렬 자율 실행"),
    ("ultrawork", "최대 병렬 실행"),
    ("writer_memory", "작가 메모리 시스템"),
    ("compact", "컨텍스트 압축"),
]

def _sync_bot_commands():
    """Register bot commands with BotFather on startup."""
    try:
        commands = [{"command": c, "description": d} for c, d in BOT_COMMANDS]
        tg_api("setMyCommands", {"commands": json.dumps(commands)})
        log.info("BotFather commands synced (%d commands)", len(commands))
    except Exception as e:
        log.warning("Failed to sync commands: %s", e)

def poll_loop():
    offset = 0
    log.info("Bot started.")
    _sync_bot_commands()
    # Start token data publishing thread
    def _token_publish_loop():
        time.sleep(10)  # Wait for bot to stabilize
        while True:
            try:
                _publish_token_data()
            except Exception as e:
                log.error("Token publish error: %s", e)
            time.sleep(PUBLISH_INTERVAL)
    threading.Thread(target=_token_publish_loop, daemon=True).start()
    log.info("Token publish thread started (interval: %ds)", PUBLISH_INTERVAL)
    state.global_tokens = _get_monthly_tokens()
    log.info("Monthly tokens loaded: %d", state.global_tokens)
    send_html("<b>Claude Code Bot 시작됨</b>\n/help 로 명령어를 확인하세요.")
    while True:
        try:
            result = tg_api("getUpdates", {"offset": offset, "timeout": POLL_TIMEOUT, "allowed_updates": json.dumps(["message", "callback_query"])})
            if not result or not result.get("ok"):
                log.warning("getUpdates failed"); time.sleep(5); continue
            for update in result.get("result", []):
                offset = update["update_id"] + 1
                try: process_update(update)
                except Exception as e: log.error("Update error: %s", e, exc_info=True)
        except KeyboardInterrupt: break
        except Exception as e:
            log.error("Poll error: %s", e, exc_info=True); time.sleep(5)

def main():
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
        try: signal.signal(signal.SIGBREAK, sig_handler)
        except (AttributeError, OSError): pass
    poll_loop()

if __name__ == "__main__":
    main()
