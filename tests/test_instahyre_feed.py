"""Runs the Instahyre feed clicker against a small mock feed in a real browser."""

from __future__ import annotations

import pytest

from job_applier.automation.browser_automator import BrowserAutomator

playwright_sync = pytest.importorskip("playwright.sync_api")

# Mimics what the feed looks like: one card per job, some still open (with
# Interested / Not interested buttons), one already applied, one disabled.
FEED = """
<html><body>
  <div class="job"><h3>Backend Engineer</h3>
    <button class="btn" onclick="respond(this)">Interested</button>
    <button class="btn" onclick="respond(this)">Not Interested</button></div>
  <div class="job"><h3>DevOps Engineer</h3>
    <button class="btn" onclick="respond(this)">Apply</button></div>
  <div class="job"><h3>Old Role</h3><span>Applied</span>
    <button class="btn">Apply</button></div>
  <div class="job"><h3>Closed Role</h3>
    <button class="btn" disabled>Apply</button></div>
  <script>
    window.clicked = [];
    function respond(button) {
      window.clicked.push(button.parentElement.querySelector('h3').textContent
        + ' / ' + button.textContent);
    }
  </script>
</body></html>
"""


@pytest.fixture
def feed_page():
    with playwright_sync.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as err:  # browser not installed on this machine
            pytest.skip(f"Chromium not available: {err}")
        page = browser.new_page()
        page.set_content(FEED)
        yield page
        browser.close()


def test_only_open_jobs_are_applied_to_once(feed_page):
    automator = BrowserAutomator()
    automator.page = feed_page
    automator._notify = lambda *args, **kwargs: None

    report = automator._instahyre_apply_on_feed(max_jobs=30)

    assert feed_page.evaluate("window.clicked") == [
        "Backend Engineer / Interested",
        "DevOps Engineer / Apply",
    ]
    assert report["applied"] == 2


def test_empty_feed_tells_the_user_to_log_in(feed_page):
    feed_page.set_content("<html><body><h1>Welcome to Instahyre</h1></body></html>")
    automator = BrowserAutomator()
    automator.page = feed_page
    messages = []
    automator._notify = lambda step, message, **kwargs: messages.append((step, message))

    report = automator._instahyre_apply_on_feed(max_jobs=30)

    assert report["applied"] == 0
    assert any(step == "login_required" for step, _ in messages)
