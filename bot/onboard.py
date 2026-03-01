#!/usr/bin/env python3
"""Sumone CLI Onboarding TUI.

Interactive setup wizard run after initial installation.
Uses arrow keys for selection (curses on Unix, msvcrt on Windows).

Items:
  1. Theme (system / dark / light)
  2. Snapshot retention (3 / 7 / 14 / 30 days)
  3. AI Model (Claude — future: GPT, Gemini, etc.)
  4. AI Sub-Model (depends on #3, e.g. Haiku / Sonnet / Opus)
"""
import json
import os
import platform
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
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
        "ai_model": "AI 모델",
        "ai_model_desc": "사용할 AI 제공자",
        "sub_model": "세부 모델",
        "sub_model_desc": "기본 모델 (새 세션에 적용)",
        "done": "설정 완료!",
        "done_desc": "봇을 실행하면 설정이 적용됩니다.",
        "days": "일",
        "system": "시스템 (OS 설정 따름)",
        "dark": "다크",
        "light": "라이트",
    },
    "en": {
        "welcome": "Sumone Initial Setup",
        "welcome_desc": "Use arrow keys (↑↓) to select, Enter to confirm",
        "step": "Step",
        "theme": "Theme",
        "theme_desc": "File viewer color theme",
        "snapshot": "Snapshot Retention",
        "snapshot_desc": "Days to keep file modification history",
        "ai_model": "AI Model",
        "ai_model_desc": "AI provider to use",
        "sub_model": "Sub-Model",
        "sub_model_desc": "Default model (applied to new sessions)",
        "done": "Setup Complete!",
        "done_desc": "Settings will be applied when the bot starts.",
        "days": "days",
        "system": "System (follow OS)",
        "dark": "Dark",
        "light": "Light",
    },
}

AI_MODELS = {
    "claude": {
        "label": "Claude",
        "sub_models": ["haiku", "sonnet", "opus"],
        "sub_model_ids": {
            "haiku": "claude-haiku-4-5-20251001",
            "sonnet": "claude-sonnet-4-6",
            "opus": "claude-opus-4-6",
        },
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


def _render_menu(lang, step_num, total_steps, title, desc, options, selected):
    _clear_screen()
    print(f"\n  ╔{'═' * 50}╗")
    print(f"  ║  {'Sumone':^46}  ║")
    print(f"  ╚{'═' * 50}╝\n")
    print(f"  {_t(lang, 'step')} {step_num}/{total_steps}: {title}")
    print(f"  {desc}\n")

    for i, opt in enumerate(options):
        if i == selected:
            print(f"  ▸ \033[1;36m{opt}\033[0m")
        else:
            print(f"    {opt}")

    print(f"\n  ↑↓ {_t(lang, 'welcome_desc').split(',')[0].strip()}")
    print(f"  Enter {_t(lang, 'welcome_desc').split(',')[1].strip()}")


def _select(lang, step_num, total_steps, title, desc, options):
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


# ---------------------------------------------------------------------------
# Main onboarding flow
# ---------------------------------------------------------------------------
def run_onboarding(lang=None):
    """Run the interactive onboarding wizard. Returns the selected settings dict."""
    if not lang:
        lang = "ko"

    total_steps = 4
    results = {}

    # Step 1: Theme
    theme_options = [
        _t(lang, "system"),
        _t(lang, "dark"),
        _t(lang, "light"),
    ]
    theme_values = ["system", "dark", "light"]
    idx = _select(lang, 1, total_steps, _t(lang, "theme"), _t(lang, "theme_desc"), theme_options)
    results["theme"] = theme_values[idx]

    # Step 2: Snapshot retention
    days_label = _t(lang, "days")
    snap_options = [f"3 {days_label}", f"7 {days_label}", f"14 {days_label}", f"30 {days_label}"]
    snap_values = [3, 7, 14, 30]
    idx = _select(lang, 2, total_steps, _t(lang, "snapshot"), _t(lang, "snapshot_desc"), snap_options)
    results["snapshot_ttl_days"] = snap_values[idx]

    # Step 3: AI Model
    model_names = list(AI_MODELS.keys())
    model_labels = [AI_MODELS[k]["label"] for k in model_names]
    idx = _select(lang, 3, total_steps, _t(lang, "ai_model"), _t(lang, "ai_model_desc"), model_labels)
    selected_model = model_names[idx]
    results["default_model"] = selected_model

    # Step 4: Sub-Model (dynamic based on Step 3)
    sub_models = AI_MODELS[selected_model]["sub_models"]
    idx = _select(lang, 4, total_steps, _t(lang, "sub_model"), _t(lang, "sub_model_desc"), sub_models)
    results["default_sub_model"] = sub_models[idx]

    # Done
    _clear_screen()
    print(f"\n  ╔{'═' * 50}╗")
    print(f"  ║  {'✓ ' + _t(lang, 'done'):^46}  ║")
    print(f"  ╚{'═' * 50}╝\n")
    print(f"  {_t(lang, 'done_desc')}\n")
    for k, v in results.items():
        print(f"    {k}: {v}")
    print()

    return results


def apply_onboarding(results):
    """Apply onboarding results to config.json."""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cfg = {}

    settings = cfg.get("settings", {})
    settings.update(results)
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
