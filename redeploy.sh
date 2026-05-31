#!/bin/bash
# ============================================================
# Redeploy Web Toolbox — pull latest code and restart gunicorn
# ============================================================
# Run as root or with sudo:
#   sudo bash redeploy.sh
#
# This script will:
#   1. Pull latest code from the prod branch
#   2. Sync files to /opt/toolbox (preserving runtime data)
#   3. Reinstall Python dependencies
#   4. Restart the gunicorn service
#
# Assumes:
#   - App is already deployed at /opt/toolbox
#   - Gunicorn systemd service is configured
# ============================================================

set -euo pipefail

# --- Configuration ---
APP_NAME="toolbox"
APP_DIR="/opt/toolbox"
GIT_REPO="https://github.com/similecat/toolbox.git"
GIT_BRANCH="prod"

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# --- Check root ---
if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root (use sudo)"
fi

# --- Check app directory ---
if [[ ! -d "${APP_DIR}" ]]; then
    error "App directory ${APP_DIR} does not exist. Run setup_nginx.sh first."
fi

info "Redeploying ${APP_NAME}..."

# --- Pull latest code to temp dir ---
TEMP_DIR=$(mktemp -d /tmp/toolbox_deploy_XXXXXX)
info "Pulling latest code..."

cd "${TEMP_DIR}"
git init -q
git remote add origin "${GIT_REPO}"
git fetch origin "${GIT_BRANCH}" -q
git checkout -b "${GIT_BRANCH}" "origin/${GIT_BRANCH}" -q

# --- Sync files to APP_DIR ---
rsync -av --delete \
    --exclude='instance/' \
    --exclude='data/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    "${TEMP_DIR}/" "${APP_DIR}/"

rm -rf "${TEMP_DIR}"
info "Files synced"

# --- Reinstall Python dependencies ---
info "Installing Python dependencies..."
cd "${APP_DIR}"
python3 -m pip install -q -r requirements.txt
info "Dependencies installed"

# --- Restart gunicorn ---
info "Restarting gunicorn service..."
systemctl daemon-reload
if systemctl restart "${APP_NAME}"; then
    sleep 1
    if systemctl is-active --quiet "${APP_NAME}"; then
        info "Gunicorn restarted successfully"
    else
        error "Gunicorn failed to start. Check: journalctl -u ${APP_NAME}"
    fi
else
    error "Failed to restart gunicorn"
fi

info "Redeployment complete!"
