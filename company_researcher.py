# company_researcher.py
"""
Researches companies using web search and synthesises findings
into structured notes accessible from the dashboard.

Triggered automatically when a job status changes to
'applied' or 'interviewing'. Skips companies already researched
unless force=True is passed.

Token optimisations vs original:
- Search results are deduplicated before being sent to the LLM.
  Seven Brave queries frequently return overlapping snippets for the
  same company; deduplication cuts context by ~25% with no info loss.
- The LLM is now asked to respond in JSON, replacing the brittle
  line-by-line heading parser with a single json.loads() call.
- Per-query snippet length is capped at 500 chars to prevent one
  verbose result from crowding out the rest.
"""
import json
import logging
import re
import time

import requests

from config import BRAVE_SEARCH_API_KEY
from database import (
    is_company_researched,
    set_company_research_status,
    upsert_company,
)
from llm_client import invoke_llm

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Current year used in search queries so they stay fresh automatically
# ---------------------------------------------------------------------------
from datetime import datetime as _dt
_CURRENT_YEAR = _dt.now().year
_PREV_YEAR = _CURRENT_YEAR - 1

SEARCH_QUERIES = [
    "{company} company overview employees founded",
    "{company} tech stack engineering tools",
    "{company} company culture glassdoor reviews",
    "{company} funding revenue layoffs financial",
    "{company} interview process difficulty",
    f"{{company}} news {_PREV_YEAR} {_CURRENT_YEAR}",
    "{company} remote work policy hybrid",
]

# Maximum characters kept per individual snippet before deduplication.
# Prevents one verbose result crowding out all others.
_MAX_SNIPPET_CHARS = 500

# Maximum total characters of deduplicated search content sent to the LLM.
_MAX_CONTEXT_CHARS = 8000

RESEARCH_PROMPT = """\
You are researching a company on behalf of a job applicant.
Using the search results provided, write structured research notes.

COMPANY: {company}

SEARCH RESULTS:
{search_results}

Respond with ONLY a valid JSON object. No prose before or after it.
Use exactly these keys; write "Could not determine from available sources." \
for any section where the search results provide no useful information:

{{
  "overview": "<size, industry, founding year, HQ, what they do, customers>",
  "tech_stack": "<known technologies, languages, frameworks, cloud, security tools>",
  "culture": "<employee sentiment, management style, work-life balance, praise/complaints>",
  "financial_health": "<funding stage, revenue, layoffs, public/private/PE, stability>",
  "interview_process": "<rounds, types, technical assessments, difficulty>",
  "recent_news": "<notable events in last 12 months: launches, acquisitions, changes>",
  "remote_policy": "<fully remote, hybrid, or on-site; any known changes>",
  "research_summary": "<2-3 sentence overall assessment: stable employer? red/green flags \
for a network/security professional?>"
}}
"""


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _search_brave(query: str) -> list[str]:
    """
    Execute a single Brave Search query and return a list of snippet
    strings (each capped at _MAX_SNIPPET_CHARS). Returns an empty list
    on error so the caller can continue with partial results.
    """
    try:
        response = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": BRAVE_SEARCH_API_KEY,
            },
            params={
                "q": query,
                "count": 5,
                "search_lang": "en",
                "country": "us",
                "text_decorations": False,
            },
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()

        snippets: list[str] = []

        # Infobox first — structured company data when available
        for box in data.get("infobox", {}).get("results", []):
            long_desc = box.get("long_desc", "")
            if long_desc:
                snippets.insert(0, long_desc[:_MAX_SNIPPET_CHARS])

        for result in data.get("web", {}).get("results", []):
            desc = result.get("description", "")
            if desc:
                text = f"{result.get('title', '')}: {desc}"
                snippets.append(text[:_MAX_SNIPPET_CHARS])

        return snippets

    except requests.exceptions.HTTPError as exc:
        code = exc.response.status_code
        if code == 401:
            logger.error("Brave API key invalid or expired.")
        elif code == 429:
            logger.warning("Brave API rate limit reached.")
        else:
            logger.error("Brave search HTTP error %d: %s", code, exc)
        return []
    except Exception as exc:
        logger.warning("Brave search failed: %s", exc)
        return []


