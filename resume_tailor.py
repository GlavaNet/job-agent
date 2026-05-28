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

TAILOR_PROMPT = """You are an expert résumé writer who specialises in ATS optimisation.

Your task is to rewrite the candidate's résumé so it scores highly against
the target job description. Follow every rule below — no exceptions.

RULES:
1. NEVER invent experience, skills, tools, or credentials the candidate
   does not already have.  Only reframe and reword what exists.
2. Mirror the exact action verbs, soft-skill phrases, and technical
   keywords from the job description wherever they truthfully apply.
3. Prioritise bullet points that align with the job's stated
   responsibilities and required qualifications — move them higher.
4. Quantify achievements wherever the original résumé already contains
   numbers; do not invent metrics.
5. Replace weak verbs ("helped with", "was responsible for") with strong
   ATS-friendly action verbs drawn from the job description or the list:
   Architected, Automated, Collaborated, Configured, Delivered, Deployed,
   Designed, Developed, Engineered, Implemented, Led, Maintained,
   Managed, Monitored, Optimised, Reduced, Resolved, Streamlined,
   Supported, Troubleshot.
6. Include the job's soft-skill language (e.g. "cross-functional
   collaboration", "stakeholder communication") in the summary/profile
   section only — do not pepper it through every bullet.
7. Keep all original section headings, dates, company names, and job
   titles intact.
8. Output the complete tailored résumé as plain text, ready to paste
   into an application form.  Do not add commentary or preamble.

---

TARGET JOB
Title:   {title}
Company: {company}
Description:
{description}

---

CANDIDATE'S CURRENT RÉSUMÉ:
{resume}

---

Write the tailored résumé now:"""

# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def generate_tailored_resume(job: Job, resume_text: str) -> str:
    """
    Generate an ATS-optimised résumé tailored to *job*.

    Parameters
    ----------
    job:
        Job dict with at minimum title, company, description.
    resume_text:
        Raw text of the candidate's current résumé.

    Returns
    -------
    str
        The tailored résumé as plain text.
    """
    description = (job.get("description") or "")[:RESUME_TAILOR_DESCRIPTION_CHARS]
    resume_excerpt = resume_text[:RESUME_TAILOR_RESUME_CHARS]

    prompt = TAILOR_PROMPT.format(
        title=job.get("title", ""),
        company=job.get("company", ""),
        description=description,
        resume=resume_excerpt,
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
