# models.py
"""
Shared type definitions for the job-agent pipeline.

Using TypedDict (rather than dataclasses or attrs) means every existing
dict literal and dict-returning function is automatically compatible —
no construction changes needed anywhere. IDEs and mypy will catch
missing keys and wrong value types on new code going forward.

Import pattern:
    from models import Job, ScoredJob, CompanyResearch, CacheEntry

All fields are marked total=False where the data may be absent at
the point a dict is first created (e.g. score is absent on a freshly
fetched job before scoring). Required fields use a separate base class
so callers that produce a fully-scored job can annotate accordingly.
"""

from typing import Literal, TypedDict


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------

class Job(TypedDict, total=False):
    """
    Represents a job posting as it flows through the pipeline.

    Fields present after fetching:
        source, title, company, location, url, description, salary

    Fields added after scoring:
        score, score_reason

    Fields added after DB insert:
        id, status, date_seen, date_applied, notes,
        cover_letter, cover_letter_status,
        interview_prep
    """
    # --- Fetched from source API ---
    source: str
    title: str
    company: str
    location: str
    url: str                  # always present; used as the unique key
    description: str
    salary: str               # raw string as returned by the source API

    # --- Added by salary_normalizer ---
    salary_min: int | None    # annual USD
    salary_max: int | None    # annual USD
    salary_midpoint: int | None  # annual USD; (min+max)/2 or whichever is present

    # --- Added by job_scorer ---
    score: int                # 1–10
    score_reason: str

    # --- Added by job_validator (disqualified jobs only) ---
    _disqualification_reason: str

    # --- Added after DB upsert ---
    id: int
    status: str
    date_seen: str            # YYYY-MM-DD
    date_applied: str         # YYYY-MM-DD
    notes: str
    cover_letter: str
    cover_letter_status: Literal["pending", "in_progress", "complete", "failed"]
    interview_prep: str


# ---------------------------------------------------------------------------
# Company research
# ---------------------------------------------------------------------------

class CompanyResearch(TypedDict, total=False):
    """
    Structured company research stored in the companies table.
    All fields are optional because research may be partial or in-progress.
    """
    id: int
    name: str
    overview: str
    tech_stack: str
    culture: str
    financial_health: str
    interview_process: str
    recent_news: str
    remote_policy: str
    research_summary: str
    date_researched: str      # YYYY-MM-DD
    research_status: Literal["pending", "in_progress", "complete", "failed"]


# ---------------------------------------------------------------------------
# Job cache
# ---------------------------------------------------------------------------

class CacheEntry(TypedDict, total=False):
    """
    A single entry in the URL-keyed job cache (job_cache.json).
    Mirrors the structure written by job_cache.mark_seen().
    """
    url: str
    title: str
    company: str
    score: int
    date_seen: str            # ISO date string


# The cache file maps URL strings to CacheEntry dicts.
JobCache = dict[str, CacheEntry]


# ---------------------------------------------------------------------------
# Pipeline summary (runner.py internal)
# ---------------------------------------------------------------------------

class PipelineSummary(TypedDict, total=False):
    """
    Parsed output of a completed pipeline run, used to build
    the ntfy notification in runner._build_notification().
    """
    fetched: int
    filtered: int
    scored: int
    matched: int
    saved: int
    errors: int
    top_jobs: list[str]       # brief "Title @ Company (score)" strings