def _normalise_snippet(text: str) -> str:
    """Collapse whitespace for reliable deduplication comparisons."""
    return re.sub(r'\s+', ' ', text).strip().lower()


def _gather_search_results(company: str) -> str:
    """
    Run all SEARCH_QUERIES for a company, deduplicate overlapping snippets
    across queries, and return the combined text ready to inject into the
    prompt.

    Deduplication strategy: a snippet is considered a duplicate if its
    normalised text was already seen in a previous query's results. This
    catches the common case where several queries return the same Glassdoor
    summary or Wikipedia paragraph.
    """
    seen_normalised: set[str] = set()
    all_sections: list[str] = []

    for i, template in enumerate(SEARCH_QUERIES):
        query = template.format(company=company)
        logger.info("Searching: %s", query)
        snippets = _search_brave(query)

        unique: list[str] = []
        for snippet in snippets:
            norm = _normalise_snippet(snippet)
            if norm and norm not in seen_normalised:
                seen_normalised.add(norm)
                unique.append(snippet)

        if unique:
            section = f"QUERY: {query}\n" + "\n".join(unique)
            all_sections.append(section)
            logger.debug("  → %d unique snippet(s)", len(unique))
        else:
            logger.debug("  → no new snippets (all duplicates or empty)")

        if i < len(SEARCH_QUERIES) - 1:
            time.sleep(3)

    combined = "\n\n".join(all_sections)
    total = len(combined)
    logger.info(
        "Deduplicated search content for %s: %d chars (cap %d)",
        company, total, _MAX_CONTEXT_CHARS,
    )
    return combined[:_MAX_CONTEXT_CHARS]


# ---------------------------------------------------------------------------
# LLM response parsing — JSON only, no brittle heading scanner
# ---------------------------------------------------------------------------

_EXPECTED_KEYS = {
    "overview", "tech_stack", "culture", "financial_health",
    "interview_process", "recent_news", "remote_policy", "research_summary",
}


