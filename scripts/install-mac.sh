#!/usr/bin/env bash
# One-attempt install + launch for Job Applier on macOS.
#
# What it does, in order:
#   1. Installs Homebrew if missing, then Python 3.12, uv, Node.js, Google Chrome
#   2. Installs Python + frontend dependencies
#   3. Builds the dashboard UI and downloads the Playwright browser
#   4. Creates your .env and data files from the provided examples
#   5. Asks for your Gemini API key (only the first time)
#   6. Starts the dashboard and opens it in your browser
#
# Usage:
#   chmod +x scripts/install-mac.sh
#   ./scripts/install-mac.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

say() { printf '\n\033[1;36m[install]\033[0m %s\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

say "Checking prerequisites..."

# --- Homebrew ---
if ! have brew; then
  say "Homebrew not found. Installing (you may be asked for your password)..."
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  if [ -d /opt/homebrew/bin ]; then eval "$(/opt/homebrew/bin/brew shellenv)"; fi
  if [ -d /usr/local/bin ]; then eval "$(/usr/local/bin/brew shellenv)"; fi
fi
say "Using Homebrew: $(brew --version | head -n1)"

# --- Python 3.10+ ---
PYTHON_BIN=""
for candidate in python3.12 python3.11 python3.10 python3; do
  if have "$candidate"; then
    ver=$("$candidate" -c 'import sys; print(sys.version_info[:2] >= (3, 10))' 2>/dev/null || echo False)
    if [ "$ver" = "True" ]; then PYTHON_BIN="$candidate"; break; fi
  fi
done
if [ -z "$PYTHON_BIN" ]; then
  say "Python 3.10+ not found. Installing..."
  brew install python@3.12
  PYTHON_BIN="python3.12"
fi
say "Using Python: $($PYTHON_BIN --version)"

# --- git ---
if ! have git; then
  say "git not found. Installing..."
  brew install git
fi

# --- uv ---
if ! have uv; then
  say "uv not found. Installing..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
say "Using uv: $(uv --version)"

# --- Node.js 20+ / npm ---
NODE_OK="false"
if have node; then
  node_major=$(node -v | sed 's/^v//' | cut -d. -f1)
  [ "$node_major" -ge 20 ] 2>/dev/null && NODE_OK="true"
fi
if [ "$NODE_OK" != "true" ]; then
  say "Node.js 20+ not found. Installing..."
  brew install node
fi
say "Using Node: $(node -v)"

# --- Google Chrome ---
if [ ! -d "/Applications/Google Chrome.app" ]; then
  say "Google Chrome not found. Installing..."
  brew install --cask google-chrome
else
  say "Google Chrome already installed."
fi

# --- Python dependencies ---
say "Installing Python dependencies (uv sync)..."
uv sync

# --- Frontend dependencies + production build ---
say "Installing frontend dependencies (npm install)..."
(cd frontend && npm install)
say "Building the dashboard UI (this can take a minute)..."
(cd frontend && npx --yes --package=node@22.12.0 node node_modules/@angular/cli/bin/ng.js build)

# --- Playwright browser (used as a fallback automation engine) ---
say "Downloading the Playwright browser..."
uv run playwright install chromium

# --- Local data files from examples (never overwrites existing files) ---
[ -f data/candidate_profile.json ] || cp data/candidate_profile.example.json data/candidate_profile.json
[ -f data/master_resume.json ] || cp data/master_resume.example.json data/master_resume.json

# --- .env with your Gemini API key ---
if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "Job Applier tailors resumes and cover letters with Google Gemini."
  echo "Get a free API key at: https://aistudio.google.com/app/apikey"
  read -rp "Paste your GOOGLE_API_KEY here (press Enter to add it later): " GEMINI_KEY
  if [ -n "${GEMINI_KEY:-}" ]; then
    sed -i '' "s|^GOOGLE_API_KEY=.*|GOOGLE_API_KEY=$GEMINI_KEY|" .env
  else
    say "No key entered. Edit the .env file and set GOOGLE_API_KEY before using AI tailoring."
  fi
fi

say "Setup complete. Starting the dashboard..."
exec "$PYTHON_BIN" start.py
