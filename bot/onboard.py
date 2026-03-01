#!/usr/bin/env python3
"""Sumone CLI Onboarding TUI.

Interactive setup wizard run after initial installation.
Uses arrow keys for selection (curses on Unix, msvcrt on Windows).

Steps:
  1. Theme (system / dark / light)
  2. Snapshot retention (3 / 7 / 14 / 30 days)
  3. AI Providers (multi-select: Claude, Codex, Gemini — min 1)
  4. CLI install + auth per selected provider
  5. Default provider (if multiple selected)
  6. Default sub-model for default provider
"""
import json
import os
import platform
import shutil
import subprocess
import sys

ROOT_DIR = os.path.expanduser("~/.sumone")
CONFIG_DIR = os.path.join(ROOT_DIR, "config")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
IS_WINDOWS = platform.system() == "Windows"

# ---------------------------------------------------------------------------
# i18n (minimal, self-contained for standalone use)
# ---------------------------------------------------------------------------
_ONBOARD_I18N = {
    "ko": {
        "welcome": "Sumone 초기 설정",
        "welcome_desc": "화살표 키(↑↓)로 선택, Enter로 확정",
        "step": "단계",
        "theme": "테마 선택",
        "theme_desc": "파일 뷰어 색상 테마",
        "snapshot": "스냅샷 보관 기간",
        "snapshot_desc": "파일 수정 기록 보관 일수",
        "ai_providers": "AI 제공자 선택",
        "ai_providers_desc": "사용할 AI CLI (Space 선택, 최소 1개)",
        "multi_hint": "↑↓ 이동  Space 선택/해제  Enter 확정",
        "default_provider": "기본 AI",
        "default_provider_desc": "새 세션에서 기본으로 사용할 AI",
        "sub_model": "세부 모델",
        "sub_model_desc": "기본 모델 (새 세션에 적용)",
        "done": "설정 완료!",
        "done_desc": "봇을 실행하면 설정이 적용됩니다.",
        "days": "일",
        "system": "시스템 (OS 설정 따름)",
        "dark": "다크",
        "light": "라이트",
        "ai_setup": "AI CLI 설정",
        "cli_found": "✓ {name} CLI 설치됨",
        "cli_missing": "✗ {name} CLI 미설치",
        "installing": "  {name} 설치 중...",
        "install_ok": "  ✓ 설치 완료",
        "install_fail": "  ✗ 설치 실패 — 수동 설치 필요",
        "install_manual": "    → {cmd}",
        "auth_start": "  {name} 인증을 시작합니다...",
        "auth_ok": "  ✓ {name} 인증 완료",
        "auth_fail": "  ✗ {name} 인증 실패 — 봇에서 재시도 가능",
        "press_enter": "  Enter를 눌러 계속...",
        "min_one": "⚠ 최소 1개를 선택하세요",
        "bot_token": "Telegram 봇 토큰",
        "bot_token_desc": "@BotFather에서 받은 토큰을 입력하세요",
        "bot_token_hint": "예: 123456789:ABCdefGhIjKlMnOpQrStUvWxYz",
        "bot_token_invalid": "⚠ 유효하지 않은 토큰입니다. 다시 입력하세요.",
        "bot_token_ok": "✓ 봇 확인됨: {name}",
        "chat_id": "Chat ID",
        "chat_id_desc": "봇과 대화할 Telegram Chat ID",
        "chat_id_auto": "✓ 자동 감지됨: {chat_id}",
        "chat_id_manual": "자동 감지 실패 — 직접 입력하세요 (봇에게 /start 메시지를 보낸 후 재시도)",
        "chat_id_hint": "숫자만 입력 (예: 123456789)",
        "auth_exists": "  ✓ {name} 이미 인증됨 — 건너뜀",
    },
    "en": {
        "welcome": "Sumone Initial Setup",
        "welcome_desc": "Use arrow keys (↑↓) to select, Enter to confirm",
        "step": "Step",
        "theme": "Theme",
        "theme_desc": "File viewer color theme",
        "snapshot": "Snapshot Retention",
        "snapshot_desc": "Days to keep file modification history",
        "ai_providers": "AI Providers",
        "ai_providers_desc": "AI CLIs to use (Space to toggle, min 1)",
        "multi_hint": "↑↓ Move  Space Toggle  Enter Confirm",
        "default_provider": "Default AI",
        "default_provider_desc": "Default AI for new sessions",
        "sub_model": "Sub-Model",
        "sub_model_desc": "Default model (applied to new sessions)",
        "done": "Setup Complete!",
        "done_desc": "Settings will be applied when the bot starts.",
        "days": "days",
        "system": "System (follow OS)",
        "dark": "Dark",
        "light": "Light",
        "ai_setup": "AI CLI Setup",
        "cli_found": "✓ {name} CLI installed",
        "cli_missing": "✗ {name} CLI not installed",
        "installing": "  Installing {name}...",
        "install_ok": "  ✓ Installed",
        "install_fail": "  ✗ Install failed — manual install required",
        "install_manual": "    → {cmd}",
        "auth_start": "  Starting {name} authentication...",
        "auth_ok": "  ✓ {name} authenticated",
        "auth_fail": "  ✗ {name} auth failed — can retry from bot",
        "press_enter": "  Press Enter to continue...",
        "min_one": "⚠ Select at least one",
        "bot_token": "Telegram Bot Token",
        "bot_token_desc": "Enter the token from @BotFather",
        "bot_token_hint": "e.g. 123456789:ABCdefGhIjKlMnOpQrStUvWxYz",
        "bot_token_invalid": "⚠ Invalid token. Please try again.",
        "bot_token_ok": "✓ Bot verified: {name}",
        "chat_id": "Chat ID",
        "chat_id_desc": "Telegram Chat ID for the bot",
        "chat_id_auto": "✓ Auto-detected: {chat_id}",
        "chat_id_manual": "Auto-detect failed — enter manually (send /start to the bot first, then retry)",
        "chat_id_hint": "Numbers only (e.g. 123456789)",
        "auth_exists": "  ✓ {name} already authenticated — skipping",
    },
}

