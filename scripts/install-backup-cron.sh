#!/bin/bash
#
# Install daily job-agent state backup via cron
#
# Adds a cron entry that runs scripts/backup.sh daily. Idempotent - safe to
# re-run; it replaces any previous entry it installed rather than duplicating it.
#
# Usage:
#   ./scripts/install-backup-cron.sh              # install/update, defaults to daily 02:00
#   ./scripts/install-backup-cron.sh --schedule "0 3 * * *"   # custom cron schedule
#   ./scripts/install-backup-cron.sh --uninstall  # remove the cron entry

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/../.env}"
BACKUP_SCRIPT="$SCRIPT_DIR/backup.sh"
LOG_FILE="$SCRIPT_DIR/../backup.log"
SCHEDULE="0 2 * * *"             # default: daily 02:00

# PROJECT_NAME comes from .env if present, so the cron marker/tag matches
# whatever backup.sh itself uses for filenames. Falls back to "job-agent".
PROJECT_NAME="job-agent"
if [ -f "$ENV_FILE" ]; then
    ENV_PROJECT_NAME=$(grep -E '^PROJECT_NAME=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)
    [ -n "$ENV_PROJECT_NAME" ] && PROJECT_NAME="$ENV_PROJECT_NAME"
fi
MARKER="# ${PROJECT_NAME}-backup"   # tags our line so we can find/replace/remove it safely

UNINSTALL=false
while [ $# -gt 0 ]; do
    case "$1" in
        --schedule)
            SCHEDULE="$2"
            shift 2
            ;;
        --uninstall)
            UNINSTALL=true
            shift
            ;;
        *)
            echo "Unknown argument: $1" >&2
            echo "Usage: $0 [--schedule \"cron expression\"] [--uninstall]" >&2
            exit 1
            ;;
    esac
done

if [ ! -x "$BACKUP_SCRIPT" ]; then
    echo "ERROR: $BACKUP_SCRIPT not found or not executable." >&2
    exit 1
fi

CRON_LINE="$SCHEDULE $BACKUP_SCRIPT >> $LOG_FILE 2>&1 $MARKER"

if [ "$UNINSTALL" = true ]; then
    if crontab -l 2>/dev/null | grep -qF "$MARKER"; then
        crontab -l 2>/dev/null | grep -vF "$MARKER" | crontab -
        echo "Removed backup cron entry ($MARKER)."
    else
        echo "No backup cron entry found ($MARKER) - nothing to remove."
    fi
    exit 0
fi

# Build the new crontab: existing entries (minus any prior copy of ours) + our line
EXISTING=$(crontab -l 2>/dev/null | grep -vF "$MARKER" || true)
{
    if [ -n "$EXISTING" ]; then
        printf '%s\n' "$EXISTING"
    fi
    printf '%s\n' "$CRON_LINE"
} | crontab -

echo "Installed cron entry:"
echo "  $CRON_LINE"
echo ""
echo "Schedule: $SCHEDULE"
echo "Logs will be written to: $LOG_FILE"
echo ""
echo "Verify with: crontab -l"
echo "Uninstall with: $0 --uninstall"
echo ""
echo "NOTE: cron jobs run with a minimal environment and may not have your"
echo "normal PATH. If backup.sh reports missing binaries when run via cron"
echo "but works fine when run manually, check /etc/crontab or run"
echo "'crontab -e' and add a PATH= line at the top, or use absolute paths."
