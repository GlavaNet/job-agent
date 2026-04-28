# runner.py
import functools
import logging
import os
import pathlib
import re
import subprocess
import sys
import threading

import requests
from flask import Flask, jsonify, request

from logger import setup_logging

setup_logging()
logger = logging.getLogger(__name__)

from config import NTFY_BASE_URL, NTFY_NOTIFICATION_TOPIC
from models import PipelineSummary

# ---------------------------------------------------------------------------
# Paths — derived from this file's location so the runner works regardless
# of which user or directory it is launched from.  No hardcoded paths.
# ---------------------------------------------------------------------------

BASE_DIR = pathlib.Path(__file__).parent.resolve()
PYTHON   = sys.executable   # same interpreter / venv that is running runner.py

# ---------------------------------------------------------------------------
# Runner config
# ---------------------------------------------------------------------------

RUNNER_SECRET: str | None = os.getenv("RUNNER_SECRET") or None
RUNNER_PORT:   int        = int(os.getenv("RUNNER_PORT", "8888"))

app = Flask(__name__)

_current_process: subprocess.Popen | None = None
_process_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Token auth
#
# When RUNNER_SECRET is set every mutating endpoint (/run, /stop,
# /run/reprocess-manual) requires:
#   Authorization: Bearer <RUNNER_SECRET>
# Read-only endpoints (/status) are always open.
# ---------------------------------------------------------------------------

def _require_token(f):
    """Decorator that enforces Bearer-token auth when RUNNER_SECRET is set."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if RUNNER_SECRET:
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] != RUNNER_SECRET:
                return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# ANSI colour helpers (visible when tailing the journal with --output=cat)
# ---------------------------------------------------------------------------

class _C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    CYAN    = "\033[96m"
    RED     = "\033[91m"
    MAGENTA = "\033[95m"
    BLUE    = "\033[94m"
    DIM     = "\033[2m"


def _colorize(line: str) -> str:
    """Apply ANSI colours to pipeline output lines for journal readability."""
    if line.startswith("==="):
        return f"{_C.BOLD}{_C.CYAN}{line}{_C.RESET}"
    if any(x in line for x in [
        "Saved to database", "✓", "complete", "Complete",
        "successfully", "Profile saved", "Profile updated",
        "Pipeline completed",
    ]):
        return f"{_C.GREEN}{line}{_C.RESET}"
    if any(x in line for x in [
        "Pre-filtered", "Skipped", "filtered", "BLOCKED",
        "FAILED", "below threshold", "no cover letter",
        "Could not", "Warning", "No new jobs",
    ]):
        return f"{_C.YELLOW}{line}{_C.RESET}"
    if any(x in line for x in [
        "Error", "error", "Traceback", "Exception",
        "DISQUALIFIED", "failed", "Failed", "exited with error",
    ]):
        return f"{_C.RED}{line}{_C.RESET}"
    if "/10]" in line:
        m = re.search(r'\[(\d+)/10\]', line)
        if m:
            score = int(m.group(1))
            if score >= 8:
                return f"{_C.GREEN}{line}{_C.RESET}"
            if score >= 6:
                return f"{_C.YELLOW}{line}{_C.RESET}"
            return f"{_C.DIM}{line}{_C.RESET}"
    if "Matched Jobs" in line:
        return f"{_C.BOLD}{_C.MAGENTA}{line}{_C.RESET}"
    if any(x in line for x in [
        "Fetching jobs from", "jobs found", "Scoring",
        "Generating cover", "Processing", "Loading résumé",
        "Scraping", "Parsing", "chars extracted",
    ]):
        return f"{_C.BLUE}{line}{_C.RESET}"
    if any(x in line for x in [
        "[Cache]", "[Database]", "[Preferences]",
        "[Validator]", "[Runner]", "[Manual]",
        "[Research]", "[Notifier]", "[Scraper]",
        "[Listener]", "[Tracker]",
    ]):
        return f"{_C.DIM}{line}{_C.RESET}"
    return line


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_running() -> bool:
    with _process_lock:
        return _current_process is not None and _current_process.poll() is None


def _header_safe(text: str) -> str:
    """Strip characters that can't be encoded as latin-1 (required for HTTP headers)."""
    return text.encode("latin-1", errors="replace").decode("latin-1")


