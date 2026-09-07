#!/bin/bash
#
# job-agent backup setup script
#
# - Installs the system binaries scripts/backup.sh & friends need
#   (sqlite3, tar, age, gzip, plus optional rclone/tailscale/rsync/logrotate)
# - Prompts to set up a daily backup scheduler (cron or systemd)
#
# Colorized terminal output + a full timestamped log written to
# setup.log in the repo root. Safe to re-run.
#
# Works from either location:
#   ./setup.sh                            # run directly from the repo root
#   ./scripts/setup.sh                    # or from inside scripts/
#
# Usage:
#   ./setup.sh                            # install everything, prompt before sudo actions
#   ./setup.sh --yes                      # don't prompt, assume yes to all installs
#   ./setup.sh --skip-system              # only do the scheduler step

set -uo pipefail   # deliberately no -e: we want to run every step and report all failures

# ---------- Paths ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "$(basename "$SCRIPT_DIR")" = "scripts" ]; then
    REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
else
    REPO_ROOT="$SCRIPT_DIR"
fi
LOG_FILE="$REPO_ROOT/setup.log"

# ---------- Colors (auto-disabled if not a terminal) ----------
if [ -t 1 ]; then
    C_RED=$'\033[0;31m'; C_GREEN=$'\033[0;32m'; C_YELLOW=$'\033[0;33m'
    C_BLUE=$'\033[0;34m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""; C_RESET=""
fi

# ---------- Args ----------
ASSUME_YES=false
SKIP_SYSTEM=false
while [ $# -gt 0 ]; do
    case "$1" in
        --yes|-y)      ASSUME_YES=true; shift ;;
        --skip-system) SKIP_SYSTEM=true; shift ;;
        *)
            echo "Unknown argument: $1" >&2
            echo "Usage: $0 [--yes] [--skip-system]" >&2
            exit 1
            ;;
    esac
done

# ---------- Logging ----------
: > "$LOG_FILE"
log_line() {
    printf '[%s] %-5s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$2" >> "$LOG_FILE"
}
info()    { echo "${C_BLUE}[INFO]${C_RESET} $1";                  log_line "INFO"  "$1"; }
success() { echo "${C_GREEN}[ OK ]${C_RESET} $1";                 log_line "OK"    "$1"; }
warn()    { echo "${C_YELLOW}[WARN]${C_RESET} $1" >&2;            log_line "WARN"  "$1"; }
error()   { echo "${C_RED}[FAIL]${C_RESET} $1" >&2;               log_line "FAIL"  "$1"; }
header()  { echo ""; echo "${C_BOLD}== $1 ==${C_RESET}"; log_line "----" "== $1 =="; }

ERRORS=0

confirm() {
    if [ "$ASSUME_YES" = true ] || [ ! -t 0 ]; then
        return 0
    fi
    read -r -p "$1 [y/N] " reply
    case "$reply" in
        [yY][eE][sS]|[yY]) return 0 ;;
        *) return 1 ;;
    esac
}

header "job-agent backup setup starting"
info "Repo root: $REPO_ROOT"
info "Log file:  $LOG_FILE"

# =========================================================================
# PART 1: system binaries for the backup scripts
# =========================================================================
if [ "$SKIP_SYSTEM" = true ]; then
    header "Skipping system dependency installation (--skip-system)"
