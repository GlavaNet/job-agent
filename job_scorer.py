# job_scorer.py
import json
import logging
import re

from config import RELEVANCE_THRESHOLD
from search_profile import (
    EXCLUDE_TITLE_KEYWORDS,
    EXCLUDE_EMPLOYMENT_TITLE_KEYWORDS as EXCLUDE_EMPLOYMENT_KEYWORDS,
    EXCLUDE_EMPLOYMENT_DESCRIPTION_PHRASES,
    EXCLUDE_REQUIREMENT_KEYWORDS,
    SENIOR_EXPERIENCE_YEAR_THRESHOLD,
)
from job_cache import (
    cache_stats, filter_seen_jobs, load_cache,
    mark_seen, prune_cache, save_cache,
)
from llm_client import invoke_llm
from models import Job, JobCache
from preference_engine import load_profile

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Resume summary cache
#
# The raw resume is typically 3,000+ characters and identical for every
# scoring call in a run. We extract a compact skills summary once per
# process lifetime via get_resume_summary(), then inject that ~200 word
# block instead of the full text. This cuts per-call prompt size by ~60%.
#
# The summary is keyed by the first 64 chars of the resume so a changed
# file invalidates the cache automatically.
# ---------------------------------------------------------------------------

_SUMMARY_CACHE: dict[str, str] = {}   # key → summary text

RESUME_SUMMARY_PROMPT = """\
Extract a compact candidate profile from this résumé. Focus only on what \
matters for job scoring.

Respond with ONLY a JSON object in this exact format, nothing else:
{{
  "title": "<current or most recent job title, one line>",
  "years_exp": <integer — total years of relevant professional experience>,
  "top_skills": ["<skill>", "<skill>", ...],
  "domains": ["<domain>", ...],
  "certs": ["<cert>", ...],
  "education": "<degree or highest credential, one line>"
}}

Rules:
- top_skills: 8-12 items, specific technologies and tools only \
(e.g. "Cisco IOS", "pfSense", "Python", "Splunk") — not soft skills
- domains: 3-5 broad areas (e.g. "Network Engineering", "Cybersecurity", \
"Systems Administration")
- certs: list certifications by common name (e.g. "CompTIA Security+"), \
empty list if none
- years_exp: count from first professional IT/tech role to present
- education: one line, e.g. "B.S. Cybersecurity (in progress)" or \
"A.A.S. Network Technology"

RESUME:
{resume}
"""

SCORE_PROMPT = """\
You are a job relevance evaluator. Score how well a job posting matches \
a candidate on a scale of 1-10.

CANDIDATE PROFILE:
{candidate_profile}

{preference_section}\
JOB POSTING:
Title: {title}
Company: {company}
Salary: {salary}
Description: {description}

Scoring guidance:
- Base the score primarily on skills and domain match
- Weight domain alignment heavily — e.g. a Network Engineer profile should \
score highly on network/security roles and low on unrelated IT support
- If a preference profile is provided, adjust up or down based on \
demonstrated patterns
- 10 = near-perfect skills + domain match; 1 = no meaningful overlap

Respond with ONLY a JSON object in this exact format, nothing else:
{{"score": <integer 1-10>, "reason": "<one sentence explanation>"}}
"""

PREFERENCE_SECTION_TEMPLATE = """\
CANDIDATE PREFERENCE PROFILE:
(Learned from application history — use to calibrate beyond pure skills matching.)
{profile}

"""


# ---------------------------------------------------------------------------
# Resume summary extraction
# ---------------------------------------------------------------------------

def _resume_cache_key(resume_text: str) -> str:
    return resume_text[:64]


