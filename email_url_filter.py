#!/usr/bin/env python3
"""
email_url_filter.py

Called by the n8n Execute Command node after the Extract Email Body JS node.
Reads a decoded email body from --body, extracts job URLs, deduplicates
against manual_jobs.txt and processed_jobs.txt, and appends new URLs.

Usage:
    python3 email_url_filter.py \
        --body "...decoded email text..." \
        --sender "dice@connect.dice.com" \
        [--is-html]

Supported email sources:
    - dice@connect.dice.com / connect.dice.com   → plain text, direct links
                                                   (elinks.dice.com tracking
                                                   links are now followed)
    - jobright.ai                                → HTML href attributes
    - mail.remotehunter.com                      → plain text
    - match.indeed.com / donotreply@indeed.com   → rc/clk and viewjob links
                                                   (clk.indeed.com tracking
                                                   links are now followed)
    - LinkedIn job alert emails                  → comm/jobs/view/ and
                                                   jobs/view/ links,
                                                   canonicalised to bare
                                                   /jobs/view/JOBID form
"""

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Paths — resolved relative to this script so it works regardless of
# the working directory n8n uses when invoking it.
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MANUAL_JOBS_FILE = os.path.join(_SCRIPT_DIR, "manual_jobs.txt")
PROCESSED_JOBS_FILE = os.path.join(_SCRIPT_DIR, "processed_jobs.txt")

# Timeout in seconds for redirect-following HEAD requests.
# Kept short because these are only used for tracking-link resolution;
# a slow response just means we drop the URL rather than blocking the run.
_REDIRECT_TIMEOUT = 6

# ---------------------------------------------------------------------------
# URL allow-list — only URLs matching at least one pattern are kept.
# Evaluated AFTER canonicalization so patterns target clean final URLs.
# ---------------------------------------------------------------------------
JOB_URL_PATTERNS = [
    r"dice\.com/job-detail/[a-f0-9\-]{30,}",
    r"jobright\.ai/jobs/info/",
    r"remotehunter\.com/apply-with-ai/",
    # Indeed — both the click-tracker form and the stable viewjob form
    r"indeed\.com/rc/clk\?.*jk=[a-f0-9]+",
    r"indeed\.com/viewjob\?jk=[a-f0-9]+",
    # LinkedIn public job pages (canonicalized form only — see canonicalize_url)
    r"linkedin\.com/jobs/view/\d+",
]

# ---------------------------------------------------------------------------
# Exclusion patterns — matched URLs are always dropped, even if they also
# match a pattern above.
#
# NOTE: elinks.dice.com and clk.indeed.com are intentionally NOT listed here.
# Those are email click-tracker subdomains that redirect to real job pages.
# They are handled by resolve_tracking_url() instead — the final destination
# URL is what gets evaluated against JOB_URL_PATTERNS.
# ---------------------------------------------------------------------------
EXCLUSION_PATTERNS = [
    # Jobright noise
    r"jobright\.ai/\?retarget",
    r"jobright\.ai/recommend",
    # Generic asset / CDN noise
    r"chromewebstore\.google\.com",
    r"user-subscription\.com",
    r"media\.licdn\.com",
    r"static\.",
    r"\.png", r"\.jpg", r"\.gif", r"\.webp", r"\.svg",
    # Remotehunter non-job pages
    r"remotehunter\.com\?",
    r"remotehunter\.com/remote-jobs",
    # Email infrastructure noise
    r"customeriomail\.com",
    r"imgping",
    r"clearbit\.com",
    r"unsubscribe",
    r"optout",
    # Dice non-job pages
    r"dice\.com/home",
    r"dice\.com/dashboard",
    r"dice\.com/support",
    # Indeed non-job pages
    r"indeed\.com/pagead/",
    r"indeed\.com/legal",
    r"subscriptions\.indeed\.com",
    r"support\.indeed\.com",
    r"hrtechprivacy\.com",
    r"engage\.indeed\.com",
    r"match\.indeed\.com/invitations",
    r"prod\.statics\.indeed\.com",
    # LinkedIn non-job pages
    r"linkedin\.com/feed",
    r"linkedin\.com/in/",           # profile links
    r"linkedin\.com/company/",      # company pages
    r"linkedin\.com/mynetwork",
    r"linkedin\.com/messaging",
    r"linkedin\.com/notifications",
    r"linkedin\.com/premium",
    r"linkedin\.com/learning",
    r"linkedin\.com/pulse",
    r"linkedin\.com/help",
    r"linkedin\.com/legal",
    r"linkedin\.com/comm/(?!jobs/)",  # comm/ links that are NOT jobs
]

