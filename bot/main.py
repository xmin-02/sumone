#!/usr/bin/env python3
"""Sumone Telegram Bot - Entry point.

Polling loop, update routing, and message handling.
"""
import json
import os
import signal
import sys
import threading
import time

# Unix lock file support (prevents duplicate instances under launchd)
if sys.platform != "win32":
    import fcntl

import i18n
import config
from config import BOT_TOKEN, CHAT_ID, POLL_TIMEOUT, IS_WINDOWS, settings, log
from state import state
from telegram import (
    escape_html, tg_api, send_html, delete_msg, send_long, send_typing,
)
from tokens import token_footer, get_monthly_tokens, publish_token_data, PUBLISH_INTERVAL
from downloader import download_tg_file, build_file_prompt
from ai import get_runner, RunnerCallbacks, format_time
from sessions import get_session_model
import cli_watcher

# commands/__init__.py auto-imports all subpackage modules via _auto_import()
from commands import dispatch, dispatch_callback
from commands.session.session import (
    show_questions, handle_answer, handle_selection, _save_session_id,
)
from commands.usage.total_tokens import handle_token_input


# ---------------------------------------------------------------------------
# Core message handler
# ---------------------------------------------------------------------------

def handle_message(text):
    """Send user text to Claude CLI and deliver the response."""
    # Route to connect flow if active
    from ai.connect import is_connect_active, handle_connect_response
    if is_connect_active():
        if not handle_connect_response(text):
            send_html(f"<i>{i18n.t('ai_connect.busy')}</i>")
        return

    with state.lock:
        if state.busy:
            state.message_queue.append(text)
            qlen = len(state.message_queue)
            send_html(f"<i>{i18n.t('queued', pos=qlen)}</i>")
            log.info("Message queued (pos %d): %s", qlen, text[:80])
            return
        state.busy = True

    _run_message(text)


