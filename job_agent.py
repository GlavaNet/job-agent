# job_agent.py
import logging
import os

from logger import setup_logging

setup_logging()
logger = logging.getLogger(__name__)

from config import RESUME_PATH
from database import update_status, upsert_job
from job_fetcher import fetch_all_jobs
from job_scorer import filter_and_score_jobs
from job_validator import run_validation
from manual_job_scraper import run as run_manual_jobs
from preference_engine import update_preference_profile
from resume_parser import load_resume


def main() -> None:
    logger.info("=== Job Agent Starting ===")

    # 1. Load résumé
    logger.info("Loading résumé from %s…", RESUME_PATH)
    resume_text = load_resume(RESUME_PATH)
    logger.info("  → %d characters extracted", len(resume_text))

    # 2. Fetch jobs from all sources
    jobs = fetch_all_jobs()

    # 3. Score and filter
    matched_jobs = filter_and_score_jobs(jobs, resume_text)

    if not matched_jobs:
        logger.info("No new jobs matched the threshold.")
    else:
        logger.info("=== Matched Jobs (pre-validation) ===")
        for job in matched_jobs:
            logger.info(
                "  [%d/10] %s @ %s (%s) — %s",
                job["score"], job["title"], job["company"],
                job["source"], job.get("salary", "Not specified"),
            )
            logger.info("         %s", job["url"])

        # 4. Full-page validation — scan complete job descriptions
        valid_jobs, disqualified_jobs = run_validation(matched_jobs)

        # Auto-withdraw disqualified jobs so the preference engine
        # can learn from them
        for job in disqualified_jobs:
            row_id = upsert_job(job)
            if row_id and row_id != -1:
                update_status(
                    row_id,
                    "withdrawn",
                    notes=(
                        "Auto-withdrawn after full description scan: "
                        + job.get(
                            "_disqualification_reason",
                            "disqualifying content found",
                        )
                    ),
                )

        if not valid_jobs:
            logger.info("All matched jobs were disqualified during validation.")
        else:
            logger.info("=== Validated Matched Jobs ===")
            for job in valid_jobs:
                logger.info(
                    "  [%d/10] %s @ %s (%s) — %s",
                    job["score"], job["title"], job["company"],
                    job["source"], job.get("salary", "Not specified"),
                )
                logger.info("         %s", job["url"])

            # 5. Save validated jobs — cover letters generated on demand
            #    via the dashboard "Generate Cover Letter" button.
            for job in valid_jobs:
                upsert_job(job)

    # 6. Process any manually submitted job URLs
    manual_file = "manual_jobs.txt"
    if os.path.exists(manual_file):
        with open(manual_file, encoding="utf-8") as fh:
            manual_urls = [
                line.strip()
                for line in fh
                if line.strip() and not line.startswith("#")
            ]
        if manual_urls:
            logger.info("=== Processing Manual Job URLs ===")
            run_manual_jobs(manual_file)

    # 7. Update preference profile from latest application history
    logger.info("=== Updating Preference Profile ===")
    update_preference_profile()

    logger.info("=== All Done! ===")


if __name__ == "__main__":
    main()
