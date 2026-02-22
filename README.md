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
- **Global cost tracking** — Per-request, bot-session, and total usage across all Claude sessions (input/output tokens, cost)
- **Interactive questions** — When Claude asks a question, pick an option by number
- **Self-updating** — `/update_bot` checks GitHub for updates and applies them automatically
- **Slash commands** — Use Claude Code slash commands (`/compact`, `/review`, etc.) directly from Telegram
- **Auto-start on boot** — systemd (Linux), launchd (macOS), Task Scheduler (Windows), .bashrc (WSL)
- **Zero dependencies** — Pure Python, no pip packages required
- **Bilingual** — Korean and English versions available

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
curl -fsSL https://raw.githubusercontent.com/xmin-02/Claude-telegram-bot/main/setup.sh -o setup.sh
chmod +x setup.sh
./setup.sh
```

### Windows (PowerShell)

```powershell
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/xmin-02/Claude-telegram-bot/main/setup.ps1" -OutFile setup.ps1
powershell -ExecutionPolicy Bypass -File setup.ps1
```

The setup script will:
1. Check prerequisites (Python, Claude CLI)
2. Ask for language (Korean / English)
3. Ask for Bot Token, Chat ID, and working directory
4. Download the bot from GitHub
5. Save settings to `config.json` (token and secrets stay local, never uploaded)
6. Register auto-start service for your OS
7. Start the bot immediately

Once running, open Telegram and send `/help` to your bot.

## Architecture

```
GitHub Repository                    Your Machine
┌─────────────────┐                  ┌──────────────────────────────┐
│ bot/             │   setup.sh/ps1  │ ~/.claude-telegram-bot/      │
│  telegram-bot-   │ ──download───→  │  telegram-bot.py  (bot code) │
│   ko.py / en.py  │                 │  config.json      (secrets)  │
│                  │  /update_bot    │  bot.log          (runtime)  │
│ setup.sh         │ ←──check────── │  downloads/       (files)    │
│ setup.ps1        │                 └──────────┬───────────────────┘
└─────────────────┘                             │
                                                ▼
                                    claude CLI (subprocess)
                                                │
                                                ▼
                                    Claude Code → files, commands, etc.
                                                │
                                                ▼
                                    Telegram Bot API → your phone
```

- **Bot code** lives on GitHub — no secrets embedded
- **config.json** stays on your machine — contains token, chat ID, language
- `/update_bot` downloads the latest code from GitHub while preserving your config

## Commands

### Bot Commands

| Command | Description |
|---|---|
| `/help` | Usage guide |
| `/session` | List and switch between recent sessions |
| `/clear` | Start a new conversation (clear session) |
| `/model [name]` | Change model (`opus`, `sonnet`, `haiku`, `default`) |
| `/cost` | Show cost info (per-request + bot session + global usage with token counts) |
| `/status` | Show bot status (session, model, state) |
| `/cancel` | Cancel currently running Claude process |
| `/update_bot` | Check GitHub for updates and auto-apply |
| `/builtin` | List CLI built-in commands |

### Passthrough Commands

These are forwarded directly to Claude Code CLI:

| Command | Description |
|---|---|
| `/compact` | Compress context |
| `/init` | Initialize project |
| `/review` | Code review |
| `/security-review` | Security review |

## Usage Examples

```
# Ask a question
Analyze the mutation.go file

# Switch model
/model opus

# Attach a screenshot and ask
(attach image) Fix this error message

# Resume previous session
/session
> 3  (pick by number)

# Check total token usage across all sessions
/cost

# Update bot to latest version
/update_bot

# Compress context when conversation gets long
/compact
```

## Configuration

```
~/.claude-telegram-bot/
├── telegram-bot.py     # Bot script (downloaded from GitHub)
├── config.json         # Your settings (token, chat ID, language) — chmod 600
├── bot.log             # Runtime log
└── downloads/          # Downloaded files from Telegram
```

`config.json` example:
```json
{
    "bot_token": "123456789:ABCdef...",
    "chat_id": "12345678",
    "work_dir": "/home/user",
    "lang": "ko",
    "github_repo": "xmin-02/Claude-telegram-bot"
}
```

### Service Management

**Linux (systemd):**
```bash
systemctl --user status claude-telegram    # Status
systemctl --user restart claude-telegram   # Restart
systemctl --user stop claude-telegram      # Stop
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

- Bot token and Chat ID are stored in `config.json` (chmod 600) — **never committed to git**
- Only messages from your configured Chat ID are processed; all others are rejected
- The bot runs with `--dangerously-skip-permissions` for unattended operation — be mindful of what you ask it to do
- Downloaded files are stored locally in `~/.claude-telegram-bot/downloads/`

## Troubleshooting

