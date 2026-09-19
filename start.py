#!/usr/bin/env python3
"""One-command startup for Windows and Ubuntu.

    python start.py            # Windows
    python3 start.py           # Ubuntu

Installs dependencies, makes your main Chrome (where you're already signed in
to LinkedIn) controllable by the app, starts the dashboard, and opens it.
If Chrome is already open without remote control, it's restarted once and
your tabs are restored. Ctrl+C stops the dashboard; Chrome keeps running.

Options:
    --port 8000           dashboard port
    --reload              restart the server on code edits (kills running applies)
    --no-chrome           don't touch Chrome
    --separate-profile    use a separate Chrome profile instead of your main one
                          (sign in to LinkedIn there once)
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

SEPARATE_PROFILE = ROOT / ".chrome-profile"


def main_chrome_user_data(chrome: str) -> Path:
    """Chrome's own user-data directory - the one your everyday browsing and
    LinkedIn login live in."""
    if IS_WINDOWS:
        return Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data"
    if "chromium" in Path(chrome).name:
        return Path.home() / ".config" / "chromium"
    return Path.home() / ".config" / "google-chrome"


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


def chrome_running() -> bool:
    if IS_WINDOWS:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
            capture_output=True, text=True,
        ).stdout
        return "chrome.exe" in out.lower()
    return subprocess.run(
        ["pgrep", "-x", "chrome|chromium|chromium-browser"], capture_output=True
    ).returncode == 0


def close_chrome() -> None:
    """Closes Chrome gracefully so it saves the open tabs for restoring,
    then force-stops anything left over (Chrome's background mode)."""
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/IM", "chrome.exe"], capture_output=True)
    else:
        subprocess.run(["pkill", "-TERM", "-x", "chrome|chromium|chromium-browser"])
    for _ in range(30):
        if not chrome_running():
            return
        time.sleep(0.5)
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True)
    else:
        subprocess.run(["pkill", "-KILL", "-x", "chrome|chromium|chromium-browser"])
    time.sleep(2)


def start_chrome(separate_profile: bool) -> None:
    cdp_url = f"http://127.0.0.1:{CDP_PORT}/json/version"
    if http_ok(cdp_url):
        say(f"Chrome is already controllable on port {CDP_PORT}.")
        return

    chrome = find_chrome()
    if not chrome:
        say("Chrome not found. Install Google Chrome, or set CHROME_PATH to its executable.")
        sys.exit(1)
    if not IS_WINDOWS and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        say("No desktop display detected - Chrome needs one so you can log in to LinkedIn.")

    if separate_profile:
        SEPARATE_PROFILE.mkdir(exist_ok=True)
        profile_args = [f"--user-data-dir={SEPARATE_PROFILE}", "https://www.linkedin.com/feed/"]
    else:
        # A running Chrome can't gain remote debugging - a second launch just
        # opens a window inside it - so it has to restart with the flag.
        if chrome_running():
            say("Restarting your Chrome once to enable remote control (tabs will be restored)...")
            close_chrome()
        profile_args = [
            f"--user-data-dir={main_chrome_user_data(chrome)}",
            "--profile-directory=Default",
            "--restore-last-session",
        ]

    args = [
        chrome,
        f"--remote-debugging-port={CDP_PORT}",
        *profile_args,
        "--no-first-run",
        "--no-default-browser-check",
    ]
    # Detach so Chrome (and your LinkedIn login) outlives this script.
    if IS_WINDOWS:
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(args, creationflags=flags, close_fds=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(args, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    say("Opening Chrome...")
    for _ in range(40):
        if http_ok(cdp_url):
            say("Chrome is ready and controllable.")
            return
        time.sleep(0.5)
    say(f"Chrome started but its debugging port {CDP_PORT} never answered.")
    if not separate_profile:
        # Newer Chrome builds may refuse remote debugging on the main profile.
        say("Your Chrome version may block remote control of the main profile.")
        say("Run:  python start.py --separate-profile   (then sign in to LinkedIn once there)")
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
                        help="don't touch Chrome")
    parser.add_argument("--separate-profile", action="store_true",
                        help="use a separate Chrome profile instead of your main one")
    args = parser.parse_args()

    os.chdir(ROOT)
    install_dependencies()
    if not (ROOT / "data" / "default_resume.pdf").exists():
        say("Note: data/default_resume.pdf is missing - LinkedIn applies will use a generated CV.")
    if not args.no_chrome:
        start_chrome(args.separate_profile)
    run_dashboard(args.port, args.reload)


if __name__ == "__main__":
    main()
