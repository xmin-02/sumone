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
- **비용 추적** — 요청별/누적 비용 표시
- **대화형 질문** — Claude가 질문하면 번호로 선택
- **스킬 명령어** — OMC 스킬 (`/autopilot`, `/plan`, `/code-review` 등)을 텔레그램에서 직접 사용
- **부팅 시 자동 시작** — systemd (Linux), launchd (macOS), 작업 스케줄러 (Windows), .bashrc (WSL)
- **외부 의존성 없음** — 순수 Python, pip 패키지 불필요

## 사전 요구 사항

| 항목 | 설치 방법 |
|---|---|
| **Python 3.8+** | [python.org](https://python.org/downloads/) |
| **Claude Code CLI** | `npm install -g @anthropic-ai/claude-code` |
| **텔레그램 봇 토큰** | [@BotFather](https://t.me/BotFather) → `/newbot` |
| **내 Chat ID** | [@userinfobot](https://t.me/userinfobot) → `/start` |

## 빠른 시작

### Linux / macOS / WSL

```bash
chmod +x setup-claude-telegram-bot.sh
./setup-claude-telegram-bot.sh
```

### Windows (PowerShell)

```powershell
powershell -ExecutionPolicy Bypass -File setup-claude-telegram-bot.ps1
```

설치 스크립트가 자동으로:
1. 필수 프로그램 확인 (Python, Claude CLI)
2. 봇 토큰, Chat ID, 작업 디렉토리 입력 받기
3. `~/.claude-telegram-bot/`에 봇 설치
4. OS별 자동 시작 서비스 등록
5. 봇 즉시 시작

실행 후 텔레그램에서 `/help`을 보내서 확인하세요.

## 명령어

### 봇 명령어

| 명령어 | 설명 |
|---|---|
| `/help` | 사용법 안내 |
| `/session` | 최근 세션 목록 및 전환 |
| `/clear` | 새 대화 시작 (세션 초기화) |
| `/model [이름]` | 모델 변경 (`opus`, `sonnet`, `haiku`, `default`) |
| `/cost` | 비용 정보 (마지막 요청 + 누적) |
| `/status` | 봇 상태 (세션, 모델, 상태) |
| `/cancel` | 실행 중인 Claude 프로세스 취소 |
| `/builtin` | CLI 빌트인 명령어 목록 |
| `/skills` | 사용 가능한 OMC 스킬 목록 |

### Claude에 전달되는 명령어

| 명령어 | 설명 |
|---|---|
| `/compact` | 컨텍스트 압축 |
| `/init` | 프로젝트 초기화 |
| `/review` | 코드 리뷰 |
| `/security-review` | 보안 리뷰 |

### OMC 스킬 (설치된 경우)

| 명령어 | 설명 |
|---|---|
| `/autopilot` | 자율 실행 |
| `/plan` | 전략적 계획 |
| `/code-review` | 종합 코드 리뷰 |
| `/ultrawork` | 최대 병렬 실행 |
| `/team` | 다중 에이전트 협업 |

## 사용 예시

```
# 질문하기
mutation.go 파일 분석해줘

# 스킬 실행
/autopilot 로그인 기능 만들어줘

# 모델 변경
/model opus

# 스크린샷 첨부해서 질문
(이미지 첨부) 이 에러 메시지 해결해줘

# 이전 세션 이어가기
/session
> 3  (번호로 선택)
```

## 작동 방식

```
텔레그램 앱 (휴대폰/데스크톱)
    │
    ▼
Telegram Bot API (롱 폴링)
    │
    ▼
telegram-bot.py (내 컴퓨터에서 실행)
    │
    ▼
claude --output-format stream-json -p "메시지"
    │
    ▼
Claude Code CLI → 파일 읽기/쓰기, 명령 실행 등
    │
    ▼
스트리밍 JSON 응답 → 파싱 → 포맷팅 → 텔레그램으로 전송
```

## 제거 방법

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

## 보안 참고사항

- 봇 토큰과 Chat ID는 설치된 스크립트에 포함됩니다 — **설치된 `telegram-bot.py`를 공유하지 마세요**
- 설정된 Chat ID의 메시지만 처리되며, 다른 사용자의 메시지는 모두 거부됩니다
- 봇은 무인 운영을 위해 `--dangerously-skip-permissions`로 실행됩니다 — 요청 내용에 주의하세요
- 다운로드된 파일은 `~/.claude-telegram-bot/downloads/`에 로컬 저장됩니다

## 문제 해결

| 문제 | 해결 방법 |
|---|---|
| 봇이 응답하지 않음 | `~/.claude-telegram-bot/bot.log` 확인 |
| "Claude CLI를 찾을 수 없음" | PATH에 `claude`가 있는지 확인: `which claude` |
| 토큰 검증 실패 | [@BotFather](https://t.me/BotFather)에서 토큰 재확인 |
| 권한 거부 | `chmod +x setup-claude-telegram-bot.sh` 실행 |
| Windows: "스크립트 사용 불가" | `-ExecutionPolicy Bypass` 플래그로 실행 |

</details>

---

Made with Claude Code.
