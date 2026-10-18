from __future__ import annotations

import pytest

from job_applier.automation.browser_automator import BrowserAutomator


@pytest.mark.parametrize(
    "label",
    ["Apply", "Apply Now", "I'm Interested", "  interested "],
)
def test_apply_labels_are_clickable(label):
    assert BrowserAutomator._instahyre_is_apply_text(label)


@pytest.mark.parametrize(
    "label",
    ["", "Applied", "Not Interested", "Withdrawn", "View details"],
)
def test_other_labels_are_not_clicked(label):
    assert not BrowserAutomator._instahyre_is_apply_text(label)


def test_open_card_with_both_buttons_is_not_treated_as_done():
    words = BrowserAutomator.INSTAHYRE_CARD_DONE_WORDS
    card_text = "backend engineer\ninterested\nnot interested"
    assert not any(w in card_text for w in words)
