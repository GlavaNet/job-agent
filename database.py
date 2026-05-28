# database.py
import logging
import sqlite3
import threading
from datetime import datetime

from models import CompanyResearch, Job

logger = logging.getLogger(__name__)

DB_PATH = "jobs.db"

# Guards all write operations so concurrent dashboard + pipeline
# access never produces a torn write or "database is locked" error.
_db_lock = threading.Lock()

APPLICATION_STATUSES = [
    "not applied",
    "applied",
    "interviewing",
    "offer received",
    "rejected",
    "withdrawn",
]

# Allowlist for the order_by parameter in get_all_jobs().
# Never interpolate user-supplied strings directly into SQL.
_VALID_ORDER_BY = {
    "score DESC",
    "score ASC",
    "date_seen DESC",
    "date_seen ASC",
    "company ASC",
    "title ASC",
    "salary_midpoint DESC",
    "salary_midpoint ASC",
}


def get_connection() -> sqlite3.Connection:
    """Return a connection with row factory and a short busy timeout."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # Enforce foreign-key constraints and WAL mode for better concurrency.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _init_companies_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            name                TEXT UNIQUE NOT NULL,
            overview            TEXT,
            tech_stack          TEXT,
            culture             TEXT,
            financial_health    TEXT,
            interview_process   TEXT,
            recent_news         TEXT,
            remote_policy       TEXT,
            research_summary    TEXT,
            date_researched     TEXT,
            research_status     TEXT DEFAULT 'pending'
        )
    """)


def _migrate_add_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    col_type: str,
) -> None:
    """
    Add a column to an existing table if it doesn't already exist.
    Safe to call every startup — is a no-op when the column is present.
    """
    existing = {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        logger.info("Migrated %s: added column %s %s", table, column, col_type)


def init_db() -> None:
    """Create all tables and indexes if they don't exist."""
    with _db_lock:
        conn = get_connection()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    url             TEXT UNIQUE NOT NULL,
                    title           TEXT,
                    company         TEXT,
                    location        TEXT,
                    salary          TEXT,
                    salary_min      INTEGER,
                    salary_max      INTEGER,
                    salary_midpoint INTEGER,
                    source          TEXT,
                    score           INTEGER,
                    score_reason    TEXT,
                    cover_letter        TEXT,
                    cover_letter_status TEXT DEFAULT 'pending',
                    interview_prep      TEXT,
                    status          TEXT DEFAULT 'not applied',
                    date_seen       TEXT,
                    date_applied    TEXT,
                    notes           TEXT
                )
            """)
            # Migrate existing databases that pre-date these columns
            _migrate_add_column(conn, "jobs", "interview_prep", "TEXT")
            _migrate_add_column(conn, "jobs", "cover_letter_status", "TEXT")
            _migrate_add_column(conn, "jobs", "salary_min", "INTEGER")
            _migrate_add_column(conn, "jobs", "salary_max", "INTEGER")
            _migrate_add_column(conn, "jobs", "salary_midpoint", "INTEGER")
            _migrate_add_column(conn, "jobs", "tailored_resume", "TEXT")
            # Index used by is_likely_duplicate() on every insert
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_company "
                "ON jobs(company)"
            )
            _init_companies_table(conn)
            conn.commit()
            logger.info("Database initialised at %s", DB_PATH)
        finally:
            conn.close()


def is_likely_duplicate(title: str, company: str) -> bool:
    """
    Return True if a job with ≥80% title-word overlap at the same
    company already exists. Prevents the same posting from multiple
    sources appearing as separate entries.

    Note: operates on the title words of the *incoming* job, so a
    shorter incoming title can match a longer existing one. This is
    intentional — "Network Engineer" should deduplicate against
    "Network Engineer (Remote)".
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT title FROM jobs WHERE company = ?",
            (company,)
        ).fetchall()

        title_words = set(title.lower().split())
        if not title_words:
            return False

        for row in rows:
            existing_words = set(row["title"].lower().split())
            overlap = len(title_words & existing_words) / len(title_words)
            if overlap >= 0.8:
                return True
        return False
    finally:
        conn.close()


