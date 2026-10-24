"""Matching job titles to the roles typed into the scraper."""

from __future__ import annotations

import pytest

from job_applier import roles


def test_title_matches_when_every_role_word_is_present():
    assert roles.title_matches_roles("Senior DevOps Engineer (AWS)", ["DevOps Engineer"])
    assert roles.title_matches_roles("Engineer - DevOps", ["DevOps Engineer"])
    assert roles.title_matches_roles("Python Developer", ["Python Dev"])
    assert roles.title_matches_roles("Cloud Engineer 2", ["Cloud Architect", "Cloud Engineer"])


def test_title_does_not_match_a_different_job():
    assert not roles.title_matches_roles("Backend Engineer", ["DevOps Engineer"])
    assert not roles.title_matches_roles("Cloud Sales Manager", ["Cloud Engineer"])
    assert not roles.title_matches_roles("Anything", [])
    assert not roles.title_matches_roles("Anything", ["  "])


@pytest.fixture
def roles_path(tmp_path, monkeypatch):
    path = tmp_path / "roles.json"
    monkeypatch.setenv("JOB_APPLIER_ROLES_PATH", str(path))
    return path


def test_scraper_roles_are_remembered(roles_path):
    assert roles.load_scraper_roles() == []
    roles.save_scraper_roles(["Cloud Architect", " DevOps Engineer ", ""])
    assert roles.load_scraper_roles() == ["Cloud Architect", "DevOps Engineer"]
    # An empty search doesn't wipe the saved list.
    roles.save_scraper_roles([])
    assert roles.chosen_roles() == ["Cloud Architect", "DevOps Engineer"]
