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
4b. DOMAIN FILTERS       — title / description words outside IT/tech entirely
4c. SALARY SENIORITY     — salary figure that implies a senior-level role
5. EMPLOYMENT TYPE       — exclude part-time, contract, temporary, etc.
6. REQUIREMENT FILTERS   — clearances or other hard deal-breakers
8. MUNICIPAL FILTER      — exclude city/county/state jobs outside CA
8b. GENERAL OPPORTUNITY  — exclude talent-pool / no-specific-role postings
9. PIPELINE TUNING       — scoring threshold, per-source cap, etc.
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
# 4b. DOMAIN FILTERS  (title & description)
# ===========================================================================
# Jobs whose title or description strongly indicates a field outside
# IT/tech entirely (retail, food service, healthcare, general clerical
# work, etc.) are automatically skipped before scoring.
#
# This exists because an LLM scorer can be fooled by incidental technical
# keywords in a non-IT posting (e.g. "point-of-sale system" in a retail
# listing, "scheduling software" in an admin-assistant listing) into
# assigning a misleadingly high relevance score. This filter is coarse
# and conservative by design — it is meant to catch obvious non-IT
# postings, not make fine-grained domain judgments. If you see a
# legitimate IT title get caught here, narrow the specific keyword
# rather than removing it outright.
#
# Adjust these lists if your target roles legitimately overlap with any
# of these fields (e.g. health-IT, retail-tech) — the keywords below
# assume no such overlap.

EXCLUDE_DOMAIN_TITLE_KEYWORDS: list[str] = [
    # Retail / food service / hospitality
    "retail associate", "sales associate", "store associate",
    "cashier", "barista", "server", "bartender", "waitstaff",
    "waiter", "waitress", "host/hostess", "line cook", "chef",
    "restaurant", "hospitality",
    # Healthcare (clinical roles — not health-IT)
    "registered nurse", "rn ", " rn,", "nurse practitioner", "nursing",
    "cna", "medical assistant", "phlebotomist", "physical therapist",
    "dental hygienist", "pharmacy technician",
    # General administrative / clerical (non-technical)
    "administrative assistant", "office assistant", "receptionist",
    "data entry clerk", "front desk",
    # Other common false-positive domains
    "delivery driver", "warehouse associate", "forklift operator",
    "teacher", "substitute teacher", "childcare", "daycare",
    "real estate agent", "insurance agent", "loan officer",
]

EXCLUDE_DOMAIN_DESCRIPTION_PHRASES: list[str] = [
    "folding clothes", "operating a cash register", "point-of-sale system",
    "patient care", "clinical setting", "bedside manner",
    "food safety certification", "serving customers",
]


# ===========================================================================
# 4c. SALARY-IMPLIED SENIORITY THRESHOLD
# ===========================================================================
# Jobs with an annual salary at or above this figure are treated as
# senior-level and filtered out via is_too_senior() in job_scorer.py —
# UNLESS the posting's body text specifically calls for education or
# experience consistent with a lower-tier role (e.g. "entry level",
# "no experience required" — see _LOWER_TIER_OVERRIDE_PHRASES in
# job_scorer.py). A high number alone isn't proof of seniority; a
# posting that explicitly says otherwise is taken at its word.

SALARY_SENIOR_THRESHOLD: int = 120_000


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
# 8. MUNICIPAL / GOVERNMENT LOCATION FILTER
# ===========================================================================
# Jobs posted by a city, county, or state municipality are filtered out
# entirely via is_municipal_non_ca() in job_scorer.py — UNLESS the role
# is based in California or the posting explicitly states it is remote.
#
# Matched against company name and job title only (NOT the description),
# to avoid false positives from postings that merely mention a "City of
# X" client or contract in passing (e.g. a private-sector network
# engineering role whose description references municipal clients).

MUNICIPAL_EMPLOYER_KEYWORDS: list[str] = [
    "city of ", "county of ", "town of ", "township of ", "village of ",
    "state of ", "borough of ",
    "department of public works", "sheriff's office", "sheriff office",
    "police department", "fire department", "county government",
    "city government", "municipal government", "municipality",
    "county clerk", "city clerk", "county administration",
]

# Tokens matched against the job's location field for the "within
# California" exemption.
CALIFORNIA_LOCATION_TOKENS: list[str] = [
    "california", " ca ", " ca,", " ca-", ", ca", "ca 9",  # "CA 9xxxx" zip
]


# ===========================================================================
# 8b. GENERAL OPPORTUNITY / TALENT POOL FILTER
# ===========================================================================
# Postings where the hiring entity isn't filling a specific open role but
# is instead collecting résumés for future consideration ("general
# application", "talent pool", "always hiring", etc.) are filtered out
# via is_general_opportunity_posting() in job_scorer.py and hard-scored
# 1/10 in effect — since filter_and_score_jobs() only saves jobs scoring
# at or above RELEVANCE_THRESHOLD, excluding them here achieves the same
# outcome without spending an LLM call on a posting that isn't a real,
# fillable position.
#
# Matched against both title and description — unlike the municipal
# filter, these phrases are specific enough that description-matching
# is low-risk (a real job posting is unlikely to casually mention
# "talent pool" or "general application" the way it might mention an
# unrelated municipal client).

GENERAL_OPPORTUNITY_KEYWORDS: list[str] = [
    "general application", "general interest", "general opportunities",
    "future opportunities", "future openings", "future opening",
    "talent pool", "talent community", "talent network",
    "candidate pool", "resume database", "résumé database",
    "various roles", "various positions", "multiple positions available",
    "we're always hiring", "always hiring", "always looking for talent",
    "no specific position", "not a specific role",
    "express interest", "submit your resume for consideration",
    "join our talent", "stay on file", "keep on file",
    "future consideration", "when a position becomes available",
    "when a role becomes available", "as positions become available",
    "evergreen requisition", "evergreen posting",
]


# ===========================================================================
# 9. PIPELINE TUNING
# ===========================================================================

# Minimum LLM relevance score (1-10) a job must achieve to be saved.
# Lower = more jobs saved; higher = only strong matches.
RELEVANCE_THRESHOLD: int = 6

# Maximum jobs fetched from any single source per pipeline run.
MAX_JOBS_PER_SOURCE: int = 50
