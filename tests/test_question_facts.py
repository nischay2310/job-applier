"""Screening questions answered from the profile and resume."""

from __future__ import annotations

import pytest

from job_applier.automation.candidate_profile import CandidateProfile
from job_applier.automation.question_facts import ProfileFacts, notice_days
from job_applier.automation.question_solver import QuestionSolver

RESUME = {
    "summary": "Cloud engineer.",
    "skills": [
        "Cloud: AWS (EC2, S3, IAM, VPC), GCP, Azure",
        "Containers: Kubernetes, Docker, Helm",
        "IaC & CI/CD: Terraform, Ansible, Jenkins, GitHub Actions",
        "Languages: Python, Bash",
    ],
}


@pytest.fixture
def solver() -> QuestionSolver:
    profile = CandidateProfile(
        full_name="Alex Example",
        city="Bangalore",
        state="Karnataka",
        country="India",
        years_of_experience="4",
        notice_period="15 days",
        nationality="Indian",
        employment_status="Employed",
        english_level="Fluent",
        current_title="Cloud Engineer",
        current_company="Acme",
        willing_to_relocate="Yes",
    )
    solver = QuestionSolver(profile)
    solver.facts = ProfileFacts(profile, RESUME)
    return solver


def ask(solver, question, options=None):
    return solver.answer_question(question, options, use_ai=False)


def test_notice_days_understands_units():
    assert notice_days("15 days") == 15
    assert notice_days("1 month") == 30
    assert notice_days("2 weeks") == 14
    assert notice_days("Immediate") == 0
    assert notice_days("Negotiable") is None


def test_years_of_experience_is_the_same_for_every_skill(solver):
    assert ask(solver, "Yrs of exp. in CI/CD - GitHub Actions / Jenkins / GitLab ?") == "4"
    assert ask(solver, "How many years of experience do you have with Kubernetes?") == "4"


def test_years_threshold_questions_are_yes_or_no(solver):
    yes_no = ["Yes", "No"]
    assert ask(solver, "Do you have 3+ years of experience in DevOps?", yes_no) == "Yes"
    assert ask(solver, "10+ years of Experience in Cloud solutions : Yes/No") == "No"
    # An age check is not an experience threshold.
    assert ask(solver, "Are you at least 18 years old?", yes_no) == "Yes"


def test_joining_questions_use_the_notice_period(solver):
    yes_no = ["Yes", "No"]
    assert ask(solver, "What is your notice period?") == "15 days"
    assert ask(solver, "Can you join us immediately?", yes_no) == "No"
    assert ask(solver, "Are you an immediate joiner? Can join within 30 days?", yes_no) == "Yes"
    assert ask(solver, "Can you join within 7 days?", yes_no) == "No"


def test_notice_period_picks_the_right_dropdown_choice(solver):
    options = ["Immediate", "15 days", "30 days", "More than 30 days"]
    assert ask(solver, "Notice period", options) == "15 days"
    ranges = ["Less than 10 days", "Less than 30 days", "60 days"]
    assert ask(solver, "What is your notice period?", ranges) == "Less than 30 days"


def test_location_questions(solver):
    assert ask(solver, "State") == "Karnataka"
    assert ask(solver, "What is your current location?") == "Bangalore, Karnataka, India"
    yes_no = ["Yes", "No"]
    assert ask(solver, "Are you currently in Bangalore?", yes_no) == "Yes"
    assert ask(solver, "Are you currently in Pune?", yes_no) == "No"


def test_onsite_and_relocation_follow_the_relocation_answer(solver):
    yes_no = ["Yes", "No"]
    assert ask(solver, "Are you willing to work from Noida as required?", yes_no) == "Yes"
    assert ask(solver, "Comfortable for Delhi Gurugram as job location ?") == "Yes"
    assert ask(solver, "Are you available for Face to Face interview in Bangalore office?", yes_no) == "Yes"
    # A dated walk-in drive is not a general willingness question.
    assert not ask(solver, "Will you attend the walk in drive at Pune on 17th October?", yes_no)


def test_about_you_questions(solver):
    assert ask(solver, "What is your nationality?") == "Indian"
    assert ask(solver, "What is your current employment status?") == "Employed"
    assert ask(solver, "What is your current designation and company name?") == "Cloud Engineer, Acme"
    assert ask(solver, "What is your proficiency level in English?") == "Fluent"
    assert ask(solver, "Where did you learn about Acme?") == "LinkedIn"
    assert "AWS" in ask(solver, "What are your key technical skills and areas of expertise?")


def test_english_level_picks_from_the_choices(solver):
    levels = ["Basic", "Conversational", "Professional working", "Native"]
    assert ask(solver, "English proficiency?", levels) == "Professional working"


def test_experience_is_yes_only_when_the_resume_covers_it(solver):
    yes_no = ["Yes", "No"]
    assert ask(solver, "Do you have hands-on experience with Terraform or CloudFormation?", yes_no) == "Yes"
    assert ask(solver, "Have you worked with Kubernetes and Docker in production?", yes_no) == "Yes"
    # Not in the resume: left for the candidate, never guessed.
    assert not ask(solver, "Do you have hands-on experience with VMware or Proxmox?", yes_no)
    # Leadership and certifications need a person to answer.
    assert not ask(solver, "Have you led a team of Kubernetes engineers?", yes_no)


def test_skill_rating_is_strong_but_only_for_real_skills(solver):
    scale = ["1 - Beginner", "2", "3", "4 - Advanced", "5 - Expert"]
    assert ask(solver, "How would you rate your Python scripting skills?", scale) == "4 - Advanced"
    assert not ask(solver, "How would you rate your COBOL skills?", scale)


def test_unknown_questions_are_left_blank(solver):
    assert ask(solver, "What is your Last Working day?") == ""


def test_experience_questions_without_do_you_are_still_yes_no(solver):
    assert ask(solver, "Have AWS services hands-on - ECS/Fargate, Lambda, IAM, VPC ?") == "Yes"


def test_suggestions_come_with_a_reason_and_never_overreach(solver):
    from job_applier.automation.question_solver import suggest_answer

    yes_no = ["Yes", "No"]
    known = suggest_answer(solver, "What is your nationality?", use_model=False)
    assert known["answer"] == "Indian"

    missing = suggest_answer(solver, "Do you hold a PMP or CISSP certificate?", yes_no, use_model=False)
    assert missing["answer"] == "No"
    assert "resume" in missing["reason"]

    event = suggest_answer(solver, "Will you attend the walk in drive on Saturday?", yes_no, use_model=False)
    assert event["answer"] == ""

    nothing = suggest_answer(solver, "Describe your biggest challenge.", use_model=False)
    assert nothing["answer"] == ""
    assert nothing["reason"]
