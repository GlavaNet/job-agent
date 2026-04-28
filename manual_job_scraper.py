# manual_job_scraper.py
import argparse
import asyncio
import json
import logging
import os
import re

from scrapling.fetchers import DynamicFetcher

from config import MANUAL_JOBS_FILE, RELEVANCE_THRESHOLD, RESUME_PATH
from database import upsert_job
from job_scorer import is_too_senior, score_job
from job_tracker import get_processed_urls, log_to_processed_file, mark_url_processed
from notifier import notify, notify_error, notify_job_filtered, notify_job_processed, notify_job_skipped
from resume_parser import load_resume

logger = logging.getLogger(__name__)

_BOT_INDICATORS = [
    "security verification",
    "verifying you are not a bot",
    "just a moment",
    "please wait",
    "checking your browser",
    "enable javascript and cookies",
    "performing security verification",
    # LinkedIn-specific auth walls
    "join linkedin",
    "sign in to linkedin",
    "authwall",
]

# ---------------------------------------------------------------------------
# LinkedIn detection
# ---------------------------------------------------------------------------

_LINKEDIN_JOB_RE = re.compile(r"linkedin\.com/jobs/view/(\d+)", re.IGNORECASE)


def _is_linkedin_url(url: str) -> bool:
    """Return True if this is a LinkedIn public job page URL."""
    return bool(_LINKEDIN_JOB_RE.search(url))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_html(text: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', text)
    for entity, char in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " ")]:
        text = text.replace(entity, char)
    return re.sub(r'\s+', ' ', text).strip()


def _load_urls(filepath: str) -> list[str]:
    """Load unprocessed URLs from a text file, skipping comments and blanks."""
    if not os.path.exists(filepath):
        logger.warning("URL file not found: %s", filepath)
        return []
    urls = []
    with open(filepath, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line.split()[0])
    logger.info("Loaded %d URLs from %s", len(urls), filepath)
    return urls


# ---------------------------------------------------------------------------
# LinkedIn scraping — plain HTTP, no Playwright
#
# LinkedIn's public job pages at linkedin.com/jobs/view/JOBID render the
# full job description in static HTML when fetched with a browser-like
# User-Agent.  Using Playwright reliably triggers their bot detection and
# results in an auth-wall redirect.  A plain requests call with a current
# Chrome UA avoids this entirely for the public (unauthenticated) page.
#
# What you get without login:
#   - Job title, company name, location
#   - Full job description text
#   - Seniority level, employment type (sometimes)
#   - Salary (when the poster included it)
#
# What requires login:
#   - Easy Apply
#   - "How you match" skills comparison
#   - Saved jobs / following company
#
# None of the missing features affect scoring or cover letter generation,
# so the unauthenticated page is sufficient for this pipeline's purposes.
# ---------------------------------------------------------------------------

# Fields LinkedIn embeds in JSON-LD on the public job page.
# We prefer these over regex-extracted text when available because they
# are structured and language-independent.
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)

# Salary range patterns for plain-text fallback extraction
_SALARY_RE = re.compile(
    r'\$[\d,]+(?:\.\d+)?(?:\s*[-–—]\s*\$[\d,]+(?:\.\d+)?)?\s*(?:per\s+(?:year|hour|month|annum)|/(?:yr|hr|mo|year|hour))?',
    re.IGNORECASE,
)

_LINKEDIN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}


def _extract_linkedin_jsonld(html: str) -> dict:
    """
    Extract structured job data from LinkedIn's JSON-LD block.
    Returns a partial job dict with whatever fields were found.
    """
    for match in _JSONLD_RE.finditer(html):
        try:
            data = json.loads(match.group(1))
            if data.get("@type") not in ("JobPosting", "jobPosting"):
                continue

            title = data.get("title", "")
            description_html = data.get("description", "")
            description = _strip_html(description_html)[:2000] if description_html else ""

            # Company
            org = data.get("hiringOrganization", {})
            company = org.get("name", "") if isinstance(org, dict) else ""

            # Location
            loc_data = data.get("jobLocation", {})
            if isinstance(loc_data, list):
                loc_data = loc_data[0] if loc_data else {}
            address = loc_data.get("address", {}) if isinstance(loc_data, dict) else {}
            if isinstance(address, dict):
                city = address.get("addressLocality", "")
                state = address.get("addressRegion", "")
                country = address.get("addressCountry", "")
                location = ", ".join(p for p in [city, state, country] if p) or "Not specified"
            else:
                location = str(address) if address else "Not specified"

            # Remote flag
            remote = data.get("jobLocationType", "")
            if remote == "TELECOMMUTE" or "remote" in location.lower():
                location = f"Remote — {location}" if location != "Not specified" else "Remote"

            # Salary
            salary = "Not specified"
            salary_data = data.get("baseSalary", {})
            if isinstance(salary_data, dict):
                value = salary_data.get("value", {})
                if isinstance(value, dict):
                    min_val = value.get("minValue")
                    max_val = value.get("maxValue")
                    currency = salary_data.get("currency", "USD")
                    unit = value.get("unitText", "YEAR").upper()
                    unit_label = {"YEAR": "/yr", "HOUR": "/hr", "MONTH": "/mo"}.get(unit, "")
                    if min_val and max_val:
                        salary = f"{currency} {int(min_val):,} – {int(max_val):,}{unit_label}"
                    elif min_val:
                        salary = f"{currency} {int(min_val):,}+{unit_label}"

            return {
                "title": title,
                "company": company,
                "location": location,
                "salary": salary,
                "description": description,
            }
        except (json.JSONDecodeError, KeyError, TypeError):
            continue

    return {}


