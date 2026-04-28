# job_fetcher.py
import asyncio
import logging
import re
import socket
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

import requests

from config import (
    ADZUNA_APP_ID,
    ADZUNA_API_KEY,
    ADZUNA_COUNTRY,
    ADZUNA_DISTANCE_MILES,
    ADZUNA_KEYWORDS,
    ADZUNA_MIN_SALARY,
    ADZUNA_SORT_BY,
    FINDWORK_API_KEY,
    JOBICY_CATEGORY,
    JOBICY_REGION,
    MAX_JOBS_PER_SOURCE,
    REMOTIVE_KEYWORDS,
    SEARCH_KEYWORDS,
)
from models import Job

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connectivity pre-flight
# ---------------------------------------------------------------------------

_CONNECTIVITY_PROBE_HOST = "8.8.8.8"
_CONNECTIVITY_PROBE_PORT = 53
_CONNECTIVITY_PROBE_TIMEOUT = 3


def check_internet() -> bool:
    """Return True if the host can reach the internet (DNS probe via TCP)."""
    try:
        socket.setdefaulttimeout(_CONNECTIVITY_PROBE_TIMEOUT)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.connect((_CONNECTIVITY_PROBE_HOST, _CONNECTIVITY_PROBE_PORT))
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Structured fetch result / error reporting
# ---------------------------------------------------------------------------

class FetchStatus(str, Enum):
    OK          = "ok"
    NO_NETWORK  = "no_network"   # connectivity lost before the request
    TIMEOUT     = "timeout"      # request timed out
    HTTP_ERROR  = "http_error"   # non-2xx response
    AUTH_ERROR  = "auth_error"   # 401 / 403
    PARSE_ERROR = "parse_error"  # unexpected response shape / JSON decode fail
    EMPTY       = "empty"        # 200 OK but zero jobs returned
    ERROR       = "error"        # any other exception


@dataclass
class FetchResult:
    source: str
    status: FetchStatus
    jobs: list[Job] = field(default_factory=list)
    # Human-readable detail logged and surfaced in the pipeline summary
    detail: str = ""
    # Raw HTTP status code when applicable
    http_status: int | None = None

    # Convenience -----------------------------------------------------------

    @property
    def ok(self) -> bool:
        return self.status == FetchStatus.OK

    @property
    def warning(self) -> bool:
        """True for degraded-but-non-fatal results (empty, partial)."""
        return self.status == FetchStatus.EMPTY

    def log(self) -> None:
        """Emit an appropriate log line for this result."""
        tag = f"[{self.source}]"
        if self.ok:
            logger.info("%s %d job(s) fetched", tag, len(self.jobs))
        elif self.warning:
            logger.warning("%s %s", tag, self.detail or "No jobs returned")
        else:
            logger.error(
                "%s Fetch failed — status=%s%s %s",
                tag,
                self.status.value,
                f" (HTTP {self.http_status})" if self.http_status else "",
                self.detail,
            )


# ---------------------------------------------------------------------------
# Internal helpers shared across fetchers
# ---------------------------------------------------------------------------

_HTML_ENTITY_MAP = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&nbsp;": " ",
    "&#39;": "'",
    "&quot;": '"',
}


def strip_html(text: str) -> str:
    """Remove HTML tags and decode common entities."""
    text = re.sub(r'<[^>]+>', ' ', text)
    for entity, char in _HTML_ENTITY_MAP.items():
        text = text.replace(entity, char)
    return re.sub(r'\s+', ' ', text).strip()


def keyword_match(text: str, keywords: list[str]) -> bool:
    """
    Return True if any keyword phrase, or any individual word longer than
    4 characters from a multi-word phrase, appears in text.
    The word-level fallback prevents long phrases from never matching.
    """
    text_lower = text.lower()
    for keyword in keywords:
        if keyword.lower() in text_lower:
            return True
        for word in keyword.lower().split():
            if len(word) > 4 and word in text_lower:
                return True
    return False


