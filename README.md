# sumone

> **Omni AI Orchestration** — Control multiple AI coding agents from Telegram.

A Telegram bot that bridges your phone to multiple AI coding CLIs — **Claude Code**, **Codex**, and **Gemini CLI**. Send messages, receive rich responses, manage sessions, switch models and providers — all from Telegram.

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.8+-blue?logo=python&logoColor=white" alt="Python 3.8+">
  <img src="https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20Windows%20%7C%20WSL-green" alt="Platform">
  <img src="https://img.shields.io/badge/AI-Claude%20%7C%20Codex%20%7C%20Gemini-blueviolet" alt="Multi-AI">
  <img src="https://img.shields.io/badge/License-MIT-yellow" alt="License">
</p>

---

## Features

- **Multi-AI provider** — Claude Code, Codex CLI, Gemini CLI — switch with `/model` or `/connect`
- **Session persistence** — Resume previous conversations with `/session`
- **Model switching** — `/model opus`, `/model sonnet`, `/model flash`, `/model codex`
- **File & image analysis** — Attach photos or documents for automatic analysis
- **Real-time status** — See what AI is doing (reading files, running commands, searching code...)
- **Global cost tracking** — Per-request, bot-session, and total usage across all sessions (input/output tokens, cost)
- **Multi-PC token aggregation** — `/total_tokens` aggregates token usage across multiple PCs
- **Interactive questions** — When AI asks a question, pick an option by number
- **Message queue** — Messages sent while AI is busy are queued and processed automatically
- **Self-updating** — `/update_bot` checks GitHub for updates and applies them automatically
- **Duplicate process guard** — Automatically detects and kills duplicate bot instances on startup
- **Slash commands** — Use CLI slash commands (`/compact`, `/review`, etc.) directly from Telegram
- **Plugin skill auto-discovery** — Automatically detects installed plugins and creates per-plugin menu commands (e.g., `/omc`)
- **Web file viewer** — Browse and download files modified by AI through a secure web interface (cloudflared tunnel, read-only, session-scoped tokens)
- **Settings UI** — `/settings` with inline keyboard + web settings page for provider management
- **AI provider connection** — `/connect claude`, `/connect codex`, `/connect gemini` for guided CLI auth setup
- **Auto-start on boot** — systemd (Linux), launchd (macOS), Task Scheduler (Windows), .bashrc (WSL)
- **Zero dependencies** — Pure Python, no pip packages required
- **i18n** — Single codebase with JSON language packs (Korean / English)

## Prerequisites

