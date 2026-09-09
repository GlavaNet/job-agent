# job_tracker.py
# unified-db shim — manual_jobs.txt and processed_jobs.txt writes are
# replaced by the manual_queue table. SQLite's own transactional writer
# serialization (module-level _db_lock + WAL) replaces the fcntl.flock
# that used to guard manual_jobs.txt against concurrent writers.
import logging

from database import get_all_processed_urls, mark_manual_url_resolved
from models import Job

logger = logging.getLogger(__name__)

# Map old free-text statuses ("DONE", "BLOCKED", "FAILED", and the
# lowercase pipeline statuses like "cover_letter_generated") onto the
# three manual_queue.status values.
_STATUS_MAP = {
    "DONE": "done",
    "BLOCKED": "blocked",
    "FAILED": "failed",
    "cover_letter_generated": "done",
    "below_threshold": "done",
    "filtered_senior": "done",
    "scrape_failed": "failed",
    "blocked": "blocked",
}


def mark_url_processed(url: str, status: str = "DONE") -> None:
    """Mark a manual_queue entry resolved. Mirrors the old signature."""
    mark_manual_url_resolved(url, _STATUS_MAP.get(status, "done"))


def log_to_processed_file(job: Job, status: str) -> None:
    """
    No-op: processed_jobs.txt is retired (nothing read it back). If you
    want an audit trail going forward, query the jobs table directly —
    every processed job's title/company/score/status is already there.
    """
    return


def get_processed_urls() -> set[str]:
    """Replaces the old four-source file union with a single DB query."""
    return get_all_processed_urls()
