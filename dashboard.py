# dashboard.py
import functools
import hashlib
import hmac
import logging
import os
import re
import threading

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from config import DASHBOARD_SECRET, RESUME_PATH, WEB_FORM_HOST, WEB_FORM_PORT
from database import (
    APPLICATION_STATUSES,
    get_all_companies,
    get_all_interview_prep,
    get_all_jobs,
    get_company_for_job,
    get_failed_cover_letters,
    get_job_by_id,
    get_stats,
    init_db,
    save_interview_prep,
    save_tailored_resume,
    update_status,
)
from logger import setup_logging

setup_logging()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema initialisation
#
# Called explicitly here rather than relying on a module-level side effect
# in database.py — init_db() is now opt-in after the database.py refactor.
# ---------------------------------------------------------------------------
init_db()

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Session secret key
#
# Previously derived as f"job-agent-{DASHBOARD_SECRET}".encode(), which
# produces a low-entropy, predictable key when DASHBOARD_SECRET is a short
# human-chosen password.  We now run it through SHA-256 to give the key
# full 256-bit entropy regardless of password length or character set.
#
# The salt prefix ("job-agent-session-v1:") domain-separates this key from
# any other derivations that might use DASHBOARD_SECRET in future, and the
# "v1" lets us rotate the derivation scheme without invalidating the env var.
#
# If DASHBOARD_SECRET is not set, a random 32-byte key is generated per
# process — sessions won't survive restarts, but that is acceptable when
# the dashboard is running without auth (localhost-only deployments).
# ---------------------------------------------------------------------------
if DASHBOARD_SECRET:
    app.secret_key = hashlib.sha256(
        f"job-agent-session-v1:{DASHBOARD_SECRET}".encode()
    ).digest()
else:
    app.secret_key = os.urandom(32)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _auth_enabled() -> bool:
    return bool(DASHBOARD_SECRET)


def _is_authenticated() -> bool:
    return not _auth_enabled() or session.get("authenticated") is True


