#!/bin/bash
#
# ZeroPythia – Service Installation Script
#
# Clones the repository to /opt/zeropythia (or a custom directory) and
# installs a systemd service that starts automatically on boot.
#
# Usage: sudo ./install.sh [OPTIONS]
#
# Source selection (mutually exclusive; defaults to the branch of this script):
#       --branch  BRANCH   Git branch to check out        (default: auto-detect)
#       --tag     TAG      Release tag to check out        (e.g. v1.2.3)
#       --repo    URL      Repository URL                  (default: https://github.com/cnadler86/zendure_zero_feed.git)
#       --install-dir DIR  Install destination             (default: /opt/zeropythia)
#
# Hardware / runtime:
#   -H, --host       HOST  Dashboard bind address          (default: 0.0.0.0)
#   -p, --port       PORT  Dashboard TCP port              (default: 8765)
#       --shelly     IP    Shelly 3EM IP address           (default: 192.168.178.77)
#       --zendure    IP    Zendure SolarFlow IP address    (default: 192.168.178.140)
#       --mqtt-broker URL  MQTT broker URL                 (default: mqtt://localhost:1883)
#       --device-id  ID    Zendure device ID               (default: SF800Pro)
#       --auto             Start immediately in AUTO mode
#   -h, --help             Show this help message and exit
#
# Examples:
#   sudo ./install.sh
#   sudo ./install.sh --branch master --shelly 192.168.1.50 --zendure 192.168.1.60
#   sudo ./install.sh --tag v1.2.3 --auto --mqtt-broker mqtt://192.168.1.5:1883
#   sudo ./install.sh --branch feat/new --install-dir /srv/zeropythia
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
DEFAULT_REPO="https://github.com/cnadler86/zendure_zero_feed.git"
DEFAULT_INSTALL_DIR="/opt/zeropythia"

# Auto-detect current branch from the directory this script lives in
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_detected_branch=""
if git -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree &>/dev/null 2>&1; then
    _detected_branch="$(git -C "$SCRIPT_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    # Detached HEAD → fall back to empty (will use remote default)
    [[ "$_detected_branch" == "HEAD" ]] && _detected_branch=""
fi

# ── Defaults ──────────────────────────────────────────────────────────────────
REPO_URL="$DEFAULT_REPO"
INSTALL_DIR="$DEFAULT_INSTALL_DIR"
REF_BRANCH="${_detected_branch}"
REF_TAG=""
HOST="0.0.0.0"
PORT="8765"
SHELLY_IP="192.168.178.77"
ZENDURE_IP="192.168.178.140"
MQTT_BROKER="mqtt://localhost:1883"
DEVICE_ID="SF800Pro"
AUTO_FLAG=""

# ── Argument parsing ──────────────────────────────────────────────────────────
usage() {
    grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -35
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --branch)      REF_BRANCH="$2"; REF_TAG="";         shift 2 ;;
        --tag)         REF_TAG="$2";    REF_BRANCH="";      shift 2 ;;
        --repo)        REPO_URL="$2";                        shift 2 ;;
        --install-dir) INSTALL_DIR="$2";                     shift 2 ;;
        -H|--host)     HOST="$2";                            shift 2 ;;
        -p|--port)     PORT="$2";                            shift 2 ;;
        --shelly)      SHELLY_IP="$2";                       shift 2 ;;
        --zendure)     ZENDURE_IP="$2";                      shift 2 ;;
        --mqtt-broker) MQTT_BROKER="$2";                     shift 2 ;;
        --device-id)   DEVICE_ID="$2";                       shift 2 ;;
        --auto)        AUTO_FLAG=" --auto";                  shift   ;;
        -h|--help)     usage ;;
        *) err "Unknown option: $1"; echo "Run with --help for usage."; exit 1 ;;
    esac
done

EXTRA_ARGS="--host ${HOST} --port ${PORT} --shelly ${SHELLY_IP} --zendure ${ZENDURE_IP} --mqtt-broker ${MQTT_BROKER} --device-id ${DEVICE_ID}${AUTO_FLAG}"

if [[ -n "$REF_TAG" ]]; then
    REF_DESC="tag: $REF_TAG"
elif [[ -n "$REF_BRANCH" ]]; then
    REF_DESC="branch: $REF_BRANCH"
else
    REF_DESC="(remote default branch)"
fi

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

# ── Ensure uv is available ────────────────────────────────────────────────────
echo ""
echo "======================================================================="
echo "  Checking for uv"
echo "======================================================================="

UV_BIN="$(command -v uv 2>/dev/null || true)"
if [[ -z "$UV_BIN" ]]; then
    warn "uv not found – installing via official installer to /usr/local/bin …"
    if ! command -v curl &>/dev/null; then
        err "curl is required to install uv. Run: apt-get install curl"
        exit 1
    fi
    curl -LsSf https://astral.sh/uv/install.sh \
        | env UV_INSTALL_DIR=/usr/local/bin sh
    UV_BIN="$(command -v uv 2>/dev/null || true)"
    if [[ -z "$UV_BIN" ]]; then
        err "uv installation failed. Install manually: https://docs.astral.sh/uv/getting-started/installation/"
        exit 1
    fi
    ok "uv installed: $UV_BIN ($("$UV_BIN" --version))"
else
    ok "uv found: $UV_BIN ($("$UV_BIN" --version))"
fi

if ! command -v git &>/dev/null; then
    err "git is required. Run: apt-get install git"
    exit 1
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "  Repository          : $REPO_URL"
echo "  Ref                 : $REF_DESC"
echo "  Install directory   : $INSTALL_DIR"
echo "  Service user/group  : $SERVICE_USER / $SERVICE_GROUP"
echo "  Installing user     : $REAL_USER"
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

