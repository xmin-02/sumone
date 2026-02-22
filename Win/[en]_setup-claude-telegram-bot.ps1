# ============================================================================
# Claude Code Telegram Bot - Windows Setup Script (PowerShell)
# Installs a bot that lets you use Claude Code remotely via Telegram.
#
# Supported OS: Windows 10/11 (PowerShell 5.1+)
# Requirements: Python 3.8+, Claude Code CLI (claude), Node.js
# Usage: powershell -ExecutionPolicy Bypass -File setup-claude-telegram-bot.ps1
# ============================================================================

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Colors / Helpers
# ---------------------------------------------------------------------------
function Write-Info  { Write-Host "[INFO] " -ForegroundColor Cyan -NoNewline; Write-Host $args }
function Write-Ok    { Write-Host "[OK] " -ForegroundColor Green -NoNewline; Write-Host $args }
function Write-Warn  { Write-Host "[WARN] " -ForegroundColor Yellow -NoNewline; Write-Host $args }
function Write-Err   { Write-Host "[ERROR] " -ForegroundColor Red -NoNewline; Write-Host $args }

# ---------------------------------------------------------------------------
# Prerequisites check
# ---------------------------------------------------------------------------
function Test-Prerequisites {
    Write-Info "Checking required programs..."

    # Python 3
    $script:PYTHON = $null
    foreach ($cmd in @("python", "python3", "py")) {
        try {
            $ver = & $cmd --version 2>&1
            if ($ver -match "Python 3") {
                $script:PYTHON = $cmd
                break
            }
        } catch {}
    }
    if (-not $script:PYTHON) {
        Write-Err "Python 3 is not installed."
        Write-Host "  Download: https://python.org/downloads/"
        Write-Host "  Make sure to check 'Add Python to PATH' during installation!"
        exit 1
    }
    Write-Ok "Python: $(& $script:PYTHON --version 2>&1)"

    # Node.js (for Claude CLI)
    try {
        $nodeVer = & node --version 2>&1
        Write-Ok "Node.js: $nodeVer"
    } catch {
        Write-Warn "Node.js is not installed."
        Write-Host "  Download: https://nodejs.org/"
    }

    # Claude CLI
    $claudeFound = $false
    foreach ($cmd in @("claude", "claude.cmd")) {
        try {
            $null = & $cmd --version 2>&1
            $claudeFound = $true
            Write-Ok "Claude CLI: installed"
            break
        } catch {}
    }
    if (-not $claudeFound) {
        Write-Warn "Claude CLI is not installed."
        Write-Host ""
        Write-Host "How to install Claude CLI:"
        Write-Host "  npm install -g @anthropic-ai/claude-code"
        Write-Host ""
        $yn = Read-Host "Continue without Claude CLI? (y/N)"
        if ($yn -notmatch '^[yY]') {
            Write-Host "Please install it and run this script again."
            exit 0
        }
        Write-Warn "You will need to install Claude CLI later for the bot to work."
    }
}

# ---------------------------------------------------------------------------
# User input
# ---------------------------------------------------------------------------
function Get-UserInput {
    Write-Host ""
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor White
    Write-Host " Telegram Bot Configuration" -ForegroundColor White
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor White
    Write-Host ""
    Write-Host "1. Create a bot via @BotFather and get your token"
    Write-Host "   Telegram -> @BotFather -> /newbot -> copy token"
    Write-Host ""
    Write-Host "2. Find your Chat ID via @userinfobot"
    Write-Host "   Telegram -> @userinfobot -> /start -> copy ID"
    Write-Host ""

    # Bot Token
    do {
        $script:BOT_TOKEN = Read-Host "Bot Token"
        if ($script:BOT_TOKEN -match '^\d+:[A-Za-z0-9_-]+$') { break }
        Write-Err "Invalid token format. (Example: 123456789:ABCdef...)"
    } while ($true)

    # Chat ID
    do {
        $script:CHAT_ID = Read-Host "Chat ID"
        if ($script:CHAT_ID -match '^-?\d+$') { break }
        Write-Err "Invalid Chat ID. (Numbers only)"
    } while ($true)

    # Working directory
    $defaultDir = $env:USERPROFILE
    $input = Read-Host "Working directory [$defaultDir]"
    $script:WORK_DIR = if ($input) { $input } else { $defaultDir }
    if (-not (Test-Path $script:WORK_DIR -PathType Container)) {
        Write-Err "Directory does not exist: $($script:WORK_DIR)"
        exit 1
    }

    Write-Host ""
    Write-Info "Configuration summary:"
    Write-Host "  Bot Token: $($script:BOT_TOKEN.Substring(0, [Math]::Min(10, $script:BOT_TOKEN.Length)))..."
    Write-Host "  Chat ID:   $($script:CHAT_ID)"
    Write-Host "  Working directory: $($script:WORK_DIR)"
    Write-Host ""
    $confirm = Read-Host "Install with these settings? (Y/n)"
    if ($confirm -match '^[nN]') {
        Write-Host "Cancelled."
        exit 0
    }
}

