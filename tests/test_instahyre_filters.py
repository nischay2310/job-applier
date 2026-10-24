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
    assert not is_instahyre_sign_in(
        "https://www.instahyre.com/candidate/opportunities/"
    )
    assert not is_instahyre_sign_in("https://www.linkedin.com/feed/")


def test_login_status_reads_cookie_names_from_the_automation_chrome():
    from job_applier.automation.auth_manager import platforms_from_cookies

    status = platforms_from_cookies(
        [("www.instahyre.com", "sessionid"), (".linkedin.com", "li_at")]
    )
    assert status["instahyre"] and status["linkedin"]
    assert not status["google"]


def test_auto_apply_settings_are_saved_and_read_back(tmp_path, monkeypatch):
    import sys

    from fastapi.testclient import TestClient

    from job_applier.web.app import app

    monkeypatch.setattr(
        sys.modules["job_applier.web.app"],
        "INSTAHYRE_WATCH_FILE",
        tmp_path / "watch.json",
    )
    client = TestClient(app)
    assert client.get("/api/instahyre/watch").json()["enabled"] is False

    settings = {
        "enabled": True,
        "every_minutes": 15,
        "filters": {"include": ["devops"], "exclude": [], "max_jobs": 5},
    }
    assert client.post("/api/instahyre/watch", json=settings).status_code == 200
    saved = client.get("/api/instahyre/watch").json()
    assert saved["enabled"] is True
    assert saved["every_minutes"] == 15
    assert saved["filters"]["include"] == ["devops"]

    too_often = {**settings, "every_minutes": 1}
    assert client.post("/api/instahyre/watch", json=too_often).status_code == 422


def test_jobs_waiting_for_a_decision_show_up_in_the_tracker_and_stay_decided():
    from job_applier.tracker import load_tracker
    from job_applier.web.app import instahyre_job_url, record_instahyre_jobs

    waiting = {
        "jobs": [
            {
                "title": "Backend Engineer",
                "company": "Acme",
                "status": "needs_decision",
                "reason": "doesn't match your roles",
            }
        ]
    }
    record_instahyre_jobs(waiting)
    rows = load_tracker()
    row = rows[rows["job_url"] == instahyre_job_url("Acme", "Backend Engineer")].iloc[0]
    assert (row["status"], row["platform"]) == ("needs_decision", "Instahyre")

    # Once skipped, the next scan must not put it back on the waiting list.
    from job_applier.tracker import record_application

    record_application(
        "Acme",
        "Backend Engineer",
        instahyre_job_url("Acme", "Backend Engineer"),
        status="skipped",
        platform="Instahyre",
    )
    record_instahyre_jobs(waiting)
    rows = load_tracker()
    assert (
        rows[rows["job_url"] == instahyre_job_url("Acme", "Backend Engineer")].iloc[0][
            "status"
        ]
        == "skipped"
    )


def test_decision_buttons_skip_or_apply_to_one_job(monkeypatch):
    import sys

    from fastapi.testclient import TestClient

    from job_applier.tracker import load_tracker
    from job_applier.web.app import app

    started = []
    monkeypatch.setattr(
        sys.modules["job_applier.web.app"],
        "run_instahyre_scan",
        lambda filters: started.append(filters),
    )
    client = TestClient(app)
    body = {"title": "Platform Engineer", "company": "Initech"}

    assert (
        client.post(
            "/api/instahyre/decision", json={**body, "action": "maybe"}
        ).status_code
        == 400
    )

    assert (
        client.post(
            "/api/instahyre/decision", json={**body, "action": "skip"}
        ).status_code
        == 200
    )
    rows = load_tracker()
    assert "skipped" in set(rows[rows["title"] == "Platform Engineer"]["status"])

    assert (
        client.post(
            "/api/instahyre/decision", json={**body, "action": "apply"}
        ).status_code
        == 200
    )
    assert started[0].include == ["Platform Engineer"]
    assert started[0].max_jobs == 1
    assert started[0].only_matching_roles is False
