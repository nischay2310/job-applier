"""Runs the Chrome instance the automation drives.

It's a separate Chrome with its own profile (.chrome-profile in the project),
running alongside your everyday Chrome - which is never closed or restarted.
Chrome refuses remote control of its default profile (since v136), so a
dedicated profile is the only reliable way to drive it.

Standard library only: start.py loads this before dependencies are installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

IS_WINDOWS = os.name == "nt"
CDP_PORT = int(os.environ.get("HOST_CHROME_CDP_PORT", "9222"))
PROFILE_DIR = Path(
    os.environ.get(
        "CHROME_AUTOMATION_PROFILE",
        Path(__file__).resolve().parents[2] / ".chrome-profile",
    )
)


def cdp_ready(timeout: float = 2) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=timeout
        ) as r:
            return r.status == 200
    except Exception:
        return False


def find_chrome() -> str | None:
    override = os.environ.get("CHROME_PATH")
    if override and Path(override).exists():
        return override
    if IS_WINDOWS:
        for base in (
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ):
            exe = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if exe.exists():
                return str(exe)
        return None
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ):
        found = shutil.which(name)
        if found:
            return found
    return None


def _stop_automation_chrome() -> None:
    """Stops only the Chrome using the automation profile - never yours.

    Needed when that instance is open without remote control (e.g. someone
    reopened it by hand): Chrome won't start a second copy on the same
    profile, it'd just hand the launch over to the stuck one.
    """
    profile = str(PROFILE_DIR)
    if IS_WINDOWS:
        script = (
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
            f"Where-Object {{ $_.CommandLine -like '*{profile}*' }} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script], capture_output=True
        )
    else:
        subprocess.run(
            ["pkill", "-f", f"--user-data-dir={profile}"], capture_output=True
        )
    time.sleep(2)


def _profile_in_use() -> bool:
    lock = PROFILE_DIR / ("lockfile" if IS_WINDOWS else "SingletonLock")
    return lock.exists() or lock.is_symlink()


def turn_off_chrome_signin(profile_dir: Path | None = None) -> bool:
    """Switches off "Allow Chrome sign-in" in the automation profile.

    Signing in to a site with Google makes Chrome open its own "Sign in to
    Chrome" page (chrome://signin-dice-web-intercept) instead of going back
    to the site, and that page can also stop the automation from attaching.
    Only touches the profile's own settings, and only while Chrome is closed.
    """
    prefs_file = (profile_dir or PROFILE_DIR) / "Default" / "Preferences"
    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        signin = prefs.setdefault("signin", {})
        if signin.get("allowed") is False:
            return False
        signin["allowed"] = False
        prefs_file.write_text(json.dumps(prefs), encoding="utf-8")
        return True
    except Exception:
        return False


def ensure_chrome() -> tuple[bool, str]:
    """Makes sure the automation Chrome is running and controllable."""
    if cdp_ready():
        return True, "Automation Chrome is running."

    chrome = find_chrome()
    if not chrome:
        return False, "Chrome not found. Install Google Chrome, or set CHROME_PATH."

    if _profile_in_use():
        _stop_automation_chrome()

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    turn_off_chrome_signin()
    args = [
        chrome,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={PROFILE_DIR}",
        "--disable-sync",
        # Open the profile directly - with more than one profile in the
        # folder Chrome stops at its profile picker, which can't be driven.
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        # Start clean: restoring the last session reopens every half-finished
        # agent window. The login lives in cookies, not in open tabs.
        "https://www.linkedin.com/feed/",
    ]
    # Chrome must outlive whatever started it - stopping the dashboard kills
    # its whole process tree. On Windows, "cmd /c start" hands Chrome off and
    # exits, so Chrome isn't in that tree at all.
    if IS_WINDOWS:
        subprocess.Popen(
            ["cmd", "/c", "start", "", *args],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        subprocess.Popen(
            args,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    for _ in range(40):
        if cdp_ready():
            return True, "Opened the automation Chrome."
        time.sleep(0.5)
    return False, f"Automation Chrome started but port {CDP_PORT} never answered."


def close_idle_windows(keep_target_ids: set[str]) -> int:
    """Closes automation Chrome tabs no running agent owns. One tab is always
    left open - closing the last one would quit Chrome."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=3
        ) as r:
            pages = [t for t in json.load(r) if t.get("type") == "page"]
    except Exception:
        return 0
    idle = [t for t in pages if t["id"] not in keep_target_ids]
    if len(idle) == len(pages):
        idle = idle[1:]
    closed = 0
    for t in idle:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{CDP_PORT}/json/close/{t['id']}", timeout=3
            )
            closed += 1
        except Exception:
            pass
    return closed


def activate_target(target_id: str) -> bool:
    """Brings one of the automation Chrome's windows to the front."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{CDP_PORT}/json/activate/{target_id}", timeout=3
        ) as r:
            return r.status == 200
    except Exception:
        return False


def restart_chrome() -> tuple[bool, str]:
    """Closes the automation Chrome and opens a fresh one.

    Worth doing when it has drifted - windows piling up, a stale CDP
    connection, or a profile left locked by a crash.
    """
    _stop_automation_chrome()
    return ensure_chrome()


def open_page(url: str) -> bool:
    """Opens a URL in a new tab of the automation Chrome and brings it forward."""
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{CDP_PORT}/json/new?{url}", method="PUT"
        )
        with urllib.request.urlopen(request, timeout=5) as r:
            tab = json.load(r)
        activate_target(tab["id"])
        return True
    except Exception:
        return False


def target_is_open(target_id: str) -> bool:
    """True while Chrome still lists this tab or window."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=3
        ) as r:
            return any(t.get("id") == target_id for t in json.load(r))
    except Exception:
        return False


def close_target(target_id: str) -> bool:
    """Closes one automation Chrome tab or window by its CDP target id."""
    if not target_id:
        return False
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{CDP_PORT}/json/close/{target_id}", timeout=3
        ) as r:
            return r.status == 200
    except Exception:
        return False
