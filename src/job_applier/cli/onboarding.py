"""First-run setup for a new user.

Asks a few basic questions (or reads an existing resume to fill in the
answers for you), then saves data/master_resume.json and
data/candidate_profile.json so the scraper and tailoring have something to
work with.

    python -m job_applier.cli.onboarding
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from job_applier.automation.candidate_profile import (
    CandidateProfile,
    save_candidate_profile,
)
from job_applier.utils import get_project_root


def data_dir() -> Path:
    return get_project_root() / "data"


def needs_onboarding() -> bool:
    """True until the user has a resume saved."""
    return not (data_dir() / "master_resume.json").exists()


def split_list(text: str) -> list[str]:
    """"a, b ,c" -> ["a", "b", "c"]. Empty pieces are dropped."""
    return [part.strip() for part in text.split(",") if part.strip()]


def ask(question: str, default: str = "") -> str:
    """Asks one question. Pressing Enter keeps the default in brackets."""
    suffix = f" [{default}]" if default else ""
    answer = input(f"{question}{suffix}: ").strip()
    return answer or default


def read_resume_with_gemini(resume_file: Path, api_key: str) -> dict[str, Any]:
    """Asks Gemini to read a resume PDF and return its details as a dict.

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
                types.Part.from_bytes(
                    data=resume_file.read_bytes(), mime_type="application/pdf"
                ),
                prompt,
            ],
        )
        text = (response.text or "").strip()
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```")
        return json.loads(text)
    except Exception as e:
        print(f"  Could not read the resume automatically ({e}).")
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


def ask_resume_file() -> Path | None:
    """Asks where the resume PDF is. Enter skips it."""
    while True:
        answer = ask("Path to your resume PDF (press Enter to skip)").strip("\"'")
        if not answer:
            return None
        path = Path(answer).expanduser()
        if path.is_file() and path.suffix.lower() == ".pdf":
            return path
        print("  That isn't a PDF file I can find - check the path and try again.")


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


def run_onboarding() -> None:
    print("\nWelcome! Let's set up your details (takes about a minute).\n")

    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not api_key:
        print("A free Gemini key lets the app read your resume and tailor it to jobs.")
        print("Get one at https://aistudio.google.com/app/apikey (optional).")
        api_key = ask("Gemini API key (press Enter to skip)")
        if api_key:
            save_api_key(api_key)

    resume_file = ask_resume_file()
    details: dict[str, Any] = {}
    if resume_file and api_key:
        print("Reading your resume...")
        details = read_resume_with_gemini(resume_file, api_key)
    elif resume_file:
        print("No Gemini key, so I can't read the resume - I'll ask instead.")

    print("\nConfirm your details (press Enter to keep what's in brackets).\n")
    details["name"] = ask("Full name", details.get("name", ""))
    details["email"] = ask("Email", details.get("email", ""))
    details["phone"] = ask("Phone", details.get("phone", ""))
    details["location"] = ask("City, Country", details.get("location", ""))
    details["linkedin"] = ask("LinkedIn URL (optional)", details.get("linkedin", ""))
    details["years_of_experience"] = ask(
        "Years of experience", str(details.get("years_of_experience", ""))
    )

    if not details.get("experience"):
        title = ask("Current or most recent job title")
        company = ask("Company")
        details["experience"] = [
            {"role": title, "company": company, "dates": "", "details": []}
        ]
    if not details.get("skills"):
        details["skills"] = split_list(ask("Your main skills (comma separated)"))

    roles = ask(
        "Job titles to search for (comma separated)",
        ", ".join(details.get("target_roles", [])),
    )
    details["target_roles"] = split_list(roles)
    details["target_locations"] = split_list(
        ask(
            "Cities or countries to search in (comma separated)",
            details.get("location", ""),
        )
    )

    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    if resume_file:
        shutil.copy(resume_file, folder / resume_file.name)
    (folder / "master_resume.json").write_text(
        json.dumps(build_master_resume(details), indent=2), encoding="utf-8"
    )
    save_candidate_profile(build_profile(details))
    print("\nSaved. You can change any of this later in the dashboard.\n")


if __name__ == "__main__":
    run_onboarding()
