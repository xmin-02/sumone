"""Claude CLI integration."""
import json
import os
import subprocess
import threading
import time

import i18n
import config as _config
from config import IS_WINDOWS, settings, log
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
        extra = ":".join(p for p in [
            os.path.expanduser("~/.local/bin"),
            "/opt/homebrew/bin",
            "/opt/homebrew/sbin",
            "/usr/local/bin",
        ] if os.path.isdir(p))
        env["PATH"] = extra + ":/usr/bin:/bin:" + env.get("PATH", "")
        goroot = os.path.join(_config.WORK_DIR, "goroot")
        gopath = os.path.join(_config.WORK_DIR, "gopath")
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


def _parse_deleted_paths(command, cwd=None):
    """Parse file paths from deletion commands (cross-platform).

    Detects: rm, rm -f, rm -rf, rmdir (Unix/Git Bash on Windows)
             del, erase, rmdir, rd (Windows CMD)
             Remove-Item, ri, rm (PowerShell)
    Returns list of absolute file paths.
    """
    import re
    import shlex
    if not command or not command.strip():
        return []
    cwd = cwd or _config.WORK_DIR
    paths = []

    # Split by && ; || to handle chained commands
    # Preserve quoted strings by using a simple split approach
    for segment in re.split(r'\s*(?:&&|\|\||;)\s*', command):
        segment = segment.strip()
        if not segment:
            continue

        # Try to tokenize; fall back to simple split on failure
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        if not tokens:
            continue

        cmd_base = os.path.basename(tokens[0]).lower()
        # Strip .exe extension for Windows
        if cmd_base.endswith(".exe"):
            cmd_base = cmd_base[:-4]

        is_delete_cmd = False
        path_start_idx = 1  # index where file paths begin

        # Unix: rm, rmdir
        if cmd_base in ("rm", "rmdir"):
            is_delete_cmd = True
        # Windows CMD: del, erase, rd
        elif cmd_base in ("del", "erase", "rd"):
            is_delete_cmd = True
        # PowerShell: Remove-Item, ri
        elif cmd_base in ("remove-item", "ri"):
            is_delete_cmd = True

        if not is_delete_cmd:
            continue

        # Extract paths (skip flags starting with -)
        for token in tokens[path_start_idx:]:
            if token.startswith("-"):
                continue
            # Skip PowerShell parameters like -Recurse, -Force, -Path
            if token.startswith("/") and len(token) > 1 and token[1:2].isalpha() and IS_WINDOWS:
                # Could be /f, /s, /q flags for del/rd on Windows CMD
                if len(token) <= 3:
                    continue
            # Convert to absolute path
            p = token.strip("'\"")
            if not p or p in (".", ".."):
                continue
            # Convert MSYS/Git Bash paths (/c/Users/...) to Windows (C:\Users\...)
            if IS_WINDOWS and re.match(r'^/[a-zA-Z]/', p):
                p = p[1].upper() + ":" + p[2:]
            if not os.path.isabs(p):
                p = os.path.join(cwd, p)
            p = os.path.normpath(p)
            paths.append(p)

    return paths


def _send_intermediate(text):
    html = md_to_telegram_html(text)
    chunks = split_message(html)
    for idx, chunk in enumerate(chunks):
        send_html(f"\U0001f4ad {chunk}")
        if idx < len(chunks) - 1: time.sleep(0.3)


def _format_time(mins, secs):
    t_str = i18n.t("time.format_ms", mins=mins, secs=secs) if mins > 0 else i18n.t("time.format_s", secs=secs)
    return t_str


