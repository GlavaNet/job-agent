#!/usr/bin/env bash
#
# run_job_agent.sh
#
# Generic launcher for job-agent systemd services. Lives in the repo
# root itself (no separate install step / copy to /usr/local/bin
# needed) — systemd units reference it via %h or a relative-to-repo
# path, e.g.:
#   ExecStart=/home/user/job-agent/run_job_agent.sh runner.py
#
# Reads JOB_AGENT_HOME and JOB_AGENT_USER from the environment
# (systemd's EnvironmentFile= already loaded these from .env before
# this script runs), autodetects venv/ vs .venv/, and execs the target
# script as the right user.
#
# Usage:
#   ./run_job_agent.sh <script.py> [args...]

set -euo pipefail

# --- Resolve project root -------------------------------------------------
# Prefer JOB_AGENT_HOME from .env if set, but always confirm it agrees
# with where this script physically lives -- since the whole point is
# running in-place from the repo, the script's own directory is the
# authoritative fallback and a sanity check against drift (e.g. .env
# left over from a previous clone/move).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -n "${JOB_AGENT_HOME:-}" ]]; then
    PROJECT_ROOT="${JOB_AGENT_HOME}"
    if [[ "$(cd "${PROJECT_ROOT}" 2>/dev/null && pwd)" != "${SCRIPT_DIR}" ]]; then
        echo "WARNING: JOB_AGENT_HOME (${PROJECT_ROOT}) does not match the" >&2
        echo "         directory this script lives in (${SCRIPT_DIR})." >&2
        echo "         Using JOB_AGENT_HOME as configured; update .env if" >&2
        echo "         the repo has moved." >&2
    fi
else
    PROJECT_ROOT="${SCRIPT_DIR}"
fi

if [[ ! -d "${PROJECT_ROOT}" ]]; then
    echo "ERROR: project root (${PROJECT_ROOT}) does not exist." >&2
    exit 1
fi

cd "${PROJECT_ROOT}"

# --- Detect venv (venv/ or .venv/) --------------------------------------
if [[ -x "${PROJECT_ROOT}/venv/bin/python" ]]; then
    PYTHON_BIN="${PROJECT_ROOT}/venv/bin/python"
elif [[ -x "${PROJECT_ROOT}/.venv/bin/python" ]]; then
    PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python"
else
    echo "ERROR: no virtualenv found at ${PROJECT_ROOT}/venv or ${PROJECT_ROOT}/.venv" >&2
    exit 1
fi

# --- Resolve run-as user -------------------------------------------------
# JOB_AGENT_USER comes from .env (same file config.py already reads via
# load_dotenv()). Falls back to the project directory's owner if unset,
# so this doesn't hard-fail on a box where it hasn't been added yet.
RUN_AS_USER="${JOB_AGENT_USER:-$(stat -c '%U' "${PROJECT_ROOT}")}"

TARGET_SCRIPT="${1:-}"
if [[ -z "${TARGET_SCRIPT}" ]]; then
    echo "ERROR: no target script given. Usage: $0 <script.py> [args...]" >&2
    exit 1
fi
shift || true

if [[ ! -f "${PROJECT_ROOT}/${TARGET_SCRIPT}" ]]; then
    echo "ERROR: ${PROJECT_ROOT}/${TARGET_SCRIPT} not found" >&2
    exit 1
fi

echo "[run_job_agent] project_root=${PROJECT_ROOT} python=${PYTHON_BIN} run_as=${RUN_AS_USER} script=${TARGET_SCRIPT}" >&2

CURRENT_USER="$(id -un)"
if [[ "${CURRENT_USER}" == "root" && "${RUN_AS_USER}" != "root" ]]; then
    exec runuser -u "${RUN_AS_USER}" -- "${PYTHON_BIN}" "${PROJECT_ROOT}/${TARGET_SCRIPT}" "$@"
else
    exec "${PYTHON_BIN}" "${PROJECT_ROOT}/${TARGET_SCRIPT}" "$@"
fi
