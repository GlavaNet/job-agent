# notifier.py
import logging

import requests

from config import NTFY_BASE_URL, NTFY_NOTIFICATION_TOPIC
from models import Job

logger = logging.getLogger(__name__)


def _send(
    topic: str,
    title: str,
    message: str,
    priority: str = "default",
    tags: list[str] | None = None,
) -> None:
    """
    Post a notification to a given ntfy topic.

    The Title header is UTF-8 encoded as bytes so that non-ASCII
    characters (em dashes, checkmarks, etc.) are transmitted correctly.
    HTTP headers are nominally latin-1, but urllib3 will pass through
    bytes values without re-encoding them, and ntfy.sh interprets them
    as UTF-8. The message body is also UTF-8 encoded bytes.
    Tags and Priority are ASCII-safe and can stay as plain strings.
    """
    try:
        requests.post(
            f"{NTFY_BASE_URL}/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": priority,
                "Tags": ",".join(tags) if tags else "",
            },
            timeout=5,
        ).raise_for_status()
    except Exception:
        logger.exception("Failed to send ntfy notification to topic '%s'", topic)


def notify(
    title: str,
    message: str,
    priority: str = "default",
    tags: list[str] | None = None,
) -> None:
    """Send a notification to the outbound notifications topic."""
    _send(NTFY_NOTIFICATION_TOPIC, title, message, priority, tags)


def notify_url_received(url: str) -> None:
    """Confirm to phone that a shared URL was received and queued."""
    _send(
        NTFY_NOTIFICATION_TOPIC,
        title="Job URL queued",
        message=f"Added to processing queue:\n{url}",
        priority="low",
        tags=["inbox_tray"],
    )


def notify_url_invalid(message_text: str) -> None:
    """Notify phone that a received message didn't look like a job URL."""
    _send(
        NTFY_NOTIFICATION_TOPIC,
        title="Unrecognized message",
        message=(
            f"Received message doesn't look like a job URL:\n{message_text}\n"
            "Make sure you're sharing a full URL starting with http."
        ),
        priority="low",
        tags=["warning"],
    )


def notify_url_duplicate(url: str) -> None:
    """Notify phone that a shared URL was already processed or queued."""
    _send(
        NTFY_NOTIFICATION_TOPIC,
        title="Already in queue or processed",
        message=url,
        priority="min",
        tags=["repeat"],
    )


def notify_job_processed(job: Job) -> None:
    """Notify that a manual job has been processed and a cover letter is ready."""
    score = job.get("score", 0)
    _send(
        NTFY_NOTIFICATION_TOPIC,
        title=f"Cover letter ready [{score}/10]",
        message=(
            f"{job.get('title', 'Unknown')} @ {job.get('company', 'Unknown')}\n"
            f"Salary: {job.get('salary', 'Not specified')}\n"
            f"{job.get('score_reason', '')}\n"
            f"{job.get('url', '')}"
        ),
        priority="high" if score >= 8 else "default",
        tags=["briefcase", "white_check_mark"],
    )


def notify_job_skipped(job: Job) -> None:
    """Notify that a job scored below threshold."""
    _send(
        NTFY_NOTIFICATION_TOPIC,
        title=f"Job scored too low [{job.get('score', 0)}/10]",
        message=(
            f"{job.get('title', 'Unknown')} @ {job.get('company', 'Unknown')}\n"
            f"{job.get('score_reason', '')}\n"
            f"{job.get('url', '')}"
        ),
        priority="low",
        tags=["briefcase", "x"],
    )


def notify_job_filtered(job: Job) -> None:
    """Notify that a job was filtered before scoring."""
    _send(
        NTFY_NOTIFICATION_TOPIC,
        title="Job filtered (too senior)",
        message=(
            f"{job.get('title', 'Unknown')} @ {job.get('company', 'Unknown')}\n"
            f"{job.get('url', '')}"
        ),
        priority="min",
        tags=["briefcase", "no_entry"],
    )


def notify_followup_needed(job: Job, age_str: str) -> None:
    """
    Remind the user to follow up on a stale application.
    Fired when a job has been in 'applied' status for longer than
    FOLLOWUP_DAYS without any progression.
    """
    score = job.get("score", 0)
    score_label = f" [{score}/10]" if score else ""
    _send(
        NTFY_NOTIFICATION_TOPIC,
        title=f"Follow up? {job.get('title', 'Unknown')} @ {job.get('company', 'Unknown')}",
        message=(
            f"Applied {age_str} — no update yet.{score_label}\n"
            f"Salary: {job.get('salary', 'Not specified')}\n"
            f"Consider following up or marking as withdrawn.\n"
            f"{job.get('url', '')}"
        ),
        priority="default",
        tags=["calendar", "wave"],
    )


def notify_error(url: str, error: str) -> None:
    """Notify that processing a URL failed."""
    _send(
        NTFY_NOTIFICATION_TOPIC,
        title="Job processing failed",
        message=f"{url}\nError: {error}",
        priority="high",
        tags=["warning"],
    )