if ! id -nG "$REAL_USER" 2>/dev/null | grep -qw "$SERVICE_GROUP"; then
    usermod -aG "$SERVICE_GROUP" "$REAL_USER"
    ok "Added $REAL_USER to group $SERVICE_GROUP"
    warn "You must log out and back in for the group change to take effect."
else
    warn "$REAL_USER is already a member of $SERVICE_GROUP"
fi

# ── Clone / update repository ─────────────────────────────────────────────────
echo ""
echo "======================================================================="
echo "  Setting up repository at $INSTALL_DIR"
echo "======================================================================="

if [[ -d "$INSTALL_DIR/.git" ]]; then
    warn "Repository already exists at $INSTALL_DIR – updating instead of cloning"
    git -C "$INSTALL_DIR" config --local safe.directory "$INSTALL_DIR"
    git -C "$INSTALL_DIR" fetch --tags origin
    ok "Fetched latest refs from origin"
    if [[ -n "$REF_TAG" ]]; then
        git -C "$INSTALL_DIR" checkout "$REF_TAG"
        ok "Checked out tag: $REF_TAG"
    elif [[ -n "$REF_BRANCH" ]]; then
        git -C "$INSTALL_DIR" checkout "$REF_BRANCH"
        git -C "$INSTALL_DIR" reset --hard "origin/$REF_BRANCH"
        ok "Updated to latest commit on branch: $REF_BRANCH"
    else
        git -C "$INSTALL_DIR" pull
        ok "Pulled latest changes"
    fi
else
    CLONE_ARGS=(--depth 1)
    if [[ -n "$REF_TAG" ]]; then
        CLONE_ARGS+=(--branch "$REF_TAG")
    elif [[ -n "$REF_BRANCH" ]]; then
        CLONE_ARGS+=(--branch "$REF_BRANCH")
    fi
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone "${CLONE_ARGS[@]}" "$REPO_URL" "$INSTALL_DIR"
    ok "Cloned $REPO_URL → $INSTALL_DIR ($REF_DESC)"
fi

# ── File ownership and permissions ────────────────────────────────────────────
echo ""
echo "======================================================================="
echo "  Setting file ownership and permissions"
echo "======================================================================="

chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "$INSTALL_DIR"
ok "Ownership set to ${SERVICE_USER}:${SERVICE_GROUP}"

find "$INSTALL_DIR" -type d -exec chmod 2775 {} \;
ok "Directories: 2775 (rwxrwsr-x)"

chmod -R u=rwX,g=rwX,o=rX "$INSTALL_DIR"
ok "Files: owner/group rw, others r (execute bits preserved)"

[[ -f "$INSTALL_DIR/install.sh" ]]   && chmod 775 "$INSTALL_DIR/install.sh"
[[ -f "$INSTALL_DIR/uninstall.sh" ]] && chmod 775 "$INSTALL_DIR/uninstall.sh"

git -C "$INSTALL_DIR" config --local safe.directory "$INSTALL_DIR" 2>/dev/null || true
sudo -u "$REAL_USER" git -C "$INSTALL_DIR" config --global --add safe.directory "$INSTALL_DIR" 2>/dev/null \
    && ok "git safe.directory configured for $REAL_USER" \
    || warn "git safe.directory skipped for $REAL_USER"

# ── Install / sync Python dependencies ────────────────────────────────────────
echo ""
echo "======================================================================="
echo "  Installing Python dependencies (uv sync --no-dev)"
echo "======================================================================="

runuser -u "$SERVICE_USER" -- "$UV_BIN" sync --no-dev --project "$INSTALL_DIR"
ok "Dependencies installed into $INSTALL_DIR/.venv"

# Re-fix permissions after uv sync (uv may create files as root or service user)
chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "$INSTALL_DIR"
chmod -R u=rwX,g=rwX,o=rX "$INSTALL_DIR"

PYTHON_BIN="${INSTALL_DIR}/.venv/bin/python3"
if [[ ! -x "$PYTHON_BIN" ]]; then
    err "Python binary not found at $PYTHON_BIN after uv sync."
    exit 1
fi
PYTHON_VERSION=$("$PYTHON_BIN" --version 2>&1)
ok "Python: $PYTHON_VERSION ($PYTHON_BIN)"

# ── Install systemd service ───────────────────────────────────────────────────
echo ""
echo "======================================================================="
echo "  Installing systemd service"
echo "======================================================================="

if [[ ! -f "$INSTALL_DIR/$SERVICE_FILE" ]]; then
    err "Service template not found: $INSTALL_DIR/$SERVICE_FILE"
    exit 1
fi

if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl stop "$SERVICE_NAME"
    warn "Stopped existing running service"
fi
if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl disable "$SERVICE_NAME"
fi

TMP_SERVICE="$(mktemp /tmp/${SERVICE_NAME}.XXXXXX.service)"
sed \
    -e "s|{{WORKING_DIR}}|${INSTALL_DIR}|g" \
    -e "s|{{PYTHON_BIN}}|${PYTHON_BIN}|g" \
    -e "s|{{EXTRA_ARGS}}|${EXTRA_ARGS}|g" \
    "$INSTALL_DIR/$SERVICE_FILE" > "$TMP_SERVICE"

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
    echo "  Dashboard:    http://${HOST}:${PORT}/"
    echo "  Install dir:  $INSTALL_DIR"
    echo "  Ref:          $REF_DESC"
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
    echo "    - Wrong Shelly or Zendure IP  →  sudo ./uninstall.sh && sudo ./install.sh --shelly <IP>"
    echo "    - Config error in $INSTALL_DIR/config/zerofeed.yaml"
    echo "    - Dependency issue            →  cd $INSTALL_DIR && uv sync --no-dev"
    echo ""
    exit 1
fi
