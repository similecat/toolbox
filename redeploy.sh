#!/bin/bash
# ============================================================
# Redeploy Web Toolbox — pull latest code and restart gunicorn
# ============================================================
# Run as root or with sudo:
#   sudo bash redeploy.sh
#
# This script will:
#   1. Pull latest code from the prod branch
#   2. Restart the gunicorn service
#
# Assumes:
#   - App is already deployed at /opt/toolbox
#   - Dependencies are already installed
#   - Gunicorn systemd service is configured
# ============================================================

set -euo pipefail

# --- Configuration ---
APP_NAME="toolbox"
APP_DIR="/opt/toolbox"
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
cd "${APP_DIR}"

# --- Pull latest code ---
info "Fetching latest changes..."
if git fetch origin "${GIT_BRANCH}"; then
    CURRENT=$(git rev-parse HEAD)
    LATEST=$(git rev-parse "origin/${GIT_BRANCH}")

    if [[ "${CURRENT}" == "${LATEST}" ]]; then
        info "Already up to date (commit ${CURRENT:0:8})"
    else
        info "Updating from ${CURRENT:0:8} to ${LATEST:0:8}"
        git reset --hard "origin/${GIT_BRANCH}"
    fi
else
    error "Failed to fetch from remote"
fi

# --- Restart gunicorn ---
info "Restarting gunicorn service..."
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
