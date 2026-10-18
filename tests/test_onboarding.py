from __future__ import annotations

import json
from pathlib import Path

import pytest

from job_applier.cli import onboarding


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


def test_build_master_resume_has_the_expected_sections():
    resume = onboarding.build_master_resume({"name": "Alex", "email": "a@b.com"})
    assert resume["contact"]["name"] == "Alex"
    assert set(resume) == {
        "contact", "summary", "experience", "skills", "education", "projects"
    }


def test_new_user_is_asked_questions_and_files_are_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(onboarding, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(
        "job_applier.cli.onboarding.save_candidate_profile",
        lambda profile: (tmp_path / "data" / "candidate_profile.json").write_text(
            json.dumps(profile.to_dict()), encoding="utf-8"
        ),
    )
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    # key, resume, name, email, phone, location, linkedin, years,
    # job title, company, skills, target roles, target places
    answers = iter(
        ["", "", "Alex Example", "a@b.com", "123", "Berlin, Germany", "", "5",
         "Engineer", "Acme", "Python, SQL", "Python Dev", "Berlin"]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    assert onboarding.needs_onboarding()
    onboarding.run_onboarding()

    assert not onboarding.needs_onboarding()
    resume = json.loads((tmp_path / "data" / "master_resume.json").read_text())
    assert resume["skills"] == ["Python", "SQL"]
    profile = json.loads((tmp_path / "data" / "candidate_profile.json").read_text())
    assert profile["target_roles"] == ["Python Dev"]
    assert profile["target_locations"] == ["Berlin"]
