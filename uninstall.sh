#!/bin/bash
#
# ZeroPythia – Service Uninstallation Script
#
# Usage: sudo ./uninstall.sh [OPTIONS]
#
# Options:
#   --remove-user   Also remove the 'zeropythia' system user.
#                   The 'pythia' group is removed only when no other
#                   members (e.g. gridpythia) remain.
#   -h, --help      Show this help message and exit
#

set -euo pipefail

# ── Colour helpers ─────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
err()  { echo -e "${RED}✗ ERROR:${NC} $*" >&2; }

SERVICE_NAME="zeropythia"
SERVICE_FILE="${SERVICE_NAME}.service"
SERVICE_USER="zeropythia"
SERVICE_GROUP="pythia"
REMOVE_USER=false

usage() {
    grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -20
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --remove-user) REMOVE_USER=true; shift ;;
        -h|--help)     usage ;;
        *) err "Unknown option: $1"; exit 1 ;;
    esac
done

echo "======================================================================="
echo "  ZeroPythia – Service Uninstallation"
echo "======================================================================="

if [[ "$EUID" -ne 0 ]]; then
    err "This script must be run with sudo."
    echo "  Usage: sudo ./uninstall.sh"
    exit 1
fi

if [[ ! -f "/etc/systemd/system/${SERVICE_FILE}" ]]; then
    warn "Service file not found at /etc/systemd/system/${SERVICE_FILE}"
    echo "  The service does not appear to be installed."
    exit 0
fi

echo ""
echo "  This will:"
echo "    - Stop and disable the '$SERVICE_NAME' service"
echo "    - Remove /etc/systemd/system/${SERVICE_FILE}"
if $REMOVE_USER; then
    echo "    - Remove system user: $SERVICE_USER"
    echo "    - Remove system group: $SERVICE_GROUP (only if no other members)"
fi
echo ""
echo "  Application files, config, and logs are NOT removed."
echo ""

read -rp "Continue? (y/N) " REPLY
echo
[[ "$REPLY" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

echo ""
echo "======================================================================="
echo "  Removing service"
echo "======================================================================="

if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl stop "$SERVICE_NAME"
    ok "Stopped $SERVICE_NAME"
else
    warn "Service was not running"
fi

if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl disable "$SERVICE_NAME"
    ok "Disabled $SERVICE_NAME (removed from boot)"
else
    warn "Service was not enabled"
fi

rm -f "/etc/systemd/system/${SERVICE_FILE}"
ok "Removed /etc/systemd/system/${SERVICE_FILE}"

systemctl daemon-reload
systemctl reset-failed "$SERVICE_NAME" 2>/dev/null || true
ok "systemd daemon reloaded"

if $REMOVE_USER; then
    echo ""
    echo "======================================================================="
    echo "  Removing user and group"
    echo "======================================================================="
    if getent passwd "$SERVICE_USER" &>/dev/null; then
        userdel "$SERVICE_USER"
        ok "Removed user: $SERVICE_USER"
    else
        warn "User '$SERVICE_USER' not found – skipping"
    fi
    if getent group "$SERVICE_GROUP" &>/dev/null; then
        MEMBERS="$(getent group "$SERVICE_GROUP" | cut -d: -f4)"
        if [[ -z "$MEMBERS" ]]; then
            groupdel "$SERVICE_GROUP"
            ok "Removed group: $SERVICE_GROUP"
        else
            warn "Group '$SERVICE_GROUP' still has members ($MEMBERS) – not removed"
        fi
    else
        warn "Group '$SERVICE_GROUP' not found – skipping"
    fi
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ""
echo "======================================================================="
echo -e "  ${GREEN}Uninstallation complete${NC}"
echo "======================================================================="
echo ""
echo "  Application files remain in: $SCRIPT_DIR"
echo "  To also restore ownership to your user:"
echo "    sudo chown -R \$USER:\$(id -gn) $SCRIPT_DIR"
echo ""
