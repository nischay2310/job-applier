from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sys
import threading
import time
import urllib.request
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from job_applier.automation.candidate_profile import (  # type: ignore[import-not-found]
    CandidateProfile,
    load_candidate_profile,
)
from job_applier.automation.cloudflare import (  # type: ignore[import-not-found]
    VirtualDisplayManager,
    extract_ray_id,
    is_cloudflare_challenge,
    solve_cloudflare_turnstile,
)
from job_applier.automation.question_store import (  # type: ignore[import-not-found]
    record_pending,
)
from job_applier.chrome_launcher import cdp_ready, close_target, ensure_chrome
from job_applier.automation.question_solver import (  # type: ignore[import-not-found]
    QuestionSolver,
)
from job_applier.logger import log_event  # type: ignore[import-not-found]
from job_applier.scrapers.job_filters import (  # type: ignore[import-not-found]
    is_excluded_application_url,
)
from job_applier.scrapers.activity_checker import (  # type: ignore[import-not-found]
    is_job_active,
)
from job_applier.tracker import record_application  # type: ignore[import-not-found]
from job_applier.utils import get_project_root


IN_DOCKER = os.path.exists("/.dockerenv")

SUGGESTION_SELECTOR = (
    "[role='option'], .search-typeahead-v2__hit, "
    "[data-test-single-typeahead-entity-form-search-result]"
)

# "Select an option", "-- Choose --" and friends aren't real answers.
PLACEHOLDER_OPTION = re.compile(r"^\s*(select|choose|please select|pick|-{2,}|—)", re.I)
HOST_CHROME_HOSTNAME = os.environ.get(
    "HOST_CHROME_HOSTNAME", "host.docker.internal" if IN_DOCKER else "127.0.0.1"
)
HOST_CHROME_CDP_PORT = os.environ.get("HOST_CHROME_CDP_PORT", "9222")
USE_HOST_CHROME = os.environ.get("USE_HOST_CHROME", "1") not in ("0", "false", "False")


def is_display_available() -> bool:
    """Checks if a graphical display server (X11 or Wayland) is available."""
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


def clean_stale_chrome_locks(profile_dir: Path) -> None:
    """Removes stale Chrome lock symlinks ONLY if the holding process is confirmed dead."""
    lock_path = profile_dir / "SingletonLock"
    if lock_path.is_symlink() or lock_path.exists():
        try:
            target = os.readlink(str(lock_path))
            parts = target.rsplit("-", 1)
            if len(parts) == 2 and parts[1].isdigit():
                pid = int(parts[1])
                try:
                    os.kill(pid, 0)
                    # Process is still actively running — do NOT kill or touch it!
                    return
                except OSError:
                    # Process is confirmed dead — safe to remove stale symlink
                    lock_path.unlink(missing_ok=True)
            else:
                lock_path.unlink(missing_ok=True)
        except OSError:
            lock_path.unlink(missing_ok=True)

    # Clean leftover socket symlinks only if lock is gone
    if not lock_path.is_symlink() and not lock_path.exists():
        for name in ["SingletonCookie", "SingletonSocket", "lockfile"]:
            lp = profile_dir / name
            try:
                if lp.is_symlink() or lp.exists():
                    lp.unlink(missing_ok=True)
            except OSError:
                pass


def _check_feed_page(page: Any) -> tuple[bool, str]:
    """Navigates an already-open page to the feed and reports whether
    LinkedIn serves it or bounces to the login wall."""
    page.goto(
        "https://www.linkedin.com/feed/",
        wait_until="domcontentloaded",
        timeout=20000,
    )
    page.wait_for_timeout(2000)
    url = (page.url or "").lower()
    pwd_visible = page.locator("input[type=password]").count() > 0
    if "/login" in url or "/signin" in url or pwd_visible:
        return False, "LinkedIn rejected the session (redirected to login)"
    return True, "LinkedIn session verified live"


def host_chrome_ws_endpoint() -> str:
    """WebSocket URL of the automation Chrome's DevTools endpoint.

    Chrome only accepts CDP Host headers that are localhost or a bare IP, so a
    hostname like host.docker.internal is resolved to its IP first."""
    host_ip = socket.gethostbyname(HOST_CHROME_HOSTNAME)
    req = urllib.request.Request(
        f"http://{host_ip}:{HOST_CHROME_CDP_PORT}/json/version",
        headers={"Host": f"localhost:{HOST_CHROME_CDP_PORT}"},
    )
    info = json.loads(urllib.request.urlopen(req, timeout=5).read().decode())
    return (
        info["webSocketDebuggerUrl"]
        .replace("localhost", host_ip)
        .replace("127.0.0.1", host_ip)
    )


def verify_linkedin_session(context: Any | None = None) -> tuple[bool, str]:
    """Actually loads the synced cookies in a browser and checks whether
    LinkedIn accepts them, instead of just trusting that the cookie file
    exists. A li_at value can be present and still get bounced to the login
    wall the moment it's replayed in this container's browser.

    Pass an existing Playwright context when one is already open - starting a
    second sync_playwright() in the same thread is not supported and fails
    with "Sync API inside the asyncio loop"."""
    if context is not None:
        page = None
        try:
            page = context.new_page()
            return _check_feed_page(page)
        except Exception as e:
            return False, f"Could not verify session: {e}"
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

    if USE_HOST_CHROME:
        from playwright.sync_api import (  # type: ignore[import-not-found, import-untyped]
            sync_playwright,
        )

        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(host_chrome_ws_endpoint())
                try:
                    return verify_linkedin_session(browser.contexts[0])
                finally:
                    # Detaches only - the user's Chrome keeps running.
                    browser.close()
        except Exception as e:
            return False, (
                "Automation Chrome isn't reachable - start it with start.py "
                f"({type(e).__name__})"
            )

    cookies_path = get_project_root() / "data" / "linkedin_cookies.json"
    if not cookies_path.exists():
        return False, "No synced cookies found"

    try:
        with open(cookies_path, encoding="utf-8") as f:
            cookies = json.load(f)
    except Exception as e:
        return False, f"Could not read synced cookies: {e}"

    if not cookies:
        return False, "Synced cookie file is empty"

    from playwright.sync_api import (  # type: ignore[import-not-found, import-untyped]
        sync_playwright,
    )

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                own_context = browser.new_context()
                own_context.add_cookies(cookies)
                return _check_feed_page(own_context.new_page())
            finally:
                browser.close()
    except Exception as e:
        return False, f"Could not verify session: {e}"


ACTIVE_AUTOMATOR: BrowserAutomator | None = None

# Every automation that's running right now, for the dashboard's live view.
AGENTS: dict[str, dict[str, Any]] = {}
AGENTS_LOCK = threading.Lock()
# Agents the dashboard has asked to stop. Killing the thread outright would
# leave a half-filled form and a claimed job behind, so the automation checks
# this at each step and bows out cleanly instead.
ABORT_REQUESTS: set[str] = set()


class AgentAborted(RuntimeError):
    """Raised inside an automation when its job was cancelled from the UI."""


def request_abort(agent_id: str) -> bool:
    """Asks a running agent to stop at its next step."""
    with AGENTS_LOCK:
        if agent_id not in AGENTS:
            return False
        ABORT_REQUESTS.add(agent_id)
    return True


def get_active_automator() -> BrowserAutomator | None:
    """Returns the currently active BrowserAutomator instance, if any."""
    return ACTIVE_AUTOMATOR


