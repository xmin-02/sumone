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

MAX_MSG_LEN = 3900
MAX_PARTS = 5
POLL_TIMEOUT = 30

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")
logging.basicConfig(
    filename=LOG_FILE, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
)
log = logging.getLogger("tg-bot")

# --- State ---
class State:
    session_id = None
    selecting = False
    answering = False
    session_list = []
    pending_question = None
    claude_proc = None
    busy = False
    model = None
    total_cost = 0.0
    last_cost = 0.0
    lock = threading.Lock()

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

def send_text(text, parse_mode=None):
    params = {"chat_id": CHAT_ID, "text": text}
    if parse_mode: params["parse_mode"] = parse_mode
    return tg_api("sendMessage", params)

def send_html(text):
    result = send_text(text, parse_mode="HTML")
    if not result or not result.get("ok"):
        send_text(re.sub(r"<[^>]+>", "", text))

def send_long(header, body_md):
    html_body = md_to_telegram_html(body_md)
    chunks = split_message(html_body)
    total = len(chunks)
    for i, chunk in enumerate(chunks):
        part = f" ({i+1}/{total})" if total > 1 else ""
        send_html(f"<b>{escape_html(header)}{part}</b>\n{'━'*20}\n{chunk}")
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
                                log.info("AskUserQuestion detected: %d questions", len(qs))
                if has_tool_use:
                    unsent = final_text[sent_text_count:]
                    if unsent:
                        combined = "\n\n".join(unsent)
                        if len(combined) > 30:
                            _send_intermediate(combined)
                            log.info("Intermediate text: %d chars", len(combined))
                        sent_text_count = len(final_text)
                now = time.time()
                if now - last_status_time >= 5:
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
                    dur_s = duration / 1000 if duration else 0
                    mins, secs = divmod(int(dur_s), 60)
                    dur_str = f"{mins}분 {secs}초" if mins > 0 else f"{secs}초"
                    cost_line = f"\U0001f4b0 ${cost:.4f} | \u23f1 {dur_str} | \U0001f504 {turns}턴 | \U0001f4ca {in_tok:,}+{out_tok:,} 토큰"
                    send_html(f"<i>{cost_line}</i>")
        proc.wait(timeout=30)
        stderr_out = proc.stderr.read().decode("utf-8", errors="replace").strip()
        with state.lock: state.claude_proc = None
        unsent = final_text[sent_text_count:]
        output = "\n\n".join(unsent).strip()
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
            if sid: state.session_id = sid
            answer_text = f'"{label}" 을 선택합니다.'
            log.info("Answer: %s (option %d)", label, idx + 1)
            handle_message(answer_text); return
        else:
            send_html(f"잘못된 번호입니다. 1-{len(options_map)} 사이를 입력하세요."); return
    state.answering = False; state.pending_question = None
    if sid: state.session_id = sid
    handle_message(text)

def handle_clear():
    state.session_id = None; state.selecting = False
    state.answering = False; state.pending_question = None
    send_html("<b>대화 초기화</b>\n이전 맥락 없이 새 대화를 시작합니다.")

def handle_cost():
    msg = (f"<b>비용 정보</b>\n{'━'*25}\n"
           f"마지막 요청: ${state.last_cost:.4f}\n"
           f"봇 세션 누적: ${state.total_cost:.4f}\n")
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
    current_path = os.path.abspath(__file__)
    local_mtime = os.path.getmtime(current_path)
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(local_mtime))
    file_path = f"bot/telegram-bot-{LANG}.py"
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/commits?path={file_path}&since={since}&per_page=20"
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

def handle_update_bot():
    send_html("<i>업데이트 확인 중...</i>")
    bot_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/bot/telegram-bot-{LANG}.py"
    current_path = os.path.abspath(__file__)
    new_path = current_path + ".new"
    try:
        urllib.request.urlretrieve(bot_url, new_path)
        with open(current_path, encoding="utf-8") as f:
            old_content = f.read()
        with open(new_path, encoding="utf-8") as f:
            new_content = f.read()
        if old_content == new_content:
            os.remove(new_path)
            send_html("<b>이미 최신 버전입니다.</b>")
            return
        patch_notes = _fetch_patch_notes()
        os.replace(new_path, current_path)
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
    send_html("<i>요청 접수됨. /cancel 로 언제든 취소 가능합니다.</i>")
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
                log.info("Auto-connected to session: %s", new_sid)
            active_sid = state.session_id or new_sid or sid
            if questions:
                _show_questions(questions, active_sid)
                if output and output not in ("(빈 응답)",):
                    header = "Claude"
                    if active_sid: header += f" [{active_sid[:8]}]"
                    send_long(header, output)
                return
            if not output: return
            header = "Claude"
            if active_sid: header += f" [{active_sid[:8]}]"
            send_long(header, output)
            log.info("Response sent to Telegram")
        except Exception as e:
            log.error("handle_message error: %s", e, exc_info=True)
            send_html(f"<i>오류: {escape_html(str(e))}</i>")
        finally:
            with state.lock: state.busy = False
    threading.Thread(target=_run, daemon=True).start()

# --- Main loop ---
def process_update(update):
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
    elif lower in ("/update_bot", "/update"): handle_update_bot()
    elif state.answering: handle_answer(text)
    elif state.selecting: handle_selection(text)
    else:
        if text.startswith("/") and "_" in text.split()[0]:
            parts = text.split(maxsplit=1)
            parts[0] = parts[0].replace("_", "-")
            text = " ".join(parts)
        handle_message(text)

def poll_loop():
    offset = 0
    log.info("Bot started.")
    send_html("<b>Claude Code Bot 시작됨</b>\n/help 로 명령어를 확인하세요.")
    while True:
        try:
            result = tg_api("getUpdates", {"offset": offset, "timeout": POLL_TIMEOUT, "allowed_updates": "message"})
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
