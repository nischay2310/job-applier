from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from job_applier.utils import get_project_root

_lock = threading.Lock()


def _store_path() -> Path:
    return get_project_root() / "data" / "screening_answers.json"


def _normalize(question: str) -> str:
    """Key questions loosely so the same question phrased with different
    whitespace, casing or a trailing required-marker still matches."""
    cleaned = question.replace("*", " ").replace("\n", " ").strip().lower()
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


def get_answer(question: str) -> str:
    """Returns a previously supplied answer for this question, if any."""
    key = _normalize(question)
    with _lock:
        data = _load()
        answers = data["answers"]
        if key in answers:
            return str(answers[key].get("answer", ""))
        # Fall back to a containment match so slight rewordings between
        # postings still reuse the answer the candidate already gave.
        for stored_key, entry in answers.items():
            if stored_key and (stored_key in key or key in stored_key):
                return str(entry.get("answer", ""))
    return ""


def record_pending(
    question: str,
    options: list[str] | None = None,
    company: str = "",
    job_title: str = "",
    required: bool = True,
) -> None:
    """Records a question the automation could not answer, so it can be put
    in front of the candidate instead of being guessed at."""
    key = _normalize(question)
    if not key:
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
            "question": question.replace("*", "").strip(),
            "options": options or existing.get("options") or [],
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
