# ============================================================================
# Claude Code Telegram Bot - Windows Setup Script (PowerShell)
# Downloads bot from GitHub, configures, and sets up auto-start.
# Usage: powershell -ExecutionPolicy Bypass -File setup.ps1
# ============================================================================
$ErrorActionPreference = "Stop"

# --- UTF-8 Console Encoding ---
try {
    [Console]::InputEncoding = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
    chcp 65001 | Out-Null
} catch {}

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
    Write-Host "========================================" -ForegroundColor White
    Write-Host " Telegram Bot Setup" -ForegroundColor White
    Write-Host "========================================" -ForegroundColor White
    Write-Host ""
    if ($script:LANG -eq "ko") {
        Write-Host "1. @BotFather -> /newbot -> 토큰 복사"
        Write-Host "2. 토큰 입력 후 봇에게 메시지를 보내면 Chat ID 자동 감지"
    } else {
        Write-Host "1. @BotFather -> /newbot -> Copy token"
        Write-Host "2. After entering token, send a message to your bot for auto Chat ID detection"
    }
    Write-Host ""

    do {
        $script:BOT_TOKEN = Read-Host "Bot Token"
        if ($script:BOT_TOKEN -match '^\d+:[A-Za-z0-9_-]+$') { break }
        Write-Err "Invalid token format"
    } while ($true)

    # --- Auto-detect Chat ID via getUpdates polling ---
    # Flush existing messages
    try {
        & $script:PYTHON -c @"
import urllib.request
try: urllib.request.urlopen('https://api.telegram.org/bot$($script:BOT_TOKEN)/getUpdates?offset=-1&limit=1', timeout=5)
except Exception: pass
"@ 2>$null
    } catch {}

    Write-Host ""
    if ($script:LANG -eq "ko") {
        Write-Info "Telegram에서 봇에게 아무 메시지를 보내주세요... (60초 대기)"
    } else {
        Write-Info "Send any message to your bot in Telegram... (waiting 60s)"
    }

    $detectedId = $null
    try {
        $detectedId = & $script:PYTHON -c @"
import urllib.request, json, time
token = '$($script:BOT_TOKEN)'
deadline = time.time() + 60
while time.time() < deadline:
    try:
        r = urllib.request.urlopen(f'https://api.telegram.org/bot{token}/getUpdates?timeout=5&limit=1', timeout=10)
        data = json.loads(r.read())
        if data.get('ok') and data.get('result'):
            msg = data['result'][0]
            chat_id = msg.get('message', {}).get('chat', {}).get('id')
            if chat_id:
                update_id = msg.get('update_id', 0)
                urllib.request.urlopen(f'https://api.telegram.org/bot{token}/getUpdates?offset={update_id+1}&limit=1', timeout=5)
                print(chat_id)
                break
    except Exception:
        pass
    time.sleep(2)
"@ 2>$null
    } catch {}

    if ($detectedId -and $detectedId -match '^-?\d+$') {
        $script:CHAT_ID = $detectedId.Trim()
        Write-Ok "Chat ID detected: $($script:CHAT_ID)"
    } else {
        if ($script:LANG -eq "ko") {
            Write-Warn "자동 감지 실패. 수동으로 입력해주세요."
        } else {
            Write-Warn "Auto-detection failed. Please enter manually."
        }
        do {
            $script:CHAT_ID = Read-Host "Chat ID"
            if ($script:CHAT_ID -match '^-?\d+$') { break }
            Write-Err "Invalid Chat ID"
        } while ($true)
    }

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
    $script:BOT_PATH = Join-Path $script:INSTALL_DIR "main.py"
    $script:CONFIG_PATH = Join-Path $script:INSTALL_DIR "config.json"

    foreach ($sub in @("", "i18n", "commands")) {
        $dir = if ($sub) { Join-Path $script:INSTALL_DIR $sub } else { $script:INSTALL_DIR }
        if (-not (Test-Path $dir)) {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }
    }

    Write-Info "Downloading bot from GitHub..."
    $files = @(
        @("bot/main.py",                    "main.py"),
        @("bot/config.py",                  "config.py"),
        @("bot/state.py",                   "state.py"),
        @("bot/telegram.py",                "telegram.py"),
        @("bot/claude.py",                  "claude.py"),
        @("bot/tokens.py",                  "tokens.py"),
        @("bot/sessions.py",                "sessions.py"),
        @("bot/downloader.py",              "downloader.py"),
        @("bot/i18n/__init__.py",           "i18n/__init__.py"),
        @("bot/i18n/ko.json",               "i18n/ko.json"),
        @("bot/i18n/en.json",               "i18n/en.json"),
        @("bot/commands/__init__.py",        "commands/__init__.py"),
        @("bot/commands/basic.py",           "commands/basic.py"),
        @("bot/commands/filesystem.py",      "commands/filesystem.py"),
        @("bot/commands/settings.py",        "commands/settings.py"),
        @("bot/commands/update.py",          "commands/update.py"),
        @("bot/commands/total_tokens.py",    "commands/total_tokens.py"),
        @("bot/commands/skills.py",          "commands/skills.py"),
        @("bot/commands/session_cmd.py",     "commands/session_cmd.py")
    )

    foreach ($entry in $files) {
        $src = $entry[0]; $dest = $entry[1]
        $url = "$GITHUB_RAW/$src"
        $outPath = Join-Path $script:INSTALL_DIR $dest
        try {
            Invoke-WebRequest -Uri $url -OutFile $outPath -ErrorAction Stop
        } catch {
            Write-Err "Download failed: $url ($_)"; exit 1
        }
    }
    Write-Ok "Bot downloaded: $($script:INSTALL_DIR) ($($files.Count) files)"

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

    # Install cloudflared for file viewer
    $cloudflaredPath = Join-Path $script:INSTALL_DIR "cloudflared.exe"
    if (-not (Test-Path $cloudflaredPath)) {
        Write-Info "Installing cloudflared for file viewer..."
        try {
            $cfUrl = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
            Invoke-WebRequest -Uri $cfUrl -OutFile $cloudflaredPath -ErrorAction Stop
            Write-Ok "cloudflared installed"
        } catch {
            Write-Warn "cloudflared install failed (file viewer will auto-install on first run): $_"
        }
    }
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

