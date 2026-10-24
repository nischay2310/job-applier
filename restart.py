#!/usr/bin/env python3
"""Restarts the whole application from the outside.

The dashboard can't relaunch itself - the process asking for the restart is
the one going away. This waits for it to stop, optionally closes the
automation Chrome too, then brings everything back through start.py.

    python restart.py            # dashboard only
    python restart.py --full     # automation Chrome as well

The dashboard runs this for you when you press Restart in the UI and it
wasn't started by start.py (start.py can relaunch it directly).
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IS_WINDOWS = os.name == "nt"

_spec = importlib.util.spec_from_file_location(
    "chrome_launcher", ROOT / "src" / "job_applier" / "chrome_launcher.py"
)
assert _spec is not None and _spec.loader is not None
chrome_launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chrome_launcher)


def say(msg: str) -> None:
    print(f"[restart] {msg}", flush=True)


def dashboard_up(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=2
        ) as r:
            return r.status == 200
    except Exception:
        return False


def wait_until_down(port: int, seconds: int = 45) -> bool:
    for _ in range(seconds * 2):
        if not dashboard_up(port):
            return True
        time.sleep(0.5)
    return False


def kill_dashboard(port: int) -> None:
    """Last resort when the dashboard won't stop on its own."""
    if IS_WINDOWS:
        listing = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True
        ).stdout
        for line in listing.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                subprocess.run(
                    ["taskkill", "/F", "/PID", line.split()[-1]], capture_output=True
                )
    else:
        subprocess.run(["pkill", "-f", f"job_applier.cli.web_app.*--port {port}"])
    time.sleep(2)


def launch_start(port: int) -> None:
    """Starts start.py detached, so it outlives this script."""
    cmd = [sys.executable, str(ROOT / "start.py"), "--port", str(port), "--no-chrome"]
    if IS_WINDOWS:
        subprocess.Popen(
            ["cmd", "/c", "start", "", *cmd],
            cwd=ROOT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
        )
    else:
        subprocess.Popen(
            cmd,
            cwd=ROOT,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Restart Job Applier")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--full", action="store_true", help="restart the automation Chrome as well"
    )
    args = parser.parse_args()
    os.chdir(ROOT)

    if dashboard_up(args.port):
        say("Waiting for the dashboard to stop...")
        if not wait_until_down(args.port):
            say("It didn't stop on its own - stopping it.")
            kill_dashboard(args.port)

    if args.full:
        ok, message = chrome_launcher.restart_chrome()
        say(message)
    elif not chrome_launcher.cdp_ready():
        ok, message = chrome_launcher.ensure_chrome()
        say(message)

    say("Starting the app again...")
    launch_start(args.port)
    for _ in range(90):
        if dashboard_up(args.port):
            say(f"Back up at http://127.0.0.1:{args.port}")
            return
        time.sleep(1)
    say("It hasn't come back - run 'python start.py' to see why.")
    sys.exit(1)


if __name__ == "__main__":
    main()