# ---------------------------------------------------------------------------
# Install bot script
# ---------------------------------------------------------------------------
function Install-Bot {
    $script:INSTALL_DIR = Join-Path $env:USERPROFILE ".claude-telegram-bot"
    $script:BOT_PATH = Join-Path $script:INSTALL_DIR "telegram-bot.py"

    if (-not (Test-Path $script:INSTALL_DIR)) {
        New-Item -ItemType Directory -Path $script:INSTALL_DIR -Force | Out-Null
    }

    Write-Info "Installing bot script: $($script:BOT_PATH)"

    # Bot code (gzip + base64 encoded)
    $b64 = "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"

    # Decode: Base64 -> gzip decompress -> UTF-8 string
    $bytes = [System.Convert]::FromBase64String($b64)
    $ms = New-Object System.IO.MemoryStream(, $bytes)
    $gz = New-Object System.IO.Compression.GZipStream($ms, [System.IO.Compression.CompressionMode]::Decompress)
    $sr = New-Object System.IO.StreamReader($gz, [System.Text.Encoding]::UTF8)
    $botContent = $sr.ReadToEnd()
    $sr.Close(); $gz.Close(); $ms.Close()

    # Replace placeholders
    $botContent = $botContent -replace "%%BOT_TOKEN%%", $script:BOT_TOKEN
    $botContent = $botContent -replace "%%CHAT_ID%%", $script:CHAT_ID
    $botContent = $botContent -replace "%%WORK_DIR%%", $script:WORK_DIR

    # Write with UTF-8 (no BOM)
    [System.IO.File]::WriteAllText($script:BOT_PATH, $botContent, [System.Text.UTF8Encoding]::new($false))

    Write-Ok "Bot script installed: $($script:BOT_PATH)"
}

# ---------------------------------------------------------------------------
# Verify bot token
# ---------------------------------------------------------------------------
function Test-BotToken {
    Write-Info "Verifying bot token..."
    try {
        $url = "https://api.telegram.org/bot$($script:BOT_TOKEN)/getMe"
        $resp = Invoke-RestMethod -Uri $url -TimeoutSec 10 -ErrorAction Stop
        if ($resp.ok) {
            Write-Ok "Token verified - Bot: @$($resp.result.username)"
        } else {
            Write-Warn "Token verification failed. Please check your token."
        }
    } catch {
        Write-Warn "Token verification failed: $_"
    }
}

# ---------------------------------------------------------------------------
# Auto-start: Windows Task Scheduler
# ---------------------------------------------------------------------------
function Setup-AutoStart {
    Write-Info "Registering with Windows Task Scheduler..."

    $taskName = "ClaudeTelegramBot"

    # Remove existing task if any
    try {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    } catch {}

    # Find python full path
    $pythonPath = (Get-Command $script:PYTHON -ErrorAction SilentlyContinue).Source
    if (-not $pythonPath) { $pythonPath = $script:PYTHON }

    # Create scheduled task that runs at logon and restarts on failure
    $action = New-ScheduledTaskAction `
        -Execute $pythonPath `
        -Argument "`"$($script:BOT_PATH)`"" `
        -WorkingDirectory $script:WORK_DIR

    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Days 365)

    try {
        Register-ScheduledTask `
            -TaskName $taskName `
            -Action $action `
            -Trigger $trigger `
            -Settings $settings `
            -Description "Claude Code Telegram Bot" `
            -RunLevel Limited `
            -Force | Out-Null

        # Start now
        Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue

        Write-Ok "Task Scheduler registration complete (auto-starts at logon)"
        Write-Host ""
        Write-Host "  Check status:  Get-ScheduledTask -TaskName $taskName | Select State"
        Write-Host "  View logs:     Get-Content '$($script:INSTALL_DIR)\bot.log' -Tail 20"
        Write-Host "  Stop:          Stop-ScheduledTask -TaskName $taskName"
        Write-Host "  Restart:       Start-ScheduledTask -TaskName $taskName"
        Write-Host "  Run manually:  $script:PYTHON `"$($script:BOT_PATH)`""
    } catch {
        Write-Warn "Task Scheduler registration failed: $_"
        Write-Host ""
        Write-Host "  Run manually:"
        Write-Host "  $script:PYTHON `"$($script:BOT_PATH)`""
        Write-Host ""
        Write-Host "  Or add to startup programs:"
        Write-Host "  Win+R -> shell:startup -> create a shortcut"
    }
}

# ---------------------------------------------------------------------------
# Uninstall info
# ---------------------------------------------------------------------------
function Show-UninstallInfo {
    Write-Host ""
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor White
    Write-Host " How to Uninstall" -ForegroundColor White
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor White
    Write-Host "  Stop-ScheduledTask -TaskName ClaudeTelegramBot"
    Write-Host "  Unregister-ScheduledTask -TaskName ClaudeTelegramBot -Confirm:`$false"
    Write-Host "  Remove-Item -Recurse -Force '$($script:INSTALL_DIR)'"
    Write-Host ""
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
function Main {
    Write-Host ""
    Write-Host "╔═══════════════════════════════════════════╗" -ForegroundColor Cyan
    Write-Host "║  Claude Code Telegram Bot - Windows Setup ║" -ForegroundColor Cyan
    Write-Host "╚═══════════════════════════════════════════╝" -ForegroundColor Cyan
    Write-Host ""

    Test-Prerequisites
    Get-UserInput
    Install-Bot
    Test-BotToken
    Setup-AutoStart
    Show-UninstallInfo

    Write-Host "Installation complete!" -ForegroundColor Green
    Write-Host "Send /help in Telegram to get started."
    Write-Host ""
}

Main
