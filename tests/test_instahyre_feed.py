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


def test_filters_pick_only_matching_jobs(feed_page):
    automator = BrowserAutomator()
    automator.page = feed_page
    automator._notify = lambda *args, **kwargs: None

    report = automator._instahyre_apply_on_feed(
        max_jobs=30, include=["devops"], exclude=[]
    )

    assert feed_page.evaluate("window.clicked") == ["DevOps Engineer / Apply"]
    assert report["applied"] == 1
    assert any(job.get("reason") == "filtered out" for job in report["jobs"])


def test_max_jobs_stops_after_that_many_applications(feed_page):
    automator = BrowserAutomator()
    automator.page = feed_page
    automator._notify = lambda *args, **kwargs: None

    report = automator._instahyre_apply_on_feed(max_jobs=1)

    assert report["applied"] == 1
    assert len(feed_page.evaluate("window.clicked")) == 1


def test_logged_in_but_nothing_new_is_not_called_a_login_problem(feed_page):
    feed_page.set_content(
        "<html><body><h1>No matching opportunities found :(</h1></body></html>"
    )
    automator = BrowserAutomator()
    automator.page = feed_page
    steps = []
    automator._notify = lambda step, message, **kwargs: steps.append(step)

    report = automator._instahyre_apply_on_feed(max_jobs=30)

    assert report["applied"] == 0
    assert "feed_empty" in steps
    assert "login_required" not in steps


RADIOS = """
<html><body><form>
  <div class="q"><span>Can you join us immediately?</span>
    <div><input type="radio" name="join"><span>Yes</span></div>
    <div><input type="radio" name="join"><span>No</span></div></div>
</form></body></html>
"""


def test_radios_without_labels_are_read_and_clicked_by_their_text(feed_page):
    """Custom forms leave the radio's value as "on"; the options are the text
    beside them."""
    feed_page.set_content(RADIOS)
    automator = BrowserAutomator()
    automator.page = feed_page

    group = automator._radio_groups()[0]
    assert [o["label"] for o in group["options"]] == ["Yes", "No"]

    class StubSolver:
        def answer_question(self, question, options=None, **kwargs):
            return "No"

    automator.question_solver = StubSolver()
    automator._answer_radio_groups({"questions_answered": [], "fields_filled": []}, "Dev", "Acme")
    assert feed_page.evaluate("document.querySelectorAll('input')[1].checked") is True


def test_only_jobs_matching_the_roles_are_applied_to_the_rest_wait(feed_page):
    automator = BrowserAutomator()
    automator.page = feed_page
    automator._notify = lambda *args, **kwargs: None

    report = automator._instahyre_apply_on_feed(max_jobs=30, roles=["DevOps Engineer"])

    assert feed_page.evaluate("window.clicked") == ["DevOps Engineer / Apply"]
    waiting = [job for job in report["jobs"] if job["status"] == "needs_decision"]
    assert [job["title"] for job in waiting] == ["Backend Engineer"]
    assert report["applied"] == 1


def test_no_roles_given_means_everything_waits_for_a_decision(feed_page):
    automator = BrowserAutomator()
    automator.page = feed_page
    automator._notify = lambda *args, **kwargs: None

    report = automator._instahyre_apply_on_feed(max_jobs=30, roles=[])

    assert feed_page.evaluate("window.clicked") == []
    assert report["applied"] == 0
    assert {job["status"] for job in report["jobs"]} == {"needs_decision"}
