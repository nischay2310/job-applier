# One-attempt install + launch for Job Applier on Windows.
#
# What it does, in order:
#   1. Installs missing prerequisites via winget (Python 3.12, Git, Node.js, Google Chrome, uv)
#   2. Installs Python + frontend dependencies
#   3. Builds the dashboard UI and downloads the Playwright browser
#   4. Creates your .env and data files from the provided examples
#   5. Asks for your Gemini API key (only the first time)
#   6. Starts the dashboard and opens it in your browser
#
# Usage (PowerShell):
#   Right-click this file -> "Run with PowerShell"
#   or, from a PowerShell prompt in the project folder:
#     powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Say($msg) { Write-Host "`n[install] $msg" -ForegroundColor Cyan }
function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

Say "Checking prerequisites..."

if (-not (Have "winget")) {
    Say "winget was not found. Install 'App Installer' from the Microsoft Store, then re-run this script."
    exit 1
}

# --- git ---
if (-not (Have "git")) {
    Say "git not found. Installing..."
    winget install --id Git.Git -e --source winget --accept-source-agreements --accept-package-agreements
    $env:PATH = "$env:PATH;C:\Program Files\Git\cmd"
}

# --- Python 3.10+ ---
$PythonBin = $null
foreach ($candidate in @("python3.12", "python3.11", "python", "py")) {
    if (Have $candidate) {
        $ok = & $candidate -c "import sys; print(sys.version_info[:2] >= (3, 10))" 2>$null
        if ($ok -eq "True") { $PythonBin = $candidate; break }
    }
}
if (-not $PythonBin) {
    Say "Python 3.10+ not found. Installing Python 3.12..."
    winget install --id Python.Python.3.12 -e --source winget --accept-source-agreements --accept-package-agreements
    $env:PATH = "$env:PATH;$env:LOCALAPPDATA\Programs\Python\Python312;$env:LOCALAPPDATA\Programs\Python\Python312\Scripts"
    $PythonBin = "python"
}
Say "Using Python: $(& $PythonBin --version)"

# --- uv ---
if (-not (Have "uv")) {
    Say "uv not found. Installing..."
    powershell -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $env:PATH = "$env:PATH;$env:USERPROFILE\.local\bin"
}
Say "Using uv: $(uv --version)"

# --- Node.js 20+ / npm ---
$NodeOk = $false
if (Have "node") {
    $nodeMajor = [int]((node -v) -replace "v", "" -split "\.")[0]
    if ($nodeMajor -ge 20) { $NodeOk = $true }
}
if (-not $NodeOk) {
    Say "Node.js 20+ not found. Installing..."
    winget install --id OpenJS.NodeJS.LTS -e --source winget --accept-source-agreements --accept-package-agreements
    $env:PATH = "$env:PATH;$env:ProgramFiles\nodejs"
}
Say "Using Node: $(node -v)"

# --- Google Chrome ---
$ChromePath = "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"
$ChromePathX86 = "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
if (-not (Test-Path $ChromePath) -and -not (Test-Path $ChromePathX86)) {
    Say "Google Chrome not found. Installing..."
    winget install --id Google.Chrome -e --source winget --accept-source-agreements --accept-package-agreements
} else {
    Say "Google Chrome already installed."
}

# --- Python dependencies ---
Say "Installing Python dependencies (uv sync)..."
uv sync

# --- Frontend dependencies + production build ---
Say "Installing frontend dependencies (npm install)..."
Push-Location frontend
npm install
Say "Building the dashboard UI (this can take a minute)..."
npx --yes --package=node@22.12.0 node node_modules/@angular/cli/bin/ng.js build
Pop-Location

# --- Playwright browser (used as a fallback automation engine) ---
Say "Downloading the Playwright browser..."
uv run playwright install chromium

# --- Local data files from examples (never overwrites existing files) ---
if (-not (Test-Path "data\candidate_profile.json")) {
    Copy-Item "data\candidate_profile.example.json" "data\candidate_profile.json"
}
if (-not (Test-Path "data\master_resume.json")) {
    Copy-Item "data\master_resume.example.json" "data\master_resume.json"
}

# --- .env with your Gemini API key ---
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host ""
    Write-Host "Job Applier tailors resumes and cover letters with Google Gemini."
    Write-Host "Get a free API key at: https://aistudio.google.com/app/apikey"
    $GeminiKey = Read-Host "Paste your GOOGLE_API_KEY here (press Enter to add it later)"
    if ($GeminiKey) {
        (Get-Content ".env") -replace "^GOOGLE_API_KEY=.*", "GOOGLE_API_KEY=$GeminiKey" | Set-Content ".env"
    } else {
        Say "No key entered. Edit the .env file and set GOOGLE_API_KEY before using AI tailoring."
    }
}

Say "Setup complete. Starting the dashboard..."
& $PythonBin start.py