# Pre-compile for performance
_JOB_URL_RE = [re.compile(p) for p in JOB_URL_PATTERNS]
_EXCLUSION_RE = [re.compile(p) for p in EXCLUSION_PATTERNS]

# ---------------------------------------------------------------------------
# Domains whose links should be followed to their redirect destination
# before evaluation.  These are known email click-tracker subdomains used
# by Dice and Indeed; the real job URL lives at the final redirect target.
# ---------------------------------------------------------------------------
_REDIRECT_DOMAINS = re.compile(
    r"https?://(?:elinks\.dice\.com|clk\.indeed\.com)/",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Redirect resolution
# ---------------------------------------------------------------------------

def resolve_tracking_url(url: str) -> str:
    """
    Follow redirect chains for known click-tracker URLs and return the
    final destination URL.  Uses a HEAD request with a short timeout so
    a slow tracker doesn't block the whole run.

    Returns the original URL unchanged if:
    - The URL is not a known tracking domain
    - The request fails or times out
    - The final destination cannot be determined
    """
    if not _REDIRECT_DOMAINS.match(url):
        return url

    try:
        req = urllib.request.Request(
            url,
            method="HEAD",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            },
        )
        with urllib.request.urlopen(req, timeout=_REDIRECT_TIMEOUT) as resp:
            final = resp.url
            if final and final != url:
                print(
                    f"[Filter] Resolved tracking URL: {url[:60]} → {final[:60]}",
                    file=sys.stderr,
                )
                return final
    except (urllib.error.URLError, Exception) as exc:
        print(
            f"[Filter] Could not resolve tracking URL {url[:60]}: {exc}",
            file=sys.stderr,
        )

    return url


# ---------------------------------------------------------------------------
# Canonicalization
# ---------------------------------------------------------------------------

def canonicalize_url(url: str) -> str:
    """
    Normalise tracking and variant URLs to stable canonical forms before
    they are evaluated against JOB_URL_PATTERNS or stored.

    Handles:
      indeed.com/rc/clk?jk=KEY   → https://www.indeed.com/viewjob?jk=KEY
      linkedin.com/comm/jobs/view/ID?tracking=...
                                  → https://www.linkedin.com/jobs/view/ID
      linkedin.com/jobs/view/ID?refId=...
                                  → https://www.linkedin.com/jobs/view/ID
      linkedin.com/jobs/collections/...?currentJobId=ID
                                  → https://www.linkedin.com/jobs/view/ID
    """
    # Indeed click-tracker form
    if "indeed.com/rc/clk" in url:
        m = re.search(r"[?&]jk=([a-f0-9]+)", url)
        if m:
            return f"https://www.indeed.com/viewjob?jk={m.group(1)}"

    # LinkedIn: comm/jobs/view/ (the form used in alert emails)
    m = re.search(r"linkedin\.com/comm/jobs/view/(\d+)", url)
    if m:
        return f"https://www.linkedin.com/jobs/view/{m.group(1)}"

    # LinkedIn: collections page with currentJobId query param
    if "linkedin.com/jobs/collections" in url:
        m = re.search(r"[?&]currentJobId=(\d+)", url)
        if m:
            return f"https://www.linkedin.com/jobs/view/{m.group(1)}"

    # LinkedIn: already a jobs/view URL but has tracking query params — strip them
    m = re.match(r"(https://www\.linkedin\.com/jobs/view/\d+)", url)
    if m:
        return m.group(1)

    return url


# ---------------------------------------------------------------------------
# URL classification
# ---------------------------------------------------------------------------

def is_job_url(url: str) -> bool:
    """Return True if the URL matches an allowed job pattern and no exclusion."""
    url_lower = url.lower()
    if any(p.search(url_lower) for p in _EXCLUSION_RE):
        return False
    return any(p.search(url_lower) for p in _JOB_URL_RE)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_urls_from_text(text: str) -> list[str]:
    """Extract all http/https URLs from plain text."""
    return re.findall(r'https?://[^\s<>"\')\]>]+', text)


