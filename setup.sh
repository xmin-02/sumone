#!/usr/bin/env bash
# ============================================================================
# Claude Code Telegram Bot - Setup Script
# Downloads bot from GitHub, configures, and sets up auto-start.
# Supports: Linux, macOS, WSL
# ============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }

GITHUB_REPO="xmin-02/Claude-telegram-bot"
GITHUB_RAW="https://raw.githubusercontent.com/${GITHUB_REPO}/main"
INSTALL_DIR="$HOME/.claude-telegram-bot"
BOT_PATH="$INSTALL_DIR/main.py"
CONFIG_PATH="$INSTALL_DIR/config.json"

# --- OS Detection ---
detect_os() {
    case "$(uname -s)" in
        Linux*)
            if grep -qi microsoft /proc/version 2>/dev/null; then echo "wsl"
            else echo "linux"; fi ;;
        Darwin*) echo "macos" ;;
        *) echo "unknown" ;;
    esac
}

# --- Prerequisites ---
check_prerequisites() {
    info "Checking prerequisites... / 필수 프로그램 확인 중..."
    if command -v python3 &>/dev/null; then PYTHON="python3"
    elif command -v python &>/dev/null && python --version 2>&1 | grep -q "Python 3"; then PYTHON="python"
    else
        err "Python 3 is not installed. / Python 3이 설치되어 있지 않습니다."
        exit 1
    fi
    ok "Python: $($PYTHON --version)"

    if command -v claude &>/dev/null; then
        ok "Claude CLI: installed"
    else
        warn "Claude CLI not found. Install: npm install -g @anthropic-ai/claude-code"
        read -rp "$(echo -e "${YELLOW}Continue without Claude CLI? / Claude CLI 없이 계속? (y/N): ${NC}")" yn
        [[ "$yn" =~ ^[yY] ]] || exit 0
    fi
}

# --- Language Selection ---
select_language() {
    echo ""
    echo -e "${BOLD}Select Language / 언어 선택${NC}"
    echo "  1) 한국어 (Korean)"
    echo "  2) English"
    echo ""
    while true; do
        read -rp "$(echo -e "${CYAN}Choice / 선택 (1-2): ${NC}")" choice
        case "$choice" in
            1) LANG="ko"; break ;;
            2) LANG="en"; break ;;
            *) echo "1 or 2" ;;
        esac
    done
}