def upsert_job(job: Job, cover_letter: str = "") -> int:
    """
    Insert a new job or update scoring data if the URL already exists.
    Returns the row id, or -1 if skipped as a likely duplicate.

    On update: refreshes scoring fields but preserves status and notes.
    On insert: runs fuzzy duplicate check first.
    """
    from salary_normalizer import enrich_job_salary
    enrich_job_salary(job)

    with _db_lock:
        conn = get_connection()
        try:
            existing = conn.execute(
                "SELECT id FROM jobs WHERE url = ?",
                (job["url"],)
            ).fetchone()

            if existing:
                conn.execute("""
                    UPDATE jobs SET
                        title               = ?,
                        company             = ?,
                        location            = ?,
                        salary              = ?,
                        salary_min          = ?,
                        salary_max          = ?,
                        salary_midpoint     = ?,
                        source              = ?,
                        score               = ?,
                        score_reason        = ?,
                        cover_letter        = ?,
                        cover_letter_status = ?
                    WHERE url = ?
                """, (
                    job.get("title", ""),
                    job.get("company", ""),
                    job.get("location", ""),
                    job.get("salary", "Not specified"),
                    job.get("salary_min"),
                    job.get("salary_max"),
                    job.get("salary_midpoint"),
                    job.get("source", ""),
                    job.get("score", 0),
                    job.get("score_reason", ""),
                    cover_letter,
                    "complete" if cover_letter else "pending",
                    job["url"],
                ))
                conn.commit()
                return existing["id"]

            if is_likely_duplicate(
                job.get("title", ""), job.get("company", "")
            ):
                logger.debug(
                    "Skipping likely duplicate: %s @ %s",
                    job.get("title", ""), job.get("company", ""),
                )
                return -1

            cursor = conn.execute("""
                INSERT INTO jobs (
                    url, title, company, location, salary,
                    salary_min, salary_max, salary_midpoint,
                    source, score, score_reason, cover_letter,
                    cover_letter_status, status, date_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'not applied', ?)
            """, (
                job["url"],
                job.get("title", ""),
                job.get("company", ""),
                job.get("location", ""),
                job.get("salary", "Not specified"),
                job.get("salary_min"),
                job.get("salary_max"),
                job.get("salary_midpoint"),
                job.get("source", ""),
                job.get("score", 0),
                job.get("score_reason", ""),
                cover_letter,
                "complete" if cover_letter else "pending",
                datetime.now().strftime("%Y-%m-%d"),
            ))
            conn.commit()
            return cursor.lastrowid

        finally:
            conn.close()


def delete_job(job_id: int) -> None:
    """Permanently delete a job from the database by ID."""
    with _db_lock:
        conn = get_connection()
        try:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            conn.commit()
        finally:
            conn.close()


def get_all_jobs(
    status: str = None,
    min_score: int = None,
    source: str = None,
    order_by: str = "score DESC",
) -> list[Job]:
    """Fetch jobs with optional filters. order_by is validated against an allowlist."""
    if order_by not in _VALID_ORDER_BY:
        logger.warning(
            "Invalid order_by value '%s', falling back to 'score DESC'",
            order_by,
        )
        order_by = "score DESC"

    conn = get_connection()
    try:
        query = "SELECT * FROM jobs WHERE 1=1"
        params: list = []

        if status:
            query += " AND status = ?"
            params.append(status)
        if min_score is not None:
            query += " AND score >= ?"
            params.append(min_score)
        if source:
            query += " AND source = ?"
            params.append(source)

        # Safe: order_by has been validated against the allowlist above
        query += f" ORDER BY {order_by}"
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_job_by_id(job_id: int) -> Job | None:
    """Fetch a single job by ID."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_status(job_id: int, status: str, notes: str = None) -> None:
    """
    Update application status and optionally notes.
    Records date_applied when status transitions to 'applied'.
    Spawns a background thread to generate an interview prep sheet
    when status becomes 'interviewing'.

    Note: company research and cover letter generation are no longer
    triggered here. They are initiated explicitly via the dashboard
    "Generate Cover Letter" button, which runs research first and
    generates the letter once research completes.
    """
    if status not in APPLICATION_STATUSES:
        raise ValueError(
            f"Invalid status '{status}'. "
            f"Must be one of: {APPLICATION_STATUSES}"
        )

    date_applied = (
        datetime.now().strftime("%Y-%m-%d")
        if status == "applied" else None
    )

    with _db_lock:
        conn = get_connection()
        try:
            if notes is not None:
                conn.execute("""
                    UPDATE jobs
                    SET status = ?, notes = ?, date_applied = ?
                    WHERE id = ?
                """, (status, notes, date_applied, job_id))
            else:
                conn.execute("""
                    UPDATE jobs
                    SET status = ?, date_applied = ?
                    WHERE id = ?
                """, (status, date_applied, job_id))
            conn.commit()
        finally:
            conn.close()

    if status == "interviewing":
        try:
            from interview_prep import generate_and_store_prep_sheet
            logger.info(
                "Spawning interview prep thread for job %d", job_id
            )
            thread = threading.Thread(
                target=generate_and_store_prep_sheet,
                args=(job_id,),
                daemon=True,
                name=f"interview-prep-{job_id}",
            )
            thread.start()
        except Exception:
            logger.exception(
                "Failed to start interview prep thread for job %d",
                job_id,
            )


def get_company(name: str) -> CompanyResearch | None:
    """Fetch a company record by name."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM companies WHERE name = ?", (name,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def upsert_company(name: str, research: dict) -> None:
    """Insert or update a company research record."""
    with _db_lock:
        conn = get_connection()
        try:
            existing = conn.execute(
                "SELECT id FROM companies WHERE name = ?", (name,)
            ).fetchone()

            fields = (
                research.get("overview", ""),
                research.get("tech_stack", ""),
                research.get("culture", ""),
                research.get("financial_health", ""),
                research.get("interview_process", ""),
                research.get("recent_news", ""),
                research.get("remote_policy", ""),
                research.get("research_summary", ""),
                datetime.now().strftime("%Y-%m-%d"),
            )

            if existing:
                conn.execute("""
                    UPDATE companies SET
                        overview            = ?,
                        tech_stack          = ?,
                        culture             = ?,
                        financial_health    = ?,
                        interview_process   = ?,
                        recent_news         = ?,
                        remote_policy       = ?,
                        research_summary    = ?,
                        date_researched     = ?,
                        research_status     = 'complete'
                    WHERE name = ?
                """, (*fields, name))
            else:
                conn.execute("""
                    INSERT INTO companies (
                        name, overview, tech_stack, culture,
                        financial_health, interview_process,
                        recent_news, remote_policy, research_summary,
                        date_researched, research_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete')
                """, (name, *fields))
            conn.commit()
        finally:
            conn.close()


def set_company_research_status(name: str, status: str) -> None:
    """
    Update the research_status field for a company.
    Used to mark research as 'in_progress' before the long-running
    gather + LLM step so the dashboard can reflect current state.
    """
    with _db_lock:
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE companies SET research_status = ? WHERE name = ?",
                (status, name),
            )
            # Ensure row exists even if research hasn't been inserted yet
            conn.execute("""
                INSERT OR IGNORE INTO companies (name, research_status)
                VALUES (?, ?)
            """, (name, status))
            conn.commit()
        finally:
            conn.close()