# --- Set Bot Profile Photo ---
function Set-BotPhoto {
    Write-Info "Setting bot profile photo..."
    $photoUrl = "$GITHUB_RAW/assets/logo.png"
    $photoPath = Join-Path $script:INSTALL_DIR "logo.png"

    try {
        Invoke-WebRequest -Uri $photoUrl -OutFile $photoPath -ErrorAction Stop
    } catch {
        Write-Warn "Logo download failed, skipping profile photo"
        return
    }

    try {
        & $script:PYTHON -c @"
import urllib.request, json, uuid
token = '$($script:BOT_TOKEN)'
photo_path = r'$photoPath'
boundary = uuid.uuid4().hex
with open(photo_path, 'rb') as f:
    photo_data = f.read()
photo_json = json.dumps({'type': 'static', 'photo': 'attach://photo_file'})
parts = []
parts.append(('--' + boundary + '\r\nContent-Disposition: form-data; name="photo"\r\n\r\n' + photo_json + '\r\n').encode())
parts.append(('--' + boundary + '\r\nContent-Disposition: form-data; name="photo_file"; filename="logo.png"\r\nContent-Type: image/png\r\n\r\n').encode() + photo_data + b'\r\n')
parts.append(('--' + boundary + '--\r\n').encode())
body = b''.join(parts)
req = urllib.request.Request('https://api.telegram.org/bot' + token + '/setMyProfilePhoto', data=body)
req.add_header('Content-Type', 'multipart/form-data; boundary=' + boundary)
resp = urllib.request.urlopen(req, timeout=30)
data = json.loads(resp.read())
if data.get('ok'): print('ok')
"@
        Write-Ok "Profile photo set"
    } catch {
        Write-Warn "Profile photo upload failed (non-critical)"
    }

    Remove-Item $photoPath -ErrorAction SilentlyContinue
}

# --- Grant read access to other users' Claude sessions ---
function Setup-TokenAccess {
    $currentUser = $env:USERNAME
    $found = @()

    # Scan C:\Users\* for .claude\projects (APPDATA path)
    foreach ($userDir in Get-ChildItem "C:\Users" -Directory -ErrorAction SilentlyContinue) {
        if ($userDir.Name -eq $currentUser) { continue }
        $appData = Join-Path $userDir.FullName "AppData\Roaming\claude\projects"
        if (-not (Test-Path $appData -PathType Container)) { continue }
        # Already readable?
        try { $null = Get-ChildItem $appData -ErrorAction Stop; continue } catch {}
        $found += $appData
    }

    if ($found.Count -eq 0) { return }

    Write-Host ""
    Write-Info "Found Claude sessions from other users:"
    foreach ($d in $found) { Write-Host "  $d" }
    Write-Host ""

    $yn = Read-Host "Include other users' tokens in aggregate? (Y/n)"
    if ($yn -match '^[nN]') { return }

    foreach ($appData in $found) {
        Write-Info "Granting read access: $appData"
        try {
            $acl = Get-Acl $appData
            $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
                $currentUser, "ReadAndExecute", "ContainerInherit,ObjectInherit", "None", "Allow")
            $acl.AddAccessRule($rule)
            Set-Acl -Path $appData -AclObject $acl

            # Parent dirs need traverse
            foreach ($parent in @((Split-Path $appData), (Split-Path (Split-Path $appData)))) {
                try {
                    $pacl = Get-Acl $parent
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
    Set-BotPhoto
    Setup-TokenAccess
    Setup-AutoStart
    Show-UninstallInfo

    Write-Host "Setup complete!" -ForegroundColor Green
    Write-Host "Send /help in Telegram to get started."
    Write-Host ""
}

Main
