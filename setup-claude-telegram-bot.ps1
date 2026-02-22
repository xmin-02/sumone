# ============================================================================
# Claude Code Telegram Bot - Windows Setup Script (PowerShell)
# 텔레그램에서 Claude Code를 원격으로 사용할 수 있는 봇을 설치합니다.
#
# 지원 OS: Windows 10/11 (PowerShell 5.1+)
# 필요 조건: Python 3.8+, Claude Code CLI (claude), Node.js
# 사용법: powershell -ExecutionPolicy Bypass -File setup-claude-telegram-bot.ps1
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
    Write-Info "필수 프로그램 확인 중..."

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
        Write-Err "Python 3이 설치되어 있지 않습니다."
        Write-Host "  다운로드: https://python.org/downloads/"
        Write-Host "  설치 시 'Add Python to PATH' 체크 필수!"
        exit 1
    }
    Write-Ok "Python: $(& $script:PYTHON --version 2>&1)"

    # Node.js (for Claude CLI)
    try {
        $nodeVer = & node --version 2>&1
        Write-Ok "Node.js: $nodeVer"
    } catch {
        Write-Warn "Node.js가 설치되어 있지 않습니다."
        Write-Host "  다운로드: https://nodejs.org/"
    }

    # Claude CLI
    $claudeFound = $false
    foreach ($cmd in @("claude", "claude.cmd")) {
        try {
            $null = & $cmd --version 2>&1
            $claudeFound = $true
            Write-Ok "Claude CLI: 설치됨"
            break
        } catch {}
    }
    if (-not $claudeFound) {
        Write-Warn "Claude CLI가 설치되어 있지 않습니다."
        Write-Host ""
        Write-Host "Claude CLI 설치 방법:"
        Write-Host "  npm install -g @anthropic-ai/claude-code"
        Write-Host ""
        $yn = Read-Host "Claude CLI 없이 계속 진행할까요? (y/N)"
        if ($yn -notmatch '^[yY]') {
            Write-Host "설치 후 다시 실행하세요."
            exit 0
        }
        Write-Warn "나중에 Claude CLI를 설치해야 봇이 작동합니다."
    }
}

# ---------------------------------------------------------------------------
# User input
# ---------------------------------------------------------------------------
function Get-UserInput {
    Write-Host ""
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor White
    Write-Host " Telegram Bot 설정" -ForegroundColor White
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor White
    Write-Host ""
    Write-Host "1. @BotFather에서 봇을 만들고 토큰을 받으세요"
    Write-Host "   Telegram -> @BotFather -> /newbot -> 토큰 복사"
    Write-Host ""
    Write-Host "2. @userinfobot에서 Chat ID를 확인하세요"
    Write-Host "   Telegram -> @userinfobot -> /start -> ID 복사"
    Write-Host ""

    # Bot Token
    do {
        $script:BOT_TOKEN = Read-Host "Bot Token"
        if ($script:BOT_TOKEN -match '^\d+:[A-Za-z0-9_-]+$') { break }
        Write-Err "유효하지 않은 토큰 형식입니다. (예: 123456789:ABCdef...)"
    } while ($true)

    # Chat ID
    do {
        $script:CHAT_ID = Read-Host "Chat ID"
        if ($script:CHAT_ID -match '^-?\d+$') { break }
        Write-Err "유효하지 않은 Chat ID입니다. (숫자만 입력)"
    } while ($true)

    # Working directory
    $defaultDir = $env:USERPROFILE
    $input = Read-Host "작업 디렉토리 [$defaultDir]"
    $script:WORK_DIR = if ($input) { $input } else { $defaultDir }
    if (-not (Test-Path $script:WORK_DIR -PathType Container)) {
        Write-Err "디렉토리가 존재하지 않습니다: $($script:WORK_DIR)"
        exit 1
    }

    Write-Host ""
    Write-Info "설정 확인:"
    Write-Host "  Bot Token: $($script:BOT_TOKEN.Substring(0, [Math]::Min(10, $script:BOT_TOKEN.Length)))..."
    Write-Host "  Chat ID:   $($script:CHAT_ID)"
    Write-Host "  작업 디렉토리: $($script:WORK_DIR)"
    Write-Host ""
    $confirm = Read-Host "이 설정으로 설치할까요? (Y/n)"
    if ($confirm -match '^[nN]') {
        Write-Host "취소됨."
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

    Write-Info "봇 스크립트 설치 중: $($script:BOT_PATH)"

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

    Write-Ok "봇 스크립트 설치 완료: $($script:BOT_PATH)"
}

# ---------------------------------------------------------------------------
# Verify bot token
# ---------------------------------------------------------------------------
function Test-BotToken {
    Write-Info "봇 토큰 검증 중..."
    try {
        $url = "https://api.telegram.org/bot$($script:BOT_TOKEN)/getMe"
        $resp = Invoke-RestMethod -Uri $url -TimeoutSec 10 -ErrorAction Stop
        if ($resp.ok) {
            Write-Ok "토큰 검증 성공 - Bot: @$($resp.result.username)"
        } else {
            Write-Warn "토큰 검증 실패. 토큰을 다시 확인하세요."
        }
    } catch {
        Write-Warn "토큰 검증 실패: $_"
    }
}

# ---------------------------------------------------------------------------
# Auto-start: Windows Task Scheduler
# ---------------------------------------------------------------------------
function Setup-AutoStart {
    Write-Info "Windows 작업 스케줄러에 등록 중..."

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

        Write-Ok "작업 스케줄러 등록 완료 (로그온 시 자동 시작)"
        Write-Host ""
        Write-Host "  상태 확인:  Get-ScheduledTask -TaskName $taskName | Select State"
        Write-Host "  로그 확인:  Get-Content '$($script:INSTALL_DIR)\bot.log' -Tail 20"
        Write-Host "  중지:       Stop-ScheduledTask -TaskName $taskName"
        Write-Host "  재시작:     Start-ScheduledTask -TaskName $taskName"
        Write-Host "  수동 실행:  $script:PYTHON `"$($script:BOT_PATH)`""
    } catch {
        Write-Warn "작업 스케줄러 등록 실패: $_"
        Write-Host ""
        Write-Host "  수동으로 실행하세요:"
        Write-Host "  $script:PYTHON `"$($script:BOT_PATH)`""
        Write-Host ""
        Write-Host "  또는 시작 프로그램에 추가:"
        Write-Host "  Win+R -> shell:startup -> 바로가기 만들기"
    }
}

# ---------------------------------------------------------------------------
# Uninstall info
# ---------------------------------------------------------------------------
function Show-UninstallInfo {
    Write-Host ""
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor White
    Write-Host " 제거 방법" -ForegroundColor White
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

    Write-Host "설치 완료!" -ForegroundColor Green
    Write-Host "텔레그램에서 /help 를 보내서 확인하세요."
    Write-Host ""
}

Main
