# logger.py
"""
Centralised logging configuration for job-agent.

Import and call setup_logging() once at the entry point of each
runnable script (job_agent.py, runner.py, dashboard.py, etc.).
All other modules obtain a logger with:

    import logging
    logger = logging.getLogger(__name__)
"""
import logging
import sys


def setup_logging(level: int = logging.INFO) -> None:
    """
    Configure the root logger with a consistent format.
    Safe to call multiple times — subsequent calls are no-ops.
    """
    root = logging.getLogger()
    if root.handlers:
        return  # already configured

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    root.addHandler(handler)
    root.setLevel(level)
