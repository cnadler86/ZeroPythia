#!/bin/bash
#
# ZeroPythia – Service Installation Script
#
# Usage: sudo ./install.sh [OPTIONS]
#
# Options:
#   -H, --host       HOST   Bind address for the dashboard  (default: 0.0.0.0)
#   -p, --port       PORT   Dashboard TCP port              (default: 8765)
#       --shelly     IP     Shelly 3EM IP address           (default: 192.168.178.77)
#       --zendure    IP     Zendure SolarFlow IP address    (default: 192.168.178.140)
#       --mqtt-broker URL   MQTT broker URL                 (default: mqtt://localhost:1883)
#       --device-id  ID     Zendure device ID               (default: SF800Pro)
#       --auto              Start immediately in AUTO mode
#   -h, --help              Show this help message and exit
#
# Examples:
#   sudo ./install.sh
#   sudo ./install.sh --shelly 192.168.1.50 --zendure 192.168.1.60 --host 0.0.0.0 --port 8765
#   sudo ./install.sh --auto --mqtt-broker mqtt://192.168.1.5:1883 --device-id SF800Pro
#

set -euo pipefail

# ── Colour helpers ─────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
err()  { echo -e "${RED}✗ ERROR:${NC} $*" >&2; }

# ── Constants ─────────────────────────────────────────────────────────────────
SERVICE_NAME="zeropythia"
SERVICE_FILE="${SERVICE_NAME}.service"
SERVICE_USER="zeropythia"
SERVICE_GROUP="pythia"

# ── Defaults ──────────────────────────────────────────────────────────────────
HOST="0.0.0.0"
PORT="8765"
SHELLY_IP="192.168.178.77"
ZENDURE_IP="192.168.178.140"
MQTT_BROKER="mqtt://localhost:1883"
DEVICE_ID="SF800Pro"
AUTO_FLAG=""

# ── Argument parsing ──────────────────────────────────────────────────────────
usage() {
    grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -30
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -H|--host)        HOST="$2";          shift 2 ;;
        -p|--port)        PORT="$2";          shift 2 ;;
           --shelly)      SHELLY_IP="$2";     shift 2 ;;
           --zendure)     ZENDURE_IP="$2";    shift 2 ;;
           --mqtt-broker) MQTT_BROKER="$2";   shift 2 ;;
           --device-id)   DEVICE_ID="$2";     shift 2 ;;
           --auto)        AUTO_FLAG=" --auto"; shift  ;;
        -h|--help)        usage ;;
        *) err "Unknown option: $1"; echo "Run with --help for usage."; exit 1 ;;
    esac
done

EXTRA_ARGS="--host ${HOST} --port ${PORT} --shelly ${SHELLY_IP} --zendure ${ZENDURE_IP} --mqtt-broker ${MQTT_BROKER} --device-id ${DEVICE_ID}${AUTO_FLAG}"

# ── Must run as root (via sudo) ───────────────────────────────────────────────
echo "======================================================================="
echo "  ZeroPythia – Service Installation"
echo "======================================================================="

if [[ "$EUID" -ne 0 ]]; then
    err "This script must be run with sudo."
    echo "  Usage: sudo ./install.sh [OPTIONS]"
    exit 1
fi

if [[ -z "${SUDO_USER:-}" ]]; then
    err "Could not determine the invoking user. Run via sudo, not as root directly."
    exit 1
fi

REAL_USER="$SUDO_USER"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Locate Python (prefer venv) ───────────────────────────────────────────────
PYTHON_BIN=""
for candidate in \
    "${SCRIPT_DIR}/.venv/bin/python3" \
    "${SCRIPT_DIR}/venv/bin/python3" \
    "$(command -v python3 2>/dev/null || true)"; do
    if [[ -x "$candidate" ]]; then
        PYTHON_BIN="$candidate"
        break
    fi
done

if [[ -z "$PYTHON_BIN" ]]; then
    err "Python3 executable not found."
    echo "  Create a venv first:"
    echo "    python3 -m venv .venv && .venv/bin/pip install -e ."
    exit 1
fi

PYTHON_VERSION=$("$PYTHON_BIN" --version 2>&1)

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "  Install directory   : $SCRIPT_DIR"
echo "  Service user/group  : $SERVICE_USER / $SERVICE_GROUP"
echo "  Installing user     : $REAL_USER"
echo "  Python              : $PYTHON_VERSION"
echo "                        $PYTHON_BIN"
echo "  Bind address        : $HOST:$PORT"
echo "  Shelly 3EM IP       : $SHELLY_IP"
echo "  Zendure SolarFlow IP: $ZENDURE_IP"
echo "  MQTT broker         : $MQTT_BROKER"
echo "  Device ID           : $DEVICE_ID"
echo "  Auto mode           : ${AUTO_FLAG:+enabled}${AUTO_FLAG:-disabled}"
echo ""

