#!/bin/bash
#
# Restore job-agent state files from an encrypted backup bundle
#
# Usage:
#   ./restore.sh /path/to/job-agent_YYYYMMDD.tar.gz.age [output_dir]  # explicit file
#   ./restore.sh --local [output_dir]                                 # latest from BACKUP_LOCAL_DIR
#   ./restore.sh --disk2 [output_dir]                                 # latest from BACKUP_SECOND_DISK_DIR
#   ./restore.sh --tailscale [output_dir]                             # latest from the Tailscale remote host
#   ./restore.sh --rclone [output_dir]                                # latest from the rclone/cloud remote
#
# The --local/--disk2/--tailscale/--rclone flags read the matching backup.sh
# .env variables and automatically pick the most recent .tar.gz.age file by
# timestamp, so you don't have to go find the filename yourself. --disk2,
# --tailscale, and --rclone print "value not set" and exit if the relevant
# .env variable(s) aren't configured, since those sources are optional.
#
# [output_dir] is a directory to extract the bundle's files into (jobs.db,
# manual_jobs.txt, preference_profile.json, processed_jobs.txt,
# seen_jobs.json - whichever were present at backup time). Defaults to
# ./restored-job-agent/. Created if it doesn't exist. Existing files of the
# same name in that directory are overwritten - back them up first if unsure.
#
# If backup.sh produced a <stamp>.tar.sha256 sidecar alongside the backup
# being restored, this script finds/fetches it automatically and verifies
# the restored tarball is byte-for-byte identical to the original, in
# addition to the PRAGMA integrity_check that always runs against jobs.db.
#
# Requires: age, gunzip, tar, sqlite3
# Optional (only for the matching flag): ssh/scp (--tailscale), rclone + python3 (--rclone)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/../.env}"

# ---------- Colors (auto-disabled if not a terminal) ----------
if [ -t 1 ]; then
    C_RED=$'\033[0;31m'; C_GREEN=$'\033[0;32m'; C_YELLOW=$'\033[0;33m'
    C_BLUE=$'\033[0;34m'; C_RESET=$'\033[0m'
else
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_RESET=""
fi
info()    { echo "${C_BLUE}$1${C_RESET}"; }
success() { echo "${C_GREEN}$1${C_RESET}"; }
warn()    { echo "${C_YELLOW}WARNING: $1${C_RESET}" >&2; }
error()   { echo "${C_RED}ERROR: $1${C_RESET}" >&2; }
# error_raw skips the "ERROR:" prefix/color-per-line for continuation lines
# in a multi-line error block, so only the first line gets the red "ERROR:"
# tag and the rest read as plain indented detail underneath it.
error_raw() { echo "${C_RED}$1${C_RESET}" >&2; }

usage() {
    error "Usage:"
    error_raw "  $0 <encrypted_backup_file> [output_dir]"
    error_raw "  $0 --local [output_dir]"
    error_raw "  $0 --disk2 [output_dir]"
    error_raw "  $0 --tailscale [output_dir]"
    error_raw "  $0 --rclone [output_dir]"
    exit 1
}

if [ $# -lt 1 ]; then
    usage
fi

SOURCE_MODE=""
LOCAL_STAGING=""
case "$1" in
    --local|--disk2|--tailscale|--rclone)
        SOURCE_MODE="$1"
        OUTPUT_DIR="${2:-./restored-job-agent}"
        ;;
    --*)
        error "unknown flag '$1'"
        usage
        ;;
    *)
        ENCRYPTED_FILE="$1"
        OUTPUT_DIR="${2:-./restored-job-agent}"
        HASH_FILE="${ENCRYPTED_FILE%.tar.gz.age}.tar.sha256"
        [ -f "$HASH_FILE" ] || HASH_FILE=""
        ;;
esac

# .env is only needed for the auto-select flags - an explicit file path
# doesn't require it, so don't fail the plain-file usage just because
# .env is missing.
if [ -n "$SOURCE_MODE" ]; then
    if [ -f "$ENV_FILE" ]; then
        set -a
        # shellcheck disable=SC1090
        source "$ENV_FILE"
        set +a
    else
        error ".env not found at $ENV_FILE (set ENV_FILE to override)"
        exit 1
    fi
fi

PROJECT_NAME="${PROJECT_NAME:-job-agent}"