def _send_notification(
    title: str,
    message: str,
    priority: str = "default",
    tags: list[str] | None = None,
) -> None:
    """Send a completion notification via ntfy."""
    try:
        requests.post(
            f"{NTFY_BASE_URL}/{NTFY_NOTIFICATION_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title":    _header_safe(title),
                "Priority": priority,
                "Tags":     ",".join(tags) if tags else "",
            },
            timeout=5,
        ).raise_for_status()
    except Exception:
        logger.exception("Failed to send ntfy notification")


def _parse_pipeline_summary(output: str) -> PipelineSummary:
    """Extract summary statistics from pipeline stdout."""
    summary: dict = {
        "matched": 0,
        "skipped_seen": 0,
        "skipped_senior": 0,
        "skipped_employment": 0,
        "skipped_requirements": 0,
        "validated": 0,
        "disqualified": 0,
        "top_jobs": [],
        "sources": {},
        "manual_processed": 0,
    }

    for line in output.splitlines():
        m = re.search(r'(\d+) jobs matched', line)
        if m:
            summary["matched"] += int(m.group(1))

        m = re.search(r'Skipped (\d+) already-seen jobs', line)
        if m:
            summary["skipped_seen"] = int(m.group(1))

        m = re.search(r'Pre-filtered (\d+) senior', line)
        if m:
            summary["skipped_senior"] = int(m.group(1))

        m = re.search(r'Pre-filtered (\d+) part-time', line)
        if m:
            summary["skipped_employment"] = int(m.group(1))

        m = re.search(r'Pre-filtered (\d+) jobs with disqualifying', line)
        if m:
            summary["skipped_requirements"] = int(m.group(1))

        m = re.search(r'\[Validator\] (\d+) passed, (\d+) disqualified', line)
        if m:
            summary["validated"]    = int(m.group(1))
            summary["disqualified"] = int(m.group(2))

        m = re.search(r'\[(\d+)/10\] (.+?) @ (.+?) \((.+?)\)', line)
        if m and len(summary["top_jobs"]) < 5:
            summary["top_jobs"].append({
                "score":   int(m.group(1)),
                "title":   m.group(2).strip(),
                "company": m.group(3).strip(),
                "source":  m.group(4).strip(),
            })

        m = re.search(r'Fetching jobs from (\w+)', line)
        if m:
            summary["sources"]["_current"] = m.group(1)
        m = re.search(r'→ (\d+) jobs found', line)
        if m and "_current" in summary["sources"]:
            source = summary["sources"].pop("_current")
            summary["sources"][source] = int(m.group(1))

        if "Cover letter ready" in line:
            summary["manual_processed"] += 1

    return summary


def _build_notification(summary: PipelineSummary) -> tuple[str, str, str]:
    """Return (title, message, priority) for the completion notification."""
    total_new = summary["matched"] + summary["manual_processed"]

    if total_new == 0:
        title    = "Job Agent — no new matches today"
        priority = "low"
    elif total_new >= 5:
        title    = f"Job Agent — {total_new} new matches found"
        priority = "high"
    else:
        plural   = "es" if total_new > 1 else ""
        title    = f"Job Agent — {total_new} new match{plural} found"
        priority = "default"

    lines: list[str] = []
    if summary["matched"]:
        lines.append(f"Automatic search: {summary['matched']} match(es)")
    if summary["manual_processed"]:
        lines.append(f"Manual URLs: {summary['manual_processed']} processed")
    if summary["skipped_seen"]:
        lines.append(f"Skipped {summary['skipped_seen']} already seen")
    if summary["skipped_senior"]:
        lines.append(f"Filtered {summary['skipped_senior']} senior/management")
    if summary["skipped_employment"]:
        lines.append(f"Filtered {summary['skipped_employment']} part-time/contract")
    if summary["skipped_requirements"]:
        lines.append(f"Filtered {summary['skipped_requirements']} clearance-required")
    if summary["disqualified"]:
        lines.append(f"Disqualified {summary['disqualified']} after full scan")

    if summary["sources"]:
        parts = [
            f"{src}: {cnt}"
            for src, cnt in summary["sources"].items()
            if isinstance(cnt, int)
        ]
        if parts:
            lines.append("\nSources: " + ", ".join(parts))

    if summary["top_jobs"]:
        lines.append("\nTop matches:")
        for job in summary["top_jobs"]:
            lines.append(
                f"  [{job['score']}/10] {job['title']} @ {job['company']}"
            )

    if not lines:
        lines.append("Check the dashboard for details.")

    return title, "\n".join(lines), priority