else
    header "System dependencies (for scripts/backup.sh and friends)"

    # Required by backup.sh / restore.sh always. Others are optional,
    # needed only if you enable that particular offsite method.
    # (logrotate is not needed for the timer path - backup.sh self-rotates
    # its own log file; it's only useful alongside the cron installer.)
    REQUIRED_BINS="sqlite3 tar age gzip"
    OPTIONAL_BINS="rclone tailscale rsync"

    # ---- Detect package manager ----
    PKG_MANAGER=""
    if command -v apt-get >/dev/null 2>&1; then
        PKG_MANAGER="apt"
    elif command -v dnf >/dev/null 2>&1; then
        PKG_MANAGER="dnf"
    elif command -v yum >/dev/null 2>&1; then
        PKG_MANAGER="yum"
    elif command -v brew >/dev/null 2>&1; then
        PKG_MANAGER="brew"
    elif command -v pacman >/dev/null 2>&1; then
        PKG_MANAGER="pacman"
    fi

    if [ -z "$PKG_MANAGER" ]; then
        warn "No supported package manager found (looked for apt-get, dnf, yum, brew, pacman)."
        warn "Install these manually: $REQUIRED_BINS (required), $OPTIONAL_BINS (optional)."
        ERRORS=$((ERRORS + 1))
    else
        info "Detected package manager: $PKG_MANAGER"

        pkg_name_for() {
            local bin="$1"
            case "$PKG_MANAGER:$bin" in
                apt:age)      echo "age" ;;
                dnf:age|yum:age) echo "age" ;;
                pacman:age)   echo "age" ;;
                brew:age)     echo "age" ;;
                *)            echo "$bin" ;;
            esac
        }

        SUDO=""
        if [ "$(id -u)" -ne 0 ]; then
            if command -v sudo >/dev/null 2>&1; then
                SUDO="sudo"
            else
                warn "Not running as root and 'sudo' is not installed - package installs will likely fail."
            fi
        fi

        install_pkg() {
            local pkg="$1"
            case "$PKG_MANAGER" in
                apt)    $SUDO apt-get install -y "$pkg" ;;
                dnf)    $SUDO dnf install -y "$pkg" ;;
                yum)    $SUDO yum install -y "$pkg" ;;
                brew)   brew install "$pkg" ;;
                pacman) $SUDO pacman -S --noconfirm "$pkg" ;;
            esac
        }

        NEED_UPDATE_APT=false

        for bin in $REQUIRED_BINS; do
            if command -v "$bin" >/dev/null 2>&1; then
                success "$bin already installed ($(command -v "$bin"))"
                continue
            fi
            pkg=$(pkg_name_for "$bin")
            if confirm "Install required dependency '$pkg' via $PKG_MANAGER?"; then
                info "Installing $pkg ..."
                if [ "$PKG_MANAGER" = "apt" ] && [ "$NEED_UPDATE_APT" = false ]; then
                    $SUDO apt-get update -y >> "$LOG_FILE" 2>&1
                    NEED_UPDATE_APT=true
                fi
                if install_pkg "$pkg" >> "$LOG_FILE" 2>&1; then
                    success "$bin installed"
                else
                    error "Failed to install $bin - see $LOG_FILE for details"
                    ERRORS=$((ERRORS + 1))
                fi
            else
                warn "Skipped installing $bin - backup.sh will fail until this is installed."
            fi
        done

        for bin in $OPTIONAL_BINS; do
            if command -v "$bin" >/dev/null 2>&1; then
                success "$bin already installed ($(command -v "$bin"))"
                continue
            fi
            pkg=$(pkg_name_for "$bin")
            if confirm "Install optional dependency '$pkg' via $PKG_MANAGER? (needed for Tailscale/rclone offsite)"; then
                info "Installing $pkg ..."
                if [ "$PKG_MANAGER" = "apt" ] && [ "$NEED_UPDATE_APT" = false ]; then
                    $SUDO apt-get update -y >> "$LOG_FILE" 2>&1
                    NEED_UPDATE_APT=true
                fi
                if install_pkg "$pkg" >> "$LOG_FILE" 2>&1; then
                    success "$bin installed"
                else
                    warn "Failed to install $bin - see $LOG_FILE for details (only needed if you use that offsite method)"
                fi
            else
                info "Skipped $bin (only needed if you enable that offsite method)."
            fi
        done

        if [ "$PKG_MANAGER" = "brew" ] && ! command -v age >/dev/null 2>&1; then
            warn "If 'age' isn't found via brew, try: brew install age"
        fi

        if ! command -v rclone >/dev/null 2>&1; then
            info "Note: rclone needs its own one-time interactive setup for Backblaze B2:"
            info "  rclone config"
            info "  -> n) New remote -> name it (e.g. b2backup) -> type: b2"
            info "  -> paste your B2 application key ID / application key"
            info "  Then set BACKUP_RCLONE_REMOTE=b2backup in .env."
        fi

        if ! command -v tailscale >/dev/null 2>&1; then
            info "Note: tailscale needs to be installed and logged in on BOTH this"
            info "  machine and the Raspberry Pi, with SSH key auth set up so"
            info "  'ssh user@raspberrypi' works non-interactively (BatchMode)."
        fi
    fi