def _scrape_linkedin_job(url: str) -> dict | None:
    """
    Fetch a LinkedIn public job page using plain HTTP (no Playwright).

    Returns the same shape as _scrape_job_page() so the rest of the
    pipeline treats it identically:
        {"url": ..., "page_title": ..., "raw_text": ..., "blocked": False}
        {"blocked": True, "url": ...}
        None  on fetch failure

    Strategy:
    1. Try to extract structured data from the JSON-LD block embedded
       in LinkedIn's HTML — this gives clean, parser-friendly fields.
    2. Fall back to raw text extraction if JSON-LD is absent or sparse.
    3. Detect auth-wall redirects and report as blocked.
    """
    import requests as _requests

    try:
        resp = _requests.get(
            url,
            headers=_LINKEDIN_HEADERS,
            timeout=15,
            allow_redirects=True,
        )
    except Exception:
        logger.exception("[LinkedIn] Request failed for %s", url)
        return None

    # Auth-wall: LinkedIn redirects to /login or /authwall when the
    # page requires a session.  We check the final URL, not the status
    # code, because they often return 200 with a login page.
    final_url = resp.url if hasattr(resp, "url") else url
    if any(wall in final_url for wall in ("/login", "/authwall", "/checkpoint")):
        logger.info("[LinkedIn] Auth wall detected for %s", url)
        return {"blocked": True, "url": url}

    if resp.status_code == 404:
        logger.warning("[LinkedIn] Job page not found (404): %s", url)
        return None

    if resp.status_code != 200:
        logger.warning("[LinkedIn] HTTP %d for %s", resp.status_code, url)
        return None

    html = resp.text

    # Check for bot/auth indicators in the page body
    html_lower = html.lower()
    if any(ind in html_lower for ind in _BOT_INDICATORS):
        logger.info("[LinkedIn] Bot/auth indicator in page body: %s", url)
        return {"blocked": True, "url": url}

    # Prefer JSON-LD structured data
    structured = _extract_linkedin_jsonld(html)

    if structured.get("title") and structured.get("company"):
        # We have good structured data — build a rich raw_text that
        # the LLM parser can use as context alongside the structured fields.
        raw_text = (
            f"Title: {structured['title']}\n"
            f"Company: {structured['company']}\n"
            f"Location: {structured['location']}\n"
            f"Salary: {structured['salary']}\n\n"
            f"Description:\n{structured['description']}"
        )
        page_title = f"{structured['title']} - {structured['company']}"
        logger.info(
            "[LinkedIn] ✓ JSON-LD extracted: %s @ %s",
            structured["title"], structured["company"],
        )
        return {
            "url": url,
            "page_title": page_title,
            "raw_text": raw_text[:8000],
            "blocked": False,
            "_structured": structured,  # passed through to skip LLM parse step
        }

    # Fallback: strip HTML and use raw text
    text = _strip_html(html)
    if len(text.strip()) < 200:
        logger.warning("[LinkedIn] Could not extract meaningful content from %s", url)
        return None

    # Try to extract a salary from the raw text
    salary_match = _SALARY_RE.search(text)
    if salary_match and structured.get("salary", "Not specified") == "Not specified":
        structured["salary"] = salary_match.group(0).strip()

    page_title_match = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
    page_title = _strip_html(page_title_match.group(1)).strip() if page_title_match else url

    logger.info("[LinkedIn] ✓ Raw text fallback: %d chars from %s", len(text), url)
    return {
        "url": url,
        "page_title": page_title,
        "raw_text": text[:8000],
        "blocked": False,
    }


