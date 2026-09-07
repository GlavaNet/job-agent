#!/bin/bash
#
# Install logrotate config for the cron backup log
#
# Only relevant if you're using install-backup-cron.sh (systemd's journald
# already handles rotation for the systemd path - see install-backup-systemd.sh).
#
# Writes /etc/logrotate.d/<project-name>-backup pointing at the same backup.log
# that install-backup-cron.sh redirects output to. Requires root.
#
# Usage:
#   sudo ./scripts/install-backup-logrotate.sh                 # install/update
#   sudo ./scripts/install-backup-logrotate.sh --weeks 12      # keep 12 rotations instead of default
#   sudo ./scripts/install-backup-logrotate.sh --name myapp    # override the config name
#   sudo ./scripts/install-backup-logrotate.sh --uninstall

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/../.env}"
LOG_FILE="$SCRIPT_DIR/../backup.log"

# Derived from PROJECT_NAME in .env (same variable backup.sh uses), so this
# config's name matches the unit/cron naming used elsewhere. --name overrides.
PROJECT_NAME="job-agent"
if [ -f "$ENV_FILE" ]; then
    ENV_PROJECT_NAME=$(grep -E '^PROJECT_NAME=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2-)
    [ -n "$ENV_PROJECT_NAME" ] && PROJECT_NAME="$ENV_PROJECT_NAME"
fi
CONFIG_NAME="${PROJECT_NAME}-backup"
KEEP_ROTATIONS=14   # ~2 weeks of daily-log history by default (backups run daily here, unlike CRM's weekly)

UNINSTALL=false
while [ $# -gt 0 ]; do
    case "$1" in
        --weeks)
            KEEP_ROTATIONS="$2"
            shift 2
            ;;
        --name)
            CONFIG_NAME="$2"
            shift 2
            ;;
        --uninstall)
            UNINSTALL=true
            shift
            ;;
        *)
            echo "Unknown argument: $1" >&2
            echo "Usage: $0 [--weeks N] [--name config-name] [--uninstall]" >&2
            exit 1
            ;;
    esac
done

CONFIG_FILE="/etc/logrotate.d/${CONFIG_NAME}"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: this script needs root (writes to /etc/logrotate.d). Try: sudo $0 $*" >&2
    exit 1
fi

if [ "$UNINSTALL" = true ]; then
    if [ -f "$CONFIG_FILE" ]; then
        rm -f "$CONFIG_FILE"
        echo "Removed $CONFIG_FILE."
    else
        echo "No logrotate config found at $CONFIG_FILE - nothing to remove."
    fi
    exit 0
fi

if ! command -v logrotate >/dev/null 2>&1; then
    echo "ERROR: logrotate not found. Install it first: apt install logrotate / brew install logrotate" >&2
    exit 1
fi

# Resolve to an absolute path with no ".." components - logrotate needs a
# real path, and this also makes the config readable on its own.
LOG_FILE_ABS="$(cd "$(dirname "$LOG_FILE")" && pwd)/$(basename "$LOG_FILE")"

cat > "$CONFIG_FILE" << EOF
$LOG_FILE_ABS {
    weekly
    rotate $KEEP_ROTATIONS
    compress
    delaycompress
    missingok
    notifempty
    create 0644 root root
}
EOF

echo "Installed logrotate config: $CONFIG_FILE"
echo ""
cat "$CONFIG_FILE"
echo ""
echo "Rotating: $LOG_FILE_ABS"
echo "Keeping: $KEEP_ROTATIONS rotations (weekly), compressed after the first rotation"
echo ""
echo "Test it without waiting for the schedule:"
echo "  sudo logrotate --force --debug $CONFIG_FILE   # dry run, --force triggers even if not due"
echo "  sudo logrotate --force $CONFIG_FILE            # actually rotate now"
echo ""
echo "Uninstall with: sudo $0 --uninstall"
echo ""
echo "NOTE: this only applies to the cron-based backup log ($LOG_FILE_ABS)."
echo "If you're using the systemd timer instead, logs go to journald, which"
echo "handles its own rotation/retention - no logrotate config needed there."
