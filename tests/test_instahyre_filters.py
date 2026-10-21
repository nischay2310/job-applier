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
