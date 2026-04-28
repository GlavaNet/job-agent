# follow_up_checker.py
"""
Scans the jobs database for applications that have gone stale —
i.e. jobs still sitting in 'applied' status after FOLLOWUP_DAYS
days with no progression — and fires an ntfy notification for each.

Definition of "stale":
  - status == 'applied'
  - date_applied is set and is more than FOLLOWUP_DAYS days ago
  - status has not since moved to interviewing / offer / rejected / withdrawn

This module has no side effects on the database. It only reads.

Triggered automatically at the end of each successful pipeline run
via runner._run_pipeline(). Can also be run standalone:

    python3 follow_up_checker.py
    python3 follow_up_checker.py --days 7    # override threshold
    python3 follow_up_checker.py --dry-run   # print without notifying
"""

import argparse
import logging
from datetime import datetime, timedelta

from config import FOLLOWUP_DAYS
from models import Job
from notifier import notify_followup_needed

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core check
# ---------------------------------------------------------------------------

def get_stale_applications(days: int | None = None) -> list[Job]:
    """
    Return jobs that have been in 'applied' status for longer than
    `days` days (defaults to FOLLOWUP_DAYS from config).

    Only jobs with a valid date_applied are considered — jobs applied
    to manually without a recorded date are excluded to avoid noise.
    """
    from database import get_all_jobs

    threshold_days = days if days is not None else FOLLOWUP_DAYS
    cutoff = datetime.now() - timedelta(days=threshold_days)

    applied_jobs = get_all_jobs(status="applied")
    stale: list[Job] = []

    for job in applied_jobs:
        date_str = job.get("date_applied", "")
        if not date_str:
            continue
        try:
            applied_date = datetime.strptime(date_str, "%Y-%m-%d")
            if applied_date < cutoff:
                stale.append(job)
        except ValueError:
            logger.warning(
                "Could not parse date_applied '%s' for job %d — skipping",
                date_str, job.get("id", -1),
            )

    return stale


def check_and_notify(days: int | None = None, dry_run: bool = False) -> int:
    """
    Check for stale applications and fire one ntfy notification per
    stale job. Returns the number of stale applications found.

    dry_run=True logs what would be sent without actually notifying.
    """
    threshold_days = days if days is not None else FOLLOWUP_DAYS
    stale = get_stale_applications(days=threshold_days)

    if not stale:
        logger.info(
            "[FollowUp] No stale applications (threshold: %d days)",
            threshold_days,
        )
        return 0

    logger.info(
        "[FollowUp] %d stale application(s) found (threshold: %d days)",
        len(stale), threshold_days,
    )

    for job in stale:
        title = job.get("title", "Unknown role")
        company = job.get("company", "Unknown company")
        date_applied = job.get("date_applied", "unknown date")
        url = job.get("url", "")

        try:
            applied_dt = datetime.strptime(date_applied, "%Y-%m-%d")
            days_ago = (datetime.now() - applied_dt).days
            age_str = f"{days_ago} day{'s' if days_ago != 1 else ''} ago"
        except ValueError:
            age_str = f"applied {date_applied}"

        logger.info(
            "[FollowUp] Stale: %s @ %s (applied %s)",
            title, company, age_str,
        )

        if not dry_run:
            notify_followup_needed(job, age_str)

    return len(stale)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from logger import setup_logging
    setup_logging()

    parser = argparse.ArgumentParser(
        description="Check for stale job applications and send follow-up reminders"
    )
    parser.add_argument(
        "--days", type=int, default=None,
        help=f"Override the staleness threshold (default: {FOLLOWUP_DAYS} days from config)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print stale applications without sending notifications",
    )
    args = parser.parse_args()

    count = check_and_notify(days=args.days, dry_run=args.dry_run)

    if args.dry_run:
        print(f"Dry run: {count} stale application(s) would trigger notifications.")
    else:
        print(f"Done. {count} follow-up notification(s) sent.")
