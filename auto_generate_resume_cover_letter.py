#!/usr/bin/env python3
"""
Auto-generate tailored resumes and cover letters for high-scoring jobs.

Runs daily via systemd timer. Finds all jobs with score >= 7 that don't
have a tailored_resume yet, generates both tailored resume and cover letter
for each, and sends a summary notification via ntfy.

Exit codes:
  0 = success (even if no jobs to process)
  1 = database error or missing config
  2 = notification failed (but generation may have succeeded)
"""
import argparse
import logging
import sys
from datetime import datetime

from database import (
    get_all_jobs,
    get_company_for_job,
    get_job_by_id,
)
from logger import setup_logging
from notifier import notify
from resume_tailor import generate_and_store_tailored_resume
from cover_letter import generate_and_store_cover_letter_for_job

logger = logging.getLogger(__name__)


def find_jobs_to_process(min_score: int = 8) -> list:
    """
    Find all jobs with score >= min_score that don't have a tailored_resume.
    Returns list of job dicts, sorted by score descending.
    """
    try:
        jobs = get_all_jobs(
            status="not applied",
            min_score=min_score, 
            order_by="score DESC"
        )
        unprocessed = [j for j in jobs if not j.get("tailored_resume")]
        logger.info("Found %d unprocessed jobs with score >= %d", len(unprocessed), min_score)
        return unprocessed
    except Exception as e:
        logger.exception("Failed to query jobs database")
        raise


def process_job(job_id: int) -> dict:
    """
    Process a single job: generate tailored resume and cover letter.
    Returns a dict with keys: success, job_id, title, company, resume_ok, letter_ok, errors.
    """
    result = {
        "success": False,
        "job_id": job_id,
        "title": "Unknown",
        "company": "Unknown",
        "resume_ok": False,
        "letter_ok": False,
        "errors": [],
    }

    job = get_job_by_id(job_id)
    if not job:
        result["errors"].append(f"Job {job_id} not found in database")
        return result

    result["title"] = job.get("title", "Unknown")
    result["company"] = job.get("company", "Unknown")

    logger.info(
        "[%d] Processing: %s @ %s [%d/10]",
        job_id, result["title"], result["company"], job.get("score", 0)
    )

    # Generate tailored resume
    try:
        resume_ok = generate_and_store_tailored_resume(job_id)
        result["resume_ok"] = resume_ok
        if not resume_ok:
            result["errors"].append("Tailored resume generation failed")
        else:
            logger.info("[%d] Tailored resume generated", job_id)
    except Exception as e:
        result["errors"].append(f"Resume error: {str(e)}")
        logger.exception("[%d] Tailored resume generation exception", job_id)

    # Generate cover letter (fetch company research first)
    try:
        research = get_company_for_job(job_id)
        letter_ok = generate_and_store_cover_letter_for_job(job_id, research)
        result["letter_ok"] = letter_ok
        if not letter_ok:
            result["errors"].append("Cover letter generation failed")
        else:
            logger.info("[%d] Cover letter generated", job_id)
    except Exception as e:
        result["errors"].append(f"Cover letter error: {str(e)}")
        logger.exception("[%d] Cover letter generation exception", job_id)

    result["success"] = result["resume_ok"] and result["letter_ok"]
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Auto-generate tailored resumes and cover letters for jobs scoring 7+"
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=7,
        help="Minimum job score to process (default: 7)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Query jobs but don't generate anything",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Don't send ntfy notification after completion",
    )
    args = parser.parse_args()

    setup_logging()

    logger.info("=" * 70)
    logger.info("Auto-generate: Starting (min_score=%d, dry_run=%s)", args.min_score, args.dry_run)

    try:
        jobs = find_jobs_to_process(min_score=args.min_score)
    except Exception as e:
        logger.error("Failed to query database: %s", e)
        return 1

    if not jobs:
        logger.info("No jobs to process")
        if not args.no_notify:
            try:
                notify(
                    title="Auto-generate: No jobs found",
                    message=f"No unprocessed jobs with score >= {args.min_score}",
                    priority="low",
                )
            except Exception as e:
                logger.warning("Failed to send notification: %s", e)
        return 0

    logger.info("Processing %d job(s)…", len(jobs))

    results = []
    for job in jobs:
        if args.dry_run:
            logger.info("[DRYRUN] %s @ %s [%d/10]", job.get("title"), job.get("company"), job.get("score"))
            results.append({
                "success": True,
                "job_id": job["id"],
                "title": job.get("title"),
                "company": job.get("company"),
                "resume_ok": True,
                "letter_ok": True,
                "errors": [],
            })
        else:
            result = process_job(job["id"])
            results.append(result)

    # Build summary
    successful = sum(1 for r in results if r["success"])
    failed = len(results) - successful

    logger.info("=" * 70)
    logger.info("Summary: %d succeeded, %d failed out of %d", successful, failed, len(results))

    for result in results:
        status = "✓" if result["success"] else "✗"
        logger.info(
            "  %s [%d] %s @ %s",
            status, result["job_id"], result["title"], result["company"]
        )
        if result["errors"]:
            for error in result["errors"]:
                logger.info("      → %s", error)

    # Send notification unless --no-notify
    if not args.no_notify:
        try:
            title = f"Auto-generate: {successful}/{len(results)} completed"
            message_lines = [
                f"Generated tailored resumes & cover letters",
                f"Successful: {successful}",
                f"Failed: {failed}",
                "",
                "Details:",
            ]
            for result in results:
                status = "✓" if result["success"] else "✗"
                message_lines.append(
                    f"  {status} [{result['job_id']}] {result['title']} @ {result['company']}"
                )
                if result["errors"]:
                    for err in result["errors"]:
                        message_lines.append(f"      • {err}")

            message = "\n".join(message_lines)
            priority = "default" if failed == 0 else "high"
            tags = ["briefcase", "memo"]

            notify(title=title, message=message, priority=priority, tags=tags)
            logger.info("Notification sent")
        except Exception as e:
            logger.error("Failed to send notification: %s", e)
            return 2

    logger.info("=" * 70)
    logger.info("Auto-generate: Complete")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