def login_required(f):
    """Decorator that redirects to /login if the user is not authenticated."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not _is_authenticated():
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Template filter
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r'^[A-Z][A-Z0-9 /&,\'-]{3,}:?\s*$')


def _prep_to_html(text: str) -> str:
    """
    Convert the plain-text prep sheet into light HTML for the dashboard.
    Lines that are ALL-CAPS section headings become <h3> tags;
    everything else becomes <p> tags. Empty lines are ignored.
    """
    if not text:
        return "<p>No prep sheet available.</p>"

    html_parts: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _HEADING_RE.match(stripped):
            html_parts.append(f"<h3>{stripped.rstrip(':')}</h3>")
        else:
            html_parts.append(f"<p>{stripped}</p>")

    return "\n".join(html_parts)


app.jinja_env.filters["prep_to_html"] = _prep_to_html


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if not _auth_enabled():
        return redirect(url_for("index"))

    error = None
    next_url = request.args.get("next") or request.form.get("next") or "/"

    if request.method == "POST":
        password = request.form.get("password", "")
        if hmac.compare_digest(password, DASHBOARD_SECRET):
            session["authenticated"] = True
            session.permanent = False
            # Restrict redirect target to same-origin paths only
            if not next_url.startswith("/") or next_url.startswith("//"):
                next_url = "/"
            return redirect(next_url)
        error = "Incorrect password."

    return render_template("login.html", error=error, next=next_url)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
def index():
    status = request.args.get("status")
    source = request.args.get("source")
    score  = request.args.get("score", type=int)

    jobs    = get_all_jobs(status=status, min_score=score, source=source)
    stats   = get_stats()
    sources = sorted(set(j["source"] for j in get_all_jobs() if j["source"]))

    return render_template(
        "index.html",
        jobs=jobs,
        stats=stats,
        statuses=APPLICATION_STATUSES,
        sources=sources,
        filters={"status": status, "source": source, "score": score},
        failed_cover_letters=get_failed_cover_letters(),
        auth_enabled=_auth_enabled(),
        active_nav="jobs",
    )


@app.route("/companies")
@login_required
def companies():
    return render_template(
        "companies.html",
        companies=get_all_companies(),
        auth_enabled=_auth_enabled(),
        active_nav="companies",
    )


@app.route("/interview-prep")
@login_required
def interview_prep_page():
    """Render the Interview Prep tab."""
    prep_jobs = get_all_interview_prep()

    # Jobs currently interviewing that don't have a prep sheet yet
    interviewing   = get_all_jobs(status="interviewing")
    prep_ids       = {j["id"] for j in prep_jobs}
    interviewing_no_prep = [j for j in interviewing if j["id"] not in prep_ids]

    return render_template(
        "interview_prep.html",
        prep_jobs=prep_jobs,
        interviewing_no_prep=interviewing_no_prep,
        auth_enabled=_auth_enabled(),
        active_nav="prep",
    )


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/api/job/<int:job_id>")
@login_required
def api_get_job(job_id: int):
    job = get_job_by_id(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404

    from salary_normalizer import market_comparison, parse_salary
    info             = parse_salary(job.get("salary") or "")
    company_research = get_company_for_job(job_id)
    comparison       = market_comparison(info, company_research, job.get("title", ""))

    result = dict(job)
    result["salary_comparison"] = comparison
    result["tailored_resume"] = result.get("tailored_resume") or ""
    return jsonify(result)


@app.route("/api/job/<int:job_id>/status", methods=["POST"])
@login_required
def api_update_status(job_id: int):
    data   = request.get_json(silent=True) or {}
    status = data.get("status")
    notes  = (data.get("notes") or "").strip() or None
    if not status:
        return jsonify({"error": "status field required"}), 400
    try:
        update_status(job_id, status, notes)
        return jsonify({"status": "ok"})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/company/<int:job_id>")
@login_required
def api_get_company_for_job(job_id: int):
    company = get_company_for_job(job_id)
    if not company:
        return jsonify({"error": "No research found for this company"}), 404
    return jsonify(company)


@app.route("/api/company/refresh/<string:company_name>", methods=["POST"])
@login_required
def api_refresh_company(company_name: str):
    """Trigger a background re-research of a company."""
    from company_researcher import research_company

    thread = threading.Thread(
        target=research_company,
        args=(company_name,),
        kwargs={"force": True},
        daemon=True,
        name=f"refresh-{company_name[:30]}",
    )
    thread.start()
    logger.info("Research refresh started for %s", company_name)
    return jsonify({
        "status":  "started",
        "message": f"Research refresh started for {company_name}",
    })


@app.route("/api/interview-prep/<int:job_id>")
@login_required
def api_get_interview_prep(job_id: int):
    """Return the interview prep sheet for a job as JSON."""
    job = get_job_by_id(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "job_id":        job_id,
        "title":         job.get("title"),
        "company":       job.get("company"),
        "interview_prep": job.get("interview_prep") or "",
    })


@app.route("/api/interview-prep/<int:job_id>/generate", methods=["POST"])
@login_required
def api_generate_interview_prep(job_id: int):
    """
    Trigger (re-)generation of an interview prep sheet for a job.
    Runs in a background thread so the response is immediate.
    """
    from interview_prep import generate_and_store_prep_sheet

    job = get_job_by_id(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404

    thread = threading.Thread(
        target=generate_and_store_prep_sheet,
        args=(job_id,),
        daemon=True,
        name=f"prep-regen-{job_id}",
    )
    thread.start()
    logger.info("Interview prep generation started for job %d", job_id)
    return jsonify({
        "status":  "ok",
        "message": f"Prep sheet generation started for job {job_id}",
    })


@app.route("/api/cover-letter/<int:job_id>/generate", methods=["POST"])
@login_required
def api_generate_cover_letter(job_id: int):
    """
    Trigger the coupled research + cover letter generation for a job.
    Runs in a background thread — response is immediate.
    Stage 1: company research. Stage 2: cover letter generation.
    The cover_letter_status field tracks progress.
    """
    from company_researcher import research_and_generate_cover_letter

    job = get_job_by_id(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404

    thread = threading.Thread(
        target=research_and_generate_cover_letter,
        args=(job_id,),
        daemon=True,
        name=f"cover-letter-generate-{job_id}",
    )
    thread.start()
    logger.info("Cover letter generation (with research) started for job %d", job_id)
    return jsonify({
        "status":  "ok",
        "message": f"Research and cover letter generation started for job {job_id}",
    })



@app.route("/api/resume/tailor/<int:job_id>", methods=["POST"])
@login_required
def api_tailor_resume(job_id: int):
    """
    Trigger ATS-optimised résumé tailoring for a job.
    Runs in a background thread — response is immediate.
    The tailored résumé is stored in jobs.tailored_resume.
    """
    from resume_tailor import generate_and_store_tailored_resume

    job = get_job_by_id(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404

    thread = threading.Thread(
        target=generate_and_store_tailored_resume,
        args=(job_id,),
        daemon=True,
        name=f"resume-tailor-{job_id}",
    )
    thread.start()
    logger.info("Résumé tailoring started for job %d", job_id)
    return jsonify({
        "status":  "ok",
        "message": f"Résumé tailoring started for job {job_id}",
    })


@app.route("/api/cover-letter/<int:job_id>/retry", methods=["POST"])
@login_required
def api_retry_cover_letter(job_id: int):
    """
    Retry cover letter generation for a job without re-running research.
    Uses existing company research if available.
    """
    from cover_letter import retry_cover_letter
    from resume_parser import load_resume

    job = get_job_by_id(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404

    try:
        resume_text = load_resume(RESUME_PATH)
    except Exception:
        logger.exception("Failed to load résumé for cover letter retry")
        return jsonify({"error": "Could not load résumé"}), 500

    thread = threading.Thread(
        target=retry_cover_letter,
        args=(job_id, resume_text),
        daemon=True,
        name=f"cover-letter-retry-{job_id}",
    )
    thread.start()
    logger.info("Cover letter retry started for job %d", job_id)
    return jsonify({"status": "ok", "message": f"Retry started for job {job_id}"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("Starting dashboard on http://localhost:%d", WEB_FORM_PORT)
    app.run(host=WEB_FORM_HOST, port=WEB_FORM_PORT, debug=False)
