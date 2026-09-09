# inbox_listener.py
import json
import logging
import re
import ssl
import time

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

from logger import setup_logging

setup_logging()
logger = logging.getLogger(__name__)

# unified-db: queue via database
from config import NTFY_BASE_URL, NTFY_INBOX_TOPIC
from database import enqueue_manual_url, is_url_queued
from job_tracker import get_processed_urls
from notifier import notify_url_duplicate, notify_url_invalid, notify_url_received

_URL_RE = re.compile(
    r'https?://(?:www\.)?[a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,}(?:/[^\s]*)?',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# HTTP/1.1 adapter — ntfy.sh uses HTTP/2 by default; the requests
# library doesn't support H2 and can produce silent connection drops
# unless we negotiate HTTP/1.1 explicitly via ALPN.
# ---------------------------------------------------------------------------

class _HTTP1Adapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.set_alpn_protocols(["http/1.1"])
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)


def _make_session() -> requests.Session:
    session = requests.Session()
    session.mount("https://", _HTTP1Adapter())
    return session


# ---------------------------------------------------------------------------
# Message handling
# ---------------------------------------------------------------------------

def _extract_url(text: str) -> str | None:
    """Return the first URL found in the message text, or None."""
    match = _URL_RE.search(text.strip())
    return match.group(0) if match else None


def _is_already_queued(url: str) -> bool:
    """Return True if the URL is already pending in manual_queue."""
    return is_url_queued(url)


def _append_url(url: str) -> None:
    """Add a new URL to manual_queue, tagged as arriving from phone."""
    enqueue_manual_url(url, source="phone")
    logger.info("Queued: %s", url)


def _process_message(text: str) -> None:
    """Handle a single incoming ntfy message."""
    logger.info("Received message: %s", text[:100])

    url = _extract_url(text)
    if not url:
        logger.info("No URL found — ignoring")
        notify_url_invalid(text[:200])
        return

    processed = get_processed_urls()
    if url in processed:
        logger.info("Already processed: %s", url)
        notify_url_duplicate(url)
        return

    if _is_already_queued(url):
        logger.info("Already in queue: %s", url)
        notify_url_duplicate(url)
        return

    _append_url(url)
    notify_url_received(url)


# ---------------------------------------------------------------------------
# SSE listener
# ---------------------------------------------------------------------------

def listen() -> None:
    """
    Connect to ntfy's SSE stream and process messages as they arrive.
    Reconnects automatically on any connection failure.
    """
    stream_url = f"{NTFY_BASE_URL}/{NTFY_INBOX_TOPIC}/sse"
    logger.info("Starting — watching %s", stream_url)

    session = _make_session()

    while True:
        try:
            logger.info("Connecting to SSE stream…")
            response = session.get(
                stream_url,
                stream=True,
                # 10 s connect timeout; 90 s read timeout resets on each
                # chunk — ntfy sends keepalives every ~30 s so a live stream
                # won't time out under normal conditions.
                timeout=(10, 90),
                headers={
                    "Accept": "text/event-stream",
                    "Cache-Control": "no-cache",
                },
            )
            response.raise_for_status()
            logger.info("Connected — waiting for messages")

            event_data: dict = {}
            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    if "data" in event_data:
                        try:
                            payload = json.loads(event_data["data"])
                            if payload.get("event") == "message":
                                msg = payload.get("message", "")
                                if msg:
                                    _process_message(msg)
                        except json.JSONDecodeError as exc:
                            logger.warning("JSON parse error: %s", exc)
                    event_data = {}
                elif line.startswith("data:"):
                    event_data["data"] = line[5:].strip()
                elif line.startswith("event:"):
                    event_data["event"] = line[6:].strip()

        except requests.exceptions.ReadTimeout:
            logger.info("Read timeout — reconnecting…")
            session = _make_session()
        except requests.exceptions.SSLError:
            logger.exception("SSL error — retrying in 10 s")
            time.sleep(10)
            session = _make_session()
        except requests.exceptions.ConnectionError:
            logger.exception("Connection error — retrying in 30 s")
            time.sleep(30)
            session = _make_session()
        except requests.exceptions.HTTPError as exc:
            logger.error("HTTP error %d — retrying in 10 s", exc.response.status_code)
            time.sleep(10)
        except Exception:
            logger.exception("Unexpected error — retrying in 10 s")
            time.sleep(10)
            session = _make_session()


if __name__ == "__main__":
    listen()
