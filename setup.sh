#!/usr/bin/env bash
# ============================================================================
# sumone - Claude · Codex · Gemini Telegram Bot
# Setup Script (Linux / macOS / WSL)
# ============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

info()  { echo -e "  ${CYAN}[INFO]${NC} $*"; }
ok()    { echo -e "  ${GREEN}[ OK ]${NC} $*"; }
warn()  { echo -e "  ${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "  ${RED}[ERR ]${NC} $*"; }

GITHUB_REPO="xmin-02/sumone"
GITHUB_RAW="https://raw.githubusercontent.com/${GITHUB_REPO}/main"
INSTALL_DIR="$HOME/.claude-telegram-bot"
BOT_PATH="$INSTALL_DIR/main.py"

# ── Banner ──────────────────────────────────────────────────────────────────
print_banner() {
    clear
    echo -e "${BOLD}${CYAN}"
    echo '  ╔══════════════════════════════════════════════════════╗'
    echo '  ║                                                      ║'
    echo '  ║   ███████╗██╗   ██╗███╗   ███╗ ██████╗ ███╗   ██╗   ║'
    echo '  ║   ██╔════╝██║   ██║████╗ ████║██╔═══██╗████╗  ██║   ║'
    echo '  ║   ███████╗██║   ██║██╔████╔██║██║   ██║██╔██╗ ██║   ║'
    echo '  ║   ╚════██║██║   ██║██║╚██╔╝██║██║   ██║██║╚██╗██║   ║'
    echo '  ║   ███████║╚██████╔╝██║ ╚═╝ ██║╚██████╔╝██║ ╚████║   ║'
    echo '  ║   ╚══════╝ ╚═════╝ ╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═══╝   ║'
    echo '  ║                                                      ║'
    echo -e "  ║${NC}${DIM}        Claude · Codex · Gemini Telegram Bot       ${NC}${BOLD}${CYAN}║"
    echo '  ╚══════════════════════════════════════════════════════╝'
    echo -e "${NC}"
}

# ── OS Detection ────────────────────────────────────────────────────────────
detect_os() {
    case "$(uname -s)" in
        Linux*)
            grep -qi microsoft /proc/version 2>/dev/null && echo "wsl" || echo "linux" ;;
        Darwin*) echo "macos" ;;
        *)       echo "unknown" ;;
    esac
}

# ── Prerequisites ───────────────────────────────────────────────────────────
check_python() {
    print_banner
    echo -e "  ${BOLD}[1/4] System Check${NC}\n"
    if command -v python3 &>/dev/null; then PYTHON="python3"
    elif command -v python &>/dev/null && python --version 2>&1 | grep -q "Python 3"; then PYTHON="python"
    else
        err "Python 3 not found. Install from https://python.org"
        exit 1
    fi
    ok "Python: $($PYTHON --version)"
}

# ── Download ─────────────────────────────────────────────────────────────────
_dl() {
    local url="$1" dest="$2"
    if command -v curl &>/dev/null; then
        curl -fsSL "$url" -o "$dest" || { err "Download failed: $url"; exit 1; }
    elif command -v wget &>/dev/null; then
        wget -q "$url" -O "$dest" || { err "Download failed: $url"; exit 1; }
    else
        $PYTHON -c "import urllib.request; urllib.request.urlretrieve('$url', '$dest')" \
            || { err "Download failed: $url"; exit 1; }
    fi
}

