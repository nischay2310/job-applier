"""Checks that a finished agent's Chrome window closes, popups and all."""

from __future__ import annotations

import json
import socket
import urllib.request

import pytest

from job_applier import chrome_launcher
from job_applier.automation.browser_automator import BrowserAutomator

playwright_sync = pytest.importorskip("playwright.sync_api")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def open_pages(port: int) -> list[str]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list") as r:
        return [t["id"] for t in json.load(r) if t["type"] == "page"]


@pytest.fixture
def chrome(monkeypatch: pytest.MonkeyPatch):
    port = free_port()
    monkeypatch.setattr(chrome_launcher, "CDP_PORT", port)
    with playwright_sync.sync_playwright() as pw:
        try:
            chrome_process = pw.chromium.launch(
                args=[f"--remote-debugging-port={port}"]
            )
        except Exception as err:  # browser not installed on this machine
            pytest.skip(f"Chromium not available: {err}")
        # Attach over CDP like the app does, so we share Chrome's own context.
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        yield port, browser
        chrome_process.close()


def test_release_window_closes_every_tab_including_popups(chrome):
    port, browser = chrome
    context = browser.contexts[0]
    context.new_page()  # someone else's tab must survive
    tabs_before = open_pages(port)

    automator = BrowserAutomator()
    automator.browser = browser
    automator.context = context
    automator.using_host_chrome = True
    automator.page = automator._open_agent_window()
    automator.page.set_content("<a href='about:blank' target='_blank'>x</a>")
    # A tab the agent never registered, like one an Apply click spawns.
    automator.page.evaluate("window.open('about:blank')")
    assert len(open_pages(port)) == len(tabs_before) + 2

    automator.release_window()

    assert automator.page is None
    assert sorted(open_pages(port)) == sorted(tabs_before)


def test_chrome_signin_prompt_is_switched_off_in_the_automation_profile(tmp_path):
    import json

    from job_applier.chrome_launcher import turn_off_chrome_signin

    prefs_file = tmp_path / "Default" / "Preferences"
    prefs_file.parent.mkdir(parents=True)
    prefs_file.write_text(json.dumps({"homepage": "x", "signin": {"allowed": True}}))

    assert turn_off_chrome_signin(tmp_path) is True
    saved = json.loads(prefs_file.read_text())
    assert saved["signin"]["allowed"] is False
    assert saved["homepage"] == "x"  # everything else is left alone
    assert turn_off_chrome_signin(tmp_path) is False  # nothing left to change
    assert turn_off_chrome_signin(tmp_path / "missing") is False
