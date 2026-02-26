#!/usr/bin/env python3
"""Sumone onboarding wizard.

Runs after initial installation to configure:
  - Language
  - AI provider(s) + sub-models
  - CLI installation check
  - Bot token + Chat ID
  - Working directory
  - Theme / snapshot retention
"""
import json
import os
import platform
import subprocess
import sys
import time
import urllib.request

IS_WINDOWS = platform.system() == "Windows"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")

GITHUB_REPO = "xmin-02/sumone"

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
if IS_WINDOWS:
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleMode(
            ctypes.windll.kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass

R  = "\033[0;31m"
G  = "\033[0;32m"
Y  = "\033[1;33m"
C  = "\033[0;36m"
B  = "\033[1;34m"
M  = "\033[0;35m"
W  = "\033[1;37m"
DIM= "\033[2m"
BLD= "\033[1m"
NC = "\033[0m"

def info(s):  print(f"  {C}[INFO]{NC} {s}")
def ok(s):    print(f"  {G}[ OK ]{NC} {s}")
def warn(s):  print(f"  {Y}[WARN]{NC} {s}")
def err(s):   print(f"  {R}[ERR ]{NC} {s}")
def step(n, total, s): print(f"\n  {BLD}{C}[{n}/{total}]{NC}{BLD} {s}{NC}")

# ---------------------------------------------------------------------------
# i18n
# ---------------------------------------------------------------------------
_I18N = {
    "ko": {
        "lang_sel":         "언어 선택",
        "provider_sel":     "사용할 AI 선택 (Space: 선택/해제, Enter: 확인)",
        "provider_min":     "최소 1개 이상 선택해주세요.",
        "submodel_sel":     "{provider} 기본 모델 선택",
        "cli_check":        "CLI 설치 확인",
        "cli_ok":           "{cli} 설치됨",
        "cli_missing":      "{cli} 미설치",
        "cli_install_guide":"설치 명령어:",
        "cli_retry":        "설치 후 Enter를 누르면 재확인합니다...",
        "cli_all_ok":       "모든 CLI 확인 완료",
        "token_step":       "Telegram Bot 토큰",
        "token_guide_1":    "1. @BotFather → /newbot → 토큰 복사",
        "token_prompt":     "Bot Token",
        "token_invalid":    "토큰 형식이 올바르지 않습니다.",
        "chat_step":        "Chat ID 감지",
        "chat_guide":       "봇에게 아무 메시지나 보내주세요... (60초 대기)",
        "chat_fail":        "자동 감지 실패. 수동으로 입력하세요.",
        "chat_prompt":      "Chat ID",
        "chat_invalid":     "Chat ID는 숫자여야 합니다.",
        "workdir_step":     "작업 디렉토리",
        "workdir_prompt":   "경로 (기본값: {default})",
        "workdir_notfound": "디렉토리를 찾을 수 없습니다.",
        "theme_step":       "테마",
        "theme_desc":       "파일 뷰어 색상 테마",
        "snapshot_step":    "스냅샷 보관 기간",
        "snapshot_desc":    "파일 수정 기록 보관 일수",
        "done_title":       "설정 완료!",
        "done_desc":        "봇이 자동으로 시작됩니다.",
        "days":             "일",
        "system":           "시스템 (OS 설정 따름)",
        "dark":             "다크",
        "light":            "라이트",
        "nav":              "↑↓ 이동  Space 선택  Enter 확인",
        "nav_single":       "↑↓ 이동  Enter 확인",
        "cancel":           "취소됨.",
    },
    "en": {
        "lang_sel":         "Select Language",
        "provider_sel":     "Select AI providers (Space: toggle, Enter: confirm)",
        "provider_min":     "Please select at least one provider.",
        "submodel_sel":     "{provider} default model",
        "cli_check":        "CLI Installation Check",
        "cli_ok":           "{cli} is installed",
        "cli_missing":      "{cli} not found",
        "cli_install_guide":"Install with:",
        "cli_retry":        "Install it, then press Enter to re-check...",
        "cli_all_ok":       "All CLIs verified",
        "token_step":       "Telegram Bot Token",
        "token_guide_1":    "1. @BotFather → /newbot → Copy token",
        "token_prompt":     "Bot Token",
        "token_invalid":    "Invalid token format.",
        "chat_step":        "Chat ID Detection",
        "chat_guide":       "Send any message to your bot... (waiting 60s)",
        "chat_fail":        "Auto-detection failed. Enter manually.",
        "chat_prompt":      "Chat ID",
        "chat_invalid":     "Chat ID must be a number.",
        "workdir_step":     "Working Directory",
        "workdir_prompt":   "Path (default: {default})",
        "workdir_notfound": "Directory not found.",
        "theme_step":       "Theme",
        "theme_desc":       "File viewer color theme",
        "snapshot_step":    "Snapshot Retention",
        "snapshot_desc":    "Days to keep file modification history",
        "done_title":       "Setup Complete!",
        "done_desc":        "The bot will start automatically.",
        "days":             "days",
        "system":           "System (follow OS)",
        "dark":             "Dark",
        "light":            "Light",
        "nav":              "↑↓ Move  Space Toggle  Enter Confirm",
        "nav_single":       "↑↓ Move  Enter Confirm",
        "cancel":           "Cancelled.",
    },
}

def _t(lang, key, **kw):
    s = _I18N.get(lang, _I18N["en"]).get(key, key)
    return s.format(**kw) if kw else s


# ---------------------------------------------------------------------------
# AI provider definitions
# ---------------------------------------------------------------------------
AI_PROVIDERS = {
    "claude": {
        "label": "Claude",
        "cli_cmds": ["claude", "claude.cmd"],
        "install": "npm install -g @anthropic-ai/claude-code",
        "sub_models": {
            "haiku":  "claude-haiku-4-5-20251001",
            "sonnet": "claude-sonnet-4-6",
            "opus":   "claude-opus-4-6",
        },
        "default": "sonnet",
    },
    "codex": {
        "label": "Codex (OpenAI)",
        "cli_cmds": ["codex", "codex.cmd"],
        "install": "npm install -g @openai/codex",
        "sub_models": {
            "codex":       "gpt-5.3-codex",
            "codex-max":   "gpt-5.1-codex-max",
            "codex-mini":  "gpt-5.1-codex-mini",
        },
        "default": "codex",
    },
    "gemini": {
        "label": "Gemini (Google)",
        "cli_cmds": ["gemini", "gemini.cmd"],
        "install": "npm install -g @google/gemini-cli",
        "sub_models": {
            "flash": "gemini-2.5-flash",
            "pro":   "gemini-2.5-pro",
        },
        "default": "flash",
    },
}


# ---------------------------------------------------------------------------
# Key input (cross-platform)
# ---------------------------------------------------------------------------
def _getch():
    if IS_WINDOWS:
        import msvcrt
        ch = msvcrt.getwch()
        if ch == '\r':    return 'ENTER'
        if ch == '\x03':  raise KeyboardInterrupt
        if ch == ' ':     return 'SPACE'
        if ch in ('\x00', '\xe0'):
            ch2 = msvcrt.getwch()
            if ch2 == 'H': return 'UP'
            if ch2 == 'P': return 'DOWN'
        return ch
    else:
        import tty, termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == '\x1b':
                ch2 = sys.stdin.read(1)
                if ch2 == '[':
                    ch3 = sys.stdin.read(1)
                    if ch3 == 'A': return 'UP'
                    if ch3 == 'B': return 'DOWN'
                return 'ESC'
            if ch in ('\r', '\n'): return 'ENTER'
            if ch == '\x03':       raise KeyboardInterrupt
            if ch == ' ':          return 'SPACE'
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _clear():
    os.system('cls' if IS_WINDOWS else 'clear')


# ---------------------------------------------------------------------------
# ASCII art banner
# ---------------------------------------------------------------------------
BANNER = f"""{BLD}{C}
  ╔══════════════════════════════════════════════════════╗
  ║                                                      ║
  ║   ███████╗██╗   ██╗███╗   ███╗ ██████╗ ███╗   ██╗   ║
  ║   ██╔════╝██║   ██║████╗ ████║██╔═══██╗████╗  ██║   ║
  ║   ███████╗██║   ██║██╔████╔██║██║   ██║██╔██╗ ██║   ║
  ║   ╚════██║██║   ██║██║╚██╔╝██║██║   ██║██║╚██╗██║   ║
  ║   ███████║╚██████╔╝██║ ╚═╝ ██║╚██████╔╝██║ ╚████║   ║
  ║   ╚══════╝ ╚═════╝ ╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═══╝   ║
  ║                                                      ║
  ║{NC}{DIM}        Claude · Codex · Gemini Telegram Bot       {NC}{BLD}{C} ║
  ╚══════════════════════════════════════════════════════╝{NC}
"""

def _print_banner():
    _clear()
    print(BANNER)


# ---------------------------------------------------------------------------
# TUI: single-select
# ---------------------------------------------------------------------------
def _single_select(lang, title, options, default=0):
    """Arrow key single-select. Returns selected index."""
    sel = default
    while True:
        _print_banner()
        print(f"  {BLD}{title}{NC}\n")
        for i, opt in enumerate(options):
            if i == sel:
                print(f"  {C}▸ {BLD}{opt}{NC}")
            else:
                print(f"  {DIM}  {opt}{NC}")
        print(f"\n  {DIM}{_t(lang, 'nav_single')}{NC}")
        k = _getch()
        if k == 'UP':    sel = (sel - 1) % len(options)
        elif k == 'DOWN': sel = (sel + 1) % len(options)
        elif k == 'ENTER': return sel


# ---------------------------------------------------------------------------
# TUI: multi-select (checkbox)
# ---------------------------------------------------------------------------
def _multi_select(lang, title, options, defaults=None):
    """Space to toggle, Enter to confirm. Returns list of selected indices."""
    checked = set(defaults or [0])
    sel = 0
    while True:
        _print_banner()
        print(f"  {BLD}{title}{NC}\n")
        for i, opt in enumerate(options):
            box = f"{G}[✓]{NC}" if i in checked else f"{DIM}[ ]{NC}"
            cursor = f"{C}▸ {NC}" if i == sel else "  "
            print(f"  {cursor}{box} {BLD if i in checked else DIM}{opt}{NC}")
        print(f"\n  {DIM}{_t(lang, 'nav')}{NC}")
        k = _getch()
        if k == 'UP':    sel = (sel - 1) % len(options)
        elif k == 'DOWN': sel = (sel + 1) % len(options)
        elif k == 'SPACE':
            if sel in checked:
                checked.discard(sel)
            else:
                checked.add(sel)
        elif k == 'ENTER':
            if not checked:
                print(f"\n  {Y}{_t(lang, 'provider_min')}{NC}")
                time.sleep(1.5)
                continue
            return sorted(checked)


# ---------------------------------------------------------------------------
# Step: Language selection
# ---------------------------------------------------------------------------
def step_language():
    langs = [("한국어 (Korean)", "ko"), ("English", "en")]
    _print_banner()
    idx = _single_select("en", "Select Language / 언어 선택",
                         [l[0] for l in langs])
    return langs[idx][1]


# ---------------------------------------------------------------------------
# Step: AI provider multi-select
# ---------------------------------------------------------------------------
def step_providers(lang):
    keys = list(AI_PROVIDERS.keys())
    labels = [AI_PROVIDERS[k]["label"] for k in keys]
    step(1, 6, _t(lang, "provider_sel"))
    indices = _multi_select(lang, _t(lang, "provider_sel"), labels)
    return [keys[i] for i in indices]


# ---------------------------------------------------------------------------
# Step: Sub-model per provider
# ---------------------------------------------------------------------------
def step_submodels(lang, providers):
    results = {}
    for prov in providers:
        info_p = AI_PROVIDERS[prov]
        sub_keys = list(info_p["sub_models"].keys())
        sub_vals = list(info_p["sub_models"].values())
        labels   = [f"{k}  {DIM}({v}){NC}" for k, v in zip(sub_keys, sub_vals)]
        title    = _t(lang, "submodel_sel", provider=info_p["label"])
        default_idx = sub_keys.index(info_p["default"]) if info_p["default"] in sub_keys else 0
        _print_banner()
        idx = _single_select(lang, title, labels, default=default_idx)
        results[prov] = sub_keys[idx]
    return results  # {provider: sub_model_alias}


# ---------------------------------------------------------------------------
# Step: CLI installation check
# ---------------------------------------------------------------------------
def _cli_installed(cmds):
    for cmd in cmds:
        try:
            kw = {}
            if IS_WINDOWS:
                kw["creationflags"] = subprocess.CREATE_NO_WINDOW
            r = subprocess.run([cmd, "--version"], capture_output=True, timeout=5, **kw)
            if r.returncode == 0:
                return True
        except Exception:
            pass
    return False


def step_cli_check(lang, providers):
    _print_banner()
    step(2, 6, _t(lang, "cli_check"))
    print()

    all_ok = True
    for prov in providers:
        p = AI_PROVIDERS[prov]
        if _cli_installed(p["cli_cmds"]):
            ok(_t(lang, "cli_ok", cli=p["label"]))
        else:
            warn(_t(lang, "cli_missing", cli=p["label"]))
            all_ok = False

    if all_ok:
        ok(_t(lang, "cli_all_ok"))
        time.sleep(1)
        return

    # Show install guides and wait for user to install
    print()
    for prov in providers:
        p = AI_PROVIDERS[prov]
        if not _cli_installed(p["cli_cmds"]):
            print(f"  {Y}{p['label']}{NC}")
            print(f"  {_t(lang, 'cli_install_guide')}  {BLD}{p['install']}{NC}")
            print()

    while True:
        print(f"  {DIM}{_t(lang, 'cli_retry')}{NC}", end="", flush=True)
        input()
        _print_banner()
        step(2, 6, _t(lang, "cli_check"))
        print()
        still_missing = []
        for prov in providers:
            p = AI_PROVIDERS[prov]
            if _cli_installed(p["cli_cmds"]):
                ok(_t(lang, "cli_ok", cli=p["label"]))
            else:
                warn(_t(lang, "cli_missing", cli=p["label"]))
                still_missing.append(prov)
        if not still_missing:
            ok(_t(lang, "cli_all_ok"))
            time.sleep(1)
            return
        print()
        for prov in still_missing:
            p = AI_PROVIDERS[prov]
            print(f"  {Y}{p['label']}{NC}  {BLD}{p['install']}{NC}")
        print()


# ---------------------------------------------------------------------------
# Step: Bot Token
# ---------------------------------------------------------------------------
def step_token(lang):
    _print_banner()
    step(3, 6, _t(lang, "token_step"))
    print(f"\n  {_t(lang, 'token_guide_1')}")
    print(f"  2. Telegram → @BotFather\n")
    import re
    while True:
        try:
            token = input(f"  {C}{_t(lang, 'token_prompt')}: {NC}").strip()
        except EOFError:
            raise KeyboardInterrupt
        if re.match(r'^\d+:[A-Za-z0-9_-]+$', token):
            return token
        err(_t(lang, "token_invalid"))


# ---------------------------------------------------------------------------
# Step: Chat ID auto-detect
# ---------------------------------------------------------------------------
def step_chat_id(lang, token):
    _print_banner()
    step(4, 6, _t(lang, "chat_step"))
    # Flush existing updates
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/getUpdates?offset=-1&limit=1",
            timeout=5)
    except Exception:
        pass

    print(f"\n  {_t(lang, 'chat_guide')}\n")
    deadline = time.time() + 60
    chat_id = None
    while time.time() < deadline:
        try:
            r = urllib.request.urlopen(
                f"https://api.telegram.org/bot{token}/getUpdates?timeout=5&limit=1",
                timeout=10)
            data = json.loads(r.read())
            if data.get("ok") and data.get("result"):
                msg = data["result"][0]
                cid = msg.get("message", {}).get("chat", {}).get("id")
                if cid:
                    uid = msg.get("update_id", 0)
                    try:
                        urllib.request.urlopen(
                            f"https://api.telegram.org/bot{token}/getUpdates?offset={uid+1}&limit=1",
                            timeout=5)
                    except Exception:
                        pass
                    chat_id = str(cid)
                    break
        except Exception:
            pass
        remaining = int(deadline - time.time())
        print(f"\r  {DIM}Waiting... {remaining}s  {NC}", end="", flush=True)
        time.sleep(2)
    print()

    if chat_id:
        ok(f"Chat ID: {chat_id}")
        time.sleep(0.8)
        return chat_id

    warn(_t(lang, "chat_fail"))
    while True:
        try:
            cid = input(f"  {C}{_t(lang, 'chat_prompt')}: {NC}").strip()
        except EOFError:
            raise KeyboardInterrupt
        if cid.lstrip("-").isdigit():
            return cid
        err(_t(lang, "chat_invalid"))