# --- User Input ---
get_user_input() {
    echo ""
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    if [[ "$LANG" == "ko" ]]; then
        echo -e "${BOLD} Telegram Bot 설정${NC}"
    else
        echo -e "${BOLD} Telegram Bot Setup${NC}"
    fi
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    if [[ "$LANG" == "ko" ]]; then
        echo "1. @BotFather → /newbot → 토큰 복사"
        echo "2. 토큰 입력 후 봇에게 메시지를 보내면 Chat ID 자동 감지"
    else
        echo "1. @BotFather → /newbot → Copy token"
        echo "2. After entering token, send a message to your bot for auto Chat ID detection"
    fi
    echo ""

    while true; do
        read -rp "$(echo -e "${CYAN}Bot Token: ${NC}")" BOT_TOKEN
        [[ "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] && break
        err "Invalid token format"
    done

    # --- Auto-detect Chat ID via getUpdates polling ---
    # Flush existing messages
    $PYTHON -c "
import urllib.request
try: urllib.request.urlopen('https://api.telegram.org/bot${BOT_TOKEN}/getUpdates?offset=-1&limit=1', timeout=5)
except Exception: pass
" 2>/dev/null

    echo ""
    if [[ "$LANG" == "ko" ]]; then
        info "Telegram에서 봇에게 아무 메시지를 보내주세요... (60초 대기)"
    else
        info "Send any message to your bot in Telegram... (waiting 60s)"
    fi

    CHAT_ID=$($PYTHON -c "
import urllib.request, json, time
token = '${BOT_TOKEN}'
deadline = time.time() + 60
while time.time() < deadline:
    try:
        r = urllib.request.urlopen(f'https://api.telegram.org/bot{token}/getUpdates?timeout=5&limit=1', timeout=10)
        data = json.loads(r.read())
        if data.get('ok') and data.get('result'):
            msg = data['result'][0]
            chat_id = msg.get('message', {}).get('chat', {}).get('id')
            if chat_id:
                # Acknowledge the update so it won't appear again
                update_id = msg.get('update_id', 0)
                urllib.request.urlopen(f'https://api.telegram.org/bot{token}/getUpdates?offset={update_id+1}&limit=1', timeout=5)
                print(chat_id)
                break
    except Exception:
        pass
    time.sleep(2)
" 2>/dev/null)

    if [[ -n "$CHAT_ID" ]]; then
        ok "Chat ID detected: $CHAT_ID"
    else
        if [[ "$LANG" == "ko" ]]; then
            warn "자동 감지 실패. 수동으로 입력해주세요."
        else
            warn "Auto-detection failed. Please enter manually."
        fi
        while true; do
            read -rp "$(echo -e "${CYAN}Chat ID: ${NC}")" CHAT_ID
            [[ "$CHAT_ID" =~ ^-?[0-9]+$ ]] && break
            err "Invalid Chat ID"
        done
    fi

    DEFAULT_WORKDIR="$HOME"
    read -rp "$(echo -e "${CYAN}Working directory [$DEFAULT_WORKDIR]: ${NC}")" WORK_DIR
    WORK_DIR="${WORK_DIR:-$DEFAULT_WORKDIR}"
    [[ -d "$WORK_DIR" ]] || { err "Directory not found: $WORK_DIR"; exit 1; }
}

# --- Download & Install ---
_dl() {
    # _dl <url> <dest> — download a single file
    local url="$1" dest="$2"
    if command -v curl &>/dev/null; then
        curl -fsSL "$url" -o "$dest" || { err "Download failed: $url"; exit 1; }
    elif command -v wget &>/dev/null; then
        wget -q "$url" -O "$dest" || { err "Download failed: $url"; exit 1; }
    else
        $PYTHON -c "import urllib.request; urllib.request.urlretrieve('$url', '$dest')" || { err "Download failed: $url"; exit 1; }
    fi
}

install_bot() {
    mkdir -p "$INSTALL_DIR/i18n" "$INSTALL_DIR/commands"
    info "Downloading bot from GitHub..."

    # Core modules
    local files=(
        "bot/main.py:main.py"
        "bot/config.py:config.py"
        "bot/state.py:state.py"
        "bot/telegram.py:telegram.py"
        "bot/claude.py:claude.py"
        "bot/tokens.py:tokens.py"
        "bot/sessions.py:sessions.py"
        "bot/downloader.py:downloader.py"
        "bot/i18n/__init__.py:i18n/__init__.py"
        "bot/i18n/ko.json:i18n/ko.json"
        "bot/i18n/en.json:i18n/en.json"
        "bot/commands/__init__.py:commands/__init__.py"
        "bot/commands/basic.py:commands/basic.py"
        "bot/commands/filesystem.py:commands/filesystem.py"
        "bot/commands/settings.py:commands/settings.py"
        "bot/commands/update.py:commands/update.py"
        "bot/commands/total_tokens.py:commands/total_tokens.py"
        "bot/commands/skills.py:commands/skills.py"
        "bot/commands/session_cmd.py:commands/session_cmd.py"
    )

    for entry in "${files[@]}"; do
        local src="${entry%%:*}" dest="${entry##*:}"
        _dl "${GITHUB_RAW}/${src}" "$INSTALL_DIR/${dest}"
    done
    ok "Bot downloaded: $INSTALL_DIR (${#files[@]} files)"

    # Create config.json
    cat > "$CONFIG_PATH" << EOF
{
    "bot_token": "$BOT_TOKEN",
    "chat_id": "$CHAT_ID",
    "work_dir": "$WORK_DIR",
    "lang": "$LANG",
    "github_repo": "$GITHUB_REPO"
}
EOF
    chmod 600 "$CONFIG_PATH"
    ok "Config saved: $CONFIG_PATH"
}

# --- Verify Token ---
verify_token() {
    info "Verifying bot token..."
    if $PYTHON -c "
import urllib.request, json
r = urllib.request.urlopen('https://api.telegram.org/bot${BOT_TOKEN}/getMe', timeout=10)
d = json.loads(r.read())
if d.get('ok'): print('Bot: @' + d['result'].get('username', ''))
else: exit(1)
" 2>/dev/null; then
        ok "Token verified"
    else
        warn "Token verification failed"
    fi
}

# --- Set Bot Profile Photo ---
set_bot_photo() {
    info "Setting bot profile photo..."
    local photo_url="${GITHUB_RAW}/assets/logo.png"
    local photo_path="$INSTALL_DIR/logo.png"

    # Download logo
    if command -v curl &>/dev/null; then
        curl -fsSL "$photo_url" -o "$photo_path" 2>/dev/null
    elif command -v wget &>/dev/null; then
        wget -q "$photo_url" -O "$photo_path" 2>/dev/null
    else
        $PYTHON -c "import urllib.request; urllib.request.urlretrieve('$photo_url', '$photo_path')" 2>/dev/null
    fi

    if [[ ! -f "$photo_path" ]]; then
        warn "Logo download failed, skipping profile photo"
        return
    fi

    # Upload via setMyProfilePhoto API
    $PYTHON -c "
import urllib.request, json, uuid
token = '${BOT_TOKEN}'
boundary = uuid.uuid4().hex
with open('${photo_path}', 'rb') as f:
    photo_data = f.read()
photo_json = json.dumps({'type': 'static', 'photo': 'attach://photo_file'})
parts = []
parts.append(('--' + boundary + '\r\nContent-Disposition: form-data; name=\"photo\"\r\n\r\n' + photo_json + '\r\n').encode())
parts.append(('--' + boundary + '\r\nContent-Disposition: form-data; name=\"photo_file\"; filename=\"logo.png\"\r\nContent-Type: image/png\r\n\r\n').encode() + photo_data + b'\r\n')
parts.append(('--' + boundary + '--\r\n').encode())
body = b''.join(parts)
req = urllib.request.Request('https://api.telegram.org/bot' + token + '/setMyProfilePhoto', data=body)
req.add_header('Content-Type', 'multipart/form-data; boundary=' + boundary)
try:
    resp = urllib.request.urlopen(req, timeout=30)
    data = json.loads(resp.read())
    if data.get('ok'): print('ok')
except Exception: pass
" 2>/dev/null && ok "Profile photo set" || warn "Profile photo upload failed (non-critical)"

    rm -f "$photo_path"
}

# --- Grant read access to other users' Claude sessions ---
setup_token_access() {
    local bot_user
    bot_user="$(whoami)"
    local found=()

    # Scan /home/* and /root for .claude/projects
    for home_dir in /home/* /root; do
        [[ -d "$home_dir/.claude/projects" ]] || continue
        # Skip own directory
        [[ "$home_dir" == "$HOME" ]] && continue
        # Already readable?
        if ls "$home_dir/.claude/projects" &>/dev/null 2>&1; then
            continue
        fi
        found+=("$home_dir")
    done

    [[ ${#found[@]} -eq 0 ]] && return

    echo ""
    info "Found Claude sessions from other users:"
    for d in "${found[@]}"; do
        echo "  $d/.claude/projects/"
    done
    echo ""

    if [[ "$LANG" == "ko" ]]; then
        read -rp "$(echo -e "${CYAN}다른 사용자의 토큰도 집계할까요? (Y/n): ${NC}")" yn
    else
        read -rp "$(echo -e "${CYAN}Include other users' tokens in aggregate? (Y/n): ${NC}")" yn
    fi
    [[ "$yn" =~ ^[nN] ]] && return

    for home_dir in "${found[@]}"; do
        info "Granting read access: $home_dir/.claude/projects/"
        sudo setfacl -m "u:${bot_user}:rX" "$home_dir" 2>/dev/null
        sudo setfacl -m "u:${bot_user}:rX" "$home_dir/.claude" 2>/dev/null
        sudo setfacl -R -m "u:${bot_user}:rX" "$home_dir/.claude/projects/" 2>/dev/null
        sudo setfacl -R -d -m "u:${bot_user}:rX" "$home_dir/.claude/projects/" 2>/dev/null
        if ls "$home_dir/.claude/projects" &>/dev/null 2>&1; then
            ok "Access granted: $home_dir"
        else
            warn "Failed: $home_dir (setfacl may not be supported)"
        fi
    done
}

# --- Auto-start ---
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
    echo "  Status:  systemctl --user status claude-telegram"
    echo "  Logs:    cat $INSTALL_DIR/bot.log"
    echo "  Stop:    systemctl --user stop claude-telegram"
    echo "  Restart: systemctl --user restart claude-telegram"
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
    <key>Label</key><string>com.claude.telegram-bot</string>
    <key>ProgramArguments</key>
    <array><string>$PYTHON</string><string>$BOT_PATH</string></array>
    <key>WorkingDirectory</key><string>$WORK_DIR</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$INSTALL_DIR/bot-stdout.log</string>
    <key>StandardErrorPath</key><string>$INSTALL_DIR/bot-stderr.log</string>
</dict>
</plist>
EOF
    launchctl load "$plist" 2>/dev/null || true
    launchctl start com.claude.telegram-bot 2>/dev/null || true
    ok "launchd service registered"
}

setup_autostart_wsl() {
    info "Setting up WSL auto-start..."
    local marker="# claude-telegram-bot autostart"
    local start_cmd="(pgrep -f 'telegram-bot.py' > /dev/null 2>&1 || nohup $PYTHON $BOT_PATH > /dev/null 2>&1 &)"
    if ! grep -q "$marker" "$HOME/.bashrc" 2>/dev/null; then
        echo -e "\n$marker\n$start_cmd" >> "$HOME/.bashrc"
    fi
    eval "$start_cmd"
    ok "WSL auto-start configured (.bashrc)"
}

# --- Uninstall info ---
print_uninstall() {
    echo ""
    echo -e "${BOLD} Uninstall / 제거 방법${NC}"
    case "$OS" in
        linux)
            echo "  systemctl --user stop claude-telegram && systemctl --user disable claude-telegram"
            echo "  rm ~/.config/systemd/user/claude-telegram.service" ;;
        macos)
            echo "  launchctl stop com.claude.telegram-bot"
            echo "  launchctl unload ~/Library/LaunchAgents/com.claude.telegram-bot.plist"
            echo "  rm ~/Library/LaunchAgents/com.claude.telegram-bot.plist" ;;
        wsl)
            echo "  pkill -f telegram-bot.py"
            echo "  Remove '# claude-telegram-bot autostart' from ~/.bashrc" ;;
    esac
    echo "  rm -rf $INSTALL_DIR"
    echo ""
}

# --- Main ---
main() {
    echo ""
    echo -e "${BOLD}╔═══════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}║  Claude Code Telegram Bot - Setup         ║${NC}"
    echo -e "${BOLD}╚═══════════════════════════════════════════╝${NC}"
    echo ""

    OS=$(detect_os)
    info "OS: $OS"
    [[ "$OS" == "unknown" ]] && { err "Unsupported OS. Use WSL on Windows."; exit 1; }

    check_prerequisites
    select_language
    get_user_input
    install_bot
    verify_token
    set_bot_photo
    setup_token_access

    case "$OS" in
        linux) setup_autostart_linux ;;
        macos) setup_autostart_macos ;;
        wsl)   setup_autostart_wsl ;;
    esac

    print_uninstall
    echo -e "${GREEN}${BOLD}Setup complete! / 설치 완료!${NC}"
    echo "Send /help in Telegram to get started."
    echo ""
}

main "$@"
