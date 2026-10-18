#!/usr/bin/env bash
# One-attempt install + launch for Job Applier on Linux (Debian/Ubuntu, Fedora, Arch).
#
# What it does, in order:
#   1. Installs missing prerequisites (Python 3.12, uv, Node.js, Google Chrome, Ollama)
#   2. Installs Python + frontend dependencies
#   3. Builds the dashboard UI and downloads the Playwright browser
#   4. Creates your .env and data files from the provided examples
#   5. Asks for your Gemini API key (only the first time)
#   6. Starts the dashboard and opens it in your browser
#
# Usage:
#   chmod +x scripts/install-linux.sh
#   ./scripts/install-linux.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

say() { printf '\n\033[1;36m[install]\033[0m %s\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

PKG=""
if have apt-get; then PKG="apt"
elif have dnf; then PKG="dnf"
elif have pacman; then PKG="pacman"
fi

sudo_if_needed() {
  if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi
}

install_pkg() {
  # install_pkg <apt-name> <dnf-name> <pacman-name>
  case "$PKG" in
    apt) sudo_if_needed apt-get update -y && sudo_if_needed apt-get install -y "$1" ;;
    dnf) sudo_if_needed dnf install -y "$2" ;;
    pacman) sudo_if_needed pacman -Sy --noconfirm "$3" ;;
    *)
      say "No supported package manager found (apt/dnf/pacman). Install '$1' manually and re-run this script."
      exit 1
      ;;
  esac
}

say "Checking prerequisites..."

# --- curl (needed below to install uv and, on some systems, Chrome) ---
if ! have curl; then
  say "curl not found. Installing..."
  install_pkg curl curl curl
fi

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
  install_pkg python3 python3 python
  PYTHON_BIN="python3"
fi
say "Using Python: $($PYTHON_BIN --version)"

# --- git (needed for the repo you already cloned, but checked for completeness) ---
if ! have git; then
  say "git not found. Installing..."
  install_pkg git git git
fi

# --- uv (fast Python package manager) ---
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
  say "Node.js 20+ not found. Installing via NodeSource (apt) or your package manager..."
  if [ "$PKG" = "apt" ]; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo_if_needed bash -
    install_pkg nodejs nodejs nodejs
  else
    install_pkg nodejs nodejs nodejs
  fi
fi
say "Using Node: $(node -v)"

# --- Google Chrome (drives the automation browser) ---
if ! have google-chrome && ! have google-chrome-stable && ! have chromium-browser && ! have chromium; then
  say "Google Chrome not found. Installing..."
  if [ "$PKG" = "apt" ]; then
    TMP_DEB="$(mktemp --suffix=.deb)"
    curl -fsSL -o "$TMP_DEB" https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    sudo_if_needed apt-get install -y "$TMP_DEB" || sudo_if_needed apt-get -f install -y
    rm -f "$TMP_DEB"
  elif [ "$PKG" = "dnf" ]; then
    sudo_if_needed dnf install -y https://dl.google.com/linux/direct/google-chrome-stable_current_x86_64.rpm
  else
    install_pkg chromium chromium chromium
  fi
else
  say "Google Chrome / Chromium already installed."
fi

# --- Ollama + a small local model (reads your resume privately) ---
if ! have ollama; then
  say "Ollama not found. Installing..."
  curl -fsSL https://ollama.com/install.sh | sh
fi
say "Downloading the resume-reading model (qwen2.5:3b, about 2 GB, first time only)..."
(have systemctl && sudo systemctl start ollama 2>/dev/null) || (nohup ollama serve >/dev/null 2>&1 &)
sleep 3
ollama pull qwen2.5:3b

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
uv run playwright install --with-deps chromium

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
    sed -i "s|^GOOGLE_API_KEY=.*|GOOGLE_API_KEY=$GEMINI_KEY|" .env
  else
    say "No key entered. Edit the .env file and set GOOGLE_API_KEY before using AI tailoring."
  fi
fi

say "Setup complete. Starting the dashboard..."
exec "$PYTHON_BIN" start.py
