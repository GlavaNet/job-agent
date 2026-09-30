# job_scorer.py
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import RELEVANCE_THRESHOLD
from job_cache import (
    cache_stats, filter_seen_jobs, load_cache,
    mark_seen, prune_cache, save_cache,
)
from json_extract import extract_json_object
from llm_client import invoke_llm
from models import Job, JobCache
from preference_engine import load_profile
from search_profile import (
    EXCLUDE_TITLE_KEYWORDS,
    EXCLUDE_EMPLOYMENT_TITLE_KEYWORDS as EXCLUDE_EMPLOYMENT_KEYWORDS,
    EXCLUDE_EMPLOYMENT_DESCRIPTION_PHRASES,
    EXCLUDE_REQUIREMENT_KEYWORDS,
    EXCLUDE_DOMAIN_TITLE_KEYWORDS,
    EXCLUDE_DOMAIN_DESCRIPTION_PHRASES,
    SENIOR_EXPERIENCE_YEAR_THRESHOLD,
    SALARY_SENIOR_THRESHOLD,
    MUNICIPAL_EMPLOYER_KEYWORDS,
    CALIFORNIA_LOCATION_TOKENS,
    GENERAL_OPPORTUNITY_KEYWORDS,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Parallel scoring
#
# Each job is scored by an independent LLM call.  With a local Ollama model
# these calls are CPU-bound on the inference side but network-bound from
# Python's perspective (HTTP to localhost), so threading gives real
# concurrency without the GIL being a bottleneck.
#
# Default: 3 workers — enough to keep Ollama's queue fed without hammering
# a single-GPU machine.  Raise via SCORER_WORKERS in .env if you are using
# an API-backed model (Anthropic, OpenAI, Groq) where concurrency is cheap.
#
#   SCORER_WORKERS=8   # good starting point for API-backed models
#   SCORER_WORKERS=1   # effectively disables parallelism (serial behaviour)
# ---------------------------------------------------------------------------
_SCORER_WORKERS: int = int(os.getenv("SCORER_WORKERS", "3"))

# ---------------------------------------------------------------------------
# Cache write lock
#
# The job cache (seen_jobs.json) is a shared dict mutated by mark_seen()
# and flushed to disk by save_cache() after each scored job.  With parallel
# workers both operations must be serialised to prevent torn writes.
# The LLM call itself (score_job) holds no lock — it only touches the
# job dict it was handed, which is not shared with any other worker.
# ---------------------------------------------------------------------------
_cache_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Resume summary cache
#
# The raw resume is typically 3,000+ characters and identical for every
# scoring call in a run.  We extract a compact skills summary once per
# process lifetime via get_resume_summary(), then inject that ~200 word
# block instead of the full text.  This cuts per-call prompt size by ~60%.
#
# The summary is keyed by the first 64 chars of the resume so a changed
# file invalidates the cache automatically.
# ---------------------------------------------------------------------------
_SUMMARY_CACHE: dict[str, str] = {}   # key → summary text


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

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
You are a strict job relevance evaluator. Score how well a job posting \
matches a candidate on a scale of 1-10.

CANDIDATE PROFILE:
{candidate_profile}

{preference_section}\
JOB POSTING:
Title: {title}
Company: {company}
Salary: {salary}
Description: {description}

Follow these steps in order:

STEP 1 — Identify the job's actual field. Read ONLY the job posting \
(ignore the candidate for this step) and name the field this role \
belongs to (e.g. "retail", "nursing/healthcare", "administrative \
support", "network engineering", "systems administration", \
"cybersecurity"). Base this strictly on the job's stated title and \
duties, not on any skills or tools mentioned in passing.

STEP 2 — Hard domain check. Is this job's field IT/technology work \
(systems administration, network engineering, cybersecurity, helpdesk/\
technical support, cloud/DevOps, or closely adjacent)?
- If NO — the job is outside IT entirely (retail, food service, \
healthcare/nursing, general administrative/clerical work, sales, \
hospitality, manual labor, education, etc.) — the score MUST be 1 or 2, \
regardless of any superficial keyword overlap (e.g. "computer skills," \
"scheduling software," "point-of-sale system" do not count as IT work). \
Skip to STEP 4.
- If YES — continue to STEP 3.

STEP 3 — Skills and domain match. Compare the job's actual requirements \
against the candidate profile. Weight domain alignment heavily — e.g. a \
Network Engineer profile should score highly on network/security roles \
and low on unrelated IT support.
- If a preference profile is provided, adjust up or down based on \
demonstrated patterns.
- 10 = near-perfect skills + domain match within IT; 3 = same broad \
field (IT) but weak overlap on specific skills; lower only if genuinely \
unrelated even within IT.

STEP 4 — Deprioritized role types. After scoring per STEP 3, apply these \
adjustments if they weren't already reflected:
- If the job title contains "AI" or is clearly an AI/ML-focused role \
(e.g. "AI Engineer", "Machine Learning Engineer", "GenAI Specialist"), \
score it lower than you otherwise would — these roles are a lower \
priority for this candidate regardless of skill overlap.
- If the role is a cybersecurity position described as Tier 2, Tier 3, \
Level II/III, or otherwise above entry-level (e.g. "SOC Lead", "Threat \
Hunter", "Detection Engineer", "Security Engineer II"), score it lower — \
the candidate does not yet have the Tier 1 cybersecurity experience \
these roles require. Entry-level / Tier 1 SOC and cybersecurity analyst \
roles are NOT affected by this and should be scored normally on merit.

STEP 5 — Write the reason. The reason must describe the JOB (what it \
actually is and why it does or doesn't fit), not restate the \
candidate's résumé. A reason that only lists the candidate's skills \
without saying whether the job needs them is invalid.

Respond with ONLY a JSON object in this exact format, nothing else:
{{"score": <integer 1-10>, "reason": "<one sentence about the job's fit>"}}

The "reason" value MUST start and end with a double-quote character, \
exactly as shown above — write "reason": "Good fit because...", never \
"reason": Good fit because... with no opening quote. A missing quote \
breaks JSON parsing just as badly as an extra one.

Formatting rule for "reason": do not use double-quote marks anywhere in \
the sentence, even to emphasise a word or quote the job posting (e.g. \
write hands-on, not "hands-on"). A stray double-quote breaks JSON \
parsing and discards the whole score.
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
    full resume text.  Returns a formatted multi-line string on success, or
    a plain-text fallback derived from the raw resume on failure.

    The result is stored in _SUMMARY_CACHE so it is only computed once
    per pipeline run regardless of how many jobs are scored.
    """
    prompt = RESUME_SUMMARY_PROMPT.format(resume=resume_text[:4000])
    try:
        # Explicit max_tokens: this response includes several list fields
        # (top_skills, domains, certs) and can run past the default
        # LLM_MAX_TOKENS budget on verbose models, especially non-thinking
        # local models that write more expansively than API models tuned
        # for concise structured output.
        raw = invoke_llm(prompt, max_tokens=1500)
        data = extract_json_object(raw)
        if data:
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
    desc_lower  = job.get("description", "").lower()
    return any(
        kw in title_lower or kw in desc_lower
        for kw in EXCLUDE_REQUIREMENT_KEYWORDS
    )


# Phrases that, if present in the job description, override the
# salary-implies-senior rule — i.e. the posting itself says this is a
# lower-tier / entry-level role despite the high listed salary.
_LOWER_TIER_OVERRIDE_PHRASES: list[str] = [
    "entry level", "entry-level", "no experience required",
    "no experience necessary", "0-2 years", "0-1 years",
    "recent graduate", "new grad", "new graduate",
    "junior level", "junior-level", "associate level",
    "associate-level", "1-2 years of experience",
    "1-3 years of experience",
]

# Title-level signals that a role is lower-tier, checked separately from
# the description-phrase list above. A posting titled "Junior Network
# Administrator" or "...Analyst I" is unlikely to repeat "junior" or
# "entry level" anywhere in its body text, so relying on description
# phrases alone missed these and let a high salary figure wrongly
# override an otherwise-obvious junior title. Word-boundary regexes are
# used (rather than plain substring checks) so, e.g., the trailing " I"
# level-suffix pattern doesn't match "I" inside ordinary words.
_LOWER_TIER_TITLE_PATTERNS: list[re.Pattern] = [
    re.compile(r'\bjunior\b'),
    re.compile(r'\bjr\.?\b'),
    re.compile(r'\bentry[\s-]?level\b'),
    re.compile(r'\bassociate\b'),
    re.compile(r'\bintern(ship)?\b'),
    re.compile(r'\btrainee\b'),
    re.compile(r'\bapprentice\b'),
    # Level-number suffixes: "...Analyst I", "...Engineer 1", "Level 1"
    re.compile(r'\bi\b\s*$'),           # trailing standalone "I"
    re.compile(r'\b1\b\s*$'),           # trailing standalone "1"
    re.compile(r'\blevel\s*1\b'),
    re.compile(r'\btier\s*1\b'),
]


def _title_implies_lower_tier(title_lower: str) -> bool:
    """
    Return True if the job title itself carries a clear lower-tier
    signal (junior/entry/associate/intern/trainee, or a level-1/"I"
    suffix). Used as a second, independent override path in
    _salary_implies_senior() alongside description-phrase matching,
    since a title-level signal is often present when no equivalent
    phrase appears in the body text.
    """
    return any(p.search(title_lower) for p in _LOWER_TIER_TITLE_PATTERNS)


def _salary_implies_senior(job: Job, title_lower: str, desc_lower: str) -> bool:
    """
    Return True if the job's salary is at or above SALARY_SENIOR_THRESHOLD
    and neither the title nor the description contains language
    indicating a lower-tier role.

    is_too_senior() runs during pre-filtering in filter_and_score_jobs(),
    before salary_normalizer.enrich_job_salary() has had a chance to run
    (that happens later, inside upsert_job() at save time) — so
    job["salary_max"] / job["salary_midpoint"] are not populated yet at
    this point in the pipeline. We parse the raw job["salary"] string
    directly with salary_normalizer.parse_salary() instead. If those
    fields DO happen to already be present (e.g. this function is reused
    on a re-scored job pulled from the DB), prefer those and skip the
    re-parse.
    """
    salary_figure = (
        job.get("salary_max")
        or job.get("salary_midpoint")
        or job.get("salary_min")
    )

    if not salary_figure:
        from salary_normalizer import parse_salary
        info = parse_salary(job.get("salary", "") or "")
        salary_figure = info.salary_max or info.midpoint or info.salary_min

    if not salary_figure or salary_figure < SALARY_SENIOR_THRESHOLD:
        return False

    if _title_implies_lower_tier(title_lower):
        return False

    if any(phrase in desc_lower for phrase in _LOWER_TIER_OVERRIDE_PHRASES):
        return False

    return True


def is_too_senior(job: Job) -> bool:
    """
    Return True if the title or description suggests a senior or
    management role that is beyond the target experience level.

    Also treats any job with a salary at or above SALARY_SENIOR_THRESHOLD
    as senior-level, UNLESS the description body specifically calls out
    education/experience language consistent with a lower-tier role
    (see _LOWER_TIER_OVERRIDE_PHRASES above) — high pay with an explicit
    "entry level" or "no experience required" framing is not excluded.
    """
    title_lower = job.get("title", "").lower()
    desc_lower  = job.get("description", "").lower()

    if any(kw in title_lower for kw in EXCLUDE_TITLE_KEYWORDS):
        return True

    description_phrases = [
        "subject matter expert", "sme",
        "team lead", "tech lead", "technical lead",
        "team leader", "group lead", "lead role",
    ]
    if any(phrase in desc_lower for phrase in description_phrases):
        return True

    if any(p.search(desc_lower) for p in _SENIOR_EXPERIENCE_PATTERNS):
        return True

    if _salary_implies_senior(job, title_lower, desc_lower):
        return True

    return False


def is_not_fulltime(job: Job) -> bool:
    """
    Return True if the job appears to be part-time, contract,
    temporary, or otherwise not full-time permanent employment.
    """
    title_lower = job.get("title", "").lower()
    desc_lower  = job.get("description", "").lower()

    if any(kw in title_lower for kw in EXCLUDE_EMPLOYMENT_KEYWORDS):
        return True

    return any(
        phrase in desc_lower
        for phrase in EXCLUDE_EMPLOYMENT_DESCRIPTION_PHRASES
    )


# ---------------------------------------------------------------------------
# Domain gate
#
# Added after observing that a hybrid-thinking local model (Qwen3.6 via
# llama.cpp) would correctly reason in its "score reason" text that a job
# was outside IT entirely, then assign it a mid-range score anyway —
# ignoring its own stated hard-constraint instruction. Three rounds of
# prompt tightening (explicit hard-cap rule, then a worked few-shot
# example) did not fix this; the few-shot version actually triggered a
# repetition/looping failure. Rather than keep tuning the prompt to
# compensate for an unreliable model, non-IT titles/descriptions are now
# rejected here, before the job ever reaches the LLM — this works
# regardless of which model is configured as the scorer backend.
#
# Keyword lists live in search_profile.py (section 4b) alongside the
# other exclusion lists.
# ---------------------------------------------------------------------------

def is_wrong_domain(job: Job) -> bool:
    """
    Return True if the job's title or description strongly indicates a
    field outside IT/tech entirely (retail, food service, healthcare,
    general clerical work, etc.), regardless of any incidental technical
    keywords ("point-of-sale system", "scheduling software") that might
    otherwise confuse a relevance-scoring LLM.

    This is a coarse, conservative filter — it is meant to catch obvious
    non-IT postings before they reach the LLM, not to make fine-grained
    domain judgments. A title/phrase match here means "definitely not
    IT," not "definitely not a good fit" — legitimate IT titles should
    never collide with this list, but if you see a false positive in
    the logs, tighten the specific keyword rather than removing the
    filter's intent.
    """
    title_lower = job.get("title", "").lower()
    desc_lower  = job.get("description", "").lower()

    if any(kw in title_lower for kw in EXCLUDE_DOMAIN_TITLE_KEYWORDS):
        return True

    return any(
        phrase in desc_lower
        for phrase in EXCLUDE_DOMAIN_DESCRIPTION_PHRASES
    )


def is_municipal_non_ca(job: Job) -> bool:
    """
    Return True if the job appears to be posted by a city, county, or
    state municipality AND is not based in California AND the posting
    does not explicitly state it is remote.

    Matches only against company name and title — NOT the description —
    to avoid false positives from postings that merely mention a "City
    of X" client or contract in passing (e.g. a private-sector network
    engineering role whose description references municipal clients).
    """
    title_lower    = job.get("title", "").lower()
    company_lower  = job.get("company", "").lower()
    location_lower = (job.get("location", "") or "").lower()

    is_municipal = any(
        kw in company_lower or kw in title_lower
        for kw in MUNICIPAL_EMPLOYER_KEYWORDS
    )
    if not is_municipal:
        return False

    # Exempt if explicitly remote
    if "remote" in location_lower:
        return False

    # Exempt if based in California
    if any(tok in location_lower for tok in CALIFORNIA_LOCATION_TOKENS):
        return False

    return True


def is_general_opportunity_posting(job: Job) -> bool:
    """
    Return True if the posting is not for a specific open role but is
    instead a "general interest" / talent-pool listing — the hiring
    entity has no currently open position and is just collecting
    candidates for future consideration.

    Checked against title and description. Keyword/phrase match only —
    no LLM judgment involved, since these postings are identifiable by
    a small, fairly stable set of stock phrases ("general application",
    "talent pool", "future opportunities", etc.) and catching them here
    avoids spending an LLM call on a posting that was never a decision
    point in the first place.
    """
    title_lower = job.get("title", "").lower()
    desc_lower  = job.get("description", "").lower()

    return any(
        kw in title_lower or kw in desc_lower
        for kw in GENERAL_OPPORTUNITY_KEYWORDS
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
        The job dict to score.  This function mutates it in-place (adds
        ``score`` and ``score_reason``) and also returns it for convenience.
        Each worker receives a distinct job dict so there is no shared state.
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
        # Explicit max_tokens: SCORE_PROMPT now walks the model through
        # five sequential steps (identify field → domain gate → skills
        # match → AI-title/cyber-tier deprioritization → write reason)
        # before it can emit JSON. 400 was sized for the original
        # shorter prompt and was cutting responses off mid-reasoning —
        # producing truncated/garbled `reason` text and occasional
        # unparseable JSON. 750 gives the model room to actually finish
        # all five steps. This is separate from the thinking-mode issue
        # (see LLM_DISABLE_THINKING in llm_client.py) — it's headroom
        # for the *visible* answer itself, not chain-of-thought.
        raw  = invoke_llm(prompt, max_tokens=750)
        data = extract_json_object(raw)
        if data:
            score  = int(data.get("score", 0))
            # 0 is reserved to mean "scoring failed" throughout the
            # pipeline (see except block below and mark_seen/cache
            # consumers). If the LLM ignores the 1-10 instruction and
            # returns 0 for a genuinely bad match, clamp it up so 0
            # stays an exclusive failure signal.
            job["score"]        = max(score, 1)
            job["score_reason"] = data.get("reason", "")
        else:
            job["score"]        = 0
            job["score_reason"] = "Could not parse score from LLM response"
            logger.warning("Could not parse score for '%s'", job.get("title"))
    except Exception:
        job["score"]        = 0
        job["score_reason"] = "Scoring error — see logs"
        logger.exception("Scoring failed for '%s'", job.get("title"))

    return job


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

def filter_and_score_jobs(jobs: list[Job], resume_text: str) -> list[Job]:
    """
    Load cache and preference profile, filter seen / ineligible jobs,
    score the remainder in parallel, and persist results to the cache.

    The resume is summarised once via get_resume_summary() before the
    scoring loop — all per-job LLM calls receive the compact profile
    rather than the raw resume text.

    Parallelism is controlled by SCORER_WORKERS (default 3).  Each worker
    calls score_job() independently; cache writes are serialised via
    _cache_lock so the JSON file is never torn.
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

    before_domain = len(jobs)
    jobs = [j for j in jobs if not is_wrong_domain(j)]
    skipped_domain = before_domain - len(jobs)
    if skipped_domain:
        logger.info(
            "Pre-filtered %d jobs outside the target IT/tech domain",
            skipped_domain,
        )

    before_fulltime = len(jobs)
    jobs = [j for j in jobs if not is_not_fulltime(j)]
    skipped_employment = before_fulltime - len(jobs)
    if skipped_employment:
        logger.info(
            "Pre-filtered %d part-time/contract/temporary roles",
            skipped_employment,
        )

    before_municipal = len(jobs)
    jobs = [j for j in jobs if not is_municipal_non_ca(j)]
    skipped_municipal = before_municipal - len(jobs)
    if skipped_municipal:
        logger.info(
            "Pre-filtered %d municipal/government jobs outside CA "
            "(non-remote)",
            skipped_municipal,
        )

    before_general = len(jobs)
    jobs = [j for j in jobs if not is_general_opportunity_posting(j)]
    skipped_general = before_general - len(jobs)
    if skipped_general:
        logger.info(
            "Pre-filtered %d general-opportunity/talent-pool postings "
            "(no specific open role)",
            skipped_general,
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

    # Extract compact resume summary once — shared read-only across all workers
    candidate_profile = get_resume_summary(resume_text)

    preference_profile = load_profile()
    if preference_profile:
        logger.info("[Preferences] Profile loaded — adjusting scores accordingly")
    else:
        logger.info("[Preferences] No profile yet — scoring on résumé match only")

    total = len(jobs)
    logger.info(
        "Scoring %d new jobs against your résumé… (%d worker%s)",
        total, _SCORER_WORKERS, "s" if _SCORER_WORKERS != 1 else "",
    )

    # completed_count is only read/written under _cache_lock, so no
    # separate atomic is needed.
    completed_count = 0
    scored: list[Job] = []

    def _score_and_cache(job: Job) -> Job:
        """Score one job then serialise the cache update."""
        result = score_job(job, candidate_profile, preference_profile)

        nonlocal completed_count
        with _cache_lock:
            completed_count += 1
            n = completed_count
            mark_seen(job["url"], result, cache)
            save_cache(cache)

        logger.info(
            "  [%d/%d] [%d/10] %s @ %s | %s",
            n, total,
            result["score"],
            result.get("title", "?"),
            result.get("company", "?"),
            result.get("salary", "Not specified"),
        )
        return result

    with ThreadPoolExecutor(
        max_workers=_SCORER_WORKERS,
        thread_name_prefix="scorer",
    ) as pool:
        futures = {pool.submit(_score_and_cache, job): job for job in jobs}
        for future in as_completed(futures):
            try:
                scored.append(future.result())
            except Exception:
                job = futures[future]
                logger.exception(
                    "Unhandled error scoring '%s' @ '%s'",
                    job.get("title", "?"), job.get("company", "?"),
                )
                # Ensure a failed job still gets a score so it flows
                # through the pipeline and can be filtered below
                job.setdefault("score", 0)
                job.setdefault("score_reason", "Unhandled scoring error")
                scored.append(job)

    matched = [j for j in scored if j["score"] >= RELEVANCE_THRESHOLD]
    logger.info(
        "%d jobs matched (score ≥ %d)", len(matched), RELEVANCE_THRESHOLD
    )
    return sorted(matched, key=lambda x: x["score"], reverse=True)
