# ============================================================================
# sumone - Claude · Codex · Gemini Telegram Bot
# Setup Script (Windows / PowerShell)
# Usage: powershell -ExecutionPolicy Bypass -File setup.ps1
# ============================================================================
$ErrorActionPreference = "Stop"

# --- UTF-8 Console Encoding ---
try {
    [Console]::InputEncoding  = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding           = [System.Text.Encoding]::UTF8
    chcp 65001 | Out-Null
} catch {}

function Write-Info  { Write-Host "  " -NoNewline; Write-Host "[INFO]" -ForegroundColor Cyan   -NoNewline; Write-Host " $args" }
function Write-Ok    { Write-Host "  " -NoNewline; Write-Host "[ OK ]" -ForegroundColor Green  -NoNewline; Write-Host " $args" }
function Write-Warn  { Write-Host "  " -NoNewline; Write-Host "[WARN]" -ForegroundColor Yellow -NoNewline; Write-Host " $args" }
function Write-Err   { Write-Host "  " -NoNewline; Write-Host "[ERR ]" -ForegroundColor Red    -NoNewline; Write-Host " $args" }

$GITHUB_REPO = "xmin-02/sumone"
$GITHUB_RAW  = "https://raw.githubusercontent.com/$GITHUB_REPO/main"
$INSTALL_DIR = Join-Path $env:USERPROFILE ".claude-telegram-bot"
$BOT_PATH    = Join-Path $INSTALL_DIR "main.py"

# ── Banner ──────────────────────────────────────────────────────────────────
function Print-Banner {
    Clear-Host
    Write-Host ""
    Write-Host "  +======================================================+" -ForegroundColor Cyan
    Write-Host "  |                                                      |" -ForegroundColor Cyan
    Write-Host "  |   ███████╗██╗   ██╗███╗   ███╗ ██████╗ ███╗   ██╗   |" -ForegroundColor Cyan
    Write-Host "  |   ██╔════╝██║   ██║████╗ ████║██╔═══██╗████╗  ██║   |" -ForegroundColor Cyan
    Write-Host "  |   ███████╗██║   ██║██╔████╔██║██║   ██║██╔██╗ ██║   |" -ForegroundColor Cyan
    Write-Host "  |   ╚════██║██║   ██║██║╚██╔╝██║██║   ██║██║╚██╗██║   |" -ForegroundColor Cyan
    Write-Host "  |   ███████║╚██████╔╝██║ ╚═╝ ██║╚██████╔╝██║ ╚████║   |" -ForegroundColor Cyan
    Write-Host "  |   ╚══════╝ ╚═════╝ ╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═══╝   |" -ForegroundColor Cyan
    Write-Host "  |                                                      |" -ForegroundColor Cyan
    Write-Host "  |        Claude · Codex · Gemini Telegram Bot         |" -ForegroundColor DarkGray
    Write-Host "  +======================================================+" -ForegroundColor Cyan
    Write-Host ""
}

# ── [1/4] System Check ───────────────────────────────────────────────────────
function Check-Python {
    Print-Banner
    Write-Host "  [1/4] System Check`n" -ForegroundColor White

    $script:PYTHON = $null
    foreach ($cmd in @("python", "python3", "py")) {
        try {
            $ver = & $cmd --version 2>&1
            if ($ver -match "Python 3") { $script:PYTHON = $cmd; break }
        } catch {}
    }
    if (-not $script:PYTHON) {
        Write-Err "Python 3 not found. Download: https://python.org/downloads/"
        exit 1
    }
    Write-Ok "Python: $(& $script:PYTHON --version 2>&1)"
}

