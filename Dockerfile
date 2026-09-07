# syntax=docker/dockerfile:1
#
# job-agent — scraping-capable image
#
# For the `runner` service only: runner.py spawns job_agent.py as a
# subprocess (subprocess.Popen([sys.executable, "job_agent.py"])), and
# job_agent.py's manual_job_scraper.py uses scrapling.fetchers.DynamicFetcher
# (Playwright) for the Dice source. dashboard.py and inbox_listener.py do
# NOT import scrapling/playwright anywhere in their call graph — see
# Dockerfile.slim for those.
#
# Built on Playwright's own image because it ships Chromium and every
# OS-level shared library it needs, already version-matched to the
# `playwright` pip package pinned in requirements-scraping.txt. This
# avoids the most common Docker+Playwright failure mode: missing
# libatk/libnss/libgbm-family .so files on a generic slim base image.

FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        sqlite3 \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Base deps first (small, changes rarely), then scraping deps (large,
# also changes rarely) — both cached independently of application code.
COPY requirements-base.txt requirements-scraping.txt ./
RUN pip install --no-cache-dir \
        -r requirements-base.txt \
        -r requirements-scraping.txt

# If a future requirements-scraping.txt bump changes the `playwright`
# package's major version, re-sync browser binaries to match:
# RUN playwright install --with-deps chromium

COPY . .

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# /app/state is where compose.yaml bind-mounts the host ./data directory.
# config.py, database.py, job_cache.py, and preference_engine.py all
# resolve their file paths through JOBAGENT_DATA_DIR (set to /app/state
# in compose.yaml), so this is a plain, explicit mount point — not a
# CWD trick.
RUN mkdir -p /app/state /app/state/resume && chown -R pwuser:pwuser /app

USER pwuser

EXPOSE 8888

# No default CMD — compose.yaml sets command: ["python3", "/app/runner.py"]