def _extract_resume_summary(resume_text: str) -> str:
    """
    Call the LLM once to produce a compact JSON candidate profile from the
    full resume text. Returns a formatted multi-line string on success, or
    a plain-text fallback derived from the raw resume on failure.

    The result is stored in _SUMMARY_CACHE so it is only computed once
    per pipeline run regardless of how many jobs are scored.
    """
    prompt = RESUME_SUMMARY_PROMPT.format(resume=resume_text[:4000])
    try:
        raw = invoke_llm(prompt)
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            data = json.loads(match.group())
            lines = [
                f"Title: {data.get('title', 'Unknown')}",
                f"Years of experience: {data.get('years_exp', '?')}",
                f"Top skills: {', '.join(data.get('top_skills', []))}",
                f"Domains: {', '.join(data.get('domains', []))}",
                f"Certifications: {', '.join(data.get('certs', [])) or 'None listed'}",
                f"Education: {data.get('education', 'Not specified')}",
            ]
            return "\n".join(lines)
    except Exception:
        logger.warning(
            "Resume summary extraction failed — falling back to raw excerpt",
            exc_info=True,
        )
    # Fallback: first 600 chars of raw resume (still better than 3,000)
    return resume_text[:600]


def get_resume_summary(resume_text: str) -> str:
    """
    Return a cached compact candidate profile, extracting it on first call.

    Using a module-level cache means:
    - Scoring 30 jobs costs 1 summary call + 30 score calls.
    - Previously: 30 calls each carrying the full 3,000-char resume.
    - A fresh process (new pipeline run) always rebuilds from the current resume.
    """
    key = _resume_cache_key(resume_text)
    if key not in _SUMMARY_CACHE:
        logger.info("Extracting compact resume summary for scoring…")
        _SUMMARY_CACHE[key] = _extract_resume_summary(resume_text)
        logger.debug("Resume summary:\n%s", _SUMMARY_CACHE[key])
    return _SUMMARY_CACHE[key]


# ---------------------------------------------------------------------------
# Filter keyword lists  (all defined in search_profile.py)
# ---------------------------------------------------------------------------

# Build senior-experience regex patterns from the configurable threshold
_t = SENIOR_EXPERIENCE_YEAR_THRESHOLD
_yr_range = "|".join(str(y) for y in range(_t, _t + 9)) + r"|\d{2}"
_SENIOR_EXPERIENCE_PATTERNS = [
    re.compile(
        rf'\b({_yr_range})\+?\s*years?\s*(of\s*)?(experience|exp)\b'
    ),
    re.compile(rf'\bminimum\s*(of\s*)?({_yr_range})\s*years?\b'),
    re.compile(rf'\bat\s*least\s*({_yr_range})\s*years?\b'),
]


# ---------------------------------------------------------------------------
# Filter functions
# ---------------------------------------------------------------------------

def has_disqualifying_requirements(job: Job) -> bool:
    """Return True if the job requires a security clearance or similar."""
    title_lower = job.get("title", "").lower()
    desc_lower = job.get("description", "").lower()
    return any(
        kw in title_lower or kw in desc_lower
        for kw in EXCLUDE_REQUIREMENT_KEYWORDS
    )


def is_too_senior(job: Job) -> bool:
    """
    Return True if the title or description suggests a senior or
    management role that is beyond the target experience level.
    """
    title_lower = job.get("title", "").lower()
    desc_lower = job.get("description", "").lower()

    if any(kw in title_lower for kw in EXCLUDE_TITLE_KEYWORDS):
        return True

    description_phrases = [
        "subject matter expert", "sme",
        "team lead", "tech lead", "technical lead",
        "team leader", "group lead", "lead role",
    ]
    if any(phrase in desc_lower for phrase in description_phrases):
        return True

    return any(p.search(desc_lower) for p in _SENIOR_EXPERIENCE_PATTERNS)


