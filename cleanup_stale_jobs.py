# cleanup_stale_jobs.py
"""
Checks all 'not applied' jobs in the database to see if the posting
is still available, and removes those that are no longer accessible.

Run manually whenever you want to clean up stale listings:
    python cleanup_stale_jobs.py

Options:
    python cleanup_stale_jobs.py --dry-run    # preview without deleting
    python cleanup_stale_jobs.py --verbose    # show all results including live jobs
"""
import re
import argparse
import asyncio
import requests
from datetime import datetime
from database import delete_job, get_all_jobs
from job_scorer import EXCLUDE_REQUIREMENT_KEYWORDS

# HTTP status codes that indicate a job is gone
DEAD_HTTP_CODES = {404, 410, 403}

# Phrases that indicate a job posting has been removed or filled
DEAD_PAGE_INDICATORS = [
    "job is no longer available",
    "this job has expired",
    "this posting has been removed",
    "position has been filled",
    "no longer accepting applications",
    "job listing is no longer active",
    "this job is closed",
    "posting is no longer available",
    "application window has closed",
    "this position has been filled",
    "job has been removed",
    "listing has expired",
    "no longer available",
    "page not found",
    "404",
]

# Sources where we can check via simple HTTP request
# without needing a full browser
SIMPLE_CHECK_SOURCES = ["Remotive", "RemoteOK", "Jobicy", "The Muse", "Dice", "BuiltIn"]

# Sources that need a real browser due to JS rendering or bot protection
BROWSER_CHECK_SOURCES = ["Adzuna", "Manual", ]


async def check_url_with_browser(url: str) -> tuple[bool, str]:
    """
    Use Scrapling's DynamicFetcher to check if a job posting is
    still live. Handles Cloudflare and other bot protection
    automatically.
    Returns (is_alive, reason).
    """
    from scrapling.fetchers import DynamicFetcher

    try:
        page = await DynamicFetcher.async_fetch(
            url,
            headless=True,
            network_idle=True,
            wait=2000,
            disable_resources=True,
            google_search=True,
        )

        # Check HTTP status
        if page.status in DEAD_HTTP_CODES:
            return False, f"HTTP {page.status}"

        # Get page content
        content = page.get_all_text()
        if not content and page.html_content:
            content = re.sub(r'<[^>]+>', ' ', str(page.html_content))
            content = re.sub(r'\s+', ' ', content).strip()

        if not content:
            # Could not get content but no error — treat as unknown
            return True, "Could not verify (empty response)"

        content_lower = content.lower()

        # Check for dead page indicators
        for indicator in DEAD_PAGE_INDICATORS:
            if indicator in content_lower:
                return False, f"Page says: '{indicator}'"

        return True, "Job posting appears active"

    except Exception as e:
        # Connection error or timeout — treat as unknown, not dead
        return True, f"Could not verify (error): {e}"


def check_url_simple(url: str) -> tuple[bool, str]:
    """
    Use a simple HTTP request to check if a job posting is still live.
    Returns (is_alive, reason).
    """
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }
        response = requests.get(url, headers=headers, timeout=10)

        if response.status_code in DEAD_HTTP_CODES:
            return False, f"HTTP {response.status_code}"

        body_lower = response.text.lower()
        for indicator in DEAD_PAGE_INDICATORS:
            if indicator in body_lower:
                return False, f"Page says: '{indicator}'"

        return True, "Job posting appears active"

    except requests.exceptions.ConnectionError:
        return True, "Could not verify (connection error)"
    except requests.exceptions.Timeout:
        return True, "Could not verify (timeout)"
    except Exception as e:
        return True, f"Could not verify: {e}"


def log_cleanup(job: dict, reason: str, dry_run: bool):
    """Append a cleanup action to the cleanup log file."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    action = "WOULD REMOVE" if dry_run else "REMOVED"
    with open("cleanup_log.txt", "a") as f:
        f.write(f"\n[{timestamp}] {action}\n")
        f.write(f"  ID:      {job['id']}\n")
        f.write(f"  Title:   {job['title']}\n")
        f.write(f"  Company: {job['company']}\n")
        f.write(f"  Source:  {job['source']}\n")
        f.write(f"  Reason:  {reason}\n")
        f.write(f"  URL:     {job['url']}\n")


async def run_cleanup(dry_run: bool = False, verbose: bool = False):
    """
    Main cleanup routine — check all not-applied jobs and remove
    those whose postings are no longer available.
    """
    print("=== Job Cleanup Starting ===\n")

    if dry_run:
        print("DRY RUN MODE — no jobs will be deleted\n")

    # Fetch all not-applied jobs
    jobs = get_all_jobs(status="not applied")
    print(f"Found {len(jobs)} jobs with status 'not applied'\n")

    if not jobs:
        print("Nothing to clean up.")
        return

    alive = []
    dead = []
    unknown = []

    for i, job in enumerate(jobs):
        source = job.get("source", "")
        url = job.get("url", "")
        title = job.get("title", "Unknown")
        company = job.get("company", "Unknown")

        print(
            f"  [{i+1}/{len(jobs)}] Checking: "
            f"{title[:40]} @ {company[:25]}"
        )

        if not url:
            print(f"             → No URL — skipping")
            unknown.append(job)
            continue

        # Choose check method based on source
        if source in SIMPLE_CHECK_SOURCES:
            is_alive, reason = check_url_simple(url)
        else:
            is_alive, reason = await check_url_with_browser(url)

        if is_alive:
            if verbose:
                print(f"             → Live: {reason}")
            alive.append(job)
        elif "could not verify" in reason.lower():
            print(f"             → Unknown: {reason}")
            unknown.append(job)
        else:
            print(f"             → STALE: {reason}")
            dead.append(job)
            log_cleanup(job, reason, dry_run)

            if not dry_run:
                delete_job(job["id"])
                print(f"             → Deleted from database")

    # Summary
    print(f"\n=== Cleanup Complete ===")
    print(f"  Checked:  {len(jobs)}")
    print(f"  Live:     {len(alive)}")
    if dry_run:
        print(f"  Would remove: {len(dead)}")
    else:
        print(f"  Removed:  {len(dead)}")
    print(f"  Unknown:  {len(unknown)} (could not verify — kept)")

    if dead:
        print(
            f"\n{'Would remove' if dry_run else 'Removed'} jobs:"
        )
        for job in dead:
            print(
                f"  [{job['id']}] {job['title'][:40]} "
                f"@ {job['company'][:25]}"
            )

    if unknown:
        print(f"\nCould not verify (kept):")
        for job in unknown:
            print(
                f"  [{job['id']}] {job['title'][:40]} "
                f"@ {job['company'][:25]}"
            )

    print(f"\nCleanup log saved to cleanup_log.txt")


def main():
    parser = argparse.ArgumentParser(
        description="Remove stale job postings from the database"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview which jobs would be removed without deleting anything"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show all results including jobs confirmed still live"
    )
    args = parser.parse_args()
    asyncio.run(run_cleanup(dry_run=args.dry_run, verbose=args.verbose))


if __name__ == "__main__":
    main()
