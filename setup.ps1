# ============================================================================
# Claude Code Telegram Bot - Windows Setup Script (PowerShell)
# Downloads bot from GitHub, configures, and sets up auto-start.
# Usage: powershell -ExecutionPolicy Bypass -File setup.ps1
# ============================================================================
$ErrorActionPreference = "Stop"

function Write-Info  { Write-Host "[INFO] " -ForegroundColor Cyan -NoNewline; Write-Host $args }
function Write-Ok    { Write-Host "[OK] " -ForegroundColor Green -NoNewline; Write-Host $args }
function Write-Warn  { Write-Host "[WARN] " -ForegroundColor Yellow -NoNewline; Write-Host $args }
function Write-Err   { Write-Host "[ERROR] " -ForegroundColor Red -NoNewline; Write-Host $args }

$GITHUB_REPO = "xmin-02/Claude-telegram-bot"
$GITHUB_RAW = "https://raw.githubusercontent.com/$GITHUB_REPO/main"

# --- Prerequisites ---
function Test-Prerequisites {
    Write-Info "Checking prerequisites..."
    $script:PYTHON = $null
    foreach ($cmd in @("python", "python3", "py")) {
        try {
            $ver = & $cmd --version 2>&1
            if ($ver -match "Python 3") { $script:PYTHON = $cmd; break }
        } catch {}
    }
    if (-not $script:PYTHON) {
        Write-Err "Python 3 is not installed. Download: https://python.org/downloads/"
        exit 1
    }
    Write-Ok "Python: $(& $script:PYTHON --version 2>&1)"

    $claudeFound = $false
    foreach ($cmd in @("claude", "claude.cmd")) {
        try { $null = & $cmd --version 2>&1; $claudeFound = $true; break } catch {}
    }
    if ($claudeFound) { Write-Ok "Claude CLI: installed" }
    else {
        Write-Warn "Claude CLI not found. Install: npm install -g @anthropic-ai/claude-code"
        $yn = Read-Host "Continue without Claude CLI? (y/N)"
        if ($yn -notmatch '^[yY]') { exit 0 }
    }
}

# --- Language Selection ---
function Select-Language {
    Write-Host ""
    Write-Host "Select Language" -ForegroundColor White
    Write-Host "  1) Korean (한국어)"
    Write-Host "  2) English"
    Write-Host ""
    do {
        $choice = Read-Host "Choice (1-2)"
        switch ($choice) {
            "1" { $script:LANG = "ko"; return }
            "2" { $script:LANG = "en"; return }
            default { Write-Host "Enter 1 or 2" }
        }
    } while ($true)
}

# --- User Input ---
function Get-UserInput {
    Write-Host ""
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor White
    Write-Host " Telegram Bot Setup" -ForegroundColor White
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor White
    Write-Host ""
    Write-Host "1. @BotFather -> /newbot -> Copy token"
    Write-Host "2. @userinfobot -> /start -> Copy Chat ID"
    Write-Host ""

    do {
        $script:BOT_TOKEN = Read-Host "Bot Token"
        if ($script:BOT_TOKEN -match '^\d+:[A-Za-z0-9_-]+$') { break }
        Write-Err "Invalid token format"
    } while ($true)

    do {
        $script:CHAT_ID = Read-Host "Chat ID"
        if ($script:CHAT_ID -match '^-?\d+$') { break }
        Write-Err "Invalid Chat ID"
    } while ($true)

    $defaultDir = $env:USERPROFILE
    $input = Read-Host "Working directory [$defaultDir]"
    $script:WORK_DIR = if ($input) { $input } else { $defaultDir }
    if (-not (Test-Path $script:WORK_DIR -PathType Container)) {
        Write-Err "Directory not found: $($script:WORK_DIR)"; exit 1
    }

    Write-Host ""
    Write-Info "Settings:"
    Write-Host "  Token:     $($script:BOT_TOKEN.Substring(0, [Math]::Min(10, $script:BOT_TOKEN.Length)))..."
    Write-Host "  Chat ID:   $($script:CHAT_ID)"
    Write-Host "  Language:  $($script:LANG)"
    Write-Host "  Work Dir:  $($script:WORK_DIR)"
    $confirm = Read-Host "Proceed? (Y/n)"
    if ($confirm -match '^[nN]') { Write-Host "Cancelled."; exit 0 }
}

