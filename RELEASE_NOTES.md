# Release Notes

## v2.0.0 — Modular Architecture (2026-02-23)

### Breaking Changes
- Bot entry point changed from `telegram-bot-ko.py` / `telegram-bot-en.py` to `main.py`
- Single-file architecture replaced with 17-module structure
- Setup scripts now download 19 files instead of 1

### Migration
- **Automatic**: Run `/update_bot` from the old bot — it downloads the migration script and transitions to v2 automatically
- **Manual**: Re-run `setup.sh` or `setup.ps1` to install fresh
- `config.json` is fully preserved during migration

### New Features

#### i18n — Single Codebase, Multiple Languages
- Eliminated 99% code duplication between ko/en versions (3,267 → 1,897 lines)
- Language packs are JSON files (`i18n/ko.json`, `i18n/en.json`) with 177 keys each
- Language switching is config-only — change `lang` in `config.json`, no code changes needed
- Adding a new language: create `i18n/xx.json`, done

#### Plugin Architecture — Decorator-based Commands
- Commands auto-register via `@command` and `@callback` decorators
- Adding a new command: create a file in `commands/`, add decorator, import in `main.py`
- 20 command handlers + 2 callback prefixes registered automatically on startup

#### Message Queue
- Messages sent while Claude is processing are now queued (previously discarded)
- Queue processes automatically in order after each task completes
- User sees "Message queued (#1). Will be processed after current task."

#### Duplicate Process Guard
- On startup, detects and kills any other running bot instances
- Uses PowerShell `Get-CimInstance` (Windows) or `ps` (Linux/macOS)
- Sends notification: "Duplicate detected: terminated N existing bot process(es)"

#### Multi-PC Token Aggregation (`/total_tokens`)
- Aggregate token usage across multiple machines
- Connect other PCs via bot token
- Inline keyboard UI for management
- Data exchange via bot description channel (`language_code='zu'`)

#### Settings UI (`/settings`)
- Inline keyboard for toggling:
  - Cost per request display
  - Status messages during processing
  - Global cost in `/cost`
  - Remote token aggregation in footer
  - Token display period (session/day/month/year/total)

#### Filesystem Commands
- `/pwd` — Show current working directory
- `/cd [path]` — Change directory (supports `~`, `-`, `..`)
- `/ls [path]` — List files with sizes, hidden file toggle with `-a`

### Improvements
- Bot profile photo auto-synced from GitHub on `/update_bot` (SHA-256 hash comparison)
- `MAX_PARTS` increased from 5 to 20 for long message handling
- Token tracking uses JSONL scanning instead of external hook files
- Cross-platform path handling for Windows APPDATA vs Linux ~/.claude
- Underscore-to-hyphen normalization for slash commands (`/code_review` → `/code-review`)

### Module Structure
```
bot/
├── main.py              # Entry point, polling, routing (317 lines)
├── config.py            # Configuration loader (64 lines)
├── state.py             # Global state (22 lines)
├── telegram.py          # TG API helpers (138 lines)
├── claude.py            # CLI integration (203 lines)
├── tokens.py            # Token tracking (192 lines)
├── sessions.py          # Session management (89 lines)
├── downloader.py        # File download (51 lines)
├── i18n/                # Language packs
│   ├── __init__.py      # t() function (26 lines)
│   ├── ko.json          # Korean (177 keys)
│   └── en.json          # English (177 keys)
└── commands/            # Plugin modules
    ├── __init__.py      # Registry (36 lines)
    ├── basic.py         # /help, /status, /cost, /model, /cancel
    ├── filesystem.py    # /pwd, /cd, /ls
    ├── settings.py      # /settings
    ├── update.py        # /update_bot
    ├── total_tokens.py  # /total_tokens
    ├── skills.py        # /builtin, /skills
    └── session_cmd.py   # /session, /clear
```

---

## v1.x — Single File Architecture

### v1.4 — Profile Photo Auto-Sync (2026-02-23)
- Bot profile photo auto-set on setup and `/update_bot`
- SHA-256 hash comparison to skip unchanged photos
- Logo update independent of code changes

### v1.3 — Multi-PC Token Aggregation (2026-02-23)
- `/total_tokens` command with inline keyboard UI
- Remote bot connection via token
- Aggregate usage across PCs
- Data stored in bot description (`language_code='zu'`)

### v1.2 — Long Message Fix (2026-02-22)
- `MAX_PARTS` increased from 5 to 20

### v1.1 — Token Tracking Fix (2026-02-22)
- Monthly token tracking via JSONL scanning
- Cross-platform path handling (Windows APPDATA)

### v1.0 — Initial Release
- Telegram ↔ Claude Code CLI bridge
- Session management, model switching
- File & image analysis
- Real-time status, cost tracking
- Auto-start service registration
- Korean and English versions