def extract_urls_from_html(html: str) -> list[str]:
    """
    Extract URLs from HTML, preferring href attributes over bare text.
    Falls back to plain-text extraction if no hrefs are found.
    """
    hrefs = re.findall(r'href=["\']?(https?://[^"\'>\s]+)', html)
    return hrefs if hrefs else extract_urls_from_text(html)


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def load_existing_urls(filepath: str) -> set[str]:
    """Load URLs from a file into a set, skipping comments and blank lines."""
    if not os.path.exists(filepath):
        return set()
    urls: set[str] = set()
    with open(filepath, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.add(line.split()[0])
    return urls


def load_existing_urls_from_db() -> set[str]:
    """
    Load all job URLs already present in the SQLite database.
    Returns an empty set if the database is unavailable or the
    import fails — this keeps email_url_filter.py self-contained
    when called from n8n outside the venv.
    """
    try:
        import sqlite3
        db_path = os.path.join(_SCRIPT_DIR, "jobs.db")
        if not os.path.exists(db_path):
            return set()
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            rows = conn.execute("SELECT url FROM jobs WHERE url IS NOT NULL").fetchall()
            return {row[0] for row in rows}
        finally:
            conn.close()
    except Exception as exc:
        print(f"[Filter] Could not query database for deduplication: {exc}", file=sys.stderr)
        return set()
    """Append new URLs to a file, one per line."""
    with open(filepath, "a", encoding="utf-8") as fh:
        for url in urls:
            fh.write(url + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Filter job URLs from an email body and append to manual_jobs.txt"
    )
    parser.add_argument(
        "--body", required=True,
        help="Decoded email body (plain text or HTML)",
    )
    parser.add_argument(
        "--sender", default="",
        help="Sender address or domain (used for logging only)",
    )
    parser.add_argument(
        "--is-html", action="store_true",
        help="Treat body as HTML and extract from href attributes",
    )
    args = parser.parse_args()

    print(f"[Filter] Sender: {args.sender.lower()}", file=sys.stderr)

    raw_urls = (
        extract_urls_from_html(args.body)
        if args.is_html
        else extract_urls_from_text(args.body)
    )
    print(f"[Filter] Extracted {len(raw_urls)} raw URLs", file=sys.stderr)

    # Resolve any click-tracker redirect URLs to their final destinations
    # before canonicalization and pattern matching.
    resolved_urls = [resolve_tracking_url(u) for u in raw_urls]
    resolved_count = sum(1 for a, b in zip(raw_urls, resolved_urls) if a != b)
    if resolved_count:
        print(f"[Filter] Resolved {resolved_count} tracking redirect(s)", file=sys.stderr)

    # Canonicalize (strip tracking params, normalize LinkedIn/Indeed forms)
    canonical_urls = [canonicalize_url(u) for u in resolved_urls]

    job_urls = [u for u in canonical_urls if is_job_url(u)]
    print(f"[Filter] {len(job_urls)} matched job URL patterns", file=sys.stderr)

    # Deduplicate within this email while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for url in job_urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    print(
        f"[Filter] {len(deduped)} after deduplication within email",
        file=sys.stderr,
    )

    existing = (
        load_existing_urls(MANUAL_JOBS_FILE)
        | load_existing_urls(PROCESSED_JOBS_FILE)
        | load_existing_urls_from_db()
    )
    new_urls = [u for u in deduped if u not in existing]
    already_known = len(deduped) - len(new_urls)

    if already_known:
        print(f"[Filter] Skipped {already_known} already-known URLs", file=sys.stderr)

    if not new_urls:
        print("[Filter] No new URLs to add.", file=sys.stderr)
        print(json.dumps({"new_count": 0, "urls": []}))
        return

    append_urls(MANUAL_JOBS_FILE, new_urls)

    print(
        f"[Filter] Added {len(new_urls)} new URLs to {MANUAL_JOBS_FILE}:",
        file=sys.stderr,
    )
    for url in new_urls:
        print(f"  {url}", file=sys.stderr)

    print(json.dumps({"new_count": len(new_urls), "urls": new_urls}))


if __name__ == "__main__":
    main()
