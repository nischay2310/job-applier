from __future__ import annotations

import os
import re

from job_applier.automation.candidate_profile import (  # type: ignore[import-not-found]
    CandidateProfile,
)
from job_applier.automation import local_model
from job_applier.automation.question_facts import (
    ProfileFacts,
    first_line,
    is_yes_no,
    top_skills,
)
from job_applier.automation.question_store import (  # type: ignore[import-not-found]
    dismiss_pending,
    get_answer,
    list_pending,
)
from job_applier.utils import get_project_root


class QuestionSolver:
    """Answers job application screening questions using rule heuristics and AI."""

    def __init__(self, profile: CandidateProfile, api_key: str | None = None):
        self.profile = profile
        self.api_key = api_key or os.environ.get("GOOGLE_API_KEY")
        # Set per application so open-ended answers can refer to what the
        # posting actually asks for.
        self.job_description: str = ""
        self.facts = ProfileFacts(profile)

    def answer_question(
        self,
        question_text: str,
        options: list[str] | None = None,
        job_title: str = "",
        company: str = "",
        use_ai: bool = True,
    ) -> str:
        """Determines the best answer for an application question."""
        cleaned_q = question_text.strip().lower()

        # 1a. Answers the candidate has already supplied for this question
        # (from the Tracker page) always win - that's the whole point of
        # asking them once and reusing it everywhere.
        stored = get_answer(question_text, options)
        if stored:
            return self._match_to_options(stored, options)

        # 1b. Check custom user-defined answers
        for key, ans in self.profile.custom_answers.items():
            if key.lower() in cleaned_q:
                return self._match_to_options(ans, options)

        # 1c. What the profile and resume already say: location, notice
        # period, years of experience, skills the resume really covers...
        from_facts = self.facts.answer(question_text, options, self._match_to_options)
        if from_facts:
            return from_facts

        # 2. Rule-based heuristics for standard compliance/screening questions
        # Work authorization
        if any(
            w in cleaned_q
            for w in [
                "authorized to work",
                "legally eligible",
                "right to work",
                "legal right",
            ]
        ):
            return self._match_to_options(
                self.profile.work_authorization, options, default="Yes"
            )

        # Visa sponsorship
        if "sponsorship" in cleaned_q or "visa" in cleaned_q:
            # Check if asking if candidate DOES need sponsorship vs DOES NOT need
            if any(
                w in cleaned_q
                for w in ["require", "need", "future", "now or in the future"]
            ):
                return self._match_to_options(
                    self.profile.sponsorship_required, options, default="No"
                )
            return self._match_to_options("No", options, default="No")

        # Age requirement
        if any(
            w in cleaned_q
            for w in ["18 years", "at least 18", "age of 18", "legal age"]
        ):
            return self._match_to_options("Yes", options, default="Yes")

        # Prior employment
        if any(
            w in cleaned_q
            for w in [
                "previously employed",
                "previously worked",
                "former employee",
                "ever worked",
            ]
        ):
            return self._match_to_options("No", options, default="No")

        # Background check
        if any(w in cleaned_q for w in ["background check", "screening", "drug test"]):
            return self._match_to_options("Yes", options, default="Yes")

        # Relocation
        if "relocate" in cleaned_q:
            return self._match_to_options(
                self.profile.willing_to_relocate, options, default="No"
            )

        # Notice period / availability
        if any(
            w in cleaned_q
            for w in [
                "notice",
                "how soon",
                "when can you start",
                "when can you join",
                "earliest joining",
                "availability",
            ]
        ):
            return self._match_to_options(
                self.profile.notice_period, options, default="1 month"
            )

        # Salary: current vs expected, and monthly vs annual CTC are different
        # numbers - answering all of them with one figure was wrong.
        if any(
            w in cleaned_q
            for w in [
                "salary",
                "compensation",
                "remuneration",
                "pay rate",
                "ctc",
                "package",
            ]
        ):
            current = any(
                w in cleaned_q for w in ["current", "present", "existing", "last drawn"]
            )
            monthly = "month" in cleaned_q
            key = ("current" if current else "expected") + (
                " monthly salary" if monthly else " ctc"
            )
            answer = get_answer(key)
            if not answer and not current and not monthly:
                answer = self.profile.salary_expectation
            if answer:
                return self._match_to_options(answer, options)

        # Referral / How did you hear
        if any(w in cleaned_q for w in ["how did you hear", "source", "hear about us"]):
            return self._match_to_options("LinkedIn", options, default="LinkedIn")

        if "country" in cleaned_q and self.profile.country:
            return self._match_to_options(self.profile.country, options)

        # Gender / EEO
        if "gender" in cleaned_q:
            return self._match_to_options(
                self.profile.gender, options, default="Prefer not to say"
            )

        # Veteran status
        if "veteran" in cleaned_q:
            return self._match_to_options(
                self.profile.veteran_status,
                options,
                default="I am not a protected veteran",
            )

        # Disability
        if "disability" in cleaned_q:
            return self._match_to_options(
                self.profile.disability_status,
                options,
                default="No, I do not have a disability",
            )

        # Years of experience with anything ("years of professional DevOps
        # experience", "experience with Python (years)") - same answer for any stack.
        if re.search(
            r"\byears?\b.*\bexperience\b|\bexperience\b.*\byears?\b", cleaned_q
        ):
            return self._match_to_options(
                self.profile.years_of_experience,
                options,
                default=self.profile.years_of_experience,
            )

        if not use_ai:
            return ""

        # 3. If Gemini is available, use AI to answer custom question
        ai_answer = self._answer_with_ai(question_text, options, job_title, company)
        if ai_answer:
            return ai_answer

        # 3a. No Gemini key: the model on this computer can still answer
        # open questions from the profile (never yes/no ones - those are
        # claims about the candidate, so they come from the resume check).
        local_answer = self._answer_with_local_model(question_text, options)
        if local_answer:
            return local_answer

        # 3b. Open-ended motivation questions ("What interests you about X?")
        # deserve a real written answer rather than being left blank.
        if not options and self._is_motivation_question(cleaned_q):
            return self._write_motivation_answer(company, job_title)

        # 4. Genuinely unknown: don't guess. Returning empty leaves the field
        # unfilled and lets the caller flag it for the candidate's review,
        # instead of submitting a made-up "Yes" or an arbitrary first option.
        return ""

    def _answer_with_local_model(
        self, question_text: str, options: list[str] | None
    ) -> str:
        cleaned = question_text.strip().lower()
        if is_yes_no(cleaned, options) or not local_model.is_running():
            return ""
        p = self.profile
        facts = [
            f"Name: {p.full_name}",
            f"Current role: {p.current_title} at {p.current_company}",
            f"Years of experience: {p.years_of_experience}",
            f"Location: {p.city}, {p.state}, {p.country}",
            f"Notice period: {p.notice_period}",
            f"Skills: {top_skills(self.facts.resume, 20)}",
            f"Summary: {self.facts.resume.get('summary', '')}",
        ]
        lines = [
            "Answer this job application question using ONLY the facts below. "
            f"If the facts don't answer it, reply exactly {local_model.UNKNOWN}. "
            "Reply with the answer only, under 40 words.",
            "",
            "Facts:",
            *facts,
            "",
            f"Question: {question_text}",
        ]
        if options:
            lines += ["Pick exactly one of:", *(f"- {o}" for o in options)]
        reply = local_model.ask("\n".join(lines))
        if not reply or local_model.UNKNOWN in reply.upper():
            return ""
        reply = reply.strip().strip("\"'")
        return self._match_to_options(reply, options) if options else reply

    MOTIVATION_MARKERS = (
        "what interests you",
        "why do you want to work",
        "why are you interested",
        "why this company",
        "why do you want to join",
        "what excites you",
        "what thrills you",
        "why would you like to join",
        "what attracts you",
        "tell us why",
        "motivat",
    )

    def _is_motivation_question(self, cleaned_q: str) -> bool:
        return any(marker in cleaned_q for marker in self.MOTIVATION_MARKERS)

    def _job_themes(self, limit: int = 3) -> list[str]:
        """Pulls the technologies this posting actually asks for, so the answer
        refers to the real role instead of generic filler."""
        if not self.job_description:
            return []
        text = self.job_description.lower()
        vocabulary = [
            "kubernetes",
            "terraform",
            "aws",
            "azure",
            "gcp",
            "ci/cd",
            "observability",
            "platform engineering",
            "site reliability",
            "automation",
            "containers",
            "infrastructure as code",
            "cost optimization",
            "security",
            "microservices",
        ]
        return [term for term in vocabulary if term in text][:limit]

    def _write_motivation_answer(self, company: str, job_title: str) -> str:
        """Writes a short, first-person answer for 'why this company' style
        questions.

        Deliberately grounded only in the posting and the candidate's own
        background - inventing praise about a company we know nothing about
        would put false claims in their application.
        """
        company_name = (company or "your team").replace("_", " ").strip()
        themes = self._job_themes()
        role = (job_title or "this role").replace("_", " ").strip()

        if themes:
            if len(themes) == 1:
                theme_phrase = themes[0]
            else:
                theme_phrase = ", ".join(themes[:-1]) + " and " + themes[-1]
            focus = (
                f"The role's focus on {theme_phrase} lines up closely with what "
                f"I do day to day"
            )
        else:
            focus = (
                f"The scope of {role} lines up closely with the infrastructure "
                f"work I do day to day"
            )

        years = self.profile.years_of_experience or "several"
        return (
            f"{focus}. I've spent the last {years} years running production "
            f"infrastructure at scale, and the part I enjoy most is making "
            f"systems boringly reliable for the people building on top of them. "
            f"That's the kind of problem I'd like to keep working on at "
            f"{company_name}."
        )

    def _match_to_options(
        self, target: str, options: list[str] | None, default: str = ""
    ) -> str:
        """Matches a target answer against a list of dropdown/radio choices."""
        if not options:
            return target

        target_lower = target.lower()
        # Exact match
        for opt in options:
            if opt.strip().lower() == target_lower:
                return opt

        # Numbers only match exactly or by range - as a substring "4" would
        # hit "14+ years".
        if re.fullmatch(r"\s*\d+(?:\.\d+)?\s*(?:\+|years?|yrs?)?\s*", target, re.I):
            return self._match_number_to_range(target, options)

        # Yes/No matching - checked before the generic substring match below,
        # since "no" is a substring of "not sure" / "none" and would
        # otherwise steal the match from the real Yes/No option.
        if target_lower in ["yes", "true", "y"]:
            for opt in options:
                opt_lower = opt.strip().lower()
                if opt_lower in [
                    "yes",
                    "i do",
                    "i agree",
                    "true",
                ] or opt_lower.startswith(("yes,", "yes ", "i do ")):
                    return opt
        elif target_lower in ["no", "false", "n"]:
            for opt in options:
                opt_lower = opt.strip().lower()
                if (
                    opt_lower in ["no", "false"]
                    or opt_lower.startswith(("no,", "no "))
                    or any(
                        phrase in opt_lower
                        for phrase in ("do not", "does not", "don't", "doesn't")
                    )
                ):
                    return opt

        # Decline / Prefer not to say
        if "prefer" in target_lower or "decline" in target_lower:
            for opt in options:
                if any(
                    w in opt.lower() for w in ["prefer not", "decline", "choose not"]
                ):
                    return opt

        # Substring match - word-boundary only, so "no" never matches inside
        # "not sure" or "none" and "a" never matches inside "java".
        for opt in options:
            opt_lower = opt.strip().lower()
            pattern = rf"\b{re.escape(target_lower)}\b"
            if re.search(pattern, opt_lower) or re.search(
                rf"\b{re.escape(opt_lower)}\b", target_lower
            ):
                return opt

        # A number against ranges: "4" -> "3-5 years", "8" -> "7+ years"
        ranged = self._match_number_to_range(target, options)
        if ranged:
            return ranged

        # No match means we don't know - never fall back to whatever option
        # happens to be first, that's a silent guess in the application.
        for opt in options:
            if default and opt.strip().lower() == default.lower():
                return opt
        return ""

    @staticmethod
    def _match_number_to_range(target: str, options: list[str]) -> str:
        match = re.fullmatch(
            r"\s*(\d+(?:\.\d+)?)\s*(?:\+|years?|yrs?)?\s*", target, re.I
        )
        if not match:
            return ""
        value = float(match.group(1))
        if value == 0:
            for opt in options:
                if "immediate" in opt.lower():
                    return opt
        for opt in options:
            text = opt.lower()
            span = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)", text)
            if span and float(span.group(1)) <= value <= float(span.group(2)):
                return opt
            floor = re.search(
                r"(\d+(?:\.\d+)?)\s*\+|(?:more than|over|above)\s*(\d+(?:\.\d+)?)", text
            )
            if floor:
                low = float(floor.group(1) or floor.group(2))
                if value >= low:
                    return opt
            ceiling = re.search(
                r"(?:less than|under|below|up to)\s*(\d+(?:\.\d+)?)", text
            )
            if ceiling and value < float(ceiling.group(1)):
                return opt
        return ""

    def _answer_with_ai(
        self,
        question_text: str,
        options: list[str] | None,
        job_title: str,
        company: str,
    ) -> str | None:
        """Uses Gemini to answer screening questions based on resume content."""
        if not self.api_key:
            return None

        try:
            from google import genai  # type: ignore[attr-defined]

            client = genai.Client(api_key=self.api_key)

            # Load master resume context
            master_resume_path = get_project_root() / "data" / "master_resume.json"
            resume_context = ""
            if master_resume_path.exists():
                try:
                    with open(master_resume_path, encoding="utf-8") as f:
                        resume_context = f.read()
                except Exception:
                    resume_context = ""

            options_prompt = ""
            if options:
                options_prompt = (
                    "Available options to choose from:\n"
                    + "\n".join(f"- {opt}" for opt in options)
                    + "\nYou MUST return exactly one of the available options verbatim."
                )
            else:
                options_prompt = "Provide a concise, direct answer (under 50 words, or single number/phrase if appropriate)."

            prompt = f"""
You are assisting candidate {self.profile.full_name} in answering a job application question for {job_title} at {company}.

Candidate Profile:
- Name: {self.profile.full_name}
- Email: {self.profile.email}
- Current role: {self.profile.current_title} at {self.profile.current_company}
- Location: {self.profile.city}, {self.profile.country}
- Years of experience: {self.profile.years_of_experience}

Resume data:
{resume_context[:2500]}

Question:
"{question_text}"

{options_prompt}

Output ONLY the final answer text with no extra commentary or formatting.
"""

            response = client.models.generate_content(
                model=os.environ.get("GEMINI_MODEL", "gemini-3.8-flash"),
                contents=prompt,
            )
            raw_answer = (response.text or "").strip()
            # Clean quotes if returned
            clean_answer = re.sub(r'^["\']|["\']$', "", raw_answer).strip()

            if options:
                return self._match_to_options(clean_answer, options)
            return clean_answer

        except Exception as e:
            print(f"Notice: Gemini question answering fallback: {e}")
            return None


