from __future__ import annotations

import os
import shutil
import time

import pandas as pd

from job_applier.automation.autofill_script import (  # type: ignore[import-not-found]
    save_autofill_assets,
)
from job_applier.automation.browser_automator import (  # type: ignore[import-not-found]
    BrowserAutomator,
)
from job_applier.automation.candidate_profile import (  # type: ignore[import-not-found]
    load_candidate_profile,
)
from job_applier.config import (  # type: ignore[import-not-found]
    get_platforms_config,
)
from job_applier.dedup import (  # type: ignore[import-not-found]
    get_existing_applications_index,
    get_role_signature,
    is_duplicate_application,
    normalize_job_url,
)
from job_applier.logger import log_event  # type: ignore[import-not-found]
from job_applier.resume.resume import generate_resume
from job_applier.resume.tailor_cv import tailor_resume
from job_applier.scrapers.activity_checker import (  # type: ignore[import-not-found]
    is_job_active,
)
from job_applier.scrapers.job_filters import (  # type: ignore[import-not-found]
    MAX_JOB_AGE_DAYS,
    exceeds_experience_cap,
    is_excluded_application_url,
    is_older_than,
)
from job_applier.scrapers.job_scraper import fetch_jobs
from job_applier.scrapers.region_config import (  # type: ignore[import-not-found]
    RegionScope,
)
from job_applier.scrapers.url_resolver import resolve_application_url
from job_applier.tracker import record_application  # type: ignore[import-not-found]
from job_applier.utils import get_project_root, sanitize_name


