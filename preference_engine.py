# preference_engine.py
"""
Analyses your job application history and builds a preference profile
that is injected into future scoring prompts to calibrate the LLM.

The profile is updated automatically at the end of every pipeline run
and persisted to preference_profile.json.

Update strategy
───────────────
On first build (no saved profile), the full application history is
analysed and a profile is generated from scratch.

On subsequent runs, only jobs decided *after* the last profile build
are collected (the delta).  The existing profile is passed to the LLM
as a baseline and the model is asked to revise it in light of the new
data.  This keeps every incremental prompt small regardless of how
large the total history grows.

Other optimisations
───────────────────
- The LLM is skipped entirely when no new decisions exist since the
  last build (_profile_is_current guard).
- Section caps in _format_signals protect the initial full build and
  force-rebuild paths against very large histories on CPU-only hardware.
"""
import json
import logging
import os
from datetime import datetime, date

from database import get_all_jobs
from llm_client import invoke_llm

logger = logging.getLogger(__name__)

PROFILE_PATH = "preference_profile.json"

# Statuses that represent a deliberate decision by the user.
_DECIDED_STATUSES = {"applied", "withdrawn", "rejected", "offer received"}

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

# Used on first build — no existing profile to revise.
ANALYSIS_PROMPT = """\
You are analysing a person's job application history to build a preference
profile that will help score future job postings more accurately.

APPLIED JOBS (jobs they chose to apply for):
{applied_jobs}

SKIPPED/WITHDRAWN JOBS (jobs they chose not to pursue):
{skipped_jobs}

SCORE MISMATCH PATTERNS:
{mismatch_patterns}

NOTES WRITTEN ON SPECIFIC JOBS:
{job_notes}

Write a concise preference profile in plain English under each heading.
This profile will be injected directly into a scoring prompt, so write
it as guidance for a scorer, not as a report.

1. PREFERRED ROLES — titles, responsibilities, and functions they gravitate toward
2. PREFERRED ENVIRONMENTS — remote vs on-site, company size, industry patterns
3. POSITIVE SIGNALS — skills, technologies, or keywords that correlate with applying
4. NEGATIVE SIGNALS — things that correlate with skipping (wrong domain, over/under qualified)
5. SCORE CALIBRATION — where the automated scorer is getting it wrong based on mismatches
6. NOTES INSIGHTS — key themes or concerns from their written notes

Use plain paragraphs under each heading, no JSON, no bullet symbols.
Write "Insufficient data yet" for any section without enough signal.
"""

# Used on incremental updates — revise the existing profile with new data only.
UPDATE_PROMPT = """\
You are updating a job preference profile based on new application activity.

EXISTING PROFILE (built from all prior history):
{existing_profile}

NEW ACTIVITY SINCE LAST UPDATE ({since_date}):

NEW APPLIED JOBS:
{applied_jobs}

NEW SKIPPED/WITHDRAWN JOBS:
{skipped_jobs}

NEW SCORE MISMATCH PATTERNS:
{mismatch_patterns}

NEW NOTES:
{job_notes}

Revise the existing profile to incorporate the new activity.
Keep all sections that are still accurate. Update or expand sections
where the new data adds meaningful signal. Remove or soften anything
the new data contradicts.

Return the complete updated profile under the same six headings:
1. PREFERRED ROLES
2. PREFERRED ENVIRONMENTS
3. POSITIVE SIGNALS
4. NEGATIVE SIGNALS
5. SCORE CALIBRATION
6. NOTES INSIGHTS

Use plain paragraphs under each heading, no JSON, no bullet symbols.
If a section has no new signal, reproduce the existing text unchanged.
"""


# ---------------------------------------------------------------------------
# Signal collection
# ---------------------------------------------------------------------------

