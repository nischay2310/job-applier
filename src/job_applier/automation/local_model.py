"""Asks the small model running on this computer (Ollama) a question.

Used when a screening question isn't covered by the profile rules and there
is no Gemini key. Nothing leaves the machine.
"""

from __future__ import annotations

import json
import os
import urllib.request

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")

UNKNOWN = "UNKNOWN"


def is_running() -> bool:
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=1.5):
            return True
    except Exception:
        return False


def ask(prompt: str, timeout: int = 90) -> str:
    """The model's reply, or "" if Ollama isn't there or fails."""
    request = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(
            {
                "model": OLLAMA_MODEL,
                "stream": False,
                "options": {"temperature": 0},
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return str(json.load(response)["message"]["content"]).strip()
    except Exception:
        return ""