AI_PROVIDERS = {
    "claude": {
        "label": "Claude (Anthropic)",
        "cli_cmd": "claude",
        "install_cmds": [
            ["npm", "install", "-g", "@anthropic-ai/claude-code"],
        ],
        "auth_cmd": ["claude", "auth", "login"],
        "default_sub": "sonnet",
        "sub_models": ["haiku", "sonnet", "opus"],
    },
    "codex": {
        "label": "Codex (OpenAI)",
        "cli_cmd": "codex",
        "install_cmds": [
            ["brew", "install", "codex"],
            ["npm", "install", "-g", "@openai/codex"],
        ],
        "auth_cmd": ["codex", "login", "--device-auth"],
        "default_sub": "codex",
        "sub_models": ["codex-mini", "codex", "codex-max"],
    },
    "gemini": {
        "label": "Gemini (Google)",
        "cli_cmd": "gemini",
        "install_cmds": [
            ["brew", "install", "gemini-cli"],
            ["npm", "install", "-g", "@google/gemini-cli"],
        ],
        "auth_cmd": ["gemini", "-p", "hello"],
        "default_sub": "flash",
        "sub_models": ["flash", "pro"],
    },
}


def _t(lang, key):
    return _ONBOARD_I18N.get(lang, _ONBOARD_I18N["en"]).get(key, key)


# ---------------------------------------------------------------------------
# Cross-platform key input
# ---------------------------------------------------------------------------
def _getch_unix():
    import tty
    import termios
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == '\x1b':
            ch2 = sys.stdin.read(1)
            if ch2 == '[':
                ch3 = sys.stdin.read(1)
                if ch3 == 'A':
                    return 'UP'
                elif ch3 == 'B':
                    return 'DOWN'
            return 'ESC'
        elif ch in ('\r', '\n'):
            return 'ENTER'
        elif ch == ' ':
            return 'SPACE'
        elif ch == '\x03':
            raise KeyboardInterrupt
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _getch_windows():
    import msvcrt
    ch = msvcrt.getwch()
    if ch == '\r':
        return 'ENTER'
    if ch == ' ':
        return 'SPACE'
    if ch == '\x03':
        raise KeyboardInterrupt
    if ch in ('\x00', '\xe0'):
        ch2 = msvcrt.getwch()
        if ch2 == 'H':
            return 'UP'
        elif ch2 == 'P':
            return 'DOWN'
        return None
    return ch


