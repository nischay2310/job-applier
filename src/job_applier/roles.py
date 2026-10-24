"""The job titles the user is looking for.

Whatever is typed into the scraper is remembered here, so other parts of the
app (the Instahyre auto-apply) follow the same list instead of asking again.
Falls back to the roles in the candidate profile.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from job_applier.utils import get_project_root

# Words that don't help tell one job title from another.
FILLER_WORDS = {"a", "an", "and", "of", "the", "for", "in", "at", "to", "-"}


def roles_file() -> Path:
    override = os.environ.get("JOB_APPLIER_ROLES_PATH")
    if override:
        return Path(override)
    return get_project_root() / "data" / "scraper_roles.json"


def save_scraper_roles(terms: list[str]) -> None:
    roles = [term.strip() for term in terms if term.strip()]
    if not roles:
        return
    path = roles_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"roles": roles}, indent=2), encoding="utf-8")


def load_scraper_roles() -> list[str]:
    try:
        data = json.loads(roles_file().read_text(encoding="utf-8"))
        return [str(role) for role in data.get("roles", []) if str(role).strip()]
    except Exception:
        return []


def chosen_roles() -> list[str]:
    """The scraper's roles, or the profile's if the scraper hasn't been used."""
    roles = load_scraper_roles()
    if roles:
        return roles
    from job_applier.automation.candidate_profile import load_candidate_profile

    return [role for role in load_candidate_profile().target_roles if role.strip()]


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9+#]+", text.lower())


def title_matches_roles(title: str, roles: list[str]) -> bool:
    """True when every word of some role appears in the job title.

    "DevOps Engineer" matches "Senior DevOps Engineer (AWS)"; a short word
    also matches a longer one that starts with it ("Python Dev" matches
    "Python Developer"). Order and extra words in the title don't matter.
    """
    title_words = words(title)
    for role in roles:
        wanted = [w for w in words(role) if w not in FILLER_WORDS]
        if wanted and all(any(t.startswith(w) for t in title_words) for w in wanted):
            return True
    return False