# ---------------------------------------------------------------------------
# Step: Working directory
# ---------------------------------------------------------------------------
def step_workdir(lang):
    _print_banner()
    step(5, 6, _t(lang, "workdir_step"))
    default = os.path.expanduser("~")
    print()
    while True:
        try:
            val = input(f"  {C}{_t(lang, 'workdir_prompt', default=default)}: {NC}").strip()
        except EOFError:
            raise KeyboardInterrupt
        path = val or default
        if os.path.isdir(path):
            return path
        err(_t(lang, "workdir_notfound"))


# ---------------------------------------------------------------------------
# Step: Theme + Snapshot
# ---------------------------------------------------------------------------
def step_prefs(lang):
    # Theme
    theme_opts  = [_t(lang, "system"), _t(lang, "dark"), _t(lang, "light")]
    theme_vals  = ["system", "dark", "light"]
    _print_banner()
    t_idx = _single_select(lang, f"{_t(lang, 'theme_step')}  —  {_t(lang, 'theme_desc')}",
                            theme_opts)
    # Snapshot
    days_lbl   = _t(lang, "days")
    snap_opts  = [f"3 {days_lbl}", f"7 {days_lbl}", f"14 {days_lbl}", f"30 {days_lbl}"]
    snap_vals  = [3, 7, 14, 30]
    _print_banner()
    s_idx = _single_select(lang, f"{_t(lang, 'snapshot_step')}  —  {_t(lang, 'snapshot_desc')}",
                            snap_opts, default=1)
    return theme_vals[t_idx], snap_vals[s_idx]


