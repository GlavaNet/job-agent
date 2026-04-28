# job-agent

I built this to stop spending hours a day manually trawling job boards. It pulls listings from 10 sources, scores them against my résumé using a local LLM, and drops the good ones into a dashboard where I can track applications, generate cover letters, and prep for interviews — all without sending my data to any third-party service. Yes I vibecoded this; I didn't want to spend weeks coding a job hunting assistant, I wanted a job hunting assistant. I also believe *smart* use of AI as a tool and knowing how to frame what you want a tool to do has its legitimacies. I have reviewed this code and tried to make it as clean as possible to the best of my abilities, however since I'm not a senior Python developer if you find something the vibecoder and his henchman missed do feel free to submit a PR.

---

## What it does

**Job fetching** pulls from Remotive, RemoteOK, The Muse, Jobicy, Adzuna, Findwork, Himalayas, Dice, USAJobs, and direct Greenhouse company boards. Everything about what you're looking for — job titles, filters, salary minimums, which companies to watch — lives in one file (`search_profile.py`). Changing job searches means editing that one file, nothing else.

**Scoring** runs each listing through a local Ollama model that compares it against your résumé and the filters you've set. Jobs that are too senior, contract-only, clearance-required, or just a bad match get dropped before they ever hit the database.

**Validation** uses a headless browser to load the actual job page before saving anything, so you're not scoring against a truncated API snippet that omitted the "must have 10 years of Kubernetes experience" requirement buried at the bottom.

**The dashboard** is where you spend most of your time. Open a job, read the full description and salary breakdown, then decide if it's worth pursuing. From there you can generate a cover letter (it researches the company first, then writes the letter using that context), add personal notes to the company card, track your application status, and access an interview prep sheet once you land an interview.

**Email automation** runs through n8n. Incoming rejection emails are matched to your applied jobs and the status gets flipped to `rejected` automatically. Job board digest emails from Indeed, Dice, Jobright, and RemoteHunter are scanned every 4 hours — URLs get extracted, deduplicated against what's already in the database, and queued for the next pipeline run.

---

## Repository layout

```
job-agent/
├── dashboard.py            # Flask web dashboard
├── job_agent.py            # Pipeline entry point
├── runner.py               # HTTP trigger endpoint (for n8n)
├── config.py               # All environment variable wiring
├── search_profile.py       # Your search — edit this to configure the agent
├── models.py               # Shared type definitions
│
├── templates/              # Jinja2 HTML templates for the dashboard
│   ├── base.html
│   ├── index.html
│   ├── companies.html
│   ├── interview_prep.html
│   └── login.html
│
├── n8n/                    # n8n workflow exports (import via n8n UI)
│   ├── Job_Agent_Daily_Run.json
│   ├── Job_Agent_Email_Scanner.json
│   └── Job_Agent_Rejection_Detector.json
│
├── systemd/                # systemd service files for Linux deployment
│   ├── job-agent-runner.service
│   ├── job-agent-dashboard.service
│   ├── job-agent-listener.service
│   └── n8n.service
│
├── resume/                 # gitignored — drop your résumé PDF here
├── .env.example            # Copy to .env and fill in your keys
├── requirements.txt
└── README.md
```