def _send_file_viewer_link(had_new_files):
    """If files were modified in this run and file viewer is active, send a link."""
    if not had_new_files or not state.file_viewer_url:
        return
    if not settings.get("auto_viewer_link", True):
        return
    try:
        from fileviewer import generate_token, get_or_create_fixed_token
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
        result = tg_api("sendMessage", {
            "chat_id": CHAT_ID,
            "text": f'<a href="{url}">{escape_html(label)}</a>',
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        try:
            msg_id = result["result"]["message_id"]
            state._viewer_msg_ids.append(msg_id)
        except (TypeError, KeyError):
            pass
        if state._file_server:
            state._file_server.update_files(state.modified_files)
        log.info("File viewer link sent (%d files)", count)
    except Exception as e:
        log.warning("Failed to send file viewer link: %s", e)


def _on_intermediate_text(text):
    """Callback: send intermediate AI text to Telegram."""
    from telegram import md_to_telegram_html, split_message
    html = md_to_telegram_html(text)
    chunks = split_message(html)
    for idx, chunk in enumerate(chunks):
        send_html(f"\U0001f4ad {chunk}")
        if idx < len(chunks) - 1:
            time.sleep(0.3)


def _on_status(label, elapsed_secs):
    """Callback: send status message to Telegram."""
    mins, secs = divmod(elapsed_secs, 60)
    t_str = format_time(mins, secs)
    send_html(f"<i>{escape_html(label)} ({t_str})</i>")


def _on_cost(parsed):
    """Callback: send cost info to Telegram."""
    if not settings["show_cost"]:
        return
    dur_s = parsed.duration_ms / 1000 if parsed.duration_ms else 0
    mins, secs = divmod(int(dur_s), 60)
    dur_str = format_time(mins, secs)
    cost_line = i18n.t("cost.line", cost=f"{parsed.cost_usd:.4f}", duration=dur_str,
                       turns=parsed.num_turns, in_tok=f"{parsed.tokens_in:,}",
                       out_tok=f"{parsed.tokens_out:,}")
    send_html(f"<i>{cost_line}</i>")


def _run_message(text):
    """Internal: execute a single message with typing animation."""
    # Animated typing indicator
    typing_id = [None]
    typing_stop = threading.Event()
    if settings.get("show_typing", True):
        r = send_html(f"<b>{i18n.t('typing.label')} \u00b7</b>\n<i>{i18n.t('typing.cancel_hint')}</i>")
        try:
            typing_id[0] = r
        except Exception:
            pass

        def _typing_anim():
            dots = ["\u00b7", "\u00b7\u00b7", "\u00b7\u00b7\u00b7"]
            idx = 0
            while not typing_stop.is_set():
                typing_stop.wait(3)
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
            callbacks = RunnerCallbacks(
                on_text=_on_intermediate_text,
                on_status=_on_status,
                on_typing=send_typing,
                on_cost=_on_cost,
                on_file_link=_send_file_viewer_link,
            )
            runner = get_runner(callbacks=callbacks)
            provider_label = config.AI_MODELS.get(
                state.provider, {}).get("label", state.provider.title())

            log.info("%s starting for: %s", provider_label, text[:80])
            output, new_sid, questions = runner.run(text, session_id=sid)
            log.info("%s finished, output=%d chars, new_sid=%s, questions=%s",
                     provider_label,
                     len(output) if output else 0, new_sid, bool(questions))

            if new_sid and not sid:
                # Brand new session (no previous session_id)
                state.session_id = new_sid
                state._provider_sessions[state.provider] = new_sid
                _save_session_id(new_sid)
                config.update_config("provider_sessions", dict(state._provider_sessions))
                log.info("Session created: %s (%s)", new_sid, state.provider)

            active_sid = state.session_id or new_sid or sid
            footer = token_footer()

            typing_stop.set()
            delete_msg(typing_id[0])

            if questions:
                show_questions(questions, active_sid)
                if output and output not in ("",):
                    header = provider_label
                    if active_sid:
                        header += f" [{active_sid[:8]}]"
                    send_long(header, output, footer=footer)
                return

            if not output:
                send_html(f"<i>{i18n.t('error.empty_response')}</i>")
                return

            header = provider_label
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
            # Process next queued message, or release busy
            next_text = None
            with state.lock:
                if state.message_queue:
                    next_text = state.message_queue.popleft()
                else:
                    state.busy = False
            if next_text:
                log.info("Processing queued message: %s", next_text[:80])
                _run_message(next_text)

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
        # Route connect: callbacks to connect flow
        if data.startswith("connect:"):
            from ai.connect import handle_connect_callback
            from telegram import tg_api as _tga
            cb_id = cb["id"]
            payload = data[len("connect:"):]
            if handle_connect_callback(payload):
                _tga("answerCallbackQuery", {"callback_query_id": cb_id})
            return
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

    # Default: send to AI
    handle_message(text)


# ---------------------------------------------------------------------------
# Bot command sync + startup
# ---------------------------------------------------------------------------

def _discover_plugin_skills():
    """Scan installed Claude Code plugins and extract skills from SKILL.md files.

    Returns a dict: {plugin_name: [(cmd, desc), ...], ...}

    Only scans plugins listed in installed_plugins.json (actually installed).
    Marketplace catalog directories are NOT scanned to avoid showing uninstalled plugins.
    """
    import re as _re
    claude_dir = os.path.join(os.path.expanduser("~"), ".claude", "plugins")
    if not os.path.isdir(claude_dir):
        return {}

    grouped = {}  # {plugin_name: [(cmd, desc), ...]}
    seen = set()

    def _parse_skill_md(skill_md, plugin_name):
        """Parse a single SKILL.md and append to grouped dict."""
        try:
            with open(skill_md, encoding="utf-8") as f:
                content = f.read()
        except Exception:
            return
        m = _re.match(r"^---\r?\n(.*?)\r?\n---", content, _re.DOTALL)
        if not m:
            return
        name = os.path.basename(os.path.dirname(skill_md))
        desc = ""
        for line in m.group(1).split("\n"):
            colon = line.find(":")
            if colon == -1:
                continue
            key = line[:colon].strip()
            val = line[colon + 1:].strip().strip("'\"")
            if key == "name":
                name = val
            elif key == "description":
                desc = val
        cmd = name.lower().replace("-", "_")
        if cmd not in seen and desc:
            seen.add(cmd)
            grouped.setdefault(plugin_name, []).append((cmd, desc))

    def _scan_skills_dir(skills_dir, plugin_name):
        """Scan a skills directory for SKILL.md files."""
        if not os.path.isdir(skills_dir):
            return
        try:
            entries = os.listdir(skills_dir)
        except Exception:
            return
        for entry in entries:
            skill_md = os.path.join(skills_dir, entry, "SKILL.md")
            if os.path.isfile(skill_md):
                _parse_skill_md(skill_md, plugin_name)

    # 1. Installed plugins (installPath + plugin.json)
    plugins_file = os.path.join(claude_dir, "installed_plugins.json")
    if os.path.isfile(plugins_file):
        try:
            with open(plugins_file, encoding="utf-8") as f:
                data = json.load(f)
            for plugin_key, installs in data.get("plugins", {}).items():
                # Prefer marketplace alias (after @) as it's shorter
                # e.g. "oh-my-claudecode@omc" → "omc"
                if "@" in plugin_key:
                    plugin_name = plugin_key.split("@")[1]
                else:
                    plugin_name = plugin_key
                for install in installs:
                    install_path = install.get("installPath", "")
                    if not install_path or not os.path.isdir(install_path):
                        continue
                    plugin_json = os.path.join(install_path, ".claude-plugin", "plugin.json")
                    skills_dir = os.path.join(install_path, "skills")
                    if os.path.isfile(plugin_json):
                        try:
                            with open(plugin_json, encoding="utf-8") as f:
                                pdata = json.load(f)
                            skills_rel = pdata.get("skills", "./skills/")
                            skills_dir = os.path.normpath(os.path.join(install_path, skills_rel))
                        except Exception:
                            pass
                    _scan_skills_dir(skills_dir, plugin_name)
        except Exception:
            pass

    total = sum(len(v) for v in grouped.values())
    log.info("Discovered %d plugin skills across %d plugins", total, len(grouped))
    return grouped


def _sync_bot_commands():
    """Register bot commands with BotFather on startup.

    - Bot-native commands: use i18n description
    - Per-plugin menu commands (e.g. /omc): dynamically generated
    - Individual plugin skills are NOT registered (shown via inline keyboard instead)
    """
    try:
        from commands import _handlers

        # 1. Bot-native commands from i18n (localized descriptions)
        bot_native = {k.lstrip("/").lower() for k in _handlers}
        bot_commands = i18n.t("bot_commands")
        if not isinstance(bot_commands, list):
            bot_commands = []

        merged = []
        seen = set()
        for cmd, desc in bot_commands:
            key = cmd.lower()
            if key in seen:
                continue
            seen.add(key)
            if key in bot_native:
                merged.append({"command": cmd, "description": desc[:256]})

        # 2. Auto-discover plugin skills and register per-plugin menu commands
        plugin_groups = _discover_plugin_skills()
        from commands.system.skills import register_plugin_menus
        register_plugin_menus(plugin_groups)

        for plugin_name, skills_list in plugin_groups.items():
            menu_cmd = plugin_name.lower().replace("-", "_")
            if menu_cmd not in seen:
                seen.add(menu_cmd)
                desc = i18n.t("plugin_menu.botfather_desc",
                              plugin=plugin_name, count=len(skills_list))
                merged.append({"command": menu_cmd, "description": desc[:256]})

        if merged:
            result = tg_api("setMyCommands", {"commands": json.dumps(merged)})
            if result and result.get("ok"):
                plugin_count = len(plugin_groups)
                log.info("BotFather commands synced (%d total: %d bot-native + %d plugin menus)",
                         len(merged), len(merged) - plugin_count, plugin_count)
            else:
                log.warning("BotFather setMyCommands failed: %s", result)
    except Exception as e:
        log.warning("Failed to sync commands: %s", e)


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

_lock_fd = None


def _acquire_instance_lock():
    """Acquire exclusive lock file. Exit(0) if another instance is running.

    On macOS/Linux with launchd KeepAlive={SuccessfulExit:false},
    exit(0) tells launchd NOT to restart — preventing kill-restart loops.
    """
    global _lock_fd
    if IS_WINDOWS:
        return  # Windows uses _kill_duplicate_bots() instead
    lock_path = os.path.join(config.ROOT_DIR, "bot.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    try:
        _lock_fd = open(lock_path, "w")
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
        log.info("Instance lock acquired (PID %d)", os.getpid())
    except (IOError, OSError):
        log.info("Another bot instance is already running. Exiting cleanly.")
        sys.exit(0)


def _kill_duplicate_bots():
    """Find and kill other bot processes (same script), return count killed."""
    import subprocess as _sp
    my_pid = os.getpid()
    skip_pids = {my_pid}
    try:
        skip_pids.add(os.getppid())
    except (AttributeError, OSError):
        pass
    killed = 0
    bot_scripts = {"main.py", "telegram-bot-ko.py", "telegram-bot-en.py", "telegram-bot.py"}
    try:
        if IS_WINDOWS:
            # Use PowerShell (wmic is removed in newer Windows)
            ps_cmd = (
                "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" "
                "| Select-Object ProcessId, CommandLine "
                "| ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }"
            )
            out = _sp.check_output(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                creationflags=_sp.CREATE_NO_WINDOW,
                timeout=10,
            ).decode("utf-8", errors="replace")
            for line in out.strip().splitlines():
                line = line.strip()
                if "|" not in line:
                    continue
                pid_str, cmdline = line.split("|", 1)
                try:
                    pid = int(pid_str.strip())
                except ValueError:
                    continue
                if pid in skip_pids:
                    continue
                if any(s in cmdline for s in bot_scripts):
                    try:
                        os.kill(pid, signal.SIGTERM)
                        killed += 1
                        log.info("Killed duplicate bot process: PID %d", pid)
                    except OSError:
                        pass
        else:
            out = _sp.check_output(
                ["ps", "-eo", "pid,args"],
                timeout=10,
            ).decode("utf-8", errors="replace")
            for line in out.strip().splitlines()[1:]:
                line = line.strip()
                if not line:
                    continue
                tok = line.split(None, 1)
                if len(tok) < 2:
                    continue
                try:
                    pid = int(tok[0])
                except ValueError:
                    continue
                cmdline = tok[1]
                if pid in skip_pids:
                    continue
                if "python" in cmdline.lower() and any(s in cmdline for s in bot_scripts):
                    try:
                        os.kill(pid, signal.SIGTERM)
                        killed += 1
                        log.info("Killed duplicate bot process: PID %d", pid)
                    except OSError:
                        pass
    except Exception as e:
        log.warning("Duplicate bot check failed: %s", e)
    return killed


def _start_file_viewer():
    """Start the file viewer HTTP server and cloudflared tunnel."""
    from fileviewer import FileViewerServer
    from tunnel import check_cloudflared, install_cloudflared, start_tunnel

    server = FileViewerServer()
    port = server.start()
    state._file_server = server

    if not check_cloudflared():
        log.info("cloudflared not found, attempting install...")
        if not install_cloudflared():
            log.warning("cloudflared install failed. File viewer will be local-only.")
            return

    proc, url = start_tunnel(port)
    if proc and url:
        state._tunnel_proc = proc
        state.file_viewer_url = url
        log.info("File viewer ready: %s", url)
    else:
        log.warning("cloudflared tunnel failed. File viewer will be local-only.")


def _stop_file_viewer():
    """Stop the file viewer server and tunnel."""
    from tunnel import stop_tunnel
    if state._tunnel_proc:
        stop_tunnel(state._tunnel_proc)
        state._tunnel_proc = None
        state.file_viewer_url = None
    if state._file_server:
        state._file_server.stop()
        state._file_server = None


def poll_loop():
    offset = 0
    log.info("Bot started.")

    # Kill duplicate bot processes (Windows only; Unix uses lock file)
    killed = _kill_duplicate_bots() if IS_WINDOWS else 0

    _sync_bot_commands()

    # File viewer server + cloudflared tunnel
    _start_file_viewer()

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

    # CLI direct-response watcher (forwards external CLI output to Telegram)
    cli_watcher.start()

    state.global_tokens = get_monthly_tokens()
    log.info("Monthly tokens loaded: %d", state.global_tokens)

    if killed > 0:
        send_html(f"<b>{i18n.t('bot_duplicate', count=killed)}</b>")
    _prov = config.AI_MODELS.get(state.provider, {}).get("label", state.provider.title())
    _mdl = state.model or "default"
    send_html(f"<b>{i18n.t('bot_started', provider=_prov, model=_mdl)}</b>")

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

def _bootstrap_files():
    """One-time full sync if local update.py lacks the new all-files updater."""
    bot_dir = os.path.dirname(os.path.abspath(__file__))
    update_path = os.path.join(bot_dir, "commands", "system", "update.py")
    marker = "_fetch_bot_file_list"
    try:
        with open(update_path, encoding="utf-8") as f:
            if marker in f.read():
                return  # already up to date
    except FileNotFoundError:
        pass  # file missing, need bootstrap

    import base64
    import hashlib
    import urllib.request
    github_repo = config._config.get("github_repo", "xmin-02/sumone")
    log.info("Bootstrap: syncing all bot files from GitHub...")
    try:
        api_url = f"https://api.github.com/repos/{github_repo}/git/trees/main?recursive=1"
        req = urllib.request.Request(api_url, headers={"Accept": "application/vnd.github.v3+json"})
        resp = urllib.request.urlopen(req, timeout=15)
        tree = json.loads(resp.read().decode())
        count = 0
        for item in tree.get("tree", []):
            if item["type"] != "blob" or not item["path"].startswith("bot/"):
                continue
            rel_path = item["path"][4:]
            local_path = os.path.join(bot_dir, rel_path)
            # Compare git blob SHA — skip if unchanged
            if os.path.exists(local_path):
                try:
                    with open(local_path, "rb") as f:
                        data = f.read()
                    local_sha = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
                    if local_sha == item["sha"]:
                        continue
                except Exception:
                    pass
            # Download via Contents API (no CDN cache)
            try:
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                contents_url = f"https://api.github.com/repos/{github_repo}/contents/bot/{rel_path}?ref=main"
                creq = urllib.request.Request(contents_url, headers={"Accept": "application/vnd.github.v3+json"})
                cresp = urllib.request.urlopen(creq, timeout=15)
                cdata = json.loads(cresp.read().decode())
                content = base64.b64decode(cdata["content"])
                with open(local_path, "wb") as f:
                    f.write(content)
                count += 1
            except Exception:
                pass
        log.info("Bootstrap complete: %d files updated", count)
        if count > 0:
            log.info("Restarting after bootstrap...")
            os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])
    except Exception as e:
        log.warning("Bootstrap failed (non-fatal): %s", e)


def _detect_cli_status():
    """Detect which AI CLIs are installed. Runs at startup and caches in state.cli_status."""
    import subprocess as _sp
    import shutil as _sh
    from state import get_provider_env
    # Ensure common CLI paths are in PATH (exec may strip them)
    _extra = [os.path.expanduser("~/.local/bin"), os.path.expanduser("~/.npm-global/bin"),
              "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin"]
    _cur_path = os.environ.get("PATH", "")
    _missing = [p for p in _extra if p not in _cur_path]
    if _missing:
        os.environ["PATH"] = ":".join(_missing) + ":" + _cur_path
    # Auth check commands per provider (exit code 0 = authenticated)
    for provider, info in config.AI_MODELS.items():
        cmd = info.get("cli_cmd", provider)
        resolved = _sh.which(cmd)
        if not resolved:
            state.cli_status[provider] = False
            continue
        try:
            r = _sp.run([resolved, "--version"], capture_output=True, timeout=5)
            if r.returncode != 0:
                state.cli_status[provider] = False
                continue
        except Exception:
            state.cli_status[provider] = False
            continue
        # Check auth status (same logic as connect.py _check_auth)
        env = {**os.environ, **get_provider_env(provider)}
        try:
            if provider == "codex":
                state.cli_status[provider] = _sp.run(
                    [resolved, "login", "status"], capture_output=True, timeout=5, env=env
                ).returncode == 0
            elif provider == "gemini":
                state.cli_status[provider] = os.path.isfile(
                    os.path.expanduser("~/.gemini/oauth_creds.json"))
            else:  # claude
                state.cli_status[provider] = _sp.run(
                    [resolved, "auth", "status"], capture_output=True, timeout=5, env=env
                ).returncode == 0
        except Exception:
            state.cli_status[provider] = False
    log.info("CLI status: %s", state.cli_status)


def _apply_default_model():
    """Apply default_sub_model from settings to state.model at startup."""
    if state.model:
        return  # session already has a model set
    from state import switch_provider
    sub = config.settings.get("default_sub_model")
    # Use last used provider (persisted), or settings default
    ai = config._config.get("provider") or config.settings.get("default_model", "claude")
    # Assign startup session_id to its actual provider (not blindly to claude)
    if state.session_id:
        from sessions import get_session_provider
        actual_prov = get_session_provider(state.session_id)
        if actual_prov:
            state._provider_sessions[actual_prov] = state.session_id
        if actual_prov != state.provider:
            state.session_id = None
    switch_provider(ai)
    # Restore the persisted model for the active provider first.
    saved_model = state._provider_models.get(ai)
    if not saved_model:
        legacy_model = config._config.get("model")
        if legacy_model:
            ai_info = config.AI_MODELS.get(ai, {})
            if legacy_model in ai_info.get("sub_models", {}).values():
                saved_model = legacy_model
    if saved_model:
        state.model = saved_model
        config.log.info("Restored model: %s (%s)", saved_model, ai)
    else:
        ai_info = config.AI_MODELS.get(ai)
        if ai_info:
            resolved = None
            subs = ai_info.get("sub_models", {})
            if sub:
                resolved = subs.get(sub)
            if not resolved:
                default_sub = ai_info.get("default")
                if default_sub:
                    resolved = subs.get(default_sub)
            if resolved:
                state.model = resolved
                config.log.info("Default model applied: %s (%s)", resolved, ai)
    config.update_config("model", state.model)


def _migrate_old_layout():
    """Migrate data and code from old location to DDD layout.

    Handles the full migration path so that a single /update_bot is enough:
    1. Migrate data (sessions, downloads, etc.) to ~/.sumone/data/
    2. Copy bot code to ~/.sumone/bot/ if running from old location
    3. Update autostart configs (launchd, systemd, Task Scheduler)
    4. Re-exec from ~/.sumone/bot/main.py
    """
    import shutil
    old_bot = os.path.expanduser("~/.claude-telegram-bot")
    root = config.ROOT_DIR            # ~/.sumone
    data_dir = config.DATA_DIR        # ~/.sumone/data
    bin_dir = config.BIN_DIR          # ~/.sumone/bin
    log_dir = config.LOG_DIR          # ~/.sumone/logs
    new_bot_dir = os.path.join(root, "bot")   # ~/.sumone/bot

    # Detect if running from old location (not under ~/.sumone/)
    cur_bot = os.path.abspath(config.BOT_DIR)
    running_from_old = not cur_bot.startswith(os.path.abspath(root) + os.sep)

    migrated = []

    # --- Data: ~/.sumone/sessions/ → ~/.sumone/data/sessions/ ---
    old_sessions = os.path.join(root, "sessions")
    new_sessions = os.path.join(data_dir, "sessions")
    if os.path.isdir(old_sessions) and old_sessions != new_sessions:
        for fname in os.listdir(old_sessions):
            src = os.path.join(old_sessions, fname)
            dst = os.path.join(new_sessions, fname)
            if os.path.isfile(src) and not os.path.isfile(dst):
                shutil.move(src, dst)
        try:
            os.rmdir(old_sessions)
            migrated.append("sessions/")
        except OSError:
            pass

    # --- Data: ~/.sumone/token_log.jsonl → ~/.sumone/data/token_log.jsonl ---
    old_tlog = os.path.join(root, "token_log.jsonl")
    new_tlog = os.path.join(data_dir, "token_log.jsonl")
    if os.path.isfile(old_tlog) and not os.path.isfile(new_tlog):
        shutil.move(old_tlog, new_tlog)
        migrated.append("token_log.jsonl")

    # --- Data from old bot dir ---
    if os.path.isdir(old_bot):
        # downloads/
        old_dl = os.path.join(old_bot, "downloads")
        new_dl = os.path.join(data_dir, "downloads")
        if os.path.isdir(old_dl):
            for fname in os.listdir(old_dl):
                src = os.path.join(old_dl, fname)
                dst = os.path.join(new_dl, fname)
                if os.path.isfile(src) and not os.path.isfile(dst):
                    shutil.move(src, dst)
            migrated.append("downloads/")

        # .snapshots/ → data/snapshots/
        old_snap = os.path.join(old_bot, ".snapshots")
        new_snap = os.path.join(data_dir, "snapshots")
        if os.path.isdir(old_snap):
            for fname in os.listdir(old_snap):
                src = os.path.join(old_snap, fname)
                dst = os.path.join(new_snap, fname)
                if os.path.isfile(src) and not os.path.isfile(dst):
                    shutil.move(src, dst)
            migrated.append("snapshots/")

        # modified_files.json
        old_mf = os.path.join(old_bot, "modified_files.json")
        new_mf = os.path.join(data_dir, "modified_files.json")
        if os.path.isfile(old_mf) and not os.path.isfile(new_mf):
            shutil.copy2(old_mf, new_mf)
            migrated.append("modified_files.json")

        # cloudflared → bin/
        for cf_name in ["cloudflared", "cloudflared.exe"]:
            old_cf = os.path.join(old_bot, cf_name)
            new_cf = os.path.join(bin_dir, cf_name)
            if os.path.isfile(old_cf) and not os.path.isfile(new_cf):
                shutil.copy2(old_cf, new_cf)
                try:
                    os.chmod(new_cf, 0o755)
                except OSError:
                    pass
                migrated.append(cf_name)

        # logs
        for lf in ["bot.log", "bot-stdout.log", "bot-stderr.log"]:
            old_lf = os.path.join(old_bot, lf)
            new_lf = os.path.join(log_dir, lf)
            if os.path.isfile(old_lf) and not os.path.isfile(new_lf):
                shutil.copy2(old_lf, new_lf)

    # --- Copy bot code to new location if running from old dir ---
    if running_from_old:
        # Orphan files from pre-DDD structure (should not be copied)
        _orphans = {
            "basic.py", "filesystem.py", "session_cmd.py",
            "settings.py", "update.py", "total_tokens.py", "skills.py",
        }
        os.makedirs(new_bot_dir, exist_ok=True)
        for dirpath, dirnames, filenames in os.walk(cur_bot):
            # Skip __pycache__
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            rel = os.path.relpath(dirpath, cur_bot)
            dst_dir = os.path.join(new_bot_dir, rel) if rel != "." else new_bot_dir
            os.makedirs(dst_dir, exist_ok=True)
            for fname in filenames:
                if not fname.endswith((".py", ".json")):
                    continue
                # Skip orphan command files at commands/ root
                if rel == "commands" and fname in _orphans:
                    continue
                # Skip old single-file claude.py at bot root (replaced by ai/)
                if rel == "." and fname == "claude.py" and os.path.isdir(
                        os.path.join(cur_bot, "ai")):
                    continue
                src = os.path.join(dirpath, fname)
                dst = os.path.join(dst_dir, fname)
                shutil.copy2(src, dst)
        migrated.append("bot code → ~/.sumone/bot/")
        config.log.info("Bot code copied to %s", new_bot_dir)

    if migrated:
        config.log.info("Migration complete: %s", ", ".join(migrated))

    # --- Re-exec from new location ---
    if running_from_old:
        # Update autostart configs only when migrating from old location
        _update_launchd_plist(new_bot_dir)
        _update_windows_task(new_bot_dir)
        _update_systemd_service(new_bot_dir)
        new_main = os.path.join(new_bot_dir, "main.py")
        if os.path.isfile(new_main):
            config.log.info("Re-executing from new location: %s", new_main)
            os.execv(sys.executable, [sys.executable, new_main])


def _update_launchd_plist(target_bot_dir):
    """Update macOS launchd plist to point to new paths."""
    if config.IS_WINDOWS:
        return
    import platform
    if platform.system() != "Darwin":
        return

    bot_main = os.path.join(target_bot_dir, "main.py")
    log_dir = config.LOG_DIR
    plist_dir = os.path.expanduser("~/Library/LaunchAgents")

    # Find and update any existing plist
    for name in ["com.claude.telegram-bot.plist", "com.sumone.telegram-bot.plist"]:
        plist_path = os.path.join(plist_dir, name)
        if not os.path.isfile(plist_path):
            continue
        try:
            with open(plist_path, encoding="utf-8") as f:
                content = f.read()
            if bot_main in content and log_dir in content:
                continue  # already up to date
            # Rewrite plist with correct paths
            import subprocess
            python_path = subprocess.check_output(
                ["which", "python3"], text=True).strip() or "python3"
            new_plist = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0"><dict>\n'
                '    <key>Label</key><string>com.sumone.telegram-bot</string>\n'
                '    <key>ProgramArguments</key>\n'
                f'    <array><string>{python_path}</string>'
                f'<string>{bot_main}</string></array>\n'
                '    <key>RunAtLoad</key><true/>\n'
                '    <key>KeepAlive</key>\n'
                '    <dict>\n'
                '        <key>SuccessfulExit</key><false/>\n'
                '    </dict>\n'
                f'    <key>StandardOutPath</key><string>{log_dir}/bot-stdout.log</string>\n'
                f'    <key>StandardErrorPath</key><string>{log_dir}/bot-stderr.log</string>\n'
                '</dict></plist>\n'
            )
            new_path = os.path.join(plist_dir, "com.sumone.telegram-bot.plist")
            with open(new_path, "w", encoding="utf-8") as f:
                f.write(new_plist)
            if name != "com.sumone.telegram-bot.plist":
                try:
                    os.remove(plist_path)
                except OSError:
                    pass
            config.log.info("launchd plist updated: %s", new_path)
        except Exception as e:
            config.log.warning("Failed to update launchd plist: %s", e)


