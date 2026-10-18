# Job Applier: AI-Powered Application Suite

Job Applier automates the job search lifecycle: it scrapes job postings for you, tailors your resume and cover letter to each one with Google Gemini, and helps you apply through a simple web dashboard — right from your own computer. You don't need to be a programmer to use it.

---

## Key Capabilities

- **Unified Web Dashboard:** A full-featured dashboard with side-by-side PDF preview, cover letter editor, and application tracker.
- **Multi-Site Scraping:** Aggregates listings from LinkedIn, Indeed, Glassdoor, Google Jobs, Romanian portals (eJobs, BestJobs, Hipo, Undelucram, Jooble), direct ATSs (Greenhouse, Lever), and remote tech boards.
- **AI Resume & Cover Letter Tailoring:** Reads each job description and rewrites your resume and cover letter to match it, using Google Gemini.
- **Browser Automation:** Opens a dedicated Chrome window that fills in applications for you — your everyday Chrome is never touched.
- **Session Vault & Desktop Sync:** Remembers your logins, and can sync them straight from your normal desktop Chrome in one click.
- **Application Tracker:** Keeps a local database of everything you've applied to, with portable backup/restore.

---

## Prerequisites

The install scripts below install almost everything automatically. This section exists so you know what's being installed and why, and so you can install things by hand if the script can't (for example, on a machine without internet access to package managers, or if you prefer full control).

| Tool | Why it's needed | Minimum version |
| --- | --- | --- |
| **Git** | To download ("clone") this project | any recent version |
| **Python** | Runs the scraping, tailoring, and dashboard backend | 3.10+ |
| **uv** | Installs Python dependencies quickly and reliably | latest |
| **Node.js + npm** | Builds the dashboard's web interface | 20+ |
| **Google Chrome** | The browser the automation drives to fill in applications | latest |
| **Google Gemini API key** | Powers the AI resume/cover-letter tailoring | free tier available |

You do **not** need to install Playwright, Docker, or anything else by hand — the install scripts handle that too.

### Manual install commands (only if you're not using the scripts)

**Linux (Debian/Ubuntu):**
```bash
sudo apt-get update
sudo apt-get install -y git python3 curl
curl -LsSf https://astral.sh/uv/install.sh | sh                     # installs uv
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo bash -       # adds Node.js 22 repo
sudo apt-get install -y nodejs
curl -fsSL -o chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt-get install -y ./chrome.deb
```

