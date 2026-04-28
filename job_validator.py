# job_validator.py
"""
Fetches the full job posting page for each matched job and scans for
disqualifying content that may not appear in the truncated descriptions
returned by job APIs.

Checks:
- Security clearance requirements
- Part-time / contract indicators (contextual matching only)
- Seniority indicators (experience year patterns)

Called after scoring, before cover letter generation.
"""
import asyncio
import logging
import re

from job_scorer import EXCLUDE_REQUIREMENT_KEYWORDS
from models import Job

logger = logging.getLogger(__name__)

# Compile patterns once at import time rather than on every call
_EXPERIENCE_PATTERNS = [
    re.compile(r'\b(7|8|9|10|11|12|13|14|15|\d{2})\+?\s*years?\s*(of\s*)?(experience|exp)\b'),
    re.compile(r'\bminimum\s*(of\s*)?(7|8|9|10|\d{2})\s*years?\b'),
    re.compile(r'\bat\s*least\s*(7|8|9|10|\d{2})\s*years?\b'),
]

_EMPLOYMENT_CONTEXT_PATTERNS = [re.compile(p) for p in [
    # Part-time
    r'this\s+is\s+a\s+part[\s-]time',
    r'part[\s-]time\s+position',
    r'part[\s-]time\s+role',
    r'part[\s-]time\s+hours',
    r'part[\s-]time\s+employment',
    r'part[\s-]time\s+opportunity',
    r'hiring\s+part[\s-]time',
    r'looking\s+for\s+part[\s-]time',
    r'seeking\s+part[\s-]time',
    # Contract
    r'this\s+is\s+a\s+contract',
    r'contract\s+position',
    r'contract\s+role',
    r'contract\s+opportunity',
    r'contract[\s-]to[\s-]hire',
    r'w2\s+contract',
    r'c2c\s+only',
    r'corp\s+to\s+corp\s+only',
    r'1099\s+position',
    r'independent\s+contractor\s+position',
    r'this\s+is\s+a\s+temporary',
    r'temporary\s+position',
    r'temp\s+position',
    r'contingent\s+position',
    r'contingent\s+role',
    # Internship
    r'internship\s+position',
    r'this\s+is\s+an?\s+intern',
    r'intern\s+role',
    r'co[\s-]op\s+position',
]]

_BOT_INDICATORS = [
    "just a moment",
    "verifying you are not a bot",
    "security verification",
    "checking your browser",
]

_DESCRIPTION_START_MARKERS = [
    "job description", "about the role", "about this role",
    "about the position", "position summary", "role summary",
    "what you'll do", "what you will do", "responsibilities",
    "overview", "the role", "about the job", "job summary",
    "role description",
]

_DESCRIPTION_END_MARKERS = [
    "similar jobs", "related jobs", "you might also like",
    "apply now", "apply for this job", "about the company",
    "company overview", "equal opportunity", "eeo statement",
    "we are an equal",
]


# ---------------------------------------------------------------------------
# Page fetching
# ---------------------------------------------------------------------------

async def _fetch_full_description(url: str) -> str | None:
    """
    Fetch the rendered text content of a job posting page.
    Returns the text, or None if fetching failed or a bot block was detected.
    """
    from scrapling.fetchers import DynamicFetcher

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
                content = re.sub(r'<[^>]+>', ' ', str(page.html_content))
                content = re.sub(r'\s+', ' ', content).strip()

        if not content or len(content.strip()) < 200:
            return None

        if any(ind in content.lower() for ind in _BOT_INDICATORS):
            logger.debug("Bot block detected for %s", url)
            return None

        return content

    except Exception:
        logger.exception("[Validator] Fetch error for %s", url)
        return None


# ---------------------------------------------------------------------------
# Content analysis
# ---------------------------------------------------------------------------

