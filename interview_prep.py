# interview_prep.py
"""
Generates a structured interview preparation sheet for a job when its
status transitions to 'interviewing'.

The prep sheet is synthesised from three sources:
  1. The job description (title, company, description, salary)
  2. The candidate's résumé (loaded from RESUME_PATH at call time)
  3. Any company research already stored in the database

Output is stored in the jobs.interview_prep column and rendered in
the dashboard under the new Interview Prep tab.

Triggered automatically by database.update_status() when a job moves
to 'interviewing'. Can also be called manually via the dashboard
"Generate Prep Sheet" button or from the command line:

    python3 interview_prep.py --job-id 42
"""

import argparse
import logging
import os
import sys

from config import (
    RESUME_PATH,
    PREP_RESUME_CHARS,
    PREP_DESCRIPTION_CHARS,
    PREP_COMPANY_RESEARCH_CHARS,
)
from database import get_company_for_job, get_job_by_id, save_interview_prep
from llm_client import invoke_llm
from models import CompanyResearch, Job
from resume_parser import load_resume

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

PREP_PROMPT = """You are an interview coach. Using the resume, job, and company info below, write a concise interview prep sheet with these sections:

ROLE SNAPSHOT: (2-3 sentences on day-to-day duties and 90-day success)
KEY TECHNICAL TOPICS: (technologies from the job description to review)
LIKELY QUESTIONS: (6 questions, labeled [Technical] or [Behavioural])
STAR STORIES: (2 resume situations mapped to job requirements)
QUESTIONS TO ASK: (3 specific questions for the interviewer)
RED FLAGS: (1-2 candidate concerns and how to frame them)

RESUME: {resume}
JOB: {title} at {company} ({location}) | Salary: {salary}
DESCRIPTION: {description}
COMPANY: {company_research}

Write the prep sheet now:"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_company_research(research: CompanyResearch | None) -> str:
    """Flatten company research dict into a readable block for the prompt."""
    if not research:
        return "No company research available yet."

    sections = [
        ("Overview", research.get("overview")),
        ("Tech Stack", research.get("tech_stack")),
        ("Culture", research.get("culture")),
        ("Financial Health", research.get("financial_health")),
        ("Interview Process", research.get("interview_process")),
        ("Recent News", research.get("recent_news")),
        ("Remote Policy", research.get("remote_policy")),
    ]

    lines = []
    for heading, content in sections:
        if content and content.strip():
            lines.append(f"{heading.upper()}:\n{content.strip()}")

    return "\n\n".join(lines) if lines else "No company research available yet."


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_prep_sheet(job_id: int) -> str | None:
    """
    Generate an interview prep sheet for the given job ID.

    Loads the job, résumé, and any existing company research, sends
    them to the LLM, and returns the generated text.

    Returns None if the job cannot be found.
    """
    job = get_job_by_id(job_id)
    if not job:
        logger.error("[PrepSheet] Job %d not found", job_id)
        return None

    logger.info(
        "[PrepSheet] Generating prep sheet for job %d: %s @ %s",
        job_id, job.get("title"), job.get("company"),
    )

    # Load résumé
    try:
        resume_text = load_resume(RESUME_PATH)
    except Exception:
        logger.exception("[PrepSheet] Failed to load résumé from %s", RESUME_PATH)
        resume_text = "Résumé not available."

    # Load company research (may be None if not yet complete)
    try:
        company_research = get_company_for_job(job_id)
    except Exception:
        logger.warning("[PrepSheet] Could not load company research for job %d", job_id)
        company_research = None

    company_research_text = _format_company_research(company_research)

    prompt = PREP_PROMPT.format(
        resume=resume_text[:PREP_RESUME_CHARS],
        title=job.get("title", ""),
        company=job.get("company", ""),
        location=job.get("location", ""),
        salary=job.get("salary", "Not specified"),
        description=(job.get("description") or "")[:PREP_DESCRIPTION_CHARS],
        company_research=company_research_text[:PREP_COMPANY_RESEARCH_CHARS],
    )

    try:
        prep_sheet = invoke_llm(prompt)
        logger.info("[PrepSheet] Generated %d chars for job %d", len(prep_sheet), job_id)
        return prep_sheet
    except Exception:
        logger.exception("[PrepSheet] LLM call failed for job %d", job_id)
        return None


def generate_and_store_prep_sheet(job_id: int) -> None:
    """
    Generate a prep sheet and persist it to the database.
    This is the function called from background threads.
    """
    prep_sheet = generate_prep_sheet(job_id)
    if prep_sheet:
        save_interview_prep(job_id, prep_sheet)
        logger.info("[PrepSheet] Saved prep sheet for job %d", job_id)
    else:
        logger.warning("[PrepSheet] No prep sheet generated for job %d", job_id)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from logger import setup_logging
    setup_logging()

    parser = argparse.ArgumentParser(
        description="Generate an interview prep sheet for a specific job"
    )
    parser.add_argument(
        "--job-id", type=int, required=True,
        help="Database ID of the job to generate a prep sheet for",
    )
    parser.add_argument(
        "--print", action="store_true",
        help="Print the prep sheet to stdout instead of saving to DB",
    )
    args = parser.parse_args()

    if args.print:
        sheet = generate_prep_sheet(args.job_id)
        if sheet:
            print(sheet)
        else:
            print("Failed to generate prep sheet.", file=sys.stderr)
            sys.exit(1)
    else:
        generate_and_store_prep_sheet(args.job_id)
        job = get_job_by_id(args.job_id)
        if job and job.get("interview_prep"):
            print(f"Prep sheet saved for job {args.job_id}.")
        else:
            print("Failed to save prep sheet.", file=sys.stderr)
            sys.exit(1)