| Requirement | Install |
|---|---|
| **Python 3.8+** | [python.org](https://python.org/downloads/) |
| **At least one AI CLI** | Claude: `npm i -g @anthropic-ai/claude-code` / Codex: `npm i -g @openai/codex` / Gemini: `npm i -g @anthropic-ai/claude-code` (see provider docs) |
| **Telegram Bot Token** | [@BotFather](https://t.me/BotFather) → `/newbot` |
| **Your Chat ID** | [@userinfobot](https://t.me/userinfobot) → `/start` |

## Quick Start

### Linux / macOS / WSL

```bash
curl -fsSL https://raw.githubusercontent.com/xmin-02/sumone/main/setup.sh -o setup.sh
chmod +x setup.sh
./setup.sh
```

### Windows (PowerShell)

```powershell
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/xmin-02/sumone/main/setup.ps1" -OutFile setup.ps1
powershell -ExecutionPolicy Bypass -File setup.ps1
```

The setup script will:
1. Check prerequisites (Python, AI CLIs)
2. Ask for language (Korean / English)
3. Ask for Bot Token, Chat ID, and working directory
4. Download all bot modules from GitHub
5. Save settings to `config.json` (token and secrets stay local, never uploaded)
6. Register auto-start service for your OS
7. Start the bot immediately

Once running, open Telegram and send `/help` to your bot.

## Architecture

```
GitHub Repository                    Your Machine
┌─────────────────┐                  ┌──────────────────────────────┐
│ bot/             │   setup.sh/ps1  │ ~/.sumone/                   │
│   main.py        │ ───download──── │   bot/                       │
│   config.py      │                 │     main.py       (entry)    │
│   ai/            │   /update_bot   │     config.py     (settings) │
│   commands/      │ ─────check───── │     ai/           (providers)│
│   i18n/          │                 │     commands/     (plugins)  │
│                  │                 │     i18n/         (lang)     │
│ setup.sh         │                 │   config/                    │
│ setup.ps1        │                 │     config.json   (secrets)  │
└─────────────────┘                 │   data/                      │
                                    │     sessions/     (history)  │
                                    │     token_log     (tracking) │
                                    │   logs/                      │
                                    │     bot.log       (runtime)  │
                                    └──────────┬───────────────────┘
                                               │
                                               ▼
                                    AI CLI (subprocess)
                                    Claude / Codex / Gemini
                                               │
                                               ▼
                                    Telegram Bot API → your phone
```

- **Bot code** lives on GitHub — no secrets embedded
- **config.json** stays on your machine — contains token, chat ID, language
- `/update_bot` downloads the latest code from GitHub while preserving your config
- **Modular design** — commands are plugins with decorator-based auto-registration

## Commands

### Bot Commands

| Command | Description |
|---|---|
| `/help` | Usage guide |
| `/session` | List and switch between recent sessions |
| `/clear` | Start a new conversation (clear session) |
| `/model [name]` | Change model (`opus`, `sonnet`, `haiku`, `flash`, `codex`, `default`) |
| `/cost` | Show cost info (per-request + bot session + global usage with token counts) |
| `/status` | Show bot status (session, model, provider, state) |
| `/cancel` | Cancel currently running AI process |
| `/settings` | Bot settings with inline keyboard + web settings page |
| `/connect [provider]` | Connect an AI provider (`claude`, `codex`, `gemini`) |
| `/total_tokens` | Aggregate token usage across multiple PCs |
| `/pwd` | Show current working directory |
| `/cd [path]` | Change working directory |
| `/ls [path]` | List files and folders |
| `/update_bot` | Check GitHub for updates and auto-apply |
| `/restart_bot` | Restart the bot process |
| `/builtin` | List CLI built-in commands |
| `/skills` | List OMC skills |

### Passthrough Commands

These are forwarded directly to the active AI CLI:

| Command | Description |
|---|---|
| `/compact` | Compress context |
| `/init` | Initialize project |
| `/review` | Code review |
| `/security-review` | Security review |
| `/autopilot` | OMC autonomous execution |
| `/ralph` | OMC repeat until complete |
| `/team` | OMC multi-agent collaboration |

### Web File Viewer

When AI modifies files during a session, the bot automatically sends a secure link to view them in your browser.

- **Read-only** — View and download only; no editing or uploading
- **Auto-detected** — Tracks `Edit` and `Write` tool usage from CLI output
- **Secure access** — Session-scoped token URL via cloudflared tunnel
- **File type preview** — Code files with line numbers, images inline, other files download-only
- **Auto-setup** — cloudflared is automatically downloaded on first run if not installed

## Usage Examples

```
# Ask a question
Analyze the mutation.go file

# Switch model / provider
/model opus
/model codex
/model flash

# Connect a new AI provider
/connect claude
/connect codex
/connect gemini

# Attach a screenshot and ask
(attach image) Fix this error message

# Resume previous session
/session
> 3  (pick by number)

# Check total token usage across all sessions
/cost

# Aggregate tokens across multiple PCs
/total_tokens

# Toggle settings (cost display, status messages)
/settings

# Update bot to latest version
/update_bot

# Compress context when conversation gets long
/compact
```

## Configuration

```
~/.sumone/
├── bot/
│   ├── main.py              # Entry point
│   ├── config.py            # Configuration loader
│   ├── state.py             # Global state
│   ├── telegram.py          # Telegram API helpers
│   ├── ai/
│   │   ├── __init__.py      # BaseRunner & factory
│   │   ├── claude.py        # Claude Code CLI integration
│   │   ├── codex.py         # Codex CLI integration
│   │   ├── gemini.py        # Gemini CLI integration
│   │   └── connect.py       # AI provider connection flow
│   ├── tokens.py            # Token tracking
│   ├── sessions.py          # Session management
│   ├── downloader.py        # File download handler
│   ├── fileviewer.py        # Read-only HTTP file viewer server
│   ├── tunnel.py            # Cloudflared tunnel management
│   ├── onboard.py           # CLI onboarding TUI
│   ├── i18n/
│   │   ├── __init__.py      # i18n loader & t() function
│   │   ├── ko.json          # Korean language pack
│   │   └── en.json          # English language pack
│   └── commands/
│       ├── __init__.py      # Command registry (@command, @callback)
│       ├── core/            # /help, /status, /cancel, /restart_bot
│       ├── ai/              # /model, /cost, /connect
│       ├── file/            # /pwd, /cd, /ls
│       ├── session/         # /session, /clear
│       ├── usage/           # /total_tokens
│       └── system/          # /settings, /update_bot, /builtin, /skills
├── config/
│   └── config.json          # Your settings (secrets — chmod 600)
├── data/
│   ├── sessions/            # Session history
│   ├── downloads/           # Downloaded files from Telegram
│   └── token_log.jsonl      # Token usage log
└── logs/
    └── bot.log              # Runtime log
```

`config.json` example:
```json
{
    "bot_token": "123456789:ABCdef...",
    "chat_id": "12345678",
    "work_dir": "/home/user",
    "lang": "ko",
    "github_repo": "xmin-02/sumone"
}
```

### Adding a New Command

Create a file in `commands/<category>/` and use the `@command` decorator:

```python
# commands/core/mycommand.py
from commands import command
from telegram import send_html

@command("/mycommand", aliases=["/mc"])
def handle_mycommand(text):
    send_html("<b>Hello from my command!</b>")
```

Commands are auto-discovered — no manual import needed.

### Service Management

**Linux (systemd):**
```bash
systemctl --user status sumone           # Status
systemctl --user restart sumone          # Restart
systemctl --user stop sumone             # Stop
```

**macOS (launchd):**
```bash
launchctl list | grep sumone             # Status
launchctl stop com.sumone.telegram-bot   # Stop
launchctl start com.sumone.telegram-bot  # Start
```

**Windows (Task Scheduler):**
```powershell
Get-ScheduledTask -TaskName SumoneBot | Select State
Stop-ScheduledTask -TaskName SumoneBot
Start-ScheduledTask -TaskName SumoneBot
```

**WSL:**
```bash
pgrep -f main.py                           # Check if running
pkill -f main.py                           # Stop
```

## Upgrading from v1

If you're running the old single-file bot (`telegram-bot-ko.py` / `telegram-bot-en.py`), simply run `/update_bot` in Telegram. The bot will automatically:

1. Download the migration script from GitHub
2. Download all v2 modular files
3. Restart with the new `main.py` entry point

No manual steps needed. Your `config.json` is preserved.

## Uninstall

**Linux:**
```bash
systemctl --user stop sumone
systemctl --user disable sumone
rm ~/.config/systemd/user/sumone.service
rm -rf ~/.sumone
```

**macOS:**
```bash
launchctl stop com.sumone.telegram-bot
launchctl unload ~/Library/LaunchAgents/com.sumone.telegram-bot.plist
rm ~/Library/LaunchAgents/com.sumone.telegram-bot.plist
rm -rf ~/.sumone
```

**Windows:**
```powershell
Stop-ScheduledTask -TaskName SumoneBot
Unregister-ScheduledTask -TaskName SumoneBot -Confirm:$false
Remove-Item -Recurse -Force "$env:USERPROFILE\.sumone"
```

## Security Notes

- Bot token and Chat ID are stored in `config.json` (chmod 600) — **never committed to git**
- Only messages from your configured Chat ID are processed; all others are rejected
- The bot runs with `--dangerously-skip-permissions` for unattended operation — be mindful of what you ask it to do
- Downloaded files are stored locally in `~/.sumone/data/downloads/`

## Troubleshooting

| Problem | Solution |
|---|---|
| Bot doesn't respond | Check `~/.sumone/logs/bot.log` |
| "CLI not found" | Ensure the AI CLI is in PATH: `which claude` / `which codex` / `which gemini` |
| Token verification failed | Re-check token with [@BotFather](https://t.me/BotFather) |
| Permission denied | Run `chmod +x setup.sh` |
| Windows: "scripts disabled" | Run with `-ExecutionPolicy Bypass` flag |
| Duplicate bot instances | Bot auto-kills duplicates on startup; or manually: `pkill -f main.py` |

## Repository Structure

```
├── README.md
├── RELEASE_NOTES.md
├── LICENSE
├── setup.sh                        # Setup script for Linux / macOS / WSL
├── setup.ps1                       # Setup script for Windows
└── bot/
    ├── main.py                     # Entry point (polling, routing, handler)
    ├── config.py                   # Configuration loader & updater
    ├── state.py                    # Global state singleton
    ├── telegram.py                 # Telegram Bot API helpers
    ├── ai/
    │   ├── __init__.py             # BaseRunner & provider factory
    │   ├── claude.py               # Claude Code CLI integration
    │   ├── codex.py                # Codex CLI integration
    │   ├── gemini.py               # Gemini CLI integration
    │   └── connect.py              # AI provider connection flow
    ├── tokens.py                   # Token tracking & multi-PC aggregation
    ├── sessions.py                 # Session listing & management
    ├── downloader.py               # Telegram file download & prompt building
    ├── fileviewer.py               # Read-only HTTP file viewer & settings page
    ├── tunnel.py                   # Cloudflared tunnel management
    ├── onboard.py                  # CLI onboarding TUI (multi-provider)
    ├── i18n/
    │   ├── __init__.py             # Language loader & t() function
    │   ├── ko.json                 # Korean strings
    │   └── en.json                 # English strings
    └── commands/
        ├── __init__.py             # Command registry (@command, @callback)
        ├── core/                   # /help, /status, /cancel, /restart_bot
        ├── ai/                     # /model, /cost, /connect
        ├── file/                   # /pwd, /cd, /ls
        ├── session/                # /session, /clear
        ├── usage/                  # /total_tokens
        └── system/                 # /settings, /update_bot, /builtin, /skills
```

## License

MIT License. See [LICENSE](LICENSE) for details.

---

<details>
<summary><b>한국어 (Korean)</b></summary>

# sumone

> **Omni AI Orchestration** — 여러 AI 코딩 에이전트를 텔레그램에서 원격 제어.

텔레그램에서 여러 AI 코딩 CLI(**Claude Code**, **Codex**, **Gemini CLI**)에 접속할 수 있는 봇입니다. 메시지 전송, 리치 응답 수신, 세션 관리, 모델/프로바이더 전환, 파일 첨부 — 모두 텔레그램에서 가능합니다.

## 주요 기능

- **멀티 AI 프로바이더** — Claude Code, Codex CLI, Gemini CLI — `/model` 또는 `/connect`로 전환
- **세션 유지** — `/session`으로 이전 대화 이어가기
- **모델 전환** — `/model opus`, `/model sonnet`, `/model flash`, `/model codex`
- **파일 & 이미지 분석** — 사진이나 문서를 첨부하면 자동 분석
- **실시간 상태 표시** — AI가 뭘 하고 있는지 표시 (파일 읽기, 명령 실행, 코드 검색 등)
- **전체 비용 추적** — 요청별 / 봇 세션 / 전체 세션 누적 비용 및 토큰 사용량
- **다중 PC 토큰 집계** — `/total_tokens`로 여러 PC의 토큰 사용량 통합 조회
- **대화형 질문** — AI가 질문하면 번호로 선택
- **메시지 대기열** — AI 처리 중 보낸 메시지는 자동으로 대기열에 추가되어 순서대로 처리
- **자동 업데이트** — `/update_bot`으로 GitHub에서 최신 버전 확인 및 자동 적용
- **중복 실행 방지** — 시작 시 중복 봇 프로세스를 자동 감지하고 종료
- **슬래시 명령어** — CLI 슬래시 명령어 (`/compact`, `/review` 등)를 텔레그램에서 직접 사용
- **플러그인 스킬 자동 탐색** — 설치된 플러그인을 자동 감지하여 플러그인별 메뉴 명령어 생성 (예: `/omc`)
- **웹 파일 뷰어** — AI가 수정한 파일을 안전한 웹 인터페이스로 열람/다운로드 (cloudflared 터널, 읽기 전용, 세션 범위 토큰)
- **설정 UI** — `/settings`로 인라인 키보드 + 웹 설정 페이지에서 프로바이더 관리
- **AI 프로바이더 연결** — `/connect claude`, `/connect codex`, `/connect gemini`으로 CLI 인증 안내
- **부팅 시 자동 시작** — systemd (Linux), launchd (macOS), 작업 스케줄러 (Windows), .bashrc (WSL)
- **외부 의존성 없음** — 순수 Python, pip 패키지 불필요
- **i18n** — 단일 코드베이스 + JSON 언어팩 (한국어 / 영어)

## 빠른 시작

### Linux / macOS / WSL

```bash
curl -fsSL https://raw.githubusercontent.com/xmin-02/sumone/main/setup.sh -o setup.sh
chmod +x setup.sh
./setup.sh
```

### Windows (PowerShell)

```powershell
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/xmin-02/sumone/main/setup.ps1" -OutFile setup.ps1
powershell -ExecutionPolicy Bypass -File setup.ps1
```

설치 스크립트가 자동으로:
1. 필수 프로그램 확인 (Python, AI CLI)
2. 언어 선택 (한국어 / English)
3. 봇 토큰, Chat ID, 작업 디렉토리 입력 받기
4. GitHub에서 전체 모듈 다운로드
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
| `/model [이름]` | 모델 변경 (`opus`, `sonnet`, `haiku`, `flash`, `codex`, `default`) |
| `/cost` | 비용 정보 (요청별 + 봇 세션 + 전체 세션 토큰 사용량) |
| `/status` | 봇 상태 (세션, 모델, 프로바이더, 상태) |
| `/cancel` | 실행 중인 AI 프로세스 취소 |
| `/settings` | 봇 설정 (인라인 키보드 + 웹 설정) |
| `/connect [프로바이더]` | AI 프로바이더 연결 (`claude`, `codex`, `gemini`) |
| `/total_tokens` | 전체 PC 토큰 사용량 집계 |
| `/pwd` | 현재 작업 디렉토리 |
| `/cd [경로]` | 디렉토리 이동 |
| `/ls [경로]` | 파일/폴더 목록 |
| `/update_bot` | GitHub에서 최신 버전 확인 및 자동 업데이트 |
| `/restart_bot` | 봇 재시작 |
| `/builtin` | CLI 빌트인 명령어 목록 |
| `/skills` | OMC 스킬 목록 |

## v1에서 업그레이드

기존 단일 파일 봇 (`telegram-bot-ko.py` / `telegram-bot-en.py`) 사용 중이라면, 텔레그램에서 `/update_bot`만 실행하세요. 봇이 자동으로:

1. GitHub에서 마이그레이션 스크립트 다운로드
2. v2 모듈 파일 전체 다운로드
3. 새로운 `main.py`로 재시작

수동 작업 없이 자동 전환됩니다. `config.json`은 그대로 유지됩니다.

## 보안 참고사항

- 봇 토큰과 Chat ID는 `config.json`에 저장됩니다 (chmod 600) — **git에 커밋되지 않음**
- 설정된 Chat ID의 메시지만 처리되며, 다른 사용자의 메시지는 모두 거부됩니다
- 봇은 무인 운영을 위해 `--dangerously-skip-permissions`로 실행됩니다 — 요청 내용에 주의하세요

</details>

---

Made with sumone.
