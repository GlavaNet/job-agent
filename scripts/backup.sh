#!/bin/bash
#
# Daily job-agent State Backup (3-2-1 rule)
#
# Backs up job-agent's state files as one bundle:
#   jobs.db, manual_jobs.txt, preference_profile.json,
#   processed_jobs.txt, seen_jobs.json
#
# Copy 1: live state files (untouched by this script)
# Copy 2: local backup on a second disk/mount
# Copy 3: offsite - choose ONE or BOTH of the methods below
#
# Requires: sqlite3, tar, age, gzip
# Optional: rclone (for cloud offsite), tailscale + rsync/ssh (for remote-device offsite)
#
# Setup once:
#   mkdir -p ~/.age
#   age-keygen -o ~/.age/job-agent-backup-key.txt
#   chmod 600 ~/.age/job-agent-backup-key.txt
#   -> back up this key file SEPARATELY from your backups (password manager, printed copy, etc.)
#   -> the public key it prints goes into BACKUP_AGE_RECIPIENT in .env
#
# All values below are read from .env. Nothing sensitive or project-specific
# is hardcoded here, so this script is safe to commit as-is.
#
# Add to .env (see .env.backup.example):
#   PROJECT_NAME=job-agent                    # used to name/tag backup files
#   JOBAGENT_DATA_DIR=/path/to/job-agent       # dir containing the 5 state files - defaults to repo root
#   BACKUP_LOCAL_DIR=/path/to/backups
#   BACKUP_SECOND_DISK_DIR=/mnt/second-disk/backups
#   BACKUP_AGE_RECIPIENT=age1xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
#   BACKUP_RETENTION_DAYS=56
#   BACKUP_USE_TAILSCALE=true
#   BACKUP_REMOTE_HOST=user@raspberrypi
#   BACKUP_REMOTE_PATH=/backups/job-agent
#   BACKUP_USE_RCLONE=true
#   BACKUP_RCLONE_REMOTE=b2backup
#   BACKUP_RCLONE_PATH=job-agent-backups
#   BACKUP_LOG_MAX_BYTES=5242880              # rotate backup.log past this size, default 5MB
#   BACKUP_LOG_KEEP=8                         # how many rotated (gzipped) logs to keep
#
# Log rotation is handled by this script itself (see bottom) - no logrotate,
# cron, or systemd config needed for it. Only relevant if you're redirecting
# output into backup.log yourself, e.g. via install-backup-cron.sh.
#
# Each backup also produces a <stamp>.tar.sha256 sidecar file next to the
# encrypted backup, at every destination it's copied to. restore.sh uses
# this to verify the restored bundle is byte-for-byte identical to what
# was backed up here, not just that jobs.db is structurally valid SQLite.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/../.env}"

# ---------- Colors (auto-disabled if not a terminal, e.g. redirected to backup.log via cron) ----------
if [ -t 1 ]; then
    C_RED=$'\033[0;31m'; C_GREEN=$'\033[0;32m'; C_YELLOW=$'\033[0;33m'
    C_BLUE=$'\033[0;34m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""; C_RESET=""
fi
info()    { echo "${C_BLUE}[$(date)]${C_RESET} $1"; }
success() { echo "${C_GREEN}[$(date)] OK:${C_RESET} $1"; }
warn()    { echo "${C_YELLOW}[$(date)] WARNING:${C_RESET} $1" >&2; }
error()   { echo "${C_RED}[$(date)] ERROR:${C_RESET} $1" >&2; }

if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
else
    error ".env not found at $ENV_FILE (set ENV_FILE to override)"
    exit 1
fi

# ---------- CONFIG (sourced from .env, with fallback defaults) ----------
PROJECT_NAME="${PROJECT_NAME:-job-agent}"              # used for backup filenames/tags - set in .env to customize
LOCAL_BACKUP_DIR="${BACKUP_LOCAL_DIR:?BACKUP_LOCAL_DIR must be set in .env}"
SECOND_DISK_DIR="${BACKUP_SECOND_DISK_DIR:-}"          # Copy 2: different physical media
AGE_RECIPIENT="${BACKUP_AGE_RECIPIENT:?BACKUP_AGE_RECIPIENT must be set in .env}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-56}"          # keep ~8 weeks of daily backups by default

