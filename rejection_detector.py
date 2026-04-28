#!/usr/bin/env python3
"""
rejection_detector.py

Called by n8n Execute Command node when a new email arrives.
Detects rejection language in the email body, identifies which
applied job it refers to, and updates the status to 'rejected'
in the database.

Usage (recommended -- avoids shell escaping issues):
    python3 rejection_detector.py --input-file /tmp/rejection_input.json

Usage (direct):
    python3 rejection_detector.py \
        --body "...decoded email text..." \
        --sender "careers@somecompany.com" \
        --subject "Re: Your application for Network Engineer" \
        [--is-html] [--dry-run]

Output:
    JSON to stdout: {"matched": true/false, "job_id": N, "company": "...", "title": "..."}
    Logs to stderr.
"""

import argparse
import json
import re
import sys
import os
from typing import Any

# Ensure the script can find database.py regardless of working directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from database import get_all_jobs, update_status


# ---------------------------------------------------------------------------
# Input file loader -- called before argparse to avoid shell escaping issues
# ---------------------------------------------------------------------------

def maybe_load_input_file() -> None:
    """
    If --input-file or --input-b64 is passed, read fields from the source
    and rebuild sys.argv so argparse sees clean individual arguments.
    This avoids shell escaping problems when email content is passed
    directly on the command line from n8n.
    """
    import base64

    if "--input-b64" in sys.argv:
        idx = sys.argv.index("--input-b64")
        b64_data = sys.argv[idx + 1]
        data = json.loads(base64.b64decode(b64_data).decode("utf-8"))
        drop_flag = "--input-b64"
    elif "--input-file" in sys.argv:
        idx = sys.argv.index("--input-file")
        path = sys.argv[idx + 1]
        with open(path) as f:
            data = json.load(f)
        drop_flag = "--input-file"
    else:
        return

    new_args = [sys.argv[0]]
    new_args += ["--sender",  data.get("sender",  "")]
    new_args += ["--subject", data.get("subject", "")]
    new_args += ["--body",    data.get("body",    "")]

    if data.get("isHtml"):
        new_args.append("--is-html")

    # Preserve any extra flags like --dry-run that were passed alongside,
    # but drop the input flag and its value
    skip_next = False
    for arg in sys.argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg == drop_flag:
            skip_next = True
            continue
        new_args.append(arg)

    sys.argv = new_args


# ---------------------------------------------------------------------------
# Rejection signal phrases -- any match is a strong rejection indicator.
# Ordered loosely from most to least unambiguous.
# ---------------------------------------------------------------------------

REJECTION_PHRASES = [
    # Direct "we won't be moving forward" language
    "we will not be moving forward",
    "we won't be moving forward",
    "we are not moving forward",
    "we are unable to move forward",
    "decided not to move forward",
    "decided to move forward with other candidates",
    "moving forward with other candidates",
    "moving forward with another candidate",
    "not moving forward with your application",

    # "Not selected / chosen" language
    "you have not been selected",
    "you were not selected",
    "not selected for",
    "not been selected for",
    "have decided to pursue other candidates",
    "have chosen to pursue other candidates",
    "chosen to move forward with other applicants",
    "we have chosen another candidate",
    "we have selected another candidate",
    "selected a candidate",

    # "After careful consideration" + negative
    "after careful consideration, we will not",
    "after careful review, we will not",
    "after careful consideration, we have decided",
    "after thorough consideration",

    # Explicit rejection wording
    "your application has been unsuccessful",
    "your application was unsuccessful",
    "we regret to inform you",
    "we are sorry to inform you",
    "unfortunately, we will not",
    "unfortunately, we are not able to",
    "unfortunately we will not be",
    "unfortunately, we have decided",
    "unfortunately we are moving forward",
    "we regret that we are unable",
    "we regret to let you know",
    "we are not able to offer you",
    "we're unable to offer you",
    "we cannot offer you",
    "not be able to offer you a position",
    "not able to offer you a position",

    # "Position has been filled"
    "position has been filled",
    "role has been filled",
    "we have filled this position",

    # "Not a match / fit"
    "not a match for",
    "not the right fit",
    "not the best fit",
    "not a strong enough match",
    "your background does not",
    "your qualifications do not",
    "your experience does not",

    # Closing / archiving language
    "we will keep your resume on file",
    "we'll keep your resume on file",
    "keep your information on file",
    "keep your resume in our database",
    "we wish you the best in your job search",
    "we wish you all the best in your search",
    "best of luck in your job search",
    "best of luck with your job search",
    "best of luck in your career search",
    "best of luck with your search",
    "best wishes in your job search",
]