# ---------------------------------------------------------------------------
# General scraping — Playwright headless (unchanged for non-LinkedIn URLs)
# ---------------------------------------------------------------------------

async def _scrape_job_page(url: str) -> dict | None:
    """
    Fetch a job posting page.

    Routes LinkedIn URLs to _scrape_linkedin_job() (plain HTTP) and all
    other URLs to the Playwright DynamicFetcher.

    Returns:
        {"url": ..., "page_title": ..., "raw_text": ..., "blocked": False}  on success
        {"blocked": True, "url": ...}  when bot protection is detected
        None  on a genuine fetch failure
    """
    if _is_linkedin_url(url):
        logger.info("[Scraper] LinkedIn URL — using HTTP fetcher (no Playwright)")
        # _scrape_linkedin_job is synchronous; run it in the executor so
        # it doesn't block the event loop.
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _scrape_linkedin_job, url)

    # --- Playwright path for all other sources ---
    try:
        page = await DynamicFetcher.async_fetch(
            url,
            headless=True,
            network_idle=True,
            wait=3000,
            disable_resources=True,
            google_search=True,
        )

        content = page.get_all_text()
        if not content or len(content.strip()) < 200:
            if page.html_content:
                content = _strip_html(str(page.html_content))

        if not content or len(content.strip()) < 200:
            logger.warning("[Scraper] Could not extract content from %s", url)
            return None

        if any(ind in content.lower() for ind in _BOT_INDICATORS):
            logger.info("[Scraper] Site blocking automated access: %s", url)
            return {"blocked": True, "url": url}

        page_title = url
        try:
            m = re.search(
                r'<title[^>]*>(.*?)</title>',
                str(page.html_content),
                re.IGNORECASE | re.DOTALL,
            )
            if m:
                page_title = _strip_html(m.group(1)).strip()
        except Exception:
            pass

        logger.info("[Scraper] ✓ %d chars extracted from %s", len(content), url)
        return {
            "url": url,
            "page_title": page_title,
            "raw_text": content[:8000],
            "blocked": False,
        }

    except Exception:
        logger.exception("[Scraper] Failed to scrape %s", url)
        return None


# ---------------------------------------------------------------------------
# LLM parsing
# ---------------------------------------------------------------------------

def _parse_job_from_text(scraped: dict) -> dict:
    """
    Use the LLM to extract structured job fields from raw scraped text.

    For LinkedIn jobs where JSON-LD extraction already produced clean
    structured data (_structured key present), the LLM is only used to
    generate the description summary — avoiding the risk of the model
    misidentifying fields that are already known-good.

    Returns a job dict compatible with the rest of the pipeline.
    """
    from llm_client import invoke_llm

    structured = scraped.get("_structured")

    # Fast path: JSON-LD gave us reliable title/company/location/salary.
    # Only ask the LLM to summarize the description.
    if structured and structured.get("title") and structured.get("company"):
        if structured.get("description"):
            summary_prompt = f"""
Summarize the following job description in 3-5 sentences, focusing on:
the primary responsibilities, required technical skills, and any notable
requirements or benefits. Be concise and factual.

JOB DESCRIPTION:
{structured['description'][:3000]}

Write the summary now (plain text, no JSON, no bullet points):
"""
            try:
                summary = invoke_llm(summary_prompt).strip()
            except Exception:
                logger.exception("[Manual] LLM summary failed for %s", scraped["url"])
                summary = structured["description"][:500]
        else:
            summary = "No description available."

        return {
            "source": "LinkedIn",
            "title": structured["title"],
            "company": structured["company"],
            "location": structured["location"],
            "salary": structured["salary"],
            "url": scraped["url"],
            "description": summary,
        }

    # Standard path: ask the LLM to extract all fields from raw text.
    prompt = f"""
You are a job posting parser. Extract structured information from the
raw text of a job posting page below.

Respond with ONLY a JSON object in this exact format, nothing else:
{{
  "title": "<job title>",
  "company": "<company name>",
  "location": "<location or Remote>",
  "salary": "<salary range or Not specified>",
  "description": "<concise summary of role and requirements, max 500 words>"
}}

If you cannot find a field, use "Not specified" as the value.
Do not include any text outside the JSON object.

PAGE TITLE: {scraped['page_title']}
URL: {scraped['url']}

RAW PAGE TEXT:
{scraped['raw_text'][:4000]}
"""
    try:
        raw = invoke_llm(prompt)
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            parsed = json.loads(m.group())
            source = "LinkedIn" if _is_linkedin_url(scraped["url"]) else "Manual"
            return {
                "source": source,
                "title": parsed.get("title", "Unknown Title"),
                "company": parsed.get("company", "Unknown Company"),
                "location": parsed.get("location", "Not specified"),
                "salary": parsed.get("salary", "Not specified"),
                "url": scraped["url"],
                "description": parsed.get("description", scraped["raw_text"][:1000]),
            }
    except Exception:
        logger.exception("[Manual] LLM parsing failed for %s", scraped["url"])

    source = "LinkedIn" if _is_linkedin_url(scraped["url"]) else "Manual"
    return {
        "source": source,
        "title": scraped["page_title"],
        "company": "Unknown Company",
        "location": "Not specified",
        "salary": "Not specified",
        "url": scraped["url"],
        "description": scraped["raw_text"][:1000],
    }


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