# Offsite method toggles
USE_TAILSCALE_REMOTE="${BACKUP_USE_TAILSCALE:-false}"
USE_RCLONE_CLOUD="${BACKUP_USE_RCLONE:-false}"

# --- Tailscale remote device settings ---
REMOTE_HOST="${BACKUP_REMOTE_HOST:-}"
REMOTE_PATH="${BACKUP_REMOTE_PATH:-}"

# --- rclone cloud settings ---
RCLONE_REMOTE="${BACKUP_RCLONE_REMOTE:-}"
RCLONE_PATH="${BACKUP_RCLONE_PATH:-${PROJECT_NAME}-backups}"

DATE=$(date +%Y%m%d)
STAMP="${PROJECT_NAME}_$DATE"

# Log self-rotation (no logrotate/cron/systemd config needed - see bottom of script)
LOG_FILE="$SCRIPT_DIR/../backup.log"
LOG_MAX_BYTES="${BACKUP_LOG_MAX_BYTES:-5242880}"       # 5 MB default
LOG_KEEP="${BACKUP_LOG_KEEP:-8}"                       # how many rotated logs to keep
# -----------------------------

# ---------- Resolve the state files to back up ----------
# Unlike a single-DB project, job-agent's state is 5 named files living
# together in one directory. JOBAGENT_DATA_DIR lets you point at that
# directory explicitly; otherwise it defaults to the repo root (one level
# up from scripts/), which is where job-agent keeps them by convention.
DATA_DIR="${JOBAGENT_DATA_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"

if [ ! -d "$DATA_DIR" ]; then
    error "JOBAGENT_DATA_DIR '$DATA_DIR' does not exist."
    exit 1
fi

DB_NAME="jobs.db"
STATE_FILES=(
    "manual_jobs.txt"
    "preference_profile.json"
    "processed_jobs.txt"
    "seen_jobs.json"
)

DB_PATH="$DATA_DIR/$DB_NAME"
if [ ! -f "$DB_PATH" ]; then
    error "no $DB_NAME found at $DB_PATH. Set JOBAGENT_DATA_DIR in .env to the correct directory."
    exit 1
fi

# Flat files are backed up best-effort: warn and skip individually if one is
# missing (e.g. seen_jobs.json hasn't been created yet on a fresh install)
# rather than failing the whole backup over an optional file.
PRESENT_STATE_FILES=()
for f in "${STATE_FILES[@]}"; do
    if [ -f "$DATA_DIR/$f" ]; then
        PRESENT_STATE_FILES+=("$f")
    else
        warn "$f not found in $DATA_DIR - skipping (fine if it hasn't been created yet)."
    fi
done
# -----------------------------

# ---------- Config validation (fail fast, before touching any files) ----------
CONFIG_ERRORS=0

if [ "$USE_TAILSCALE_REMOTE" = true ]; then
    if [ -z "$REMOTE_HOST" ]; then
        error "BACKUP_USE_TAILSCALE=true but BACKUP_REMOTE_HOST is empty."
        CONFIG_ERRORS=1
    fi
    if [ -z "$REMOTE_PATH" ]; then
        error "BACKUP_USE_TAILSCALE=true but BACKUP_REMOTE_PATH is empty."
        CONFIG_ERRORS=1
    fi
fi

if [ "$USE_RCLONE_CLOUD" = true ]; then
    if [ -z "$RCLONE_REMOTE" ]; then
        error "BACKUP_USE_RCLONE=true but BACKUP_RCLONE_REMOTE is empty."
        CONFIG_ERRORS=1
    fi
fi

if [ "$USE_TAILSCALE_REMOTE" != true ] && [ "$USE_RCLONE_CLOUD" != true ]; then
    warn "no offsite method enabled (BACKUP_USE_TAILSCALE and BACKUP_USE_RCLONE are both false)."
    warn "This backup will NOT satisfy the 3-2-1 rule's offsite requirement."
fi

if [ "$CONFIG_ERRORS" -ne 0 ]; then
    error "fix the .env issues above before running backup.sh."
    exit 1
