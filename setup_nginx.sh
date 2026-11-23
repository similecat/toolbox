#!/bin/bash
# ============================================================
# Nginx (HTTPS) + Gunicorn setup script for Web Toolbox (Ubuntu/Debian)
# ============================================================
# Run as root or with sudo:
#   sudo bash setup_nginx.sh           # pulls latest code from prod branch
#   sudo bash setup_nginx.sh --local   # deploys from current directory
#
# This script will:
#   0. Deploy app files to /opt/toolbox
#   1. Install nginx and openssl
#   2. Install Python dependencies
#   3. Create a systemd service for gunicorn
#   4. Generate a self-signed TLS certificate (if not present)
#   5. Configure nginx HTTPS on port 443 + HTTP->HTTPS redirect on port 80
#   6. Enable and start both services
# ============================================================

set -euo pipefail

# --- Configuration ---
APP_NAME="toolbox"
APP_DIR="/opt/toolbox"
GIT_REPO="https://github.com/similecat/toolbox.git"
GIT_BRANCH="prod"
SOCKET_PATH="/run/gunicorn/${APP_NAME}.sock"
USER="www-data"
WORKERS=1
BIND="unix:${SOCKET_PATH}"
SSL_DIR="/etc/nginx/ssl"
SSL_CERT="${SSL_DIR}/${APP_NAME}.crt"
SSL_KEY="${SSL_DIR}/${APP_NAME}.key"
SERVER_CN="${SERVER_DOMAIN:-byang.japaneast.cloudapp.azure.com}"

LOCAL_DEPLOY=0
if [[ "${1:-}" == "--local" ]]; then
    LOCAL_DEPLOY=1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

# --- Step 0: Deploy code to APP_DIR ---
mkdir -p "${APP_DIR}"

if [[ "${LOCAL_DEPLOY}" -eq 1 ]]; then
    if [[ "${SCRIPT_DIR}" != "${APP_DIR}" ]]; then
        info "Syncing local files from ${SCRIPT_DIR} to ${APP_DIR}..."
        rsync -av --delete \
            --exclude='instance/' \
            --exclude='data/' \
            --exclude='.git/' \
            --exclude='__pycache__/' \
            --exclude='*.pyc' \
            "${SCRIPT_DIR}/" "${APP_DIR}/"
    else
        info "Using existing files in ${APP_DIR}..."
    fi
else
    TEMP_DIR=$(mktemp -d /tmp/toolbox_deploy_XXXXXX)
    info "Pulling latest code to ${TEMP_DIR}..."

    cd "${TEMP_DIR}"
    git init -q
    git remote add origin "${GIT_REPO}"
    git fetch origin "${GIT_BRANCH}" -q
    git checkout -b "${GIT_BRANCH}" "origin/${GIT_BRANCH}" -q

    rsync -av --delete \
        --exclude='instance/' \
        --exclude='data/' \
        --exclude='__pycache__/' \
        --exclude='*.pyc' \
        "${TEMP_DIR}/" "${APP_DIR}/"

    rm -rf "${TEMP_DIR}"
fi

chown -R "${USER}:${USER}" "${APP_DIR}"
info "App is ready at ${APP_DIR}"

# --- Step 1: Install nginx & openssl ---
info "Installing nginx and openssl..."
apt-get update -qq
apt-get install -y -qq nginx openssl > /dev/null 2>&1
info "nginx and openssl installed successfully"

# --- Step 2: Ensure Python dependencies are installed ---
info "Checking Python dependencies..."
if ! command -v pip3 &> /dev/null; then
    apt-get install -y -qq python3-pip > /dev/null 2>&1
fi

apt-get install -y -qq python3-zope.event > /dev/null 2>&1 || true

cd "${APP_DIR}"
python3 -m pip install -q -r requirements.txt
info "Python dependencies installed"

# --- Step 3: Create gunicorn systemd service ---
info "Creating gunicorn systemd service..."

mkdir -p /run/gunicorn
chown "${USER}:${USER}" /run/gunicorn

GUNICORN_PATH=""
for path in "$(which gunicorn 2>/dev/null || true)" \
            "$(python3 -c 'import gunicorn.app.wsgiapp; import os; print(os.path.dirname(gunicorn.app.wsgiapp.__file__))' 2>/dev/null || true)/../bin/gunicorn" \
            "/usr/local/bin/gunicorn" \
            "/usr/bin/gunicorn"; do
    if [[ -n "$path" && -x "$path" ]]; then
        GUNICORN_PATH="$path"
        break
    fi