read -rp "Continue with installation? (y/N) " REPLY
echo
[[ "$REPLY" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

# ── Create group and service user ─────────────────────────────────────────────
echo ""
echo "======================================================================="
echo "  Setting up user and group"
echo "======================================================================="

if ! getent group "$SERVICE_GROUP" &>/dev/null; then
    groupadd --system "$SERVICE_GROUP"
    ok "Created system group: $SERVICE_GROUP"
else
    warn "Group '$SERVICE_GROUP' already exists – skipping"
fi

if ! getent passwd "$SERVICE_USER" &>/dev/null; then
    useradd \
        --system \
        --gid "$SERVICE_GROUP" \
        --no-create-home \
        --shell /usr/sbin/nologin \
        --comment "ZeroPythia service account" \
        "$SERVICE_USER"
    ok "Created system user: $SERVICE_USER (no login, no home)"
else
    warn "User '$SERVICE_USER' already exists – skipping"
fi

# Add the installer to the pythia group so they can still edit project files
if ! id -nG "$REAL_USER" 2>/dev/null | grep -qw "$SERVICE_GROUP"; then
    usermod -aG "$SERVICE_GROUP" "$REAL_USER"
    ok "Added $REAL_USER to group $SERVICE_GROUP"
    warn "You must log out and back in for the group change to take effect."
else
    warn "$REAL_USER is already a member of $SERVICE_GROUP"
fi

# ── File ownership and permissions ────────────────────────────────────────────
echo ""
echo "======================================================================="
echo "  Setting file ownership and permissions"
echo "======================================================================="

chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "$SCRIPT_DIR"
ok "Ownership set to ${SERVICE_USER}:${SERVICE_GROUP}"

# Directories: rwxrwsr-x  (setgid so new files inherit the group)
find "$SCRIPT_DIR" -type d -exec chmod 2775 {} \;
ok "Directories: 2775 (rwxrwsr-x)"

# Files: use capital X – sets execute only where it was already set (venv bins, .so)
chmod -R u=rwX,g=rwX,o=rX "$SCRIPT_DIR"
ok "Files: owner/group rw, others r (execute bits preserved)"

# Ensure install/uninstall scripts are executable
[[ -f "$SCRIPT_DIR/install.sh" ]]   && chmod 775 "$SCRIPT_DIR/install.sh"
[[ -f "$SCRIPT_DIR/uninstall.sh" ]] && chmod 775 "$SCRIPT_DIR/uninstall.sh"

# Allow git operations in this directory for the real user
# (needed after chown because git checks directory ownership)
sudo -u "$REAL_USER" git -C "$SCRIPT_DIR" config --local safe.directory "$SCRIPT_DIR" 2>/dev/null \
    && ok "git safe.directory configured for $REAL_USER" \
    || warn "git safe.directory skipped (not a git repo?)"

# ── Install systemd service ───────────────────────────────────────────────────
echo ""
echo "======================================================================="
echo "  Installing systemd service"
echo "======================================================================="

if [[ ! -f "$SCRIPT_DIR/$SERVICE_FILE" ]]; then
    err "Service template not found: $SCRIPT_DIR/$SERVICE_FILE"
    exit 1
fi

# Stop and disable any existing installation
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl stop "$SERVICE_NAME"
    warn "Stopped existing running service"
fi
if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl disable "$SERVICE_NAME"
fi

# Generate concrete service file from template (substituting placeholders)
TMP_SERVICE="$(mktemp /tmp/${SERVICE_NAME}.XXXXXX.service)"
sed \
    -e "s|{{WORKING_DIR}}|${SCRIPT_DIR}|g" \
    -e "s|{{PYTHON_BIN}}|${PYTHON_BIN}|g" \
    -e "s|{{EXTRA_ARGS}}|${EXTRA_ARGS}|g" \
    "$SCRIPT_DIR/$SERVICE_FILE" > "$TMP_SERVICE"

# Install with strict root ownership
install -m 644 -o root -g root "$TMP_SERVICE" "/etc/systemd/system/${SERVICE_FILE}"
rm -f "$TMP_SERVICE"
ok "Installed /etc/systemd/system/${SERVICE_FILE}"

systemctl daemon-reload
ok "systemd daemon reloaded"

systemctl enable "$SERVICE_NAME"
ok "Service enabled (auto-start on boot)"

systemctl start "$SERVICE_NAME"
sleep 2

# ── Result ────────────────────────────────────────────────────────────────────
if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo ""
    echo "======================================================================="
    echo -e "  ${GREEN}Installation successful!${NC}"
    echo "======================================================================="
    echo ""
    echo "  Dashboard:  http://${HOST}:${PORT}/"
    echo ""
    echo "  Useful commands:"
    echo "    sudo systemctl status  $SERVICE_NAME"
    echo "    sudo journalctl -u $SERVICE_NAME -f"
    echo "    sudo systemctl restart $SERVICE_NAME"
    echo "    sudo systemctl stop    $SERVICE_NAME"
    echo "    sudo systemctl disable $SERVICE_NAME"
    echo ""
    echo "  IMPORTANT: Log out and back in so the '$SERVICE_GROUP' group takes effect"
    echo "  (required to edit project files as $REAL_USER)."
    echo ""
    echo "  Recent log output:"
    echo "  -------------------------------------------------------------------"
    journalctl -u "$SERVICE_NAME" -n 15 --no-pager
    echo "  -------------------------------------------------------------------"
else
    echo ""
    echo "======================================================================="
    echo -e "  ${RED}Service failed to start!${NC}"
    echo "======================================================================="
    echo ""
    echo "  Full logs:"
    journalctl -u "$SERVICE_NAME" -n 40 --no-pager
    echo ""
    echo "  Common causes:"
    echo "    - venv missing or incomplete  →  python3 -m venv .venv && .venv/bin/pip install -e ."
    echo "    - Wrong Shelly or Zendure IP  →  sudo ./uninstall.sh && sudo ./install.sh --shelly <IP>"
    echo "    - Config error in config/zerofeed.yaml"
    echo ""
    exit 1
fi