| Problem | Solution |
|---|---|
| Bot doesn't respond | Check `~/.claude-telegram-bot/bot.log` |
| "Claude CLI not found" | Ensure `claude` is in PATH: `which claude` |
| Token verification failed | Re-check token with [@BotFather](https://t.me/BotFather) |
| Permission denied | Run `chmod +x setup.sh` |
| Windows: "scripts disabled" | Run with `-ExecutionPolicy Bypass` flag |
| Update failed | Check network, then try manual: `curl -o ~/.claude-telegram-bot/telegram-bot.py https://raw.githubusercontent.com/xmin-02/Claude-telegram-bot/main/bot/telegram-bot-ko.py` |

## Repository Structure

```
├── README.md            # This file
├── LICENSE              # MIT License
├── setup.sh             # Setup script for Linux / macOS / WSL
├── setup.ps1            # Setup script for Windows
└── bot/
    ├── telegram-bot-ko.py   # Bot code (Korean)
    └── telegram-bot-en.py   # Bot code (English)
```

## License

MIT License. See [LICENSE](LICENSE) for details.

---

<details>
<summary><b>한국어 (Korean)</b></summary>

# Claude Telegram Bot

> [Claude Code](https://claude.ai/code)를 텔레그램에서 원격으로 사용하세요 — 언제 어디서나.

휴대폰의 텔레그램에서 Claude Code CLI 세션에 접속할 수 있는 봇입니다. 메시지 전송, 리치 응답 수신, 세션 관리, 모델 전환, 파일 첨부 — 모두 텔레그램에서 가능합니다.

## 주요 기능

- **Claude Code 전체 접근** — 메시지나 명령어를 보내면 스트리밍 결과를 받음
- **세션 유지** — `/session`으로 이전 대화 이어가기
- **모델 전환** — `/model opus`, `/model sonnet`, `/model haiku`
- **파일 & 이미지 분석** — 사진이나 문서를 첨부하면 Claude가 자동으로 분석
- **실시간 상태 표시** — Claude가 뭘 하고 있는지 표시 (파일 읽기, 명령 실행, 코드 검색 등)
- **전체 비용 추적** — 요청별 / 봇 세션 / 전체 세션 누적 비용 및 토큰 사용량 표시
- **대화형 질문** — Claude가 질문하면 번호로 선택
- **자동 업데이트** — `/update_bot`으로 GitHub에서 최신 버전 확인 및 자동 적용
- **슬래시 명령어** — Claude Code 슬래시 명령어 (`/compact`, `/review` 등)를 텔레그램에서 직접 사용
- **부팅 시 자동 시작** — systemd (Linux), launchd (macOS), 작업 스케줄러 (Windows), .bashrc (WSL)
- **외부 의존성 없음** — 순수 Python, pip 패키지 불필요
- **한/영 지원** — 한국어, 영어 버전 선택 가능

## 빠른 시작

### Linux / macOS / WSL

```bash
curl -fsSL https://raw.githubusercontent.com/xmin-02/Claude-telegram-bot/main/setup.sh -o setup.sh
chmod +x setup.sh
./setup.sh
```

### Windows (PowerShell)

```powershell
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/xmin-02/Claude-telegram-bot/main/setup.ps1" -OutFile setup.ps1
powershell -ExecutionPolicy Bypass -File setup.ps1
```

설치 스크립트가 자동으로:
1. 필수 프로그램 확인 (Python, Claude CLI)
2. 언어 선택 (한국어 / English)
3. 봇 토큰, Chat ID, 작업 디렉토리 입력 받기
4. GitHub에서 봇 다운로드
5. `config.json`에 설정 저장 (토큰은 로컬에만 보관)
6. OS별 자동 시작 서비스 등록
7. 봇 즉시 시작

실행 후 텔레그램에서 `/help`을 보내서 확인하세요.

## 명령어

| 명령어 | 설명 |
|---|---|
| `/help` | 사용법 안내 |
| `/session` | 최근 세션 목록 및 전환 |
| `/clear` | 새 대화 시작 (세션 초기화) |
| `/model [이름]` | 모델 변경 (`opus`, `sonnet`, `haiku`, `default`) |
| `/cost` | 비용 정보 (요청별 + 봇 세션 + 전체 세션 토큰 사용량) |
| `/status` | 봇 상태 (세션, 모델, 상태) |
| `/cancel` | 실행 중인 Claude 프로세스 취소 |
| `/update_bot` | GitHub에서 최신 버전 확인 및 자동 업데이트 |
| `/builtin` | CLI 빌트인 명령어 목록 |

## 보안 참고사항

- 봇 토큰과 Chat ID는 `config.json`에 저장됩니다 (chmod 600) — **git에 커밋되지 않음**
- 설정된 Chat ID의 메시지만 처리되며, 다른 사용자의 메시지는 모두 거부됩니다
- 봇은 무인 운영을 위해 `--dangerously-skip-permissions`로 실행됩니다 — 요청 내용에 주의하세요

</details>

---

Made with Claude Code.