def _extract_description_section(full_text: str) -> str:
    """
    Narrow the page text to the job description section where possible,
    to reduce false positives from navigation elements and filter widgets.
    Falls back to the full text if no clear section boundary is found.
    """
    text_lower = full_text.lower()

    start_idx = 0
    for marker in _DESCRIPTION_START_MARKERS:
        idx = text_lower.find(marker)
        if idx != -1:
            start_idx = idx
            break

    end_idx = len(full_text)
    if start_idx > 0:
        for marker in _DESCRIPTION_END_MARKERS:
            idx = text_lower.find(marker, start_idx + 100)
            if idx != -1:
                end_idx = min(end_idx, idx)

    extracted = full_text[start_idx:end_idx].strip()
    return extracted if len(extracted) > 300 else full_text


def _find_disqualifying_content(full_text: str) -> tuple[bool, str | None]:
    """
    Scan job page text for disqualifying content.

    Security clearance keywords are matched anywhere — they are
    unambiguous regardless of context. Employment type keywords
    require a surrounding phrase that unambiguously describes this
    specific role, since bare terms like 'contract' appear in page
    UI elements on nearly every job board.

    Returns (is_disqualified, reason_string).
    """
    text = _extract_description_section(full_text)
    text_lower = text.lower()

    for kw in EXCLUDE_REQUIREMENT_KEYWORDS:
        if kw in text_lower:
            return True, f"security clearance requirement (matched: '{kw}')"

    for pattern in _EMPLOYMENT_CONTEXT_PATTERNS:
        m = pattern.search(text_lower)
        if m:
            return True, f"part-time or contract employment (matched: '{m.group()}')"

    for pattern in _EXPERIENCE_PATTERNS:
        m = pattern.search(text_lower)
        if m:
            return True, f"seniority requirement (matched: '{m.group()}')"

    return False, None


# ---------------------------------------------------------------------------
# Per-job validation
# ---------------------------------------------------------------------------

async def _validate_job(job: Job) -> tuple[bool, str | None]:
    """
    Fetch and validate a single job posting.
    Returns (is_valid, disqualification_reason).
    Jobs that cannot be fetched are kept (benefit of the doubt).
    """
    url = job.get("url", "")
    if not url:
        return True, None

    logger.info("[Validator] Fetching: %s", url[:70])
    full_text = await _fetch_full_description(url)

    if not full_text:
        logger.info("[Validator] Could not fetch — keeping job")
        return True, None

    is_disqualified, reason = _find_disqualifying_content(full_text)

    if is_disqualified:
        logger.info("[Validator] DISQUALIFIED — %s", reason)
        return False, reason

    logger.info("[Validator] ✓ Passed full description check")
    return True, None


async def _validate_matched_jobs(
    jobs: list[Job],
) -> tuple[list[Job], list[Job]]:
    """
    Validate all matched jobs sequentially.
    Returns (valid_jobs, disqualified_jobs).
    Disqualified jobs carry a '_disqualification_reason' key.
    """
    if not jobs:
        return [], []

    logger.info("[Validator] Full description scan for %d jobs…", len(jobs))

    valid: list[Job] = []
    disqualified: list[Job] = []

    for i, job in enumerate(jobs):
        logger.info(
            "  [%d/%d] %s @ %s",
            i + 1, len(jobs),
            job.get("title", "?")[:45],
            job.get("company", "?")[:25],
        )
        is_valid, reason = await _validate_job(job)
        if is_valid:
            valid.append(job)
        else:
            job["_disqualification_reason"] = reason
            disqualified.append(job)

    logger.info(
        "[Validator] %d passed, %d disqualified",
        len(valid), len(disqualified),
    )

    for job in disqualified:
        logger.info(
            "  ✗ %s @ %s — %s",
            job.get("title", "?")[:45],
            job.get("company", "?")[:25],
            job.get("_disqualification_reason"),
        )

    return valid, disqualified


# ---------------------------------------------------------------------------
# Public synchronous wrapper
# ---------------------------------------------------------------------------

def run_validation(
    jobs: list[Job],
) -> tuple[list[Job], list[Job]]:
    """Synchronous entry point for use from non-async pipeline code."""
    return asyncio.run(_validate_matched_jobs(jobs))