def auto_answer_pending(solver: QuestionSolver) -> int:
    """Clears pending questions the solver can answer now (profile and resume
    facts only). Returns how many were cleared; the solver answers them
    itself the next time they come up."""
    cleared = 0
    for item in list_pending():
        options = item.get("options") or None
        # Radios once recorded with the browser's placeholder value "on".
        if options and all(o.strip().lower() == "on" for o in options):
            options = None
        answer = solver.answer_question(item["question"], options, use_ai=False)
        if answer and dismiss_pending(item["key"]):
            cleared += 1
    return cleared


STOP_WORDS = {
    "about",
    "above",
    "across",
    "after",
    "along",
    "answer",
    "available",
    "before",
    "being",
    "candidate",
    "company",
    "could",
    "describe",
    "does",
    "each",
    "every",
    "experience",
    "following",
    "from",
    "have",
    "having",
    "hence",
    "including",
    "into",
    "more",
    "most",
    "other",
    "please",
    "project",
    "projects",
    "ready",
    "should",
    "some",
    "such",
    "than",
    "that",
    "their",
    "them",
    "there",
    "these",
    "this",
    "those",
    "through",
    "using",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "will",
    "with",
    "within",
    "work",
    "worked",
    "would",
    "years",
    "your",
}


def suggest_answer(
    solver: QuestionSolver,
    question: str,
    options: list[str] | None = None,
    use_model: bool = True,
) -> dict[str, str]:
    """A suggested answer for a question the solver couldn't settle, with the
    reason for it. Only a suggestion: the candidate decides."""
    if options and all(o.strip().lower() == "on" for o in options):
        options = None
    answer = solver.answer_question(question, options, use_ai=False)
    if answer:
        return {"answer": answer, "reason": "From your profile and resume."}

    q = first_line(question).lower()
    if re.search(r"walk[- ]?in|\bdrive\b", q):
        return {
            "answer": "",
            "reason": "A one-off event - check the date and place before answering.",
        }

    if is_yes_no(q, options):
        words = sorted(
            {w for w in re.findall(r"[a-z][a-z+#./-]{4,}", q) if w not in STOP_WORDS}
        )
        found = [w for w in words if w in solver.facts.resume_text]
        if found and not (len(found) >= 2 and len(found) * 10 >= len(words) * 6):
            return {
                "answer": "",
                "reason": f"Only partly in your resume (it mentions {', '.join(found[:3])}) - you decide.",
            }
        guess = "Yes" if found else "No"
        picked = solver._match_to_options(guess, options)
        if not picked:
            return {"answer": "", "reason": "Pick the choice that is true for you."}
        if found:
            why = f"Your resume mentions {', '.join(found[:4])}."
        else:
            why = "Your resume doesn't show this."
        return {"answer": picked, "reason": f"{why} Check it is true before using it."}

    if use_model:
        draft = solver._answer_with_local_model(question, options)
        if draft:
            return {
                "answer": draft,
                "reason": "Drafted by the model on this computer from your resume - check it.",
            }
    if options:
        return {"answer": "", "reason": "None of the choices clearly fit your profile."}
    return {
        "answer": "",
        "reason": "Nothing in your resume covers this - type your own.",
    }
