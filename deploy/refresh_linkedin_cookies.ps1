# Hourly LinkedIn cookie sync for job-applier.
#
# Reads whatever cookies are currently valid in the user's real Chrome
# profile (via Chrome DevTools Protocol - no new requests to LinkedIn,
# purely local state) and, only if a valid li_at session token is present,
# pushes them into the job-applier container as plaintext JSON.
#
# This can only sync an already-valid session. If LinkedIn has logged the
# user out (e.g. after suspicious-activity detection), this script cannot
# and will not attempt to log back in - that always requires the user's
# own action. It exits quietly in that case.

$ErrorActionPreference = "Stop"
$logPrefix = "[refresh-linkedin-cookies]"

function Write-Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "$ts $logPrefix $msg"
}

$cdpPort = 9222
$chromeUserData = Join-Path $env:LOCALAPPDATA "Google\Chrome\User Data"
$chromePath = Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"
$plaintextCookiesPath = Join-Path $env:TEMP "linkedin_cookies_plaintext.json"
$extractScript = Join-Path $env:TEMP "extract_cookies_via_cdp.py"
$containerName = "job-applier-app-1"

$cdpUp = $false
try {
    $null = Invoke-RestMethod -Uri "http://localhost:$cdpPort/json/version" -TimeoutSec 3
    $cdpUp = $true
} catch {
    $cdpUp = $false
}

if (-not $cdpUp) {
    Write-Log "No debug Chrome running, launching one against the existing profile."
    Start-Process -FilePath $chromePath -ArgumentList `
        "--remote-debugging-port=$cdpPort", `
        "--user-data-dir=$chromeUserData", `
        "--profile-directory=Default", `
        "--no-startup-window" `
        -WindowStyle Minimized
    Start-Sleep -Seconds 4
}

if (-not (Test-Path $extractScript)) {
    Write-Log "Extraction script missing, skipping this run."
    exit 0
}

$output = python $extractScript 2>&1 | Out-String
$outputOneLine = $output.Replace("`r", "").Replace("`n", " | ")
Write-Log "Extraction result: $outputOneLine"

if (-not (Test-Path $plaintextCookiesPath)) {
    Write-Log "No cookies file produced, skipping."
    exit 0
}

$hasLiAt = $output -match "Has li_at: True"
if (-not $hasLiAt) {
    Write-Log "Session is not currently valid, nothing to sync. User needs to log in again for this to resume."
    exit 0
}

$containerRunning = docker ps --filter "name=$containerName" --format "{{.Names}}" 2>$null
if (-not $containerRunning) {
    Write-Log "Container is not running, skipping."
    exit 0
}

docker exec $containerName mkdir -p /app/data | Out-Null
try {
    docker cp $plaintextCookiesPath "${containerName}:/app/data/linkedin_cookies.json" | Out-Null
    Write-Log "Synced valid LinkedIn session cookies into the container."
} finally {
    # The session cookies are secrets: never leave them lying in Temp.
    Remove-Item $plaintextCookiesPath -Force -ErrorAction SilentlyContinue
}