fi
# -----------------------------

# ---------- Preflight: required binaries (fail fast, before touching any files) ----------
MISSING_BINS=0

for bin in sqlite3 tar age gzip; do
    if ! command -v "$bin" >/dev/null 2>&1; then
        error "required binary '$bin' not found in PATH. See README > Backup script dependencies."
        MISSING_BINS=1
    fi
done

if [ "$USE_TAILSCALE_REMOTE" = true ]; then
    for bin in tailscale rsync; do
        if ! command -v "$bin" >/dev/null 2>&1; then
            error "BACKUP_USE_TAILSCALE=true but '$bin' not found in PATH."
            MISSING_BINS=1
        fi
    done
fi

if [ "$USE_RCLONE_CLOUD" = true ]; then
    if ! command -v rclone >/dev/null 2>&1; then
        error "BACKUP_USE_RCLONE=true but 'rclone' not found in PATH."
        MISSING_BINS=1
    fi
fi

if [ "$MISSING_BINS" -ne 0 ]; then
    error "install the missing binaries above before running backup.sh."
    exit 1
fi
# -----------------------------

mkdir -p "$LOCAL_BACKUP_DIR"

# ---------- Self-rotate backup.log (only relevant if something redirects into it, e.g. cron) ----------
# Runs on every invocation, before anything else, so a huge log never blocks
# a backup and rotation happens even if the run fails downstream. No root,
# no /etc/logrotate.d, no separate script - just this script managing its
# own log file.
#
# Uses copy-then-truncate rather than mv/rename: if the caller redirected
# into this file with shell `>>` (e.g. cron's "backup.sh >> backup.log"),
# that redirection already holds an open file descriptor pointing at the
# file's current inode. Renaming the file out from under that descriptor
# would leave the caller writing into the old, now-unlinked file forever -
# nothing would ever land in a fresh backup.log again. Truncating in place
# keeps the same inode, so the open descriptor keeps working correctly.
if [ -f "$LOG_FILE" ]; then
    LOG_BYTES=$(wc -c < "$LOG_FILE" 2>/dev/null || echo 0)
    if [ "$LOG_BYTES" -ge "$LOG_MAX_BYTES" ]; then
        ROTATED="${LOG_FILE}.$(date +%Y%m%d-%H%M%S)"
        cp "$LOG_FILE" "$ROTATED"
        : > "$LOG_FILE"   # truncate in place - keeps the same inode
        gzip "$ROTATED" 2>/dev/null || true   # best-effort; keep going even if gzip is missing
        # Prune old rotated logs beyond LOG_KEEP, oldest first
        # shellcheck disable=SC2012
        ls -1t "${LOG_FILE}".*.gz 2>/dev/null | tail -n "+$((LOG_KEEP + 1))" | xargs -r rm -f
    fi
fi
# -----------------------------

info "Starting backup: $STAMP"
info "State directory: $DATA_DIR"
info "Files: $DB_NAME ${PRESENT_STATE_FILES[*]}"

# ---------- Staging area for this run ----------
STAGE_DIR=$(mktemp -d)
cleanup_stage() { rm -rf "$STAGE_DIR"; }
trap cleanup_stage EXIT

# 1. Consistent SQLite snapshot of jobs.db (safe even if the DB is actively
#    being written to - sqlite3 .backup takes its own read lock/WAL handling).
sqlite3 "$DB_PATH" ".backup '$STAGE_DIR/$DB_NAME'"

# 2. Integrity check before trusting this backup at all
INTEGRITY=$(sqlite3 "$STAGE_DIR/$DB_NAME" "PRAGMA integrity_check;")
if [ "$INTEGRITY" != "ok" ]; then
    error "jobs.db integrity check failed: $INTEGRITY"
    exit 1
fi

# 3. Copy the flat state files alongside the DB snapshot into the same
#    staging dir, so everything ends up in one tarball together.
for f in "${PRESENT_STATE_FILES[@]}"; do
    cp "$DATA_DIR/$f" "$STAGE_DIR/$f"
done

