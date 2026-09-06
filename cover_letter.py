# cover_letter.py
import logging

from config import (
    CANDIDATE_CONTEXT,
    CL_RESUME_CHARS,
    CL_DESCRIPTION_CHARS,
    CL_RESEARCH_CHARS,
)
from llm_client import invoke_llm
from models import Job

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt
#
# {candidate_context} — injected from config.CANDIDATE_CONTEXT so it can
#   be updated in .env without touching source code.
# {resume} — compact candidate profile from job_scorer.get_resume_summary(),
#   not raw resume text. Structured and ~60% smaller.
# {description} — capped at CL_DESCRIPTION_CHARS (default 2000).
# {company_research} — capped at CL_RESEARCH_CHARS (default 2000).
# ---------------------------------------------------------------------------
# TONED-DOWN PROMPT FOR COVER_LETTER.PY
# Replace the existing COVER_LETTER_PROMPT with this version
# It's more honest and avoids overselling skills

COVER_LETTER_PROMPT = """\
You are a professional cover letter writer. Your job is to write honest,
authentic letters that match the candidate to the role.

Write a 3-paragraph cover letter:
  1. Opening: Why this company and role appeal to the candidate
  2. Body: 1-2 specific skills/experiences that match the job
  3. Closing: Simple call to action

CRITICAL GUIDELINES:
- Be honest and specific. Avoid generic language.
- Do not claim expertise you cannot defend in an interview.
- Do not invent skills or exaggerate experience.
- If the candidate doesn't have a required skill, acknowledge the gap
  rather than pretend to have it. Frame related skills instead.
- Avoid superlatives ("passionate about", "driven by", "excited").
  Use straightforward language instead.
- If company research is provided, reference ONE specific fact
  (tech stack, recent news, culture detail). Do not fabricate claims.
- Keep it human and conversational. Sound like the candidate, not a sales pitch.
- Do not oversell the candidate's home lab or side projects as proof of
  professional-grade expertise. They are learning tools, not credentials.

{candidate_context}

CANDIDATE PROFILE:
{resume}

JOB:
Title: {title}
Company: {company}
Description: {description}

COMPANY RESEARCH:
{company_research}

Write the cover letter now:\
"""

