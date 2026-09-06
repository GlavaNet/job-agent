# resume_tailor.py
"""
Generates an ATS-optimised tailored résumé for a specific job posting.

The tailored résumé is produced by analysing the job description for:
  - Hard skills / technical keywords
  - Action verbs and power words
  - Soft skills and competency language
  - Role-specific terminology

…then rewriting the candidate's résumé to mirror that language without
fabricating experience.  The output is stored in the jobs.tailored_resume
column and surfaced in the dashboard job modal.

Triggered on demand via the dashboard "Tailor Résumé" button.
Can also be called from the command line:

    python3 resume_tailor.py --job-id 42
"""

import argparse
import logging
import sys

from config import RESUME_PATH, RESUME_TAILOR_DESCRIPTION_CHARS, RESUME_TAILOR_RESUME_CHARS
from database import get_job_by_id, save_tailored_resume
from llm_client import invoke_llm
from models import Job
from resume_parser import load_resume

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
# KEYWORD-AWARE RESUME TAILOR PROMPT
# This version preserves structure AND strategically highlights keywords
# you actually have from the job description

TAILOR_PROMPT = """You are a résumé editor. Your job is to tailor the provided résumé
to a specific job by reordering and rewording bullet points to surface relevant
skills. You do NOT create, add, or remove sections. You do NOT add preamble.

CRITICAL RULES:
1. PRESERVE EVERY SECTION from the original résumé exactly as-is.
   - Keep all section headers, formatting, structure
   - Do not remove sections
   - Do not add new sections like "Objective"
2. PRESERVE ALL DATES, company names, job titles exactly
3. USE KEYWORD GUIDANCE (below) to reorder bullets strategically
4. WITHIN EACH SECTION: reorder bullets to prioritize matched keywords
   - Move bullets that showcase the prioritized keywords to the top
   - Reword bullets (minimally) to make keyword connections explicit
   - Example: "Managed servers" → "Managed AWS servers" if AWS is prioritized
5. Use straight quotes (") and hyphens (-) only. No smart quotes or em-dashes.
6. Do not add conversational text, preamble, or explanation
7. Return ONLY the edited résumé text, no preamble

{keyword_guidance}

TARGET JOB
Title:   {title}
Company: {company}
Description:
{description}

---

ORIGINAL RÉSUMÉ (preserve this structure):
{resume}

---

Now edit the résumé by:
1. Reordering bullets within each section to surface the prioritized keywords
2. Minimally rewording to make connections explicit (e.g., mention tool names)
3. Keeping every section and all original information

Output ONLY the edited résumé, no preamble:"""

# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def generate_tailored_resume(job: Job, resume_text: str) -> str:
    """Generate a tailored resume for a job, highlighting matched keywords."""
    from keyword_matcher import match_keywords, generate_keyword_hint
    
    description = (job.get("description") or "")[:RESUME_TAILOR_DESCRIPTION_CHARS]
    resume_excerpt = resume_text[:RESUME_TAILOR_RESUME_CHARS]

    # Find keywords from job that you actually have
    matched_keywords, unmatched = match_keywords(description, resume_excerpt)
    
    # Generate hint for LLM
    keyword_guidance = generate_keyword_hint(matched_keywords)

    prompt = TAILOR_PROMPT.format(
        title=job.get("title", ""),
        company=job.get("company", ""),
        description=description,
        resume=resume_excerpt,
        keyword_guidance=keyword_guidance,
    )
    return invoke_llm(prompt)

def generate_and_store_tailored_resume(job_id: int) -> bool:
    """
    Generate and persist a tailored résumé for a single job by ID.

    Called from the dashboard API endpoint in a background thread.
    Returns True on success, False on failure.
    """
    job = get_job_by_id(job_id)
    if not job:
        logger.error("[ResumeTailor] Job %d not found", job_id)
        return False

    logger.info(
        "[ResumeTailor] Starting for job %d: %s @ %s",
        job_id, job.get("title"), job.get("company"),
    )

    try:
        resume_text = load_resume(RESUME_PATH)
    except Exception:
        logger.exception("[ResumeTailor] Failed to load résumé for job %d", job_id)
        return False

    try:
        tailored = generate_tailored_resume(job, resume_text)
        save_tailored_resume(job_id, tailored)
        logger.info("[ResumeTailor] Saved tailored résumé for job %d", job_id)
        return True
    except Exception:
        logger.exception("[ResumeTailor] Generation failed for job %d", job_id)
        return False


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate an ATS-tailored résumé for a job in the database"
    )
    parser.add_argument(
        "--job-id",
        type=int,
        required=True,
        help="Database ID of the job to tailor the résumé for",
    )
    parser.add_argument(
        "--print",
        dest="print_output",
        action="store_true",
        help="Print the tailored résumé to stdout after saving",
    )
    args = parser.parse_args()

    from logger import setup_logging
    from database import init_db, get_job_by_id
    setup_logging()
    init_db()

    success = generate_and_store_tailored_resume(args.job_id)
    if not success:
        sys.exit(1)

    if args.print_output:
        job = get_job_by_id(args.job_id)
        if job and job.get("tailored_resume"):
            print(job["tailored_resume"])


if __name__ == "__main__":
    main()
