# search_profile.py
"""
╔══════════════════════════════════════════════════════════════════╗
║               USER SEARCH PROFILE — EDIT THIS FILE              ║
╚══════════════════════════════════════════════════════════════════╝

This is the single place to configure everything about *what* kind
of job you are looking for.  No other file needs to be changed when
switching to a new job search.

Sections
--------
1. SEARCH KEYWORDS       — terms sent to each job board API
2. SOURCE SETTINGS       — per-source tuning (location, salary, etc.)
3. GREENHOUSE COMPANIES  — employers to query directly via Greenhouse
4. SENIORITY FILTERS     — title / description words that disqualify a role
5. EMPLOYMENT TYPE       — exclude part-time, contract, temporary, etc.
6. REQUIREMENT FILTERS   — clearances or other hard deal-breakers
7. PIPELINE TUNING       — scoring threshold, per-source cap, etc.
"""

# ===========================================================================
# 1. SEARCH KEYWORDS
# ===========================================================================
# These keywords are sent to every job board that accepts a search query.
# Each entry triggers a separate API request, so keep the list focused —
# 8-15 entries is a good range.  Use the most specific titles first.

PRIMARY_KEYWORDS: list[str] = [
    # --- Adjust these for your target roles ---

    # Network / infrastructure
    "network engineer",
    "network administrator",
    "systems administrator",
    "sysadmin",

    # Security — entry level
    "junior cybersecurity analyst",
    "cybersecurity analyst",
    "information security analyst",
    "SOC analyst",
    "security operations analyst",

    # IT support crossover
    "IT support specialist",
    "IT administrator",
    "systems support engineer",

    # Broader catches
    "network security engineer",
    "junior security engineer",
    "infrastructure engineer",
]

# ---------------------------------------------------------------------------
# Optional: per-source keyword overrides
#
# If a source works better with a shorter or different keyword set, list
# them here.  Leave a key absent (or set to None) to fall back to
# PRIMARY_KEYWORDS.
# ---------------------------------------------------------------------------

ADZUNA_KEYWORDS: list[str] = [
    "network engineer",
    "network administrator",
    "systems administrator",
    "cybersecurity analyst",
    "SOC analyst",
    "IT administrator",
    "security analyst",
    "infrastructure engineer",
]

REMOTIVE_KEYWORDS: list[str] = [
    "network engineer",
    "network administrator",
    "systems administrator",
    "cybersecurity",
    "security analyst",
    "IT administrator",
]

# Himalayas, Dice, Findwork, USAJobs, and Greenhouse all use PRIMARY_KEYWORDS.
# Add overrides here following the same pattern if needed.


# ===========================================================================
# 2. SOURCE SETTINGS
# ===========================================================================

# --- Jobicy ---
JOBICY_REGION = "usa"       # e.g. "usa", "gb", "ca"
JOBICY_CATEGORY = "engineering"

# --- Adzuna ---
# Country code: us, gb, au, ca, de, fr, in, nl, nz, sg, za, br, mx, pl, ru
ADZUNA_COUNTRY = "us"

# Miles radius around your location (0 = remote / nationwide)
ADZUNA_DISTANCE_MILES = 0

# Sort results by: "relevance", "date", or "salary"
ADZUNA_SORT_BY = "date"

# Only return jobs above this annual salary in local currency (0 = no minimum)
ADZUNA_MIN_SALARY = 80_000


# ===========================================================================
# 3. GREENHOUSE COMPANY BOARDS
# ===========================================================================
# Greenhouse is queried directly — add or remove companies whose boards
# you want to monitor.  The slug is the subdomain of their Greenhouse URL,
# e.g.  https://boards.greenhouse.io/cloudflare  →  "cloudflare"

GREENHOUSE_BOARDS: list[str] = [
    # Security / IT / infrastructure focused companies
    "cloudflare", "paloaltonetworks", "crowdstrike", "sentinelone",
    "tenable", "rapid7", "lacework", "cyberark", "darktrace",
    # Large tech / cloud (heavy IT/infra hiring)
    "gitlab", "hashicorp", "elastic", "mongodb", "datadog",
    "newrelic", "samsara", "grafana", "influxdata",
    # Staffing / consulting (high volume of sysadmin/network postings)
    "leidos", "saic", "boozallenhamiltononline",
]


# ===========================================================================
# 4. SENIORITY FILTERS  (title & description)
# ===========================================================================
# Jobs whose title contains any of these words are automatically skipped.
# Adjust to match your experience level — e.g. remove "lead" if you are
# ready for a lead role, or add "junior" if you only want senior positions.

EXCLUDE_TITLE_KEYWORDS: list[str] = [
    # Seniority
    "senior", "sr.", "sr ", "lead", "principal", "staff",
    "manager", "director", "head of", "vp ", "vice president",
    "chief", "ciso", "cto", "architect",
    # Subject matter expert
    "sme", "subject matter expert",
    # Lead roles
    "team lead", "tech lead", "technical lead", "lead role",
    "team leader", "group lead",
    # Other senior titles
    "supervisor", "superintendent", "foreman",
    "distinguished", "fellow", "expert",
]

# Minimum years of experience that signals a "too senior" role.
# Jobs requiring this many years or more in their description are skipped.
SENIOR_EXPERIENCE_YEAR_THRESHOLD: int = 7


# ===========================================================================
# 5. EMPLOYMENT TYPE FILTERS
# ===========================================================================
# Jobs that appear to be contract, part-time, or temporary are skipped.
# Remove entries you are comfortable with (e.g. remove "contract" if you
# are open to contract-to-hire).

EXCLUDE_EMPLOYMENT_TITLE_KEYWORDS: list[str] = [
    # Part-time
    "part time", "part-time", "parttime",
    "per diem", "casual", "seasonal",
    "temporary", "temp ",
    # Contract
    "contract", "contractor", "contracting",
    "freelance", "freelancer",
    "1099", "c2c", "corp to corp", "corp-to-corp",
    "w2 contract", "contract to hire",
    "contingent",
    # Internship
    "intern", "internship",
    "co-op", "coop",
    "apprentice", "apprenticeship",
    "trainee",
]

EXCLUDE_EMPLOYMENT_DESCRIPTION_PHRASES: list[str] = [
    "this is a contract position",
    "this is a part-time position",
    "part-time hours",
    "contract role",
    "contract opportunity",
    "contract position",
    "temporary position",
    "temp position",
    "contingent position",
    "this is a temporary",
    "hourly contractor",
    "independent contractor",
    "1099 position",
    "c2c only",
    "corp to corp only",
]


# ===========================================================================
# 6. REQUIREMENT FILTERS  (hard deal-breakers)
# ===========================================================================
# Jobs that mention any of these phrases anywhere in the posting are skipped.
# Typical use: security clearances, drug testing, physical requirements, etc.

EXCLUDE_REQUIREMENT_KEYWORDS: list[str] = [
    "security clearance", "clearance required", "active clearance",
    "must have clearance", "clearance eligible",
    "ts/sci", "ts sci", "top secret", "secret clearance",
    "public trust", "dod clearance", "government clearance",
    "clearance preferred", "clearance is required",
    "obtain a clearance", "obtain clearance", "eligible for clearance",
    "w/ clearance", "with clearance", "with security clearance",
]


# ===========================================================================
# 7. PIPELINE TUNING
# ===========================================================================

# Minimum LLM relevance score (1-10) a job must achieve to be saved.
# Lower = more jobs saved; higher = only strong matches.
RELEVANCE_THRESHOLD: int = 6

# Maximum jobs fetched from any single source per pipeline run.
MAX_JOBS_PER_SOURCE: int = 50
