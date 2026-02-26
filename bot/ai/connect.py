"""CLI connection flow via PTY + Telegram interactivity.

Handles interactive CLI auth (claude/codex/gemini) by:
- Running auth command in a PTY
- Parsing output for prompts (y/n, menus, URL, text input)
- Sending prompts to Telegram as inline keyboard or text request
- Forwarding user responses back to the PTY stdin
"""
import os
import re
import pty
import select
import threading
import time

from config import AI_MODELS, IS_WINDOWS, log
from state import state

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
}
_connect_lock = threading.Lock()


def _strip_ansi(text):
    return _ANSI_ESCAPE.sub('', text)


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
        buttons = [[{"text": "🔗 인증하기", "url": url}]]
        result = tg_api("sendMessage", {
            "chat_id": CHAT_ID,
            "text": f"🔌 <b>{prov_label}</b> 인증 링크:\n브라우저에서 인증 후 자동으로 완료됩니다.",
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": buttons},
        })
        return result.get("result", {}).get("message_id")

    elif prompt_type == "yn":
        buttons = [[
            {"text": "✅ 예", "callback_data": f"connect:y"},
            {"text": "❌ 아니오", "callback_data": f"connect:n"},
        ]]
        result = tg_api("sendMessage", {
            "chat_id": CHAT_ID,
            "text": f"🔌 <b>{prov_label}</b>\n<code>{clean[-200:]}</code>",
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": buttons},
        })
        return result.get("result", {}).get("message_id")

    elif prompt_type == "menu":
        items = data
        buttons = [[{"text": f"{i+1}. {item[:40]}", "callback_data": f"connect:{i+1}"}]
                   for i, item in enumerate(items[:10])]
        result = tg_api("sendMessage", {
            "chat_id": CHAT_ID,
            "text": f"🔌 <b>{prov_label}</b> 선택해주세요:\n<code>{clean[-300:]}</code>",
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": buttons},
        })
        with _connect_lock:
            _connect_state["menu_items"] = items
        return result.get("result", {}).get("message_id")

    elif prompt_type == "text":
        result = tg_api("sendMessage", {
            "chat_id": CHAT_ID,
            "text": f"🔌 <b>{prov_label}</b>\n<code>{clean[-300:]}</code>\n\n입력 후 전송해주세요.",
            "parse_mode": "HTML",
        })
        return result.get("result", {}).get("message_id")

    return None


def run_connect_flow(provider):
    """Run CLI auth flow for the given provider in a PTY."""
    global _connect_state

    info = AI_MODELS.get(provider)
    if not info:
        send_html(f"❌ 알 수 없는 provider: {provider}")
        return

    cli_cmd = info.get("cli_cmd", provider)
    auth_args = [cli_cmd, "auth", "login"]
    prov_label = info.get("label", provider.title())

    log.info("Starting connect flow for %s: %s", provider, auth_args)

    if IS_WINDOWS:
        send_html(f"❌ Windows에서는 PTY 연결이 지원되지 않습니다.")
        return

    try:
        pid, fd = pty.fork()
    except Exception as e:
        send_html(f"❌ PTY 생성 실패: {e}")
        return

    if pid == 0:
        # Child process
        try:
            os.execvp(cli_cmd, auth_args)
        except Exception:
            os._exit(1)

    # Parent process
    with _connect_lock:
        _connect_state.update({
            "active": True,
            "provider": provider,
            "fd": fd,
            "pid": pid,
            "waiting": None,
            "menu_items": [],
            "msg_id": None,
        })

    send_html(f"🔌 <b>{prov_label}</b> 연결 중...")

    buf = ""
    last_output_time = time.time()
    IDLE_TIMEOUT = 3.0  # seconds of no output before treating as prompt

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
                        with _connect_lock:
                            _connect_state["waiting"] = prompt_type if prompt_type != "url" else None
                        msg_id = _send_prompt_to_telegram(provider, prompt_type, prompt_data, buf)
                        with _connect_lock:
                            _connect_state["msg_id"] = msg_id
                        buf = ""
                        if prompt_type == "url":
                            # Wait for process to complete after URL auth
                            _wait_for_completion(pid, fd, provider, prov_label)
                            return
                        # Wait for user response (handled by handle_connect_response)
                        _wait_for_user_input(fd, provider)
                        buf = ""
                    else:
                        # Non-prompt output — show progress
                        clean = _strip_ansi(buf).strip()
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

    # Check exit
    clean_buf = _strip_ansi(buf).strip()
    if clean_buf:
        send_html(f"<code>{clean_buf[-300:]}</code>")

    # Re-detect CLI status
    from state import state as _st
    try:
        import subprocess
        r = subprocess.run([cli_cmd, "--version"], capture_output=True, timeout=5)
        _st.cli_status[provider] = (r.returncode == 0)
    except Exception:
        pass

    if _st.cli_status.get(provider):
        send_html(f"✅ <b>{prov_label}</b> 연결 완료!")
    else:
        send_html(f"❌ <b>{prov_label}</b> 연결 실패. 다시 시도해주세요.")


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
                os.read(fd, 4096)  # drain output
        except OSError:
            break
        time.sleep(0.5)

    with _connect_lock:
        _connect_state["active"] = False

    from state import state as _st
    cli_cmd = AI_MODELS.get(provider, {}).get("cli_cmd", provider)
    try:
        import subprocess
        r = subprocess.run([cli_cmd, "--version"], capture_output=True, timeout=5)
        _st.cli_status[provider] = (r.returncode == 0)
    except Exception:
        pass

    if _st.cli_status.get(provider):
        send_html(f"✅ <b>{prov_label}</b> 연결 완료!")
    else:
        send_html(f"⏳ 인증을 완료했다면 <b>/restart_bot</b> 으로 재시작해주세요.")


def _wait_for_user_input(fd, provider):
    """Block until user sends a response via Telegram (handle_connect_response sets it)."""
    _user_response_event.clear()
    timeout = 120
    if not _user_response_event.wait(timeout=timeout):
        send_html("⏰ 입력 시간 초과. 연결을 중단합니다.")
        with _connect_lock:
            _connect_state["active"] = False
        return

    with _connect_lock:
        response = _connect_state.get("_pending_response", "")
        _connect_state["_pending_response"] = None

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
                    send_html(f"1 ~ {len(items)} 사이의 숫자를 입력해주세요.")
                    return True
            except ValueError:
                send_html("숫자를 입력해주세요.")
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
