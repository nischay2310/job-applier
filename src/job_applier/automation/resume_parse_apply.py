from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from job_applier.automation.question_store import (  # type: ignore[import-not-found]
    record_pending,
)
from job_applier.utils import get_project_root


class ResumeParseApplyHandler:
    """Handles the 'upload your resume and we'll fill the form' style of
    application (Outmarket and similar ATS).

    The site does the parsing: attach the resume, wait for it to populate the
    fields itself, then only fill what it left empty. Anything still blank
    that can't be answered confidently is recorded for the candidate instead
    of being guessed at.
    """

    UPLOAD_SETTLE_SECONDS = 3
    PARSE_TIMEOUT_SECONDS = 25

    def __init__(self, automator: Any):
        self.automator = automator

    @property
    def page(self) -> Any:
        return self.automator.page

    def _root(self) -> Any:
        return self.automator._form_root()

    def _resume_path(self, app_dir: Path) -> Path | None:
        default_resume = get_project_root() / "data" / "default_resume.pdf"
        if default_resume.exists():
            return default_resume
        return next(app_dir.glob("CV_*.pdf"), None)

    def upload_resume(self, app_dir: Path) -> bool:
        resume = self._resume_path(app_dir)
        if resume is None or not resume.exists():
            return False
        try:
            file_inputs = self._root().locator("input[type=file]")
            if file_inputs.count() == 0:
                return False
            file_inputs.first.set_input_files(str(resume.resolve()))
            time.sleep(self.UPLOAD_SETTLE_SECONDS)
            return True
        except Exception as e:
            print(f"Notice: resume upload failed: {e}")
            return False

    def wait_for_parse(self) -> int:
        """Waits for the site to populate fields from the uploaded resume.

        Returns the number of populated fields once the count stops growing,
        which is the signal that parsing has settled.
        """
        stable_for = 0
        last_count = -1
        waited = 0
        while waited < self.PARSE_TIMEOUT_SECONDS:
            count = self._filled_field_count()
            if count == last_count:
                stable_for += 1
                if stable_for >= 2 and count > 0:
                    return count
            else:
                stable_for = 0
            last_count = count
            time.sleep(2)
            waited += 2
        return max(last_count, 0)

    def _filled_field_count(self) -> int:
        try:
            return int(
                self._root().evaluate("""root => {
                const els = root.querySelectorAll(
                    "input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=file]):not([type=checkbox]):not([type=radio]), textarea, select"
                );
                let n = 0;
                for (const el of els) {
                    if ((el.value || '').trim()) n++;
                }
                return n;
            }""")
            )
        except Exception:
            return 0

    def empty_fields(self) -> list[dict[str, Any]]:
        """Fields the resume parse left blank, with their question text."""
        try:
            return (
                self._root().evaluate("""root => {
                const els = Array.from(root.querySelectorAll(
                    "input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=file]):not([type=checkbox]):not([type=radio]):not([type=password]), textarea, select"
                ));
                return els.filter(el => !(el.value || '').trim() && el.offsetParent !== null)
                    .map((el, idx) => {
                        const lbl = el.labels && el.labels[0] ? el.labels[0].innerText : '';
                        const aria = el.getAttribute('aria-label') || '';
                        const ph = el.getAttribute('placeholder') || '';
                        const label = (lbl || aria || ph || '').trim();
                        return {
                            index: idx,
                            label: label,
                            required: el.required
                                || el.getAttribute('aria-required') === 'true'
                                || /\\*/.test(lbl),
                            tag: el.tagName.toLowerCase(),
                        };
                    })
                    .filter(f => f.label.length > 2);
            }""")
                or []
            )
        except Exception:
            return []

    def fill_remaining(
        self, report: dict[str, Any], job_title: str, company: str
    ) -> None:
        """Fills whatever the resume parse left blank, flagging the rest."""
        selector = (
            "input:not([type=hidden]):not([type=submit]):not([type=button])"
            ":not([type=file]):not([type=checkbox]):not([type=radio])"
            ":not([type=password]), textarea"
        )
        inputs = self._root().locator(selector)
        for i in range(inputs.count()):
            try:
                el = inputs.nth(i)
                if not el.is_visible():
                    continue
                if (el.input_value() or "").strip():
                    continue

                label = self.automator._get_question_label(el)
                if len(label) < 3:
                    continue

                answer = self.automator.question_solver.answer_question(
                    label, job_title=job_title, company=company
                )
                if answer:
                    el.fill(answer)
                    self.automator._highlight_element(el)
                    report["fields_filled"].append(f"Question: {label[:30]}")
                    report["questions_answered"].append(
                        {"question": label[:50], "answer": answer}
                    )
                else:
                    record_pending(
                        label,
                        company=company,
                        job_title=job_title,
                        required=self.automator._is_required(el),
                    )
                    report.setdefault("needs_review", []).append(label[:120])
                    self.automator._highlight_element(el, color="#ff9800")
            except Exception:
                continue

    def apply(
        self, app_dir: Path, job_title: str = "", company: str = ""
    ) -> dict[str, Any]:
        """Runs the upload -> parse -> fill-the-gaps flow for one application."""
        report: dict[str, Any] = {
            "fields_filled": [],
            "resume_uploaded": False,
            "cover_letter_filled": False,
            "questions_answered": [],
            "needs_review": [],
            "platform": self.automator.detect_platform(self.page.url),
        }

        report["resume_uploaded"] = self.upload_resume(app_dir)
        if report["resume_uploaded"]:
            parsed = self.wait_for_parse()
            self.automator._notify(
                "form_detection",
                f"Resume parsed by site - {parsed} field(s) auto-populated, filling the rest.",
            )

        self.fill_remaining(report, job_title, company)
        self.automator._answer_radio_groups(report, job_title, company)
        self.automator._fill_select_inputs(report, job_title, company)
        self.automator._check_consent_boxes()
        return report
