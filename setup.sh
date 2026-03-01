#!/usr/bin/env bash
# ============================================================================
# sumone - Omni AI Orchestration
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
INSTALL_DIR="$HOME/.sumone"
BOT_PATH="$INSTALL_DIR/bot/main.py"

# ── Banner ──────────────────────────────────────────────────────────────────
print_banner() {
    clear
    echo -e "${BOLD}${CYAN}"
    echo '  ╔══════════════════════════════════════════════════════╗'
    echo '  ║                                                      ║'
    echo '  ║   ███████╗██╗   ██╗███╗   ███╗ ██████╗ ███╗   ██╗    ║'
    echo '  ║   ██╔════╝██║   ██║████╗ ████║██╔═══██╗████╗  ██║    ║'
    echo '  ║   ███████╗██║   ██║██╔████╔██║██║   ██║██╔██╗ ██║    ║'
    echo '  ║   ╚════██║██║   ██║██║╚██╔╝██║██║   ██║██║╚██╗██║    ║'
    echo '  ║   ███████║╚██████╔╝██║ ╚═╝ ██║╚██████╔╝██║ ╚████║    ║'
    echo '  ║   ╚══════╝ ╚═════╝ ╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═══╝    ║'
    echo '  ║                                                      ║'
    echo -e "  ║${NC}${DIM}        Omni AI Orchestration       ${NC}${BOLD}${CYAN}   ║"
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
check_deps() {
    print_banner
    echo -e "  ${BOLD}[1/4] System Check${NC}\n"

    # --- Python ---
    if command -v python3 &>/dev/null; then PYTHON="python3"
    elif command -v python &>/dev/null && python --version 2>&1 | grep -q "Python 3"; then PYTHON="python"
    else
        info "Python 3 not found. Installing..."
        case "$OS" in
            macos)  brew install python3 2>/dev/null || { err "Install Python 3: https://python.org"; exit 1; } ;;
            linux)  sudo apt-get update -qq && sudo apt-get install -y -qq python3 2>/dev/null \
                    || sudo dnf install -y python3 2>/dev/null \
                    || sudo pacman -S --noconfirm python 2>/dev/null \
                    || { err "Install Python 3: https://python.org"; exit 1; } ;;
            wsl)    sudo apt-get update -qq && sudo apt-get install -y -qq python3 2>/dev/null \
                    || { err "Install Python 3: https://python.org"; exit 1; } ;;
        esac
        PYTHON="python3"
    fi
    ok "Python: $($PYTHON --version)"

    # --- Node.js / npm (required for AI CLI installs) ---
    if command -v node &>/dev/null && command -v npm &>/dev/null; then
        ok "Node.js: $(node --version)"
    else
        info "Node.js not found. Installing..."
        case "$OS" in
            macos)  brew install node 2>/dev/null || true ;;
            linux)  # Try NodeSource LTS, fall back to distro package
                    if command -v curl &>/dev/null; then
                        curl -fsSL https://deb.nodesource.com/setup_lts.x 2>/dev/null | sudo -E bash - 2>/dev/null \
                        && sudo apt-get install -y -qq nodejs 2>/dev/null
                    fi
                    if ! command -v node &>/dev/null; then
                        sudo apt-get install -y -qq nodejs npm 2>/dev/null \
                        || sudo dnf install -y nodejs npm 2>/dev/null \
                        || sudo pacman -S --noconfirm nodejs npm 2>/dev/null \
                        || true
                    fi ;;
            wsl)    sudo apt-get update -qq && sudo apt-get install -y -qq nodejs npm 2>/dev/null || true ;;
        esac
        if command -v node &>/dev/null; then
            ok "Node.js: $(node --version)"
        else
            warn "Node.js not installed — AI CLI installs may fail"
        fi
    fi

    # --- npm user prefix (avoids EACCES on global installs) ---
    if command -v npm &>/dev/null; then
        local npm_prefix
        npm_prefix="$(npm config get prefix 2>/dev/null)"
        if [[ "$npm_prefix" == "/usr" || "$npm_prefix" == "/usr/local" ]]; then
            local user_prefix="$HOME/.npm-global"
            mkdir -p "$user_prefix"
            npm config set prefix "$user_prefix" 2>/dev/null
            export PATH="$user_prefix/bin:$PATH"
            ok "npm prefix: $user_prefix (no sudo needed)"
        fi
    fi
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

    # DDD directory structure
    mkdir -p "$INSTALL_DIR/bot/i18n" "$INSTALL_DIR/bot/ai"
    mkdir -p "$INSTALL_DIR/bot/commands/core" "$INSTALL_DIR/bot/commands/ai"
    mkdir -p "$INSTALL_DIR/bot/commands/file" "$INSTALL_DIR/bot/commands/session"
    mkdir -p "$INSTALL_DIR/bot/commands/usage" "$INSTALL_DIR/bot/commands/system"
    mkdir -p "$INSTALL_DIR/config" "$INSTALL_DIR/data/sessions" "$INSTALL_DIR/data/downloads"
    mkdir -p "$INSTALL_DIR/data/snapshots" "$INSTALL_DIR/logs" "$INSTALL_DIR/bin"

    local files=(
        "bot/main.py:bot/main.py"
        "bot/config.py:bot/config.py"
        "bot/state.py:bot/state.py"
        "bot/telegram.py:bot/telegram.py"
        "bot/tokens.py:bot/tokens.py"
        "bot/sessions.py:bot/sessions.py"
        "bot/downloader.py:bot/downloader.py"
        "bot/fileviewer.py:bot/fileviewer.py"
        "bot/tunnel.py:bot/tunnel.py"
        "bot/onboard.py:bot/onboard.py"
        "bot/ai/__init__.py:bot/ai/__init__.py"
        "bot/ai/claude.py:bot/ai/claude.py"
        "bot/ai/codex.py:bot/ai/codex.py"
        "bot/ai/gemini.py:bot/ai/gemini.py"
        "bot/ai/connect.py:bot/ai/connect.py"
        "bot/cli_watcher.py:bot/cli_watcher.py"
        "bot/i18n/__init__.py:bot/i18n/__init__.py"
        "bot/i18n/ko.json:bot/i18n/ko.json"
        "bot/i18n/en.json:bot/i18n/en.json"
        "bot/commands/__init__.py:bot/commands/__init__.py"
        "bot/commands/core/__init__.py:bot/commands/core/__init__.py"
        "bot/commands/core/help.py:bot/commands/core/help.py"
        "bot/commands/core/status.py:bot/commands/core/status.py"
        "bot/commands/core/cancel.py:bot/commands/core/cancel.py"
        "bot/commands/core/restart.py:bot/commands/core/restart.py"
        "bot/commands/ai/__init__.py:bot/commands/ai/__init__.py"
        "bot/commands/ai/model.py:bot/commands/ai/model.py"
        "bot/commands/ai/cost.py:bot/commands/ai/cost.py"
        "bot/commands/ai/connect_cmd.py:bot/commands/ai/connect_cmd.py"
        "bot/commands/file/__init__.py:bot/commands/file/__init__.py"
        "bot/commands/file/pwd.py:bot/commands/file/pwd.py"
        "bot/commands/file/cd.py:bot/commands/file/cd.py"
        "bot/commands/file/ls.py:bot/commands/file/ls.py"
        "bot/commands/session/__init__.py:bot/commands/session/__init__.py"
        "bot/commands/session/session.py:bot/commands/session/session.py"
        "bot/commands/session/clear.py:bot/commands/session/clear.py"
        "bot/commands/usage/__init__.py:bot/commands/usage/__init__.py"
        "bot/commands/usage/total_tokens.py:bot/commands/usage/total_tokens.py"
        "bot/commands/system/__init__.py:bot/commands/system/__init__.py"
        "bot/commands/system/settings.py:bot/commands/system/settings.py"
        "bot/commands/system/update.py:bot/commands/system/update.py"
        "bot/commands/system/skills.py:bot/commands/system/skills.py"
        "bot/commands/system/builtin.py:bot/commands/system/builtin.py"
    )

    local total=${#files[@]} i=0
    for entry in "${files[@]}"; do
        local src="${entry%%:*}" dest="${entry##*:}"
        _dl "${GITHUB_RAW}/${src}" "$INSTALL_DIR/${dest}"
        (( ++i ))
        printf "\r\033[K  ${CYAN}[%d/%d]${NC} %s" "$i" "$total" "${dest}"
    done
    echo ""
    ok "Downloaded ${total} files → ${INSTALL_DIR}"
}