def _getch():
    if IS_WINDOWS:
        return _getch_windows()
    return _getch_unix()


# ---------------------------------------------------------------------------
# TUI rendering
# ---------------------------------------------------------------------------
def _clear_screen():
    os.system('cls' if IS_WINDOWS else 'clear')


def _header():
    return (f"\n  ╔{'═' * 50}╗\n"
            f"  ║  {'sumone — Omni AI Orchestration':^46}  ║\n"
            f"  ╚{'═' * 50}╝\n")


def _render_menu(lang, step_num, total_steps, title, desc, options, selected):
    _clear_screen()
    print(_header())
    print(f"  {_t(lang, 'step')} {step_num}/{total_steps}: {title}")
    print(f"  {desc}\n")

    for i, opt in enumerate(options):
        if i == selected:
            print(f"  ▸ \033[1;36m{opt}\033[0m")
        else:
            print(f"    {opt}")

    print(f"\n  ↑↓ {_t(lang, 'welcome_desc').split(',')[0].strip()}")
    print(f"  Enter {_t(lang, 'welcome_desc').split(',')[1].strip()}")


def _render_multi_menu(lang, step_num, total_steps, title, desc,
                       options, cursor, checked, warn=False):
    _clear_screen()
    print(_header())
    print(f"  {_t(lang, 'step')} {step_num}/{total_steps}: {title}")
    print(f"  {desc}\n")

    for i, opt in enumerate(options):
        mark = "✓" if checked[i] else " "
        if i == cursor:
            print(f"  ▸ [{mark}] \033[1;36m{opt}\033[0m")
        else:
            print(f"    [{mark}] {opt}")

    if warn:
        print(f"\n  \033[1;33m{_t(lang, 'min_one')}\033[0m")
    else:
        print()

    print(f"  {_t(lang, 'multi_hint')}")


def _text_input(lang, step_num, total_steps, title, desc, hint="",
                validate=None, error_msg=""):
    """Text input with TUI header. Loops until validate() returns truthy."""
    while True:
        _clear_screen()
        print(_header())
        print(f"  {_t(lang, 'step')} {step_num}/{total_steps}: {title}")
        print(f"  {desc}\n")
        if hint:
            print(f"  {hint}\n")
        value = input("  > ").strip()
        if not value:
            continue
        if validate is None:
            return value
        result = validate(value)
        if result:
            return result if isinstance(result, str) else value
        if error_msg:
            print(f"\n  {error_msg}")
            input(f"\n  {_t(lang, 'press_enter')}")


def _select(lang, step_num, total_steps, title, desc, options):
    """Single-select menu. Returns selected index."""
    selected = 0
    while True:
        _render_menu(lang, step_num, total_steps, title, desc, options, selected)
        key = _getch()
        if key == 'UP':
            selected = (selected - 1) % len(options)
        elif key == 'DOWN':
            selected = (selected + 1) % len(options)
        elif key == 'ENTER':
            return selected


def _multi_select(lang, step_num, total_steps, title, desc, options,
                  defaults=None):
    """Multi-select menu. Returns list of selected indices (min 1)."""
    cursor = 0
    checked = list(defaults) if defaults else [False] * len(options)
    warn = False
    while True:
        _render_multi_menu(lang, step_num, total_steps, title, desc,
                           options, cursor, checked, warn=warn)
        key = _getch()
        warn = False
        if key == 'UP':
            cursor = (cursor - 1) % len(options)
        elif key == 'DOWN':
            cursor = (cursor + 1) % len(options)
        elif key == 'SPACE':
            checked[cursor] = not checked[cursor]
        elif key == 'ENTER':
            selected = [i for i, c in enumerate(checked) if c]
            if not selected:
                warn = True
            else:
                return selected