# --- Download & Install ---
function Install-Bot {
    $script:INSTALL_DIR = Join-Path $env:USERPROFILE ".claude-telegram-bot"
    $script:BOT_PATH = Join-Path $script:INSTALL_DIR "telegram-bot.py"
    $script:CONFIG_PATH = Join-Path $script:INSTALL_DIR "config.json"

    if (-not (Test-Path $script:INSTALL_DIR)) {
        New-Item -ItemType Directory -Path $script:INSTALL_DIR -Force | Out-Null
    }

    Write-Info "Downloading bot from GitHub..."
    $botUrl = "$GITHUB_RAW/bot/telegram-bot-$($script:LANG).py"
    try {
        Invoke-WebRequest -Uri $botUrl -OutFile $script:BOT_PATH -ErrorAction Stop
        Write-Ok "Bot downloaded: $($script:BOT_PATH)"
    } catch {
        Write-Err "Download failed: $_"; exit 1
    }

    # Create config.json
    $config = @{
        bot_token = $script:BOT_TOKEN
        chat_id = $script:CHAT_ID
        work_dir = $script:WORK_DIR
        lang = $script:LANG
        github_repo = $GITHUB_REPO
    } | ConvertTo-Json
    [System.IO.File]::WriteAllText($script:CONFIG_PATH, $config, [System.Text.UTF8Encoding]::new($false))
    Write-Ok "Config saved: $($script:CONFIG_PATH)"
}

# --- Verify Token ---
function Test-BotToken {
    Write-Info "Verifying bot token..."
    try {
        $resp = Invoke-RestMethod -Uri "https://api.telegram.org/bot$($script:BOT_TOKEN)/getMe" -TimeoutSec 10
        if ($resp.ok) { Write-Ok "Token verified - Bot: @$($resp.result.username)" }
        else { Write-Warn "Token verification failed" }
    } catch { Write-Warn "Token verification failed: $_" }
}

# --- Auto-start ---
function Setup-AutoStart {
    Write-Info "Registering Windows Task Scheduler..."
    $taskName = "ClaudeTelegramBot"
    try { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue } catch {}

    $pythonPath = (Get-Command $script:PYTHON -ErrorAction SilentlyContinue).Source
    if (-not $pythonPath) { $pythonPath = $script:PYTHON }

    $action = New-ScheduledTaskAction -Execute $pythonPath `
        -Argument "`"$($script:BOT_PATH)`"" -WorkingDirectory $script:WORK_DIR
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Days 365)

    try {
        Register-ScheduledTask -TaskName $taskName -Action $action `
            -Trigger $trigger -Settings $settings `
            -Description "Claude Code Telegram Bot" -RunLevel Limited -Force | Out-Null
        Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Write-Ok "Task Scheduler registered (auto-start at logon)"
        Write-Host "  Status:  Get-ScheduledTask -TaskName $taskName | Select State"
        Write-Host "  Logs:    Get-Content '$($script:INSTALL_DIR)\bot.log' -Tail 20"
        Write-Host "  Stop:    Stop-ScheduledTask -TaskName $taskName"
        Write-Host "  Restart: Start-ScheduledTask -TaskName $taskName"
        Write-Host "  Manual:  $script:PYTHON `"$($script:BOT_PATH)`""
    } catch {
        Write-Warn "Task Scheduler failed: $_"
        Write-Host "  Run manually: $script:PYTHON `"$($script:BOT_PATH)`""
    }
}

# --- Uninstall ---
function Show-UninstallInfo {
    Write-Host ""
    Write-Host " Uninstall" -ForegroundColor White
    Write-Host "  Stop-ScheduledTask -TaskName ClaudeTelegramBot"
    Write-Host "  Unregister-ScheduledTask -TaskName ClaudeTelegramBot -Confirm:`$false"
    Write-Host "  Remove-Item -Recurse -Force '$($script:INSTALL_DIR)'"
    Write-Host ""
}

# --- Main ---
function Main {
    Write-Host ""
    Write-Host "=========================================" -ForegroundColor Cyan
    Write-Host "  Claude Code Telegram Bot - Setup" -ForegroundColor Cyan
    Write-Host "=========================================" -ForegroundColor Cyan
    Write-Host ""

    Test-Prerequisites
    Select-Language
    Get-UserInput
    Install-Bot
    Test-BotToken
    Setup-AutoStart
    Show-UninstallInfo

    Write-Host "Setup complete!" -ForegroundColor Green
    Write-Host "Send /help in Telegram to get started."
    Write-Host ""
}

Main
