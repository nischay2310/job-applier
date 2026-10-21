from __future__ import annotations

import sys

from fastapi.testclient import TestClient

from job_applier.automation.browser_automator import job_passes_filters
from job_applier.web.app import app


JOB = "DevOps Engineer - senior role, Kubernetes"


def test_no_filters_lets_every_job_through():
    assert job_passes_filters(JOB, [], [])


def test_job_must_mention_one_of_the_include_words():
    assert job_passes_filters(JOB, ["python", "kubernetes"], [])
    assert not job_passes_filters(JOB, ["python", "golang"], [])


def test_job_mentioning_an_exclude_word_is_skipped():
    assert not job_passes_filters(JOB, [], ["intern", "senior"])
    assert job_passes_filters(JOB, [], ["intern"])


def test_exclude_wins_over_include():
    assert not job_passes_filters(JOB, ["devops"], ["senior"])


def test_words_match_regardless_of_case():
    assert job_passes_filters("Senior SRE", ["sre"], [])
    assert not job_passes_filters("Senior SRE", [], ["SENIOR"])


def test_login_endpoint_opens_instahyre_in_the_automation_chrome(monkeypatch):
    module = sys.modules["job_applier.web.app"]
    opened = []
    monkeypatch.setattr(module, "ensure_chrome", lambda: (True, "ok"))
    monkeypatch.setattr(module, "open_page", lambda url: opened.append(url) or True)

    response = TestClient(app).post("/api/instahyre/login")

    assert response.status_code == 200
    assert opened == ["https://www.instahyre.com/login/"]


def test_login_endpoint_reports_a_chrome_that_wont_start(monkeypatch):
    module = sys.modules["job_applier.web.app"]
    monkeypatch.setattr(module, "ensure_chrome", lambda: (False, "no chrome"))

    response = TestClient(app).post("/api/instahyre/login")

    assert response.status_code == 503


def test_sync_from_desktop_chrome_says_so_when_nothing_was_copied(monkeypatch):
    from job_applier.automation.auth_manager import AuthManager

    monkeypatch.setattr(
        AuthManager, "sync_desktop_cookies", lambda self: {"cookies_merged": 0}
    )

    response = TestClient(app).post("/api/auth/sync-chrome")

    assert response.status_code == 200
    assert "Open Instahyre Login" in response.json()["message"]


def test_instahyre_sign_in_pages_are_recognised():
    from job_applier.automation.auth_manager import is_instahyre_sign_in

    assert is_instahyre_sign_in("https://www.instahyre.com/login/")
    assert is_instahyre_sign_in(
        "https://accounts.google.com/v3/signin/identifier?redirect_uri=https%3A%2F%2Fwww.instahyre.com%2Fcomplete"
    )
    assert not is_instahyre_sign_in("https://www.instahyre.com/candidate/opportunities/")
    assert not is_instahyre_sign_in("https://www.linkedin.com/feed/")


def test_login_status_reads_cookie_names_from_the_automation_chrome():
    from job_applier.automation.auth_manager import platforms_from_cookies

    status = platforms_from_cookies(
        [("www.instahyre.com", "sessionid"), (".linkedin.com", "li_at")]
    )
    assert status["instahyre"] and status["linkedin"]
    assert not status["google"]