# ---------------------------------------------------------------------------
# CLI install & auth helpers
# ---------------------------------------------------------------------------
def _validate_bot_token(token):
    """Validate bot token via Telegram getMe API. Returns bot name or None."""
    import urllib.request
    try:
        url = f"https://api.telegram.org/bot{token}/getMe"
        resp = urllib.request.urlopen(url, timeout=10)
        data = json.loads(resp.read())
        if data.get("ok"):
            bot_info = data.get("result", {})
            return bot_info.get("first_name") or bot_info.get("username") or "OK"
    except Exception:
        pass
    return None


def _detect_chat_id(token):
    """Try to detect chat ID from recent messages sent to the bot."""
    import urllib.request
    try:
        url = f"https://api.telegram.org/bot{token}/getUpdates?limit=10"
        resp = urllib.request.urlopen(url, timeout=10)
        data = json.loads(resp.read())
        if data.get("ok"):
            for update in reversed(data.get("result", [])):
                msg = update.get("message", {})
                chat = msg.get("chat", {})
                if chat.get("id"):
                    return str(chat["id"])
    except Exception:
        pass
    return None


def _is_authenticated(provider_key):
    """Check if a provider CLI is already authenticated.

    Uses CLI command first, falls back to credential file detection.
    """
    info = AI_PROVIDERS[provider_key]
    cli = info["cli_cmd"]
    resolved = shutil.which(cli)

    if provider_key == "claude":
        # Method 1: CLI auth status
        if resolved:
            try:
                r = subprocess.run(
                    [resolved, "auth", "status"],
                    capture_output=True, timeout=5,
                )
                if r.returncode == 0:
                    return True
            except Exception:
                pass
        # Method 2: credential files
        claude_dir = os.path.expanduser("~/.claude")
        for name in [".credentials.json", "credentials.json", "oauth_token"]:
            if os.path.isfile(os.path.join(claude_dir, name)):
                return True
        return False

    elif provider_key == "codex":
        if os.environ.get("OPENAI_API_KEY"):
            return True
        # Check codex config
        for p in ["~/.codex/config.json", "~/.config/codex/config.json"]:
            if os.path.isfile(os.path.expanduser(p)):
                return True
        return False

    elif provider_key == "gemini":
        # Check credential file (same as main.py)
        if os.path.isfile(os.path.expanduser("~/.gemini/oauth_creds.json")):
            return True
        if resolved:
            try:
                r = subprocess.run(
                    [resolved, "--version"],
                    capture_output=True, timeout=5,
                )
                return r.returncode == 0
            except Exception:
                pass
        return False

    return False


def _ensure_path():
    """Add common CLI install locations to PATH for detection."""
    extra = []
    if IS_WINDOWS:
        npm_dir = os.path.join(os.environ.get("APPDATA", ""), "npm")
        if os.path.isdir(npm_dir):
            extra.append(npm_dir)
    else:
        for d in [os.path.expanduser("~/.npm-global/bin"),
                  os.path.expanduser("~/.local/bin"),
                  "/opt/homebrew/bin", "/usr/local/bin"]:
            if os.path.isdir(d):
                extra.append(d)
    if extra:
        sep = ";" if IS_WINDOWS else ":"
        os.environ["PATH"] = sep.join(extra) + sep + os.environ.get("PATH", "")


def _is_cli_installed(cli_cmd):
    """Check if a CLI command is on PATH."""
    return shutil.which(cli_cmd) is not None