# ── cloudflared ──────────────────────────────────────────────────────────────
install_cloudflared() {
    local bin_path="$INSTALL_DIR/bin/cloudflared"
    if command -v cloudflared &>/dev/null || [ -f "$bin_path" ]; then
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
            tar -xzf /tmp/cloudflared.tgz -C "$INSTALL_DIR/bin" cloudflared 2>/dev/null || true
            chmod +x "$bin_path" 2>/dev/null || true
            rm -f /tmp/cloudflared.tgz
            ok "cloudflared installed"
        else
            warn "cloudflared install failed (will retry on first run)"
        fi
    else
        cf_url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${arch}"
        if curl -sL "$cf_url" -o "$bin_path" 2>/dev/null; then
            chmod +x "$bin_path"
            ok "cloudflared installed"
        else
            warn "cloudflared install failed (will retry on first run)"
        fi
    fi
}

# ── Onboarding ───────────────────────────────────────────────────────────────
run_onboarding() {
    $PYTHON "$INSTALL_DIR/bot/onboard.py" || {
        warn "Onboarding exited early — run '$PYTHON $INSTALL_DIR/bot/onboard.py' to reconfigure."
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
            cat > "$svc/sumone-bot.service" << EOF
[Unit]
Description=sumone Telegram Bot

[Service]
ExecStart=$PYTHON $BOT_PATH
Restart=on-failure
RestartSec=5
Environment=HOME=$HOME

[Install]
WantedBy=default.target
EOF
            # Clean up old service name
            systemctl --user stop claude-telegram.service 2>/dev/null || true
            systemctl --user disable claude-telegram.service 2>/dev/null || true
            rm -f "$svc/claude-telegram.service"
            # Kill any stale bot process
            pkill -f "python.*main\.py.*\.sumone" 2>/dev/null || true
            sleep 1
            systemctl --user daemon-reload
            systemctl --user enable sumone-bot.service
            systemctl --user restart sumone-bot.service
            ok "systemd service registered (auto-start enabled)"
            echo -e "  ${DIM}Status:  systemctl --user status sumone-bot"
            echo -e "  Logs:    tail -f $INSTALL_DIR/logs/bot.log${NC}"
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
    <key>StandardOutPath</key><string>$INSTALL_DIR/logs/bot-stdout.log</string>
    <key>StandardErrorPath</key><string>$INSTALL_DIR/logs/bot-stderr.log</string>
</dict></plist>
EOF
            launchctl load "$plist" 2>/dev/null || true
            launchctl start com.sumone.telegram-bot 2>/dev/null || true
            ok "launchd service registered"
            echo -e "  ${DIM}Logs: tail -f $INSTALL_DIR/logs/bot.log${NC}"
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
        linux) echo "    systemctl --user stop sumone-bot && systemctl --user disable sumone-bot" ;;
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

    check_deps           # [1/4]
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
