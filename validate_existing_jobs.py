# validate_existing_jobs.py
"""
One-shot script that fetches full page content for all 'not applied'
jobs in the database, scans for disqualifying content, and
auto-withdraws any that fail validation with a note that feeds
the preference engine.

Run manually:
    python validate_existing_jobs.py

Options:
    python validate_existing_jobs.py --dry-run    # preview without updating
    python validate_existing_jobs.py --verbose    # show full detail per job
"""
import asyncio
import argparse
from database import get_all_jobs, get_job_by_id, upsert_job, update_status
from job_validator import _validate_matched_jobs


async def run(dry_run: bool = False, verbose: bool = False):
    print("=== Existing Job Validator ===\n")

    if dry_run:
        print("DRY RUN MODE — no changes will be made\n")

    # Fetch all not-applied jobs
    jobs = get_all_jobs(status="not applied")
    print(f"Found {len(jobs)} jobs with status 'not applied'\n")

    if not jobs:
        print("Nothing to validate.")
        return

    # Run full page validation
    valid_jobs, disqualified_jobs = await _validate_matched_jobs(jobs)

    if not disqualified_jobs:
        print("\nAll jobs passed validation — nothing to withdraw.")
        return

    # Show summary
    print(f"\n=== Validation Results ===")
    print(f"  Passed:       {len(valid_jobs)}")
    print(f"  Disqualified: {len(disqualified_jobs)}\n")

    print(f"{'ID':<5} {'Reason':<45} Title")
    print("─" * 100)
    for job in disqualified_jobs:
        reason = job.get("_disqualification_reason", "Unknown")
        print(
            f"{job['id']:<5} "
            f"{reason[:43]:<45} "
            f"{job['title'][:45]}"
        )

    if dry_run:
        print(
            f"\nDry run complete — "
            f"{len(disqualified_jobs)} jobs would be withdrawn"
        )
        return

    # Confirm before proceeding
    print(f"\nAbout to withdraw {len(disqualified_jobs)} jobs.")
    confirm = input("Proceed? [yes/no]: ").strip().lower()
    if confirm not in ("yes", "y"):
        print("Cancelled.")
        return

    # Auto-withdraw disqualified jobs
    withdrawn = 0
    failed = 0

    for job in disqualified_jobs:
        try:
            reason = job.get(
                "_disqualification_reason", "disqualifying content found"
            )

            # Ensure job exists in database — upsert in case it
            # somehow isn't stored yet
            row_id = job.get("id")
            if not row_id:
                row_id = upsert_job(job)

            if row_id and row_id != -1:
                update_status(
                    row_id,
                    "withdrawn",
                    notes=(
                        f"Auto-withdrawn after full description scan: "
                        f"{reason}"
                    )
                )
                print(
                    f"  ✓ [{row_id}] Withdrawn — "
                    f"{job['title'][:45]}"
                )
                withdrawn += 1
            else:
                print(
                    f"  ✗ Could not resolve database ID for "
                    f"{job['title'][:45]}"
                )
                failed += 1

        except Exception as e:
            print(f"  ✗ [{job.get('id')}] Failed: {e}")
            failed += 1

    print(f"\n=== Complete ===")
    print(f"  Withdrawn: {withdrawn}")
    if failed:
        print(f"  Failed:    {failed}")

    print(
        "\nThese withdrawals will be included in the next preference "
        "profile update. Run the following to update immediately:\n"
        "  python cli.py preferences --update"
    )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Validate existing 'not applied' jobs against full "
            "page content and auto-withdraw disqualifying ones"
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview which jobs would be withdrawn without making changes"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show full detail for each job checked"
    )
    args = parser.parse_args()
    asyncio.run(run(dry_run=args.dry_run, verbose=args.verbose))


if __name__ == "__main__":
    main()