def run_application_pipeline(
    api_key: str,
    search_terms: list[str] | None = None,
    locations: list[str] | None = None,
    results_wanted: int = 50,
    sites: list[str] | None = None,
    is_remote: bool = False,
    region: str = "EMEA",
    countries: list[str] | None = None,
    emea_only: bool = True,
    auto_apply: bool = False,
    autonomous: bool = False,
    headless: bool = False,
    easy_apply_only: bool = False,
) -> None:
    """
    Core pipeline service that scrapes job listings, predicts active status,
    tailors JSON resumes and cover letters via Google Gemini (gemini-3.8-flash),
    generates PDF packages, and optionally triggers autonomous/assisted browser submission.
    """
    project_root = get_project_root()
    master_resume = project_root / "data" / "master_resume.json"
    output_base = project_root / "output" / "applications"
    applied_base = project_root / "output" / "applied"
    processed_jobs_file = project_root / "data" / "processed_jobs.txt"

    output_base.mkdir(parents=True, exist_ok=True)
    applied_base.mkdir(parents=True, exist_ok=True)

    scope = RegionScope(region=region, countries=countries, strict=emea_only)
    candidate_profile = load_candidate_profile(master_resume_path=master_resume)

    active_terms = list(
        search_terms
        or candidate_profile.target_roles
        or get_platforms_config().get("default_search_roles")
        or [
            "Software Engineer",
            "Solutions Architect",
            "DevOps Engineer",
            "Cloud Architect",
            "Python Dev",
        ]
    )
    if locations is None:
        locations = candidate_profile.target_locations or (
            [candidate_profile.city] if candidate_profile.city else ["Bucharest"]
        )

    # Load processed jobs
    processed_urls: set[str] = set()
    if processed_jobs_file.exists():
        try:
            with open(processed_jobs_file, encoding="utf-8") as f:
                processed_urls = set(line.strip() for line in f if line.strip())
        except Exception as e:
            log_event(
                f"Notice reading processed jobs: {e}", level="WARN", category="Pipeline"
            )

    automator: BrowserAutomator | None = None
    if auto_apply:
        instance = BrowserAutomator(profile=candidate_profile, headless=headless)
        try:
            instance.start()
            automator = instance
        except Exception as e:
            log_event(
                f"Could not start browser automator: {e}. Continuing without browser.",
                level="WARN",
                category="Pipeline",
            )
            automator = None

    existing_urls, existing_roles = get_existing_applications_index(project_root)

    try:
        for term in active_terms:
            log_event(f"--- Processing jobs for: {term} ---", category="Pipeline")
            csv_path = fetch_jobs(
                search_term=term,
                locations=locations,
                results_wanted=results_wanted,
                sites=sites,
                is_remote=is_remote,
                region=region,
                countries=countries,
                emea_only=emea_only,
                easy_apply_only=easy_apply_only,
            )

            if not csv_path or not os.path.exists(csv_path):
                continue

            try:
                df = pd.read_csv(csv_path)
            except Exception as e:
                log_event(
                    f"Error reading scraped CSV: {e}",
                    level="ERROR",
                    category="Pipeline",
                )
                continue

            run_stats = {
                "queued": 0,
                "already queued/applied": 0,
                "older than 3 days": 0,
                "over 7 yrs experience": 0,
                "expired/closed": 0,
            }
            for index, row in df.iterrows():
                company = sanitize_name(str(row.get("company", "Unknown_Company")))
                job_title = sanitize_name(str(row.get("title", "Position")))
                jd_text = str(row.get("description", ""))
                job_url_raw = str(row.get("job_url", "")).strip()
                job_url_direct = str(row.get("job_url_direct", "")).strip()

                if not job_url_raw or job_url_raw == "URL not found":
                    continue

                # Skip if already handled this run, or rejected as expired before
                if job_url_raw in processed_urls:
                    run_stats["already queued/applied"] += 1
                    continue

                if is_older_than(row.get("date_posted"), MAX_JOB_AGE_DAYS):
                    run_stats["older than 3 days"] += 1
                    processed_urls.add(job_url_raw)
                    continue

                over_cap, cap_reason = exceeds_experience_cap(
                    f"{job_title} {jd_text}"
                )
                if over_cap:
                    log_event(
                        f"Skipping over-experience job: {company} - {job_title} ({cap_reason})",
                        level="INFO",
                        category="Pipeline",
                    )
                    run_stats["over 7 yrs experience"] += 1
                    processed_urls.add(job_url_raw)
                    continue

                # Resolve the actual application form website (bypasses aggregator landing pages)
                job_url, detected_platform = resolve_application_url(
                    job_url=job_url_raw,
                    job_url_direct=job_url_direct if job_url_direct else None,
                )

                if is_excluded_application_url(job_url) or is_excluded_application_url(
                    job_url_raw
                ):
                    log_event(
                        f"Skipping excluded platform: {company} - {job_title}",
                        level="INFO",
                        category="Pipeline",
                    )
                    processed_urls.add(job_url_raw)
                    continue

                # Deduplication check by canonical URL and (company, title) signature
                is_dup, dup_reason = is_duplicate_application(
                    job_url=job_url,
                    company=company,
                    title=job_title,
                    existing_urls=existing_urls,
                    existing_roles=existing_roles,
                )
                if is_dup:
                    log_event(
                        f"Skipping duplicate job: {company} - {job_title} ({dup_reason})",
                        level="INFO",
                        category="Pipeline",
                    )
                    run_stats["already queued/applied"] += 1
                    processed_urls.add(job_url_raw)
                    continue

                # Validate regional compatibility
                is_comp, comp_reason = scope.is_compatible(
                    str(row.get("location", "")), jd_text
                )
                if not is_comp:
                    log_event(
                        f"Skipping non-matching job for {scope.region}: {company} - {job_title} ({comp_reason})",
                        level="INFO",
                        category="Pipeline",
                    )
                    processed_urls.add(job_url_raw)
                    continue

                # Predict and verify if the job posting is active and accepting applications
                is_active, active_reason = is_job_active(job_url)
                if not is_active:
                    log_event(
                        f"Skipping inactive/expired job: {company} - {job_title} ({active_reason})",
                        level="WARN",
                        category="Pipeline",
                    )
                    run_stats["expired/closed"] += 1
                    processed_urls.add(job_url_raw)
                    try:
                        with open(processed_jobs_file, "a", encoding="utf-8") as f:
                            f.write(f"{job_url_raw}\n")
                    except Exception:
                        pass
                    continue

                if not jd_text or len(jd_text) < 50:
                    jd_text = f"Role: {job_title} at {company}. (Full description unavailable, please assume standard industry requirements for this role.)"

                app_id = f"{company}_{job_title}_{index}"
                app_dir = output_base / app_id
                app_dir.mkdir(parents=True, exist_ok=True)

                # Keep the posting text alongside the application - open-ended
                # questions ("what interests you about us?") are answered from
                # what the role actually asks for.
                try:
                    with open(
                        app_dir / "job_description.txt", "w", encoding="utf-8"
                    ) as f:
                        f.write(jd_text)
                except Exception as e:
                    log_event(
                        f"Could not write job_description.txt: {e}",
                        level="WARN",
                        category="Pipeline",
                    )

                # Save the application link for easy access
                try:
                    with open(app_dir / "APPLY_HERE.txt", "w", encoding="utf-8") as f:
                        f.write(job_url)
                except Exception as e:
                    log_event(
                        f"Could not write APPLY_HERE.txt: {e}",
                        level="WARN",
                        category="Pipeline",
                    )

                # Tag whether this job was scraped under the Easy-Apply-only
                # filter. jobspy doesn't expose easy-apply status per job, only
                # as a search-time filter — so "true" is only ever written
                # when that filter was actually on for this run.
                try:
                    with open(app_dir / "EASY_APPLY.txt", "w", encoding="utf-8") as f:
                        f.write("true" if easy_apply_only else "unknown")
                except Exception:
                    pass

                log_event(
                    f"Tailoring application for: {company} - {job_title}...",
                    category="Pipeline",
                )

                # 1. Tailor CV and get Cover Letter
                tailored_json, cover_letter = tailor_resume(
                    api_key=api_key,
                    master_resume_path=master_resume,
                    job_description=jd_text,
                    output_dir=app_dir,
                    custom_instructions=candidate_profile.custom_ai_instructions,
                    target_role=candidate_profile.current_title or job_title,
                )

                if not tailored_json:
                    log_event(
                        f"Resume tailoring failed or was interrupted for {company} - {job_title}. Cleaning up empty folder.",
                        level="WARN",
                        category="Pipeline",
                    )
                    shutil.rmtree(str(app_dir), ignore_errors=True)
                    continue

                # 2. Generate PDF
                pdf_output = app_dir / f"CV_{company}_{job_title}.pdf"
                generate_resume(tailored_json, str(pdf_output))

                # 3. Generate 1-Click Autofill Assets (Bookmarklet and JS)
                save_autofill_assets(
                    app_dir=app_dir,
                    profile=candidate_profile,
                    cover_letter=cover_letter or "",
                    cv_path=str(pdf_output.resolve()),
                )

                # Mark as processed in tracker
                record_application(
                    company=company,
                    title=job_title,
                    job_url=job_url,
                    platform=detected_platform,
                    status="auto_filled",
                    submission_type="pipeline_generated",
                    cv_path=str(pdf_output),
                )

                # Index for runtime deduplication within the same batch
                norm_u = normalize_job_url(job_url)
                if norm_u:
                    existing_urls[norm_u] = app_id
                sig = get_role_signature(company, job_title)
                if sig:
                    existing_roles[sig] = app_id

                # Remembered for this run only. Across runs the queue and
                # applied folders are the record: persisting queued jobs here
                # meant a cleared queue could never be refilled by a re-scrape.
                processed_urls.add(job_url)
                run_stats["queued"] += 1

                # 4. If --auto-apply is active, immediately run browser automation!
                if automator is not None:
                    if autonomous:
                        status, msg = automator.run_autonomous_apply(
                            app_dir=app_dir,
                            job_url=job_url,
                            company=company,
                            job_title=job_title,
                        )
                    else:
                        status, msg = automator.run_assisted_apply(
                            app_dir=app_dir,
                            job_url=job_url,
                            company=company,
                            job_title=job_title,
                        )

                    if status == "applied":
                        target_dir = applied_base / app_dir.name
                        try:
                            shutil.move(str(app_dir), str(target_dir))
                            log_event(
                                f"Successfully applied! Moved to {target_dir.name}",
                                level="SUCCESS",
                                category="Pipeline",
                            )
                        except Exception as e:
                            log_event(
                                f"Could not move {app_dir}: {e}",
                                level="WARN",
                                category="Pipeline",
                            )

                # Avoid hitting rate limits
                time.sleep(2)

            skipped = ", ".join(
                f"{n} {why}" for why, n in run_stats.items() if why != "queued" and n
            )
            log_event(
                f"'{term}': {len(df)} found, {run_stats['queued']} new jobs queued"
                + (f" (skipped: {skipped})" if skipped else ""),
                level="SUCCESS" if run_stats["queued"] else "INFO",
                category="Pipeline",
            )
    finally:
        if automator is not None:
            automator.close()

    log_event("Pipeline run completed!", level="SUCCESS", category="Pipeline")