**macOS:**
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"  # installs Homebrew
brew install git python@3.12 node
curl -LsSf https://astral.sh/uv/install.sh | sh                     # installs uv
brew install --cask google-chrome
```

**Windows (PowerShell, run as a normal user):**
```powershell
winget install --id Git.Git -e
winget install --id Python.Python.3.12 -e
winget install --id OpenJS.NodeJS.LTS -e
winget install --id Google.Chrome -e
powershell -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"   # installs uv
```

### Getting a free Gemini API key

1. Go to <https://aistudio.google.com/app/apikey>.
2. Sign in with a Google account.
3. Click **Create API key** and copy it.
4. Paste it when the install script asks for `GOOGLE_API_KEY` (or add it to `.env` yourself later).

---

## Quickstart (one script, one attempt)

### 1. Download the project

Open a terminal (**Terminal** on Mac/Linux, **PowerShell** on Windows) and run:

```bash
git clone https://github.com/nischay2310/job-applier.git
cd job-applier
```

### 2. Run the installer for your operating system

Each script installs every missing prerequisite, sets up the project, asks for your Gemini API key once, and launches the dashboard — all in a single run. You will only be asked for your computer password if a system package needs to be installed (normal for installing software).

| Your system | Command |
| --- | --- |
| 🐧 **Linux** | `./scripts/install-linux.sh` |
| 🍎 **macOS** | `./scripts/install-mac.sh` |
| 🪟 **Windows** | Right-click `scripts\install-windows.ps1` → **Run with PowerShell** (or run `powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1`) |

If a script says "permission denied" on Mac/Linux, run `chmod +x scripts/*.sh` once and try again.

### 3. Use the dashboard

When setup finishes, your browser opens automatically at **<http://127.0.0.1:8000>**. From there you can:

1. Check your profile and resume details. On the very first run, `start.py` asks for your resume PDF (optional) and a few basic questions in the terminal, saves them, and offers to start the first job search for you. You can edit `data/master_resume.json` and `data/candidate_profile.json` any time.
2. Click **Scrape Jobs** to pull in new postings.
3. Review each tailored resume/cover letter, then **Auto-Apply** or **Autonomous Submit**.

The first time you scrape or apply to LinkedIn, a separate "automation" Chrome window opens — log in there once and it's remembered for next time.

### Starting it again later

You don't need to re-run the installer every time. Once it's installed:

```bash
python3 start.py      # Linux / macOS
python start.py       # Windows
```

Useful flags: `--status` (what's running), `--stop` (shut everything down), `--port 8000` (change the port).

---

## Deploy with Docker (optional, for advanced/server use)

```bash
cp .env.example .env  # Add GOOGLE_API_KEY
docker compose up -d --build
```

Published releases can be installed on another server using only `compose.yaml` and `.env`; no repository checkout is required. See the **[deployment guide](docs/deployment.md)** for upgrades, persistent data, private access, and optional host deployment through GitHub Environment secrets.

---

## Common Tasks (`just`)

If you have [`just`](https://github.com/casey/just) installed, run `just` to see all available recipes:

| Task | Command | Description |
| ------- | ------------- | ------------- |
| **Start Web Dashboard** | `just web` | Runs FastAPI server hosting the dashboard on port 8000 |
| **Angular Dev Server** | `just ui` | Runs `ng serve` on port 4200 with proxy to port 8000 |
| **Build Frontend** | `just build-ui` | Compiles production Angular bundle into `frontend/dist/` |
| **Run Pipeline** | `just pipeline` | Runs scraping, tailoring, and PDF generation |
| **Run Remote Pipeline** | `just pipeline-remote` | Runs pipeline targeting remote/telework positions |
| **Terminal Assistant** | `just assistant` | Launches interactive terminal review & auto-apply CLI |
| **Log In to Platform** | `just login linkedin` | Launches native Chrome on DISPLAY to sign in & save session |
| **Desktop Chrome Sync** | `just sync-chrome` | 1-click copies sessions from desktop Chrome into profile |
| **Auth Status** | `just auth-status` | Inspects real cookie database for active login tokens |
| **Export Backup** | `just export backup.zip` | Creates portable `.zip` backup of all applications & SQLite DB |
| **Import Backup** | `just import backup.zip` | Unpacks and merges backup into local environment |
| **Prune Duplicates** | `just prune-dupes` | Removes redundant packages with matching canonical URLs/roles |
| **Prune Expired** | `just prune-expired` | Purges closed/expired job postings from queue |
| **Run Tests** | `just test` | Runs the full `pytest` test suite |
| **Run All Checks** | `just check` | Runs linters, pytest tests, and Angular production build |

---

## Troubleshooting

| Problem | Fix |
| --- | --- |
| Script says a command isn't recognized after installing it | Close and reopen your terminal, then re-run the script — this refreshes your `PATH`. |
| Dashboard opens but tailoring fails | Check `.env` has a valid `GOOGLE_API_KEY`, saved from <https://aistudio.google.com/app/apikey>. |
| LinkedIn asks to log in every time | Run `just login linkedin` (or use **Desktop Chrome Sync** in the dashboard) to save your session. |
| Port 8000 already in use | Run `python3 start.py --port 8001` (or any free port). |
| Need to fully stop everything | Run `python3 start.py --stop`. |

---

## Documentation

Detailed guides and references are split into specialized documentation:

| Guide | Description |
| ------- | ------------- |
| 🏗️ **[Architecture & Project Structure](docs/architecture.md)** | Directory tree, component design, and module roles. |
| 🖥️ **[Web Dashboard Guide](docs/web_dashboard.md)** | Queue workflows, live HUD monitor, diagnostics, and tracker views. |
| ⌨️ **[CLI Reference](docs/cli.md)** | Options for `orchestrator.py`, `apply_assistant.py`, and interactive keybindings. |
| 🔐 **[Authentication & Sessions](docs/authentication.md)** | Persistent browser profiles, desktop Chrome cookie sync, and 2FA handling. |
| 📦 **[Database & Portable Sync](docs/sync.md)** | SQLite database model, backup bundle structure, and machine migration. |
| 🚀 **[Deployment](docs/deployment.md)** | Docker Compose, GHCR releases, upgrades, private access, and secret-based CI/CD. |

---

## Testing

Run the test suite with `uv`:

```bash
uv run pytest
```

---

## License

[MIT License](LICENSE)
