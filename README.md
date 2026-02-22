# Claude Telegram Bot

> Control [Claude Code](https://claude.ai/code) from Telegram — anywhere, anytime.

A Telegram bot that bridges your phone to a running Claude Code CLI session. Send messages, receive rich responses, manage sessions, switch models, attach files — all from Telegram.

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.8+-blue?logo=python&logoColor=white" alt="Python 3.8+">
  <img src="https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20Windows%20%7C%20WSL-green" alt="Platform">
  <img src="https://img.shields.io/badge/Claude%20Code-CLI-blueviolet?logo=anthropic" alt="Claude Code CLI">
  <img src="https://img.shields.io/badge/License-MIT-yellow" alt="License">
</p>

---

## Features

- **Full Claude Code access** — Send any message or command, get streamed results
- **Session persistence** — Resume previous conversations with `/session`
- **Model switching** — `/model opus`, `/model sonnet`, `/model haiku`
- **File & image analysis** — Attach photos or documents, Claude analyzes them automatically
- **Real-time status** — See what Claude is doing (reading files, running commands, searching code...)
- **Cost tracking** — Per-request and cumulative cost display
- **Interactive questions** — When Claude asks a question, pick an option by number
- **Skill commands** — Use OMC skills (`/autopilot`, `/plan`, `/code-review`, etc.) directly from Telegram
- **Auto-start on boot** — systemd (Linux), launchd (macOS), Task Scheduler (Windows), .bashrc (WSL)
- **Zero dependencies** — Pure Python, no pip packages required

## Prerequisites

| Requirement | Install |
|---|---|
| **Python 3.8+** | [python.org](https://python.org/downloads/) |
| **Claude Code CLI** | `npm install -g @anthropic-ai/claude-code` |
| **Telegram Bot Token** | [@BotFather](https://t.me/BotFather) → `/newbot` |
| **Your Chat ID** | [@userinfobot](https://t.me/userinfobot) → `/start` |

## Quick Start

### Linux / macOS / WSL

```bash
chmod +x setup-claude-telegram-bot.sh
./setup-claude-telegram-bot.sh
```

### Windows (PowerShell)

```powershell
powershell -ExecutionPolicy Bypass -File setup-claude-telegram-bot.ps1
```

The setup script will:
1. Check prerequisites (Python, Claude CLI)
2. Ask for your Bot Token, Chat ID, and working directory
3. Install the bot to `~/.claude-telegram-bot/`
4. Register auto-start service for your OS
5. Start the bot immediately

Once running, open Telegram and send `/help` to your bot.

## Commands

### Bot Commands

| Command | Description |
|---|---|
| `/help` | Usage guide |
| `/session` | List and switch between recent sessions |
| `/clear` | Start a new conversation (clear session) |
| `/model [name]` | Change model (`opus`, `sonnet`, `haiku`, `default`) |
| `/cost` | Show cost info (last request + cumulative) |
| `/status` | Show bot status (session, model, state) |
| `/cancel` | Cancel currently running Claude process |
| `/builtin` | List CLI built-in commands |
| `/skills` | List available OMC skills |

### Passthrough Commands

These are forwarded directly to Claude Code CLI:

| Command | Description |
|---|---|
| `/compact` | Compress context |
| `/init` | Initialize project |
| `/review` | Code review |
| `/security-review` | Security review |

### OMC Skills (if installed)

| Command | Description |
|---|---|
| `/autopilot` | Autonomous execution |
| `/plan` | Strategic planning |
| `/code-review` | Comprehensive code review |
| `/ultrawork` | Maximum parallelism |
| `/team` | Multi-agent collaboration |

## Usage Examples

```
# Ask a question
mutation.go 파일 분석해줘

# Run a skill
/autopilot 로그인 기능 만들어줘

# Switch model
/model opus

# Attach a screenshot and ask
(attach image) 이 에러 메시지 해결해줘

# Resume previous session
/session
> 3  (pick by number)
```

## How It Works

```
Telegram App (phone/desktop)
    │
    ▼
Telegram Bot API (long polling)
    │
    ▼
telegram-bot.py (on your machine)
    │
    ▼
claude --output-format stream-json -p "your message"
    │
    ▼
Claude Code CLI → reads/writes files, runs commands, etc.
    │
    ▼
Streamed JSON response → parsed → formatted → sent back to Telegram
```

The bot runs `claude` CLI as a subprocess with `--output-format stream-json`, parsing events in real-time to:
- Show intermediate progress (tool usage, thinking status)
- Detect `AskUserQuestion` events and present interactive choices
- Capture session IDs for conversation continuity
- Track cost and token usage

## Configuration

The bot is installed to `~/.claude-telegram-bot/` with these files:

```
~/.claude-telegram-bot/
├── telegram-bot.py     # Bot script (with your token embedded)
├── bot.log             # Runtime log
└── downloads/          # Downloaded files from Telegram
```

### Service Management

**Linux (systemd):**
```bash
systemctl --user status claude-telegram    # Status
systemctl --user restart claude-telegram   # Restart
systemctl --user stop claude-telegram      # Stop
journalctl --user -u claude-telegram -f    # Logs
```

**macOS (launchd):**
```bash
launchctl list | grep claude               # Status
launchctl stop com.claude.telegram-bot     # Stop
launchctl start com.claude.telegram-bot    # Start
```

**Windows (Task Scheduler):**
```powershell
Get-ScheduledTask -TaskName ClaudeTelegramBot | Select State
Stop-ScheduledTask -TaskName ClaudeTelegramBot
Start-ScheduledTask -TaskName ClaudeTelegramBot
```

**WSL:**
```bash
pgrep -f telegram-bot.py                  # Check if running
pkill -f telegram-bot.py                  # Stop
```

## Uninstall

**Linux:**
```bash
systemctl --user stop claude-telegram
systemctl --user disable claude-telegram
rm ~/.config/systemd/user/claude-telegram.service
rm -rf ~/.claude-telegram-bot
```

**macOS:**
```bash
launchctl stop com.claude.telegram-bot
launchctl unload ~/Library/LaunchAgents/com.claude.telegram-bot.plist
rm ~/Library/LaunchAgents/com.claude.telegram-bot.plist
rm -rf ~/.claude-telegram-bot
```

**Windows:**
```powershell
Stop-ScheduledTask -TaskName ClaudeTelegramBot
Unregister-ScheduledTask -TaskName ClaudeTelegramBot -Confirm:$false
Remove-Item -Recurse -Force "$env:USERPROFILE\.claude-telegram-bot"
```

## Security Notes

- The bot token and chat ID are embedded in the installed script — **do not share the installed `telegram-bot.py`**
- Only messages from your configured Chat ID are processed; all others are rejected
- The bot runs with `--dangerously-skip-permissions` for unattended operation — be mindful of what you ask it to do
- Downloaded files are stored locally in `~/.claude-telegram-bot/downloads/`

## Troubleshooting

| Problem | Solution |
|---|---|
| Bot doesn't respond | Check `~/.claude-telegram-bot/bot.log` |
| "Claude CLI not found" | Ensure `claude` is in PATH: `which claude` |
| Token verification failed | Re-check token with [@BotFather](https://t.me/BotFather) |
| Permission denied | Run `chmod +x setup-claude-telegram-bot.sh` |
| Windows: "scripts disabled" | Run with `-ExecutionPolicy Bypass` flag |

## License

MIT License. See [LICENSE](LICENSE) for details.

---

Made with Claude Code.