# ---------------------------------------------------------------------------
# Write config.json
# ---------------------------------------------------------------------------
def write_config(lang, token, chat_id, work_dir,
                 providers, submodels, theme, snapshot_days):
    # Default provider = first selected
    default_prov = providers[0]
    default_sub  = submodels.get(default_prov, AI_PROVIDERS[default_prov]["default"])

    cfg = {
        "bot_token":   token,
        "chat_id":     chat_id,
        "work_dir":    work_dir,
        "lang":        lang,
        "github_repo": GITHUB_REPO,
        "settings": {
            "show_cost":               False,
            "show_status":             True,
            "show_global_cost":        True,
            "token_display":           "month",
            "show_remote_tokens":      True,
            "theme":                   theme,
            "snapshot_ttl_days":       snapshot_days,
            "token_ttl":               "session",
            "default_model":           default_prov,
            "default_sub_model":       default_sub,
            "auto_viewer_link":        True,
            "viewer_link_fixed":       False,
            "show_typing":             True,
            "settings_timeout_minutes": 15,
        },
    }

    os.makedirs(SCRIPT_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)

    if not IS_WINDOWS:
        os.chmod(CONFIG_FILE, 0o600)


# ---------------------------------------------------------------------------
# Done screen
# ---------------------------------------------------------------------------
def show_done(lang, providers, submodels, work_dir):
    _print_banner()
    step(6, 6, _t(lang, "done_title"))
    print(f"\n  {_t(lang, 'done_desc')}\n")
    print(f"  {'─'*48}")
    for prov in providers:
        p    = AI_PROVIDERS[prov]
        sub  = submodels.get(prov, p["default"])
        mid  = p["sub_models"].get(sub, sub)
        print(f"  {G}✓{NC} {BLD}{p['label']}{NC}  {DIM}{mid}{NC}")
    print(f"  {'─'*48}")
    print(f"  {DIM}Work dir: {work_dir}{NC}")
    print(f"  {DIM}Config:   {CONFIG_FILE}{NC}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    try:
        lang = step_language()

        providers = step_providers(lang)
        submodels = step_submodels(lang, providers)
        step_cli_check(lang, providers)

        token   = step_token(lang)
        chat_id = step_chat_id(lang, token)
        workdir = step_workdir(lang)
        theme, snapshot_days = step_prefs(lang)

        write_config(lang, token, chat_id, workdir,
                     providers, submodels, theme, snapshot_days)

        show_done(lang, providers, submodels, workdir)

    except KeyboardInterrupt:
        print(f"\n\n  {Y}{_t(lang if 'lang' in dir() else 'en', 'cancel')}{NC}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