def is_not_fulltime(job: Job) -> bool:
    """
    Return True if the job appears to be part-time, contract,
    temporary, or otherwise not full-time permanent employment.
    """
    title_lower = job.get("title", "").lower()
    desc_lower = job.get("description", "").lower()

    if any(kw in title_lower for kw in EXCLUDE_EMPLOYMENT_KEYWORDS):
        return True

    return any(
        phrase in desc_lower
        for phrase in EXCLUDE_EMPLOYMENT_DESCRIPTION_PHRASES
    )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_job(
    job: Job,
    candidate_profile: str,
    preference_profile: str | None = None,
) -> Job:
    """
    Ask the LLM to score a job's relevance to the candidate profile.

    Parameters
    ----------
    job:
        The job dict to score.
    candidate_profile:
        Compact candidate profile string produced by get_resume_summary().
        Inject this — do not pass raw resume text here.
    preference_profile:
        Optional learned preference profile from preference_engine.
    """
    desc_excerpt = job.get("description", "")[:1500]

    preference_section = (
        PREFERENCE_SECTION_TEMPLATE.format(profile=preference_profile)
        if preference_profile else ""
    )

    prompt = SCORE_PROMPT.format(
        candidate_profile=candidate_profile,
        preference_section=preference_section,
        title=job.get("title", ""),
        company=job.get("company", ""),
        salary=job.get("salary", "Not specified"),
        description=desc_excerpt,
    )

    try:
        raw = invoke_llm(prompt)
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            result = json.loads(match.group())
            job["score"] = int(result.get("score", 0))
            job["score_reason"] = result.get("reason", "")
        else:
            job["score"] = 0
            job["score_reason"] = "Could not parse score from LLM response"
            logger.warning("Could not parse score for '%s'", job.get("title"))
    except Exception:
        job["score"] = 0
        job["score_reason"] = "Scoring error — see logs"
        logger.exception("Scoring failed for '%s'", job.get("title"))

    return job


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

def filter_and_score_jobs(jobs: list[Job], resume_text: str) -> list[Job]:
    """
    Load cache and preference profile, filter seen / ineligible jobs,
    score the remainder, and persist results to the cache.

    The resume is summarised once via get_resume_summary() before the
    scoring loop — all per-job LLM calls receive the compact profile
    rather than the raw resume text.
    """
    cache: JobCache = load_cache()
    logger.info("[Cache] %s", cache_stats(cache))

    pruned = prune_cache(cache, days=90)
    if pruned:
        logger.info("[Cache] Pruned %d entries older than 90 days", pruned)

    jobs, skipped_seen = filter_seen_jobs(jobs, cache)
    if skipped_seen:
        logger.info("[Cache] Skipped %d already-seen jobs", skipped_seen)

    before_senior = len(jobs)
    jobs = [j for j in jobs if not is_too_senior(j)]
    skipped_senior = before_senior - len(jobs)
    if skipped_senior:
        logger.info("Pre-filtered %d senior/management roles", skipped_senior)

    before_fulltime = len(jobs)
    jobs = [j for j in jobs if not is_not_fulltime(j)]
    skipped_employment = before_fulltime - len(jobs)
    if skipped_employment:
        logger.info(
            "Pre-filtered %d part-time/contract/temporary roles",
            skipped_employment,
        )

    before_clearance = len(jobs)
    jobs = [j for j in jobs if not has_disqualifying_requirements(j)]
    skipped_requirements = before_clearance - len(jobs)
    if skipped_requirements:
        logger.info(
            "Pre-filtered %d jobs with disqualifying requirements",
            skipped_requirements,
        )

    if not jobs:
        logger.info("No new jobs to score after filtering.")
        save_cache(cache)
        return []

    # Extract compact resume summary once for the whole run
    candidate_profile = get_resume_summary(resume_text)

    preference_profile = load_profile()
    if preference_profile:
        logger.info(
            "[Preferences] Profile loaded — adjusting scores accordingly"
        )
    else:
        logger.info(
            "[Preferences] No profile yet — scoring on résumé match only"
        )

    logger.info("Scoring %d new jobs against your résumé…", len(jobs))
    scored: list[dict] = []

    for i, job in enumerate(jobs):
        logger.info(
            "  [%d/%d] Scoring: %s @ %s | %s",
            i + 1, len(jobs),
            job.get("title", "?"), job.get("company", "?"),
            job.get("salary", "Not specified"),
        )
        scored_job = score_job(job, candidate_profile, preference_profile)
        scored.append(scored_job)

        # Persist immediately so progress survives a killed run
        mark_seen(job["url"], scored_job, cache)
        save_cache(cache)

    matched = [j for j in scored if j["score"] >= RELEVANCE_THRESHOLD]
    logger.info(
        "%d jobs matched (score ≥ %d)", len(matched), RELEVANCE_THRESHOLD
    )
    return sorted(matched, key=lambda x: x["score"], reverse=True)
