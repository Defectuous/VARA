<#
.SYNOPSIS
    Start V.A.R.A.: Ollama, the backend, and the chat page.

.DESCRIPTION
    Uses the Ollama at VARA_OLLAMA_URL (a server such as defectserver, or this PC; only a local one is ever
    started), loads the model into GPU memory in the background,
    upgrades the Python packages (once a day, kept only if the tests pass), starts the V.A.R.A. backend
    (hidden; logs go to data\logs; it updates SearXNG and the Ollama models), starts the voice listener tray app if
    it has been built, and opens http://127.0.0.1:8000/.
    Anything already running is left alone, so it is safe to run more than once.

.EXAMPLE
    .\start-vara.ps1                   # start everything and open the chat page
    .\start-vara.ps1 -Stop             # stop the backend and the listener (Ollama keeps running)
    .\start-vara.ps1 -NoListener       # start without the voice listener
    .\start-vara.ps1 -InstallAtLogin   # start V.A.R.A. automatically when you sign in
    .\start-vara.ps1 -RemoveAtLogin    # undo that
#>
param(
    [switch]$NoBrowser,
    [switch]$NoListener,
    [switch]$Stop,
    [switch]$InstallAtLogin,
    [switch]$RemoveAtLogin
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$taskName = "V.A.R.A."
# Pick up VARA_* settings from .env (the backend reads it too); real environment variables win.
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile -Encoding UTF8) {
        if ($line -match '^\s*(VARA_[A-Z0-9_]+)\s*=\s*(.*?)\s*$') {
            $name = $Matches[1]
            $value = $Matches[2].Trim('"', "'")
            if (-not [Environment]::GetEnvironmentVariable($name)) { Set-Item "env:$name" $value }
        }
    }
}

$port = if ($env:VARA_PORT) { $env:VARA_PORT } else { 8000 }
$model = if ($env:VARA_OLLAMA_MODEL) { $env:VARA_OLLAMA_MODEL } else { "llama3.1:8b" }
$ollamaUrl = if ($env:VARA_OLLAMA_URL) { $env:VARA_OLLAMA_URL } else { "http://127.0.0.1:11434" }
$varaUrl = "http://127.0.0.1:$port"

function Test-Url([string]$url) {
    try { Invoke-RestMethod $url -TimeoutSec 2 | Out-Null; return $true } catch { return $false }
}

function Wait-Url([string]$url, [int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Url $url) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Get-BackendProcessId {
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($listener) { return $listener.OwningProcess }
}

if ($InstallAtLogin) {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $root `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\start-vara.ps1`" -NoBrowser"
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
        -Description "Start the V.A.R.A. backend at sign-in (localhost only)." -Force | Out-Null
    Write-Host "V.A.R.A. will start when you sign in. Open $varaUrl/ to chat."
    return
}

if ($RemoveAtLogin) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "V.A.R.A. will no longer start at sign-in."
    return
}

if ($Stop) {
    Get-Process VaraListener -ErrorAction SilentlyContinue | Stop-Process -Confirm:$false
    $id = Get-BackendProcessId
    if ($id) {
        Stop-Process -Id $id -Confirm:$false
        Write-Host "V.A.R.A. backend stopped."
    } else {
        Write-Host "V.A.R.A. backend is not running."
    }
    return
}