# Phrases that could look like rejections but are NOT
# (e.g. interview confirmations, offer letters, onboarding emails)
FALSE_POSITIVE_GUARDS = [
    "interview is confirmed",
    "interview has been scheduled",
    "we would like to schedule",
    "please schedule a time",
    "we'd like to move forward with you",
    "we are pleased to offer",
    "offer letter",
    "background check",
    "start date",
    "onboarding",
]


# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------

def strip_html(text: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


# ---------------------------------------------------------------------------
# Company name extraction helpers
# ---------------------------------------------------------------------------

def extract_company_from_sender(sender: str) -> str | None:
    """
    Derive a rough company name from the sender email domain.
    E.g. "careers@google.com" -> "google"
         "noreply@mail.greenhouse.io" -> None (ATS platform, not a company)
    """
    ats_domains = {
        "greenhouse.io", "lever.co", "workday.com", "taleo.net",
        "icims.com", "jobvite.com", "smartrecruiters.com",
        "successfactors.com", "myworkdayjobs.com", "bamboohr.com",
        "recruitee.com", "ashbyhq.com", "rippling.com",
        "paylocity.com", "ultipro.com", "adp.com",
        "mail.indeed.com", "indeed.com", "linkedin.com",
        "dice.com", "ziprecruiter.com", "monster.com",
        "careerbuilder.com", "glassdoor.com",
    }

    match = re.search(r'@[\w.]+', sender)
    if not match:
        return None

    domain = match.group().lstrip('@').lower()

    for ats in ats_domains:
        if domain.endswith(ats):
            return None

    # Strip common mail subdomains and TLD
    # e.g. "mail.acmecorp.com" -> "acmecorp"
    parts = domain.split('.')
    if len(parts) >= 2:
        noise = {'mail', 'careers', 'jobs', 'hr', 'recruiting', 'talent',
                 'no-reply', 'noreply', 'notifications', 'auto', 'do-not-reply'}
        meaningful = [p for p in parts[:-1] if p not in noise]
        if meaningful:
            return meaningful[-1]  # rightmost meaningful part = company name

    return None


def extract_company_from_subject(subject: str) -> str | None:
    """
    Try to extract a company name from the subject line.
    Handles patterns like:
      "Your application at Acme Corp"
      "Update on your application to Acme Corp"
      "Acme Corp - Application Status Update"
    """
    patterns = [
        r'application (?:at|to|with|for) (.+?)(?:\s*[--:|]|$)',
        r'(?:from|at) ([A-Z][A-Za-z0-9 &,]+?)(?:\s*[--:|,]|$)',
        r'^([A-Z][A-Za-z0-9 &]+?)\s*[--:|]',
    ]
    for pattern in patterns:
        match = re.search(pattern, subject, re.IGNORECASE)
        if match:
            candidate = match.group(1).strip()
            generic = {'your', 'application', 'update', 'status', 'role',
                       'position', 'opportunity', 'job', 'regarding'}
            if len(candidate) >= 2 and candidate.lower() not in generic:
                return candidate
    return None


# ---------------------------------------------------------------------------
# Rejection detection
# ---------------------------------------------------------------------------

def is_rejection(body: str, subject: str = "") -> bool:
    """
    Return True if the email body contains clear rejection language
    and no false-positive guards suggest it's actually good news.
    """
    text = (subject + " " + body).lower()

    for guard in FALSE_POSITIVE_GUARDS:
        if guard in text:
            return False

    for phrase in REJECTION_PHRASES:
        if phrase in text:
            return True

    return False


# ---------------------------------------------------------------------------
# Job matching
# ---------------------------------------------------------------------------

def normalize(s: str) -> str:
    """Lowercase, strip punctuation and extra whitespace."""
    s = s.lower()
    s = re.sub(r'[^\w\s]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def score_jobs(
    jobs: list[dict],
    company_hint: str | None,
    subject: str,
    body: str,
) -> list[dict]:
    """
    Score a list of jobs against the email and return candidates with a
    positive score, sorted by score descending.

    Scoring:
      +3  company name appears verbatim in the email body
      +2  sender domain matches job company name
      +2  company name appears in the subject line

    Accepts an explicit job list so it can be called without a database
    (pass any list of job dicts with at least 'company' and 'id' keys).
    """
    text = normalize(subject + " " + body[:2000])
    candidates = []

    for job in jobs:
        company = job.get("company", "")
        if not company:
            continue

        company_norm = normalize(company)
        score = 0

        if company_norm and company_norm in text:
            score += 3

        if company_hint:
            hint_norm = normalize(company_hint)
            if hint_norm in company_norm or company_norm in hint_norm:
                score += 2

        if company_norm and company_norm in normalize(subject):
            score += 2

        if score > 0:
            candidates.append((score, job))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return [job for _, job in candidates]


def find_matching_jobs(
    company_hint: str | None,
    subject: str,
    body: str,
) -> list[dict]:
    """
    Query applied jobs from the database and return scored candidates.
    Thin wrapper around score_jobs() for use in production.
    """
    applied_jobs = get_all_jobs(status="applied")
    if not applied_jobs:
        return []
    return score_jobs(applied_jobs, company_hint, subject, body)


# ---------------------------------------------------------------------------
# Core processing — testable, no I/O
# ---------------------------------------------------------------------------

def process_rejection_email(
    body: str,
    sender: str = "",
    subject: str = "",
    jobs: list[dict] | None = None,
) -> dict[str, Any]:
    """
    Determine whether an email is a rejection and identify the matching job.

    Parameters
    ----------
    body:
        Decoded, plain-text email body (strip HTML before calling if needed).
    sender:
        Sender address, used for company name extraction.
    subject:
        Email subject line.
    jobs:
        List of applied job dicts to match against.  If None, the live
        database is queried.  Pass an explicit list in tests.

    Returns
    -------
    A result dict with at minimum a ``"matched"`` bool key, plus:

    On no rejection detected:
        {"matched": False, "reason": "no rejection language"}

    On rejection but no job match:
        {"matched": False, "reason": "...", "company_hint": ..., ...}

    On successful match:
        {"matched": True, "job_id": N, "title": ..., "company": ...,
         "other_candidates": [...]}   # other_candidates only if ambiguous
    """
    if not is_rejection(body, subject):
        return {"matched": False, "reason": "no rejection language"}

    company_from_sender = extract_company_from_sender(sender)
    company_from_subject = extract_company_from_subject(subject)
    company_hint = company_from_sender or company_from_subject

    if jobs is None:
        matches = find_matching_jobs(company_hint, subject, body)
    else:
        matches = score_jobs(jobs, company_hint, subject, body)

    if not matches:
        return {
            "matched": False,
            "reason": "rejection detected but no matching job in database",
            "company_hint": company_hint,
            "sender": sender,
            "subject": subject,
        }

    best = matches[0]
    result: dict[str, Any] = {
        "matched": True,
        "job_id": best["id"],
        "title": best.get("title", ""),
        "company": best.get("company", ""),
        "company_hint": company_hint,
    }

    if len(matches) > 1:
        result["other_candidates"] = [
            {"job_id": j["id"], "title": j.get("title"), "company": j.get("company")}
            for j in matches[1:3]
        ]

    return result


# ---------------------------------------------------------------------------
# Main — thin I/O wrapper around process_rejection_email()
# ---------------------------------------------------------------------------

def main() -> None:
    # Must be called before argparse so --input-file / --input-b64 are
    # transparently converted into individual --sender / --subject / --body args
    maybe_load_input_file()

    parser = argparse.ArgumentParser(
        description="Detect rejection emails and update job status"
    )
    parser.add_argument("--body",    required=True, help="Decoded email body")
    parser.add_argument("--sender",  default="",    help="Sender email address")
    parser.add_argument("--subject", default="",    help="Email subject line")
    parser.add_argument(
        "--is-html", action="store_true",
        help="Treat body as HTML -- will be stripped before analysis"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Detect and report but do NOT update the database"
    )
    # Declared so argparse does not error if either flag remains in sys.argv
    # after maybe_load_input_file() runs
    parser.add_argument("--input-file", default=None)
    parser.add_argument("--input-b64",  default=None)
    args = parser.parse_args()

    body    = strip_html(args.body) if args.is_html else args.body
    sender  = args.sender.strip()
    subject = args.subject.strip()

    print(f"[Rejection] Sender:  {sender}",        file=sys.stderr)
    print(f"[Rejection] Subject: {subject[:80]}",  file=sys.stderr)

    result = process_rejection_email(body, sender, subject)

    if not result["matched"]:
        reason = result.get("reason", "")
        print(f"[Rejection] {reason}.", file=sys.stderr)
        print(json.dumps({"matched": False, "reason": reason}))
        return

    print("[Rejection] Rejection language detected.", file=sys.stderr)
    print(
        f"[Rejection] Company hint: {result.get('company_hint')!r}",
        file=sys.stderr,
    )
    print(
        f"[Rejection] Matched job: [{result['job_id']}] "
        f"{result['title']} @ {result['company']}",
        file=sys.stderr,
    )

    if args.dry_run:
        print("[Rejection] DRY RUN -- skipping database update.", file=sys.stderr)
    else:
        update_status(
            result["job_id"],
            "rejected",
            notes=f"Auto-rejected via email from: {sender}",
        )
        print(
            f"[Rejection] Status updated to 'rejected' for job {result['job_id']}",
            file=sys.stderr,
        )

    output = {k: v for k, v in result.items() if k != "company_hint"}
    output["dry_run"] = args.dry_run
    print(json.dumps(output))


if __name__ == "__main__":
    main()
