"""Finds an already-answered question that means the same thing.

Screeners ask the same handful of things in endlessly different wording -
"Notice period (in days)", "What is your notice period?", "How soon can
you join?" - so keying answers on their exact text means answering every
phrasing again. This matches on what a question is *about*, and only what
survives that lands in the Tracker.

Matching deliberately refuses near-misses that would flip the answer:
current vs expected salary, monthly vs annual, and negated phrasings
("are you NOT able to...") all count as different questions.
"""

from __future__ import annotations

import re

# What a question is about. Keys are arbitrary tags; the point is that two
# questions sharing a tag set are asking the same thing.
TOPICS: dict[str, tuple[str, ...]] = {
    "salary": ("salary", "ctc", "compensation", "remuneration", "package",
               "pay", "lpa", "stipend"),
    "notice": ("notice", "join", "joining", "onboard", "start date",
               "available to start", "how soon"),
    "buyout": ("buyout", "buy out", "buy-out"),
    "experience": ("experience", "yrs", "years"),
    "work_auth": ("authorized", "authorised", "work permit", "right to work",
                  "eligible to work", "legally eligible", "sponsorship",
                  "visa", "work authorization", "work authorisation"),
    "relocate": ("relocate", "relocation", "willing to move"),
    "location": ("location", "city", "reside", "residence", "based in",
                 "hometown", "postal", "zip", "pin code"),
    "phone": ("phone", "mobile", "contact number", "whatsapp"),
    "email": ("email", "e-mail"),
    "education": ("degree", "graduat", "qualification", "education",
                  "university", "college", "b.tech", "bachelor", "master"),
    "language": ("english", "hindi", "language", "fluen", "proficien",
                 "speak", "read and write"),
    "gender": ("gender",),
    "disability": ("disab", "differently abled"),
    "veteran": ("veteran", "military", "armed forces"),
    "ethnicity": ("race", "ethnic", "caste", "community"),
    "non_compete": ("non-compete", "non compete", "noncompete",
                    "restrictive covenant", "bond"),
    "conflict": ("conflict of interest", "relative", "family member",
                 "related to any"),
    "background": ("background check", "background verification", "bgv",
                   "reference check"),
    "criminal": ("convicted", "criminal", "felony", "arrested"),
    "workplace": ("hybrid", "work from office", "work from home", "remote",
                  "onsite", "on-site", "in office", "in-office", "commute",
                  "days a week", "days per week"),
    "shift": ("shift", "night", "rotational", "weekend", "on-call", "on call"),
    "source": ("referral", "referred", "how did you hear", "where did you hear",
               "how you came to know"),
    "links": ("linkedin profile", "linkedin url", "github", "portfolio",
              "personal website"),
    "certification": ("certification", "certified", "certificate"),
    "rating": ("rate your", "rate yourself", "proficiency in", "scale of",
               "out of 10", "out of 5"),
    "notice_serving": ("serving notice", "currently serving"),
    "employer": ("current employer", "current company", "present employer",
                 "organization you work"),
    "title": ("current designation", "current title", "current role", "job title"),
    "offers": ("other offer", "offers in hand", "in hand offer", "interview process"),
    "age": ("date of birth", "age", "dob"),
    "maritial": ("marital", "married"),
    "notice_immediate": ("immediate joiner", "immediately available"),
}

# Axes that split a topic in two. A mismatch here is a different question,
# never a near-match: answering "expected CTC" with the current one is the
# kind of mistake that is invisible until an offer comes in low.
AXES: dict[str, dict[str, tuple[str, ...]]] = {
    "when": {
        "current": ("current", "present", "existing", "drawing", "last drawn",
                    "presently", "today"),
        "expected": ("expected", "desired", "expectation", "asking",
                     "looking for", "would you like", "require"),
    },
    "period": {
        "monthly": ("month", "monthly", "per month", "in hand", "in-hand",
                    "take home", "take-home"),
        "annual": ("annual", "yearly", "per annum", "per year", "ctc", "lpa"),
    },
}

# Axes only matter where the distinction changes the answer.
AXIS_TOPICS = {"when": {"salary", "employer", "title", "location"},
               "period": {"salary"}}

NEGATIONS = ("not ", "n't", "unable", "unwilling", "never", " no longer",
             "without ", "cannot", "can not", "don't", "do not")

STOPWORDS = frozenset("""
a an the is are am do does did you your yours we our us i me my mine of in on
at to for from with and or if any as be been being have has had will would
can could shall should may might must this that these those it its please
provide enter specify tell about what which how many much long when where who
whom whose why yes no per number value years year field required optional
question answer applicant candidate role position job company kind confirm
state mention give us select choose type write
""".split())


