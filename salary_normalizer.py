# salary_normalizer.py
"""
Parses and normalizes salary strings from job postings into structured
annual figures, then compares them against market-rate data drawn from
the company's Brave search results.

Salary strings in the wild include formats like:
  "$85,000 – $110,000"     (Adzuna range)
  "$95,000+"               (Adzuna minimum)
  "$45/hr"                 (hourly)
  "$40 - $55 per hour"     (hourly range)
  "120k-150k"              (shorthand)
  "Up to $130,000"         (ceiling only)
  "Competitive"            (useless)
  "Not specified"          (absent)

All outputs are annual USD integers or None.

Public API
----------
    parse_salary(raw: str) -> SalaryInfo
    enrich_job_salary(job: Job) -> Job        # adds salary_min/max/midpoint in-place

The market comparison uses the company research already stored in the
database (gathered by company_researcher.py). No additional API calls
are made — this is purely a parsing and inference step.
"""

import logging
import re

from models import Job

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class SalaryInfo:
    """Parsed, normalized salary data for a single job."""

    __slots__ = ("raw", "salary_min", "salary_max", "midpoint", "is_hourly")

    def __init__(
        self,
        raw: str,
        salary_min: int | None = None,
        salary_max: int | None = None,
        is_hourly: bool = False,
    ) -> None:
        self.raw = raw
        self.salary_min = salary_min
        self.salary_max = salary_max
        self.is_hourly = is_hourly
        if salary_min is not None and salary_max is not None:
            self.midpoint: int | None = (salary_min + salary_max) // 2
        elif salary_min is not None:
            self.midpoint = salary_min
        elif salary_max is not None:
            self.midpoint = salary_max
        else:
            self.midpoint = None

    def __repr__(self) -> str:
        return (
            f"SalaryInfo(min={self.salary_min}, max={self.salary_max}, "
            f"midpoint={self.midpoint}, hourly={self.is_hourly})"
        )


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

# Annual hours assumed for hourly → annual conversion
_HOURS_PER_YEAR = 2080  # 40 hrs/wk × 52 wks

# Patterns ordered from most-specific to least-specific
_PATTERNS: list[tuple[str, re.Pattern]] = [
    # "$85,000 – $110,000" or "$85,000 - $110,000"
    ("range_annual", re.compile(
        r'\$\s*([\d,]+)\s*(?:–|-|to)\s*\$?\s*([\d,]+)',
        re.IGNORECASE,
    )),
    # "120k - 150k" or "120K–150K"
    ("range_k", re.compile(
        r'([\d.]+)\s*k\s*(?:–|-|to)\s*([\d.]+)\s*k',
        re.IGNORECASE,
    )),
    # "$45 - $55 per hour" or "$45/hr"
    ("range_hourly", re.compile(
        r'\$\s*([\d,]+(?:\.\d+)?)\s*(?:–|-|to)\s*\$?\s*([\d,]+(?:\.\d+)?)'
        r'\s*(?:per\s+hour|/\s*hr|/\s*hour)',
        re.IGNORECASE,
    )),
    # "$45/hr" single hourly rate
    ("single_hourly", re.compile(
        r'\$\s*([\d,]+(?:\.\d+)?)\s*(?:per\s+hour|/\s*hr|/\s*hour)',
        re.IGNORECASE,
    )),
    # "Up to $130,000"
    ("ceiling", re.compile(
        r'up\s+to\s+\$\s*([\d,]+)',
        re.IGNORECASE,
    )),
    # "$95,000+"
    ("floor", re.compile(
        r'\$\s*([\d,]+)\s*\+',
        re.IGNORECASE,
    )),
    # "150k" single shorthand
    ("single_k", re.compile(
        r'([\d.]+)\s*k\b',
        re.IGNORECASE,
    )),
    # "$95,000" plain annual
    ("single_annual", re.compile(
        r'\$\s*([\d,]+)',
        re.IGNORECASE,
    )),
]


def _clean(value: str) -> int:
    """Strip commas and convert to int."""
    return int(value.replace(",", "").split(".")[0])


def _hourly_to_annual(hourly: float) -> int:
    return int(hourly * _HOURS_PER_YEAR)