def _as_words(text: str) -> str:
    """'phoneNumber-national_number' -> 'phone number national number'."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return re.sub(r"[_\-.:/]+", " ", text).lower()


def candidate_resume_path() -> Path | None:
    """The candidate's own resume PDF in data/. Its filename is what shows up
    on the account, so it stays as they named it."""
    folder = get_project_root() / "data"
    override = os.environ.get("RESUME_PATH")
    if override and Path(override).exists():
        return Path(override)
    return next(iter(sorted(folder.glob("*.pdf"))), None)


def agent_frame_path(agent_id: str) -> Path:
    return get_project_root() / "data" / "live" / f"{agent_id}.png"


class BrowserAutomator:
    """Automates job application submission and form autofill using Playwright."""

    def __init__(
        self,
        profile: CandidateProfile | None = None,
        headless: bool | None = None,
        use_persistent_profile: bool = True,
        profile_dir: Path | None = None,
        api_key: str | None = None,
        status_callback: Callable[[str, str, str, dict[str, Any]], None] | None = None,
    ):
        self.profile = profile or load_candidate_profile()
        self.status_callback = status_callback

        # Auto-detect display: check if an X11 display is available or can be provided by Xvfb
        display_ok = is_display_available()
        if not display_ok:
            disp = VirtualDisplayManager.ensure_display()
            if disp:
                display_ok = True

        if headless is None:
            self.headless = not display_ok
        elif not headless and not display_ok:
            print(
                "Notice: No graphical display detected ($DISPLAY not set). Running in headless mode."
            )
            self.headless = True
        else:
            self.headless = headless

        self.use_persistent_profile = use_persistent_profile
        self.api_key = api_key
        self.question_solver = QuestionSolver(self.profile, api_key=self.api_key)
        self.waiting_for_code: bool = False
        self.provided_code: str | None = None

        project_root = get_project_root()
        self.profile_dir = profile_dir or (project_root / ".browser_profile")

        self.use_host_chrome = USE_HOST_CHROME
        self.using_host_chrome = False
        self.last_apply_kind: str | None = None
        self.agent_id = uuid.uuid4().hex[:8]
        self.target_id: str | None = None
        # Tabs this run opened. Only these get closed when it ends - the
        # automation Chrome's own tabs (and your session with them) stay put.
        self._own_pages: list[Any] = []
        self.last_job_inactive = ""
        self.last_excluded = ""
        self._last_frame_at = 0.0

        self.playwright: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None

    def _notify(
        self,
        step: str,
        message: str,
        level: str = "INFO",
        details: dict[str, Any] | None = None,
    ) -> None:
        """Dispatches structured log event, captures a live-view screenshot,
        and triggers the live status callback."""
        log_event(message, level=level, category="Auto-Apply")
        self._capture_frame()
        self._update_agent(step=step, message=message, level=level)
        if self.status_callback:
            try:
                self.status_callback(step, message, level, details or {})
            except Exception as cb_err:
                print(f"Notice in status callback: {cb_err}")

    def _capture_frame(self, min_gap: float = 1.5) -> None:
        """Snapshot for the dashboard: the open dialog only, at most one every
        min_gap seconds. Photographing the whole page on every status update
        was a large part of each application's time."""
        if self.page is None or time.monotonic() - self._last_frame_at < min_gap:
            return
        self._last_frame_at = time.monotonic()
        try:
            frame = agent_frame_path(self.agent_id)
            frame.parent.mkdir(parents=True, exist_ok=True)
            # Clip to the dialog rather than screenshotting the element: taking
            # an element's picture scrolls it into view, which drags the whole
            # page around while an application is being filled.
            clip = None
            try:
                box = self._form_root().bounding_box(timeout=1000)
                view = self.page.viewport_size or {"width": 1280, "height": 900}
                if box:
                    x, y = max(0.0, box["x"]), max(0.0, box["y"])
                    width = min(box["width"], view["width"] - x)
                    height = min(box["height"], view["height"] - y)
                    if width > 50 and height > 50:
                        clip = {"x": x, "y": y, "width": width, "height": height}
            except Exception:
                pass
            if clip:
                self.page.screenshot(path=str(frame), clip=clip, timeout=5000)
            else:
                self.page.screenshot(path=str(frame), timeout=5000)
            shutil.copyfile(frame, get_project_root() / "data" / "live_view.png")
        except Exception:
            pass

    def _update_agent(self, **fields: Any) -> None:
        now = time.strftime("%H:%M:%S")
        # Every step reports through here, which makes it the natural place to
        # notice a cancellation and stop before touching the form again.
        with AGENTS_LOCK:
            aborting = self.agent_id in ABORT_REQUESTS
        if aborting:
            with AGENTS_LOCK:
                ABORT_REQUESTS.discard(self.agent_id)
            raise AgentAborted(f"Agent {self.agent_id} was stopped")
        with AGENTS_LOCK:
            agent = AGENTS.setdefault(
                self.agent_id,
                {"id": self.agent_id, "started_at": now, "step": "starting", "message": ""},
            )
            agent.update(fields, target_id=self.target_id, updated_at=now)

    def _inject_plaintext_cookies(self) -> None:
        """Loads cookies from data/linkedin_cookies.json (plaintext, extracted
        via CDP from a real logged-in browser) into the current context.

        Chrome's own Cookies SQLite file cannot just be copied between
        machines/environments — since Chrome ~127+ on Windows, sensitive
        cookie values are protected with "v20" App-Bound Encryption, which is
        cryptographically tied to that specific Windows Chrome install and
        cannot be decrypted anywhere else (including this Linux container).
        Plaintext cookies extracted from an authenticated session via CDP
        (Network.getCookies through the browser's own decrypted state) sidestep
        that entirely, and work identically across every browser profile/slot.
        """
        cookies_path = get_project_root() / "data" / "linkedin_cookies.json"
        if not cookies_path.exists():
            return
        try:
            with open(cookies_path, encoding="utf-8") as f:
                cookies = json.load(f)
            if cookies:
                self.context.add_cookies(cookies)
                print(f"Injected {len(cookies)} plaintext cookies into browser context.")
        except Exception as e:
            print(f"Notice: could not inject plaintext cookies: {e}")

    def _connect_host_chrome(self) -> bool:
        """Attaches to the user's own already-logged-in desktop Chrome over CDP
        instead of driving a fresh containerised browser.

        LinkedIn terminates the whole account session about a minute after a
        replayed-cookie headless browser starts submitting; the real browser is
        the session LinkedIn already trusts, so drive that one directly.
        Chrome only accepts CDP Host headers that are localhost or a bare IP,
        so host.docker.internal has to be resolved before connecting."""
        try:
            self.browser = self.playwright.chromium.connect_over_cdp(
                host_chrome_ws_endpoint()
            )
            self.context = self.browser.contexts[0]
            self.page = self._open_agent_window()
            self.using_host_chrome = True
            print(f"Attached to automation Chrome over CDP ({HOST_CHROME_HOSTNAME}:{HOST_CHROME_CDP_PORT})")
            return True
        except Exception as e:
            print(f"Notice: could not attach to desktop Chrome ({e}).")
            self.browser = None
            self.context = None
            self.page = None
            return False

    def _open_agent_window(self) -> Any:
        """Each agent works in its own Chrome window so runs can be watched
        side by side instead of fighting over the tab you're looking at."""
        try:
            cdp = self.browser.new_browser_cdp_session()
            with self.context.expect_page(timeout=5000) as new_page:
                target = cdp.send(
                    "Target.createTarget", {"url": "about:blank", "newWindow": True}
                )
            self.target_id = target["targetId"]
            self._own_pages.append(new_page.value)
            self._install_kill_switch(new_page.value)
            return new_page.value
        except Exception:
            page = self.context.new_page()
            self._own_pages.append(page)
            try:
                info = self.context.new_cdp_session(page).send("Target.getTargetInfo")
                self.target_id = info["targetInfo"]["targetId"]
            except Exception:
                pass
            self._install_kill_switch(page)
            return page

    # Injected into every page an agent opens. Fixed to the corner, outside
    # the form, so it never collides with what the automation is filling in.
    KILL_SWITCH_JS = """(agentId) => {
        if (document.getElementById('job-applier-killswitch')) return;
        const bar = document.createElement('div');
        bar.id = 'job-applier-killswitch';
        bar.style.cssText = [
            'position:fixed', 'z-index:2147483647', 'bottom:16px', 'right:16px',
            'display:flex', 'align-items:center', 'gap:10px',
            'padding:9px 12px', 'border-radius:12px',
            'background:#0f172a', 'border:1px solid #334155',
            'box-shadow:0 8px 24px rgba(0,0,0,.45)',
            'font:600 12px system-ui,-apple-system,Segoe UI,sans-serif',
            'color:#e2e8f0',
        ].join(';');
        const dot = document.createElement('span');
        dot.style.cssText = 'width:8px;height:8px;border-radius:50%;background:#34d399';
        const label = document.createElement('span');
        label.textContent = 'Agent ' + agentId + ' working';
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = 'Stop this agent';
        button.style.cssText = [
            'cursor:pointer', 'padding:5px 10px', 'border-radius:8px',
            'border:1px solid #f87171', 'background:#7f1d1d', 'color:#fff',
            'font:600 12px system-ui,-apple-system,Segoe UI,sans-serif',
        ].join(';');
        button.onclick = async () => {
            button.disabled = true;
            button.textContent = 'Stopping...';
            dot.style.background = '#f87171';
            label.textContent = 'Finishing the current step';
            try { await window.__jobApplierStop(); } catch (e) {
                button.textContent = 'Could not stop';
            }
        };
        bar.append(dot, label, button);
        (document.body || document.documentElement).appendChild(bar);
    }"""

    def _install_kill_switch(self, page: Any) -> None:
        """Puts a stop button in the agent's window.

        Watching a run go wrong and having to find the dashboard to stop it is
        the long way round - the window you are already looking at can do it.
        The button calls back into this process rather than the HTTP API, so
        it works whatever page LinkedIn has navigated to.
        """
        try:
            page.expose_function("__jobApplierStop", self._stop_from_page)
        except Exception:
            pass  # already exposed on this page
        try:
            page.add_init_script(
                f"window.addEventListener('DOMContentLoaded', () => "
                f"({self.KILL_SWITCH_JS})({self.agent_id!r}));"
            )
            page.evaluate(self.KILL_SWITCH_JS, self.agent_id)
        except Exception:
            pass  # a page mid-navigation gets it from the init script instead

    def _label_kill_switch(self, company: str, job_title: str) -> None:
        """Names the job on the badge - with several windows open, "Agent
        4f2a" tells you nothing about which one you are about to stop."""
        try:
            self.page.evaluate(
                """(text) => {
                    const bar = document.getElementById('job-applier-killswitch');
                    if (bar) bar.children[1].textContent = text;
                }""",
                f"{company} - {job_title}"[:60],
            )
        except Exception:
            pass

    def _stop_from_page(self) -> bool:
        """Handles a click on the in-window stop button."""
        with AGENTS_LOCK:
            ABORT_REQUESTS.add(self.agent_id)
        log_event(
            f"Agent {self.agent_id} stopped from its Chrome window - job stays queued",
            level="WARNING",
            category="Automation",
        )
        return True

    def start(self) -> None:
        """Starts Playwright and launches the browser with automatic fallbacks."""
        if self.context is not None:
            return

        try:
            from playwright.sync_api import (
                sync_playwright,  # type: ignore[import-not-found, import-untyped]
            )

            self.playwright = sync_playwright().start()
            global ACTIVE_AUTOMATOR
            ACTIVE_AUTOMATOR = self

            if self.use_host_chrome:
                # Chrome loses remote control whenever it's reopened normally;
                # running natively we can put it back instead of failing.
                if not IN_DOCKER and not cdp_ready():
                    ok, msg = ensure_chrome()
                    log_event(msg, level="INFO" if ok else "ERROR", category="Auto-Apply")
                if self._connect_host_chrome():
                    self.page.set_default_navigation_timeout(30000)
                    self.page.set_default_timeout(10000)
                    self._update_agent(step="starting", message="Opened its Chrome window")
                    return
                if not IN_DOCKER:
                    raise RuntimeError(
                        "Couldn't take control of Chrome - use 'Reconnect Chrome' on the "
                        "dashboard or rerun start.py"
                    )

            # Prefer installed Google Chrome
            chrome_path = "/usr/bin/google-chrome"
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--start-maximized",
            ]

            if self.use_persistent_profile:
                self.profile_dir.mkdir(parents=True, exist_ok=True)
                clean_stale_chrome_locks(self.profile_dir)
                kwargs: dict[str, Any] = {
                    "user_data_dir": str(self.profile_dir),
                    "headless": self.headless,
                    "args": launch_args,
                    "viewport": {"width": 1280, "height": 900}
                    if not self.headless
                    else {"width": 1920, "height": 1080},
                }
                if Path(chrome_path).exists():
                    kwargs["executable_path"] = chrome_path

                try:
                    self.context = self.playwright.chromium.launch_persistent_context(
                        **kwargs
                    )
                except Exception as pe:
                    print(
                        f"Notice: Persistent browser launch failed ({pe}). Cleaning locks and retrying..."
                    )
                    clean_stale_chrome_locks(self.profile_dir)
                    time.sleep(1)
                    # Retry with persistent context to preserve authenticated cookies
                    self.context = self.playwright.chromium.launch_persistent_context(
                        **kwargs
                    )

                self.page = (
                    self.context.pages[0]
                    if self.context.pages
                    else self.context.new_page()
                )
                self._inject_plaintext_cookies()
            else:
                kwargs = {
                    "headless": self.headless,
                    "args": launch_args,
                }
                if Path(chrome_path).exists():
                    kwargs["executable_path"] = chrome_path

                self.browser = self.playwright.chromium.launch(**kwargs)
                self.context = self.browser.new_context(
                    viewport={"width": 1280, "height": 900}
                    if not self.headless
                    else {"width": 1920, "height": 1080},
                    user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                )
                self.page = self.context.new_page()
                self._inject_plaintext_cookies()

            # Set sensible navigation timeouts
            self.page.set_default_navigation_timeout(30000)
            self.page.set_default_timeout(10000)

        except Exception as e:
            print(f"Error starting Playwright browser: {e}")
            self.close()
            raise

    def close(self) -> None:
        """Closes browser and stops Playwright engine."""
        try:
            if self.using_host_chrome:
                # Never tear down the automation Chrome itself - close the
                # tabs this run opened (including any the Apply button spawned)
                # and its window, then detach. Anything left behind would sit
                # there holding a logged-in LinkedIn page open for nothing.
                for page in reversed(self._own_pages):
                    try:
                        page.close()
                    except Exception:
                        pass
                self._own_pages = []
                if self.target_id:
                    close_target(self.target_id)
                if self.playwright:
                    self.playwright.stop()
                return
            if self.context:
                self.context.close()
            if self.browser:
                self.browser.close()
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        finally:
            with AGENTS_LOCK:
                AGENTS.pop(self.agent_id, None)
            agent_frame_path(self.agent_id).unlink(missing_ok=True)
            self.context = None
            self.browser = None
            self.playwright = None
            self.page = None
            global ACTIVE_AUTOMATOR
            if ACTIVE_AUTOMATOR is self:
                ACTIVE_AUTOMATOR = None

    def detect_platform(self, url: str) -> str:
        """Detects the underlying ATS or job board from URL."""
        url_lower = url.lower()
        if "greenhouse.io" in url_lower or "grnh.se" in url_lower:
            return "Greenhouse"
        if "lever.co" in url_lower:
            return "Lever"
        if "myworkdayjobs.com" in url_lower or "workday" in url_lower:
            return "Workday"
        if "indeed.com" in url_lower:
            return "Indeed"
        if "linkedin.com" in url_lower:
            return "LinkedIn"
        if "ashbyhq.com" in url_lower:
            return "Ashby"
        if "smartrecruiters.com" in url_lower:
            return "SmartRecruiters"
        return "Generic"

    def navigate_and_open_form(self, job_url: str) -> bool:
        """
        Navigates to job URL and attempts to click 'Apply' / 'Apply now' if on an overview page.
        """
        if not self.page:
            self.start()
        self.last_apply_kind = None
        self.last_job_inactive = ""
        self.last_excluded = ""

        if is_excluded_application_url(job_url):
            self._notify(
                "skipped",
                f"Skipping excluded platform (Workday) for {job_url}",
                level="WARN",
            )
            return False

        print(f" Navigating to: {job_url}")
        try:
            self.page.goto(job_url, wait_until="domcontentloaded")
            time.sleep(3)
        except Exception as e:
            print(f" Navigation error: {e}")
            return False

        # Check if Cloudflare challenge is present
        if is_cloudflare_challenge(self.page):
            solve_cloudflare_turnstile(self.page)

        # Dismiss modal overlays and cookie consent banners
        self._dismiss_overlays()

        # Read the apply button before clicking it: the search-time Easy Apply
        # filter is only a hint, this is the real answer for this posting.
        self.last_apply_kind = self._linkedin_apply_kind()

        # Check if page indicates the job is closed or expired
        try:
            page_html = self.page.content()
            active_ok, active_reason = is_job_active(job_url, html=page_html)
            if not active_ok:
                print(f"⚠️ Page indicates job is not active: {active_reason}")
                self.last_job_inactive = active_reason
                self._notify(
                    "job_expired",
                    f"Job posting is inactive: {active_reason}",
                    level="WARN",
                )
                return False
        except Exception:
            pass

        # If it opened a new tab/popup upon clicking an apply button
        initial_pages_count = len(self.context.pages)

        # Check for common "Apply" / "Easy Apply" / "Apply on company website" buttons
        apply_selectors = [
            # Romanian Platforms (BestJobs, eJobs, Hipo, Undelucram)
            "button:has-text('Aplică & Începe interviul')",
            "a:has-text('Aplică & Începe interviul')",
            "button:has-text('Aplică extern')",
            "a:has-text('Aplică extern')",
            "button:has-text('Aplică acum')",
            "a:has-text('Aplică acum')",
            "button:has-text('Aplică')",
            "a:has-text('Aplică')",
            "button:has-text('Aplica')",
            "a:has-text('Aplica')",
            "button:has-text('Trimite CV')",
            "a:has-text('Trimite CV')",
            "[data-test*='apply']",
            # Standard English & International ATS
            "#indeedApplyButton",
            "[id*='indeedApply']",
            "button:has-text('Apply now')",
            "a:has-text('Apply now')",
            "button:has-text('Apply on company website')",
            "a:has-text('Apply on company website')",
            "a:has-text('Apply Now')",
            "button:has-text('Apply Now')",
            "button:has-text('Easy Apply')",
            "a:has-text('Apply for this job')",
            "button:has-text('Apply for this job')",
            "a[href*='apply']:not([href*='login']):not([href*='help'])",
            "a:has-text('Apply')",
            "button:has-text('Apply')",
            "[data-automation-id='adventureButton']",
            "[data-automation-id='applyButton']",
        ]

        # Only click apply if we are not already on an application form page.
        # A bare `form` selector false-positives on LinkedIn's own top-nav job
        # search form, which is present on every job listing page whether or
        # not Easy Apply has been opened — so this must require something an
        # actual application form has: a resume upload field, or real form
        # controls inside a modal/dialog (how Easy Apply and most ATS present
        # their forms).
        has_form = (
            self.page.locator(
                "input[type=file], "
                "[role='dialog'] input, [role='dialog'] textarea, [role='dialog'] select, "
                "dialog input, dialog textarea, dialog select, "
                ".jobs-easy-apply-modal"
            ).count()
            > 0
        )
        if not has_form:
            clicked = False
            for selector in apply_selectors:
                try:
                    matches = self.page.locator(selector)
                    # LinkedIn (and others) render hidden duplicate/placeholder
                    # buttons matching the same text before the real one, so
                    # `.first` alone can silently lock onto an invisible match
                    # and never reach the actual clickable button. Check every
                    # match for the first one that's genuinely visible.
                    match_count = min(matches.count(), 5)
                    for i in range(match_count):
                        candidate = matches.nth(i)
                        if candidate.is_visible(timeout=800):
                            print(
                                f" Clicking '{selector}' (match {i}) to open application form..."
                            )
                            candidate.click()
                            time.sleep(3)
                            clicked = True
                            break
                    if clicked:
                        # Check if a new tab was opened
                        if len(self.context.pages) > initial_pages_count:
                            self.page = self.context.pages[-1]
                            self._own_pages.append(self.page)
                            self._install_kill_switch(self.page)
                            self.page.bring_to_front()
                            time.sleep(2)
                        break
                except Exception:
                    continue

        # Apply can hand off to a platform we don't handle - the job URL looked
        # fine, the destination doesn't.
        if is_excluded_application_url(self.page.url):
            self.last_excluded = f"Application continues on {self.detect_platform(self.page.url)}"
            self._notify("skipped", self.last_excluded, level="WARN")
            return False

        return True

    def _linkedin_apply_kind(self) -> str | None:
        """'easy' when the posting shows LinkedIn's Easy Apply button,
        'external' when its Apply button goes to the company's own site, None
        when this isn't a LinkedIn job page or no apply button is visible."""
        if self.detect_platform(self.page.url) != "LinkedIn":
            return None
        checks = (
            ("easy", "button.jobs-apply-button[aria-label*='Easy Apply'], button:has-text('Easy Apply')"),
            ("external", "button.jobs-apply-button, a.jobs-apply-button, [aria-label*='company website']"),
        )
        for kind, sel in checks:
            try:
                buttons = self.page.locator(sel)
                for i in range(min(buttons.count(), 5)):
                    if buttons.nth(i).is_visible(timeout=500):
                        return kind
            except Exception:
                continue
        return None

    def _record_apply_kind(self, app_dir: Path) -> None:
        """Corrects the job's Easy Apply marker with what the page showed, so
        the dashboard's Easy Apply filter reflects each real posting."""
        kind = getattr(self, "last_apply_kind", None)
        if kind is None:
            return
        try:
            (app_dir / "EASY_APPLY.txt").write_text(
                "true" if kind == "easy" else "false", encoding="utf-8"
            )
        except OSError:
            pass

    def _form_root(self) -> Any:
        """Returns the Easy Apply modal/dialog as the scope for form-filling
        when one is open, else falls back to the whole page. Without this,
        field-filling selectors like a generic 'input:not([type=hidden])...'
        can reach OUTSIDE the modal and hit LinkedIn's own page chrome (e.g.
        the global top-nav search box), typing garbage into unrelated site
        controls instead of the actual application form.
        """
        # The Easy Apply form first, then any other visible dialog - LinkedIn
        # keeps unrelated hidden dialogs in the DOM, so "the first
        # [role=dialog]" is often not the application form. Its newer UI uses
        # a plain <dialog> with none of the old classes or roles.
        for sel in (
            ".jobs-easy-apply-modal, [data-test-modal-id='easy-apply-modal'], dialog[open]",
            "[role='dialog'], dialog",
        ):
            try:
                candidates = self.page.locator(sel)
                for i in range(min(candidates.count(), 6)):
                    dialog = candidates.nth(i)
                    if dialog.is_visible(timeout=300):
                        return dialog
            except Exception:
                continue

        # LinkedIn's Easy Apply is always a modal. If no dialog is open there
        # is no form to read, and scanning the page instead would pick up
        # LinkedIn's own chrome (top-nav search, feed composer). Hand back an
        # empty scope so callers find nothing rather than the wrong thing.
        if self.detect_platform(self.page.url) == "LinkedIn":
            return self.page.locator("[role='dialog'], dialog, .jobs-easy-apply-modal")

        # Other ATS (Greenhouse, Lever, Workday) render full-page forms.
        return self.page

    def _dismiss_overlays(self) -> None:
        """Dismisses modal overlays (LinkedIn sign-in prompts) and cookie banners."""
        try:
            # Remove modal scrims and sign-in containers that intercept clicks
            self.page.evaluate("""() => {
                document.querySelectorAll('.top-level-modal-container, .modal__overlay, .contextual-sign-in-modal, [data-test-modal-id]').forEach(el => {
                    // Never delete the application form itself.
                    if (el.matches('.jobs-easy-apply-modal, [data-test-modal-id="easy-apply-modal"]')
                        || el.querySelector('.jobs-easy-apply-modal, [data-test-modal-id="easy-apply-modal"]')) return;
                    el.remove();
                });
            }""")
        except Exception:
            pass

        # Click accept on cookie consent dialogs if present
        for sel in [
            "button#onetrust-accept-btn-handler",
            "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
            "button:has-text('Accept all')",
            "button:has-text('Accept All')",
            "button:has-text('Accept Cookies')",
            "button:has-text('Accept')",
            "button:has-text('I agree')",
        ]:
            try:
                btn = self.page.locator(sel).first
                if btn.is_visible(timeout=300):
                    btn.click()
                    time.sleep(0.2)
                    break
            except Exception:
                pass

    def _fill_location_autocomplete(self, report: dict[str, Any]) -> None:
        """Fills location autocomplete inputs (e.g. Greenhouse #candidate-location)."""
        city = (self.profile.city or "").strip()
        if not city:
            return

        loc_selectors = [
            "#candidate-location",
            "#job_application_location",
            "input[name*='location']",
            "input[id*='location']",
        ]
        for sel in loc_selectors:
            try:
                inp = self._form_root().locator(sel).first
                if inp.is_visible(timeout=500):
                    val = inp.input_value() or ""
                    if len(val.strip()) < 2:
                        inp.click()
                        inp.fill(city)
                        time.sleep(0.8)
                        # Suggestions are often rendered in a floating listbox
                        # outside the form, so look there rather than anywhere
                        # on the page that happens to mention the city.
                        opt = self.page.get_by_role("option", name=city).first
                        if opt.is_visible(timeout=1000):
                            opt.click()
                        else:
                            self.page.keyboard.press("ArrowDown")
                            self.page.keyboard.press("Enter")
                        report["fields_filled"].append("Location Autocomplete")
                        break
            except Exception:
                pass

    def _fill_combobox_inputs(
        self, report: dict[str, Any], job_title: str, company: str
    ) -> None:
        """Fills custom React-Select / ARIA Combobox dropdowns (e.g. Greenhouse custom questions)."""
        try:
            combos = self._form_root().locator("input[role='combobox']").all()
            for c in combos:
                try:
                    if not c.is_visible(timeout=300):
                        continue
                    val = c.input_value() or ""
                    if len(val.strip()) > 2:
                        continue

                    label_text = ""
                    try:
                        parent = c.locator(
                            "xpath=ancestor::div[contains(@class, 'field') or contains(@class, 'remix')]"
                        ).first
                        label_text = (
                            parent.inner_text(timeout=500).split("\n")[0].strip()
                        )
                    except Exception:
                        label_text = self._get_element_descriptor(c)

                    if not label_text or len(label_text) < 3:
                        continue

                    ans = self.question_solver.answer_question(
                        label_text,
                        job_title=job_title,
                        company=company,
                    )
                    if ans:
                        c.click()
                        c.fill(ans)
                        time.sleep(0.2)
                        self.page.keyboard.press("Enter")
                        time.sleep(0.2)
                        report["fields_filled"].append(f"Combobox: {label_text[:25]}")
                except Exception:
                    continue
        except Exception:
            pass

    def _get_target_scopes(self) -> list[Any]:
        """Returns the main page plus any child frames to find inputs in iframes."""
        scopes = [self.page]
        try:
            for frame in self.page.frames:
                if frame != self.page.main_frame:
                    scopes.append(frame)
        except Exception:
            pass
        return scopes

    def fill_application_form(
        self,
        app_dir: Path,
        job_title: str = "",
        company: str = "",
    ) -> dict[str, Any]:
        """
        Fills form inputs (text, email, tel, select, file uploads, textareas)
        with candidate information, CV PDF, and cover letter.
        """
        if not self.page:
            self.start()

        # Check if Cloudflare challenge is present before filling
        if is_cloudflare_challenge(self.page):
            solve_cloudflare_turnstile(self.page)

        platform = self.detect_platform(self.page.url)
        report: dict[str, Any] = {
            "fields_filled": [],
            "resume_uploaded": False,
            "cover_letter_filled": False,
            "questions_answered": [],
            "needs_review": [],
            "platform": platform,
        }

        # Give the solver this posting's text so open-ended questions are
        # answered against the real role rather than generically.
        jd_file = app_dir / "job_description.txt"
        if jd_file.exists():
            try:
                with open(jd_file, encoding="utf-8") as f:
                    self.question_solver.job_description = f.read()
            except Exception:
                pass

        # 1. Locate CV PDF and Cover Letter. On LinkedIn, always use the
        # candidate's real named resume rather than the per-job generated
        # one - the generated CV is only a tailoring fallback.
        own_resume = candidate_resume_path()
        if platform == "LinkedIn" and own_resume:
            cv_pdf = own_resume
        else:
            cv_pdf = next(app_dir.glob("CV_*.pdf"), None)
        cover_letter_file = app_dir / "cover_letter.txt"
        cover_letter_text = ""
        if cover_letter_file.exists():
            try:
                with open(cover_letter_file, encoding="utf-8") as f:
                    cover_letter_text = f.read().strip()
            except Exception:
                pass

        # 2. Upload Resume PDF
        if cv_pdf and cv_pdf.exists():
            report["resume_uploaded"] = self._upload_resume(str(cv_pdf.resolve()))

        # 3. Fill text, email, tel, url, and textarea inputs
        self._fill_standard_inputs(report, cover_letter_text)

        # 4. Fill location autocomplete (Greenhouse #candidate-location)
        self._fill_location_autocomplete(report)

        # 5. Fill custom React-Select / ARIA comboboxes
        self._fill_combobox_inputs(report, job_title, company)

        # 6. Fill dropdowns / select elements
        self._fill_select_inputs(report, job_title, company)

        # 5. If 0 fields filled, check if an unclicked Apply button is present
        if len(report["fields_filled"]) == 0:
            apply_buttons = [
                "#indeedApplyButton",
                "[id*='indeedApply']",
                "button:has-text('Apply now')",
                "a:has-text('Apply now')",
                "a:has-text('Apply on company website')",
                "button:has-text('Apply on company website')",
                "button:has-text('Apply for this job')",
                "a:has-text('Apply for this job')",
                "button:has-text('Apply')",
                "a:has-text('Apply')",
            ]
            clicked_fallback = False
            for btn_sel in apply_buttons:
                try:
                    matches = self.page.locator(btn_sel)
                    match_count = min(matches.count(), 5)
                    for i in range(match_count):
                        candidate = matches.nth(i)
                        if candidate.is_visible(timeout=800):
                            print(
                                f" Found unclicked apply button '{btn_sel}' (match {i}). Clicking to open form..."
                            )
                            candidate.click()
                            time.sleep(3)
                            self._fill_standard_inputs(report, cover_letter_text)
                            self._fill_select_inputs(report, job_title, company)
                            if not report["resume_uploaded"] and cv_pdf and cv_pdf.exists():
                                report["resume_uploaded"] = self._upload_resume(
                                    str(cv_pdf.resolve())
                                )
                            clicked_fallback = True
                            break
                    if clicked_fallback:
                        break
                except Exception:
                    continue

        # 6. Answer screening questions
        self._answer_custom_questions(report, job_title, company)
        self._answer_radio_groups(report, job_title, company)

        # 7. Check common consent / agreement checkboxes
        self._check_consent_boxes()

        # 8. Inject visual highlight / banner in the browser
        self._inject_autofill_indicator(
            len(report["fields_filled"]), report["resume_uploaded"]
        )

        return report

    def _use_existing_resume(self, name: str) -> bool:
        """Picks a resume already saved on the account.

        LinkedIn keeps every uploaded file, so uploading on each application
        fills the profile with copies of the same CV.
        """
        try:
            root = self._form_root()
            if root.locator(f"[aria-label*='Deselect resume {name}']").count():
                return True  # already the chosen one
            for selector in (
                f"[aria-label*='Select resume {name}']",
                f"label:has-text('{name}')",
            ):
                option = root.locator(selector)
                for i in range(min(option.count(), 5)):
                    if option.nth(i).is_visible(timeout=500):
                        option.nth(i).click()
                        time.sleep(1)
                        return True
        except Exception:
            pass
        return False

    def _upload_resume(self, cv_path: str) -> bool:
        """Attaches the CV, reusing the copy already on the account."""
        name = Path(cv_path).name
        if self._use_existing_resume(name):
            print(f" Using the resume already on the account: {name}")
            return True
        try:
            file_inputs = self._form_root().locator("input[type=file]")
            if file_inputs.count() > 0:
                print(f" Uploading resume: {name}")
                file_inputs.first.set_input_files(cv_path)
                time.sleep(1)
                return True
        except Exception as e:
            print(f" Notice: Resume upload failed or input not standard: {e}")
        return False

    def _fill_standard_inputs(
        self, report: dict[str, Any], cover_letter_text: str
    ) -> None:
        """Fills text, email, phone, link, and textarea fields."""
        inputs = self._form_root().locator(
            "input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=file]):not([type=checkbox]):not([type=radio]):not([type=password]), textarea"
        )
        count = inputs.count()

        for i in range(count):
            try:
                el = inputs.nth(i)
                if not el.is_visible():
                    continue
                if self._is_search_box(el):
                    continue

                # Get element metadata
                name_attr = (el.get_attribute("name") or "").lower()
                id_attr = (el.get_attribute("id") or "").lower()
                placeholder = (el.get_attribute("placeholder") or "").lower()
                aria_label = (el.get_attribute("aria-label") or "").lower()
                data_auto = (el.get_attribute("data-automation-id") or "").lower()
                autocomplete = (el.get_attribute("autocomplete") or "").lower()

                # Get associated label text
                label_text = ""
                try:
                    # Check parent or preceding label
                    label_text = el.evaluate("""el => {
                        const lbl = el.labels && el.labels[0] ? el.labels[0].innerText : "";
                        const aria = el.getAttribute('aria-label') || "";
                        const placeholder = el.getAttribute('placeholder') || "";
                        return (lbl + " " + aria + " " + placeholder).toLowerCase();
                    }""")
                except Exception:
                    pass

                descriptor = _as_words(
                    f"{name_attr} {id_attr} {placeholder} {aria_label} {data_auto} {autocomplete} {label_text}"
                )

                def has(*words: str) -> bool:
                    return any(re.search(rf"\b{re.escape(w)}\b", descriptor) for w in words)

                # These rules are for short profile fields. A real question
                # ("Please note our salary package...") goes to the question
                # solver - matching words inside it filled salary boxes with
                # the cover letter and "tell us..." fields with a phone number.
                label = self._get_question_label(el)
                if "?" in label or len(label.split()) > 6:
                    continue

                is_cover = has("cover letter", "coverletter", "motivation letter",
                               "additional information", "additional info", "comments")
                current_val = el.input_value() or ""
                if len(current_val) > 2 and not is_cover:
                    # Typed-but-never-picked typeahead text still fails validation.
                    self._pick_typeahead_suggestion(el, current_val)
                    continue

                # Middle name is left empty - it's optional and not ours to invent.
                if has("middle name", "middlename", "middle initial"):
                    continue

                val = None
                field_name = None
                if has("first name", "firstname", "given name", "forename"):
                    val, field_name = self.profile.first_name, "First Name"
                elif has("last name", "lastname", "family name", "surname"):
                    val, field_name = self.profile.last_name, "Last Name"
                elif has("full name", "fullname", "your name") or (
                    has("name") and not has("company", "user", "username", "file",
                                            "org", "organization", "school", "manager", "reference")
                ):
                    val, field_name = self.profile.full_name, "Full Name"
                elif has("email", "e mail"):
                    val, field_name = self.profile.email, "Email"
                elif has("phone", "mobile", "tel", "telephone", "cell", "cellphone"):
                    val, field_name = self.profile.phone, "Phone"
                elif has("linkedin"):
                    val, field_name = self.profile.linkedin_url, "LinkedIn"
                elif has("github"):
                    val, field_name = self.profile.github_url, "GitHub"
                elif has("website", "portfolio", "blog"):
                    val, field_name = self.profile.portfolio_url or self.profile.github_url, "Portfolio / Website"
                elif has("city", "town"):
                    val, field_name = self.profile.city, "City"
                elif has("address", "street"):
                    val, field_name = self.profile.address, "Address"
                elif has("postal", "zip", "pin code", "pincode"):
                    val, field_name = self.profile.postal_code, "Postal Code"
                elif has("company", "employer", "organization", "org"):
                    val, field_name = self.profile.current_company, "Current Company"
                elif has("title", "role", "designation", "headline"):
                    val, field_name = self.profile.current_title, "Current Title"
                elif is_cover and cover_letter_text:
                    val, field_name = cover_letter_text, "Cover Letter"
                    report["cover_letter_filled"] = True

                if val and field_name:
                    el.fill(val)
                    self._pick_typeahead_suggestion(el, val)
                    self._highlight_element(el)
                    report["fields_filled"].append(field_name)

            except Exception:
                continue

    def _fill_select_inputs(
        self, report: dict[str, Any], job_title: str = "", company: str = ""
    ) -> None:
        """Answers dropdown questions like any other question: a known answer
        is selected, an unknown one goes to the Tracker instead of a guess."""
        selects = self._form_root().locator("select")
        for i in range(selects.count()):
            try:
                el = selects.nth(i)
                if not el.is_visible():
                    continue
                if self._is_search_box(el):
                    continue

                options = [o.strip() for o in el.locator("option").all_inner_texts()]
                choices = [o for o in options if o and not PLACEHOLDER_OPTION.match(o)]
                if not choices:
                    continue
                selected = el.evaluate(
                    "s => s.selectedIndex >= 0 ? s.options[s.selectedIndex].text.trim() : ''"
                )
                if selected in choices:
                    continue  # already answered (LinkedIn prefills some)

                question = self._get_question_label(el) or self._get_element_descriptor(el)
                answer = self.question_solver.answer_question(
                    question, options=choices, job_title=job_title, company=company
                )
                if answer in choices:
                    el.select_option(label=answer)
                    self._highlight_element(el)
                    report["fields_filled"].append(f"Dropdown: {question[:30]}")
                    report["questions_answered"].append(
                        {"question": question[:50], "answer": answer}
                    )
                else:
                    record_pending(
                        question,
                        options=choices,
                        company=company,
                        job_title=job_title,
                        required=self._is_required(el),
                        kind="select",
                    )
                    report.setdefault("needs_review", []).append(question[:120])
                    self._highlight_element(el, color="#ff9800")
            except Exception:
                continue

    def _answer_custom_questions(
        self, report: dict[str, Any], job_title: str, company: str
    ) -> None:
        """Detects remaining unfilled fields and uses QuestionSolver to answer them."""
        unfilled_inputs = self._form_root().locator(
            "input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=file]):not([type=checkbox]):not([type=radio]):not([type=password]), textarea"
        )
        count = unfilled_inputs.count()

        for i in range(count):
            try:
                el = unfilled_inputs.nth(i)
                if not el.is_visible():
                    continue
                if self._is_search_box(el):
                    continue

                val = el.input_value() or ""
                invalid = el.get_attribute("aria-invalid") == "true"
                if val.strip() and not invalid:
                    continue  # already answered, and the site accepted it

                label_text = self._get_element_descriptor(el)
                if len(label_text.strip()) > 5:
                    answer = self.question_solver.answer_question(
                        self._get_question_label(el) or label_text,
                        job_title=job_title,
                        company=company,
                    )
                    if answer:
                        el.fill(answer)
                        self._pick_typeahead_suggestion(el, answer)
                        self._highlight_element(el)
                        report["questions_answered"].append(
                            {"question": label_text[:50], "answer": answer}
                        )
                        report["fields_filled"].append(f"Question: {label_text[:30]}")
                    else:
                        # Genuinely unknown - leave it unfilled and flag it
                        # rather than guessing, so the candidate can answer
                        # it themselves before submitting.
                        clean_label = self._get_question_label(el) or label_text
                        record_pending(
                            clean_label,
                            company=company,
                            job_title=job_title,
                            required=self._is_required(el),
                            kind=self._field_kind(el),
                        )
                        report.setdefault("needs_review", []).append(
                            clean_label[:120]
                        )
                        self._highlight_element(el, color="#ff9800")

            except Exception:
                continue

    def _radio_groups(self) -> list[dict[str, Any]]:
        """Reads every radio-button question on the current step as
        {question, required, options:[{value,label}], name}."""
        try:
            return self._form_root().evaluate("""root => {
                const radios = Array.from(root.querySelectorAll("input[type=radio]"));
                const groups = {};
                for (const r of radios) {
                    const name = r.getAttribute('name') || '';
                    if (!name) continue;
                    if (!groups[name]) {
                        // The question lives anywhere but on the radio itself.
                        // A legend is the tidy case; LinkedIn often uses a
                        // plain div with the text in a span alongside, and
                        // missing it meant the question never got asked.
                        const fs = r.closest('fieldset');
                        const text = el => (el && el.innerText ? el.innerText.trim() : '');
                        const byId = id => text(id ? document.getElementById(id) : null);
                        const group = r.closest("[role=radiogroup], [data-test-form-builder-radio-button-form-component], fieldset");
                        // What the options say, so option text is never
                        // mistaken for the question - that turned real
                        // screeners into "Male Female I prefer not to specify".
                        const optionText = new Set();
                        if (group) {
                            for (const o of group.querySelectorAll("input[type=radio]")) {
                                const ol = o.labels && o.labels[0] ? text(o.labels[0]) : '';
                                if (ol) optionText.add(ol.toLowerCase());
                            }
                        }
                        const looksLikeQuestion = t => {
                            const v = (t || '').replace(/\\s+/g, ' ').trim();
                            if (v.length < 12) return '';
                            const low = v.toLowerCase();
                            if (optionText.has(low)) return '';
                            // Nothing but options strung together is not a question.
                            let rest = low;
                            for (const o of optionText) rest = rest.split(o).join(' ');
                            if (rest.replace(/[^a-z0-9]/g, '').length < 10) return '';
                            return v;
                        };
                        let q = '';
                        if (fs) q = looksLikeQuestion(text(fs.querySelector('legend')));
                        if (!q) q = looksLikeQuestion(byId(r.getAttribute('aria-labelledby')));
                        if (!q && fs) q = looksLikeQuestion(byId(fs.getAttribute('aria-labelledby')));
                        if (!q && group) {
                            q = looksLikeQuestion(byId(group.getAttribute('aria-labelledby')))
                                || looksLikeQuestion(group.getAttribute('aria-label'));
                        }
                        if (!q && group) {
                            // Whatever sits above the options, inside or just
                            // before the group, is normally the question.
                            let scope = group;
                            for (let up = 0; up < 3 && scope && !q; up++) {
                                for (const el of scope.querySelectorAll('legend,h2,h3,h4,p,span')) {
                                    if (el.querySelector("input[type=radio]")) continue;
                                    const candidate = looksLikeQuestion(text(el));
                                    if (candidate) { q = candidate; break; }
                                }
                                if (!q && scope.previousElementSibling) {
                                    q = looksLikeQuestion(text(scope.previousElementSibling));
                                }
                                scope = scope.parentElement;
                            }
                        }
                        if (!q) q = looksLikeQuestion(r.getAttribute('aria-label'));
                        groups[name] = {
                            name: name,
                            question: q.trim(),
                            required: /\\*/.test(q) || r.required
                                || r.getAttribute('aria-required') === 'true',
                            options: [],
                            answered: false,
                        };
                    }
                    const lbl = (r.labels && r.labels[0]) ? r.labels[0].innerText.trim() : '';
                    groups[name].options.push({
                        value: r.value || lbl,
                        label: lbl || r.value || '',
                    });
                    if (r.checked) groups[name].answered = true;
                }
                return Object.values(groups);
            }""")
        except Exception:
            return []

    def _answer_radio_groups(
        self, report: dict[str, Any], job_title: str, company: str
    ) -> None:
        """Answers radio questions (Yes/No screeners), recording any it can't
        answer so the candidate can supply them from the Tracker page."""
        for group in self._radio_groups():
            try:
                if group.get("answered"):
                    continue
                question = (group.get("question") or "").strip()
                if len(question) < 5:
                    continue
                option_labels = [
                    o["label"] for o in group.get("options", []) if o.get("label")
                ]
                answer = self.question_solver.answer_question(
                    question,
                    options=option_labels,
                    job_title=job_title,
                    company=company,
                )
                if not answer:
                    record_pending(
                        question,
                        options=option_labels,
                        company=company,
                        job_title=job_title,
                        required=bool(group.get("required")),
                        kind="radio",
                    )
                    report.setdefault("needs_review", []).append(question[:120])
                    continue

                radios = self._form_root().locator(
                    f"input[type=radio][name='{group['name']}']"
                )
                for i in range(radios.count()):
                    radio = radios.nth(i)
                    label = self._get_element_descriptor(radio)
                    value = (radio.get_attribute("value") or "").lower()
                    if answer.lower() in label or answer.lower() == value:
                        radio.check()
                        report["questions_answered"].append(
                            {"question": question[:50], "answer": answer}
                        )
                        report["fields_filled"].append(f"Question: {question[:30]}")
                        break
            except Exception:
                continue

    def _check_consent_boxes(self) -> None:
        """Checks required consent or agreement checkboxes (e.g. Privacy policy)."""
        try:
            checkboxes = self._form_root().locator("input[type=checkbox]")
            count = checkboxes.count()
            for i in range(count):
                cb = checkboxes.nth(i)
                if cb.is_visible() and not cb.is_checked():
                    desc = self._get_element_descriptor(cb)
                    if any(
                        w in desc
                        for w in [
                            "agree",
                            "consent",
                            "terms",
                            "policy",
                            "privacy",
                            "acknowledge",
                        ]
                    ):
                        cb.check()
        except Exception:
            pass

    def _field_kind(self, el: Any) -> str:
        """Which control a field is, so the Tracker can ask for the answer the
        same way the site does instead of always offering a text box."""
        try:
            return str(
                el.evaluate(
                    "e => e.tagName === 'TEXTAREA' ? 'textarea'"
                    " : e.tagName === 'SELECT' ? 'select'"
                    " : (e.getAttribute('type') || 'text').toLowerCase()"
                )
                or "text"
            )
        except Exception:
            return "text"

    def _is_search_box(self, el: Any) -> bool:
        """A site's search box is not an application question - it only gets
        in the way on ATS pages, which have no form popup to scope to."""
        try:
            return bool(el.evaluate("""el => {
                const type = (el.getAttribute('type') || '').toLowerCase();
                const role = (el.getAttribute('role') || '').toLowerCase();
                const text = ((el.labels && el.labels[0] ? el.labels[0].innerText : '') + ' '
                    + (el.getAttribute('aria-label') || '') + ' ' + (el.placeholder || '')
                    + ' ' + (el.name || '') + ' ' + (el.id || '')).toLowerCase();
                return type === 'search' || role === 'searchbox' || /\\bsearch\\b/.test(text);
            }"""))
        except Exception:
            return False

    def _get_element_descriptor(self, el: Any) -> str:
        """Extracts text, label, aria, and id descriptor for an element."""
        try:
            return el.evaluate("""el => {
                const lbl = el.labels && el.labels[0] ? el.labels[0].innerText : "";
                const aria = el.getAttribute('aria-label') || "";
                const placeholder = el.getAttribute('placeholder') || "";
                const name = el.getAttribute('name') || "";
                const id = el.getAttribute('id') || "";
                return (lbl + " " + aria + " " + placeholder + " " + name + " " + id).toLowerCase();
            }""")
        except Exception:
            return ""

    def _suggestion_list(self) -> Any:
        """The typeahead's suggestions, wherever the site renders them."""
        inside = self._form_root().locator(SUGGESTION_SELECTOR)
        try:
            if inside.count():
                return inside
        except Exception:
            pass
        return self.page.locator("[role='listbox'] [role='option'], .pac-item")

    def _wait_for_suggestions(self, timeout: float = 4000) -> Any | None:
        try:
            options = self._suggestion_list()
            options.first.wait_for(state="visible", timeout=timeout)
            return options if options.count() else None
        except Exception:
            return None

    def _pick_typeahead_suggestion(self, el: Any, text: str) -> None:
        """Typeahead fields (LinkedIn's "Location (city)") only accept a value
        chosen from their list; typed text alone is rejected.

        The list answers real keystrokes and sometimes doesn't come at all, so
        this retries with a shorter query and falls back to the keyboard. The
        original value goes back if nothing could be chosen - better a typed
        value than an empty required field.
        """
        try:
            if el.get_attribute("role") != "combobox" and not el.get_attribute("aria-autocomplete"):
                return
        except Exception:
            return

        original = (el.input_value() or "").strip() or text
        # Full text first, then a prefix: a stale or too-specific query
        # ("Bengaluru, Karnataka, India") often returns nothing.
        queries = [text, text.split(",")[0].strip()]
        if len(text) > 5:
            queries.append(text[:5])

        for query in dict.fromkeys(q for q in queries if q):
            try:
                el.click()
                el.press("Control+a")
                el.press("Backspace")
                # Typing key by key is what fires the lookup.
                el.press_sequentially(query, delay=60)
                options = self._wait_for_suggestions()
                if options is None:
                    continue

                options.first.click()
                time.sleep(0.5)
                if (el.input_value() or "").strip():
                    return
            except Exception:
                continue

        try:  # last resort: let the field's own keyboard handling choose
            el.click()
            el.press("ArrowDown")
            el.press("Enter")
            time.sleep(0.3)
            if not (el.input_value() or "").strip():
                el.fill(original)
        except Exception:
            pass

    def _get_question_label(self, el: Any) -> str:
        """Human-readable question text for an input, without the element
        name/id noise that _get_element_descriptor mixes in for matching."""
        try:
            return (
                el.evaluate("""el => {
                const lbl = el.labels && el.labels[0] ? el.labels[0].innerText : '';
                if (lbl && lbl.trim()) return lbl.trim();
                const aria = el.getAttribute('aria-label') || '';
                if (aria.trim()) return aria.trim();
                return el.getAttribute('placeholder') || '';
            }""")
                or ""
            ).strip()
        except Exception:
            return ""

    def _is_required(self, el: Any) -> bool:
        """A question is mandatory when the platform marks it required or its
        visible label ends in the '*' the candidate asked us to look for."""
        try:
            return bool(
                el.evaluate("""el => {
                if (el.required || el.getAttribute('aria-required') === 'true') return true;
                const lbl = el.labels && el.labels[0] ? el.labels[0].innerText : '';
                return /\\*/.test(lbl);
            }""")
            )
        except Exception:
            return False

    def _highlight_element(self, el: Any, color: str = "#43a047") -> None:
        """Applies a colored border highlight. Green (default) marks an
        automated fill; a different color (e.g. orange) can flag a field
        that was left for the candidate to answer manually."""
        try:
            el.evaluate(
                """(el, color) => {
                el.style.backgroundColor = color === '#43a047' ? '#f1f8e9' : '#fff3e0';
                el.style.border = '2px solid ' + color;
            }""",
                color,
            )
        except Exception:
            pass

    def _inject_autofill_indicator(self, filled_count: int, resume_ok: bool) -> None:
        """Injects a floating toast notification in the page."""
        try:
            cf_active = is_cloudflare_challenge(self.page)
            if cf_active:
                ray_id = extract_ray_id(self.page)
                ray_msg = f" (Ray ID: {ray_id})" if ray_id else ""
                msg = f"🛡️ Cloudflare verification required{ray_msg}! Please complete check in browser."
                bg = "#b91c1c"
            elif filled_count == 0:
                msg = "⚡ JobApplier: 0 fields found. Click 'Apply Now' on the page to open the application form!"
                bg = "#b45309"
            else:
                msg = f"⚡ JobApplier: {filled_count} fields autofilled" + (
                    " + Resume attached!" if resume_ok else ""
                )
                bg = "#2e7d32"

            self.page.evaluate(f"""() => {{
                const d = document.createElement('div');
                d.style.position = 'fixed';
                d.style.top = '15px';
                d.style.right = '15px';
                d.style.zIndex = '999999';
                d.style.background = '{bg}';
                d.style.color = '#ffffff';
                d.style.padding = '12px 18px';
                d.style.borderRadius = '6px';
                d.style.fontSize = '14px';
                d.style.fontWeight = 'bold';
                d.style.boxShadow = '0 4px 12px rgba(0,0,0,0.3)';
                d.innerText = "{msg}";
                document.body.appendChild(d);
                setTimeout(() => d.remove(), 8000);
            }}""")
        except Exception:
            pass

    def find_submit_button(self) -> Any | None:
        """Locates the final submit button on the application page."""
        submit_selectors = [
            "button[type=submit]",
            "input[type=submit]",
            "button:has-text('Submit Application')",
            "button:has-text('Submit application')",
            "button:has-text('Submit resume')",
            "button:has-text('Review Application')",
            "button:has-text('Send Application')",
            "button:has-text('Apply')",
            "[data-automation-id='bottom-navigation-next-button']",
        ]
        for sel in submit_selectors:
            try:
                matches = self._form_root().locator(sel)
                match_count = min(matches.count(), 5)
                for i in range(match_count):
                    btn = matches.nth(i)
                    if btn.is_visible(timeout=800):
                        return btn
            except Exception:
                continue
        return None

    def _detect_validation_errors(self) -> list[str]:
        """Detects visible validation error messages on the page."""
        errors = []
        error_selectors = [
            ".error",
            ".field-error",
            ".validation-error",
            "[aria-invalid='true']",
            "div:has-text('This field is required')",
            "span:has-text('This field is required')",
            "p:has-text('This field is required')",
            "div:has-text('Please enter your location')",
            "div:has-text('Please correct the errors')",
        ]
        for sel in error_selectors:
            try:
                loc = self._form_root().locator(sel)
                count = loc.count()
                for i in range(min(count, 5)):
                    if loc.nth(i).is_visible(timeout=200):
                        txt = loc.nth(i).inner_text(timeout=200).strip()
                        if txt and txt not in errors and len(txt) < 80:
                            errors.append(txt)
            except Exception:
                pass
        return errors

    def supply_verification_code(self, code: str) -> bool:
        """Supplies a 2FA or email verification code received externally (e.g. from web UI)."""
        self.provided_code = code.strip()
        self.waiting_for_code = False
        return True

    def _detect_login_wall(self) -> tuple[bool, str]:
        """Detects if the page is currently blocked behind a login or sign-up wall."""
        if not self.page:
            return False, ""
        url = (self.page.url or "").lower()
        if any(
            w in url
            for w in [
                "/login",
                "/signin",
                "/cold-join",
                "/sign-in",
                "accounts.google.com",
            ]
        ):
            return True, "Redirected to platform sign-in page"

        try:
            pwd_inputs = self.page.locator("input[type='password']")
            if pwd_inputs.count() > 0 and pwd_inputs.first.is_visible(timeout=300):
                return True, "Password input visible (account login required)"
        except Exception:
            pass
        return False, ""

    def _detect_verification_challenge(self) -> tuple[bool, Any | None]:
        """Detects if the page is currently asking for an email/SMS verification code or OTP."""
        if not self.page:
            return False, None
        otp_selectors = [
            "input[autocomplete='one-time-code']",
            "input[name*='code' i]:not([name*='postal']):not([name*='zip']):not([name*='country'])",
            "input[name*='otp' i]",
            "input[id*='verification' i]",
            "input[placeholder*='verification' i]",
            "input[placeholder*='code' i]:not([placeholder*='postal']):not([placeholder*='zip'])",
        ]
        for sel in otp_selectors:
            try:
                inp = self.page.locator(sel).first
                if inp.is_visible(timeout=300):
                    return True, inp
            except Exception:
                continue
        return False, None

    def _handle_verification_challenge(
        self, inp_element: Any, timeout: int = 90
    ) -> bool:
        """Prompts for and fills in the email/SMS verification code."""
        self._notify(
            "verification_code_required",
            "🔑 Email/SMS verification code required by application form!",
            level="WARN",
        )
        self.waiting_for_code = True
        self.provided_code = None

        code = ""
        is_interactive = sys.stdin.isatty() and not self.headless

        if is_interactive:
            try:
                print("\n" + "=" * 60)
                print("🔑 VERIFICATION CODE REQUIRED")
                print("   A security code was sent to your email / phone.")
                code = input("   Enter verification code: ").strip()
                print("=" * 60 + "\n")
            except (EOFError, KeyboardInterrupt):
                code = ""
        else:
            print(
                f" Waiting for verification code via API / Web Dashboard (timeout: {timeout}s)..."
            )
            start = time.time()
            while time.time() - start < timeout:
                if self.provided_code:
                    code = self.provided_code
                    break
                time.sleep(1)

        self.waiting_for_code = False

        if code:
            try:
                inp_element.fill(code)
                time.sleep(1)
                for sub in [
                    "button:has-text('Verify')",
                    "button:has-text('Confirm')",
                    "button:has-text('Submit')",
                    "button:has-text('Continuă')",
                    "button:has-text('Verifică')",
                    "button[type=submit]",
                ]:
                    btn = self.page.locator(sub).first
                    if btn.is_visible(timeout=500):
                        btn.click()
                        time.sleep(3)
                        return True
            except Exception as e:
                print(f"Notice filling verification code: {e}")
        return False

    def _verify_submission(self) -> tuple[bool, str]:
        """Checks whether the application submission was genuinely accepted by the site."""
        errors = self._detect_validation_errors()
        if errors:
            return False, f"Validation errors on page: {', '.join(errors[:2])}"

        if is_cloudflare_challenge(self.page):
            return False, "Blocked by Cloudflare verification challenge"

        # Check if an OTP / verification challenge popped up
        has_otp, otp_inp = self._detect_verification_challenge()
        if has_otp and otp_inp:
            solved = self._handle_verification_challenge(otp_inp)
            if solved:
                time.sleep(3)
                return self._verify_submission()
            return False, "Verification code required but not supplied"

        # Check if a login wall intercepted
        is_login, login_msg = self._detect_login_wall()
        if is_login:
            return False, f"Blocked by platform login wall: {login_msg}"

        url_lower = (self.page.url or "").lower()
        if any(
            w in url_lower
            for w in [
                "/confirmation",
                "/thank_you",
                "/thank-you",
                "/submitted",
                "/applied",
                "/success",
            ]
        ):
            return True, "Submission confirmed by redirect to confirmation page"

        try:
            body_text = self.page.locator("body").inner_text(timeout=1000).lower()
            if any(
                w in body_text
                for w in [
                    "application submitted",
                    "thank you for your application",
                    "thank you for applying",
                    "we have received your application",
                    "application received",
                    "aplicația a fost trimisă",
                    "aplicație trimisă",
                ]
            ):
                return True, "Submission confirmed by success message"
        except Exception:
            pass

        submit_btn = self.find_submit_button()
        if submit_btn and submit_btn.is_visible(timeout=500):
            return False, "Form still active with submit button visible"

        return False, "Submission could not be confirmed by destination site"

    def _save_diagnostics(
        self,
        app_dir: Path,
        company: str,
        job_title: str,
        job_url: str,
        report: dict[str, Any],
        reason: str,
    ) -> None:
        """Saves diagnostics JSON and DOM HTML snippet upon submission issues."""
        diag_file = app_dir / "diagnostics.json"
        try:
            with open(diag_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "company": company,
                        "title": job_title,
                        "job_url": job_url,
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "status": "failed",
                        "reason": reason,
                        "platform": report.get("platform", "Unknown"),
                        "fields_filled": report.get("fields_filled", []),
                        "resume_uploaded": report.get("resume_uploaded", False),
                        "validation_errors": self._detect_validation_errors(),
                        # Everything below is what actually makes a failure
                        # diagnosable: where it stopped, what blocked it, and
                        # what the form looked like at that moment.
                        "stopped_at_step": report.get("stopped_at_step"),
                        "steps_completed": report.get("steps_completed"),
                        "questions_answered": report.get("questions_answered", []),
                        "needs_review": report.get("needs_review", []),
                        "inline_errors": self._validation_errors(),
                        "unanswered_required": self._unanswered_required_fields(),
                        "current_url": self.page.url if self.page else "",
                        "form_visible_text": self._form_fingerprint(),
                    },
                    f,
                    indent=2,
                )
        except OSError as err:
            print(f"Notice: Could not save diagnostics.json: {err}")

        dom_file = app_dir / "diagnostic_dom.html"
        try:
            with open(dom_file, "w", encoding="utf-8") as f:
                f.write(self.page.content() if self.page else "")
        except OSError as err:
            print(f"Notice: Could not save diagnostic_dom.html: {err}")

    def _options_for_question(self, label: str) -> list[str]:
        """Choices available for a question, so the Tracker can offer buttons
        (Yes/No) instead of a free-text box."""
        try:
            return (
                self._form_root().evaluate(
                    """(root, label) => {
                const norm = s => (s || '').replace(/\\*/g, ' ').replace(/\\s+/g, ' ').trim().toLowerCase();
                const target = norm(label);
                for (const fs of root.querySelectorAll('fieldset')) {
                    if (norm(fs.innerText).startsWith(target.slice(0, 40))) {
                        return Array.from(fs.querySelectorAll('input[type=radio]'))
                            .map(r => (r.labels && r.labels[0] ? r.labels[0].innerText : r.value) || '')
                            .map(s => s.trim()).filter(Boolean);
                    }
                }
                for (const sel of root.querySelectorAll('select')) {
                    const lbl = sel.labels && sel.labels[0] ? sel.labels[0].innerText : '';
                    if (norm(lbl).startsWith(target.slice(0, 40))) {
                        return Array.from(sel.options).map(o => o.text.trim())
                            .filter(t => t && !/^select/i.test(t));
                    }
                }
                return [];
            }""",
                    label,
                )
                or []
            )
        except Exception:
            return []

    def _unanswered_required_fields(self) -> list[dict[str, Any]]:
        """Every required field on the current step that is still empty - the
        concrete reason a wizard refuses to advance or submit."""
        try:
            return (
                self._form_root().evaluate("""root => {
                const out = [];
                const els = root.querySelectorAll(
                    "input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=file]), textarea, select"
                );
                for (const el of els) {
                    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
                    const lbl = el.labels && el.labels[0] ? el.labels[0].innerText : '';
                    const aria = el.getAttribute('aria-label') || '';
                    // A site search box is not an application question.
                    if (type === 'search' || /\\bsearch\\b/i.test(lbl + ' ' + aria + ' ' + (el.name || '') + ' ' + (el.id || ''))) continue;
                    const required = el.required
                        || el.getAttribute('aria-required') === 'true'
                        || /\\*/.test(lbl);
                    if (!required) continue;
                    let empty;
                    if (type === 'radio' || type === 'checkbox') {
                        const name = el.getAttribute('name');
                        empty = name
                            ? !root.querySelector("input[name='" + name + "']:checked")
                            : !el.checked;
                    } else {
                        empty = !(el.value || '').trim();
                    }
                    if (!empty) continue;
                    // A radio's own label is its option ("Yes"); the question
                    // is the group's legend.
                    const fs = el.closest('fieldset');
                    const legend = fs && fs.querySelector('legend') ? fs.querySelector('legend').innerText : '';
                    const question = (type === 'radio' || type === 'checkbox') && legend ? legend : lbl;
                    const label = (question || aria || '').trim();
                    if (!label) continue;
                    if (!out.some(o => o.label === label)) {
                        out.push({ label: label.slice(0, 160), type: type });
                    }
                }
                return out;
            }""")
                or []
            )
        except Exception:
            return []

    def _click_next(self, selectors: list[str], step: int, before: str = "") -> bool:
        for sel in selectors:
            try:
                # Inside the popup only - a page-wide "Next" also matches
                # carousel arrows and pagination on the job page itself.
                matches = self._form_root().locator(sel)
                for i in range(min(matches.count(), 5)):
                    btn = matches.nth(i)
                    if btn.is_visible(timeout=500):
                        self._notify(
                            "form_detection",
                            f"Advancing to wizard step {step + 2} via '{sel}'...",
                        )
                        btn.click()
                        # Continue as soon as the step changes instead of
                        # always waiting out a fixed pause.
                        deadline = time.monotonic() + 3
                        while time.monotonic() < deadline:
                            time.sleep(0.2)
                            if not before or self._form_fingerprint() != before:
                                break
                        return True
            except Exception:
                continue
        return False

    def _form_fingerprint(self) -> str:
        """The step's field labels and headings - they change when the step
        does, but not when an error message appears on a rejected Next."""
        try:
            return self._form_root().evaluate(
                "root => Array.from(root.querySelectorAll('label, legend, h3'))"
                ".map(e => e.innerText.trim()).join('|') + '#' + Array.from("
                "root.querySelectorAll('progress, [aria-valuenow]'))"
                ".map(e => e.value || e.getAttribute('aria-valuenow')).join(',')"
            ) or ""
        except Exception:
            return ""

    def _validation_errors(self) -> list[str]:
        """Collects the inline error messages the platform is showing on the
        current step (e.g. 'Please make a selection')."""
        errors: list[str] = []
        selectors = [
            ".artdeco-inline-feedback--error",
            "[role='alert']",
            ".fb-dash-form-element__error-field",
        ]
        root = self._form_root()
        for sel in selectors:
            try:
                matches = root.locator(sel)
                for i in range(min(matches.count(), 8)):
                    el = matches.nth(i)
                    if not el.is_visible(timeout=300):
                        continue
                    text = (el.inner_text() or "").strip()
                    if text and text not in errors:
                        errors.append(text[:120])
            except Exception:
                continue
        return errors

    def _advance_multistep_form(
        self,
        app_dir: Path,
        job_title: str = "",
        company: str = "",
        max_steps: int = 8,
    ) -> dict[str, Any]:
        """LinkedIn Easy Apply (and many other ATS) forms are multi-step
        wizards — Contact info -> Resume -> Screening questions -> Review ->
        Submit — not a single page. Fills whatever is visible on the current
        step, then clicks 'Next'/'Continue'/'Review' to advance and fills the
        next step, repeating until no further advance button is found (i.e.
        we've reached the final review/submit step). The actual final
        'Submit application' click is intentionally left to the caller.
        """
        combined: dict[str, Any] = {
            "fields_filled": [],
            "resume_uploaded": False,
            "cover_letter_filled": False,
            "questions_answered": [],
            "needs_review": [],
            "platform": self.detect_platform(self.page.url),
        }
        next_selectors = [
            "button[aria-label='Continue to next step']",
            "button[aria-label='Review your application']",
            "button:has-text('Next')",
            "button:has-text('Continue')",
            # The newer UI labels the last step's button plainly.
            "button:has-text('Review')",
        ]
        for step in range(max_steps):
            step_report = self.fill_application_form(app_dir, job_title, company)
            combined["fields_filled"].extend(step_report["fields_filled"])
            combined["resume_uploaded"] = (
                combined["resume_uploaded"] or step_report["resume_uploaded"]
            )
            combined["cover_letter_filled"] = (
                combined["cover_letter_filled"] or step_report["cover_letter_filled"]
            )
            combined["questions_answered"].extend(step_report["questions_answered"])
            combined["needs_review"].extend(step_report.get("needs_review", []))

            before = self._form_fingerprint()
            if not self._click_next(next_selectors, step, before):
                break

            if before and self._form_fingerprint() == before:
                # Next marks the rejected fields invalid - answer those again
                # (a restored draft can hold a wrong value) and retry once.
                self.fill_application_form(app_dir, job_title, company)
                self._click_next(next_selectors, step, before)

            # Clicking Next doesn't mean the step actually moved: LinkedIn
            # keeps you on the same step and shows an inline error when a
            # required question wasn't answered. Without this check the loop
            # just re-clicks the same blocked button until max_steps.
            if before and self._form_fingerprint() == before:
                combined["stopped_at_step"] = step + 1
                combined["blocked_reason"] = "step_did_not_advance"
                # Name the actual fields holding it up, not just the platform's
                # generic "Please make a selection".
                unanswered = self._unanswered_required_fields()
                blockers = [f["label"] for f in unanswered] or self._validation_errors()
                # Put the blockers in front of the candidate. Without this a
                # question the field handlers never recognised just stalls the
                # run forever with nothing to answer in the Tracker.
                for field in unanswered:
                    options = self._options_for_question(field["label"])
                    record_pending(
                        field["label"],
                        options=options,
                        company=company,
                        job_title=job_title,
                        required=True,
                        kind=field.get("type") or "text",
                    )
                combined["needs_review"].extend(
                    blockers or ["A required question on this step needs your answer"]
                )
                break
            combined["steps_completed"] = step + 1
        return combined

    def run_assisted_apply(
        self,
        app_dir: Path,
        job_url: str,
        company: str,
        job_title: str,
        non_interactive: bool = False,
    ) -> tuple[str, str]:
        """
        Assisted Apply Mode:
        1. Navigates to job URL and opens application form.
        2. Fills all fields, attaches tailored CV PDF, fills cover letter, answers questions.
        3. Highlights submit button and presents a review prompt to the user.
        4. User can confirm submit with [Enter] or submit manually in Chrome.
        """
        print(f"\n🚀 Starting Assisted Auto-Apply for {company} - {job_title}")
        self._update_agent(company=company, title=job_title, app_id=app_dir.name)
        success = self.navigate_and_open_form(job_url)
        self._label_kill_switch(company, job_title)
        self._record_apply_kind(app_dir)
        if not success:
            if self.last_job_inactive:
                return "expired", self.last_job_inactive
            if self.last_excluded:
                return "excluded", self.last_excluded
            return "failed", "Could not navigate to job URL"

        report = self._advance_multistep_form(
            app_dir, job_title=job_title, company=company
        )
        fields_str = ", ".join(report["fields_filled"][:6])
        if len(report["fields_filled"]) > 6:
            fields_str += f" (+{len(report['fields_filled']) - 6} more)"

        print("\n Application Form Autofilled!")
        print(f"  • Platform Detected: {report['platform']}")
        print(
            f"  • Resume Attached:   {' Yes' if report['resume_uploaded'] else '⚠️  Manual upload needed'}"
        )
        print(
            f"  • Cover Letter:      {' Yes' if report['cover_letter_filled'] else ' None'}"
        )
        print(f"  • Fields Filled:     {fields_str or 'None'}")
        if report["questions_answered"]:
            print(
                f"  • AI Questions:      {len(report['questions_answered'])} answered"
            )

        if report.get("needs_review"):
            questions_list = "; ".join(report["needs_review"][:5])
            proof_path = app_dir / "submission_failed.png"
            try:
                self.page.screenshot(path=str(proof_path))
            except Exception:
                pass
            self._notify(
                "needs_review",
                f"Could not confidently answer {len(report['needs_review'])} question(s) for "
                f"{company} - {job_title}, left for manual review: {questions_list}",
                level="WARN",
            )
            record_application(
                company=company,
                title=job_title,
                job_url=job_url,
                status="needs_review",
                platform=report["platform"],
                submission_type="assisted_browser",
                proof_path=str(proof_path) if proof_path.exists() else "",
                notes=f"Unanswered questions: {questions_list}",
            )
            return (
                "needs_review",
                f"{len(report['needs_review'])} question(s) need your input before this can be submitted",
            )

        submit_btn = self.find_submit_button()
        if submit_btn:
            try:
                submit_btn.scroll_into_view_if_needed()
                submit_btn.evaluate("""el => {
                    el.style.outline = '4px solid #ff9800';
                    el.style.boxShadow = '0 0 15px #ff9800';
                }""")
            except Exception:
                pass

        is_interactive = (
            sys.stdin.isatty() and not self.headless and not non_interactive
        )
        if not is_interactive:
            print(" Non-interactive/headless mode: Auto-confirming submit...")
            choice = ""
        else:
            print("\n👉 Chrome is open with the filled application form.")
            print("   Review the fields in your browser window.")
            print("   Options:")
            print("     [Enter] -> Auto-click the Submit button")
            print(
                "     [m]     -> You will submit manually in the browser (mark as applied)"
            )
            print("     [s]     -> Skip this application")

            try:
                choice = input("   Select action ([Enter]/m/s): ").strip().lower()
            except EOFError:
                choice = ""

        if choice == "s":
            record_application(
                company=company,
                title=job_title,
                job_url=job_url,
                status="skipped",
                platform=report["platform"],
                submission_type="assisted_browser",
                notes="Skipped by user during assisted review",
            )
            return "skipped", "User skipped"

        if choice == "" or choice == "enter":
            if submit_btn:
                try:
                    print(" Submitting application...")
                    submit_btn.click()
                    time.sleep(4)
                except Exception as e:
                    print(
                        f"⚠️ Could not auto-click submit: {e}. Please click submit in browser."
                    )

            submitted_ok, verif_msg = self._verify_submission()
            proof_path = app_dir / (
                "submission_proof.png" if submitted_ok else "submission_failed.png"
            )
            try:
                self.page.screenshot(path=str(proof_path))
            except Exception:
                pass

            if submitted_ok or choice == "m":
                self._notify(
                    "submitted",
                    f"Successfully submitted and verified for {company} - {job_title}",
                    level="SUCCESS",
                )
                record_application(
                    company=company,
                    title=job_title,
                    job_url=job_url,
                    status="applied",
                    platform=report["platform"],
                    submission_type="assisted_browser",
                    proof_path=str(proof_path) if proof_path.exists() else "",
                    notes=f"Verified: {verif_msg}",
                )
                return "applied", "Successfully completed assisted apply"
            else:
                self._save_diagnostics(
                    app_dir, company, job_title, job_url, report, verif_msg
                )
                self._notify(
                    "submission_failed",
                    f"Submission blocked for {company} - {job_title}: {verif_msg}",
                    level="WARN",
                )
                record_application(
                    company=company,
                    title=job_title,
                    job_url=job_url,
                    status="failed",
                    platform=report["platform"],
                    submission_type="assisted_browser",
                    proof_path=str(proof_path) if proof_path.exists() else "",
                    notes=f"Unconfirmed: {verif_msg}",
                )
                return "failed", f"Submission unconfirmed: {verif_msg}"

        return "skipped", "User did not submit"

    def run_autonomous_apply(
        self,
        app_dir: Path,
        job_url: str,
        company: str,
        job_title: str,
    ) -> tuple[str, str]:
        """
        Autonomous Apply Mode:
        Completely automated: Navigates -> Fills form -> Attaches CV -> Submits -> Records proof.
        """
        print(f"\n⚡ Starting Autonomous Auto-Apply for {company} - {job_title}")
        self._update_agent(company=company, title=job_title, app_id=app_dir.name)
        success = self.navigate_and_open_form(job_url)
        self._record_apply_kind(app_dir)
        if not success:
            if self.last_job_inactive:
                return "expired", self.last_job_inactive
            if self.last_excluded:
                return "excluded", self.last_excluded
            return "failed", "Could not navigate to job URL"

        report = self._advance_multistep_form(
            app_dir, job_title=job_title, company=company
        )
        time.sleep(2)

        if report.get("needs_review"):
            questions_list = "; ".join(report["needs_review"][:5])
            proof_path = app_dir / "submission_failed.png"
            try:
                self.page.screenshot(path=str(proof_path))
            except Exception:
                pass
            self._notify(
                "needs_review",
                f"Could not confidently answer {len(report['needs_review'])} question(s) for "
                f"{company} - {job_title}, left for manual review: {questions_list}",
                level="WARN",
            )
            record_application(
                company=company,
                title=job_title,
                job_url=job_url,
                status="needs_review",
                platform=report["platform"],
                submission_type="autonomous_browser",
                proof_path=str(proof_path) if proof_path.exists() else "",
                notes=f"Unanswered questions: {questions_list}",
            )
            return (
                "needs_review",
                f"{len(report['needs_review'])} question(s) need your input before this can be submitted",
            )

        submit_btn = self.find_submit_button()
        if not submit_btn:
            # Save screenshot for diagnosis
            fail_shot = app_dir / "submission_failed.png"
            try:
                self.page.screenshot(path=str(fail_shot))
            except Exception:
                pass
            self._save_diagnostics(
                app_dir,
                company,
                job_title,
                job_url,
                report,
                "Submit button could not be located",
            )
            self._notify(
                "button_not_found",
                f"Submit button not found for {company} - {job_title}",
                level="WARN",
            )
            record_application(
                company=company,
                title=job_title,
                job_url=job_url,
                status="failed",
                platform=report["platform"],
                submission_type="autonomous_browser",
                notes="Submit button could not be located",
            )
            return "failed", "Submit button not found"

        try:
            self._notify(
                "submitting",
                f"Auto-submitting application for {company} - {job_title}...",
                level="INFO",
            )
            print(" Auto-submitting application...")
            submit_btn.click()
            time.sleep(5)

            submitted_ok, verif_msg = self._verify_submission()
            proof_path = app_dir / (
                "submission_proof.png" if submitted_ok else "submission_failed.png"
            )
            try:
                self.page.screenshot(path=str(proof_path))
            except Exception:
                pass

            if submitted_ok:
                self._notify(
                    "submitted",
                    f"Verified application submission for {company} - {job_title}",
                    level="SUCCESS",
                )
                record_application(
                    company=company,
                    title=job_title,
                    job_url=job_url,
                    status="applied",
                    platform=report["platform"],
                    submission_type="autonomous_browser",
                    proof_path=str(proof_path) if proof_path.exists() else "",
                    notes=f"Submission verified. Fields filled: {len(report['fields_filled'])}",
                )
                return "applied", f"Successfully auto-submitted ({verif_msg})"
            else:
                self._save_diagnostics(
                    app_dir, company, job_title, job_url, report, verif_msg
                )
                self._notify(
                    "submission_failed",
                    f"Submission unconfirmed for {company} - {job_title}: {verif_msg}",
                    level="WARN",
                )
                record_application(
                    company=company,
                    title=job_title,
                    job_url=job_url,
                    status="failed",
                    platform=report["platform"],
                    submission_type="autonomous_browser",
                    proof_path=str(proof_path) if proof_path.exists() else "",
                    notes=f"Unconfirmed: {verif_msg}",
                )
                return "failed", verif_msg

        except Exception as e:
            record_application(
                company=company,
                title=job_title,
                job_url=job_url,
                status="failed",
                platform=report["platform"],
                submission_type="autonomous_browser",
                notes=f"Error during submission: {e}",
            )
            return "failed", str(e)
