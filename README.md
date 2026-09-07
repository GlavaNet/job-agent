# job-agent — Docker migration

Containerizes `runner.py`, `dashboard.py`, and `inbox_listener.py` (the
three systemd services), plus a `scheduler` container that replaces
both `job-agent-autogen.timer` and n8n's daily pipeline trigger. n8n
itself is not part of this stack — the workflows that used to justify
it (email digest scanning, rejection detection) are no longer in use.

## Files

| File                    | Purpose                                                          |
|-------------------------|-------------------------------------------------------------------|
| `Dockerfile`             | Playwright-based image — `runner` only                          |
| `Dockerfile.slim`        | Slim image — `dashboard`, `listener`, and base for `scheduler`   |
| `Dockerfile.scheduler`   | Cron wrapper (`FROM job-agent-slim`) — daily pipeline + autogen  |
| `compose.yaml`           | Wires all five services together                                 |
| `requirements-base.txt`  | Shared deps (Flask, PyMuPDF, requests) — no Playwright            |
| `requirements-scraping.txt` | Playwright/Scrapling stack — `runner` only                    |
| `.env.example`           | Copy to `.env` and fill in secrets                                |
| `.dockerignore`          | Keeps state files and secrets out of build context                |

## First-time setup

```bash
# 1. Copy your actual application source into this directory —
#    job_agent.py, dashboard.py, runner.py, inbox_listener.py, config.py,
#    search_profile.py, database.py, models.py, job_cache.py,
#    preference_engine.py, templates/, all job_*.py / *_researcher.py /
#    resume_*.py / cover_letter.py modules, auto_generate_resume_cover_letter.py,
#    follow_up_checker.py — everything currently in ~/job-agent/.
cp -r ~/job-agent/*.py ~/job-agent/templates ~/job-agent/search_profile.py .

# 2. Host-side state directory (bind-mounted into every container at
#    /app/state)
mkdir -p data/resume
cp ~/job-agent/resume/resume.pdf data/resume/resume.pdf

# If migrating an existing install, bring its state along too:
cp ~/job-agent/jobs.db data/ 2>/dev/null || true
cp ~/job-agent/manual_jobs.txt data/ 2>/dev/null || true
cp ~/job-agent/processed_jobs.txt data/ 2>/dev/null || true
cp ~/job-agent/seen_jobs.json data/ 2>/dev/null || true
cp ~/job-agent/preference_profile.json data/ 2>/dev/null || true

# 3. Environment
cp .env.example .env
# edit .env with your real API keys/topics

# 4. Build order matters: Dockerfile.scheduler is FROM
#    job-agent-slim:latest, and `docker compose build` doesn't track
#    that as a dependency (it only tracks compose-level depends_on
#    between running containers, not Dockerfile FROM references to
#    another service's image). Build the slim image first.
docker compose build dashboard
docker compose build
docker compose up -d

# 5. First run downloads the Ollama model (several GB) before the other
#    services start — that's what ollama-pull's
#    service_completed_successfully dependency enforces.
docker compose logs -f ollama-pull
```

## Everyday commands

```bash
docker compose logs -f dashboard                              # tail dashboard logs
docker compose logs -f scheduler                               # tail cron output
docker compose exec runner python3 /app/job_agent.py           # one-off manual pipeline run
docker compose exec runner curl -X POST localhost:8888/run     # trigger via HTTP, like cron does
docker compose exec dashboard sqlite3 /app/state/jobs.db       # inspect the DB directly
docker compose restart dashboard                                # restart just one service
docker compose down                                             # stop everything (./data and ollama-models volume persist)
```

Dashboard is reachable at `http://localhost:5000`.

## What replaced what

| Bare-metal piece | Docker replacement |
|---|---|
| `job-agent-runner.service` | `runner` container |
| `job-agent-dashboard.service` | `dashboard` container |
| `job-agent-listener.service` | `listener` container |
| `job-agent-autogen.timer` (2am daily) | cron line in `scheduler` |
| n8n `Job_Agent_Daily_Run.json` (8am weekdays) | cron line in `scheduler`, `curl`-ing `runner`'s `/run` |
| n8n `Job_Agent_Email_Scanner.json` | dropped — no longer in use |
| n8n `Job_Agent_Rejection_Detector.json` | dropped — no longer in use |
| `n8n.service` | removed — no longer needed |
| bare-metal Ollama install | `ollama` + `ollama-pull` containers |

## Path handling

`config.py`, `database.py`, `job_cache.py`, and `preference_engine.py`
all read `JOBAGENT_DATA_DIR` (defaulting to `.` for bare-metal
compatibility). Every container here sets it to `/app/state`, which is
bind-mounted from `./data` on the host — `jobs.db` and its WAL/SHM
sidecars, `manual_jobs.txt`, `processed_jobs.txt`, `seen_jobs.json`,
`preference_profile.json`, and `resume/resume.pdf` all land there,
exactly mirroring the current bare-metal layout.

## Known gaps to decide on later

- **Backups**: `scripts/backup.sh` can keep running on the host,
  pointed at `./data` via `JOBAGENT_DATA_DIR` — it doesn't need to run
  inside a container, since `./data` is a normal host directory.
- **email_url_filter.py / rejection_detector.py**: no longer called by
  anything now that n8n is gone. Left in the repo untouched per your
  call — safe to remove later whenever you clean up.
- **GPU inference**: Ollama runs CPU-only in the container by default,
  same as before. Uncomment the `deploy:` block under the `ollama`
  service in `compose.yaml` if this box has an NVIDIA GPU and
  `nvidia-container-toolkit` installed.