def parse_salary(raw: str) -> SalaryInfo:
    """
    Parse a raw salary string into a SalaryInfo.
    Returns a SalaryInfo with all-None fields if the string is
    unparseable (e.g. "Competitive", "Not specified", "").
    """
    if not raw or raw.strip().lower() in (
        "not specified", "competitive", "n/a", "tbd",
        "negotiable", "doe", "depends on experience",
    ):
        return SalaryInfo(raw)

    text = raw.strip()

    for name, pattern in _PATTERNS:
        m = pattern.search(text)
        if not m:
            continue

        try:
            if name == "range_annual":
                lo, hi = _clean(m.group(1)), _clean(m.group(2))
                # Sanity check: treat suspiciously low values as hourly
                if hi < 500:
                    return SalaryInfo(
                        raw,
                        salary_min=_hourly_to_annual(lo),
                        salary_max=_hourly_to_annual(hi),
                        is_hourly=True,
                    )
                return SalaryInfo(raw, salary_min=lo, salary_max=hi)

            elif name == "range_k":
                lo = int(float(m.group(1)) * 1000)
                hi = int(float(m.group(2)) * 1000)
                return SalaryInfo(raw, salary_min=lo, salary_max=hi)

            elif name == "range_hourly":
                lo = _hourly_to_annual(float(m.group(1).replace(",", "")))
                hi = _hourly_to_annual(float(m.group(2).replace(",", "")))
                return SalaryInfo(raw, salary_min=lo, salary_max=hi, is_hourly=True)

            elif name == "single_hourly":
                annual = _hourly_to_annual(float(m.group(1).replace(",", "")))
                return SalaryInfo(raw, salary_min=annual, is_hourly=True)

            elif name == "ceiling":
                hi = _clean(m.group(1))
                return SalaryInfo(raw, salary_max=hi)

            elif name == "floor":
                lo = _clean(m.group(1))
                return SalaryInfo(raw, salary_min=lo)

            elif name == "single_k":
                val = int(float(m.group(1)) * 1000)
                return SalaryInfo(raw, salary_min=val)

            elif name == "single_annual":
                val = _clean(m.group(1))
                if val < 500:
                    # Almost certainly hourly
                    return SalaryInfo(
                        raw,
                        salary_min=_hourly_to_annual(val),
                        is_hourly=True,
                    )
                return SalaryInfo(raw, salary_min=val)

        except (ValueError, IndexError):
            logger.debug("Salary parse error on pattern '%s' for: %r", name, raw)
            continue

    logger.debug("Could not parse salary: %r", raw)
    return SalaryInfo(raw)


# ---------------------------------------------------------------------------
# Market comparison
# ---------------------------------------------------------------------------

def _extract_market_salary_from_research(
    company_research: dict | None,
    job_title: str,
) -> int | None:
    """
    Try to infer a market rate from the company's existing research text.
    Looks for salary figures mentioned in overview, culture, or
    financial_health sections. Returns an annual midpoint or None.
    """
    if not company_research:
        return None

    # Concatenate the sections most likely to mention pay
    text = " ".join(filter(None, [
        company_research.get("overview", ""),
        company_research.get("culture", ""),
        company_research.get("financial_health", ""),
    ]))

    if not text:
        return None

    # Look for salary figures in research text
    figures: list[int] = []
    for _, pattern in _PATTERNS[:4]:  # only strong patterns
        for m in pattern.finditer(text):
            try:
                val = _clean(m.group(1))
                # Filter to plausible annual salary range ($30k–$400k)
                if 30_000 <= val <= 400_000:
                    figures.append(val)
            except (IndexError, ValueError):
                continue

    if not figures:
        return None

    # Return the median of found figures as an approximation
    figures.sort()
    mid = len(figures) // 2
    return figures[mid]


def market_comparison(
    info: SalaryInfo,
    company_research: dict | None,
    job_title: str,
) -> str | None:
    """
    Return a short plain-English market comparison string, or None if
    there is insufficient data to make a comparison.

    Examples:
      "At market rate (~$105k midpoint)"
      "Below market (~$105k midpoint vs ~$120k typical)"
      "Above market (~$140k midpoint vs ~$110k typical)"
    """
    if info.midpoint is None:
        return None

    market_rate = _extract_market_salary_from_research(company_research, job_title)

    if market_rate is None:
        # No comparison data — just report the midpoint
        return f"Midpoint ~${info.midpoint:,}"

    diff_pct = (info.midpoint - market_rate) / market_rate * 100

    midpoint_str = f"~${info.midpoint // 1000}k midpoint"
    market_str = f"~${market_rate // 1000}k typical"

    if diff_pct > 15:
        return f"Above market ({midpoint_str} vs {market_str})"
    elif diff_pct < -15:
        return f"Below market ({midpoint_str} vs {market_str})"
    else:
        return f"At market rate ({midpoint_str})"


# ---------------------------------------------------------------------------
# Public pipeline function
# ---------------------------------------------------------------------------

def enrich_job_salary(job: Job) -> Job:
    """
    Parse the job's salary string and add salary_min, salary_max,
    and salary_midpoint fields in-place. Returns the same job dict.

    Called from upsert_job so every job entering the database gets
    normalized salary fields automatically.
    """
    raw = job.get("salary", "") or ""
    info = parse_salary(raw)

    job["salary_min"] = info.salary_min        # type: ignore[typeddict-unknown-key]
    job["salary_max"] = info.salary_max        # type: ignore[typeddict-unknown-key]
    job["salary_midpoint"] = info.midpoint     # type: ignore[typeddict-unknown-key]

    if info.salary_min or info.salary_max:
        logger.debug(
            "Salary enriched: %r → min=%s max=%s midpoint=%s hourly=%s",
            raw, info.salary_min, info.salary_max, info.midpoint, info.is_hourly,
        )

    return job
