from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from job_applier.automation.question_match import find_match
from job_applier.utils import get_project_root

_lock = threading.Lock()


def _store_path() -> Path:
    return get_project_root() / "data" / "screening_answers.json"


def _first_line(question: str) -> str:
    """LinkedIn repeats label text for screen readers ("Q?\\nQ?") - one copy."""
    for line in question.splitlines():
        if line.strip():
            return line.strip()
    return question.strip()


def _normalize(question: str) -> str:
    """Key questions loosely so the same question phrased with different
    whitespace, casing or a trailing required-marker still matches."""
    cleaned = _first_line(question).replace("*", " ").strip().lower()
    return " ".join(cleaned.split())


def _load() -> dict[str, Any]:
    path = _store_path()
    if not path.exists():
        return {"answers": {}, "pending": {}}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("answers", {})
        data.setdefault("pending", {})
        return data
    except Exception:
        return {"answers": {}, "pending": {}}


def _save(data: dict[str, Any]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def match_answer(question: str, options: list[str] | None = None) -> tuple[str, str]:
    """The stored answer for this question and how it was found.

    Exact text first, then a same-subject scan over everything already
    answered - two postings rarely word a screener the same way, and
    asking again for what amounts to the same question is the thing this
    store exists to avoid.
    """
    key = _normalize(question)
    if not key:
        return "", ""
    with _lock:
        answers = _load()["answers"]
    if key in answers:
        return str(answers[key].get("answer", "")), "exact match"

    texts = {k: str(v.get("question") or k) for k, v in answers.items()}
    given = {k: str(v.get("answer") or "") for k, v in answers.items()}
    matched, why = find_match(question, texts, given, options)
    if matched:
        return str(answers[matched].get("answer", "")), f"{why}: {texts[matched][:60]}"
    return "", ""


def get_answer(question: str, options: list[str] | None = None) -> str:
    """Returns a previously supplied answer for this question, if any."""
    answer, why = match_answer(question, options)
    if answer and why != "exact match":
        print(f"   Reusing a stored answer - {why}")
    return answer


def record_pending(
    question: str,
    options: list[str] | None = None,
    company: str = "",
    job_title: str = "",
    required: bool = True,
    kind: str = "text",
) -> None:
    """Records a question the automation could not answer, so it can be put
    in front of the candidate instead of being guessed at."""
    key = _normalize(question)
    if not key:
        return
    # Something already answered may cover this wording - don't ask twice.
    if match_answer(question)[0]:
        return
    with _lock:
        data = _load()
        if key in data["answers"]:
            return
        existing = data["pending"].get(key, {})
        seen_on = existing.get("seen_on", [])
        label = f"{company} - {job_title}".strip(" -")
        if label and label not in seen_on:
            seen_on.append(label)
        data["pending"][key] = {
            "question": _first_line(question).replace("*", "").strip(),
            "options": options or existing.get("options") or [],
            # Which control the site used, so the Tracker can ask the same
            # way - radios as radios, a dropdown as a dropdown.
            "kind": kind or existing.get("kind") or "text",
            "required": required or existing.get("required", False),
            "seen_on": seen_on,
            "times_seen": int(existing.get("times_seen", 0)) + 1,
            "first_seen": existing.get(
                "first_seen", datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ),
        }
        _save(data)


def save_answer(question: str, answer: str) -> None:
    """Stores the candidate's answer and clears it from the pending list."""
    key = _normalize(question)
    if not key:
        return
    with _lock:
        data = _load()
        pending = data["pending"].pop(key, {})
        data["answers"][key] = {
            "question": pending.get("question", question),
            "answer": answer,
            "options": pending.get("options", []),
            "answered_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save(data)


def list_pending() -> list[dict[str, Any]]:
    with _lock:
        data = _load()
        items = []
        for key, entry in data["pending"].items():
            items.append({"key": key, **entry})
    items.sort(key=lambda i: (not i.get("required"), -int(i.get("times_seen", 0))))
    return items


def list_answers() -> list[dict[str, Any]]:
    with _lock:
        data = _load()
        return [{"key": k, **v} for k, v in data["answers"].items()]


def delete_answer(question_key: str) -> None:
    with _lock:
        data = _load()
        data["answers"].pop(question_key, None)
        _save(data)


def dismiss_pending(question_key: str) -> bool:
    """Drops a question from the pending list without answering it.

    For the ones that arrive mangled - a form whose question text couldn't be
    read properly - there is nothing worth answering, and leaving it there
    just hides the questions that matter.
    """
    with _lock:
        data = _load()
        removed = data["pending"].pop(question_key, None) is not None
        if removed:
            _save(data)
    return removed