def _safe_get(
    url: str,
    source: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = 15,
) -> tuple[requests.Response | None, FetchResult | None]:
    """
    Perform a GET request and translate network/HTTP exceptions into a
    FetchResult so every caller gets uniform error handling for free.

    Returns (response, None) on success or (None, FetchResult) on failure.
    """
    try:
        resp = requests.get(
            url,
            params=params,
            headers={
                "User-Agent": "job-agent/2.0 (automated job search tool)",
                **(headers or {}),
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp, None

    except requests.exceptions.ConnectionError as exc:
        # Could be DNS failure, refused connection, or mid-request drop.
        detail = f"Connection failed: {exc}"
        # Distinguish a full loss of internet from a per-host refusal.
        if not check_internet():
            return None, FetchResult(source, FetchStatus.NO_NETWORK, detail="No internet connectivity")
        return None, FetchResult(source, FetchStatus.ERROR, detail=detail)

    except requests.exceptions.Timeout:
        return None, FetchResult(
            source, FetchStatus.TIMEOUT,
            detail=f"Request timed out after {timeout}s",
        )

    except requests.exceptions.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else None
        if code in (401, 403):
            return None, FetchResult(
                source, FetchStatus.AUTH_ERROR, http_status=code,
                detail="Authentication failed — check credentials in .env",
            )
        return None, FetchResult(
            source, FetchStatus.HTTP_ERROR, http_status=code,
            detail=str(exc),
        )

    except Exception as exc:
        return None, FetchResult(source, FetchStatus.ERROR, detail=str(exc))


def _parse_json(resp: requests.Response, source: str) -> tuple[dict | list | None, FetchResult | None]:
    """Parse JSON from a response; return (data, None) or (None, FetchResult)."""
    try:
        return resp.json(), None
    except Exception as exc:
        return None, FetchResult(
            source, FetchStatus.PARSE_ERROR,
            detail=f"JSON decode failed: {exc}",
        )


# ---------------------------------------------------------------------------
# Source fetchers  (each returns FetchResult)
# ---------------------------------------------------------------------------

def fetch_remotive_jobs() -> FetchResult:
    """Fetch remote tech jobs from Remotive's free public API."""
    source = "Remotive"
    resp, err = _safe_get(
        "https://remotive.com/api/remote-jobs",
        source,
        params={"limit": MAX_JOBS_PER_SOURCE * max(len(REMOTIVE_KEYWORDS), 1)},
    )
    if err:
        return err

    data, err = _parse_json(resp, source)
    if err:
        return err

    if not isinstance(data, dict) or "jobs" not in data:
        return FetchResult(source, FetchStatus.PARSE_ERROR, detail="Unexpected response shape — missing 'jobs' key")

    jobs: list[Job] = []
    for job in data["jobs"]:
        if not isinstance(job, dict):
            continue
        title = job.get("title", "")
        description = strip_html(job.get("description", ""))
        if keyword_match(title + " " + description, REMOTIVE_KEYWORDS):
            jobs.append({
                "source": source,
                "title": title,
                "company": job.get("company_name", "Unknown"),
                "location": "Remote — " + job.get("candidate_required_location", "Worldwide"),
                "url": job.get("url", ""),
                "description": description[:1000],
                "salary": job.get("salary") or "Not specified",
            })

    if not jobs:
        return FetchResult(source, FetchStatus.EMPTY, detail="API returned no matching jobs")

    return FetchResult(source, FetchStatus.OK, jobs=jobs[:MAX_JOBS_PER_SOURCE])


def fetch_remoteok_jobs() -> FetchResult:
    """Fetch remote tech jobs from RemoteOK's public API."""
    source = "RemoteOK"
    resp, err = _safe_get("https://remoteok.com/api", source)
    if err:
        return err

    data, err = _parse_json(resp, source)
    if err:
        return err

    if not isinstance(data, list):
        return FetchResult(source, FetchStatus.PARSE_ERROR, detail="Expected a JSON array at root")

    listings = [item for item in data if isinstance(item, dict) and "position" in item]
    if not listings:
        return FetchResult(source, FetchStatus.EMPTY, detail="No job listings found in response")

    jobs: list[Job] = [
        {
            "source": source,
            "title": job.get("position", ""),
            "company": job.get("company", "Unknown"),
            "location": "Remote",
            "url": job.get("url", ""),
            "description": strip_html(job.get("description", ""))[:1000],
            "salary": "Not specified",
        }
        for job in listings[:MAX_JOBS_PER_SOURCE]
    ]
    return FetchResult(source, FetchStatus.OK, jobs=jobs)


def fetch_muse_jobs() -> FetchResult:
    """Fetch tech jobs from The Muse's public API."""
    source = "The Muse"
    jobs: list[Job] = []
    last_err: FetchResult | None = None

    for keyword in SEARCH_KEYWORDS:
        resp, err = _safe_get(
            "https://www.themuse.com/api/public/jobs",
            source,
            params={"descending": "true", "page": 0},
        )
        if err:
            last_err = err
            # Stop immediately on connectivity / auth failures
            if err.status in (FetchStatus.NO_NETWORK, FetchStatus.AUTH_ERROR):
                return err
            continue

        data, err = _parse_json(resp, source)
        if err:
            last_err = err
            continue

        if not isinstance(data, dict):
            last_err = FetchResult(source, FetchStatus.PARSE_ERROR, detail="Unexpected root type")
            continue

        for job in data.get("results", [])[:MAX_JOBS_PER_SOURCE]:
            if not isinstance(job, dict):
                continue

            title = job.get("name", "")
            contents = job.get("contents", "")
            if isinstance(contents, str):
                description = strip_html(contents)
            elif isinstance(contents, list):
                description = strip_html(" ".join(
                    c.get("body", "") for c in contents if isinstance(c, dict)
                ))
            else:
                description = ""

            if not keyword_match(title + " " + description, [keyword]):
                continue

            locations = job.get("locations", [])
            location = (
                locations[0].get("name", "Not specified")
                if locations and isinstance(locations[0], dict)
                else "Not specified"
            )
            company_data = job.get("company", {})
            company = company_data.get("name", "Unknown") if isinstance(company_data, dict) else "Unknown"
            refs = job.get("refs", {})
            job_url = refs.get("landing_page", "") if isinstance(refs, dict) else ""

            jobs.append({
                "source": source,
                "title": title,
                "company": company,
                "location": location,
                "url": job_url,
                "description": description[:1000],
                "salary": "Not specified",
            })

    if not jobs:
        status = last_err.status if last_err else FetchStatus.EMPTY
        detail = last_err.detail if last_err else "No matching jobs found"
        return FetchResult(source, status, detail=detail)

    return FetchResult(source, FetchStatus.OK, jobs=jobs)


def fetch_jobicy_jobs() -> FetchResult:
    """Fetch remote jobs from Jobicy's free public API."""
    source = "Jobicy"
    params: dict = {"count": MAX_JOBS_PER_SOURCE}
    if JOBICY_REGION:
        params["geo"] = JOBICY_REGION
    if JOBICY_CATEGORY:
        params["industry"] = JOBICY_CATEGORY

    resp, err = _safe_get("https://jobicy.com/api/v2/remote-jobs", source, params=params)
    if err:
        return err

    data, err = _parse_json(resp, source)
    if err:
        return err

    if not isinstance(data, dict) or "jobs" not in data:
        return FetchResult(source, FetchStatus.PARSE_ERROR, detail="Missing 'jobs' key in response")

    jobs: list[Job] = []
    for job in data["jobs"]:
        if not isinstance(job, dict):
            continue
        title = job.get("jobTitle", "")
        description = strip_html(job.get("jobDescription", ""))
        if keyword_match(title + " " + description, SEARCH_KEYWORDS):
            jobs.append({
                "source": source,
                "title": title,
                "company": job.get("companyName", "Unknown"),
                "location": "Remote — " + job.get("jobGeo", "Worldwide"),
                "url": job.get("url", ""),
                "description": description[:1000],
                "salary": "Not specified",
            })

    if not jobs:
        return FetchResult(source, FetchStatus.EMPTY, detail="No matching jobs in response")

    return FetchResult(source, FetchStatus.OK, jobs=jobs)


def fetch_adzuna_jobs() -> FetchResult:
    """Fetch US jobs from Adzuna's API for each configured keyword."""
    source = "Adzuna"
    jobs: list[Job] = []
    last_err: FetchResult | None = None

    for keyword in ADZUNA_KEYWORDS:
        params: dict = {
            "app_id": ADZUNA_APP_ID,
            "app_key": ADZUNA_API_KEY,
            "results_per_page": MAX_JOBS_PER_SOURCE,
            "what": keyword,
            "sort_by": ADZUNA_SORT_BY,
            "content-type": "application/json",
        }
        if ADZUNA_DISTANCE_MILES:
            params["distance"] = ADZUNA_DISTANCE_MILES
        if ADZUNA_MIN_SALARY:
            params["salary_min"] = ADZUNA_MIN_SALARY

        resp, err = _safe_get(
            f"https://api.adzuna.com/v1/api/jobs/{ADZUNA_COUNTRY}/search/1",
            source,
            params=params,
        )
        if err:
            last_err = err
            if err.status in (FetchStatus.NO_NETWORK, FetchStatus.AUTH_ERROR):
                return err
            continue

        data, err = _parse_json(resp, source)
        if err:
            last_err = err
            continue

        if not isinstance(data, dict):
            last_err = FetchResult(source, FetchStatus.PARSE_ERROR, detail="Unexpected root type")
            continue

        for job in data.get("results", []):
            if not isinstance(job, dict):
                continue
            location_data = job.get("location", {})
            area = location_data.get("area", [])
            if len(area) >= 2:
                location = f"{area[-1]}, {area[1]}"
            elif len(area) == 1:
                location = location_data.get("display_name", "US")
            else:
                location = "Not specified"

            salary_min = job.get("salary_min")
            salary_max = job.get("salary_max")
            if salary_min and salary_max:
                salary = f"${int(salary_min):,} – ${int(salary_max):,}"
            elif salary_min:
                salary = f"${int(salary_min):,}+"
            else:
                salary = "Not specified"

            jobs.append({
                "source": source,
                "title": job.get("title", ""),
                "company": job.get("company", {}).get("display_name", "Unknown"),
                "location": location,
                "url": job.get("redirect_url", ""),
                "description": job.get("description", "")[:1000],
                "salary": salary,
            })

    if not jobs:
        status = last_err.status if last_err else FetchStatus.EMPTY
        detail = last_err.detail if last_err else "No jobs returned across all keywords"
        return FetchResult(source, status, detail=detail)

    return FetchResult(source, FetchStatus.OK, jobs=jobs)


# ---------------------------------------------------------------------------
# NEW SOURCE: Findwork
# ---------------------------------------------------------------------------

def fetch_findwork_jobs() -> FetchResult:
    """
    Fetch US tech jobs from Findwork's public API.
    Requires a free API key from findwork.dev — set FINDWORK_API_KEY in .env.
    """
    source = "Findwork"

    if not FINDWORK_API_KEY:
        return FetchResult(
            source, FetchStatus.AUTH_ERROR,
            detail="FINDWORK_API_KEY not set in .env — register free at findwork.dev",
        )

    jobs: list[Job] = []
    last_err: FetchResult | None = None

    for keyword in SEARCH_KEYWORDS[:6]:  # limit keyword iterations for this source
        resp, err = _safe_get(
            "https://findwork.dev/api/jobs/",
            source,
            params={
                "search": keyword,
                "remote": "true",
                # Note: Findwork does not accept ISO country codes in the location
                # param. Omitting it returns global results; remote=true is the
                # effective US filter since the site is US-centric.
            },
            headers={"Authorization": f"Token {FINDWORK_API_KEY}"},
        )
        if err:
            last_err = err
            if err.status in (FetchStatus.NO_NETWORK, FetchStatus.AUTH_ERROR):
                return err
            continue

        data, err = _parse_json(resp, source)
        if err:
            last_err = err
            continue

        if not isinstance(data, dict) or "results" not in data:
            last_err = FetchResult(source, FetchStatus.PARSE_ERROR, detail="Missing 'results' key")
            continue

        for job in data["results"]:
            if not isinstance(job, dict):
                continue
            # Guard every field against None values (API returns null for missing data)
            title       = job.get("role") or ""
            company     = job.get("company_name") or "Unknown"
            location    = job.get("location") or "Remote"
            url         = job.get("url") or ""
            text        = job.get("text") or ""
            keywords_list = job.get("keywords") or []
            description = " ".join(keywords_list) if isinstance(keywords_list, list) else ""
            full_text   = title + " " + description + " " + text

            if not keyword_match(full_text, [keyword]):
                continue

            jobs.append({
                "source": source,
                "title": title,
                "company": company,
                "location": location,
                "url": url,
                "description": strip_html(text)[:1000] if text else description[:1000],
                "salary": "Not specified",
            })

    if not jobs:
        status = last_err.status if last_err else FetchStatus.EMPTY
        detail = last_err.detail if last_err else "No jobs returned across all keywords"
        return FetchResult(source, status, detail=detail)

    return FetchResult(source, FetchStatus.OK, jobs=jobs[:MAX_JOBS_PER_SOURCE])



# ---------------------------------------------------------------------------
# NEW SOURCE: Greenhouse (ATS job board — public API, no auth required)
# ---------------------------------------------------------------------------

# Greenhouse hosts job boards for hundreds of tech companies.
# Their board API is fully public for GET requests — no key needed.
# We query a curated list of IT/security-focused employers that post
# on Greenhouse and are known to hire remote US workers.

from search_profile import GREENHOUSE_BOARDS as _GREENHOUSE_BOARDS

_GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"


def fetch_greenhouse_jobs() -> FetchResult:
    """
    Fetch US remote IT/security jobs from Greenhouse-hosted job boards.
    Greenhouse's board API is fully public — no authentication required.
    Queries a curated list of tech/security employers and filters for
    remote US roles matching the configured search keywords.
    """
    source = "Greenhouse"
    jobs: list[Job] = []
    seen_urls: set[str] = set()
    last_err: FetchResult | None = None

    for board in _GREENHOUSE_BOARDS:
        resp, err = _safe_get(
            _GREENHOUSE_API.format(board=board),
            source,
            params={"content": "true"},   # includes full job description
        )
        if err:
            # Individual board failures are non-fatal — log and continue
            if err.status in (FetchStatus.NO_NETWORK,):
                return err
            if err.status != FetchStatus.HTTP_ERROR or (err.http_status or 0) not in (404, 410):
                last_err = err
                logger.debug("[%s] Board '%s': %s", source, board, err.detail)
            continue

        data, err = _parse_json(resp, source)
        if err or not isinstance(data, dict):
            last_err = err or FetchResult(source, FetchStatus.PARSE_ERROR, detail=f"Bad response from board '{board}'")
            continue

        for job in data.get("jobs", []):
            if not isinstance(job, dict):
                continue

            title = job.get("title") or ""
            description = strip_html(job.get("content") or "")

            # Filter by keyword match
            if not keyword_match(title + " " + description, SEARCH_KEYWORDS):
                continue

            # Location filtering — require US or remote
            loc_data = job.get("location", {})
            raw_location = (loc_data.get("name") or "") if isinstance(loc_data, dict) else ""
            loc_lower = raw_location.lower()
            is_us_eligible = (
                not raw_location  # blank = check job metadata
                or "remote" in loc_lower
                or "united states" in loc_lower
                or ", us" in loc_lower
                or loc_lower.endswith(" us")
                or any(s in loc_lower for s in (
                    "new york", "san francisco", "austin", "seattle",
                    "boston", "chicago", "denver", "atlanta", "raleigh",
                ))
            )
            if not is_us_eligible:
                continue

            url = job.get("absolute_url") or ""
            if not url or url in seen_urls:
                continue

            # Check metadata for remote indicator
            metadata = job.get("metadata") or []
            is_remote = any(
                isinstance(m, dict) and "remote" in str(m.get("value") or "").lower()
                for m in metadata
            )
            location = ("Remote — United States" if is_remote
                       else raw_location or "United States")

            seen_urls.add(url)
            jobs.append({
                "source": source,
                "title": title,
                "company": board.replace("-", " ").title(),
                "location": location,
                "url": url,
                "description": description[:1000],
                "salary": "Not specified",
            })

    if not jobs:
        status = last_err.status if last_err else FetchStatus.EMPTY
        detail = last_err.detail if last_err else "No matching remote US jobs found across boards"
        return FetchResult(source, status, detail=detail)

    return FetchResult(source, FetchStatus.OK, jobs=jobs[:MAX_JOBS_PER_SOURCE])


# ---------------------------------------------------------------------------
# NEW SOURCE: USAJobs (US federal government jobs — official public API)
# ---------------------------------------------------------------------------

# USAJobs requires a registered email and API key as request headers.
# Both are free: https://developer.usajobs.gov/apirequest/
# Set in .env: USAJOBS_API_KEY and USAJOBS_EMAIL
# If not set, this source is skipped gracefully.

_USAJOBS_API_URL = "https://data.usajobs.gov/api/Search"


def fetch_usajobs_jobs() -> FetchResult:
    """
    Fetch US federal IT/security jobs from USAJobs (data.usajobs.gov).
    Free public API — requires a registered API key and email in .env.
    Focuses on IT, cybersecurity, network, and systems roles.
    """
    from config import USAJOBS_API_KEY, USAJOBS_EMAIL  # imported lazily to keep config changes optional
    source = "USAJobs"

    if not USAJOBS_API_KEY or not USAJOBS_EMAIL:
        return FetchResult(
            source, FetchStatus.AUTH_ERROR,
            detail="USAJOBS_API_KEY or USAJOBS_EMAIL not set — register free at developer.usajobs.gov",
        )

    jobs: list[Job] = []
    seen_urls: set[str] = set()
    last_err: FetchResult | None = None

    for keyword in SEARCH_KEYWORDS[:6]:
        resp, err = _safe_get(
            _USAJOBS_API_URL,
            source,
            params={
                "Keyword": keyword,
                "LocationName": "Remote",
                "RemoteIndicator": "True",
                "ResultsPerPage": str(MAX_JOBS_PER_SOURCE),
                "SortField": "OpenDate",
                "SortDirection": "Desc",
                "Fields": "Min",
            },
            headers={
                "Host": "data.usajobs.gov",
                "User-Agent": USAJOBS_EMAIL,
                "Authorization-Key": USAJOBS_API_KEY,
            },
        )
        if err:
            last_err = err
            if err.status in (FetchStatus.NO_NETWORK, FetchStatus.AUTH_ERROR):
                return err
            continue

        data, err = _parse_json(resp, source)
        if err:
            last_err = err
            continue

        if not isinstance(data, dict):
            last_err = FetchResult(source, FetchStatus.PARSE_ERROR, detail="Unexpected root type")
            continue

        search_result = data.get("SearchResult", {})
        items = search_result.get("SearchResultItems", [])

        for item in items:
            if not isinstance(item, dict):
                continue
            matched = item.get("MatchedObjectDescriptor", {})
            if not isinstance(matched, dict):
                continue

            title       = matched.get("PositionTitle", "")
            org         = matched.get("OrganizationName", "Unknown")
            url         = matched.get("PositionURI", "")
            apply_uri   = matched.get("ApplyURI", [""])[0] if matched.get("ApplyURI") else url

            locations   = matched.get("PositionLocation", [])
            if locations and isinstance(locations[0], dict):
                loc = locations[0]
                city  = loc.get("CityName", "")
                state = loc.get("CountrySubDivisionCode", "")
                location = f"{city}, {state}".strip(", ") or "United States"
            else:
                location = "United States"

            remuneration = matched.get("PositionRemuneration", [{}])
            sal = remuneration[0] if remuneration else {}
            sal_min = sal.get("MinimumRange", "")
            sal_max = sal.get("MaximumRange", "")
            if sal_min and sal_max:
                try:
                    salary = f"${int(float(sal_min)):,} – ${int(float(sal_max)):,}"
                except ValueError:
                    salary = f"{sal_min} – {sal_max}"
            else:
                salary = "Not specified"

            qual = matched.get("QualificationSummary", "") or ""
            description = strip_html(qual)[:1000]

            if not apply_uri or apply_uri in seen_urls:
                continue

            seen_urls.add(apply_uri)
            jobs.append({
                "source": source,
                "title": title,
                "company": org,
                "location": location,
                "url": apply_uri,
                "description": description,
                "salary": salary,
            })

    if not jobs:
        status = last_err.status if last_err else FetchStatus.EMPTY
        detail = last_err.detail if last_err else "No matching federal jobs found"
        return FetchResult(source, status, detail=detail)

    return FetchResult(source, FetchStatus.OK, jobs=jobs[:MAX_JOBS_PER_SOURCE])


# ---------------------------------------------------------------------------
# SOURCE: Dice (Playwright headless — React-rendered search results)
# ---------------------------------------------------------------------------

# Dice's search results are fully React-rendered — plain HTTP requests get
# an empty shell.  The pipeline already has Playwright installed via Scrapling
# for job validation and manual URL scraping, so we reuse the same
# DynamicFetcher here.  google_search=True spoofs a Google referrer, which
# bypasses Dice's light Cloudflare protection.

_DICE_SEARCH_URL = "https://www.dice.com/jobs"
_DICE_BOT_INDICATORS = ["just a moment", "verifying you are human", "checking your browser"]


async def _fetch_dice_keyword_async(keyword: str, seen_urls: set[str]) -> list[Job]:
    """
    Fetch one page of Dice search results for a single keyword using Playwright.
    Returns a list of Job dicts.
    """
    from scrapling.fetchers import DynamicFetcher

    url = (
        f"{_DICE_SEARCH_URL}?q={keyword.replace(' ', '+')}"
        f"&filters.workplaceTypes=Remote&countryCode=US&language=en"
    )

    try:
        page = await DynamicFetcher.async_fetch(
            url,
            headless=True,
            network_idle=True,
            wait=4000,          # extra wait for React hydration
            disable_resources=True,
            google_search=True,
        )
    except Exception as exc:
        logger.warning("[Dice] Playwright fetch failed for '%s': %s", keyword, exc)
        return []

    html = str(page.html_content or "")

    if any(ind in html.lower() for ind in _DICE_BOT_INDICATORS):
        logger.warning("[Dice] Bot protection triggered for keyword '%s'", keyword)
        return []

    jobs: list[Job] = []

    # Dice renders job cards with data-cy="card" and inner elements with
    # data-cy="card-title-link".  Extract via regex against the rendered HTML.
    # Pattern targets the anchor that wraps the job title inside each card.
    card_pat = re.compile(
        r'<a[^>]+data-cy="card-title-link"[^>]+href="([^"]+)"[^>]*>\s*([^<]{3,150})',
        re.IGNORECASE,
    )
    company_pat = re.compile(
        r'data-cy="search-result-company-name"[^>]*>\s*([^<]{2,100})',
        re.IGNORECASE,
    )
    location_pat = re.compile(
        r'data-cy="search-result-location"[^>]*>\s*([^<]{2,100})',
        re.IGNORECASE,
    )
    salary_pat = re.compile(
        r'data-cy="search-result-salary"[^>]*>\s*([^<]{2,80})',
        re.IGNORECASE,
    )

    companies = [m.group(1).strip() for m in company_pat.finditer(html)]
    locations = [m.group(1).strip() for m in location_pat.finditer(html)]
    salaries  = [m.group(1).strip() for m in salary_pat.finditer(html)]

    for i, m in enumerate(card_pat.finditer(html)):
        href  = m.group(1).strip()
        title = strip_html(m.group(2)).strip()

        if not title or not href:
            continue

        # Dice card URLs may be relative or absolute
        job_url = href if href.startswith("http") else f"https://www.dice.com{href}"
        if job_url in seen_urls:
            continue

        company  = companies[i] if i < len(companies) else "Unknown"
        location = locations[i] if i < len(locations) else "Remote — United States"
        salary   = salaries[i]  if i < len(salaries)  else "Not specified"

        seen_urls.add(job_url)
        jobs.append({
            "source": "Dice",
            "title": title,
            "company": company,
            "location": location,
            "url": job_url,
            "description": "",  # full description fetched by job_validator downstream
            "salary": salary,
        })

    logger.debug("[Dice] '%s' → %d card(s) parsed", keyword, len(jobs))
    return jobs


def fetch_dice_jobs() -> FetchResult:
    """
    Fetch remote US tech jobs from Dice using Playwright (headless Chromium).
    Dice's search results are React-rendered and cannot be fetched with plain
    HTTP — this reuses the DynamicFetcher already installed for job validation.
    """
    source = "Dice"

    async def _run_all() -> list[Job]:
        jobs: list[Job] = []
        seen_urls: set[str] = set()
        for keyword in SEARCH_KEYWORDS[:5]:  # limit to 5 keywords; Playwright is slower
            batch = await _fetch_dice_keyword_async(keyword, seen_urls)
            jobs.extend(batch)
            if len(jobs) >= MAX_JOBS_PER_SOURCE:
                break
        return jobs[:MAX_JOBS_PER_SOURCE]

    try:
        jobs = asyncio.run(_run_all())
    except Exception as exc:
        return FetchResult(
            source, FetchStatus.ERROR,
            detail=f"Playwright runner failed: {exc}",
        )

    if not jobs:
        return FetchResult(
            source, FetchStatus.EMPTY,
            detail="No job cards found — Dice HTML structure may have changed",
        )

    return FetchResult(source, FetchStatus.OK, jobs=jobs)


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

#: All registered fetchers as (display_name, callable) pairs.
#: Add new sources here — no other changes required.
# ---------------------------------------------------------------------------
# SOURCE: Himalayas (free public JSON API — no auth required)
# ---------------------------------------------------------------------------

_HIMALAYAS_SEARCH_URL = "https://himalayas.app/jobs/api/search"


def fetch_himalayas_jobs() -> FetchResult:
    """
    Fetch remote US tech jobs from Himalayas via their free public search API.
    No authentication required. Filters to US-eligible remote roles and runs
    one request per configured keyword, with page 1 only to respect rate limits.
    Full API docs: https://himalayas.app/docs/remote-jobs-api
    """
    source = "Himalayas"
    jobs: list[Job] = []
    seen_urls: set[str] = set()
    last_err: FetchResult | None = None

    for keyword in SEARCH_KEYWORDS[:6]:
        resp, err = _safe_get(
            _HIMALAYAS_SEARCH_URL,
            source,
            params={
                "q": keyword,
                "country": "United States",
                "employment_type": "Full Time",
                "sort": "recent",
                "page": "1",
            },
        )
        if err:
            last_err = err
            if err.status in (FetchStatus.NO_NETWORK, FetchStatus.AUTH_ERROR):
                return err
            if err.http_status == 429:
                logger.warning("[%s] Rate limited — stopping early", source)
                break
            continue

        data, err = _parse_json(resp, source)
        if err:
            last_err = err
            continue

        if not isinstance(data, dict):
            last_err = FetchResult(source, FetchStatus.PARSE_ERROR, detail="Unexpected root type")
            continue

        for job in data.get("jobs", []):
            if not isinstance(job, dict):
                continue

            title       = job.get("title") or ""
            description = strip_html(job.get("description") or "")

            # locationRestrictions is an array of objects: {"alpha2": "US", "name": "United States", ...}
            # Empty array = worldwide (US-eligible). Non-empty must contain US.
            loc_restrictions = job.get("locationRestrictions") or []
            if loc_restrictions:
                country_codes = {
                    (r.get("alpha2") or "").upper()
                    for r in loc_restrictions if isinstance(r, dict)
                }
                if "US" not in country_codes:
                    continue

            url = job.get("applicationLink") or job.get("url") or ""
            if not url or url in seen_urls:
                continue

            company = job.get("companyName") or "Unknown"

            salary_min = job.get("minSalary")
            salary_max = job.get("maxSalary")
            currency   = job.get("currency") or "USD"
            if salary_min and salary_max:
                salary = f"{currency} {int(salary_min):,} – {int(salary_max):,}/yr"
            elif salary_min:
                salary = f"{currency} {int(salary_min):,}+/yr"
            else:
                salary = "Not specified"

            tz_restrictions = job.get("timezoneRestrictions") or []
            restriction_names = [r.get("name") for r in loc_restrictions if isinstance(r, dict) and r.get("name")]
            if restriction_names:
                location = "Remote — " + ", ".join(restriction_names[:3])
            elif tz_restrictions:
                location = "Remote — " + ", ".join(str(t) for t in tz_restrictions[:2])
            else:
                location = "Remote — Worldwide"

            seen_urls.add(url)
            jobs.append({
                "source": source,
                "title": title,
                "company": company,
                "location": location,
                "url": url,
                "description": description[:1000],
                "salary": salary,
            })

    if not jobs:
        status = last_err.status if last_err else FetchStatus.EMPTY
        detail = last_err.detail if last_err else "No matching US remote jobs found"
        return FetchResult(source, status, detail=detail)

    return FetchResult(source, FetchStatus.OK, jobs=jobs[:MAX_JOBS_PER_SOURCE])


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

_SOURCES: list[tuple[str, Callable[[], FetchResult]]] = [
    ("Remotive",         fetch_remotive_jobs),
    ("RemoteOK",         fetch_remoteok_jobs),
    ("The Muse",         fetch_muse_jobs),
    ("Jobicy",           fetch_jobicy_jobs),
    ("Adzuna",           fetch_adzuna_jobs),
    # --- new sources ---
    ("Findwork",         fetch_findwork_jobs),
    ("Himalayas",        fetch_himalayas_jobs),
    ("Greenhouse",       fetch_greenhouse_jobs),
    ("Dice",             fetch_dice_jobs),
    ("USAJobs",          fetch_usajobs_jobs),
]


def fetch_all_jobs() -> list[Job]:
    """
    Fetch jobs from all registered sources.

    Behaviour:
    - A connectivity pre-flight runs before any network I/O.  If the host
      has no internet access the function returns immediately with an empty
      list so the rest of the pipeline is not blocked.
    - Each fetcher returns a FetchResult.  Results are logged at the
      appropriate level (INFO / WARNING / ERROR) and a human-readable
      summary is emitted at the end.
    - Jobs are deduplicated by URL before being returned.
    """
    # --- Connectivity pre-flight -------------------------------------------
    if not check_internet():
        logger.error(
            "[Fetcher] No internet connectivity detected — skipping all job sources. "
            "The pipeline will continue with zero new fetched jobs."
        )
        return []

    logger.info("[Fetcher] Connectivity OK — starting fetch across %d sources", len(_SOURCES))

    # --- Per-source fetch ---------------------------------------------------
    results: list[FetchResult] = []
    for display_name, fetcher in _SOURCES:
        logger.info("[Fetcher] → %s …", display_name)
        try:
            result = fetcher()
        except Exception as exc:
            # Belt-and-suspenders: fetchers should never raise, but just in case.
            result = FetchResult(display_name, FetchStatus.ERROR, detail=str(exc))
            logger.exception("[Fetcher] Unhandled exception from %s fetcher", display_name)

        result.log()
        results.append(result)

    # --- Summary -----------------------------------------------------------
    ok_sources      = [r for r in results if r.ok]
    empty_sources   = [r for r in results if r.warning]
    failed_sources  = [r for r in results if not r.ok and not r.warning]

    logger.info(
        "[Fetcher] Fetch complete — %d/%d sources OK, %d empty, %d failed",
        len(ok_sources), len(_SOURCES), len(empty_sources), len(failed_sources),
    )

    if failed_sources:
        for r in failed_sources:
            logger.warning(
                "[Fetcher] DEGRADED source '%s': [%s] %s",
                r.source, r.status.value, r.detail,
            )

    # --- Deduplication by URL ----------------------------------------------
    all_jobs: list[Job] = [job for r in results for job in r.jobs]
    seen: set[str] = set()
    unique: list[Job] = []
    for job in all_jobs:
        url = job.get("url", "")
        if url and url not in seen:
            seen.add(url)
            unique.append(job)

    logger.info("[Fetcher] Total unique jobs: %d (from %d raw)", len(unique), len(all_jobs))
    return unique