def _collect_signals(since: date | None = None) -> dict:
    """
    Pull relevant data from the database for preference analysis.
    Only jobs in a decided status are included.

    Parameters
    ----------
    since:
        When provided, only jobs whose date_seen or date_applied falls
        on or after this date are included.  Used for incremental delta
        collection.  When None, all decided jobs are collected (full build).
    """
    all_jobs = get_all_jobs()

    applied: list[str] = []
    skipped: list[str] = []
    mismatches: list[str] = []
    notes: list[str] = []

    for job in all_jobs:
        status = job.get("status", "not applied")
        if status not in _DECIDED_STATUSES:
            continue

        # Date filter for incremental mode
        if since is not None:
            raw = job.get("date_seen") or job.get("date_applied") or ""
            if not raw:
                continue
            try:
                job_date = datetime.strptime(raw[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            if job_date < since:
                continue

        score = job.get("score", 0)
        title = job.get("title", "Unknown")
        company = job.get("company", "Unknown")
        note = job.get("notes", "")

        summary = f"{title} @ {company} (score: {score}/10, status: {status})"

        if status == "applied":
            applied.append(summary)
            if score < 6:
                mismatches.append(
                    f"Applied to '{title} @ {company}' despite low score "
                    f"({score}/10) — scorer may be undervaluing this type of role"
                )
        elif status in ("withdrawn", "rejected"):
            skipped.append(summary)
            if score >= 7:
                mismatches.append(
                    f"Skipped '{title} @ {company}' despite high score "
                    f"({score}/10) — scorer may be overvaluing this type of role"
                )

        if note and note.strip():
            notes.append(f"{title} @ {company}: {note.strip()}")

    return {
        "applied": applied,
        "skipped": skipped,
        "mismatches": mismatches,
        "notes": notes,
    }


def _has_enough_data(signals: dict) -> bool:
    """Require at least 3 decided jobs before building the initial profile."""
    return len(signals["applied"]) + len(signals["skipped"]) >= 3


def _has_delta(signals: dict) -> bool:
    """Return True if there is any new signal to incorporate."""
    return any([
        signals["applied"],
        signals["skipped"],
        signals["mismatches"],
        signals["notes"],
    ])


def _format_signals(signals: dict, caps: bool = True) -> dict:
    """
    Format signal lists as readable strings for prompt injection.

    caps=True applies section limits to protect against very large prompts
    on the initial full build or force-rebuild paths.  caps=False is used
    for incremental delta prompts, which are inherently small.

    Caps:
      applied     — uncapped (typically small, high signal)
      skipped     — 50 most recent
      mismatches  — 30 most recent
      notes       — uncapped (manually written, always high value)
    """
    def _fmt(items: list[str], empty: str, cap: int | None = None) -> str:
        if not items:
            return empty
        capped = items[-cap:] if cap and len(items) > cap else items
        suffix = (
            f"\n  (showing {cap} most recent of {len(items)} total)"
            if cap and len(items) > cap else ""
        )
        return "\n".join(f"- {item}" for item in capped) + suffix

    skipped_cap   = 50 if caps else None
    mismatch_cap  = 30 if caps else None

    return {
        "applied_jobs":      _fmt(signals["applied"],    "None.",                            ),
        "skipped_jobs":      _fmt(signals["skipped"],    "None.",             cap=skipped_cap),
        "mismatch_patterns": _fmt(signals["mismatches"], "None.",             cap=mismatch_cap),
        "job_notes":         _fmt(signals["notes"],      "None."                             ),
    }


# ---------------------------------------------------------------------------
# Profile timestamp helpers
# ---------------------------------------------------------------------------

def _profile_last_updated() -> date | None:
    """
    Return the date portion of the profile's updated timestamp, or None.
    """
    if not os.path.exists(PROFILE_PATH):
        return None
    try:
        with open(PROFILE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("updated", "")
        return datetime.strptime(raw[:10], "%Y-%m-%d").date() if raw else None
    except (json.JSONDecodeError, ValueError, OSError):
        return None


def _most_recent_decision_date() -> date | None:
    """
    Return the date of the most recently decided job in the database.
    Used to decide whether any new decisions exist since the last build.
    """
    all_jobs = get_all_jobs()
    dates: list[date] = []
    for job in all_jobs:
        if job.get("status") not in _DECIDED_STATUSES:
            continue
        raw = job.get("date_seen") or job.get("date_applied") or ""
        if raw:
            try:
                dates.append(datetime.strptime(raw[:10], "%Y-%m-%d").date())
            except ValueError:
                pass
    return max(dates) if dates else None


def _profile_is_current() -> bool:
    """
    Return True if no new decisions have been made since the last build.
    Skips the LLM call entirely when True.
    """
    last_decision = _most_recent_decision_date()
    last_build = _profile_last_updated()

    if last_decision is None or last_build is None:
        return False

    is_current = last_build >= last_decision
    if is_current:
        logger.info(
            "[Preferences] Profile is current (built %s, last decision %s)"
            " — skipping LLM call",
            last_build, last_decision,
        )
    return is_current


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_preference_profile(force_full: bool = False) -> str | None:
    """
    Build or incrementally update the preference profile.

    On first call (no saved profile) or when force_full=True, collects
    the full history and generates a profile from scratch.

    On subsequent calls, collects only jobs decided since the last build
    (the delta) and asks the LLM to revise the existing profile in light
    of that new data.  The prompt stays small regardless of history size.

    Returns the updated profile text, or None if there is insufficient
    data for an initial build.
    """
    logger.info("Analysing application history for preference profile…")

    existing_profile = load_profile()
    last_build = _profile_last_updated()

    # ── Incremental update path ──────────────────────────────────────────
    if existing_profile and last_build and not force_full:
        delta = _collect_signals(since=last_build)

        if not _has_delta(delta):
            logger.info(
                "[Preferences] No new decisions since %s — profile unchanged",
                last_build,
            )
            return existing_profile

        logger.info(
            "Delta since %s — %d applied, %d skipped, %d mismatches, %d notes",
            last_build,
            len(delta["applied"]), len(delta["skipped"]),
            len(delta["mismatches"]), len(delta["notes"]),
        )

        formatted = _format_signals(delta, caps=False)
        prompt = UPDATE_PROMPT.format(
            existing_profile=existing_profile,
            since_date=last_build.strftime("%Y-%m-%d"),
            **formatted,
        )
        logger.info("Updating preference profile from delta…")
        return invoke_llm(prompt)

    # ── Full build path (first run or force_full=True) ───────────────────
    signals = _collect_signals(since=None)

    decided = len(signals["applied"]) + len(signals["skipped"])
    if not _has_enough_data(signals):
        logger.info(
            "Insufficient data (%d decided jobs). Need at least 3.", decided
        )
        return None

    logger.info(
        "Full build — %d applied, %d skipped, %d mismatches, %d notes",
        len(signals["applied"]), len(signals["skipped"]),
        len(signals["mismatches"]), len(signals["notes"]),
    )

    formatted = _format_signals(signals, caps=True)
    prompt = ANALYSIS_PROMPT.format(**formatted)
    logger.info("Generating preference profile from full history…")
    return invoke_llm(prompt)


def save_profile(profile_text: str) -> None:
    """Persist the preference profile to disk with a timestamp."""
    payload = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "profile": profile_text,
    }
    with open(PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    logger.info("Preference profile saved to %s", PROFILE_PATH)


def load_profile() -> str | None:
    """
    Load the current preference profile from disk.
    Returns the profile text, or None if no profile exists yet.
    """
    if not os.path.exists(PROFILE_PATH):
        return None
    try:
        with open(PROFILE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("profile")
    except (json.JSONDecodeError, OSError):
        logger.warning("Could not load preference profile from %s", PROFILE_PATH)
        return None


def update_preference_profile(force_full: bool = False) -> None:
    """
    Build or incrementally update the preference profile, then save it.

    Skips the LLM call entirely when no new decisions exist since the
    last build.  Pass force_full=True to regenerate from scratch.
    """
    if not force_full and _profile_is_current():
        return

    profile_text = build_preference_profile(force_full=force_full)
    if profile_text:
        save_profile(profile_text)
        logger.info("Preference profile updated successfully")
    else:
        logger.info("Preference profile not updated — insufficient data")


if __name__ == "__main__":
    import argparse
    from logger import setup_logging
    setup_logging()

    parser = argparse.ArgumentParser(description="Update job preference profile")
    parser.add_argument(
        "--full", action="store_true",
        help="Force a full rebuild from all history instead of a delta update",
    )
    args = parser.parse_args()

    print("=== Preference Profile Update ===\n")
    update_preference_profile(force_full=args.full)
    profile = load_profile()
    if profile:
        print("\n=== Current Preference Profile ===\n")
        print(profile)