fi

# =========================================================================
# PART 2: age key generation
# =========================================================================
header "age encryption key"
AGE_KEY="$HOME/.age/job-agent-backup-key.txt"
if [ -f "$AGE_KEY" ]; then
    success "age key already exists at $AGE_KEY"
else
    if command -v age-keygen >/dev/null 2>&1; then
        if confirm "No age key found. Generate one now at $AGE_KEY?"; then
            mkdir -p "$HOME/.age"
            if age-keygen -o "$AGE_KEY" 2>>"$LOG_FILE"; then
                chmod 600 "$AGE_KEY"
                success "age key generated at $AGE_KEY (permissions 600)"
                PUBKEY=$(grep -oE '^# public key: .*' "$AGE_KEY" | sed 's/# public key: //' || true)
                if [ -z "$PUBKEY" ]; then
                    PUBKEY=$(grep -oE 'age1[a-z0-9]+' "$AGE_KEY" | head -1 || true)
                fi
                if [ -n "$PUBKEY" ]; then
                    echo ""
                    echo "  Public key (put this in BACKUP_AGE_RECIPIENT in .env):"
                    echo "  ${C_BOLD}${PUBKEY}${C_RESET}"
                    echo ""
                fi
                warn "IMPORTANT: back up $AGE_KEY itself somewhere separate from your"
                warn "  backups (password manager, printed copy, etc.) - lose it and"
                warn "  every encrypted backup becomes permanently unrecoverable."
            else
                error "age-keygen failed - see $LOG_FILE for details"
                ERRORS=$((ERRORS + 1))
            fi
        else
            info "Skipped key generation - set BACKUP_AGE_RECIPIENT manually once you have a key."
        fi
    else
        warn "age-keygen not found (age may not be installed yet) - re-run this script after installing age."
    fi
fi

# =========================================================================
# PART 3: backup scheduler (cron or systemd) - detect, prompt if missing
# =========================================================================
header "Backup scheduler"

SCRIPTS_DIR="$REPO_ROOT/scripts"
CRON_INSTALLER="$SCRIPTS_DIR/install-backup-cron.sh"
SYSTEMD_INSTALLER="$SCRIPTS_DIR/install-backup-systemd.sh"

ENV_FILE_FOR_SCHEDULER="$REPO_ROOT/.env"
SCHED_PROJECT_NAME="job-agent"
if [ -f "$ENV_FILE_FOR_SCHEDULER" ]; then
    ENV_PROJECT_NAME=$(grep -E '^PROJECT_NAME=' "$ENV_FILE_FOR_SCHEDULER" 2>/dev/null | tail -1 | cut -d= -f2- || true)
    [ -n "$ENV_PROJECT_NAME" ] && SCHED_PROJECT_NAME="$ENV_PROJECT_NAME"
fi
CRON_MARKER="# ${SCHED_PROJECT_NAME}-backup"
SYSTEMD_UNIT="${SCHED_PROJECT_NAME}-backup"

CRON_FOUND=false
if command -v crontab >/dev/null 2>&1 && crontab -l 2>/dev/null | grep -qF "$CRON_MARKER"; then
    CRON_FOUND=true
fi

SYSTEMD_FOUND=false
if command -v systemctl >/dev/null 2>&1 && [ -f "/etc/systemd/system/${SYSTEMD_UNIT}.timer" ]; then
    SYSTEMD_FOUND=true
fi

if [ "$CRON_FOUND" = true ] && [ "$SYSTEMD_FOUND" = true ]; then
    warn "Both a cron entry and a systemd timer are installed for '${SYSTEMD_UNIT}' -"
    warn "  backups will run twice. Consider removing one."
elif [ "$CRON_FOUND" = true ]; then
    success "Cron entry already installed for backups ('$CRON_MARKER' in crontab)."