# ── [2/4] Download ───────────────────────────────────────────────────────────
function Download-Bot {
    Print-Banner
    Write-Host "  [2/4] Downloading bot files...`n" -ForegroundColor White

    foreach ($sub in @("", "i18n", "commands", "ai")) {
        $dir = if ($sub) { Join-Path $INSTALL_DIR $sub } else { $INSTALL_DIR }
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    }

    $files = @(
        @("bot/main.py",                 "main.py"),
        @("bot/config.py",               "config.py"),
        @("bot/state.py",                "state.py"),
        @("bot/telegram.py",             "telegram.py"),
        @("bot/tokens.py",               "tokens.py"),
        @("bot/sessions.py",             "sessions.py"),
        @("bot/downloader.py",           "downloader.py"),
        @("bot/fileviewer.py",           "fileviewer.py"),
        @("bot/onboard.py",              "onboard.py"),
        @("bot/ai/__init__.py",          "ai/__init__.py"),
        @("bot/ai/claude.py",            "ai/claude.py"),
        @("bot/ai/codex.py",             "ai/codex.py"),
        @("bot/ai/gemini.py",            "ai/gemini.py"),
        @("bot/i18n/__init__.py",        "i18n/__init__.py"),
        @("bot/i18n/ko.json",            "i18n/ko.json"),
        @("bot/i18n/en.json",            "i18n/en.json"),
        @("bot/commands/__init__.py",    "commands/__init__.py"),
        @("bot/commands/basic.py",       "commands/basic.py"),
        @("bot/commands/filesystem.py",  "commands/filesystem.py"),
        @("bot/commands/settings.py",    "commands/settings.py"),
        @("bot/commands/update.py",      "commands/update.py"),
        @("bot/commands/total_tokens.py","commands/total_tokens.py"),
        @("bot/commands/skills.py",      "commands/skills.py"),
        @("bot/commands/session_cmd.py", "commands/session_cmd.py")
    )

    $total = $files.Count
    $i = 0
    foreach ($entry in $files) {
        $src  = $entry[0]; $dest = $entry[1]
        $url  = "$GITHUB_RAW/$src"
        $out  = Join-Path $INSTALL_DIR $dest
        $i++
        Write-Host "`r  " -NoNewline
        Write-Host "[$i/$total]" -ForegroundColor Cyan -NoNewline
        Write-Host " $dest" -NoNewline
        try {
            Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing -ErrorAction Stop
        } catch {
            Write-Host ""
            Write-Err "Download failed: $url"
            exit 1
        }
    }
    Write-Host ""
    Write-Ok "Downloaded $total files -> $INSTALL_DIR"
}

# ── cloudflared ───────────────────────────────────────────────────────────────
function Install-Cloudflared {
    $cfExe = Join-Path $INSTALL_DIR "cloudflared.exe"
    $cfCmd = Get-Command "cloudflared" -ErrorAction SilentlyContinue
    if ($cfCmd -or (Test-Path $cfExe)) {
        Write-Ok "cloudflared: already installed"
        return
    }
    Write-Info "Installing cloudflared (file viewer)..."
    try {
        $cfUrl = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
        Invoke-WebRequest -Uri $cfUrl -OutFile $cfExe -UseBasicParsing -ErrorAction Stop
        Write-Ok "cloudflared installed"
    } catch {
        Write-Warn "cloudflared install failed (will retry on first run)"
    }
}

# ── [3/4] Onboarding ─────────────────────────────────────────────────────────
function Run-Onboarding {
    & $script:PYTHON (Join-Path $INSTALL_DIR "onboard.py")
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "Onboarding exited early — run '$($script:PYTHON) $(Join-Path $INSTALL_DIR "onboard.py")' to reconfigure."
        exit 1
    }
}

# ── sumone command ────────────────────────────────────────────────────────────
function Register-SumoneCommand {
    $pythonPath = (Get-Command $script:PYTHON -ErrorAction SilentlyContinue).Source
    if (-not $pythonPath) { $pythonPath = $script:PYTHON }

    $batContent = "@echo off`r`n`"$pythonPath`" `"$BOT_PATH`" %*"
    $batPath    = Join-Path $INSTALL_DIR "sumone.bat"
    [System.IO.File]::WriteAllText($batPath, $batContent, [System.Text.Encoding]::ASCII)

    $currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($currentPath -notlike "*$INSTALL_DIR*") {
        [Environment]::SetEnvironmentVariable("Path", "$currentPath;$INSTALL_DIR", "User")
        Write-Ok "'sumone' command registered (restart terminal to use)"
    } else {
        Write-Ok "'sumone' command registered"
    }
}

