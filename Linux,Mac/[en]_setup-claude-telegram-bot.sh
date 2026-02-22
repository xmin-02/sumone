#!/usr/bin/env bash
# ============================================================================
# Setup script for Telegram Claude Code bot
# Installs a bot for remote Claude Code interaction via Telegram.
#
# Supported OS: Linux, macOS, Windows (WSL)
# Requirements: Python 3.8+, Claude Code CLI (claude)
# ============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }

# ---------------------------------------------------------------------------
# OS Detection
# ---------------------------------------------------------------------------
detect_os() {
    case "$(uname -s)" in
        Linux*)
            if grep -qi microsoft /proc/version 2>/dev/null; then
                OS="wsl"
            else
                OS="linux"
            fi
            ;;
        Darwin*)  OS="macos" ;;
        MINGW*|MSYS*|CYGWIN*) OS="windows-git-bash" ;;
        *)        OS="unknown" ;;
    esac
    echo "$OS"
}

# ---------------------------------------------------------------------------
# Prerequisites check
# ---------------------------------------------------------------------------
check_prerequisites() {
    info "Checking required programs..."

    # Python 3
    if command -v python3 &>/dev/null; then
        PYTHON="python3"
    elif command -v python &>/dev/null && python --version 2>&1 | grep -q "Python 3"; then
        PYTHON="python"
    else
        err "Python 3 is not installed."
        echo "  - Linux: sudo apt install python3"
        echo "  - macOS: brew install python3"
        echo "  - Windows: https://python.org/downloads/"
        exit 1
    fi
    ok "Python: $($PYTHON --version)"

    # Claude CLI
    if command -v claude &>/dev/null; then
        ok "Claude CLI: $(claude --version 2>/dev/null || echo 'installed')"
    else
        warn "Claude CLI is not installed."
        echo ""
        echo "How to install Claude CLI:"
        echo "  npm install -g @anthropic-ai/claude-code"
        echo ""
        read -rp "$(echo -e "${YELLOW}Continue without Claude CLI? (y/N): ${NC}")" yn
        case "$yn" in
            [yY]*) warn "You will need to install Claude CLI later for the bot to work." ;;
            *)     echo "Please install it and run this script again."; exit 0 ;;
        esac
    fi
}

# ---------------------------------------------------------------------------
# User input
# ---------------------------------------------------------------------------
get_user_input() {
    echo ""
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD} Telegram Bot Configuration${NC}"
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo "1. Create a bot with @BotFather and get the token"
    echo "   Telegram → @BotFather → /newbot → copy token"
    echo ""
    echo "2. Get your Chat ID from @userinfobot"
    echo "   Telegram → @userinfobot → /start → copy ID"
    echo ""

    # Bot Token
    while true; do
        read -rp "$(echo -e "${CYAN}Bot Token: ${NC}")" BOT_TOKEN
        if [[ "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
            break
        fi
        err "Invalid token format. (e.g. 123456789:ABCdef...)"
    done

    # Chat ID
    while true; do
        read -rp "$(echo -e "${CYAN}Chat ID: ${NC}")" CHAT_ID
        if [[ "$CHAT_ID" =~ ^-?[0-9]+$ ]]; then
            break
        fi
        err "Invalid Chat ID. (numbers only)"
    done

    # Working directory
    DEFAULT_WORKDIR="$HOME"
    read -rp "$(echo -e "${CYAN}Working directory [$DEFAULT_WORKDIR]: ${NC}")" WORK_DIR
    WORK_DIR="${WORK_DIR:-$DEFAULT_WORKDIR}"
    if [[ ! -d "$WORK_DIR" ]]; then
        err "Directory does not exist: $WORK_DIR"
        exit 1
    fi

    echo ""
    info "Configuration summary:"
    echo "  Bot Token: ${BOT_TOKEN:0:10}..."
    echo "  Chat ID:   $CHAT_ID"
    echo "  Working directory: $WORK_DIR"
    echo ""
    read -rp "$(echo -e "${YELLOW}Proceed with this configuration? (Y/n): ${NC}")" confirm
    case "$confirm" in
        [nN]*) echo "Cancelled."; exit 0 ;;
    esac
}

