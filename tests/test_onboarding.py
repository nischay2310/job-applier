from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from job_applier import db
from job_applier.cli import onboarding
from job_applier.web.app import app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A dashboard whose onboarding files go to a temp folder, not data/."""
    monkeypatch.setattr(onboarding, "data_dir", lambda: tmp_path / "data")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    # Don't let a real .env bring a key back in.
    monkeypatch.setattr(
        sys.modules["job_applier.web.app"], "load_dotenv", lambda *a, **k: None
    )
    db.init_db()
    return TestClient(app)


def test_split_list_drops_blanks_and_spaces():
    assert onboarding.split_list(" Python Dev, ,DevOps ,") == ["Python Dev", "DevOps"]


def test_build_profile_splits_name_and_location():
    profile = onboarding.build_profile(
        {
            "name": "Alex Example Smith",
            "location": "Berlin, Germany",
            "experience": [{"role": "Engineer", "company": "Acme"}],
            "target_roles": ["Engineer"],
        }
    )
    assert profile.first_name == "Alex"
    assert profile.last_name == "Example Smith"
    assert (profile.city, profile.country) == ("Berlin", "Germany")
    assert (profile.current_title, profile.current_company) == ("Engineer", "Acme")


def test_popup_script_is_added_to_the_dashboard(client):
    page = client.get("/")
    assert '<script src="/onboarding.js"></script>' in page.text
    assert client.get("/onboarding.js").status_code == 200


def test_new_user_is_asked_then_answers_are_saved(client, tmp_path):
    assert client.get("/api/onboarding").json()["needed"] is True

    details = {
        "name": "Alex Example",
        "email": "a@b.com",
        "location": "Berlin, Germany",
        "job_title": "Engineer",
        "company": "Acme",
        "skills": "Python, SQL",
        "target_roles": "Python Dev",
        "target_locations": "Berlin",
    }
    response = client.post(
        "/api/onboarding",
        data={"details": json.dumps(details)},
        files={"resume": ("cv.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert response.status_code == 200

    status = client.get("/api/onboarding").json()
    assert status["needed"] is False
    assert status["answers"]["email"] == "a@b.com"
    assert status["answers"]["skills"] == "Python, SQL"

    data = tmp_path / "data"
    assert (data / "resume.pdf").read_bytes().startswith(b"%PDF")
    resume = json.loads((data / "master_resume.json").read_text())
    assert resume["skills"] == ["Python", "SQL"]
    profile = json.loads((data / "candidate_profile.json").read_text())
    assert profile["target_roles"] == ["Python Dev"]
    assert profile["current_company"] == "Acme"


def test_name_is_required(client):
    response = client.post(
        "/api/onboarding", data={"details": json.dumps({"name": " "})}
    )
    assert response.status_code == 400


def test_resume_reader_rejects_non_pdf(client):
    response = client.post(
        "/api/onboarding/resume", files={"file": ("cv.txt", b"hello", "text/plain")}
    )
    assert response.status_code == 400


def test_resume_reader_says_so_when_ollama_is_missing(client, monkeypatch):
    monkeypatch.setattr(onboarding, "pdf_to_text", lambda _pdf: "Alex Example, Python")
    monkeypatch.setattr(onboarding, "OLLAMA_URL", "http://127.0.0.1:9")  # nothing there
    response = client.post(
        "/api/onboarding/resume",
        files={"file": ("cv.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert response.status_code == 422
    assert "Ollama" in response.json()["detail"]


def test_resume_text_goes_to_the_local_model_and_comes_back_as_details(monkeypatch):
    asked = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {"message": {"content": json.dumps({"name": "Alex Example"})}}
            ).encode()

    def fake_urlopen(request, timeout):
        asked["body"] = json.loads(request.data)
        return FakeResponse()

    monkeypatch.setattr(
        onboarding, "pdf_to_text", lambda _pdf: "Alex Example, Python dev"
    )
    monkeypatch.setattr(onboarding.urllib.request, "urlopen", fake_urlopen)

    assert onboarding.read_resume(b"%PDF")["name"] == "Alex Example"
    assert asked["body"]["model"] == onboarding.OLLAMA_MODEL
    assert "Python dev" in asked["body"]["messages"][0]["content"]


def test_odd_shapes_from_a_small_model_are_tidied():
    tidy = onboarding.tidy_details(
        {
            "name": "Alex",
            "location": {"City": "Bangalore", "Country": "India"},
            "skills": "Python, SQL",
            "experience": "none",
            "education": ["x"],
        }
    )
    assert tidy["location"] == "Bangalore, India"
    assert tidy["skills"] == ["Python", "SQL"]
    assert tidy["experience"] == []
    assert tidy["education"] == {}