def _try_install(provider_key, lang):
    """Attempt to install CLI using available package manager.
    Returns True on success.
    """
    info = AI_PROVIDERS[provider_key]
    name = info["label"]

    for cmd in info["install_cmds"]:
        # Skip if package manager (cmd[0]) is not available
        if not shutil.which(cmd[0]):
            continue

        print(f"\n{_t(lang, 'installing').format(name=name)}")
        print(f"    $ {' '.join(cmd)}\n")
        try:
            result = subprocess.run(cmd, timeout=180)
            # Re-check PATH after install
            _ensure_path()
            if result.returncode == 0 and _is_cli_installed(info["cli_cmd"]):
                print(f"\n{_t(lang, 'install_ok')}")
                return True
        except Exception:
            pass

    # All install attempts failed
    print(f"\n{_t(lang, 'install_fail')}")
    # Show first command as manual instruction
    if info["install_cmds"]:
        cmd_str = " ".join(info["install_cmds"][0])
        print(_t(lang, "install_manual").format(cmd=cmd_str))
    return False


def _try_auth(provider_key, lang):
    """Run authentication command interactively.
    Returns True if exit code 0.
    """
    info = AI_PROVIDERS[provider_key]
    name = info["label"]
    cmd = info["auth_cmd"]

    print(f"\n{_t(lang, 'auth_start').format(name=name)}\n")
    try:
        result = subprocess.run(cmd, timeout=300)
        if result.returncode == 0:
            print(f"\n{_t(lang, 'auth_ok').format(name=name)}")
            return True
    except Exception:
        pass

    print(f"\n{_t(lang, 'auth_fail').format(name=name)}")
    return False


def _setup_providers(selected_keys, lang):
    """Install & authenticate each selected provider.

    Shows a progress screen, attempts install for missing CLIs,
    then runs auth for installed CLIs.

    Returns the list of provider keys that are ready (CLI installed).
    """
    _clear_screen()
    print(_header())
    print(f"  {_t(lang, 'ai_setup')}\n")

    _ensure_path()
    ready = []

    for pkey in selected_keys:
        info = AI_PROVIDERS[pkey]
        name = info["label"]
        cli = info["cli_cmd"]

        # 1. Check if CLI is installed
        installed = _is_cli_installed(cli)
        if installed:
            print(f"  {_t(lang, 'cli_found').format(name=name)}")
        else:
            print(f"  {_t(lang, 'cli_missing').format(name=name)}")
            installed = _try_install(pkey, lang)

        # 2. Authenticate (only if CLI is available and not already authed)
        if installed:
            if _is_authenticated(pkey):
                print(f"{_t(lang, 'auth_exists').format(name=name)}")
            else:
                _try_auth(pkey, lang)
            ready.append(pkey)

        print()

    print(_t(lang, "press_enter"), end="")
    input()

    # If nothing installed successfully, keep all selected
    # (user can fix later)
    return ready if ready else selected_keys