# ---------------------------------------------------------------------------
# Install bot script
# ---------------------------------------------------------------------------
INSTALL_DIR=""
BOT_PATH=""

install_bot() {
    INSTALL_DIR="$HOME/.claude-telegram-bot"
    BOT_PATH="$INSTALL_DIR/telegram-bot.py"

    mkdir -p "$INSTALL_DIR"
    info "Installing bot script: $BOT_PATH"

    cat > "$BOT_PATH" << 'BOTSCRIPT'
#!/usr/bin/env python3
"""Telegram bot for bidirectional Claude Code interaction.

Commands:
  /session  - List recent sessions, enter selection mode
  /clear    - Clear session, start fresh
  /model    - Change or show current model
  /cost     - Show cost info
  /status   - Show bot status
  /builtin  - List CLI built-in commands
  /skills   - List OMC skills
  /help     - Usage guide
  /cancel   - Cancel running claude process
  <number>  - (During selection/answering) Select that option
  <text>    - Send message to Claude via CLI
"""
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Config (replaced by setup script) ---
BOT_TOKEN = "%%BOT_TOKEN%%"
CHAT_ID = "%%CHAT_ID%%"
WORK_DIR = "%%WORK_DIR%%"
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
DOWNLOAD_DIR = os.path.expanduser("~/.claude-telegram-bot/downloads")

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
        prompt = f"Please analyze this image file: {local_path}"
        if caption: prompt = f"{caption}\n\nFile: {local_path}"
        return prompt
    if ext in TEXT_EXTS or ext == "":
        try:
            with open(local_path, "r", errors="replace") as f:
                content = f.read(50000)
            truncated = " (partially included)" if len(content) >= 50000 else ""
            return f"{caption or 'Please analyze this file content'}\n\n--- {fname}{truncated} ---\n{content}"
        except Exception: pass
    return f"{caption or 'Please analyze this file'}\n\nFile path: {local_path}"

# --- Session listing ---
def _find_project_dirs():
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
        with open(fpath) as f:
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
    return "(no preview)"

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
TOOL_LABELS = {
    "Read": "Reading file", "Edit": "Editing file", "Write": "Creating file",
    "Bash": "Running command", "Grep": "Searching code", "Glob": "Browsing files",
    "Task": "Running agent", "WebFetch": "Fetching web page", "WebSearch": "Searching web",
    "AskUserQuestion": "Generating question", "TodoWrite": "Updating task list",
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
            else: label += f" ({len(todos)} items)"
        return label
    return None

def run_claude(message, session_id=None):
    cmd = ["claude"]
    if session_id: cmd += ["-r", session_id]
    cmd += ["-p", message, "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]
    if state.model: cmd += ["--model", state.model]
    log.info("Running: %s", " ".join(cmd[:6]) + "...")
    try:
        with state.lock:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=WORK_DIR, env=_claude_env(),
            )
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
                        t = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"
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
                    dur_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"
                    cost_line = f"\U0001f4b0 ${cost:.4f} | \u23f1 {dur_str} | \U0001f504 {turns} turns | \U0001f4ca {in_tok:,}+{out_tok:,} tokens"
                    send_html(f"<i>{cost_line}</i>")
        proc.wait(timeout=30)
        stderr_out = proc.stderr.read().decode("utf-8", errors="replace").strip()
        with state.lock: state.claude_proc = None
        unsent = final_text[sent_text_count:]
        output = "\n\n".join(unsent).strip()
        if proc.returncode != 0 and not output and sent_text_count == 0:
            err_msg = f"Error (code {proc.returncode}):\n{stderr_out[:500]}" if stderr_out else f"Error (code {proc.returncode})"
            return err_msg, captured_session_id, None
        return output or "", captured_session_id, pending_questions
    except subprocess.TimeoutExpired:
        with state.lock:
            if state.claude_proc: state.claude_proc.kill(); state.claude_proc = None
        return "Timed out (1 hour limit)", None, None
    except Exception as e:
        with state.lock: state.claude_proc = None
        return f"Error: {e}", None, None

def _claude_env():
    env = os.environ.copy()
    env["HOME"] = os.path.expanduser("~")
    env["CLAUDE_TELEGRAM_BOT"] = "1"
    env.pop("CLAUDECODE", None)
    env["PATH"] = os.path.expanduser("~/.local/bin") + ":/usr/local/bin:/usr/bin:/bin"
    goroot = os.path.join(WORK_DIR, "goroot")
    gopath = os.path.join(WORK_DIR, "gopath")
    if os.path.isdir(goroot):
        env["GOROOT"] = goroot
        env["PATH"] = f"{gopath}/bin:{goroot}/bin:{env['PATH']}"
    if os.path.isdir(gopath):
        env["GOPATH"] = gopath
    return env

# --- Command handlers ---
def handle_session():
    sessions = get_sessions(10)
    state.session_list = sessions
    state.selecting = True
    if not sessions:
        send_html("<b>No sessions found.</b>"); return
    lines = []
    for i, (sid, ts, preview) in enumerate(sessions, 1):
        p = preview[:50] + "..." if len(preview) > 50 else preview
        lines.append(f"<b>{i}.</b> <code>{sid[:8]}</code> {escape_html(ts)}\n    {escape_html(p)}")
    current = ""
    if state.session_id: current = f"\nCurrent: <code>{state.session_id[:8]}</code>"
    msg = (f"<b>Recent Sessions</b>{current}\n{'━'*25}\n"
           + "\n".join(lines)
           + f"\n{'━'*25}\nEnter a number (1-10) or a session UUID.\n/clear to start a new session")
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
    msg = (f"<b>Claude — Selection Required</b>\n{'━'*25}\n{body}\n{'━'*25}\n"
           "Enter a number to select an option.")
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
            answer_text = f'I select "{label}".'
            log.info("Answer: %s (option %d)", label, idx + 1)
            handle_message(answer_text); return
        else:
            send_html(f"Invalid number. Please enter a value between 1 and {len(options_map)}."); return
    state.answering = False; state.pending_question = None
    if sid: state.session_id = sid
    handle_message(text)

def handle_clear():
    state.session_id = None; state.selecting = False
    state.answering = False; state.pending_question = None
    send_html("<b>Conversation cleared</b>\nStarting a new conversation with no prior context.")

def handle_cost():
    msg = (f"<b>Cost Info</b>\n{'━'*25}\n"
           f"Last request: ${state.last_cost:.4f}\n"
           f"Bot session total: ${state.total_cost:.4f}\n")
    send_html(msg)

def handle_model(text):
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or parts[1].strip() == "":
        current = state.model or "default (sonnet)"
        aliases = ", ".join(sorted(MODEL_ALIASES.keys()))
        send_html(
            f"<b>Current model:</b> <code>{escape_html(current)}</code>\n{'━'*25}\n"
            f"<b>Usage:</b> /model [name]\n<b>Aliases:</b> {escape_html(aliases)}\n"
            f"<b>Examples:</b>\n  /model opus\n  /model sonnet\n  /model haiku\n"
            f"  /model default — restore default")
        return
    name = parts[1].strip().lower()
    if name in ("default", "reset"):
        state.model = None
        send_html("<b>Model reset:</b> default (sonnet)"); return
    resolved = MODEL_ALIASES.get(name)
    if not resolved:
        if name.startswith("claude-"): resolved = name
        else:
            aliases = ", ".join(sorted(MODEL_ALIASES.keys()))
            send_html(f"Unknown model: <code>{escape_html(name)}</code>\nAvailable: {escape_html(aliases)}"); return
    state.model = resolved
    send_html(f"<b>Model changed:</b> <code>{escape_html(resolved)}</code>")

def handle_status():
    session_info = f"<code>{state.session_id[:8]}</code>" if state.session_id else "None (new session mode)"
    model_info = f"<code>{escape_html(state.model)}</code>" if state.model else "default (sonnet)"
    busy_info = "Processing" if state.busy else "Idle"
    msg = (f"<b>Bot Status</b>\n{'━'*25}\n"
           f"Session: {session_info}\nModel: {model_info}\nState: {busy_info}\n")
    send_html(msg)

def handle_builtin():
    msg = (
        "<b>Built-in Commands (CLI)</b>\n" + '━'*25 + "\n"
        "<b>Handled by bot</b>\n"
        "  /clear — Clear conversation\n  /cost — Check cost\n  /model — Change/check model\n"
        "  /session — Select session\n  /status — Check status\n  /cancel — Cancel task\n"
        "\n<b>Passed to Claude</b>\n"
        "  /compact — Compress context\n  /context — Context usage\n  /init — Initialize project\n"
        "  /review — Code review\n  /security-review — Security review\n  /pr-comments — PR comments\n"
        "  /release-notes — Release notes\n  /insights — Insights\n  /extra-usage — Extra usage\n"
        "\n<b>CLI only (not supported by bot)</b>\n"
        "  /config — Change settings\n  /permissions — Permission settings\n  /doctor — Diagnostics\n"
        "  /login, /logout — Authentication\n  /add-dir — Add directory\n  /agents — Agent settings\n")
    send_html(msg)

def handle_skills():
    msg = (
        "<b>Available Skills (OMC)</b>\n" + '━'*25 + "\n"
        "<b>Execution Modes</b>\n"
        "  /autopilot — Autonomous execution\n  /ralph — Repeat until complete\n  /ultrawork — Maximum parallelism\n"
        "  /ultrapilot — Parallel autonomous\n  /ultraqa — QA iteration cycle\n  /team — Multi-agent collaboration\n"
        "  /pipeline — Agent chaining\n  /ccg — Claude+Codex+Gemini\n"
        "\n<b>Planning/Analysis</b>\n"
        "  /plan — Strategic planning\n  /ralplan — Consensus-based planning\n  /review — Plan review\n"
        "  /analyze — Deep analysis\n  /sciomc — Parallel research\n  /deepinit — Codebase initialization\n"
        "\n<b>Code Quality</b>\n"
        "  /code-review — Code review\n  /security-review — Security review\n"
        "  /tdd — Test-driven development\n  /build-fix — Fix build errors\n"
        "\n<b>Utilities</b>\n"
        "  /note — Save a note\n  /learner — Extract skills\n  /skill — Skill management\n"
        "  /trace — Agent tracing\n  /hud — HUD settings\n  /external-context — External document search\n"
        "  /writer-memory — Writer memory\n"
        "\n<b>Configuration/Management</b>\n"
        "  /omc-setup — OMC setup\n  /omc-doctor — OMC diagnostics\n  /mcp-setup — MCP setup\n"
        "  /ralph-init — PRD initialization\n  /configure-notifications — Notification settings\n"
        "  /learn-about-omc — Usage pattern analysis\n  /cancel — Cancel execution mode\n"
        + '━'*25 + "\n<i>Example: /autopilot build a login feature</i>")
    send_html(msg)

def handle_help():
    session_info = f"<code>{state.session_id[:8]}</code>" if state.session_id else "None"
    model_info = escape_html(state.model) if state.model else "default (sonnet)"
    msg = (
        "<b>Claude Code Telegram Bot</b>\n" + '━'*25 + "\n\n"
        "<b>Usage</b>\n"
        "Send text to chat with Claude.\n"
        "Attach photos or files to have them analyzed automatically.\n"
        "Send skill commands (/autopilot, etc.) and Claude will execute them.\n\n"
        "<b>Sessions</b>\n"
        "The bot maintains conversation context per session.\n"
        "A session is created automatically with your first message,\n"
        "and subsequent messages continue in the same session.\n"
        "Use /session to reconnect to a previous session,\n"
        "or /clear to start a new one.\n\n"
        "<b>Models</b>\n"
        "/model opus, /model sonnet, /model haiku\n"
        "/model default to restore default\n\n"
        "<b>Command Reference</b>\n"
        "/builtin — List CLI built-in commands\n"
        "/skills — List OMC skills\n\n"
        "<b>Examples</b>\n"
        "<code>Analyze the mutation.go file</code>\n"
        "<code>/autopilot build a login feature</code>\n"
        "<code>/plan create a refactoring strategy</code>\n"
        "<code>/code_review prog/rand.go</code>\n\n"
        + '━'*25 + f"\nSession: {session_info} | Model: <code>{model_info}</code>\n")
    send_html(msg)

def handle_cancel():
    with state.lock: proc = state.claude_proc; was_busy = state.busy
    if proc and proc.poll() is None:
        proc.kill()
        with state.lock: state.claude_proc = None; state.busy = False
        send_html("<b>Cancelled.</b> The running process has been terminated.")
    elif was_busy:
        with state.lock: state.busy = False
        send_html("<b>Reset.</b> Busy state has been cleared.")
    else:
        send_html("No task is currently running.")

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
            model_line = f"\nModel: <code>{escape_html(state.model or 'default')}</code>" if sess_model else ""
            send_html(
                f"<b>Session connected</b>\nID: <code>{sid[:8]}</code>\n"
                f"Time: {escape_html(ts)}\nPreview: {escape_html(p)}{model_line}\n"
                f"{'━'*25}\nSend a message to continue in this session.")
        else:
            send_html(f"Invalid number. Please enter a value between 1 and {len(state.session_list)}.")
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
            model_info = f" | Model: {escape_html(sess_model)}" if sess_model else ""
            send_html(f"<b>Session connected</b> <code>{text[:8]}</code>{model_info}")
        else:
            send_html("Session not found. Please check the UUID.")
        return
    state.selecting = False
    handle_message(text)

def handle_message(text):
    with state.lock:
        if state.busy:
            send_html("<i>Claude is processing. Use /cancel to stop.</i>"); return
        state.busy = True
    send_html("<i>Request received. You can cancel at any time with /cancel.</i>")
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
                if output and output not in ("(empty response)",):
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
            send_html(f"<i>Error: {escape_html(str(e))}</i>")
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
            prompt = build_file_prompt(local, caption or "Please analyze this image")
            log.info("Photo received: %s", local); handle_message(prompt)
        else: send_html("<i>Failed to download photo</i>")
        return
    doc = msg.get("document")
    if doc:
        fname = doc.get("file_name", "file")
        local = download_tg_file(doc["file_id"], fname)
        if local:
            prompt = build_file_prompt(local, caption or "Please analyze this file content")
            log.info("Document received: %s -> %s", fname, local); handle_message(prompt)
        else: send_html("<i>Failed to download file</i>")
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
    send_html("<b>Claude Code Bot started</b>\nSend /help to see available commands.")
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
    signal.signal(signal.SIGTERM, sig_handler)
    signal.signal(signal.SIGINT, sig_handler)
    poll_loop()

if __name__ == "__main__":
    main()
BOTSCRIPT

    # Replace placeholders
    sed -i "s|%%BOT_TOKEN%%|${BOT_TOKEN}|g" "$BOT_PATH"
    sed -i "s|%%CHAT_ID%%|${CHAT_ID}|g" "$BOT_PATH"
    sed -i "s|%%WORK_DIR%%|${WORK_DIR}|g" "$BOT_PATH"

    # macOS sed compatibility
    if [[ "$OS" == "macos" ]]; then
        sed -i '' "s|%%BOT_TOKEN%%|${BOT_TOKEN}|g" "$BOT_PATH" 2>/dev/null || true
        sed -i '' "s|%%CHAT_ID%%|${CHAT_ID}|g" "$BOT_PATH" 2>/dev/null || true
        sed -i '' "s|%%WORK_DIR%%|${WORK_DIR}|g" "$BOT_PATH" 2>/dev/null || true
    fi

    chmod +x "$BOT_PATH"
    ok "Bot script installed: $BOT_PATH"
}

# ---------------------------------------------------------------------------
# Auto-start setup per OS
# ---------------------------------------------------------------------------
setup_autostart_linux() {
    info "Registering systemd service..."
    local service_dir="$HOME/.config/systemd/user"
    mkdir -p "$service_dir"

    cat > "$service_dir/claude-telegram.service" << EOF
[Unit]
Description=Claude Code Telegram Bot

[Service]
ExecStart=$PYTHON $BOT_PATH
WorkingDirectory=$WORK_DIR
Restart=always
RestartSec=5
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=HOME=$HOME

[Install]
WantedBy=default.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable claude-telegram.service
    systemctl --user start claude-telegram.service
    ok "systemd service registered (auto-start enabled)"
    echo ""
    echo "  Check status: systemctl --user status claude-telegram"
    echo "  View logs:    cat $INSTALL_DIR/bot.log"
    echo "  Stop:         systemctl --user stop claude-telegram"
    echo "  Restart:      systemctl --user restart claude-telegram"
}

setup_autostart_macos() {
    info "Registering launchd service..."
    local plist_dir="$HOME/Library/LaunchAgents"
    local plist="$plist_dir/com.claude.telegram-bot.plist"
    mkdir -p "$plist_dir"

    cat > "$plist" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claude.telegram-bot</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$BOT_PATH</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$WORK_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$INSTALL_DIR/bot-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$INSTALL_DIR/bot-stderr.log</string>
</dict>
</plist>
EOF

    launchctl load "$plist" 2>/dev/null || true
    launchctl start com.claude.telegram-bot 2>/dev/null || true
    ok "launchd service registered (auto-start enabled)"
    echo ""
    echo "  Check status: launchctl list | grep claude"
    echo "  View logs:    cat $INSTALL_DIR/bot.log"
    echo "  Stop:         launchctl stop com.claude.telegram-bot"
    echo "  Restart:      launchctl stop com.claude.telegram-bot && launchctl start com.claude.telegram-bot"
}

setup_autostart_wsl() {
    info "Configuring WSL auto-start..."
    # Add to .bashrc for auto-start on WSL login
    local marker="# claude-telegram-bot autostart"
    local start_cmd="(pgrep -f 'telegram-bot.py' > /dev/null 2>&1 || nohup $PYTHON $BOT_PATH > /dev/null 2>&1 &)"

    if ! grep -q "$marker" "$HOME/.bashrc" 2>/dev/null; then
        echo "" >> "$HOME/.bashrc"
        echo "$marker" >> "$HOME/.bashrc"
        echo "$start_cmd" >> "$HOME/.bashrc"
    fi

    # Start now
    eval "$start_cmd"
    ok "WSL auto-start configured (added to .bashrc)"
    echo ""
    echo "  Check status: pgrep -f telegram-bot.py"
    echo "  View logs:    cat $INSTALL_DIR/bot.log"
    echo "  Stop:         pkill -f telegram-bot.py"
}

# ---------------------------------------------------------------------------
# Uninstall info
# ---------------------------------------------------------------------------
print_uninstall() {
    echo ""
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD} How to Uninstall${NC}"
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    case "$OS" in
        linux)
            echo "  systemctl --user stop claude-telegram"
            echo "  systemctl --user disable claude-telegram"
            echo "  rm ~/.config/systemd/user/claude-telegram.service"
            ;;
        macos)
            echo "  launchctl stop com.claude.telegram-bot"
            echo "  launchctl unload ~/Library/LaunchAgents/com.claude.telegram-bot.plist"
            echo "  rm ~/Library/LaunchAgents/com.claude.telegram-bot.plist"
            ;;
        wsl)
            echo "  pkill -f telegram-bot.py"
            echo "  Remove the '# claude-telegram-bot autostart' line from .bashrc"
            ;;
    esac
    echo "  rm -rf $INSTALL_DIR"
    echo ""
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    echo ""
    echo -e "${BOLD}╔═══════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}║  Claude Code Telegram Bot - Setup         ║${NC}"
    echo -e "${BOLD}╚═══════════════════════════════════════════╝${NC}"
    echo ""

    OS=$(detect_os)
    info "OS: $OS"

    if [[ "$OS" == "unknown" || "$OS" == "windows-git-bash" ]]; then
        err "This OS is not directly supported."
        echo "On Windows, please run this inside WSL (Windows Subsystem for Linux)."
        exit 1
    fi

    check_prerequisites
    get_user_input
    install_bot

    # Test bot token
    info "Validating bot token..."
    if $PYTHON -c "
import urllib.request, json
r = urllib.request.urlopen('https://api.telegram.org/bot${BOT_TOKEN}/getMe', timeout=10)
d = json.loads(r.read())
if d.get('ok'): print('Bot: @' + d['result'].get('username', ''))
else: exit(1)
" 2>/dev/null; then
        ok "Token validation successful"
    else
        warn "Token validation failed. Please double-check your token."
    fi

    # Setup autostart
    case "$OS" in
        linux) setup_autostart_linux ;;
        macos) setup_autostart_macos ;;
        wsl)   setup_autostart_wsl ;;
    esac

    print_uninstall

    echo -e "${GREEN}${BOLD}Installation complete!${NC}"
    echo "Send /help in Telegram to get started."
    echo ""
}

main "$@"