# ---------------------------------------------------------------------------
# Pipeline execution
# ---------------------------------------------------------------------------

def _run_pipeline() -> None:
    """
    Launch job_agent.py as a subprocess using the same Python interpreter
    and working directory as this runner, then send a completion notification.
    """
    global _current_process
    output_lines: list[str] = []

    try:
        logger.info("[Runner] Starting job agent pipeline…")
        with _process_lock:
            _current_process = subprocess.Popen(
                [str(PYTHON), str(BASE_DIR / "job_agent.py")],
                cwd=str(BASE_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

        for line in _current_process.stdout:
            stripped = line.rstrip()
            print(f"[Pipeline] {_colorize(stripped)}")
            output_lines.append(stripped)

        return_code = _current_process.wait()

        with _process_lock:
            _current_process = None

        full_output = "\n".join(output_lines)

        if return_code == 0:
            logger.info("[Runner] Pipeline completed successfully")
            summary = _parse_pipeline_summary(full_output)
            title, message, priority = _build_notification(summary)
            _send_notification(title, message, priority, tags=["robot", "white_check_mark"])

            # Check for stale applications that need a follow-up
            try:
                from follow_up_checker import check_and_notify
                stale_count = check_and_notify()
                if stale_count:
                    logger.info(
                        "[Runner] Follow-up reminders sent for %d stale application(s)",
                        stale_count,
                    )
            except Exception:
                logger.exception("[Runner] Follow-up check failed — pipeline result unaffected")

        elif return_code == -15:
            logger.warning("[Runner] Pipeline was stopped manually")
            _send_notification(
                "Job Agent run stopped",
                "Pipeline was manually stopped.",
                priority="low",
                tags=["robot", "x"],
            )

        else:
            logger.error("[Runner] Pipeline exited with code %d", return_code)
            summary = _parse_pipeline_summary(full_output)
            title, message, _ = _build_notification(summary)
            _send_notification(
                title,
                f"{message}\n\nPipeline exited early (code {return_code}).",
                priority="high",
                tags=["robot", "warning"],
            )

    except Exception:
        logger.exception("[Runner] Unexpected error running pipeline")
        with _process_lock:
            _current_process = None
        _send_notification(
            "Job Agent run failed",
            "Unexpected error — check journalctl for details.",
            priority="high",
            tags=["robot", "warning"],
        )


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/run", methods=["POST"])
@_require_token
def trigger_run():
    if _is_running():
        return jsonify({
            "status":  "busy",
            "message": "Pipeline already running — wait or call /stop first",
        }), 409
    threading.Thread(target=_run_pipeline, daemon=True).start()
    return jsonify({"status": "started", "message": "Pipeline started"}), 200


@app.route("/stop", methods=["POST"])
@_require_token
def stop_run():
    global _current_process
    with _process_lock:
        if _current_process is None or _current_process.poll() is not None:
            return jsonify({"status": "idle", "message": "No pipeline running"}), 200
        logger.info("[Runner] Stopping pipeline on request…")
        _current_process.terminate()
    return jsonify({"status": "stopping", "message": "Stop signal sent"}), 200


@app.route("/run/reprocess-manual", methods=["POST"])
@_require_token
def trigger_reprocess_manual():
    if _is_running():
        return jsonify({"status": "busy", "message": "Pipeline already running"}), 409

    def _reprocess():
        # Import here so the module resolves paths relative to BASE_DIR,
        # not the runner's cwd.  No os.chdir() needed.
        import sys as _sys
        if str(BASE_DIR) not in _sys.path:
            _sys.path.insert(0, str(BASE_DIR))
        from manual_job_scraper import run as run_manual
        run_manual(str(BASE_DIR / "manual_jobs.txt"), reprocess=True)

    threading.Thread(target=_reprocess, daemon=True).start()
    return jsonify({"status": "started", "message": "Manual reprocessing started"}), 200


@app.route("/status", methods=["GET"])
def status():
    running = _is_running()
    pid     = _current_process.pid if running and _current_process else None
    return jsonify({"status": "busy" if running else "idle", "running": running, "pid": pid})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("[Runner] Starting on http://0.0.0.0:%d", RUNNER_PORT)
    app.run(host="0.0.0.0", port=RUNNER_PORT, debug=False)
