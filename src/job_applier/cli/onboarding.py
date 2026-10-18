"""First-run setup for a new user.

The dashboard shows a popup with a few basic questions (and a resume upload
that fills in the answers for you). What comes back is saved to the
database, plus data/master_resume.json and data/candidate_profile.json so
the scraper and tailoring have something to work with.
"""

from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from pypdf import PdfReader

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


OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")

RESUME_PROMPT = (
    "Read this resume and return only a JSON object with these keys: "
    "name, email, phone, location (City, Country), linkedin, github, "
    "languages, summary, skills (list of strings), "
    "experience (list of {role, company, dates, details: list of strings}), "
    "education ({institution, degree, details}), "
    "years_of_experience (a number), target_roles (list of 3 job titles "
    "this person should search for). Use empty strings when unknown.\n\n"
)


class ResumeReadError(Exception):
    """The resume couldn't be read; the message is safe to show the user."""


def pdf_to_text(pdf_bytes: bytes) -> str:
    """The plain text of a resume PDF."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:
        raise ResumeReadError(f"That PDF couldn't be opened ({e}).") from e
    if not text.strip():
        raise ResumeReadError(
            "No text found in that PDF (is it a scan?) - fill in the form by hand."
        )
    return text


def read_resume(pdf_bytes: bytes) -> dict[str, Any]:
    """Reads a resume PDF with a small model running on this computer
    (Ollama) and returns its details as a dict. Nothing leaves the machine."""
    text = pdf_to_text(pdf_bytes)
    request = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(
            {
                "model": OLLAMA_MODEL,
                "stream": False,
                "format": "json",
                "messages": [{"role": "user", "content": RESUME_PROMPT + text[:12000]}],
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            answer = json.load(response)["message"]["content"]
    except urllib.error.URLError as e:
        raise ResumeReadError(
            "Couldn't reach Ollama. Install it from https://ollama.com, run "
            f"'ollama pull {OLLAMA_MODEL}', then try again."
        ) from e
    except Exception as e:
        raise ResumeReadError(f"The local model failed ({e}).") from e
    try:
        details = json.loads(answer)
    except json.JSONDecodeError as e:
        raise ResumeReadError("The local model's answer wasn't usable.") from e
    return tidy_details(details) if isinstance(details, dict) else {}


TEXT_FIELDS = ("name", "email", "phone", "location", "linkedin", "github", "languages", "summary")


def tidy_details(details: dict[str, Any]) -> dict[str, Any]:
    """Small models sometimes answer in the wrong shape (a location as
    {"City": ..., "Country": ...}). Turn everything into what the form and
    the rest of the app expect."""
    tidy = dict(details)
    for key in TEXT_FIELDS:
        value = tidy.get(key, "")
        if isinstance(value, dict):
            value = ", ".join(str(v) for v in value.values() if v)
        elif isinstance(value, list):
            value = ", ".join(str(v) for v in value if v)
        tidy[key] = str(value or "").strip()
    tidy["skills"] = as_list(tidy.get("skills"))
    tidy["target_roles"] = as_list(tidy.get("target_roles"))
    if not isinstance(tidy.get("experience"), list):
        tidy["experience"] = []
    if not isinstance(tidy.get("education"), dict):
        tidy["education"] = {}
    return tidy


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
