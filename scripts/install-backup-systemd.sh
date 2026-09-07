#!/bin/bash
#
# Install daily job-agent state backup via systemd (service + timer)
#
# Generates and installs a systemd service + timer unit that runs
# scripts/backup.sh daily. Requires root (systemd system units live in
# /etc/systemd/system and need a privileged reload/enable).
#
# Idempotent - safe to re-run; it overwrites the units it previously wrote.
#
# Usage:
#   sudo ./scripts/install-backup-systemd.sh                       # default: daily, 02:00
#   sudo ./scripts/install-backup-systemd.sh --schedule "*-*-* 03:00:00"  # custom OnCalendar
#   sudo ./scripts/install-backup-systemd.sh --user chris          # run backup.sh as this user
#   sudo ./scripts/install-backup-systemd.sh --name myapp          # override the unit name
#   sudo ./scripts/install-backup-systemd.sh --uninstall

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/../.env}"
BACKUP_SCRIPT="$SCRIPT_DIR/backup.sh"

# The unit name is derived from PROJECT_NAME in .env (same variable backup.sh
# uses for filenames), so it lines up with what backup.sh actually produces.
# --name overrides this explicitly if you want something different.
PROJECT_NAME="job-agent"
if [ -f "$ENV_FILE" ]; then
    ENV_PROJECT_NAME=$(grep -E '^PROJECT_NAME=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)
    [ -n "$ENV_PROJECT_NAME" ] && PROJECT_NAME="$ENV_PROJECT_NAME"
fi
UNIT_NAME="${PROJECT_NAME}-backup"

SCHEDULE="*-*-* 02:00:00"   # systemd OnCalendar syntax, default: daily at 02:00
RUN_AS_USER="${SUDO_USER:-$(whoami)}"
UNINSTALL=false

while [ $# -gt 0 ]; do
    case "$1" in
        --schedule)
            SCHEDULE="$2"
            shift 2
            ;;
        --user)
            RUN_AS_USER="$2"
            shift 2
            ;;
        --name)
            UNIT_NAME="$2"
            shift 2
            ;;
        --uninstall)
            UNINSTALL=true
            shift
            ;;
        *)
            echo "Unknown argument: $1" >&2
            echo "Usage: $0 [--schedule \"OnCalendar expression\"] [--user username] [--name unit-name] [--uninstall]" >&2
            exit 1
            ;;
    esac
done

SERVICE_FILE="/etc/systemd/system/${UNIT_NAME}.service"
TIMER_FILE="/etc/systemd/system/${UNIT_NAME}.timer"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: this script needs root (systemd system units require it). Try: sudo $0 $*" >&2
    exit 1
fi

if [ "$UNINSTALL" = true ]; then
    systemctl disable --now "${UNIT_NAME}.timer" 2>/dev/null || true
    rm -f "$SERVICE_FILE" "$TIMER_FILE"
    systemctl daemon-reload
    echo "Removed ${UNIT_NAME}.service and ${UNIT_NAME}.timer."
    exit 0
fi

if [ ! -x "$BACKUP_SCRIPT" ]; then
    echo "ERROR: $BACKUP_SCRIPT not found or not executable." >&2
    exit 1
fi

if ! id "$RUN_AS_USER" >/dev/null 2>&1; then
    echo "ERROR: user '$RUN_AS_USER' does not exist. Use --user to specify a valid user." >&2
    exit 1
fi

cat > "$SERVICE_FILE" << EOF
[Unit]
Description=${PROJECT_NAME} state backup (3-2-1 rule)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_AS_USER
ExecStart=$BACKUP_SCRIPT
# Hardening - loosen these if backup.sh needs broader access (e.g. writing
# outside \$HOME for BACKUP_LOCAL_DIR or BACKUP_SECOND_DISK_DIR)
NoNewPrivileges=true
ProtectSystem=full
EOF

cat > "$TIMER_FILE" << EOF
[Unit]
Description=Daily timer for ${PROJECT_NAME} backup

[Timer]
OnCalendar=$SCHEDULE
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now "${UNIT_NAME}.timer"

echo "Installed and enabled:"
echo "  $SERVICE_FILE"
echo "  $TIMER_FILE"
echo ""
echo "Runs as user: $RUN_AS_USER"
echo "Schedule (OnCalendar): $SCHEDULE"
echo ""
echo "Useful commands:"
echo "  systemctl status ${UNIT_NAME}.timer      # confirm it's active and see next run"
echo "  systemctl list-timers ${UNIT_NAME}.timer # see next scheduled run time"
echo "  sudo systemctl start ${UNIT_NAME}.service # run a backup right now"
echo "  journalctl -u ${UNIT_NAME}.service        # view backup logs"
echo "  sudo $0 --uninstall                       # remove"
echo ""
echo "NOTE: ProtectSystem=full in the service makes most of the filesystem"
echo "read-only to this unit except \$HOME and a few standard paths. If"
echo "BACKUP_LOCAL_DIR, BACKUP_SECOND_DISK_DIR, or JOBAGENT_DATA_DIR live"
echo "outside $RUN_AS_USER's home directory, edit $SERVICE_FILE and either"
echo "add a ReadWritePaths= line for those directories or remove"
echo "ProtectSystem=full, then run: sudo systemctl daemon-reload"