elif [ "$SYSTEMD_FOUND" = true ]; then
    success "Systemd timer already installed for backups (${SYSTEMD_UNIT}.timer)."
else
    info "No backup scheduler (cron or systemd) is currently installed."

    if [ ! -x "$CRON_INSTALLER" ] && [ ! -x "$SYSTEMD_INSTALLER" ]; then
        warn "Neither install-backup-cron.sh nor install-backup-systemd.sh found in"
        warn "  $SCRIPTS_DIR - skipping scheduler setup."
    elif [ "$ASSUME_YES" = true ] || [ ! -t 0 ]; then
        info "Running non-interactively (--yes or no tty) - skipping scheduler prompt."
        info "  Run '$CRON_INSTALLER' or '$SYSTEMD_INSTALLER' manually when ready,"
        info "  or re-run this script interactively to be prompted."
    else
        echo ""
        echo "How would you like daily backups scheduled? (job-agent already runs"
        echo "under systemd, so systemd is recommended for consistent logging.)"
        echo "  1) systemd  - requires root, gives journalctl logging + auto catch-up"
        echo "  2) cron     - simpler, no root required"
        echo "  3) skip     - don't set one up now"
        SCHED_CHOICE=""
        while true; do
            read -r -p "Choice [1/2/3]: " SCHED_CHOICE
            case "$SCHED_CHOICE" in
                1|systemd)
                    if [ ! -x "$SYSTEMD_INSTALLER" ]; then
                        error "$SYSTEMD_INSTALLER not found or not executable."
                        ERRORS=$((ERRORS + 1))
                    else
                        info "Running install-backup-systemd.sh (requires root - you may be prompted for sudo) ..."
                        if [ "$(id -u)" -eq 0 ]; then
                            RUN_SYSTEMD_INSTALLER=("$SYSTEMD_INSTALLER")
                        else
                            RUN_SYSTEMD_INSTALLER=(sudo "$SYSTEMD_INSTALLER")
                        fi
                        if "${RUN_SYSTEMD_INSTALLER[@]}"; then
                            success "Systemd backup timer installed."
                        else
                            error "install-backup-systemd.sh failed - see output above."
                            ERRORS=$((ERRORS + 1))
                        fi
                    fi
                    break
                    ;;
                2|cron)
                    if [ ! -x "$CRON_INSTALLER" ]; then
                        error "$CRON_INSTALLER not found or not executable."
                        ERRORS=$((ERRORS + 1))
                    else
                        info "Running install-backup-cron.sh ..."
                        if "$CRON_INSTALLER"; then
                            success "Cron backup schedule installed."
                        else
                            error "install-backup-cron.sh failed - see output above."
                            ERRORS=$((ERRORS + 1))
                        fi
                    fi
                    break
                    ;;
                3|skip|"")
                    info "Skipped - no backup scheduler installed. Run this script again later."
                    break
                    ;;
                *)
                    echo "Please enter 1, 2, or 3."
                    ;;
            esac
        done
    fi
fi

# =========================================================================
# Summary
# =========================================================================
header "Summary"
if [ "$ERRORS" -eq 0 ]; then
    success "Setup completed with no errors."
    log_line "----" "Setup completed with no errors."
    echo ""
    echo "Next steps:"
    echo "  1. Copy .env.backup.example values into job-agent's .env"
    echo "  2. Fill in BACKUP_AGE_RECIPIENT (printed above if a key was just generated)"
    echo "  3. Fill in BACKUP_REMOTE_HOST (user@raspberry-pi-tailscale-name)"
    echo "  4. Run rclone config to set up the Backblaze B2 remote, if not done"
    echo "  5. Run ./scripts/check-deps.sh to verify everything"
    echo "  6. Run ./scripts/backup.sh once manually to test"
    echo ""
    echo "Full log: $LOG_FILE"
    exit 0
else
    error "Setup completed with $ERRORS issue(s) - review the messages above."
    log_line "----" "Setup completed with $ERRORS issue(s)."
    echo ""
    echo "Full log: $LOG_FILE"
    exit 1
fi
