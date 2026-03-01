"""CLI connection flow via PTY + Telegram interactivity.

Handles interactive CLI auth (claude/codex/gemini) by:
- Running auth command in a PTY
- Parsing output for prompts (y/n, menus, URL, text input)
- Sending prompts to Telegram as inline keyboard or text request
- Forwarding user responses back to the PTY stdin
"""
import base64
import json
import os
import re
import sys
import subprocess
if sys.platform != "win32":
    import pty
    import select
    import termios
import threading
import time
import hashlib
import urllib.error
import urllib.parse
import urllib.request

import i18n
from config import AI_MODELS, IS_WINDOWS, log
from state import get_provider_env, set_provider_auth

# Sent via Telegram
from telegram import send_html, tg_api, CHAT_ID

# Prompt detection patterns
_RE_URL = re.compile(r'https?://\S+', re.IGNORECASE)
_RE_YN = re.compile(r'\(y(?:/n|es)?\)|y/n|yes/no', re.IGNORECASE)
_RE_MENU_ITEM = re.compile(r'(?:^|\n)\s*(?:[❯>*]\s*|\d+[.)]\s*)(.+)', re.MULTILINE)
_ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*[mABCDEFGHJKLMPSTfhilnpqrsu]|\x1b\[\?[0-9;]*[hl]|\x1b[=>]|\r')
# State for active connection flow
_connect_state = {
    "active": False,
    "provider": None,
    "fd": None,               # PTY master fd
    "pid": None,              # child PID
    "waiting": None,          # "yn" | "menu" | "text" | None
    "menu_items": [],
    "msg_id": None,           # last bot message id
    "url_prompt_sent": False, # avoid duplicate OAuth prompt messages
    "last_user_input": "",    # used to mask echoed auth code from PTY output
    "oauth_code_verifier": "",
    "oauth_state": "",
}
_connect_lock = threading.Lock()


def _strip_ansi(text):
    return _ANSI_ESCAPE.sub('', text)


def _sanitize_cli_output(text):
    """Mask sensitive user-entered input that may be echoed by the CLI."""
    clean = _strip_ansi(text)
    with _connect_lock:
        secret = (_connect_state.get("last_user_input") or "").strip()
    # Only mask long values (e.g., OAuth auth codes), not short menu inputs like "1" or "y".
    if len(secret) >= 8:
        clean = clean.replace(secret, i18n.t("ai_connect.input_masked"))
    return clean


def _build_auth_env(provider):
    """Return environment with provider-specific auth variables applied."""
    env = dict(os.environ)
    env.update(get_provider_env(provider))
    return env

def _looks_like_auth_payload(text):
    """Heuristic for pasted auth payloads when the CLI is not requesting input."""
    value = (text or "").strip()
    if len(value) < 24:
        return False
    if "#" in value:
        return True
    return bool(re.fullmatch(r'[A-Za-z0-9/_=-]{24,}', value))


def _parse_claude_auth_payload(text):
    """Parse a Claude browser auth payload into code/state parts."""
    value = (text or "").strip()
    if "#" not in value:
        return None, None
    code, state = value.split("#", 1)
    code = code.strip()
    state = state.strip()
    if not code or not state:
        return None, None
    return code, state


def _b64url(data):
    """Return unpadded base64url bytes."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _make_claude_code_verifier():
    """Generate a PKCE verifier compatible with Claude's manual OAuth flow."""
    return _b64url(os.urandom(32))


def _make_claude_manual_auth_url():
    """Build Claude's manual OAuth URL and return (url, verifier, state)."""
    verifier = _make_claude_code_verifier()
    challenge = _b64url(hashlib.sha256(verifier.encode("utf-8")).digest())
    state = _b64url(os.urandom(32))
    params = {
        "code": "true",
        "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        "response_type": "code",
        "redirect_uri": "https://platform.claude.com/oauth/code/callback",
        "scope": (
            "org:create_api_key "
            "user:profile "
            "user:inference "
            "user:sessions:claude_code "
            "user:mcp_servers"
        ),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    url = "https://claude.ai/oauth/authorize?" + urllib.parse.urlencode(params)
    return url, verifier, state


def _exchange_claude_manual_code(raw_text, expected_state, code_verifier):
    """Exchange a Claude manual auth code for OAuth tokens."""
    code, state = _parse_claude_auth_payload(raw_text)
    if not code or not state:
        raise RuntimeError(i18n.t("ai_connect.invalid_auth_format"))
    if expected_state and state != expected_state:
        raise RuntimeError(i18n.t("ai_connect.state_mismatch"))
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "https://platform.claude.com/oauth/code/callback",
        "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        "code_verifier": code_verifier,
        "state": state,
    }
    req = urllib.request.Request(
        "https://platform.claude.com/v1/oauth/token",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "sumone/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        detail = body[-300:] if body else str(e)
        raise RuntimeError(i18n.t("ai_connect.token_exchange_http_fail", code=e.code, detail=detail)) from e
    except Exception as e:
        raise RuntimeError(i18n.t("ai_connect.token_exchange_fail", error=e)) from e

    access_token = (data or {}).get("access_token")
    refresh_token = (data or {}).get("refresh_token")
    if not access_token or not refresh_token:
        raise RuntimeError(i18n.t("ai_connect.token_no_tokens"))
    account = (data or {}).get("account") or {}
    organization = (data or {}).get("organization") or {}
    auth = {
        "oauth_token": access_token,
        "oauth_refresh_token": refresh_token,
    }
    if account.get("uuid"):
        auth["account_uuid"] = account["uuid"]
    if account.get("email_address"):
        auth["user_email"] = account["email_address"]
    if organization.get("uuid"):
        auth["organization_uuid"] = organization["uuid"]
    return auth

def _detect_prompt(text):
    """Detect what kind of prompt the CLI is showing."""
    clean = _strip_ansi(text)
    lines = [l.strip() for l in clean.splitlines() if l.strip()]
    if not lines:
        return None, []

    # URL
    urls = _RE_URL.findall(clean)
    if urls:
        return "url", urls

    # y/n
    if _RE_YN.search(clean):
        return "yn", []

    # Numbered menu (3+ items that look like a list)
    items = []
    for line in lines:
        m = re.match(r'^\s*(?:\d+[.)]\s*|[❯>*]\s*)(.+)', line)
        if m:
            items.append(m.group(1).strip())
    if len(items) >= 2:
        return "menu", items

    # Generic text prompt (ends with : or ?)
    last = lines[-1]
    if last.endswith(':') or last.endswith('?') or last.endswith('> '):
        return "text", [last]

    return None, []


def _send_prompt_to_telegram(provider, prompt_type, data, raw_text):
    """Send the detected prompt as an appropriate Telegram message."""
    prov_label = AI_MODELS.get(provider, {}).get("label", provider.title())
    clean = _strip_ansi(raw_text).strip()

    if prompt_type == "url":
        url = data[0]
        # Extract device code if present (e.g. "3XQR-0J450")
        code_match = re.search(r'\b([A-Z0-9]{4}-[A-Z0-9]{4,6})\b', clean)
        code_line = "\n\n" + i18n.t("ai_connect.auth_code", code=code_match.group(1)) if code_match else ""
        # Check if auth code input is needed (Gemini)
        needs_code = "authorization code" in clean.lower()
        if provider == "claude":
            footer = "\n\n" + i18n.t("ai_connect.url_footer_claude")
        elif needs_code:
            footer = "\n\n" + i18n.t("ai_connect.url_footer_code_needed")
        else:
            footer = "\n\n" + i18n.t("ai_connect.url_footer_no_code")
        buttons = [[{"text": i18n.t("ai_connect.auth_button"), "url": url}]]
        result = tg_api("sendMessage", {
            "chat_id": CHAT_ID,
            "text": i18n.t("ai_connect.url_message", label=prov_label, code_line=code_line, footer=footer),
            "parse_mode": "HTML",
            "reply_markup": json.dumps({"inline_keyboard": buttons}),
        })
        return (result or {}).get("result", {}).get("message_id")

    elif prompt_type == "yn":
        buttons = [[
            {"text": i18n.t("ai_connect.yes"), "callback_data": "connect:y"},
            {"text": i18n.t("ai_connect.no"), "callback_data": "connect:n"},
        ]]
        result = tg_api("sendMessage", {
            "chat_id": CHAT_ID,
            "text": f"🔌 <b>{prov_label}</b>\n<code>{clean[-200:]}</code>",
            "parse_mode": "HTML",
            "reply_markup": json.dumps({"inline_keyboard": buttons}),
        })
        return (result or {}).get("result", {}).get("message_id")

    elif prompt_type == "menu":
        items = data
        buttons = [[{"text": f"{i+1}. {item[:40]}", "callback_data": f"connect:{i+1}"}]
                   for i, item in enumerate(items[:10])]
        result = tg_api("sendMessage", {
            "chat_id": CHAT_ID,
            "text": f"{i18n.t('ai_connect.select_prompt', label=prov_label)}\n<code>{clean[-300:]}</code>",
            "parse_mode": "HTML",
            "reply_markup": json.dumps({"inline_keyboard": buttons}),
        })
        with _connect_lock:
            _connect_state["menu_items"] = items
        return (result or {}).get("result", {}).get("message_id")

    elif prompt_type == "text":
        result = tg_api("sendMessage", {
            "chat_id": CHAT_ID,
            "text": f"🔌 <b>{prov_label}</b>\n<code>{clean[-300:]}</code>\n\n{i18n.t('ai_connect.text_prompt')}",
            "parse_mode": "HTML",
        })
        return (result or {}).get("result", {}).get("message_id")

    return None


def _check_auth(provider, cli_cmd):
    """Check if a provider is authenticated."""
    import subprocess, shutil
    resolved = shutil.which(cli_cmd)
    if not resolved:
        return False
    env = _build_auth_env(provider)
    if provider == "codex":
        try:
            r = subprocess.run([resolved, "login", "status"], capture_output=True, timeout=5, env=env)
            return r.returncode == 0
        except Exception:
            return False
    elif provider == "gemini":
        gdir = os.path.expanduser("~/.gemini")
        return (os.path.isfile(os.path.join(gdir, "oauth_creds.json"))
                or os.path.isfile(os.path.join(gdir, "google_accounts.json")))
    else:  # claude
        try:
            r = subprocess.run([resolved, "auth", "status"], capture_output=True, timeout=5, env=env)
            return r.returncode == 0
        except Exception:
            return False


def _is_cli_installed(cli_cmd):
    """Check if a CLI command is available."""
    import shutil
    return shutil.which(cli_cmd) is not None


def _install_cli(provider, info):
    """Install CLI for the given provider. Returns True on success."""
    import subprocess
    prov_label = info.get("label", provider.title())
    install_cmd = info.get("install_cmd")
    if not install_cmd:
        send_html(i18n.t("ai_connect.no_install_cmd", label=prov_label))
        return False

    send_html(f"{i18n.t('ai_connect.installing', label=prov_label)}\n<code>{' '.join(install_cmd)}</code>")
    log.info("Installing %s CLI: %s", provider, install_cmd)

    try:
        result = subprocess.run(
            install_cmd, capture_output=True, text=True, timeout=300,
            env={**os.environ}
        )
        if result.returncode == 0:
            log.info("%s CLI installed successfully", provider)
            # macOS: remove quarantine attribute to avoid Gatekeeper popup
            import shutil, platform
            if platform.system() == "Darwin":
                cli_path = shutil.which(info.get("cli_cmd", provider))
                if cli_path:
                    try:
                        # Resolve symlink to actual binary
                        real_path = os.path.realpath(cli_path)
                        subprocess.run(["xattr", "-d", "com.apple.quarantine", real_path],
                                       capture_output=True, timeout=10)
                        subprocess.run(["xattr", "-d", "com.apple.quarantine", cli_path],
                                       capture_output=True, timeout=10)
                        log.info("Removed quarantine from %s", real_path)
                    except Exception:
                        pass
            send_html(i18n.t("ai_connect.install_ok", label=prov_label))
            return True
        else:
            err_msg = (result.stderr or result.stdout or "unknown error")[-500:]
            log.error("%s CLI install failed: %s", provider, err_msg)
            send_html(f"{i18n.t('ai_connect.install_fail', label=prov_label)}\n<code>{err_msg}</code>")
            return False
    except subprocess.TimeoutExpired:
        send_html(i18n.t("ai_connect.install_timeout", label=prov_label))
        return False
    except Exception as e:
        send_html(i18n.t("ai_connect.install_error", label=prov_label, error=e))
        return False


def _ensure_gemini_oauth_mode():
    """Ensure Gemini CLI uses oauth-personal auth for connect flow."""
    gemini_dir = os.path.expanduser("~/.gemini")
    settings_path = os.path.join(gemini_dir, "settings.json")
    os.makedirs(gemini_dir, exist_ok=True)

    data = {}
    if os.path.isfile(settings_path):
        try:
            with open(settings_path, encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            data = {}

    security = data.get("security") if isinstance(data.get("security"), dict) else {}
    auth = security.get("auth") if isinstance(security.get("auth"), dict) else {}
    auth["selectedType"] = "oauth-personal"
    security["auth"] = auth
    data["security"] = security

    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info("Gemini auth mode set to oauth-personal: %s", settings_path)


def run_connect_flow(provider):
    """Run CLI install (if needed) + auth flow for the given provider."""
    global _connect_state

    info = AI_MODELS.get(provider)
    if not info:
        send_html(i18n.t("ai_connect.unknown_provider", name=provider))
        return

    cli_cmd = info.get("cli_cmd", provider)
    prov_label = info.get("label", provider.title())

    # Step 1: Install if not available
    if not _is_cli_installed(cli_cmd):
        if not _install_cli(provider, info):
            return
        # Re-check after install
        if not _is_cli_installed(cli_cmd):
            send_html(i18n.t("ai_connect.cli_not_found", cmd=cli_cmd))
            return

    # Step 2: Clear existing auth for re-authentication
    if provider == "gemini":
        try:
            _ensure_gemini_oauth_mode()
        except Exception as e:
            log.error("Failed to prepare Gemini auth mode: %s", e)
            send_html(i18n.t("ai_connect.setup_fail", label=prov_label, error=e))
            return
        for f in ["oauth_creds.json", "google_accounts.json"]:
            p = os.path.expanduser(f"~/.gemini/{f}")
            if os.path.isfile(p):
                os.remove(p)
                log.info("Removed %s for re-auth", p)

    # Step 3: Auth flow
    auth_args = info.get("auth_cmd", [cli_cmd, "auth", "login"])
    log.info("Starting connect flow for %s: %s", provider, auth_args)

    if provider == "claude":
        url, verifier, oauth_state = _make_claude_manual_auth_url()
        with _connect_lock:
            _connect_state.update({
                "active": True,
                "provider": provider,
                "fd": None,
                "pid": None,
                "waiting": None,
                "menu_items": [],
                "msg_id": None,
                "url_prompt_sent": True,
                "last_user_input": "",
                "oauth_code_verifier": verifier,
                "oauth_state": oauth_state,
            })
        send_html(i18n.t("ai_connect.connecting", label=prov_label))
        msg_id = _send_prompt_to_telegram(provider, "url", [url], url)
        with _connect_lock:
            _connect_state["msg_id"] = msg_id
        prompt_msg_id = _send_prompt_to_telegram(
            provider,
            "text",
            ["Paste code here if prompted > "],
            "Paste code here if prompted > ",
        )
        with _connect_lock:
            _connect_state["msg_id"] = prompt_msg_id
        return

    if IS_WINDOWS:
        send_html(i18n.t("ai_connect.pty_not_supported"))
        return

    import shutil
    resolved_cmd = shutil.which(auth_args[0]) or auth_args[0]
    resolved_args = [resolved_cmd] + auth_args[1:]

    try:
        pid, fd = pty.fork()
    except Exception as e:
        send_html(i18n.t("ai_connect.pty_fail", error=e))
        return

    if pid == 0:
        # Child process — prevent CLI from auto-opening browser
        os.environ["BROWSER"] = "echo"
        os.environ["DISPLAY"] = ""
        if provider == "gemini":
            os.environ["NO_BROWSER"] = "true"
        try:
            os.execvp(resolved_cmd, resolved_args)
        except Exception:
            os._exit(1)

    # Parent process — disable PTY echo to prevent input being echoed back
    try:
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except Exception:
        pass

    with _connect_lock:
        _connect_state.update({
            "active": True,
            "provider": provider,
            "fd": fd,
            "pid": pid,
            "waiting": None,
            "menu_items": [],
            "msg_id": None,
            "url_prompt_sent": False,
            "last_user_input": "",
            "oauth_code_verifier": "",
            "oauth_state": "",
        })

    send_html(i18n.t("ai_connect.connecting", label=prov_label))

    buf = ""
    last_output_time = time.time()
    url_prompt_time = None
    IDLE_TIMEOUT = 3.0  # seconds of no output before treating as prompt
    URL_WAIT_TIMEOUT = 300.0

    try:
        while True:
            try:
                ready, _, _ = select.select([fd], [], [], 0.3)
            except (ValueError, OSError):
                break

            if ready:
                try:
                    chunk = os.read(fd, 4096).decode("utf-8", errors="replace")
                    buf += chunk
                    last_output_time = time.time()
                except OSError:
                    break
            else:
                # No new data — check if we've been idle long enough
                if buf and (time.time() - last_output_time) >= IDLE_TIMEOUT:
                    prompt_type, prompt_data = _detect_prompt(buf)
                    if prompt_type:
                        if prompt_type == "url":
                            with _connect_lock:
                                if _connect_state.get("url_prompt_sent"):
                                    # Gemini can redraw URL blocks; ignore repeated URL prompts.
                                    buf = ""
                                    continue
                                _connect_state["url_prompt_sent"] = True
                        # Gemini requires auth code input after URL
                        needs_input = (prompt_type == "url" and "authorization code" in _strip_ansi(buf).lower())
                        with _connect_lock:
                            if prompt_type == "url" and not needs_input:
                                _connect_state["waiting"] = None
                            else:
                                _connect_state["waiting"] = "text" if needs_input else prompt_type
                        msg_id = _send_prompt_to_telegram(provider, prompt_type, prompt_data, buf)
                        with _connect_lock:
                            _connect_state["msg_id"] = msg_id
                        if prompt_type == "url" and not needs_input:
                            url_prompt_time = time.time()
                        buf = ""
                        if needs_input:
                            # Wait for user response (handled by handle_connect_response)
                            _wait_for_user_input(fd, provider)
                            buf = ""
                            if not is_connect_active():
                                return
                            # Auth code entered, now wait for process to finish
                            _wait_for_completion(pid, fd, provider, prov_label)
                            return
                    else:
                        # Non-prompt output — show progress
                        clean = _sanitize_cli_output(buf).strip()
                        if clean and len(clean) > 5:
                            send_html(f"<code>{clean[-500:]}</code>")
                        buf = ""

            # Check if process ended
            try:
                result = os.waitpid(pid, os.WNOHANG)
                if result[0] != 0:
                    break
            except ChildProcessError:
                break

            if url_prompt_time and (time.time() - url_prompt_time) >= URL_WAIT_TIMEOUT:
                _cancel_connect_flow(i18n.t("ai_connect.auth_timeout", label=prov_label))
                return

    except Exception as e:
        log.error("Connect flow error: %s", e)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        with _connect_lock:
            _connect_state["active"] = False
            _connect_state["fd"] = None
            _connect_state["pid"] = None
            _connect_state["last_user_input"] = ""
            _connect_state["oauth_code_verifier"] = ""
            _connect_state["oauth_state"] = ""

    # Check exit
    clean_buf = _sanitize_cli_output(buf).strip()
    if clean_buf:
        send_html(f"<code>{clean_buf[-300:]}</code>")

    # Re-detect CLI + auth status
    from state import state as _st
    authenticated = _check_auth(provider, cli_cmd)
    _st.cli_status[provider] = authenticated

    if authenticated:
        send_html(i18n.t("ai_connect.connected", label=prov_label))
    else:
        lower_buf = clean_buf.lower()
        if provider == "gemini" and (
            "interactive consent could not be obtained" in lower_buf
            or "please set an auth method" in lower_buf
            or "authorization code" in lower_buf
        ):
            send_html(i18n.t("ai_connect.gemini_fail"))
            return
        send_html(i18n.t("ai_connect.connect_fail", label=prov_label))


def _wait_for_completion(pid, fd, provider, prov_label):
    """Wait for process to finish after OAuth URL was shown."""
    log.info("Waiting for %s auth completion...", provider)
    timeout = 300  # 5 minutes
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = os.waitpid(pid, os.WNOHANG)
            if r[0] != 0:
                break
        except ChildProcessError:
            break
        try:
            ready, _, _ = select.select([fd], [], [], 1.0)
            if ready:
                os.read(fd, 4096)
        except OSError:
            break
        time.sleep(0.5)

    with _connect_lock:
        _connect_state["active"] = False
        _connect_state["oauth_code_verifier"] = ""
        _connect_state["oauth_state"] = ""

    from state import state as _st
    cli_cmd = AI_MODELS.get(provider, {}).get("cli_cmd", provider)
    authenticated = _check_auth(provider, cli_cmd)
    _st.cli_status[provider] = authenticated

    if authenticated:
        send_html(i18n.t("ai_connect.connected", label=prov_label))
    else:
        send_html(i18n.t("ai_connect.auth_restart_hint"))


def _cancel_connect_flow(message):
    """Abort the active connect flow and notify the user."""
    pid = None
    fd = None
    with _connect_lock:
        pid = _connect_state.get("pid")
        fd = _connect_state.get("fd")
        _connect_state["active"] = False
        _connect_state["waiting"] = None
        _connect_state["_pending_response"] = None
        _connect_state["oauth_code_verifier"] = ""
        _connect_state["oauth_state"] = ""
    try:
        if pid:
            os.kill(pid, 15)
    except OSError:
        pass
    try:
        if fd is not None:
            os.close(fd)
    except OSError:
        pass
    send_html(message)


def _wait_for_user_input(fd, provider):
    """Block until user sends a response via Telegram (handle_connect_response sets it)."""
    with _connect_lock:
        response = (_connect_state.get("_pending_response") or "").strip()
        if response:
            _connect_state["_pending_response"] = None
            _connect_state["last_user_input"] = response
    if response:
        try:
            os.write(fd, (response + "\n").encode("utf-8"))
            log.info("Wrote to PTY: %r", i18n.t("ai_connect.input_masked") if len(response) >= 8 else response)
        except OSError as e:
            log.error("PTY write error: %s", e)
        return

    _user_response_event.clear()
    timeout = 120
    if not _user_response_event.wait(timeout=timeout):
        send_html(i18n.t("ai_connect.input_timeout"))
        with _connect_lock:
            _connect_state["active"] = False
        return

    with _connect_lock:
        response = _connect_state.get("_pending_response", "")
        _connect_state["_pending_response"] = None
        _connect_state["last_user_input"] = (response or "").strip()

    if response is None:
        return

    # Write response to PTY
    try:
        os.write(fd, (response + "\n").encode("utf-8"))
        log.info("Wrote to PTY: %r", response)
    except OSError as e:
        log.error("PTY write error: %s", e)


_user_response_event = threading.Event()


def handle_connect_response(text):
    """Called from main polling loop when user sends a message during connect flow."""
    with _connect_lock:
        if not _connect_state["active"]:
            return False
        provider = _connect_state.get("provider")
        waiting = _connect_state.get("waiting")

    if not waiting:
        if provider == "claude" and _looks_like_auth_payload(text):
            with _connect_lock:
                url_prompt_sent = bool(_connect_state.get("url_prompt_sent"))
                code_verifier = (_connect_state.get("oauth_code_verifier") or "").strip()
                expected_state = (_connect_state.get("oauth_state") or "").strip()
                if not _connect_state["active"]:
                    return False
            if not url_prompt_sent:
                send_html(i18n.t("ai_connect.claude_auth_early"))
                return True
            if code_verifier:
                try:
                    auth = _exchange_claude_manual_code(text.strip(), expected_state, code_verifier)
                    set_provider_auth("claude", auth)
                    from state import state as _st
                    _st.cli_status["claude"] = _check_auth("claude", AI_MODELS.get("claude", {}).get("cli_cmd", "claude"))
                except Exception as e:
                    send_html(
                        f"{i18n.t('ai_connect.claude_token_fail')}\n"
                        f"<code>{_strip_ansi(str(e))[-300:]}</code>"
                    )
                    return True
                with _connect_lock:
                    _connect_state["active"] = False
                    _connect_state["waiting"] = None
                    _connect_state["last_user_input"] = ""
                    _connect_state["url_prompt_sent"] = False
                    _connect_state["oauth_code_verifier"] = ""
                    _connect_state["oauth_state"] = ""
                send_html(i18n.t("ai_connect.connected", label="Claude"))
                return True
            return True
        return False

    with _connect_lock:
        if not _connect_state["active"]:
            return False
        waiting = _connect_state.get("waiting")
        if not waiting:
            return False

        if waiting == "menu":
            # Convert number to menu item index
            try:
                idx = int(text.strip()) - 1
                items = _connect_state.get("menu_items", [])
                if 0 <= idx < len(items):
                    # Send appropriate arrow key count + enter
                    _connect_state["_pending_response"] = str(idx)
                    _connect_state["waiting"] = None
                else:
                    send_html(i18n.t("ai_connect.invalid_menu_choice", max=len(items)))
                    return True
            except ValueError:
                send_html(i18n.t("ai_connect.enter_number"))
                return True
        else:
            _connect_state["_pending_response"] = text.strip()
            _connect_state["waiting"] = None

    _user_response_event.set()
    return True


def handle_connect_callback(data):
    """Called from callback handler when user taps inline keyboard button during connect flow."""
    with _connect_lock:
        if not _connect_state["active"]:
            return False
        waiting = _connect_state.get("waiting")
        if not waiting:
            return False

        if waiting == "yn":
            _connect_state["_pending_response"] = data  # "y" or "n"
            _connect_state["waiting"] = None
        elif waiting == "menu":
            try:
                idx = int(data) - 1
                items = _connect_state.get("menu_items", [])
                if 0 <= idx < len(items):
                    _connect_state["_pending_response"] = str(idx)
                    _connect_state["waiting"] = None
                else:
                    return False
            except ValueError:
                return False
        else:
            _connect_state["_pending_response"] = data
            _connect_state["waiting"] = None

    _user_response_event.set()
    return True


def is_connect_active():
    with _connect_lock:
        return _connect_state["active"]