# ── Grant token access (Windows multi-user) ───────────────────────────────────
function Setup-TokenAccess {
    $currentUser = $env:USERNAME
    $found = @()
    foreach ($userDir in Get-ChildItem "C:\Users" -Directory -ErrorAction SilentlyContinue) {
        if ($userDir.Name -eq $currentUser) { continue }
        $appData = Join-Path $userDir.FullName "AppData\Roaming\claude\projects"
        if (-not (Test-Path $appData -PathType Container)) { continue }
        try { $null = Get-ChildItem $appData -ErrorAction Stop; continue } catch {}
        $found += $appData
    }
    if ($found.Count -eq 0) { return }

    Write-Host ""
    Write-Info "Found Claude sessions from other users:"
    foreach ($d in $found) { Write-Host "  $d" }
    $yn = Read-Host "  Include in token aggregate? (Y/n)"
    if ($yn -match '^[nN]') { return }

    foreach ($appData in $found) {
        try {
            $acl  = Get-Acl $appData
            $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
                $currentUser, "ReadAndExecute", "ContainerInherit,ObjectInherit", "None", "Allow")
            $acl.AddAccessRule($rule)
            Set-Acl -Path $appData -AclObject $acl
            foreach ($parent in @((Split-Path $appData), (Split-Path (Split-Path $appData)))) {
                try {
                    $pacl  = Get-Acl $parent
                    $prule = New-Object System.Security.AccessControl.FileSystemAccessRule(
                        $currentUser, "ReadAndExecute", "None", "None", "Allow")
                    $pacl.AddAccessRule($prule)
                    Set-Acl -Path $parent -AclObject $pacl
                } catch {}
            }
            Write-Ok "Access granted: $appData"
        } catch {
            Write-Warn "Failed: $appData ($_)"
        }
    }
}

# ── [4/4] Auto-start ─────────────────────────────────────────────────────────
function Setup-AutoStart {
    Print-Banner
    Write-Host "  [4/4] Auto-start setup`n" -ForegroundColor White

    $taskName   = "ClaudeTelegramBot"
    $pythonPath = (Get-Command $script:PYTHON -ErrorAction SilentlyContinue).Source
    if (-not $pythonPath) { $pythonPath = $script:PYTHON }

    try { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue } catch {}

    $action   = New-ScheduledTaskAction -Execute $pythonPath -Argument "`"$BOT_PATH`""
    $trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Days 365)

    try {
        Register-ScheduledTask -TaskName $taskName -Action $action `
            -Trigger $trigger -Settings $settings `
            -Description "sumone Telegram Bot" -RunLevel Limited -Force | Out-Null
        Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Write-Ok "Task Scheduler registered (auto-start at logon)"
        Write-Host "  Status:  Get-ScheduledTask -TaskName $taskName | Select State" -ForegroundColor DarkGray
        Write-Host "  Logs:    Get-Content '$INSTALL_DIR\bot.log' -Tail 20"          -ForegroundColor DarkGray
    } catch {
        Write-Warn "Task Scheduler failed: $_"
        Write-Host "  Run manually: $pythonPath `"$BOT_PATH`""
    }

    Write-Host ""
    Write-Host "  Uninstall:" -ForegroundColor DarkGray
    Write-Host "    Stop-ScheduledTask -TaskName ClaudeTelegramBot" -ForegroundColor DarkGray
    Write-Host "    Unregister-ScheduledTask -TaskName ClaudeTelegramBot -Confirm:`$false" -ForegroundColor DarkGray
    Write-Host "    Remove-Item -Recurse -Force '$INSTALL_DIR'" -ForegroundColor DarkGray
}

# ── Main ──────────────────────────────────────────────────────────────────────
function Main {
    Check-Python         # [1/4]
    Download-Bot         # [2/4]
    Install-Cloudflared

    Run-Onboarding       # [3/4] — interactive: AI, token, chat_id, workdir, prefs

    Register-SumoneCommand
    Setup-TokenAccess
    Setup-AutoStart      # [4/4]

    Print-Banner
    Write-Host "  Setup complete!" -ForegroundColor Green
    Write-Host "  Send /help in Telegram to get started.`n"
}

Main
