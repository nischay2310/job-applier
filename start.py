#!/usr/bin/env python3
"""One-command startup for Windows and Ubuntu.

    python start.py            # Windows
    python3 start.py           # Ubuntu

Installs dependencies, opens the automation Chrome (a separate window with
its own profile - your everyday Chrome is never touched), starts the
dashboard, and opens it. First time only: sign in to LinkedIn in the
automation Chrome. Ctrl+C stops the dashboard; that Chrome keeps running.

Options:
    --stop          stop the dashboard and the automation Chrome
    --status        show what's running
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
# Matches the web app's restart codes: the dashboard asking to come back,
# either on its own or with a fresh automation Chrome behind it.
RESTART_EXIT_CODE = 42
RESTART_FULL_EXIT_CODE = 43

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
    # JOB_APPLIER_SUPERVISED tells the app a restart is as cheap as exiting:
    # this loop brings it back. Without it the app has to hand the job to
    # restart.py instead.
    env = dict(
        os.environ,
        USE_HOST_CHROME="1",
        PYTHONUNBUFFERED="1",
        PYTHONUTF8="1",
        JOB_APPLIER_SUPERVISED="1",
    )
    cmd = [
        str(venv_python()), "-m", "job_applier.cli.web_app",
        "--host", "127.0.0.1", "--port", str(port), "--no-browser",
    ]
    if not reload:
        cmd.append("--no-reload")

    opened = False
    while True:
        server = subprocess.Popen(cmd, cwd=ROOT, env=env)
        for _ in range(60):
            if server.poll() is not None:
                say("Dashboard failed to start - see the error above.")
                sys.exit(server.returncode or 1)
            if http_ok(f"{url}/api/health"):
                break
            time.sleep(0.5)
        say(f"Dashboard running at {url}  (Ctrl+C to stop)")
        if not opened:
            webbrowser.open(url)
            opened = True

        try:
            code = server.wait()
        except KeyboardInterrupt:
            say("Stopping dashboard...")
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
            return
        if code not in (RESTART_EXIT_CODE, RESTART_FULL_EXIT_CODE):
            return
        say("Restart requested from the dashboard...")
        if code == RESTART_FULL_EXIT_CODE:
            ok, message = chrome_launcher.restart_chrome()
            say(message)


def stop_everything(port: int) -> None:
    """Stops the dashboard and the automation Chrome."""
    if http_ok(f"http://127.0.0.1:{port}/api/health"):
        if IS_WINDOWS:
            listing = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True
            ).stdout
            pids = {
                line.split()[-1]
                for line in listing.splitlines()
                if f":{port}" in line and "LISTENING" in line
            }
            for pid in pids:
                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
        else:
            subprocess.run(["pkill", "-f", f"job_applier.cli.web_app --host 127.0.0.1 --port {port}"])
        say("Dashboard stopped.")
    else:
        say("Dashboard wasn't running.")

    if chrome_launcher.cdp_ready():
        chrome_launcher._stop_automation_chrome()
        say("Automation Chrome stopped.")
    else:
        say("Automation Chrome wasn't running.")


def show_status(port: int) -> None:
    running = http_ok(f"http://127.0.0.1:{port}/api/health")
    say(f"Dashboard:         {'running at http://127.0.0.1:' + str(port) if running else 'stopped'}")
    say(f"Automation Chrome: {'running' if chrome_launcher.cdp_ready() else 'stopped'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Start Job Applier")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true",
                        help="restart on code edits (interrupts running applies)")
    parser.add_argument("--no-chrome", action="store_true",
                        help="don't open the automation Chrome")
    parser.add_argument("--stop", action="store_true",
                        help="stop the dashboard and the automation Chrome")
    parser.add_argument("--status", action="store_true", help="show what's running")
    args = parser.parse_args()

    os.chdir(ROOT)
    if args.stop:
        stop_everything(args.port)
        return
    if args.status:
        show_status(args.port)
        return

    install_dependencies()
    if not list((ROOT / "data").glob("*.pdf")):
        say("Note: no resume PDF in data/ - LinkedIn applies will use a generated CV.")
    if not args.no_chrome:
        start_chrome()
    run_dashboard(args.port, args.reload)


if __name__ == "__main__":
    main()
