#!/usr/bin/env python3
"""One-command startup for Windows and Ubuntu.

    python start.py            # Windows
    python3 start.py           # Ubuntu

Installs dependencies, opens the automation Chrome (a separate window with
its own profile - your everyday Chrome is never touched), starts the
dashboard, and opens it. First time only: sign in to LinkedIn in the
automation Chrome. Ctrl+C stops the dashboard; that Chrome keeps running.

Options:
    --port 8000     dashboard port
    --reload        restart the server on code edits (kills running applies)
    --no-chrome     don't open the automation Chrome
"""

from __future__ import annotations

import argparse
import importlib.util
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

# Loaded by path, not "import job_applier...": the package __init__ needs
# dependencies that aren't installed yet on a first run.
_spec = importlib.util.spec_from_file_location(
    "chrome_launcher", ROOT / "src" / "job_applier" / "chrome_launcher.py"
)
chrome_launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chrome_launcher)


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


def start_chrome() -> None:
    if not IS_WINDOWS and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        say("No desktop display detected - Chrome needs one so you can log in to LinkedIn.")
    ok, message = chrome_launcher.ensure_chrome()
    say(message)
    if not ok:
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
                        help="don't open the automation Chrome")
    args = parser.parse_args()

    os.chdir(ROOT)
    install_dependencies()
    if not list((ROOT / "data").glob("*.pdf")):
        say("Note: no resume PDF in data/ - LinkedIn applies will use a generated CV.")
    if not args.no_chrome:
        start_chrome()
    run_dashboard(args.port, args.reload)


if __name__ == "__main__":
    main()