---

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com) running locally with `llama3.1:8b` pulled
- Playwright / Scrapling (for headless page validation)
- Free API keys: [Adzuna](https://developer.adzuna.com/), [Brave Search](https://brave.com/search/api/), [Findwork](https://findwork.dev/)
- [ntfy.sh](https://ntfy.sh) account or self-hosted instance
- [n8n](https://n8n.io) for the email workflows

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/yourname/job-agent.git
cd job-agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Set up your environment

```bash
cp .env.example .env
```

Fill in `.env` with your keys. Don't commit this file.

```env
# Required
ADZUNA_APP_ID=your_app_id
ADZUNA_API_KEY=your_api_key
BRAVE_SEARCH_API_KEY=your_brave_key
FINDWORK_API_KEY=your_findwork_key
NTFY_INBOX_TOPIC=your-inbox-topic
NTFY_NOTIFICATION_TOPIC=your-notification-topic

# Optional — lock down the dashboard and runner endpoint
DASHBOARD_SECRET=your-dashboard-password
RUNNER_SECRET=your-runner-token

# Optional — LLM settings
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.1:8b

# Optional — pipeline behaviour
FOLLOWUP_DAYS=14
COVER_LETTER_TIMEOUT_MINUTES=10

# Optional — interview prep context limits (chars sent to the LLM)
# Defaults are sized for a local 4096-token Ollama model.
# Bump these up if you switch to an API-based model with a larger context window.
PREP_RESUME_CHARS=600
PREP_DESCRIPTION_CHARS=400
PREP_COMPANY_RESEARCH_CHARS=300
```

### 3. Add your résumé

Drop your résumé PDF at `resume/resume.pdf`. That directory is gitignored.

### 4. Configure your search

Open `search_profile.py` and set it up for your job search. This is the only file you need to touch. It covers:

- `PRIMARY_KEYWORDS` — the job titles you want, sent to every source
- Per-source overrides (`ADZUNA_KEYWORDS`, `REMOTIVE_KEYWORDS`) if you need a tighter list for specific boards
- `GREENHOUSE_BOARDS` — companies you want to watch directly on Greenhouse
- `EXCLUDE_TITLE_KEYWORDS` — seniority levels and roles you want filtered out
- `SENIOR_EXPERIENCE_YEAR_THRESHOLD` — how many years of required experience triggers a "too senior" skip
- Employment type exclusions for contract, part-time, and internship roles
- `EXCLUDE_REQUIREMENT_KEYWORDS` — hard no's like security clearances
- `RELEVANCE_THRESHOLD` — the minimum LLM score (1–10) a job needs to get saved
- `MAX_JOBS_PER_SOURCE` — how many jobs to pull per source per run

### 5. Run it

```bash
python job_agent.py
```

### 6. Open the dashboard

```bash
python dashboard.py
# http://localhost:5000
```

---

## Day-to-day workflow

1. The pipeline runs on schedule via n8n (or cron if you prefer), scores everything, and saves matches to the database.
2. Open the dashboard, browse what came in, click into anything that looks interesting.
3. Hit **Generate Cover Letter** when you want to apply. It researches the company first and uses that context to write the letter — takes a few minutes but the result is noticeably better than a generic template. You can also add your own notes to the company research card.
4. Apply externally, then flip the status to `applied` in the dashboard.
5. When you get an interview, change the status to `interviewing`. A prep sheet is generated automatically — role overview, likely questions, STAR story prompts pulled from your own résumé, and questions to ask the interviewer.
6. Rejection emails are handled without any action on your part. n8n catches them, matches them to the right job, and updates the status.
7. Job board digest emails get processed every 4 hours. Any new URLs not already in the database get queued up for the next run.

---

## Running as a service (Linux)

Four systemd service files are in `systemd/`:

| Service | Purpose | Port |
|---|---|---|
| `job-agent-runner.service` | HTTP trigger endpoint for n8n | 8888 |
| `job-agent-dashboard.service` | Web dashboard | 5000 |
| `job-agent-listener.service` | ntfy inbox listener | — |
| `n8n.service` | n8n automation | 5678 |

```bash
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now job-agent-runner job-agent-dashboard job-agent-listener
```

Before installing, update `WorkingDirectory` and `ExecStart` in each file to match where you cloned the repo.

`n8n.service` has the timezone hardcoded to `America/Los_Angeles` — change `GENERIC_TIMEZONE` and `TZ` to your local timezone or your scheduled runs will fire at the wrong time.

If you've set `RUNNER_SECRET`, add `Authorization: Bearer your-token` to your n8n HTTP Request nodes.

---

## n8n workflows

The `n8n/` directory contains three workflow exports. Import them via **Settings → Import workflow** in the n8n UI.

| File | Purpose |
|---|---|
| `Job_Agent_Daily_Run.json` | Triggers the pipeline on a schedule via the runner HTTP endpoint |
| `Job_Agent_Email_Scanner.json` | Scans job board digest emails every 4 hours and queues new URLs |
| `Job_Agent_Rejection_Detector.json` | Detects rejection emails and updates job status automatically |

---

## Sending jobs from your phone

Send any job posting URL to your ntfy inbox topic. The listener picks it up, adds it to `manual_jobs.txt`, and it gets scraped and scored on the next pipeline run.

---

## Retargeting for a different job search

Edit `search_profile.py`. Update the keywords, Greenhouse board list, and filters for your new target role. Nothing else needs to change.

---

## Migrating to an API-based LLM

All LLM calls go through `llm_client.py`, so switching backends only requires changes there. When you do, the interview prep context limits are worth increasing — the current defaults are conservative for a local 4096-token Ollama model. A 32k-token model can comfortably handle much more context:

```env
PREP_RESUME_CHARS=3000
PREP_DESCRIPTION_CHARS=2000
PREP_COMPANY_RESEARCH_CHARS=2000
```

---

## Utility scripts

| Script | What it's for |
|---|---|
| `follow_up_checker.py` | Flags applications that have gone stale and sends reminders via ntfy. |
| `rejection_detector.py` | Called by n8n to detect rejections in email bodies and update job status. Accepts `--input-b64`, `--input-file`, or direct `--body`/`--sender`/`--subject` args. Supports `--dry-run`. |
| `email_url_filter.py` | Called by n8n to extract and deduplicate job URLs from email bodies. Follows tracking redirects for Dice and Indeed links before checking against the database. |
| `preference_engine.py` | Rebuilds the scoring preference profile from your application history. Run this manually if you want to recalibrate scoring after a batch of applications. |
| `manual_job_scraper.py` | Scrapes and scores jobs from URLs in `manual_jobs.txt`. |