# ---------------------------------------------------------------------------
# Main onboarding flow
# ---------------------------------------------------------------------------
def run_onboarding(lang=None):
    """Run the interactive onboarding wizard. Returns the selected settings dict."""
    if not lang:
        lang = "ko"

    total = 7  # max steps (adjusts based on conditions)
    results = {}
    step = 1

    # --- Step 1: Bot Token ---
    def _check_token(token):
        name = _validate_bot_token(token)
        if name:
            print(f"\n  {_t(lang, 'bot_token_ok').format(name=name)}")
            import time; time.sleep(1)
            return True
        return None

    bot_token = _text_input(lang, step, total,
                            _t(lang, "bot_token"),
                            _t(lang, "bot_token_desc"),
                            hint=_t(lang, "bot_token_hint"),
                            validate=_check_token,
                            error_msg=_t(lang, "bot_token_invalid"))
    results["bot_token"] = bot_token
    step += 1

    # --- Step 2: Chat ID (auto-detect first) ---
    detected_id = _detect_chat_id(bot_token)
    if detected_id:
        _clear_screen()
        print(_header())
        print(f"  {_t(lang, 'step')} {step}/{total}: {_t(lang, 'chat_id')}")
        print(f"  {_t(lang, 'chat_id_desc')}\n")
        print(f"  {_t(lang, 'chat_id_auto').format(chat_id=detected_id)}")
        input(f"\n  {_t(lang, 'press_enter')}")
        results["chat_id"] = detected_id
    else:
        chat_id = _text_input(lang, step, total,
                              _t(lang, "chat_id"),
                              _t(lang, "chat_id_manual"),
                              hint=_t(lang, "chat_id_hint"),
                              validate=lambda v: v if v.lstrip("-").isdigit() else None,
                              error_msg=_t(lang, "chat_id_hint"))
        results["chat_id"] = chat_id
    step += 1

    # --- Step 3: Theme ---
    theme_options = [_t(lang, "system"), _t(lang, "dark"), _t(lang, "light")]
    theme_values = ["system", "dark", "light"]
    idx = _select(lang, step, total,
                  _t(lang, "theme"), _t(lang, "theme_desc"), theme_options)
    results["theme"] = theme_values[idx]
    step += 1

    # --- Step 4: Snapshot retention ---
    days_label = _t(lang, "days")
    snap_options = [f"3 {days_label}", f"7 {days_label}",
                    f"14 {days_label}", f"30 {days_label}"]
    snap_values = [3, 7, 14, 30]
    idx = _select(lang, step, total,
                  _t(lang, "snapshot"), _t(lang, "snapshot_desc"), snap_options)
    results["snapshot_ttl_days"] = snap_values[idx]
    step += 1

    # --- Step 5: AI Providers (multi-select, min 1) ---
    provider_keys = list(AI_PROVIDERS.keys())
    provider_labels = [AI_PROVIDERS[k]["label"] for k in provider_keys]
    defaults = [k == "claude" for k in provider_keys]  # Claude pre-checked
    sel_indices = _multi_select(lang, step, total,
                                _t(lang, "ai_providers"),
                                _t(lang, "ai_providers_desc"),
                                provider_labels, defaults=defaults)
    selected = [provider_keys[i] for i in sel_indices]
    results["enabled_providers"] = selected
    step += 1

    # --- CLI install + auth (progress screen, not a numbered step) ---
    ready = _setup_providers(selected, lang)

    # Adjust total if single provider (skip default-provider step)
    multiple = len(ready) > 1
    if not multiple:
        total -= 1

    # --- Step 6 (conditional): Default provider ---
    if multiple:
        ready_labels = [AI_PROVIDERS[k]["label"] for k in ready]
        idx = _select(lang, step, total,
                      _t(lang, "default_provider"),
                      _t(lang, "default_provider_desc"),
                      ready_labels)
        default_prov = ready[idx]
        step += 1
    else:
        default_prov = ready[0]
    results["default_model"] = default_prov

    # --- Step 7 (or 6): Sub-model ---
    sub_models = AI_PROVIDERS[default_prov]["sub_models"]
    idx = _select(lang, step, total,
                  _t(lang, "sub_model"), _t(lang, "sub_model_desc"),
                  sub_models)
    results["default_sub_model"] = sub_models[idx]

    # --- Done ---
    _clear_screen()
    print(_header())
    print(f"  ✓ {_t(lang, 'done')}\n")
    print(f"  {_t(lang, 'done_desc')}\n")
    for k, v in results.items():
        val = ", ".join(v) if isinstance(v, list) else v
        print(f"    {k}: {val}")
    print()

    return results


def apply_onboarding(results):
    """Apply onboarding results to config.json."""
    os.makedirs(CONFIG_DIR, exist_ok=True)

    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cfg = {}

    # bot_token and chat_id must be top-level (config.py reads them there)
    TOP_LEVEL_KEYS = {"bot_token", "chat_id"}
    settings_data = {}
    for k, v in results.items():
        if k in TOP_LEVEL_KEYS:
            cfg[k] = v
        else:
            settings_data[k] = v

    settings = cfg.get("settings", {})
    settings.update(settings_data)
    cfg["settings"] = settings

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)

    print(f"  Config saved: {CONFIG_FILE}\n")


def main():
    """Entry point for standalone execution."""
    lang = "ko"
    if len(sys.argv) > 1:
        lang = sys.argv[1]

    try:
        results = run_onboarding(lang)
        apply_onboarding(results)
    except KeyboardInterrupt:
        print("\n\n  Cancelled.\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
