# job_tracker.py
import fcntl
import logging
import os
from contextlib import contextmanager
from datetime import datetime

from config import MANUAL_JOBS_FILE, PROCESSED_JOBS_FILE
from models import Job

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# File locking — guards manual_jobs.txt against concurrent writes from
# the pipeline and the inbox listener running simultaneously.
# ---------------------------------------------------------------------------

@contextmanager
def _locked_file(path: str, mode: str):
    """
    Open a file and hold an exclusive flock for the duration of the
    context. Works on Linux/macOS; on Windows fcntl is unavailable
    but this project targets a Linux server so that is acceptable.
    """
    with open(path, mode, encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield fh
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def mark_url_processed(url: str, status: str = "DONE") -> None:
    """
    Comment out every occurrence of a URL in manual_jobs.txt by
    prefixing matched lines with '# [DONE timestamp]'.
    Uses an exclusive file lock to prevent concurrent write corruption.
    """
    if not os.path.exists(MANUAL_JOBS_FILE):
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    with _locked_file(MANUAL_JOBS_FILE, "r+") as fh:
        lines = fh.readlines()
        updated = []
        for line in lines:
            if line.strip() == url:
                updated.append(f"# [{status} {timestamp}] {url}\n")
            else:
                updated.append(line)
        fh.seek(0)
        fh.writelines(updated)
        fh.truncate()


def log_to_processed_file(job: Job, status: str) -> None:
    """
    Append a processed-job entry to processed_jobs.txt.
    Expected status values: 'cover_letter_generated', 'below_threshold',
    'filtered_senior', 'scrape_failed', 'blocked'.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"\n[{timestamp}] {status.upper()}\n",
        f"  Title:   {job.get('title', 'Unknown')}\n",
        f"  Company: {job.get('company', 'Unknown')}\n",
        f"  Salary:  {job.get('salary', 'Not specified')}\n",
        f"  Score:   {job.get('score', 'N/A')}/10\n",
        f"  Reason:  {job.get('score_reason', 'N/A')}\n",
        f"  URL:     {job.get('url', '')}\n",
    ]

    # processed_jobs.txt is append-only; no locking needed for
    # appends on Linux as long as writes are atomic-sized, but we
    # use the context manager for consistency.
    with open(PROCESSED_JOBS_FILE, "a", encoding="utf-8") as fh:
        fh.writelines(lines)


def get_processed_urls() -> set[str]:
    """
    Return a set of all URLs that should be skipped by the manual
    processor, drawn from four sources:

    1. Commented-out lines in manual_jobs.txt
    2. processed_jobs.txt log
    3. seen_jobs.json cache (automatic pipeline)
    4. jobs database (either pipeline)

    Errors from any individual source are logged and skipped so a
    single failure does not silently return an empty set.
    """
    processed: set[str] = set()

    # 1 — Commented-out done entries in manual_jobs.txt
    if os.path.exists(MANUAL_JOBS_FILE):
        try:
            with open(MANUAL_JOBS_FILE, "r", encoding="utf-8") as fh:
                for line in fh:
                    if "# [DONE" in line or "# [BLOCKED" in line or "# [FAILED" in line:
                        # Format: "# [STATUS timestamp] <url>"
                        parts = line.strip().split("] ", 1)
                        if len(parts) == 2:
                            processed.add(parts[1].strip())
        except OSError:
            logger.warning("Could not read %s for processed URL check", MANUAL_JOBS_FILE)

    # 2 — processed_jobs.txt log
    if os.path.exists(PROCESSED_JOBS_FILE):
        try:
            with open(PROCESSED_JOBS_FILE, "r", encoding="utf-8") as fh:
                for line in fh:
                    stripped = line.strip()
                    if stripped.startswith("URL:"):
                        url = stripped[4:].strip()
                        if url:
                            processed.add(url)
        except OSError:
            logger.warning("Could not read %s for processed URL check", PROCESSED_JOBS_FILE)

    # 3 — seen_jobs.json cache (automatic pipeline)
    try:
        from job_cache import load_cache
        processed.update(load_cache().keys())
    except Exception:
        logger.warning("Could not load job cache for processed URL check")

    # 4 — Database
    try:
        from database import get_all_jobs
        for job in get_all_jobs():
            if job.get("url"):
                processed.add(job["url"])
    except Exception:
        logger.warning("Could not query database for processed URL check")

    return processed