def _send_file_viewer_link(had_new_files):
    """If files were modified in this run and file viewer is active, send a link."""
    if not had_new_files or not state.file_viewer_url:
        return
    if not settings.get("auto_viewer_link", True):
        return
    try:
        from fileviewer import generate_token, get_or_create_fixed_token
        from telegram import delete_msg
        # Delete previous viewer link messages
        for mid in state._viewer_msg_ids:
            delete_msg(mid)
        state._viewer_msg_ids.clear()
        if settings.get("viewer_link_fixed", False):
            token = get_or_create_fixed_token()
        else:
            token = generate_token()
        url = f"{state.file_viewer_url}?token={token}"
        count = len(set(e["path"] for e in state.modified_files))
        label = i18n.t("file_viewer.link", count=count)
        # Disable link preview to prevent Telegram from prefetching the token URL
        result = tg_api("sendMessage", {
            "chat_id": CHAT_ID,
            "text": f'<a href="{url}">{escape_html(label)}</a>',
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        # Track message ID for future deletion
        try:
            msg_id = result["result"]["message_id"]
            state._viewer_msg_ids.append(msg_id)
        except (TypeError, KeyError):
            pass
        # Update file viewer server with current file list
        if state._file_server:
            state._file_server.update_files(state.modified_files)
        log.info("File viewer link sent (%d files)", count)
    except Exception as e:
        log.warning("Failed to send file viewer link: %s", e)


def run_claude(message, session_id=None):
    cmd = [CLAUDE_CMD]
    if session_id: cmd += ["-r", session_id]
    cmd += ["-p", message, "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]
    if state.model: cmd += ["--model", state.model]
    log.info("Running: %s", " ".join(cmd[:6]) + "...")
    from state import next_run_id
    run_id = next_run_id(label=message)
    log.info("Run cycle #%d: %s", run_id, message[:50])
    files_count_before = len(state.modified_files)
    try:
        popen_kwargs = dict(
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=_config.WORK_DIR, env=_claude_env(),
        )
        if IS_WINDOWS:
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        with state.lock:
            proc = subprocess.Popen(cmd, **popen_kwargs)
            state.claude_proc = proc
        final_text = []; sent_text_count = 0
        captured_session_id = None; pending_questions = None
        last_status_time = 0; start_time = time.time()
        pending_edit_snapshots = []  # deferred Edit snapshot captures
        def _typing_loop():
            while proc.poll() is None: send_typing(); time.sleep(5)
        threading.Thread(target=_typing_loop, daemon=True).start()
        for raw_line in proc.stdout:
            # Process deferred Edit snapshots (file now written from prev iteration)
            if pending_edit_snapshots:
                from state import add_modified_file
                for _pfp in pending_edit_snapshots:
                    try:
                        with open(_pfp, encoding="utf-8", errors="replace") as _f:
                            _content = _f.read()
                        add_modified_file(_pfp, content=_content, op="edit")
                    except Exception:
                        add_modified_file(_pfp, content=None, op="edit")
                pending_edit_snapshots.clear()
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
                        # Track file modifications for file viewer (with snapshots)
                        t_name = block.get("name", "")
                        if t_name == "Write":
                            fp = block.get("input", {}).get("file_path", "")
                            if fp:
                                from state import add_modified_file
                                w_content = block.get("input", {}).get("content", "")
                                add_modified_file(fp, content=w_content, op="write")
                        elif t_name == "Edit":
                            fp = block.get("input", {}).get("file_path", "")
                            if fp:
                                pending_edit_snapshots.append(fp)
                        elif t_name == "Bash":
                            bash_cmd = block.get("input", {}).get("command", "")
                            deleted = _parse_deleted_paths(bash_cmd, cwd=_config.WORK_DIR)
                            if deleted:
                                from state import add_modified_file
                                for dp in deleted:
                                    add_modified_file(dp, content=None, op="delete")
                                log.info("Tracked %d deleted path(s) from Bash", len(deleted))
                        if t_name == "AskUserQuestion":
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
        # Process remaining deferred Edit snapshots
        if pending_edit_snapshots:
            from state import add_modified_file
            for _pfp in pending_edit_snapshots:
                try:
                    with open(_pfp, encoding="utf-8", errors="replace") as _f:
                        _content = _f.read()
                    add_modified_file(_pfp, content=_content, op="edit")
                except Exception:
                    add_modified_file(_pfp, content=None, op="edit")
        try: proc.wait(timeout=10)
        except Exception: pass
        try: stderr_out = proc.stderr.read().decode("utf-8", errors="replace").strip()
        except Exception: stderr_out = ""
        with state.lock: state.claude_proc = None
        # Send file viewer link only if NEW files were modified in this run
        had_new_files = len(state.modified_files) > files_count_before
        _send_file_viewer_link(had_new_files)
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
