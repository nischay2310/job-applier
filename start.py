#!/usr/bin/env python3
"""One-command startup for Windows and Ubuntu.

    python start.py            # Windows
    python3 start.py           # Ubuntu

Installs dependencies, opens the automation Chrome (log in to LinkedIn there
once - it remembers you), starts the dashboard, and opens it in your browser.
Ctrl+C stops the dashboard; the automation Chrome keeps running.

Options:
    --port 8000         dashboard port
    --reload            restart the server on code edits (kills running applies)
    --no-chrome         don't launch the automation Chrome
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IS_WINDOWS = os.name == "nt"
CDP_PORT = 9222

# A dedicated profile rather than your everyday one: it runs alongside your
# normal Chrome instead of fighting it for the same profile, and remote
# debugging is refused on Chrome's default profile in recent versions.
CHROME_PROFILE = ROOT / ".chrome-profile"


def say(msg: str) -> None:
    print(f"[start] {msg}", flush=True)


def venv_python() -> Path:
    if IS_WINDOWS:
        return ROOT / ".venv" / "Scripts" / "python.exe"
    return ROOT / ".venv" / "bin" / "python"


def install_dependencies() -> None:
    uv = shutil.which("uv")
    if uv:
        say("Syncing dependencies with uv...")
        subprocess.run([uv, "sync", "--quiet"], cwd=ROOT, check=True)
        return

    stamp = ROOT / ".venv" / ".installed"
    if stamp.exists() and stamp.stat().st_mtime >= (ROOT / "pyproject.toml").stat().st_mtime:
        return
    if not venv_python().exists():
        say("Creating virtual environment...")
        subprocess.run([sys.executable, "-m", "venv", str(ROOT / ".venv")], check=True)
    say("Installing dependencies (first run takes a few minutes)...")
    subprocess.run(
        [str(venv_python()), "-m", "pip", "install", "--quiet", "-e", str(ROOT)],
        check=True,
    )
    stamp.touch()


def http_ok(url: str, timeout: float = 2) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def find_chrome() -> str | None:
    override = os.environ.get("CHROME_PATH")
    if override and Path(override).exists():
        return override
    if IS_WINDOWS:
        candidates = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
            Path(os.environ.get("LOCALAPPDATA", "")),
        ]
        for base in candidates:
            exe = base / "Google" / "Chrome" / "Application" / "chrome.exe"
            if exe.exists():
                return str(exe)
        return None
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def start_chrome() -> None:
    cdp_url = f"http://127.0.0.1:{CDP_PORT}/json/version"
    if http_ok(cdp_url):
        say(f"Automation Chrome already running on port {CDP_PORT}.")
        return

    chrome = find_chrome()
    if not chrome:
        say("Chrome not found. Install Google Chrome, or set CHROME_PATH to its executable.")
        sys.exit(1)
    if not IS_WINDOWS and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        say("No desktop display detected - Chrome needs one so you can log in to LinkedIn.")

    CHROME_PROFILE.mkdir(exist_ok=True)
    args = [
        chrome,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={CHROME_PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        "https://www.linkedin.com/feed/",
    ]
    # Detach so Chrome (and your LinkedIn login) outlives this script.
    if IS_WINDOWS:
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(args, creationflags=flags, close_fds=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(args, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    say("Opening automation Chrome...")
    for _ in range(40):
        if http_ok(cdp_url):
            say("Automation Chrome is ready. Log in to LinkedIn in that window if it asks.")
            return
        time.sleep(0.5)
    say(f"Chrome started but its debugging port {CDP_PORT} never answered.")
    say("If another app uses that port, close it and run start.py again.")
    sys.exit(1)


def run_dashboard(port: int, reload: bool) -> None:
    url = f"http://127.0.0.1:{port}"
    if http_ok(f"{url}/api/health"):
        say(f"Dashboard already running - opening {url}")
        webbrowser.open(url)
        return

    # PYTHONUTF8: the app logs emoji, which Windows' default console
    # encoding (cp1252) can't print, crashing the server on startup.
    env = dict(os.environ, USE_HOST_CHROME="1", PYTHONUNBUFFERED="1", PYTHONUTF8="1")
    cmd = [
        str(venv_python()), "-m", "job_applier.cli.web_app",
        "--host", "127.0.0.1", "--port", str(port), "--no-browser",
    ]
    if not reload:
        cmd.append("--no-reload")

    server = subprocess.Popen(cmd, cwd=ROOT, env=env)
    for _ in range(60):
        if server.poll() is not None:
            say("Dashboard failed to start - see the error above.")
            sys.exit(server.returncode or 1)
        if http_ok(f"{url}/api/health"):
            break
        time.sleep(0.5)
    say(f"Dashboard running at {url}  (Ctrl+C to stop)")
    webbrowser.open(url)

    try:
        server.wait()
    except KeyboardInterrupt:
        say("Stopping dashboard...")
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


def main() -> None:
    parser = argparse.ArgumentParser(description="Start Job Applier")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true",
                        help="restart on code edits (interrupts running applies)")
    parser.add_argument("--no-chrome", action="store_true",
                        help="don't launch the automation Chrome")
    args = parser.parse_args()

    os.chdir(ROOT)
    install_dependencies()
    if not (ROOT / "data" / "default_resume.pdf").exists():
        say("Note: data/default_resume.pdf is missing - LinkedIn applies will use a generated CV.")
    if not args.no_chrome:
        start_chrome()
    run_dashboard(args.port, args.reload)


if __name__ == "__main__":
    main()
