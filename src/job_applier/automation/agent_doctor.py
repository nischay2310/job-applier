"""Works out why an agent looks stuck, and what to do about it.

The live view shows what an agent is doing but not whether it's actually
getting anywhere. These rules read an agent's last reported step and how
long ago it reported, and turn that into one sentence worth acting on -
plus whether restarting the job is the right move or would throw away a
submission that's mid-flight.
"""

from __future__ import annotations

import time

# Steps where a restart would interrupt something that can't be repeated
# safely: the click may already have gone through.
UNSAFE_TO_RESTART = ("submitting", "submit", "confirming")

STALL_WARN = 90
STALL_BAD = 240


def seconds_since(hhmmss: str) -> int:
    """How long ago an agent reported, from its HH:MM:SS timestamp."""
    if not hhmmss:
        return 0
    try:
        hh, mm, ss = (int(p) for p in hhmmss.split(":"))
    except ValueError:
        return 0
    now = time.localtime()
    delta = (now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec) - (
        hh * 3600 + mm * 60 + ss
    )
    if delta < 0:
        delta += 24 * 3600  # reported just before midnight
    return delta


def diagnose(agent: dict) -> dict:
    """Returns {stalled_for, severity, suggestion, can_restart} for one agent."""
    step = str(agent.get("step") or "").lower()
    message = str(agent.get("message") or "").lower()
    stalled = seconds_since(str(agent.get("updated_at") or ""))
    can_restart = not any(s in step for s in UNSAFE_TO_RESTART)

    def out(severity: str, suggestion: str, restart: bool = can_restart) -> dict:
        return {
            "stalled_for": stalled,
            "severity": severity,
            "suggestion": suggestion,
            "can_restart": restart,
        }

    if (
        "code" in message
        or "verification" in message
        or agent.get("is_waiting_for_code")
    ):
        return out(
            "blocked",
            "LinkedIn wants a verification code - enter it in the "
            "automation Chrome window, then let this job continue.",
        )
    if "sign in" in message or "login" in step or "checkpoint" in message:
        return out(
            "blocked",
            "The session needs a sign-in. Open the automation Chrome, "
            "log in to LinkedIn, then restart this job.",
        )
    if not can_restart:
        return out(
            "busy", "Submitting - leave this one alone until it finishes.", False
        )
    if stalled >= STALL_BAD:
        return out(
            "stuck",
            f"No progress for {stalled // 60}m. Restart this job; if it "
            "stalls in the same place, restart the whole app so Chrome "
            "comes back clean.",
        )
    if stalled >= STALL_WARN:
        if "form" in step or "fill" in step or "question" in step:
            return out(
                "slow",
                "The form hasn't moved in over a minute - usually a "
                "required question it can't answer. Check the Tracker, "
                "answer what's waiting there, then restart this job.",
            )
        return out(
            "slow",
            "Waiting on the page longer than usual. Give it a moment, "
            "then restart this job if nothing changes.",
        )
    return out("ok", "Running normally.")