done

if [[ -z "${GUNICORN_PATH}" ]]; then
    error "gunicorn not found. Make sure it's installed: pip3 install gunicorn"
fi

info "Using gunicorn at ${GUNICORN_PATH}"

cat > /etc/systemd/system/${APP_NAME}.service <<EOF
[Unit]
Description=Gunicorn daemon for ${APP_NAME}
After=network.target
RuntimeDirectory=gunicorn
RuntimeDirectoryPreserve=yes

[Service]
User=${USER}
Group=${USER}
WorkingDirectory=${APP_DIR}
ExecStart=${GUNICORN_PATH} \\
    --workers ${WORKERS} \\
    --worker-class gevent \\
    --bind ${BIND} \\
    --access-logfile - \\
    --error-logfile - \\
    --timeout 120 \\
    app:app

Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

info "gunicorn service created"

# --- Step 4: Generate self-signed SSL certificate ---
info "Ensuring self-signed TLS certificate..."
mkdir -p "${SSL_DIR}"

if [[ ! -f "${SSL_CERT}" || ! -f "${SSL_KEY}" ]]; then
    HOST_FQDN="$(hostname -f 2>/dev/null || hostname || echo localhost)"
    SAN_LIST="DNS:${SERVER_CN},DNS:localhost,IP:127.0.0.1"
    if [[ "${HOST_FQDN}" != "${SERVER_CN}" && "${HOST_FQDN}" != "localhost" ]]; then
        SAN_LIST="${SAN_LIST},DNS:${HOST_FQDN}"
    fi

    openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
        -keyout "${SSL_KEY}" \
        -out "${SSL_CERT}" \
        -subj "/CN=${SERVER_CN}/O=Web Toolbox" \
        -addext "subjectAltName=${SAN_LIST}" > /dev/null 2>&1

    chmod 600 "${SSL_KEY}"
    chmod 644 "${SSL_CERT}"
    info "Generated self-signed TLS certificate at ${SSL_CERT}"
else
    info "Existing TLS certificate found at ${SSL_CERT}"
fi

# --- Step 5: Configure nginx HTTPS reverse proxy ---
info "Configuring nginx HTTPS reverse proxy..."

cat > /etc/nginx/sites-available/${APP_NAME} <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name _;

    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name _;

    ssl_certificate ${SSL_CERT};
    ssl_certificate_key ${SSL_KEY};
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 10m;

    # Allow large file uploads (500 MB for audio files)
    client_max_body_size 500M;

    # Security headers
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Strict-Transport-Security "max-age=31536000" always;

    # Static files (if any)
    location /static {
        alias ${APP_DIR}/static;
        expires 30d;
    }

    # Internal audio files - served by Nginx after Flask authentication check
    location /internal-audio/ {
        internal;
        alias ${APP_DIR}/instance/audio/;
        expires 7d;
    }

    # Proxy to gunicorn socket
    location / {
        proxy_pass http://unix:${SOCKET_PATH};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Nginx-Proxy "1";
        proxy_redirect off;
        proxy_buffering off;
        proxy_request_buffering off;
    }

    # Deny access to hidden files
    location ~ /\\. {
        deny all;
    }
}
EOF

ln -sf /etc/nginx/sites-available/${APP_NAME} /etc/nginx/sites-enabled/${APP_NAME}

if [[ -f /etc/nginx/sites-enabled/default ]]; then
    rm /etc/nginx/sites-enabled/default
    info "Removed default nginx site"
fi

if ! nginx -t > /dev/null 2>&1; then
    nginx -t
    error "nginx configuration test failed"
fi

info "nginx configured successfully"

# --- Step 6: Reload services ---
info "Starting services..."
systemctl daemon-reload
systemctl enable ${APP_NAME}
systemctl restart ${APP_NAME}
systemctl restart nginx

if systemctl is-active --quiet ${APP_NAME} && systemctl is-active --quiet nginx; then
    info "All services running!"
    echo ""
    echo "=========================================="
    echo "  HTTPS Setup complete!"
    echo "=========================================="
    echo ""
    echo "  App URL:      https://${SERVER_CN}"
    echo "  TLS Cert:     ${SSL_CERT}"
    echo "  Gunicorn:     systemctl status ${APP_NAME}"
    echo "  Nginx:        systemctl status nginx"
    echo ""
else
    warn "One or more services failed to start. Check logs:"
    echo "  journalctl -u ${APP_NAME}"
    echo "  /var/log/nginx/error.log"
fi