download_bot() {
    print_banner
    echo -e "  ${BOLD}[2/4] Downloading bot files...${NC}\n"

    mkdir -p "$INSTALL_DIR/i18n" "$INSTALL_DIR/commands" "$INSTALL_DIR/ai"

    local files=(
        "bot/main.py:main.py"
        "bot/config.py:config.py"
        "bot/state.py:state.py"
        "bot/telegram.py:telegram.py"
        "bot/tokens.py:tokens.py"
        "bot/sessions.py:sessions.py"
        "bot/downloader.py:downloader.py"
        "bot/fileviewer.py:fileviewer.py"
        "bot/onboard.py:onboard.py"
        "bot/ai/__init__.py:ai/__init__.py"
        "bot/ai/claude.py:ai/claude.py"
        "bot/ai/codex.py:ai/codex.py"
        "bot/ai/gemini.py:ai/gemini.py"
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

    local total=${#files[@]} i=0
    for entry in "${files[@]}"; do
        local src="${entry%%:*}" dest="${entry##*:}"
        _dl "${GITHUB_RAW}/${src}" "$INSTALL_DIR/${dest}"
        (( i++ ))
        printf "\r  ${CYAN}[%d/%d]${NC} %s" "$i" "$total" "${dest}"
    done
    echo ""
    ok "Downloaded ${total} files → ${INSTALL_DIR}"
}

# ── cloudflared ──────────────────────────────────────────────────────────────
install_cloudflared() {
    if command -v cloudflared &>/dev/null || [ -f "$INSTALL_DIR/cloudflared" ]; then
        ok "cloudflared: already installed"
        return
    fi
    info "Installing cloudflared (file viewer)..."
    local arch cf_url
    case "$(uname -m)" in
        x86_64)        arch="amd64" ;;
        aarch64|arm64) arch="arm64" ;;
        armv7l)        arch="arm" ;;
        *)             arch="amd64" ;;
    esac
    if [[ "$(uname)" == "Darwin" ]]; then
        cf_url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-${arch}.tgz"
        if curl -sL "$cf_url" -o "/tmp/cloudflared.tgz" 2>/dev/null; then
            tar -xzf /tmp/cloudflared.tgz -C "$INSTALL_DIR" cloudflared 2>/dev/null || true
            chmod +x "$INSTALL_DIR/cloudflared" 2>/dev/null || true
            rm -f /tmp/cloudflared.tgz
            ok "cloudflared installed"
        else
            warn "cloudflared install failed (will retry on first run)"
        fi
    else
        cf_url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${arch}"
        if curl -sL "$cf_url" -o "$INSTALL_DIR/cloudflared" 2>/dev/null; then
            chmod +x "$INSTALL_DIR/cloudflared"
            ok "cloudflared installed"
        else
            warn "cloudflared install failed (will retry on first run)"
        fi
    fi
}

# ── Onboarding ───────────────────────────────────────────────────────────────
run_onboarding() {
    $PYTHON "$INSTALL_DIR/onboard.py" || {
        warn "Onboarding exited early — run '$PYTHON $INSTALL_DIR/onboard.py' to reconfigure."
        exit 1
    }
}

# ── sumone command ────────────────────────────────────────────────────────────
register_command() {
    local bin_dir="$HOME/.local/bin"
    mkdir -p "$bin_dir"
    cat > "$bin_dir/sumone" << CMDEOF
#!/usr/bin/env bash
$PYTHON "$BOT_PATH" "\$@"
CMDEOF
    chmod +x "$bin_dir/sumone"
    local rc=""
    [[ -f "$HOME/.zshrc" ]] && rc="$HOME/.zshrc" || rc="$HOME/.bashrc"
    if [[ -n "$rc" ]] && ! grep -q '\.local/bin' "$rc" 2>/dev/null; then
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$rc"
    fi
    ok "'sumone' command registered"
}

# ── Auto-start ────────────────────────────────────────────────────────────────
setup_autostart() {
    print_banner
    echo -e "  ${BOLD}[4/4] Auto-start setup${NC}\n"
    case "$OS" in
        linux)
            local svc="$HOME/.config/systemd/user"
            mkdir -p "$svc"
            cat > "$svc/claude-telegram.service" << EOF
[Unit]
Description=sumone Telegram Bot

[Service]
ExecStart=$PYTHON $BOT_PATH
Restart=always
RestartSec=5
Environment=HOME=$HOME