_NO_RESEARCH_NOTE = (
    "No company research available — write based on the job description alone."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_research(research: dict | None) -> str:
    """Flatten company research into a prompt-ready block."""
    if not research:
        return _NO_RESEARCH_NOTE
    sections = [
        ("Overview",          research.get("overview")),
        ("Tech Stack",        research.get("tech_stack")),
        ("Culture",           research.get("culture")),
        ("Financial Health",  research.get("financial_health")),
        ("Remote Policy",     research.get("remote_policy")),
        ("Research Summary",  research.get("research_summary")),
    ]
    lines = [
        f"{heading.upper()}:\n{content.strip()}"
        for heading, content in sections
        if content and content.strip()
    ]
    return "\n\n".join(lines) if lines else _NO_RESEARCH_NOTE


def _get_candidate_profile(resume_text: str) -> str:
    """
    Return a compact candidate profile suitable for injection into the
    cover letter prompt.

    Prefers the cached summary produced by job_scorer.get_resume_summary()
    when available (avoids a redundant LLM call). Falls back to a raw
    excerpt capped at CL_RESUME_CHARS if the scorer hasn't run yet —
    which can happen when cover letters are generated in isolation (e.g.
    the dashboard retry path or a standalone test).
    """
    try:
        from job_scorer import get_resume_summary, _SUMMARY_CACHE, _resume_cache_key
        key = _resume_cache_key(resume_text)
        if key in _SUMMARY_CACHE:
            return _SUMMARY_CACHE[key]
        # Summary not yet cached — generate it now and cache for any
        # subsequent calls in this process.
        return get_resume_summary(resume_text)
    except Exception:
        logger.debug(
            "Could not load resume summary from job_scorer — using raw excerpt",
            exc_info=True,
        )
        return resume_text[:CL_RESUME_CHARS]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_cover_letter(
    job: Job,
    resume_text: str,
    research: dict | None = None,
) -> str:
    """
    Generate a complete cover letter for a single job.

    Parameters
    ----------
    job:
        Job dict containing at minimum title, company, description.
    resume_text:
        Raw resume text. A compact profile is extracted/retrieved from
        the job_scorer cache; raw text is only used as a fallback.
    research:
        Company research dict from the database, or None.
    """
    candidate_profile = _get_candidate_profile(resume_text)
    research_block = _format_research(research)[:CL_RESEARCH_CHARS]

    prompt = COVER_LETTER_PROMPT.format(
        candidate_context=CANDIDATE_CONTEXT,
        resume=candidate_profile,
        title=job.get("title", ""),
        company=job.get("company", ""),
        description=(job.get("description") or "")[:CL_DESCRIPTION_CHARS],
        company_research=research_block,
    )
    return invoke_llm(prompt)


def get_resume_text_for_job(job_id: int) -> str:
    """
    Return the best available resume text for cover letter generation.

    Prefers the tailored resume stored in jobs.tailored_resume for this
    specific job (produced by resume_tailor.py) so the cover letter is
    written from an already ATS-optimised profile.  Falls back to the
    raw resume file when no tailored version exists yet.
    """
    from config import RESUME_PATH
    from database import get_job_by_id
    from resume_parser import load_resume

    job = get_job_by_id(job_id)
    if job:
        tailored = (job.get("tailored_resume") or "").strip()
        if tailored:
            logger.debug(
                "[CoverLetter] Using tailored resume for job %d", job_id
            )
            return tailored

    logger.debug(
        "[CoverLetter] No tailored resume for job %d — using default", job_id
    )
    return load_resume(RESUME_PATH)


def generate_and_store_cover_letter_for_job(
    job_id: int,
    research: dict | None,
) -> bool:
    """
    Generate and persist a cover letter for a single job by ID.

    Called from company_researcher.research_and_generate_cover_letter()
    after research completes, and from the dashboard API endpoint.

    Returns True on success, False on failure.
    """
    from config import RESUME_PATH
    from database import get_job_by_id, set_cover_letter_status, upsert_job
    from resume_parser import load_resume

    job = get_job_by_id(job_id)
    if not job:
        logger.error("[CoverLetter] Job %d not found", job_id)
        return False

    set_cover_letter_status(job_id, "in_progress")

    try:
        resume_text = get_resume_text_for_job(job_id)
    except Exception:
        logger.exception("[CoverLetter] Failed to load résumé for job %d", job_id)
        set_cover_letter_status(job_id, "failed")
        return False

    try:
        letter = generate_cover_letter(job, resume_text, research=research)
        upsert_job(job, cover_letter=letter)
        logger.info("[CoverLetter] Saved cover letter for job %d", job_id)
        return True
    except Exception:
        logger.exception("[CoverLetter] Generation failed for job %d", job_id)
        set_cover_letter_status(job_id, "failed")
        return False


def retry_cover_letter(job_id: int, resume_text: str) -> bool:
    """
    Retry cover letter generation for a single job by ID.
    Used by the dashboard retry button — does not re-run research.
    Returns True on success, False on failure.
    """
    from database import get_company_for_job, set_cover_letter_status, upsert_job, get_job_by_id

    job = get_job_by_id(job_id)
    if not job:
        logger.error("[CoverLetter] Retry: job %d not found", job_id)
        return False

    logger.info(
        "[CoverLetter] Retrying for job %d: %s @ %s",
        job_id, job.get("title"), job.get("company"),
    )
    set_cover_letter_status(job_id, "in_progress")

    try:
        research = get_company_for_job(job_id)
        letter = generate_cover_letter(job, resume_text, research=research)
        upsert_job(job, cover_letter=letter)
        logger.info("[CoverLetter] Retry succeeded for job %d", job_id)
        return True
    except Exception:
        logger.exception("[CoverLetter] Retry failed for job %d", job_id)
        set_cover_letter_status(job_id, "failed")
        return False