def is_company_researched(name: str) -> bool:
    """Return True if this company already has completed research."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT research_status FROM companies WHERE name = ?",
            (name,)
        ).fetchone()
        return row is not None and row["research_status"] == "complete"
    finally:
        conn.close()


def get_all_companies() -> list[CompanyResearch]:
    """Fetch all company records ordered by most recently researched."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM companies ORDER BY date_researched DESC"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_company_for_job(job_id: int) -> CompanyResearch | None:
    """Fetch company research for a given job id."""
    conn = get_connection()
    try:
        job = conn.execute(
            "SELECT company FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if not job:
            return None
        return get_company(job["company"])
    finally:
        conn.close()


def set_cover_letter_status(job_id: int, status: str) -> None:
    """
    Update the cover_letter_status field for a job.
    Valid values: 'pending', 'in_progress', 'complete', 'failed'.
    Mirrors the research_status pattern used for companies.
    """
    with _db_lock:
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE jobs SET cover_letter_status = ? WHERE id = ?",
                (status, job_id),
            )
            conn.commit()
        finally:
            conn.close()


def get_failed_cover_letters() -> list[Job]:
    """
    Return all jobs whose cover letter generation has failed,
    so the dashboard can surface a retry option.
    """
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT id, title, company, date_seen
            FROM jobs
            WHERE cover_letter_status = 'failed'
            ORDER BY date_seen DESC
        """).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()



def save_tailored_resume(job_id: int, resume_text: str) -> None:
    """Persist an ATS-tailored résumé to the jobs table."""
    with _db_write_lock:
        conn = get_connection()
        conn.execute(
            "UPDATE jobs SET tailored_resume = ? WHERE id = ?",
            (resume_text, job_id),
        )
        conn.commit()


def save_interview_prep(job_id: int, prep_text: str) -> None:
    """Persist a generated interview prep sheet to the jobs table."""
    with _db_lock:
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE jobs SET interview_prep = ? WHERE id = ?",
                (prep_text, job_id),
            )
            conn.commit()
        finally:
            conn.close()


def get_all_interview_prep() -> list[Job]:
    """
    Return all jobs that have a generated interview prep sheet,
    ordered by most recently applied.
    """
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT id, title, company, location, salary, status,
                   date_applied, interview_prep
            FROM jobs
            WHERE interview_prep IS NOT NULL AND interview_prep != ''
            ORDER BY date_applied DESC, id DESC
        """).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_stats() -> dict:
    """Return summary statistics about the jobs database."""
    conn = get_connection()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM jobs"
        ).fetchone()[0]

        by_status = conn.execute("""
            SELECT status, COUNT(*) as count
            FROM jobs GROUP BY status
        """).fetchall()

        by_source = conn.execute("""
            SELECT source, COUNT(*) as count
            FROM jobs GROUP BY source ORDER BY count DESC
        """).fetchall()

        avg_score = conn.execute(
            "SELECT ROUND(AVG(score), 1) FROM jobs WHERE score > 0"
        ).fetchone()[0]

        return {
            "total": total,
            "avg_score": avg_score or 0,
            "by_status": {r["status"]: r["count"] for r in by_status},
            "by_source": {r["source"]: r["count"] for r in by_source},
        }
    finally:
        conn.close()


# Initialise on import so every module that imports database
# gets a ready schema without needing to call init_db() explicitly.
init_db()