def _update_windows_task(target_bot_dir):
    """Update Windows Task Scheduler to point to new bot path."""
    if not config.IS_WINDOWS:
        return
    import subprocess as _sp
    bot_main = os.path.join(target_bot_dir, "main.py")
    # Check both old and new task names for backward compatibility
    for task_name in ["SumoneBot", "ClaudeTelegramBot"]:
        try:
            result = _sp.run(
                ["schtasks", "/Query", "/TN", task_name],
                capture_output=True, text=True, timeout=10,
                creationflags=_sp.CREATE_NO_WINDOW,
            )
            if result.returncode != 0:
                continue  # task doesn't exist
            python_path = sys.executable
            _sp.run(
                ["schtasks", "/Change", "/TN", task_name,
                 "/TR", f'"{python_path}" "{bot_main}"'],
                capture_output=True, text=True, timeout=10,
                creationflags=_sp.CREATE_NO_WINDOW,
            )
            config.log.info("Windows Task Scheduler updated: %s", task_name)
            return
        except _sp.TimeoutExpired:
            config.log.warning("schtasks timed out for task %s, skipping", task_name)
        except Exception as e:
            config.log.warning("Failed to update Windows task %s: %s", task_name, e)


def _update_systemd_service(target_bot_dir):
    """Update Linux systemd service to point to new bot path."""
    if config.IS_WINDOWS:
        return
    import platform
    if platform.system() != "Linux":
        return
    bot_main = os.path.join(target_bot_dir, "main.py")
    svc_path = os.path.expanduser("~/.config/systemd/user/claude-telegram.service")
    if not os.path.isfile(svc_path):
        return
    try:
        with open(svc_path, encoding="utf-8") as f:
            content = f.read()
        if bot_main in content:
            return  # already up to date
        python_path = sys.executable
        new_svc = (
            "[Unit]\n"
            "Description=sumone Telegram Bot\n\n"
            "[Service]\n"
            f"ExecStart={python_path} {bot_main}\n"
            "Restart=always\n"
            "RestartSec=5\n"
            f"Environment=HOME={os.path.expanduser('~')}\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        with open(svc_path, "w", encoding="utf-8") as f:
            f.write(new_svc)
        import subprocess
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True, timeout=10,
        )
        config.log.info("systemd service updated: %s", svc_path)
    except Exception as e:
        config.log.warning("Failed to update systemd service: %s", e)


def main():
    _migrate_old_layout()
    _bootstrap_files()
    i18n.load(config.LANG)
    _apply_default_model()
    _detect_cli_status()

    def sig_handler(signum, frame):
        log.info("Signal %s, exiting.", signum)
        cli_watcher.stop()
        _stop_file_viewer()
        with state.lock:
            if state.ai_proc and state.ai_proc.poll() is None:
                state.ai_proc.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    if not IS_WINDOWS:
        signal.signal(signal.SIGTERM, sig_handler)
    else:
        try:
            signal.signal(signal.SIGBREAK, sig_handler)
        except (AttributeError, OSError):
            pass

    _acquire_instance_lock()
    poll_loop()


if __name__ == "__main__":
    main()
