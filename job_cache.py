# job_cache.py
import json
import logging
import os
from datetime import datetime, timedelta

from models import Job, JobCache

logger = logging.getLogger(__name__)

CACHE_FILE = "seen_jobs.json"


def load_cache() -> JobCache:
    """
    Load the seen-jobs cache from disk.
    Returns an empty dict if the file does not exist or is corrupted.
    """
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        logger.warning("Cache file '%s' is corrupted — starting fresh", CACHE_FILE)
        return {}


def save_cache(cache: JobCache) -> None:
    """Write the cache atomically by writing to a temp file then renaming."""
    tmp = CACHE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=2)
        os.replace(tmp, CACHE_FILE)
    except OSError:
        logger.exception("Failed to save cache to '%s'", CACHE_FILE)


def is_seen(url: str, cache: JobCache) -> bool:
    """Return True if this URL has been processed in a previous run."""
    return url in cache


def mark_seen(url: str, job: Job, cache: JobCache) -> None:
    """
    Record a job URL in the cache with scoring metadata.
    Call this after a job has been scored and processed.
    """
    cache[url] = {
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "score": job.get("score", 0),
        "date_seen": datetime.now().strftime("%Y-%m-%d"),
    }


def filter_seen_jobs(
    jobs: list[Job], cache: JobCache
) -> tuple[list[Job], int]:
    """
    Remove jobs already present in the cache.
    Returns (new_jobs, skipped_count).
    """
    new_jobs = [j for j in jobs if not is_seen(j["url"], cache)]
    skipped = len(jobs) - len(new_jobs)
    return new_jobs, skipped


def cache_stats(cache: JobCache) -> str:
    """Return a human-readable summary of cache contents."""
    if not cache:
        return "Cache is empty"
    total = len(cache)
    scored = sum(1 for v in cache.values() if v.get("score", 0) >= 1)
    return f"{total} jobs seen across all runs ({scored} previously scored)"


def prune_cache(cache: JobCache, days: int = 90) -> int:
    """
    Remove entries older than `days` days so the cache does not grow
    indefinitely. Returns the number of entries pruned.
    Entries with a missing or malformed date are also removed.
    """
    cutoff = datetime.now() - timedelta(days=days)
    to_remove = []

    for url, data in cache.items():
        try:
            seen_date = datetime.strptime(data["date_seen"], "%Y-%m-%d")
            if seen_date < cutoff:
                to_remove.append(url)
        except (KeyError, ValueError):
            to_remove.append(url)

    for url in to_remove:
        del cache[url]

    return len(to_remove)