[Install]
WantedBy=default.target
EOF
            systemctl --user daemon-reload
            systemctl --user enable claude-telegram.service
            systemctl --user start claude-telegram.service
            ok "systemd service registered (auto-start enabled)"
            echo -e "  ${DIM}Status:  systemctl --user status claude-telegram"
            echo -e "  Logs:    tail -f $INSTALL_DIR/bot.log${NC}"
            ;;
        macos)
            local plist="$HOME/Library/LaunchAgents/com.sumone.telegram-bot.plist"
            mkdir -p "$(dirname "$plist")"
            cat > "$plist" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>Label</key><string>com.sumone.telegram-bot</string>
    <key>ProgramArguments</key>
    <array><string>$PYTHON</string><string>$BOT_PATH</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$INSTALL_DIR/bot-stdout.log</string>
    <key>StandardErrorPath</key><string>$INSTALL_DIR/bot-stderr.log</string>
</dict></plist>
EOF
            launchctl load "$plist" 2>/dev/null || true
            launchctl start com.sumone.telegram-bot 2>/dev/null || true
            ok "launchd service registered"
            echo -e "  ${DIM}Logs: tail -f $INSTALL_DIR/bot.log${NC}"
            ;;
        wsl)
            local marker="# sumone-bot autostart"
            local cmd="(pgrep -f 'main.py' > /dev/null 2>&1 || nohup $PYTHON $BOT_PATH > /dev/null 2>&1 &)"
            grep -q "$marker" "$HOME/.bashrc" 2>/dev/null || echo -e "\n$marker\n$cmd" >> "$HOME/.bashrc"
            eval "$cmd"
            ok "WSL auto-start configured (.bashrc)"
            ;;
    esac

    echo ""
    echo -e "  ${DIM}Uninstall:"
    case "$OS" in
        linux) echo "    systemctl --user stop claude-telegram && systemctl --user disable claude-telegram" ;;
        macos) echo "    launchctl unload ~/Library/LaunchAgents/com.sumone.telegram-bot.plist" ;;
        wsl)   echo "    pkill -f main.py  # remove autostart line from ~/.bashrc" ;;
    esac
    echo -e "    rm -rf $INSTALL_DIR${NC}"
}

# ── Grant token access (Linux multi-user) ────────────────────────────────────
setup_token_access() {
    local found=()
    for home_dir in /home/* /root; do
        [[ -d "$home_dir/.claude/projects" ]] || continue
        [[ "$home_dir" == "$HOME" ]] && continue
        ls "$home_dir/.claude/projects" &>/dev/null 2>&1 && continue
        found+=("$home_dir")
    done
    [[ ${#found[@]} -eq 0 ]] && return
    echo ""
    info "Found Claude sessions from other users:"
    for d in "${found[@]}"; do echo "  $d/.claude/projects/"; done
    read -rp "  Include in token aggregate? (Y/n): " yn
    [[ "$yn" =~ ^[nN] ]] && return
    for home_dir in "${found[@]}"; do
        sudo setfacl -m "u:$(whoami):rX" "$home_dir" 2>/dev/null
        sudo setfacl -m "u:$(whoami):rX" "$home_dir/.claude" 2>/dev/null
        sudo setfacl -R -m "u:$(whoami):rX" "$home_dir/.claude/projects/" 2>/dev/null
        ok "Access granted: $home_dir"
    done
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
    OS=$(detect_os)
    [[ "$OS" == "unknown" ]] && { err "Unsupported OS. Use WSL on Windows."; exit 1; }

    check_python         # [1/4]
    download_bot         # [2/4]
    install_cloudflared

    run_onboarding       # [3/4] — interactive: AI, token, chat_id, workdir, prefs

    register_command
    [[ "$OS" == "linux" ]] && setup_token_access
    setup_autostart      # [4/4]

    print_banner
    echo -e "  ${GREEN}${BOLD}Setup complete!${NC}"
    echo -e "  Send ${CYAN}/help${NC} in Telegram to get started.\n"
}

main "$@"
