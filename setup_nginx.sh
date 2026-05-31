#!/bin/bash
# ============================================================
# Nginx + Gunicorn setup script for Web Toolbox (Ubuntu/Debian)
# ============================================================
# Run as root or with sudo:
#   sudo bash setup_nginx.sh
#
# This script will:
#   0. Clone or update the app to /opt/toolbox (from prod branch)
#   1. Install nginx
#   2. Create a systemd service for gunicorn
#   3. Configure nginx as a reverse proxy on port 80
#   4. Enable and start both services
# ============================================================

set -euo pipefail

# --- Configuration ---
APP_NAME="toolbox"
APP_DIR="/opt/toolbox"
GIT_REPO="https://github.com/similecat/toolbox.git"
GIT_BRANCH="prod"
SOCKET_PATH="/run/gunicorn/${APP_NAME}.sock"
USER="www-data"
WORKERS=2
BIND="unix:${SOCKET_PATH}"

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

# --- Step 0: Clone or update the app ---
if [[ -d "${APP_DIR}" ]]; then
    info "App directory already exists at ${APP_DIR}, updating..."
    cd "${APP_DIR}"
    git fetch origin
    git reset --hard "origin/${GIT_BRANCH}"
else
    info "Cloning app to ${APP_DIR}..."
    mkdir -p "${APP_DIR}"
    cd "${APP_DIR}"
    git init
    git remote add origin "${GIT_REPO}"
    git fetch origin "${GIT_BRANCH}"
    git checkout -b "${GIT_BRANCH}" "origin/${GIT_BRANCH}"
fi

chown -R "${USER}:${USER}" "${APP_DIR}"
info "App is ready at ${APP_DIR}"

# --- Step 1: Install nginx ---
info "Installing nginx..."
apt-get update -qq
apt-get install -y -qq nginx > /dev/null 2>&1
info "nginx installed successfully"

# --- Step 2: Ensure Python dependencies are installed ---
info "Checking Python dependencies..."
if ! command -v pip3 &> /dev/null; then
    apt-get install -y -qq python3-pip > /dev/null 2>&1
fi

cd "${APP_DIR}"
pip3 install -q -r requirements.txt
info "Python dependencies installed"

# --- Step 3: Create gunicorn systemd service ---
info "Creating gunicorn systemd service..."

mkdir -p /run/gunicorn
chown "${USER}:${USER}" /run/gunicorn

cat > /etc/systemd/system/${APP_NAME}.service <<EOF
[Unit]
Description=Gunicorn daemon for ${APP_NAME}
After=network.target

[Service]
User=${USER}
Group=${USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/gunicorn \\
    --workers ${WORKERS} \\
    --worker-class gevent \\
    --bind ${BIND} \\
    --access-logfile - \\
    --error-logfile - \\
    app:app

Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# If no venv, adjust path
if [[ ! -d "${APP_DIR}/venv" ]]; then
    info "No virtual env detected, using system Python..."
    sed -i "s|${APP_DIR}/venv/bin/gunicorn|/usr/bin/gunicorn|" /etc/systemd/system/${APP_NAME}.service
fi

info "gunicorn service created"

# --- Step 4: Configure nginx ---
info "Configuring nginx reverse proxy..."

cat > /etc/nginx/sites-available/${APP_NAME} <<EOF
server {
    listen 80;
    server_name _;

    # Security headers
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;

    # Static files (if any)
    location /static {
        alias ${APP_DIR}/static;
        expires 30d;
    }

    # Proxy to gunicorn socket
    location / {
        uwsgi_pass off;
        proxy_pass http://unix:${SOCKET_PATH};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_redirect off;
        proxy_buffering off;
        proxy_request_buffering off;
    }

    # Deny access to hidden files
    location ~ /\. {
        deny all;
    }
}
EOF

# Enable the site
ln -sf /etc/nginx/sites-available/${APP_NAME} /etc/nginx/sites-enabled/${APP_NAME}

# Remove default site if it exists to avoid conflicts
if [[ -f /etc/nginx/sites-enabled/default ]]; then
    rm /etc/nginx/sites-enabled/default
    info "Removed default nginx site"
fi

# Test nginx config
if ! nginx -t > /dev/null 2>&1; then
    error "nginx configuration test failed"
fi

info "nginx configured successfully"

# --- Step 5: Reload services ---
info "Starting services..."
systemctl daemon-reload
systemctl enable ${APP_NAME}
systemctl restart ${APP_NAME}
systemctl restart nginx

# --- Verify ---
if systemctl is-active --quiet ${APP_NAME} && systemctl is-active --quiet nginx; then
    info "All services running!"
    echo ""
    echo "=========================================="
    echo "  Setup complete!"
    echo "=========================================="
    echo ""
    echo "  App URL:      http://localhost"
    echo "  Gunicorn:     systemctl status ${APP_NAME}"
    echo "  Nginx:        systemctl status nginx"
    echo "  Nginx logs:   /var/log/nginx/"
    echo "  Gunicorn log: journalctl -u ${APP_NAME}"
    echo ""
else
    warn "One or more services failed to start. Check logs:"
    echo "  journalctl -u ${APP_NAME}"
    echo "  /var/log/nginx/error.log"
fi
