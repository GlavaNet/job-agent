# check_sources.py
"""
Runs every registered job source fetcher and reports whether each one
returned usable data.

Usage:
    python check_sources.py                  # test all sources
    python check_sources.py Dice BuiltIn     # test specific sources by name
    python check_sources.py --list           # print registered source names

Output columns:
    SOURCE   — display name as registered in _SOURCES
    STATUS   — OK / EMPTY / TIMEOUT / AUTH_ERROR / HTTP_ERROR / ERROR
    JOBS     — number of job records returned
    DETAIL   — error message or a sample job title on success

Exit code is 0 if every tested source returned at least one job,
non-zero if any source failed or returned empty.
"""
import argparse
import logging
import sys
import time

from logger import setup_logging
from job_fetcher import _SOURCES, FetchStatus

setup_logging(level=logging.WARNING)  # suppress per-fetcher debug noise


# ---------------------------------------------------------------------------
# ANSI colours (gracefully degraded on Windows / non-TTY)
# ---------------------------------------------------------------------------

_USE_COLOUR = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOUR else text


def _green(t):  return _c("92", t)
def _yellow(t): return _c("93", t)
def _red(t):    return _c("91", t)
def _bold(t):   return _c("1",  t)
def _dim(t):    return _c("2",  t)


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------

def _status_label(status: FetchStatus, job_count: int) -> str:
    if status == FetchStatus.OK and job_count > 0:
        return _green("OK")
    if status == FetchStatus.EMPTY or (status == FetchStatus.OK and job_count == 0):
        return _yellow("EMPTY")
    if status == FetchStatus.TIMEOUT:
        return _yellow("TIMEOUT")
    if status == FetchStatus.AUTH_ERROR:
        return _yellow("AUTH_ERROR")
    if status == FetchStatus.NO_NETWORK:
        return _red("NO_NETWORK")
    return _red(status.value.upper())


def _sample_title(jobs: list) -> str:
    """Return the title of the first job, truncated, for the detail column."""
    if not jobs:
        return ""
    title = jobs[0].get("title", "")
    return f'"{title[:60]}"' if title else "(no title)"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that every job source fetcher returns non-empty data"
    )
    parser.add_argument(
        "sources",
        nargs="*",
        metavar="SOURCE",
        help="Names of specific sources to test (default: all)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print registered source names and exit",
    )
    args = parser.parse_args()

    registered = {name: fn for name, fn in _SOURCES}

    if args.list:
        print("Registered sources:")
        for name in registered:
            print(f"  {name}")
        return 0

    # Resolve which sources to test
    if args.sources:
        unknown = [s for s in args.sources if s not in registered]
        if unknown:
            for name in unknown:
                print(f"Unknown source: '{name}'. Run with --list to see valid names.", file=sys.stderr)
            return 2
        targets = [(name, registered[name]) for name in args.sources]
    else:
        targets = list(_SOURCES)

    # Column widths
    max_name = max(len(name) for name, _ in targets)
    col_name   = max(max_name, 10)
    col_status = 12
    col_jobs   = 6

    header = (
        f"{'SOURCE':<{col_name}}  "
        f"{'STATUS':<{col_status}}  "
        f"{'JOBS':>{col_jobs}}  "
        f"DETAIL"
    )
    divider = "-" * (col_name + col_status + col_jobs + 20)

    print()
    print(_bold(header))
    print(divider)

    failed: list[str] = []

    for name, fetcher in targets:
        t0 = time.monotonic()
        try:
            result = fetcher()
        except Exception as exc:
            elapsed = time.monotonic() - t0
            label   = _red("EXCEPTION")
            detail  = str(exc)[:80]
            print(
                f"{name:<{col_name}}  "
                f"{label:<{col_status + (10 if _USE_COLOUR else 0)}}  "
                f"{'—':>{col_jobs}}  "
                f"{detail}  {_dim(f'({elapsed:.1f}s)')}"
            )
            failed.append(name)
            continue

        elapsed    = time.monotonic() - t0
        job_count  = len(result.jobs)
        label      = _status_label(result.status, job_count)
        jobs_str   = str(job_count) if result.ok else "—"

        if result.ok and job_count > 0:
            detail = _sample_title(result.jobs)
        else:
            detail = result.detail[:80] if result.detail else ""

        print(
            f"{name:<{col_name}}  "
            f"{label:<{col_status + (10 if _USE_COLOUR else 0)}}  "
            f"{jobs_str:>{col_jobs}}  "
            f"{detail}  {_dim(f'({elapsed:.1f}s)')}"
        )

        if not result.ok or job_count == 0:
            failed.append(name)

    # Summary
    print(divider)
    total  = len(targets)
    passed = total - len(failed)

    if not failed:
        print(_green(f"\nAll {total} source(s) returned data successfully.\n"))
        return 0
    else:
        print(
            _yellow(f"\n{passed}/{total} source(s) OK. ")
            + _red(f"Failed: {', '.join(failed)}\n")
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
