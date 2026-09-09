# job_cache.py
# unified-db shim — seen_jobs.json is retired; process_status on the
# jobs table now carries this information. This module keeps the same
# public API (load_cache, save_cache, is_seen, mark_seen, filter_seen_jobs,
# cache_stats, prune_cache) so job_scorer.py needs no changes.
import logging

from database import (
    get_connection,
    is_url_seen,
    prune_filtered_jobs,
)
from models import Job, JobCache

logger = logging.getLogger(__name__)


def load_cache() -> JobCache:
    """
    Compatibility shim: returns a dict shaped like the old seen_jobs.json,
    built from the jobs table. Callers that only check membership
    (`url in cache`) work unchanged; is_seen()/mark_seen() below are the
    preferred direct entry points and avoid loading the whole table.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT url, title, company, score, date_seen FROM jobs "
            "WHERE process_status IN ('ok', 'filtered')"
        ).fetchall()
        return {
            row["url"]: {
                "title": row["title"] or "",
                "company": row["company"] or "",
                "score": row["score"] or 0,
                "date_seen": row["date_seen"] or "",
            }
            for row in rows
        }
    finally:
        conn.close()


def save_cache(cache: JobCache) -> None:
    """
    No-op: writes now happen directly via upsert_job()/database helpers
    at the point a job is scored. Kept only so existing call sites
    (which call save_cache() after mark_seen()) don't need edits.
    """
    return


def is_seen(url: str, cache: JobCache | None = None) -> bool:
    """Ignore the in-memory cache argument; query the DB directly."""
    return is_url_seen(url)


def mark_seen(url: str, job: Job, cache: JobCache | None = None) -> None:
    """
    No-op: job_scorer.py's _score_and_cache() already calls upsert_job()
    (via the pipeline) with process_status derived from the score, so
    the jobs table is the single source of truth. This function is kept
    only for call-site compatibility.
    """
    return


def filter_seen_jobs(jobs: list[Job], cache: JobCache | None = None) -> tuple[list[Job], int]:
    """Remove jobs already present (ok/filtered) in the jobs table."""
    new_jobs = [j for j in jobs if not is_url_seen(j["url"])]
    skipped = len(jobs) - len(new_jobs)
    return new_jobs, skipped


def cache_stats(cache: JobCache | None = None) -> str:
    conn = get_connection()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE process_status IN ('ok', 'filtered')"
        ).fetchone()[0]
        scored = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE process_status = 'ok' AND score >= 1"
        ).fetchone()[0]
        return f"{total} jobs seen across all runs ({scored} previously scored)"
    finally:
        conn.close()


def prune_cache(cache: JobCache | None = None, days: int = 90) -> int:
    """Delegates to database.prune_filtered_jobs(). Never prunes 'ok' rows."""
    return prune_filtered_jobs(days=days)