# Single cleanup trap for the whole script, set once so nothing later
# overwrites it. TMP_GZ is created further down; LOCAL_STAGING is only
# created by --tailscale/--rclone. Both are safe to rm even if unset/empty
# or never created, since we guard with -n and check emptiness.
TMP_GZ=""
cleanup() {
    [ -n "$TMP_GZ" ] && rm -f "$TMP_GZ"
    [ -n "$LOCAL_STAGING" ] && rm -rf "$LOCAL_STAGING"
    return 0
}
trap cleanup EXIT

# ---------- Resolve ENCRYPTED_FILE based on the selected source ----------
case "$SOURCE_MODE" in
    --local)
        : "${BACKUP_LOCAL_DIR:?BACKUP_LOCAL_DIR must be set in .env}"
        if [ ! -d "$BACKUP_LOCAL_DIR" ]; then
            error "BACKUP_LOCAL_DIR '$BACKUP_LOCAL_DIR' does not exist."
            exit 1
        fi
        # shellcheck disable=SC2012
        ENCRYPTED_FILE=$(ls -1t "$BACKUP_LOCAL_DIR"/*.tar.gz.age 2>/dev/null | head -n 1 || true)
        if [ -z "$ENCRYPTED_FILE" ]; then
            error "no .tar.gz.age files found in $BACKUP_LOCAL_DIR"
            exit 1
        fi
        info "Latest local backup: $ENCRYPTED_FILE"
        HASH_FILE="${ENCRYPTED_FILE%.tar.gz.age}.tar.sha256"
        [ -f "$HASH_FILE" ] || HASH_FILE=""
        ;;

    --disk2)
        if [ -z "${BACKUP_SECOND_DISK_DIR:-}" ]; then
            echo "value not set"
            exit 1
        fi
        if [ ! -d "$BACKUP_SECOND_DISK_DIR" ]; then
            error "BACKUP_SECOND_DISK_DIR '$BACKUP_SECOND_DISK_DIR' does not exist."
            exit 1
        fi
        # shellcheck disable=SC2012
        ENCRYPTED_FILE=$(ls -1t "$BACKUP_SECOND_DISK_DIR"/*.tar.gz.age 2>/dev/null | head -n 1 || true)
        if [ -z "$ENCRYPTED_FILE" ]; then
            error "no .tar.gz.age files found in $BACKUP_SECOND_DISK_DIR"
            exit 1
        fi
        info "Latest disk2 backup: $ENCRYPTED_FILE"
        HASH_FILE="${ENCRYPTED_FILE%.tar.gz.age}.tar.sha256"
        [ -f "$HASH_FILE" ] || HASH_FILE=""
        ;;

    --tailscale)
        if [ -z "${BACKUP_REMOTE_HOST:-}" ] || [ -z "${BACKUP_REMOTE_PATH:-}" ]; then
            echo "value not set"
            exit 1
        fi
        if ! command -v ssh >/dev/null 2>&1; then
            error "ssh not found in PATH - required for --tailscale."
            exit 1
        fi

        # ---- Step 1: can we reach the host at all? ----
        # A trivial, fast command (`true`) that only tests connectivity/auth,
        # separately from the later listing command. This is what lets us
        # tell "can't connect" apart from "connected fine, found nothing".
        info "Connecting to $BACKUP_REMOTE_HOST ..."
        SSH_CONNECT_ERR=$(mktemp)
        if ! ssh -o ConnectTimeout=10 -o BatchMode=yes "$BACKUP_REMOTE_HOST" "true" 2>"$SSH_CONNECT_ERR"; then
            error "could not connect to $BACKUP_REMOTE_HOST via ssh."
            error_raw "  This means the connection itself failed - check that the host is"
            error_raw "  reachable (e.g. 'tailscale status'), the hostname/IP in"
            error_raw "  BACKUP_REMOTE_HOST is correct, and SSH key auth is set up."
            error_raw "  ssh said:"
            sed 's/^/    /' "$SSH_CONNECT_ERR" >&2
            rm -f "$SSH_CONNECT_ERR"
            exit 1
        fi
        rm -f "$SSH_CONNECT_ERR"
        info "Connected. Looking up the latest backup in $BACKUP_REMOTE_PATH ..."

        # ---- Step 2: connection is good - now look for a matching file ----
        REMOTE_LATEST=$(ssh -o ConnectTimeout=10 -o BatchMode=yes "$BACKUP_REMOTE_HOST" \
            "ls -1t '$BACKUP_REMOTE_PATH'/*.tar.gz.age 2>/dev/null | head -n 1" || true)
        if [ -z "$REMOTE_LATEST" ]; then
            error "connected to $BACKUP_REMOTE_HOST successfully, but found no"
            error_raw "  .tar.gz.age files in $BACKUP_REMOTE_PATH. Check BACKUP_REMOTE_PATH"
            error_raw "  points at the right directory, and that backups have actually"
            error_raw "  been synced there (see the Tailscale offsite step in backup.sh)."
            exit 1
        fi
        info "Latest Tailscale remote backup: $BACKUP_REMOTE_HOST:$REMOTE_LATEST"

        # ---- Step 3: get the remote file's checksum before transferring ----
        # sha256sum is nearly universal on Linux; fall back to shasum (macOS/BSD)
        # if it's not present. If neither exists remotely, skip verification
        # rather than failing the whole restore over a missing convenience tool.
        REMOTE_SUM=$(ssh -o ConnectTimeout=10 -o BatchMode=yes "$BACKUP_REMOTE_HOST" \
            "sha256sum '$REMOTE_LATEST' 2>/dev/null || shasum -a 256 '$REMOTE_LATEST' 2>/dev/null" \
            | awk '{print $1}')

        # ---- Step 4: fetch it ----
        LOCAL_STAGING=$(mktemp -d)
        STAGED_FILE="$LOCAL_STAGING/$(basename "$REMOTE_LATEST")"
        info "Fetching to $STAGED_FILE ..."
        if ! scp -q -o ConnectTimeout=10 "$BACKUP_REMOTE_HOST:$REMOTE_LATEST" "$STAGED_FILE"; then
            error "connected fine and found the file, but the transfer itself"
            error_raw "  failed partway through (scp error). This is different from a"
            error_raw "  connection failure or a missing file - the network dropped, disk"
            error_raw "  filled up, or permissions changed mid-transfer. Try again."
            rm -rf "$LOCAL_STAGING"
            exit 1
        fi

        # ---- Step 5: verify the transferred bytes match the remote's checksum ----
        if [ -n "$REMOTE_SUM" ]; then
            LOCAL_SUM=$(sha256sum "$STAGED_FILE" 2>/dev/null | awk '{print $1}')
            if [ "$LOCAL_SUM" != "$REMOTE_SUM" ]; then
                error "downloaded file's checksum does not match the remote's."
                error_raw "  Remote sha256: $REMOTE_SUM"
                error_raw "  Local  sha256: $LOCAL_SUM"
                error_raw "  The transfer completed but the bytes don't match what's on"
                error_raw "  $BACKUP_REMOTE_HOST - this file is corrupt. Do not trust it."
                rm -rf "$LOCAL_STAGING"
                exit 1
            fi
            success "Checksum verified: downloaded file matches remote (sha256)."
        else
            warn "could not compute a remote checksum (no sha256sum/shasum on"
            warn "  $BACKUP_REMOTE_HOST) - skipping transfer verification. The"
            warn "  decryption/integrity check below will still catch most corruption."
        fi

        ENCRYPTED_FILE="$STAGED_FILE"

        # ---- Step 6: fetch the matching hash sidecar, if backup.sh produced one ----
        # Best-effort: backups made before this feature existed won't have a
        # sidecar, so a missing one is not an error, just skips that extra check.
        REMOTE_HASH_FILE="${REMOTE_LATEST%.tar.gz.age}.tar.sha256"
        HASH_FILE="$LOCAL_STAGING/$(basename "$REMOTE_HASH_FILE")"
        if scp -q -o ConnectTimeout=10 "$BACKUP_REMOTE_HOST:$REMOTE_HASH_FILE" "$HASH_FILE" 2>/dev/null; then
            info "Fetched hash sidecar for post-restore verification."
        else
            HASH_FILE=""
        fi
        ;;

    --rclone)
        if [ -z "${BACKUP_RCLONE_REMOTE:-}" ]; then
            echo "value not set"
            exit 1
        fi
        RCLONE_PATH="${BACKUP_RCLONE_PATH:-${PROJECT_NAME}-backups}"
        if ! command -v rclone >/dev/null 2>&1; then
            error "rclone not found in PATH - required for --rclone."
            exit 1
        fi
        if ! command -v python3 >/dev/null 2>&1; then
            error "python3 not found in PATH - required for --rclone (used to parse rclone's file listing)."
            exit 1
        fi

        # ---- Step 1: can we reach/authenticate to the remote at all? ----
        # `rclone lsjson` on the target path also proves connectivity, but we
        # need to separate "the command itself failed" (network/auth/remote
        # misconfigured) from "it succeeded and returned zero matching files"
        # (right connection, wrong/empty path) - so capture stderr and check
        # rclone's own exit code before ever looking at what it returned.
        info "Connecting to $BACKUP_RCLONE_REMOTE ..."
        RCLONE_ERR=$(mktemp)
        RCLONE_JSON=$(mktemp)
        if ! rclone lsjson "$BACKUP_RCLONE_REMOTE:$RCLONE_PATH" >"$RCLONE_JSON" 2>"$RCLONE_ERR"; then
            error "could not connect to $BACKUP_RCLONE_REMOTE:$RCLONE_PATH."
            error_raw "  This means rclone itself failed to reach or authenticate to the"
            error_raw "  remote - check network connectivity, that BACKUP_RCLONE_REMOTE"
            error_raw "  matches a remote in 'rclone listremotes', and that its credentials"
            error_raw "  are still valid."
            error_raw "  rclone said:"
            sed 's/^/    /' "$RCLONE_ERR" >&2
            rm -f "$RCLONE_ERR" "$RCLONE_JSON"
            exit 1
        fi
        rm -f "$RCLONE_ERR"
        info "Connected. Looking up the latest backup in $RCLONE_PATH ..."

        # ---- Step 2: connection is good - now look for a matching file ----
        REMOTE_LATEST=$(python3 -c "
import json, sys
try:
    with open('$RCLONE_JSON') as f:
        items = json.load(f)
except (json.JSONDecodeError, ValueError, OSError):
    items = []
matches = [i for i in items if i['Path'].endswith('.tar.gz.age')]
if matches:
    matches.sort(key=lambda i: i['ModTime'], reverse=True)
    print(matches[0]['Path'])
" || true)
        rm -f "$RCLONE_JSON"
        if [ -z "$REMOTE_LATEST" ]; then
            error "connected to $BACKUP_RCLONE_REMOTE successfully, but found no"
            error_raw "  .tar.gz.age files in $RCLONE_PATH. Check BACKUP_RCLONE_PATH includes"
            error_raw "  the bucket name as its first segment, and that backups have"
            error_raw "  actually been uploaded there (see the rclone offsite step in"
            error_raw "  backup.sh)."
            exit 1
        fi
        info "Latest rclone backup: $BACKUP_RCLONE_REMOTE:$RCLONE_PATH/$REMOTE_LATEST"

        # ---- Step 3: fetch it ----
        LOCAL_STAGING=$(mktemp -d)
        STAGED_FILE="$LOCAL_STAGING/$(basename "$REMOTE_LATEST")"
        info "Fetching to $STAGED_FILE ..."
        if ! rclone copyto "$BACKUP_RCLONE_REMOTE:$RCLONE_PATH/$REMOTE_LATEST" "$STAGED_FILE" --quiet; then
            error "connected fine and found the file, but the transfer itself"
            error_raw "  failed partway through (rclone copy error). This is different"
            error_raw "  from a connection failure or a missing file - the network dropped,"
            error_raw "  local disk filled up, or permissions changed mid-transfer. Try again."
            rm -rf "$LOCAL_STAGING"
            exit 1
        fi

        # ---- Step 4: verify the transferred bytes against the remote's own hash ----
        # `rclone check` compares the local file against the remote object
        # using whatever hash the backend supports (B2 uses SHA1). This
        # catches transfer corruption that a bare copy wouldn't surface.
        info "Verifying download integrity ..."
        if rclone check "$LOCAL_STAGING" "$BACKUP_RCLONE_REMOTE:$RCLONE_PATH" \
            --include "$(basename "$REMOTE_LATEST")" --quiet 2>/dev/null; then
            success "Checksum verified: downloaded file matches the remote."
        else
            error "downloaded file failed rclone's checksum comparison against"
            error_raw "  $BACKUP_RCLONE_REMOTE:$RCLONE_PATH - the transfer completed but the"
            error_raw "  bytes don't match what's stored remotely. This file is corrupt."
            error_raw "  Do not trust it."
            rm -rf "$LOCAL_STAGING"
            exit 1
        fi

        ENCRYPTED_FILE="$STAGED_FILE"

        # ---- Step 5: fetch the matching hash sidecar, if backup.sh produced one ----
        # Best-effort: backups made before this feature existed won't have a
        # sidecar, so a missing one is not an error, just skips that extra check.
        REMOTE_HASH_PATH="${REMOTE_LATEST%.tar.gz.age}.tar.sha256"
        HASH_FILE="$LOCAL_STAGING/$(basename "$REMOTE_HASH_PATH")"
        if rclone copyto "$BACKUP_RCLONE_REMOTE:$RCLONE_PATH/$REMOTE_HASH_PATH" "$HASH_FILE" --quiet 2>/dev/null; then
            info "Fetched hash sidecar for post-restore verification."
        else
            HASH_FILE=""
        fi
        ;;
esac

# ---------- Prepare output directory ----------
mkdir -p "$OUTPUT_DIR"
info "Restoring into: $OUTPUT_DIR"

# AGE_KEY_PATH is intentionally NOT read from .env - the private key should
# never live in the repo's env file. Pass it explicitly or rely on the default.
AGE_KEY="${AGE_KEY_PATH:-$HOME/.age/job-agent-backup-key.txt}"

if [ ! -f "$AGE_KEY" ]; then
    error "age private key not found at $AGE_KEY"
    error_raw "Set AGE_KEY_PATH env var if it's stored elsewhere."
    exit 1
fi

TMP_GZ=$(mktemp)
TMP_TAR=$(mktemp)

info "Decrypting $ENCRYPTED_FILE ..."
age -d -i "$AGE_KEY" -o "$TMP_GZ" "$ENCRYPTED_FILE"

info "Decompressing ..."
gunzip -c "$TMP_GZ" > "$TMP_TAR"

# Compare the decrypted+decompressed tarball against the backup-time hash,
# if a sidecar was found/fetched, BEFORE extracting anything. This proves
# the bundle is byte-for-byte identical to what backup.sh originally
# produced, prior to any files being written into OUTPUT_DIR.
if [ -n "${HASH_FILE:-}" ] && [ -f "$HASH_FILE" ]; then
    EXPECTED_SUM=$(tr -d '[:space:]' < "$HASH_FILE")
    ACTUAL_SUM=$(sha256sum "$TMP_TAR" | awk '{print $1}')
    if [ "$ACTUAL_SUM" != "$EXPECTED_SUM" ]; then
        error "restored bundle does not match the hash recorded at backup time."
        error_raw "  Expected sha256: $EXPECTED_SUM"
        error_raw "  Actual   sha256: $ACTUAL_SUM"
        error_raw "  Refusing to extract. Do not trust this restore."
        exit 1
    fi
    success "Hash verified: bundle matches the original backup exactly (sha256)."
else
    info "NOTE: no hash sidecar found for this backup - skipping byte-for-byte"
    info "  verification against the original. (Backups made before this feature"
    info "  existed won't have one.)"
fi

info "Extracting into $OUTPUT_DIR ..."
tar -xf "$TMP_TAR" -C "$OUTPUT_DIR"

info "Listing restored files:"
tar -tf "$TMP_TAR" | sed 's/^/  /'

# Verify jobs.db specifically, if it was part of this bundle
RESTORED_DB="$OUTPUT_DIR/jobs.db"
if [ -f "$RESTORED_DB" ]; then
    info "Verifying jobs.db integrity ..."
    INTEGRITY=$(sqlite3 "$RESTORED_DB" "PRAGMA integrity_check;")
    if [ "$INTEGRITY" != "ok" ]; then
        warn "jobs.db integrity check returned: $INTEGRITY"
        exit 1
    fi
    success "jobs.db integrity check: ok"
else
    warn "no jobs.db found in this bundle - nothing to run PRAGMA integrity_check against."
fi

success "Restored successfully to: $OUTPUT_DIR"