# 4. Bundle into a single tar (uncompressed - gzip is applied as its own
#    step next, matching the CRM pipeline's compress-then-encrypt order).
TAR_FILE="$LOCAL_BACKUP_DIR/$STAMP.tar"
tar -cf "$TAR_FILE" -C "$STAGE_DIR" "$DB_NAME" "${PRESENT_STATE_FILES[@]}"

# 4b. Hash the plaintext tarball before it's touched by compression/encryption.
# gzip and age are both lossless/reversible, so this hash should match the
# hash of the file restore.sh produces after decrypt+decompress, exactly,
# every time. restore.sh compares against this sidecar to prove the restored
# bundle is byte-for-byte identical to what was backed up here.
sha256sum "$TAR_FILE" | awk '{print $1}' > "$LOCAL_BACKUP_DIR/$STAMP.tar.sha256"

# 5. Compress
gzip "$TAR_FILE"

# 6. Encrypt with age (asymmetric - only the private key holder can decrypt)
age -r "$AGE_RECIPIENT" -o "$LOCAL_BACKUP_DIR/$STAMP.tar.gz.age" "$LOCAL_BACKUP_DIR/$STAMP.tar.gz"
rm "$LOCAL_BACKUP_DIR/$STAMP.tar.gz"

ENCRYPTED_FILE="$LOCAL_BACKUP_DIR/$STAMP.tar.gz.age"
HASH_FILE="$LOCAL_BACKUP_DIR/$STAMP.tar.sha256"
success "Encrypted backup ready: $ENCRYPTED_FILE"
success "Hash sidecar ready: $HASH_FILE ($(cat "$HASH_FILE"))"

# ---------- COPY 2: local, second disk/media ----------
if [ -n "$SECOND_DISK_DIR" ] && [ -d "$SECOND_DISK_DIR" ]; then
    cp "$ENCRYPTED_FILE" "$SECOND_DISK_DIR/"
    cp "$HASH_FILE" "$SECOND_DISK_DIR/"
    success "Copy 2 (local, second disk) done."
else
    warn "second disk path not found, skipping Copy 2."
fi

# ---------- COPY 3a: offsite via Tailscale to a device you own ----------
if [ "$USE_TAILSCALE_REMOTE" = true ]; then
    if command -v tailscale >/dev/null 2>&1; then
        if rsync -avz -e ssh "$ENCRYPTED_FILE" "$HASH_FILE" "$REMOTE_HOST:$REMOTE_PATH/"; then
            success "Copy 3a (Tailscale remote device) done."
        else
            warn "Tailscale remote sync failed."
        fi
    else
        warn "tailscale not found, skipping remote-device offsite."
    fi
fi

# ---------- COPY 3b: offsite via rclone to encrypted cloud (e.g. Backblaze B2 free tier) ----------
if [ "$USE_RCLONE_CLOUD" = true ]; then
    if command -v rclone >/dev/null 2>&1; then
        if rclone copy "$ENCRYPTED_FILE" "$RCLONE_REMOTE:$RCLONE_PATH/" --quiet \
            && rclone copy "$HASH_FILE" "$RCLONE_REMOTE:$RCLONE_PATH/" --quiet; then
            success "Copy 3b (rclone cloud) done."
        else
            warn "rclone upload failed."
        fi
    else
        warn "rclone not found, skipping cloud offsite."
    fi
fi

# ---------- Retention: prune old local backups ----------
find "$LOCAL_BACKUP_DIR" -name "${PROJECT_NAME}_*.tar.gz.age" -mtime "+$RETENTION_DAYS" -delete
find "$LOCAL_BACKUP_DIR" -name "${PROJECT_NAME}_*.tar.sha256" -mtime "+$RETENTION_DAYS" -delete
if [ -n "$SECOND_DISK_DIR" ] && [ -d "$SECOND_DISK_DIR" ]; then
    find "$SECOND_DISK_DIR" -name "${PROJECT_NAME}_*.tar.gz.age" -mtime "+$RETENTION_DAYS" -delete
    find "$SECOND_DISK_DIR" -name "${PROJECT_NAME}_*.tar.sha256" -mtime "+$RETENTION_DAYS" -delete
fi

success "Backup complete: $STAMP"
