"""Claude CLI integration."""
import json
import os
import subprocess
import threading
import time

import i18n
from config import IS_WINDOWS, WORK_DIR, settings, log
from state import state
from telegram import escape_html, md_to_telegram_html, split_message, send_html, send_typing, tg_api, CHAT_ID


def _find_claude_cmd():
    for cmd in ["claude", "claude.cmd"]:
        try:
            result = subprocess.run(
                [cmd, "--version"], capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
            )
            if result.returncode == 0: return cmd
        except Exception: continue
    return "claude"

CLAUDE_CMD = _find_claude_cmd()


def _claude_env():
    env = os.environ.copy()
    env["CLAUDE_TELEGRAM_BOT"] = "1"
    env.pop("CLAUDECODE", None)
    if IS_WINDOWS:
        npm_prefix = os.path.join(env.get("APPDATA", ""), "npm")
        if os.path.isdir(npm_prefix):
            env["PATH"] = npm_prefix + ";" + env.get("PATH", "")
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


def _describe_tool(event):
    tool_labels = i18n.t("tool_labels")
    if not isinstance(tool_labels, dict):
        tool_labels = {}
    content = event.get("message", {}).get("content", [])
    if not isinstance(content, list): return None
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use": continue
        name = block.get("name", ""); inp = block.get("input", {})
        label = tool_labels.get(name, name)
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
            else: label += f" {i18n.t('todo_count', count=len(todos))}"
        return label
    return None


def _send_intermediate(text):
    html = md_to_telegram_html(text)
    chunks = split_message(html)
    for idx, chunk in enumerate(chunks):
        send_html(f"\U0001f4ad {chunk}")
        if idx < len(chunks) - 1: time.sleep(0.3)


def _format_time(mins, secs):
    t_str = i18n.t("time.format_ms", mins=mins, secs=secs) if mins > 0 else i18n.t("time.format_s", secs=secs)
    return t_str


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
                        t_val = block.get("text", "").strip()
                        if t_val: final_text.append(t_val)
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
                        t_str = _format_time(mins, secs)
                        send_html(f"<i>{escape_html(desc)} ({t_str})</i>")
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
                        dur_str = _format_time(mins, secs)
                        cost_line = i18n.t("cost.line", cost=f"{cost:.4f}", duration=dur_str,
                                           turns=turns, in_tok=f"{in_tok:,}", out_tok=f"{out_tok:,}")
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
            if stderr_out:
                err_msg = i18n.t("error.code", code=proc.returncode, detail=stderr_out[:500])
            else:
                err_msg = i18n.t("error.code_short", code=proc.returncode)
            return err_msg, captured_session_id, None
        return output or "", captured_session_id, pending_questions
    except subprocess.TimeoutExpired:
        with state.lock:
            if state.claude_proc: state.claude_proc.kill(); state.claude_proc = None
        return i18n.t("error.timeout"), None, None
    except Exception as e:
        with state.lock: state.claude_proc = None
        return i18n.t("error.generic", msg=str(e)), None, None