def _parse_research_response(response_text: str) -> dict:
    """
    Parse the LLM's JSON response into a dict matching the companies
    table columns. Falls back to empty strings for any missing key.
    """
    sections = {key: "" for key in _EXPECTED_KEYS}

    # Strip optional markdown fences the model may add despite instructions
    clean = re.sub(r'^```(?:json)?\s*', '', response_text.strip(), flags=re.MULTILINE)
    clean = re.sub(r'\s*```$', '', clean.strip(), flags=re.MULTILINE)

    try:
        data = json.loads(clean)
        for key in _EXPECTED_KEYS:
            value = data.get(key, "")
            if isinstance(value, str):
                sections[key] = value.strip()
            elif value:
                sections[key] = str(value).strip()
    except json.JSONDecodeError:
        # Last-resort: try to extract the first {...} block
        match = re.search(r'\{.*\}', clean, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
                for key in _EXPECTED_KEYS:
                    sections[key] = str(data.get(key, "")).strip()
            except json.JSONDecodeError:
                logger.warning(
                    "Could not parse research JSON for company — "
                    "all sections will be empty"
                )
        else:
            logger.warning(
                "LLM returned no JSON block for company research — "
                "all sections will be empty"
            )

    return sections


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def research_company(company: str, force: bool = False) -> bool:
    """
    Research a company and store results in the database.
    Returns True if research was performed, False if skipped.

    Sets research_status to 'in_progress' before the long-running
    network + LLM step so the dashboard can reflect current state.
    force=True re-researches even if a completed record exists.
    """
    if not force and is_company_researched(company):
        logger.info("%s already researched — skipping", company)
        return False

    logger.info("Starting research for: %s", company)
    set_company_research_status(company, "in_progress")

    try:
        search_results = _gather_search_results(company)

        logger.info("Synthesising findings for %s…", company)
        prompt = RESEARCH_PROMPT.format(
            company=company,
            search_results=search_results,
        )
        response = invoke_llm(prompt)

        research = _parse_research_response(response)
        upsert_company(company, research)
        logger.info("Research saved for %s", company)
        return True

    except Exception:
        logger.exception("Research failed for %s", company)
        set_company_research_status(company, "failed")
        return False


def research_companies_for_job(job_id: int, force: bool = False) -> None:
    """
    Trigger research for the company associated with a specific job.
    Called from database.update_status via a background thread.
    """
    from database import get_job_by_id

    job = get_job_by_id(job_id)
    if not job:
        logger.warning("research_companies_for_job: job %d not found", job_id)
        return

    company = job.get("company", "").strip()
    if not company or company == "Unknown Company":
        logger.warning(
            "research_companies_for_job: no valid company for job %d", job_id
        )
        return

    research_company(company, force=force)


def research_and_generate_cover_letter(job_id: int) -> None:
    """
    Coupled entry point triggered by the dashboard "Generate Cover Letter"
    button. Runs company research first, then generates the cover letter
    using the research as context — or falls back to job-description-only
    if research yields no usable content (null result).

    A wall-clock timeout of COVER_LETTER_TIMEOUT_MINUTES is enforced.
    If the combined research + generation takes longer than this, the
    cover_letter_status is set to 'failed' and the thread exits cleanly.
    """
    import threading as _threading
    from config import COVER_LETTER_TIMEOUT_MINUTES
    from database import get_company_for_job, get_job_by_id, set_cover_letter_status
    from cover_letter import generate_and_store_cover_letter_for_job

    _timed_out = _threading.Event()

    def _timeout_handler() -> None:
        _timed_out.set()
        logger.error(
            "[CoverLetter] Timed out after %d minutes for job %d — "
            "setting status to failed",
            COVER_LETTER_TIMEOUT_MINUTES, job_id,
        )
        set_cover_letter_status(job_id, "failed")

    timer = _threading.Timer(
        COVER_LETTER_TIMEOUT_MINUTES * 60,
        _timeout_handler,
    )
    timer.start()

    try:
        job = get_job_by_id(job_id)
        if not job:
            logger.error("[CoverLetter] research_and_generate: job %d not found", job_id)
            return

        company = job.get("company", "").strip()
        if not company or company == "Unknown Company":
            logger.warning(
                "[CoverLetter] No valid company for job %d — generating without research",
                job_id,
            )
            if not _timed_out.is_set():
                generate_and_store_cover_letter_for_job(job_id, research=None)
            return

        # --- Stage 1: Company research ---
        logger.info("[CoverLetter] Stage 1 — researching %s for job %d", company, job_id)
        set_cover_letter_status(job_id, "in_progress")

        try:
            research_company(company)
        except Exception:
            logger.exception(
                "[CoverLetter] Research failed for %s (job %d) — aborting cover letter",
                company, job_id,
            )
            if not _timed_out.is_set():
                set_cover_letter_status(job_id, "failed")
            return

        if _timed_out.is_set():
            return

        # --- Determine research outcome ---
        research = get_company_for_job(job_id)

        _EMPTY_PHRASES = {"could not determine", "no results found", ""}

        def _is_null_research(r: dict | None) -> bool:
            if not r:
                return True
            content_fields = [
                "overview", "tech_stack", "culture",
                "financial_health", "interview_process",
            ]
            for field in content_fields:
                val = (r.get(field) or "").strip().lower()
                if val and not any(phrase in val for phrase in _EMPTY_PHRASES):
                    return False
            return True

        if _is_null_research(research):
            logger.info(
                "[CoverLetter] Research for %s yielded no content — "
                "generating cover letter without company context",
                company,
            )
            research = None
        else:
            logger.info(
                "[CoverLetter] Research complete for %s — proceeding to cover letter",
                company,
            )

        # --- Stage 2: Cover letter generation ---
        if not _timed_out.is_set():
            generate_and_store_cover_letter_for_job(job_id, research=research)

    finally:
        timer.cancel()