async def _process_manual_jobs(
    url_file: str = "manual_jobs.txt",
    reprocess: bool = False,
) -> None:
    """
    Scrape all unprocessed URLs, score against the résumé,
    generate cover letters for matches, and send notifications.

    reprocess=True bypasses deduplication and re-scrapes all URLs —
    useful after a scraper upgrade or to refresh stale data.
    """
    logger.info("=== Manual Job Processor Starting ===")
    if reprocess:
        logger.info("REPROCESS MODE — ignoring previous processing status")

    logger.info("Loading résumé from %s…", RESUME_PATH)
    resume_text = load_resume(RESUME_PATH)
    logger.info("  → %d characters extracted", len(resume_text))

    all_urls = _load_urls(url_file)
    if reprocess:
        urls = all_urls
        logger.info("Processing all %d URLs (reprocess mode)", len(urls))
    else:
        processed = get_processed_urls()
        urls = [u for u in all_urls if u not in processed]
        skipped = len(all_urls) - len(urls)
        if skipped:
            logger.info("Skipping %d already processed URLs", skipped)

    if not urls:
        logger.info("No URLs to process.")
        return

    # Log a breakdown of what we're about to scrape
    linkedin_count = sum(1 for u in urls if _is_linkedin_url(u))
    other_count = len(urls) - linkedin_count
    logger.info(
        "Scraping %d job pages… (%d LinkedIn / %d other)",
        len(urls), linkedin_count, other_count,
    )

    scraped_jobs: list[dict] = []
    blocked_urls: list[str] = []

    for i, url in enumerate(urls):
        logger.info("  [%d/%d] Scraping: %s", i + 1, len(urls), url)
        result = await _scrape_job_page(url)

        if result is None:
            mark_url_processed(url, status="FAILED")
            log_to_processed_file(
                {"title": "Unknown", "company": "Unknown", "url": url,
                 "salary": "N/A", "score": "N/A", "score_reason": "Scrape failed"},
                "scrape_failed",
            )
            notify_error(url, "Could not extract page content")

        elif result.get("blocked"):
            blocked_urls.append(url)
            mark_url_processed(url, status="BLOCKED")
            log_to_processed_file(
                {"title": "Blocked", "company": "Unknown", "url": url,
                 "salary": "N/A", "score": "N/A",
                 "score_reason": "Site blocked automated access"},
                "blocked",
            )
        else:
            scraped_jobs.append(result)

    if blocked_urls:
        # Separate LinkedIn blocks from other blocks for a more useful message
        li_blocked = [u for u in blocked_urls if _is_linkedin_url(u)]
        other_blocked = [u for u in blocked_urls if not _is_linkedin_url(u)]

        if li_blocked:
            notify(
                title=f"{len(li_blocked)} LinkedIn URL(s) blocked — login required",
                message=(
                    "These LinkedIn jobs require a logged-in session to view.\n"
                    "This usually means the posting has been set to 'Easy Apply only'\n"
                    "or the company has restricted public access.\n\n"
                    "You can view them manually:\n"
                    + "\n".join(li_blocked)
                ),
                priority="low",
                tags=["no_entry", "briefcase"],
            )

        if other_blocked:
            notify(
                title=f"{len(other_blocked)} URL(s) blocked — cannot scrape",
                message=(
                    "The following URLs block automated access.\n"
                    "Find these jobs through Adzuna or another source instead:\n\n"
                    + "\n".join(other_blocked)
                ),
                priority="low",
                tags=["no_entry", "briefcase"],
            )

    if not scraped_jobs:
        logger.info("No pages could be scraped successfully.")
        return

    logger.info("Parsing job details from %d scraped pages…", len(scraped_jobs))
    jobs: list[dict] = []
    for i, scraped in enumerate(scraped_jobs):
        logger.info(
            "  [%d/%d] Parsing: %s",
            i + 1, len(scraped_jobs), scraped["page_title"][:60],
        )
        job = _parse_job_from_text(scraped)
        logger.info("     → %s @ %s", job["title"], job["company"])
        jobs.append(job)

    # Filter jobs where parsing clearly failed (still looks blocked)
    parseable = []
    unparseable = []
    for job in jobs:
        if (
            job.get("title") in ("Not specified", "Unknown Title")
            and job.get("company") in ("Not specified", "Unknown Company")
        ):
            unparseable.append(job)
            mark_url_processed(job["url"], status="BLOCKED")
            log_to_processed_file(
                {**job, "score_reason": "Site blocked automated access"},
                "blocked",
            )
        else:
            parseable.append(job)

    if unparseable:
        logger.info(
            "%d URLs returned no parseable content (blocked sites)",
            len(unparseable),
        )
        notify(
            title=f"{len(unparseable)} manual URL(s) blocked",
            message=(
                f"{len(unparseable)} URLs could not be scraped.\n"
                "These are likely ZipRecruiter or Indeed links.\n"
                "Find these jobs through Adzuna instead."
            ),
            priority="low",
            tags=["no_entry", "briefcase"],
        )

    jobs = parseable
    if not jobs:
        logger.info("No parseable job content found.")
        return

    # Filter senior roles
    filtered: list[dict] = []
    for job in jobs:
        if is_too_senior(job):
            logger.info(
                "[Filtered] %s @ %s — too senior", job["title"], job["company"]
            )
            mark_url_processed(job["url"])
            log_to_processed_file(job, "filtered_senior")
            notify_job_filtered(job)
        else:
            filtered.append(job)

    if not filtered:
        logger.info("All manually provided jobs were filtered as too senior.")
        return

    logger.info("Scoring %d jobs against your résumé…", len(filtered))
    scored = []
    for i, job in enumerate(filtered):
        logger.info(
            "  [%d/%d] Scoring: %s @ %s",
            i + 1, len(filtered), job["title"], job["company"],
        )
        scored.append(score_job(job, resume_text))

    scored_sorted = sorted(scored, key=lambda x: x["score"], reverse=True)

    logger.info("=== Manual Job Results ===")
    for job in scored_sorted:
        logger.info(
            "  [%d/10] %s @ %s | %s",
            job["score"], job["title"], job["company"],
            job.get("salary", "Not specified"),
        )
        logger.info("         %s", job.get("score_reason", ""))
        logger.info("         %s", job["url"])

    matched = [j for j in scored_sorted if j["score"] >= RELEVANCE_THRESHOLD]
    below = [j for j in scored_sorted if j["score"] < RELEVANCE_THRESHOLD]

    for job in below:
        mark_url_processed(job["url"])
        log_to_processed_file(job, "below_threshold")
        notify_job_skipped(job)

    if matched:
        logger.info("Saving %d matched manual jobs to database…", len(matched))
        for job in matched:
            logger.info("  Saving: %s @ %s", job["title"], job["company"])
            row_id = upsert_job(job)
            logger.info(
                "     Saved (id=%s) — use dashboard to generate cover letter",
                row_id,
            )
            mark_url_processed(job["url"])
            log_to_processed_file(job, "saved_awaiting_cover_letter")
            notify_job_processed(job)
    else:
        logger.info("No manual jobs met the relevance threshold.")

    logger.info("=== Manual Job Processing Complete ===")


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def run(url_file: str = "manual_jobs.txt", reprocess: bool = False) -> None:
    """Synchronous entry point for use from job_agent.py or command line."""
    asyncio.run(_process_manual_jobs(url_file, reprocess=reprocess))


if __name__ == "__main__":
    from logger import setup_logging
    setup_logging()

    parser = argparse.ArgumentParser(description="Process manual job URLs")
    parser.add_argument(
        "--reprocess",
        action="store_true",
        help=(
            "Re-scrape all URLs regardless of prior processing status. "
            "Useful after scraper upgrades or to refresh stale data."
        ),
    )
    parser.add_argument(
        "--file",
        default="manual_jobs.txt",
        help="Path to URL file (default: manual_jobs.txt)",
    )
    args = parser.parse_args()
    run(url_file=args.file, reprocess=args.reprocess)
