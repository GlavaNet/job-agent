# config.py
import os
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    """
    Return the value of a required environment variable.

    Raises EnvironmentError at the call site — i.e. when the value is
    first *used* — rather than at import time.  This lets lightweight
    CLI scripts (rejection_detector, email_url_filter, follow_up_checker,
    inbox_listener) import config without crashing because an unrelated
    API key is absent from their environment.
    """
    value = os.getenv(name)
    if not value:
        raise EnvironmentError(
            f"Required environment variable '{name}' is not set. "
            f"Copy .env.example to .env and fill in your values."
        )
    return value


class _LazyEnv:
    """
    Descriptor that defers a _require_env() call until the attribute is
    first accessed on the module-level _secrets singleton.

    Usage in this module:
        class _Secrets:
            ADZUNA_APP_ID = _LazyEnv("ADZUNA_APP_ID")

    Consuming modules see a plain string at runtime:
        from config import ADZUNA_APP_ID   # resolved via module __getattr__
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._attr = f"_cached_{name}"

    def __set_name__(self, owner: type, name: str) -> None:
        self._attr = f"_cached_{name}"

    def __get__(self, obj: object, objtype: type | None = None) -> str:
        if obj is None:
            return self  # type: ignore[return-value]
        cached = obj.__dict__.get(self._attr)
        if cached is None:
            cached = _require_env(self._name)
            obj.__dict__[self._attr] = cached
        return cached


class _Secrets:
    """
    Holds all required secrets as lazy attributes.  Importing config never
    triggers a _require_env() call; the error surfaces only when the
    specific secret is actually read.
    """
    ADZUNA_APP_ID           = _LazyEnv("ADZUNA_APP_ID")
    ADZUNA_API_KEY          = _LazyEnv("ADZUNA_API_KEY")
    NTFY_INBOX_TOPIC        = _LazyEnv("NTFY_INBOX_TOPIC")
    NTFY_NOTIFICATION_TOPIC = _LazyEnv("NTFY_NOTIFICATION_TOPIC")
    BRAVE_SEARCH_API_KEY    = _LazyEnv("BRAVE_SEARCH_API_KEY")


_secrets = _Secrets()


# ---------------------------------------------------------------------------
# Data directory
#
# All of job-agent's runtime state (jobs.db, manual_jobs.txt,
# processed_jobs.txt, seen_jobs.json, preference_profile.json, resume/)
# lives under this directory. Defaults to "." (the current working
# directory) so existing bare-metal installs that have never set this
# variable keep reading/writing exactly where they always have - the
# repo root. Set JOBAGENT_DATA_DIR in .env once you've moved these
# files into a dedicated data/ directory (see README).
# ---------------------------------------------------------------------------
DATA_DIR = os.getenv("JOBAGENT_DATA_DIR", ".")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

RESUME_PATH           = os.path.join(DATA_DIR, "resume", "resume.pdf")
MANUAL_JOBS_FILE      = os.path.join(DATA_DIR, "manual_jobs.txt")
PROCESSED_JOBS_FILE   = os.path.join(DATA_DIR, "processed_jobs.txt")

# RESUME_PATH           = "resume/resume.pdf"
# MANUAL_JOBS_FILE      = "manual_jobs.txt"
# PROCESSED_JOBS_FILE   = "processed_jobs.txt"

# ---------------------------------------------------------------------------
# Pipeline tuning  (values come from search_profile.py)
# ---------------------------------------------------------------------------

from search_profile import RELEVANCE_THRESHOLD, MAX_JOBS_PER_SOURCE  # noqa: E402

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL",    "llama3.1:8b")

# ---------------------------------------------------------------------------
# Job search keywords & source settings  (all defined in search_profile.py)
# ---------------------------------------------------------------------------

from search_profile import (  # noqa: E402
    PRIMARY_KEYWORDS as SEARCH_KEYWORDS,
    ADZUNA_KEYWORDS,
    REMOTIVE_KEYWORDS,
    JOBICY_REGION,
    JOBICY_CATEGORY,
    ADZUNA_COUNTRY,
    ADZUNA_DISTANCE_MILES,
    ADZUNA_SORT_BY,
    ADZUNA_MIN_SALARY,
)

# ---------------------------------------------------------------------------
# API credentials — resolved lazily via module __getattr__ (see bottom of
# file).  EnvironmentError is raised on first *use*, not on import, so CLI
# scripts that don't need a given key won't crash at startup.
#
#   ADZUNA_APP_ID          — required by job_fetcher
#   ADZUNA_API_KEY         — required by job_fetcher
#   NTFY_INBOX_TOPIC       — required by inbox_listener
#   NTFY_NOTIFICATION_TOPIC — required by notifier / runner
#   BRAVE_SEARCH_API_KEY   — required by company_researcher
# ---------------------------------------------------------------------------

# --- Findwork ---
# Free API key from https://findwork.dev — register and add to .env.
# Leave blank to skip this source gracefully.
FINDWORK_API_KEY: str = os.getenv("FINDWORK_API_KEY", "")

# --- USAJobs ---
# Free API key from https://developer.usajobs.gov/apirequest/
# Set both in .env to enable this source; leave blank to skip gracefully.
USAJOBS_API_KEY: str = os.getenv("USAJOBS_API_KEY", "")
USAJOBS_EMAIL: str   = os.getenv("USAJOBS_EMAIL",   "")

# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

WEB_FORM_PORT = 5000
WEB_FORM_HOST = "0.0.0.0"

# Secret used to protect the dashboard with a simple password.
# If not set, the dashboard is accessible without authentication
# (original behaviour — acceptable when bound to localhost only).
# Set this in .env to enable login:  DASHBOARD_SECRET=your-password-here
DASHBOARD_SECRET: str | None = os.getenv("DASHBOARD_SECRET") or None

# ---------------------------------------------------------------------------
# Cover letter generation
# ---------------------------------------------------------------------------

COVER_LETTER_TIMEOUT_MINUTES: int = int(os.getenv("COVER_LETTER_TIMEOUT_MINUTES", "10"))

# Cover letter context window limits (chars fed into the LLM prompt).
# Defaults are generous — the cover letter is on-demand, not per-job in a loop.
# CL_RESUME_CHARS applies to the raw fallback path only; when the job_scorer
# summary cache is warm the compact profile (~300 chars) is used instead.
#
# Override in .env:
#   CL_RESUME_CHARS=3000
#   CL_DESCRIPTION_CHARS=2000
#   CL_RESEARCH_CHARS=2000
CL_RESUME_CHARS: int       = int(os.getenv("CL_RESUME_CHARS",       "3000"))
CL_DESCRIPTION_CHARS: int  = int(os.getenv("CL_DESCRIPTION_CHARS",  "2000"))
CL_RESEARCH_CHARS: int     = int(os.getenv("CL_RESEARCH_CHARS",     "2000"))

# ---------------------------------------------------------------------------
# Candidate context — injected verbatim into the cover letter prompt.
#
# These are facts about the applicant that are always true regardless of the
# specific job, and that the LLM should weave naturally into every letter.
# Keeping them here (rather than hard-coded in cover_letter.py) means you
# can update them — or clear them entirely — without touching source code.
#
# Format: plain bullet points, one per line, starting with "- ".
# Set CL_CANDIDATE_CONTEXT="" in .env to disable entirely.
#
# The default value reflects the original hard-coded prompt content.
# ---------------------------------------------------------------------------
_DEFAULT_CANDIDATE_CONTEXT = """\
IMPORTANT DETAILS TO INCORPORATE:
- The applicant actively follows industry news feeds and listens to \
industry-relevant podcasts to stay current with developments in \
networking, systems, and cybersecurity.
- The applicant maintains an active home lab where they manage various \
personal services and regularly explore new technologies. Work this \
in naturally as evidence of self-driven learning and genuine passion.
- The applicant is working toward a Bachelor's degree in Cybersecurity \
and Information Assurance and has completed most required credits. \
Do NOT present this as a completed degree — frame it positively as \
ongoing education.
- Do not mention or speculate about salary figures in the letter.
- If the job is an entry-level cybersecurity role, emphasise \
transferable skills from network engineering and systems \
administration — these are directly relevant foundations for \
security work."""

CANDIDATE_CONTEXT: str = os.getenv("CL_CANDIDATE_CONTEXT", _DEFAULT_CANDIDATE_CONTEXT)

# ---------------------------------------------------------------------------
# Follow-up reminders
# ---------------------------------------------------------------------------

# Number of days after which an 'applied' job with no status change is
# considered stale and triggers a follow-up reminder notification.
# Override in .env:  FOLLOWUP_DAYS=7
FOLLOWUP_DAYS: int = int(os.getenv("FOLLOWUP_DAYS", "14"))

# ---------------------------------------------------------------------------
# ntfy — private push notification relay
# Topics act as shared secrets: keep them out of source control.
# NTFY_INBOX_TOPIC and NTFY_NOTIFICATION_TOPIC are resolved lazily (below).
# ---------------------------------------------------------------------------

NTFY_BASE_URL = "https://ntfy.sh"

# ---------------------------------------------------------------------------
# Interview prep — context window limits (chars fed into the LLM prompt)
#
# Current defaults are sized conservatively for a local Ollama model with a
# 4096-token context window.  If you migrate to an API-based model (e.g.
# claude-haiku, gpt-4o-mini) bump these up — a good starting point for a
# 32k-token model is RESUME=3000, DESCRIPTION=2000, COMPANY_RESEARCH=2000.
#
# Override in .env:
#   PREP_RESUME_CHARS=3000
#   PREP_DESCRIPTION_CHARS=2000
#   PREP_COMPANY_RESEARCH_CHARS=2000

# ---------------------------------------------------------------------------
# Resume tailoring — context window limits (chars fed into the LLM prompt)
# ---------------------------------------------------------------------------

RESUME_TAILOR_DESCRIPTION_CHARS = 6000   # full JD; ATS keywords live here
RESUME_TAILOR_RESUME_CHARS       = 4000  # same budget as cover letter resume
# ---------------------------------------------------------------------------

PREP_RESUME_CHARS: int           = int(os.getenv("PREP_RESUME_CHARS",           "600"))
PREP_DESCRIPTION_CHARS: int      = int(os.getenv("PREP_DESCRIPTION_CHARS",      "400"))
PREP_COMPANY_RESEARCH_CHARS: int = int(os.getenv("PREP_COMPANY_RESEARCH_CHARS", "300"))


# ---------------------------------------------------------------------------
# Module-level __getattr__ — makes lazy secrets importable as plain names
#
# `from config import ADZUNA_APP_ID` binds the name in the caller's
# namespace at import time, but the _require_env() call — and therefore
# any EnvironmentError — only fires when that line is reached during the
# importing module's own initialisation.  Modules that never import a given
# secret will never trigger its validation.
#
# For the fully-deferred case (no EnvironmentError until the value is
# actually *used* at runtime), access via the module object directly:
#   import config
#   ...
#   value = config.ADZUNA_APP_ID   # error raised here, not at import
# ---------------------------------------------------------------------------

_LAZY: dict[str, object] = {
    "ADZUNA_APP_ID":           lambda: _secrets.ADZUNA_APP_ID,
    "ADZUNA_API_KEY":          lambda: _secrets.ADZUNA_API_KEY,
    "NTFY_INBOX_TOPIC":        lambda: _secrets.NTFY_INBOX_TOPIC,
    "NTFY_NOTIFICATION_TOPIC": lambda: _secrets.NTFY_NOTIFICATION_TOPIC,
    "BRAVE_SEARCH_API_KEY":    lambda: _secrets.BRAVE_SEARCH_API_KEY,
}


def __getattr__(name: str) -> str:
    if name in _LAZY:
        return _LAZY[name]()  # type: ignore[operator]
    raise AttributeError(f"module 'config' has no attribute {name!r}")