# 1. Ollama
$ollamaHost = ([Uri]$ollamaUrl).Host
$ollamaIsLocal = $ollamaHost -in @("127.0.0.1", "localhost", "::1", "[::1]")
if (Test-Url "$ollamaUrl/api/tags") {
    Write-Host "Ollama is running at $ollamaUrl."
} elseif (-not $ollamaIsLocal) {
    Write-Warning "Can't reach Ollama at $ollamaUrl. Check that the server is on and Ollama listens on the network (OLLAMA_HOST=0.0.0.0)."
} else {
    $ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
    if (-not $ollama) { $ollama = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" }
    if (-not (Test-Path $ollama)) {
        Write-Warning "Ollama is not installed, so only cloud models (if configured) can answer. Get it at https://ollama.com"
    } else {
        Write-Host "Starting Ollama..."
        Start-Process $ollama -ArgumentList "serve" -WindowStyle Hidden
        if (-not (Wait-Url "$ollamaUrl/api/tags" 30)) { Write-Warning "Ollama did not start within 30 seconds." }
    }
}

# 2. Load the model into GPU memory now, so the first reply doesn't wait for it (about a minute).
if (Test-Url "$ollamaUrl/api/tags") {
    $warm = Join-Path $env:TEMP "vara-warm.json"
    @{ model = $model; keep_alive = "2h" } | ConvertTo-Json -Compress | Set-Content $warm -Encoding ascii
    Start-Process curl.exe -ArgumentList "-s", "-o", "NUL", "-X", "POST", "$ollamaUrl/api/generate", "--data-binary", "@$warm" `
        -WindowStyle Hidden
    Write-Host "Loading $model in the background..."
}

# 3. Python packages: once a day, before the backend starts (Windows locks the files of a running one).
#    An upgrade is kept only if the tests and lint pass; otherwise the previous uv.lock is restored.
#    SearXNG and the Ollama models are updated by the backend itself, at startup and daily.
function Update-Packages([string]$uv) {
    $statusFile = Join-Path $root "data\modules\packages-update.json"
    if (Test-Path $statusFile) {
        try {
            $last = [datetime](Get-Content $statusFile -Raw | ConvertFrom-Json).checked_at
            if (((Get-Date) - $last).TotalHours -lt 24) { return }
        } catch {}
    }
    $lock = Join-Path $root "uv.lock"
    $backup = Join-Path $env:TEMP "vara-uv.lock.bak"
    Copy-Item $lock $backup -Force
    $before = (Get-FileHash $lock).Hash
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"   # uv writes progress to stderr
    try {
        Push-Location $root
        & $uv lock --upgrade -q 2>$null
        if ($LASTEXITCODE -ne 0) {
            $result = "failed: couldn't check for upgrades (offline?)"
        } elseif ((Get-FileHash $lock).Hash -eq $before) {
            $result = "up to date"
        } else {
            Write-Host "Python package upgrades found; testing them..."
            & $uv sync -q 2>$null
            $ok = $LASTEXITCODE -eq 0
            if ($ok) { & $uv run -q pytest -q *> $null; $ok = $LASTEXITCODE -eq 0 }
            if ($ok) { & $uv run -q ruff check . *> $null; $ok = $LASTEXITCODE -eq 0 }
            if ($ok) {
                $result = "updated"
            } else {
                Copy-Item $backup $lock -Force
                & $uv sync -q 2>$null
                $result = "rolled back: tests or lint failed with the upgrades"
            }
        }
    } finally {
        Pop-Location
        $ErrorActionPreference = $previousPreference
    }
    New-Item -ItemType Directory -Force (Split-Path $statusFile) | Out-Null
    @{ checked_at = (Get-Date).ToString("o"); result = $result } | ConvertTo-Json |
        Set-Content $statusFile -Encoding utf8
    Write-Host "Python packages: $result."
}

# 4. Backend
if (Test-Url "$varaUrl/health") {
    Write-Host "V.A.R.A. backend is already running."
} else {
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if (-not $uv) { throw "uv is not on PATH. Install it from https://docs.astral.sh/uv/" }
    Update-Packages $uv
    $logs = Join-Path $root "data\logs"
    New-Item -ItemType Directory -Force $logs | Out-Null
    Write-Host "Starting V.A.R.A. backend (logs in data\logs)..."
    Start-Process $uv -ArgumentList "run", "python", "-m", "vara" -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs "vara.out.log") -RedirectStandardError (Join-Path $logs "vara.log")
    if (-not (Wait-Url "$varaUrl/health" 60)) {
        throw "The backend did not start within 60 seconds. See $logs\vara.log"
    }
}

# 5. Voice listener (the tray app), if it has been built
$listener = Join-Path $root "clients\windows\publish\VaraListener.exe"
if ($NoListener) {
    # skipped on request
} elseif (Get-Process VaraListener -ErrorAction SilentlyContinue) {
    Write-Host "Voice listener is already running."
} elseif (Test-Path $listener) {
    Start-Process $listener
    Write-Host "Voice listener started (look for the V.A.R.A. icon in the system tray)."
} else {
    Write-Host "Voice listener not built yet; see README (Voice listener)."
}

Write-Host "V.A.R.A. is ready at $varaUrl/"
if (-not $NoBrowser) { Start-Process "$varaUrl/" }
