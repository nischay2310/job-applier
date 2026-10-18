"""First-run setup for a new user.

The dashboard shows a popup with a few basic questions (and a resume upload
that fills in the answers for you). What comes back is saved to the
database, plus data/master_resume.json and data/candidate_profile.json so
the scraper and tailoring have something to work with.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from job_applier.automation.candidate_profile import (
    CandidateProfile,
    save_candidate_profile,
)
from job_applier.db import save_onboarding_answers
from job_applier.utils import get_project_root


def data_dir() -> Path:
    return get_project_root() / "data"


def needs_onboarding() -> bool:
    """True until the user has a resume saved."""
    return not (data_dir() / "master_resume.json").exists()


def split_list(text: str) -> list[str]:
    """"a, b ,c" -> ["a", "b", "c"]. Empty pieces are dropped."""
    return [part.strip() for part in text.split(",") if part.strip()]


def as_list(value: Any) -> list[str]:
    """Accepts a list or a comma separated string."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return split_list(str(value or ""))


def read_resume_with_gemini(pdf_bytes: bytes, api_key: str) -> dict[str, Any]:
    """Asks Gemini to read a resume PDF (given as bytes) and returns its
    details as a dict.

    Returns an empty dict if anything goes wrong, so the caller can just
    fall back to asking the questions by hand.
    """
    try:
        from google import genai  # type: ignore[attr-defined]
        from google.genai import types

        prompt = (
            "Read this resume and return only a JSON object with these keys: "
            "name, email, phone, location (City, Country), linkedin, github, "
            "languages, summary, skills (list of strings), "
            "experience (list of {role, company, dates, details: list of strings}), "
            "education ({institution, degree, details}), "
            "years_of_experience (a number), target_roles (list of 3 job titles "
            "this person should search for). Use empty strings when unknown. "
            "No markdown, just the JSON."
        )
        client = genai.Client(api_key=api_key)
        model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        response = client.models.generate_content(
            model=model,
            contents=[
                types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                prompt,
            ],
        )
        text = (response.text or "").strip()
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```")
        return json.loads(text)
    except Exception as e:
        print(f"Could not read the resume automatically: {e}")
        return {}


def build_master_resume(details: dict[str, Any]) -> dict[str, Any]:
    """The master_resume.json layout the rest of the app expects."""
    return {
        "contact": {
            "name": details.get("name", ""),
            "phone": details.get("phone", ""),
            "email": details.get("email", ""),
            "linkedin": details.get("linkedin", ""),
            "github": details.get("github", ""),
            "location": details.get("location", ""),
            "languages": details.get("languages", ""),
        },
        "summary": details.get("summary", ""),
        "experience": details.get("experience", []),
        "skills": details.get("skills", []),
        "education": details.get("education", {}),
        "projects": [],
    }


def build_profile(details: dict[str, Any]) -> CandidateProfile:
    """The screening-answers profile, from what the user told us."""
    name = details.get("name", "").strip()
    first, _, last = name.partition(" ")
    city, _, country = details.get("location", "").partition(",")
    experience = details.get("experience") or [{}]
    education = details.get("education") or {}
    return CandidateProfile(
        full_name=name,
        first_name=first,
        last_name=last,
        email=details.get("email", ""),
        phone=details.get("phone", ""),
        city=city.strip(),
        country=country.strip(),
        address=details.get("location", ""),
        linkedin_url=details.get("linkedin", ""),
        github_url=details.get("github", ""),
        languages=details.get("languages", ""),
        current_company=experience[0].get("company", ""),
        current_title=experience[0].get("role", ""),
        years_of_experience=str(details.get("years_of_experience", "")),
        education_institution=education.get("institution", ""),
        education_degree=education.get("degree", ""),
        target_roles=details.get("target_roles", []),
        target_locations=details.get("target_locations", []),
    )


def save_api_key(key: str) -> None:
    """Adds GOOGLE_API_KEY to .env, keeping anything already in there."""
    env_file = get_project_root() / ".env"
    lines = []
    if env_file.exists():
        lines = [
            line
            for line in env_file.read_text(encoding="utf-8").splitlines()
            if not line.startswith("GOOGLE_API_KEY=")
        ]
    lines.append(f"GOOGLE_API_KEY={key}")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ["GOOGLE_API_KEY"] = key


def normalise(details: dict[str, Any]) -> dict[str, Any]:
    """Cleans up what the popup sent: lists as lists, an experience entry
    from the typed job title if the resume gave none."""
    clean = dict(details)
    for key in ("skills", "target_roles", "target_locations"):
        clean[key] = as_list(details.get(key))
    if not clean.get("experience"):
        clean["experience"] = [
            {
                "role": str(details.get("job_title", "")).strip(),
                "company": str(details.get("company", "")).strip(),
                "dates": "",
                "details": [],
            }
        ]
    return clean


def save_onboarding(details: dict[str, Any], resume_pdf: bytes | None = None) -> None:
    """Saves the popup's answers: in the database, as the resume and profile
    JSON files, and (if given) as the user's own resume PDF."""
    details = normalise(details)
    api_key = str(details.pop("api_key", "") or "").strip()
    if api_key:
        save_api_key(api_key)

    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    if resume_pdf:
        (folder / "resume.pdf").write_bytes(resume_pdf)

    answers = {
        key: ", ".join(value) if isinstance(value, list) else str(value or "")
        for key, value in details.items()
        if key not in ("experience", "education")
    }
    save_onboarding_answers(answers)

    (folder / "master_resume.json").write_text(
        json.dumps(build_master_resume(details), indent=2), encoding="utf-8"
    )
    save_candidate_profile(build_profile(details), folder / "candidate_profile.json")
