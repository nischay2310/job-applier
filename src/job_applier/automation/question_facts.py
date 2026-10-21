"""Answers screening questions from facts the candidate has already given.

Most screeners ask the same few things: where do you live, how soon can you
join, how many years of experience, do you know Terraform. The answers are
in the profile and the resume, so they are worked out here instead of being
asked again. Anything not covered returns None and the solver moves on - a
question is never answered with a guess.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from job_applier.automation.candidate_profile import CandidateProfile
from job_applier.utils import get_project_root

# Picks the dropdown/radio choice for an answer; the solver passes in its own.
Matcher = Callable[[str, list[str] | None], str]

# Words that mean the same skill. A question mentioning any of them is asking
# about that skill, and the resume covers it if it mentions any of them too.
SKILL_GROUPS: tuple[tuple[str, ...], ...] = (
    ("aws", "ec2", "s3", "lambda", "cloudwatch", "eks", "ecs", "fargate", "codepipeline"),
    ("azure", "aks", "azure devops"),
    ("gcp", "google cloud", "gke", "bigquery"),
    ("kubernetes", "k8s", "eks", "gke", "aks", "helm"),
    ("docker", "containers", "container"),
    ("terraform", "infrastructure as code", "iac", "cloudformation", "ansible"),
    ("ci/cd", "cicd", "pipeline", "pipelines", "jenkins", "github actions", "gitlab"),
    ("python", "bash", "powershell", "scripting", "shell"),
    ("linux", "unix"),
    ("monitoring", "alerting", "observability", "prometheus", "grafana", "datadog"),
    ("networking", "vpc", "dns", "vpn", "tcp/ip", "load balancer", "load balancers", "firewall", "firewalls"),
    ("iam", "security", "secrets", "vault"),
    ("sre", "site reliability", "devops", "platform engineering"),
    ("kafka", "redis", "mongodb", "postgresql", "cassandra"),
    ("on-premise", "on-premises", "onpremises", "on premise", "on prem", "bare-metal", "bare metal"),
    ("ansible",),
    ("gitlab",),
    ("jenkins",),
    ("github actions",),
)

# An experience question about these needs more than a keyword match.
NEEDS_HUMAN = ("lead ", "led ", "mentor", "certif", "architect", "manage a team", "managed a team")

EXPERIENCE_VERBS = (
    "experience", "worked", "hands-on", "hands on", "implemented", "configured",
    "designed", "deployed", "administered", "exposure", "familiar", "used ",
)


def first_line(question: str) -> str:
    """LinkedIn repeats the label for screen readers; keep one copy."""
    for line in question.splitlines():
        if line.strip():
            return line.strip()
    return question.strip()


def has_word(text: str, word: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None


def notice_days(notice: str) -> int | None:
    """"15 days" -> 15, "1 month" -> 30, "2 weeks" -> 14. None if unclear."""
    match = re.search(r"(\d+)\s*(day|week|month)?", notice.lower())
    if not match:
        return 0 if "immediate" in notice.lower() else None
    amount = int(match.group(1))
    unit = match.group(2) or "day"
    return amount * {"day": 1, "week": 7, "month": 30}[unit]


def is_yes_no(question: str, options: list[str] | None) -> bool:
    if options:
        names = {o.strip().lower() for o in options}
        return bool(names & {"yes", "no"})
    return bool(
        re.match(r"(do|does|did|have|has|are|is|can|could|will|would)\b", question)
        or "yes/no" in question
    )


def top_skills(resume: dict[str, Any], limit: int = 12) -> str:
    """"Cloud: AWS (EC2, S3), GCP" -> "AWS, GCP": the first few of each line."""
    names: list[str] = []
    skills = resume.get("skills") or []
    if isinstance(skills, dict):
        skills = [f"{k}: {v}" for k, v in skills.items()]
    for entry in skills:
        text = re.sub(r"\([^)]*\)", "", str(entry)).split(":", 1)[-1]
        names.extend(part.strip() for part in text.split(",")[:3] if part.strip())
    return ", ".join(names[:limit])


class ProfileFacts:
    """Answers from the profile and the master resume."""

    def __init__(self, profile: CandidateProfile, resume: dict[str, Any] | None = None):
        self.profile = profile
        self.resume = resume if resume is not None else self._load_resume()
        self.resume_text = json.dumps(self.resume).lower()

    @staticmethod
    def _load_resume() -> dict[str, Any]:
        path = get_project_root() / "data" / "master_resume.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def answer(self, question: str, options: list[str] | None, match: Matcher) -> str | None:
        """The answer, or None if these facts don't cover the question."""
        q = " ".join(first_line(question).replace("*", " ").lower().split())
        yes_no = is_yes_no(q, options)

        def yes_or_no(yes: bool) -> str:
            return match("Yes" if yes else "No", options)

        for rule in (
            self._joining,
            self._years,
            self._willingness,
            self._location,
            self._about_you,
            self._skill_question,
        ):
            result = rule(q, options, match, yes_no, yes_or_no)
            if result is not None:
                return result
        return None

    # Rules ---------------------------------------------------------------

    def _joining(self, q, options, match, yes_no, yes_or_no):
        days = notice_days(self.profile.notice_period)
        if days is None or "serving" in q:
            return None
        asks_immediate = re.search(r"immediate|right away|urgent", q) is not None
        asks_timing = re.search(
            r"notice|how soon|when can you (start|join)|earliest|joining time|"
            r"join (us|within|in)|start date|date of joining|available to (join|start)",
            q,
        )
        if not (asks_immediate or asks_timing):
            return None

        if yes_no and (asks_immediate or "within" in q):
            window = re.search(r"within\s*(?:the next\s*)?(\d+)\s*(day|week|month)", q)
            if window:
                limit = int(window.group(1)) * {"day": 1, "week": 7, "month": 30}[window.group(2)]
                return yes_or_no(days <= limit)
            return yes_or_no(days == 0)

        if options:
            return self._notice_option(days, options, match)
        return self.profile.notice_period

    def _notice_option(self, days: int, options: list[str], match: Matcher) -> str:
        """The choice that covers the notice period: "15 days", "Less than 30
        days", "1 month", "Immediate"..."""
        found = match(self.profile.notice_period, options)
        if found:
            return found
        best = ""
        for option in options:
            text = option.lower()
            if days == 0 and "immediate" in text:
                return option
            number = re.search(r"(\d+)", text)
            if not number:
                continue
            value = int(number.group(1)) * (30 if "month" in text else 7 if "week" in text else 1)
            if re.search(r"less than|under|within|up to|<|max", text) and days <= value:
                return option
            span = re.search(r"(\d+)\s*(?:-|–|to)\s*(\d+)", text)
            if span:
                unit = 30 if "month" in text else 7 if "week" in text else 1
                if int(span.group(1)) * unit <= days <= int(span.group(2)) * unit:
                    return option
            if value >= days and (not best or value < self._days_in(best)):
                best = option
        return best

    @staticmethod
    def _days_in(option: str) -> int:
        number = re.search(r"(\d+)", option)
        if not number:
            return 10**6
        return int(number.group(1)) * (30 if "month" in option.lower() else 1)

    def _years(self, q, options, match, yes_no, yes_or_no):
        try:
            years = float(self.profile.years_of_experience)
        except ValueError:
            return None
        asks_years = re.search(
            r"\b(years?|yrs?)\b.*\bexp(erience)?\b|\bexp(erience|\.)?\b.*\b(years?|yrs?)\b"
            r"|how many years|how long have you",
            q,
        )
        threshold = re.search(r"(\d+)\s*(?:\+|plus)?\s*(?:-|–|to)?\s*\d*\s*\+?\s*(?:years?|yrs?)", q)
        about_age = re.search(r"\bold\b|\bage\b", q)
        if yes_no and threshold and not about_age:
            return yes_or_no(years >= int(threshold.group(1)))
        if asks_years and "how long have you been working on" not in q:
            return match(self.profile.years_of_experience, options)
        return None

    def _willingness(self, q, options, match, yes_no, yes_or_no):
        city = self.profile.city.lower()
        aliases = {city, "bangalore", "bengaluru"} if city in ("bangalore", "bengaluru") else {city}
        aliases.discard("")

        here = re.search(r"(currently|presently|now)\s+(in|at|based in|located in|residing in)\b", q)
        if here and yes_no:
            return yes_or_no(any(has_word(q, a) for a in aliases))

        willing = re.search(
            r"willing|comfortable|fine with|ready (for|to)|able to|okay with|ok with|"
            r"open to|available for|can you (work|relocate)",
            q,
        )
        place = re.search(
            r"on-?site|office|wfo|hybrid|relocat|commute|travel|face[- ]to[- ]face|"
            r"work from|location",
            q,
        )
        one_off = re.search(r"walk[- ]?in|\bdrive\b", q)
        asks_open = re.match(r"(what|which|where|how)\b", q)
        if willing and place and not one_off and not asks_open:
            if any(has_word(q, a) for a in aliases):
                return yes_or_no(True)
            return match(self.profile.willing_to_relocate, options)
        return None

    def _location(self, q, options, match, yes_no, yes_or_no):
        if yes_no:
            return None
        p = self.profile
        if has_word(q, "state") and not re.search(r"united states|statement|state of mind", q):
            return match(p.state, options) if p.state else None
        if re.search(r"\bcity\b", q) and not re.search(r"\b(preferred|which)\b", q):
            return match(p.city, options) if p.city else None
        if re.search(r"(current|present|your|where are you).*(location|located|based)|current city", q):
            if not p.city:
                return None
            where = ", ".join(x for x in (p.city, p.state, p.country) if x)
            return match(where, options) or match(p.city, options)
        return None

    def _about_you(self, q, options, match, yes_no, yes_or_no):
        p = self.profile
        if yes_no:
            return None
        if re.search(r"nationality|citizen of|citizenship", q) and p.nationality:
            return match(p.nationality, options)
        if re.search(r"employment status|currently employed|are you employed", q) and p.employment_status:
            return match(p.employment_status, options)
        if "designation" in q or re.search(r"current (job )?(title|role|position)", q):
            title = p.current_title
            if re.search(r"company|organi[sz]ation|employer", q) and p.current_company:
                return f"{title}, {p.current_company}"
            return match(title, options) if title else None
        if re.search(r"current (company|employer|organi[sz]ation)", q):
            return p.current_company or None
        if re.search(r"technical skills|areas of expertise|primary skills|skill ?set|key skills", q) and not options:
            return top_skills(self.resume) or None
        if "english" in q and re.search(r"proficien|level|fluen|rate", q):
            return self._english(options, match)
        if re.search(r"where did you (learn|hear|find)|how did you (hear|find|come)|learn about", q):
            return match("LinkedIn", options)
        if q.strip("? ") in ("website", "portfolio", "personal website", "portfolio url"):
            return p.portfolio_url or p.github_url or p.linkedin_url or None
        return None

    def _english(self, options, match):
        level = self.profile.english_level or "Fluent"
        if not options:
            return level
        found = match(level, options)
        if found:
            return found
        for word in ("fluent", "professional", "native", "advanced", "full", "excellent"):
            for option in options:
                if word in option.lower():
                    return option
        return ""

    def _skill_question(self, q, options, match, yes_no, yes_or_no):
        """"Do you have hands-on experience with X" - Yes only when the resume
        really covers most of what is named."""
        mentioned = [g for g in SKILL_GROUPS if any(has_word(q, w) for w in g)]
        if not mentioned or any(w in q for w in NEEDS_HUMAN):
            return None
        covered = [g for g in mentioned if any(has_word(self.resume_text, w) for w in g)]
        if len(covered) * 2 <= len(mentioned):
            return None

        if yes_no and any(v in q for v in EXPERIENCE_VERBS):
            return yes_or_no(True)

        if options and re.search(r"rate|proficiency|skill level|how would you", q):
            return self._strong_option(options)
        return None

    @staticmethod
    def _strong_option(options: list[str]) -> str:
        """A good-not-boastful choice: "Advanced", or 4 of 5 / 8 of 10."""
        for word in ("advanced", "proficient", "strong", "expert", "good"):
            for option in options:
                if word in option.lower():
                    return option
        numbers = []
        for option in options:
            match = re.match(r"\s*(\d+)", option)
            if not match:
                return ""
            numbers.append((int(match.group(1)), option))
        numbers.sort()
        return numbers[round((len(numbers) - 1) * 0.8)][1] if numbers else ""