def _clean(question: str) -> str:
    text = question.splitlines()[0] if question.strip() else question
    text = text.replace("*", " ").lower()
    return " ".join(text.split())


def _words(question: str) -> set[str]:
    raw = re.findall(r"[a-z0-9.+#]+", _clean(question))
    out = set()
    for w in raw:
        w = w.strip(".")
        if len(w) < 2 or w in STOPWORDS:
            continue
        # Crude stemming is enough here: "years"/"year", "offers"/"offer".
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def topics(question: str) -> frozenset[str]:
    """Which subjects a question touches."""
    text = _clean(question)
    return frozenset(
        tag for tag, needles in TOPICS.items() if any(n in text for n in needles)
    )


def axes(question: str, tags: frozenset[str]) -> dict[str, str]:
    """The current/expected and monthly/annual sense of a question, where
    that distinction matters for the topics it covers."""
    text = _clean(question)
    found: dict[str, str] = {}
    for axis, values in AXES.items():
        if not (tags & AXIS_TOPICS[axis]):
            continue
        for value, needles in values.items():
            if any(n in text for n in needles):
                found[axis] = value
                break
    return found


def _negated(question: str) -> bool:
    return any(n in _clean(question) for n in NEGATIONS)


def similarity(a: str, b: str) -> float:
    """Word overlap, ignoring filler - 1.0 means the same content words."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def same_question(new: str, stored: str) -> str:
    """Why these two questions are the same thing, or "" if they aren't."""
    if _negated(new) != _negated(stored):
        return ""  # a Yes/No answer would come out backwards

    new_tags, stored_tags = topics(new), topics(stored)
    if new_tags and new_tags == stored_tags:
        if axes(new, new_tags) != axes(stored, stored_tags):
            return ""
        shared = ", ".join(sorted(new_tags))
        if similarity(new, stored) >= 0.25 or len(new_tags) > 1:
            return f"same subject ({shared})"
        return ""

    # No topic in common and no tags to go on - fall back to wording, but
    # only on a strong overlap of real content words.
    if new_tags and stored_tags and new_tags != stored_tags:
        return ""
    score = similarity(new, stored)
    if score >= 0.7 and len(_words(new) & _words(stored)) >= 3:
        return f"near-identical wording ({int(score * 100)}%)"
    return ""


YES_NO = frozenset(("yes", "no", "true", "false"))
ASKS_YES_NO = frozenset("""
do does did are is was were have has had can could will would shall should
may might must
""".split())
ASKS_VALUE = frozenset("what which how where when who".split())


def answer_fits(question: str, answer: str, options: list[str] | None = None) -> bool:
    """Guards against reusing an answer of the wrong shape.

    "Do you reside in India?" and "Which city do you reside in?" are the
    same subject, but carrying "Yes" over to the second one types Yes into
    a city box - which is how this went wrong before the guard existed.
    An answer the question actually offers as a choice is always fine, however
    long it reads.
    """
    answer = (answer or "").strip()
    if not answer:
        return False
    low = answer.lower()
    # Word-boundary match: "no" must not match inside "not sure".
    if options and any(
        low == o.strip().lower() or re.search(rf"\b{re.escape(low)}\b", o.lower())
        for o in options
    ):
        return True
    text = _clean(question)
    first = text.split()[0] if text else ""
    wants_yes_no = first in ASKS_YES_NO
    wants_value = first in ASKS_VALUE or "share your" in text or "enter your" in text

    if answer.lower() in YES_NO:
        return not wants_value or wants_yes_no
    if wants_yes_no and not wants_value:
        # A number or a sentence is not an answer to a Yes/No screener.
        return not (answer.replace(".", "").isdigit() or len(answer.split()) > 4)
    return True


def find_match(
    question: str,
    stored_questions: dict[str, str],
    stored_answers: dict[str, str] | None = None,
    options: list[str] | None = None,
) -> tuple[str, str]:
    """Picks the stored question that best answers this one.

    `stored_questions` maps a store key to the question text, and
    `stored_answers` the same keys to what was answered - given both, an
    answer whose shape doesn't suit the new question is skipped. Returns
    (key, why) or ("", "").
    """
    best_key, best_why, best_score = "", "", 0.0
    for key, text in stored_questions.items():
        if not text:
            continue
        why = same_question(question, text)
        if not why:
            continue
        if stored_answers and not answer_fits(
            question, stored_answers.get(key, ""), options
        ):
            continue
        score = similarity(question, text)
        if score >= best_score:
            best_key, best_why, best_score = key, why, score
    return best_key, best_why
