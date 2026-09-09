#!/bin/bash
#
# Backup dependency checker
#
# Verifies the system binaries and .env config that scripts/backup.sh and
# scripts/restore.sh need are present, without actually running a backup.
# Safe to run any time - read-only, touches no data.
#
# Usage: ./scripts/check-deps.sh

set -uo pipefail  # deliberately no -e: we want to keep checking after failures

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/../.env}"

FAILED=0
WARNED=0

pass() { echo "  OK    $1"; }
fail() { echo "  FAIL  $1"; FAILED=1; }
warn() { echo "  WARN  $1"; WARNED=1; }

echo "=== Checking .env ==="
if [ -f "$ENV_FILE" ]; then
    pass ".env found at $ENV_FILE"
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
else
    fail ".env not found at $ENV_FILE (set ENV_FILE to override)"
fi
echo ""

echo "=== Checking always-required binaries ==="
for bin in sqlite3 tar age gzip; do
    if command -v "$bin" >/dev/null 2>&1; then
        pass "$bin ($(command -v "$bin"))"
    else
        fail "$bin not found in PATH"
    fi
done
echo ""

echo "=== Checking always-required .env values ==="
DATA_DIR="${JOBAGENT_DATA_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
[ -n "${BACKUP_LOCAL_DIR:-}" ]    && pass "BACKUP_LOCAL_DIR is set"    || fail "BACKUP_LOCAL_DIR is not set"
[ -n "${BACKUP_AGE_RECIPIENT:-}" ] && pass "BACKUP_AGE_RECIPIENT is set" || fail "BACKUP_AGE_RECIPIENT is not set"

echo ""
echo "=== Checking state directory in $DATA_DIR ==="
if [ -d "$DATA_DIR" ]; then
    pass "state directory found: $DATA_DIR"
    if [ -f "$DATA_DIR/jobs.db" ]; then
        pass "jobs.db found"
    else
        fail "jobs.db not found in $DATA_DIR - backup.sh requires this file"
    fi
else
    fail "JOBAGENT_DATA_DIR is set to $DATA_DIR but that directory doesn't exist"
fi
echo ""

echo "=== Checking age key ==="
AGE_KEY="${AGE_KEY_PATH:-$HOME/.age/job-agent-backup-key.txt}"
if [ -f "$AGE_KEY" ]; then
    PERMS=$(stat -c '%a' "$AGE_KEY" 2>/dev/null || stat -f '%Lp' "$AGE_KEY" 2>/dev/null)
    if [ "$PERMS" = "600" ]; then
        pass "private key found at $AGE_KEY (permissions 600)"
    else
        warn "private key found at $AGE_KEY but permissions are $PERMS, not 600 (run: chmod 600 $AGE_KEY)"
    fi
else
    warn "no private key at $AGE_KEY - fine if backups haven't been set up yet, but restore.sh will need this"
fi
echo ""

echo "=== Checking Copy 2 (local second disk) ==="
if [ -n "${BACKUP_SECOND_DISK_DIR:-}" ]; then
    if [ -d "$BACKUP_SECOND_DISK_DIR" ]; then
        pass "second disk dir found at $BACKUP_SECOND_DISK_DIR"
    else
        warn "BACKUP_SECOND_DISK_DIR is set to $BACKUP_SECOND_DISK_DIR but that path doesn't exist"
    fi
else
    warn "BACKUP_SECOND_DISK_DIR not set - Copy 2 (local, second media) will be skipped"
fi
echo ""

echo "=== Checking offsite methods ==="
USE_TAILSCALE_REMOTE="${BACKUP_USE_TAILSCALE:-false}"
USE_RCLONE_CLOUD="${BACKUP_USE_RCLONE:-false}"

if [ "$USE_TAILSCALE_REMOTE" = true ]; then
    echo "  Tailscale offsite is enabled:"
    command -v tailscale >/dev/null 2>&1 && pass "  tailscale binary found" || fail "  tailscale binary not found"
    command -v rsync >/dev/null 2>&1 && pass "  rsync binary found" || fail "  rsync binary not found"
    command -v ssh >/dev/null 2>&1 && pass "  ssh binary found" || fail "  ssh binary not found"
    [ -n "${BACKUP_REMOTE_HOST:-}" ] && pass "  BACKUP_REMOTE_HOST is set" || fail "  BACKUP_REMOTE_HOST is not set"
    [ -n "${BACKUP_REMOTE_PATH:-}" ] && pass "  BACKUP_REMOTE_PATH is set" || fail "  BACKUP_REMOTE_PATH is not set"
    if command -v tailscale >/dev/null 2>&1; then
        if tailscale status >/dev/null 2>&1; then
            pass "  tailscale appears to be running/connected"
        else
            warn "  tailscale binary found but 'tailscale status' failed - is it connected?"
        fi
    fi
else
    echo "  Tailscale offsite is disabled (BACKUP_USE_TAILSCALE != true)"
fi
echo ""

if [ "$USE_RCLONE_CLOUD" = true ]; then
    echo "  rclone offsite is enabled:"
    if command -v rclone >/dev/null 2>&1; then
        pass "  rclone binary found"
        if [ -n "${BACKUP_RCLONE_REMOTE:-}" ]; then
            pass "  BACKUP_RCLONE_REMOTE is set to '$BACKUP_RCLONE_REMOTE'"
            if rclone listremotes 2>/dev/null | grep -q "^${BACKUP_RCLONE_REMOTE}:$"; then
                pass "  rclone remote '$BACKUP_RCLONE_REMOTE' is configured"
            else
                fail "  rclone remote '$BACKUP_RCLONE_REMOTE' not found in 'rclone listremotes' - run 'rclone config'"
            fi
        else
            fail "  BACKUP_RCLONE_REMOTE is not set"
        fi
    else
        fail "  rclone binary not found"
    fi
else
    echo "  rclone offsite is disabled (BACKUP_USE_RCLONE != true)"
fi
echo ""

if [ "$USE_TAILSCALE_REMOTE" != true ] && [ "$USE_RCLONE_CLOUD" != true ]; then
    warn "no offsite method enabled - this setup will NOT satisfy the 3-2-1 rule's offsite requirement"
fi

echo "=== Summary ==="
if [ "$FAILED" -ne 0 ]; then
    echo "Result: FAIL - fix the FAIL items above before relying on backup.sh"
    exit 1
elif [ "$WARNED" -ne 0 ]; then
    echo "Result: OK with warnings - review the WARN items above"
    exit 0
else
    echo "Result: all checks passed"
    exit 0
fi
