# Web Toolbox

A collection of useful web tools built with Python Flask, secured with **HTTPS transport encryption**, **email allow-listed user authentication**, and **shared REST API key access**.

## Features
- **HTTPS Encryption**: All web traffic and API payloads are encrypted over TLS (`TLSv1.2` / `TLSv1.3`). Self-signed certificates are generated automatically out of the box, and Nginx redirects all HTTP (`:80`) requests to HTTPS (`:443`).
- **Web Login & Email Allow-List**: Accessing any Toolbox webpage requires signing in. Only email addresses on the allow list (default: `bo.yang@hotmail.com`) can register an account or modify their password.
- **Admin Configuration (`/admin`)**: Admins can add/remove allow-listed email addresses, manage registered user roles/accounts, and view or rotate the shared REST API key.
- **Shared REST API Key**: External scripts and workers authenticate across **every** REST API service (`/api/tts/*` and `/api/github/download`) using a single shared default API key (`tbx-default-api-key-2026`, overridable via `TOOLBOX_API_KEY` or `/admin`).
- **GitHub Repo Downloader**: Download any public or private GitHub repository as a ZIP archive (Web UI & REST API).
- **Text to Voice**: Submit English or Chinese text for asynchronous VibeVoice speech synthesis and download completed audio files.
- **Machine Discovery (`/machines` & `/api/machines/*`)**: Cross-platform Linux & Windows machine status inventory reporting IPv4, IPv6, and serving status over persistent HTTPS Keep-Alive heartbeats (`60s` pulse, `>90s` unhealthy, `>180s` unresponsive).
- **Worker & Service Discovery + Direct P2P Mesh (`/services` & `/api/services/*`)**: Mandatory registration & lease enforcement (`X-Worker-Id` + `X-Worker-Lease`) for all existing and future worker services, live health & statistical telemetry dashboard (with 5-minute rollup charts), and direct Worker-to-Worker Peer-to-Peer (P2P) RPC invocation via `clients/worker_sdk.py` without routing payloads through the central server.

---

## Security & Authentication Architecture

### 1. HTTPS Transport Encryption
- **Production (Nginx + Gunicorn)**:
  - `setup_nginx.sh` generates a 2048-bit RSA self-signed X.509 certificate (`/etc/nginx/ssl/toolbox.crt` and `/etc/nginx/ssl/toolbox.key`, valid for 10 years with Subject Alternative Names for the server domain and `localhost`).
  - Nginx listens on port `80` solely to issue a `301 Moved Permanently` redirect to `https://$host$request_uri`, ensuring web content and credentials are never transferred in plaintext.
  - Nginx terminates TLS (`TLSv1.2` / `TLSv1.3`) on port `443` and proxies requests over a local Unix domain socket (`/run/gunicorn/toolbox.sock`) to Gunicorn, passing `X-Forwarded-Proto: https` (handled by Werkzeug `ProxyFix` in `app.py`).
  - Audio file downloads use Nginx `internal` locations (`/internal-audio/`) with `X-Accel-Redirect`, so Nginx streams large files directly from disk **only after** Flask verifies the user's session or API key.
- **Standalone / Local Development (`run.py` / `app.py`)**:
  - `ssl_utils.ensure_self_signed_cert()` automatically generates `instance/ssl/cert.pem` and `instance/ssl/key.pem` (using `cryptography` or `openssl`) if not already present, serving HTTPS directly from Flask.

### 2. Registration, Email Allow-List & Password Management
- **Database Tables (`data/toolbox.db` via `db.py`)**:
  - `allowed_emails`: Stores normalized lowercase email addresses permitted to register or change passwords. Seeded automatically on first initialization with **`bo.yang@hotmail.com`**.
  - `users`: Stores `id`, `email`, `password_hash` (hashed with Werkzeug's salted key-derivation `generate_password_hash`), `is_admin`, `created_at`, and `updated_at`.
  - `settings`: Persists global settings including the Flask `secret_key` and shared `api_key` across server restarts and redeployments.
- **Registration (`GET/POST /register`)**:
  - Normalizes the submitted email and checks `db.is_email_allowed(email)`. Non-allow-listed emails are rejected with `403 Forbidden`.
  - Enforces a minimum 8-character password and stores only the salted password hash.
  - When `bo.yang@hotmail.com` (or the first user when no admin exists) registers, `is_admin = 1` is granted automatically.
- **Modify Password (`GET/POST /change-password`)**:
  - Accessible both from the sign-in screen and while logged in.
  - Verifies **both** that the email is currently in `allowed_emails` (`403` if removed by an admin) **and** that `current_password` matches the stored hash (`401` if invalid) before updating `password_hash` and `updated_at`.
- **Admin Dashboard (`/admin`)**:
  - Restricted strictly to logged-in users with `is_admin == 1` (protected by CSRF tokens on all state-changing forms).
  - Allows admins to add/remove allow-listed emails, promote/revoke admin roles or delete user accounts, and view/update the shared REST API key.

### 3. How Web Login Works & How Login State Is Preserved Across Requests

```text
Browser                                     Nginx (HTTPS :443) & Flask (app.py)
  │                                                        │
  ├── 1. GET /text-to-voice (no cookie) ──────────────────►│ @app.before_request: no session
  │◄─ 302 Redirect -> /login?next=/text-to-voice ──────────┤
  │                                                        │
  ├── 2. POST /login (email + password over TLS) ─────────►│ Verify check_password_hash() against SQLite `users`
  │                                                        │ Create session = { user_id, pwd_updated_at }
  │                                                        │ Sign payload with persistent HMAC `secret_key`
  │◄─ 302 Redirect -> /text-to-voice ──────────────────────┤
  │   Set-Cookie: session=<signed_token>;                  │
  │               Secure; HttpOnly; SameSite=Lax;          │
  │               Max-Age=604800 (7 days)                  │
  │                                                        │
  ├── 3. GET /text-to-voice ──────────────────────────────►│ @app.before_request -> enforce_authentication():
  │   Cookie: session=<signed_token>                       │  a) Verify HMAC signature & 7-day expiration
  │                                                        │  b) Query SQLite `users` by `session["user_id"]`
  │                                                        │  c) Check `session["pwd_updated_at"] == user["updated_at"]`
  │◄─ 200 OK (rendered HTML, g.current_user populated) ────┤
  │                                                        │
  ├── 4. fetch('/api/tts/jobs') (same-origin JS) ─────────►│ Browser automatically attaches `Cookie: session=...`
  │   Cookie: session=<signed_token>                       │ Validated via `g.current_user` (no API key needed in JS)
  │◄─ 200 OK {"jobs": [...]} ──────────────────────────────┤
```

Step-by-step lifecycle of login state:

1. **Authentication (`POST /login` or `POST /register`)**:
   - Once credentials are verified, Flask clears any prior session state (`session.clear()`), marks the session permanent (`session.permanent = True`), and writes two fields into Flask's session dictionary:
     - `session["user_id"]`: The integer primary key of the authenticated user in `users`.
     - `session["pwd_updated_at"]`: The exact `updated_at` timestamp of the user record when the password was set.
2. **Cryptographic Signing & Cookie Storage (`Set-Cookie`)**:
   - Flask serializes and signs the session dictionary using `itsdangerous` HMAC signing with a 256-bit `secret_key` persisted in SQLite `settings` (or `TOOLBOX_SECRET_KEY` environment variable). Because `secret_key` is stored in `data/toolbox.db`, active user logins remain valid across Gunicorn restarts and `redeploy.sh` runs.
   - The signed token is returned to the browser in `Set-Cookie: session=...` with hardened cookie attributes:
     - **`Secure`**: The browser only sends the cookie over encrypted HTTPS connections (never over plain HTTP).
     - **`HttpOnly`**: JavaScript (`document.cookie`) cannot read the cookie, protecting session tokens against XSS.
     - **`SameSite=Lax`**: Protects against cross-site request forgery (CSRF) on cross-origin requests; admin actions additionally verify a per-session `csrf_token`.
     - **7-Day Lifetime**: Configured via `PERMANENT_SESSION_LIFETIME = timedelta(days=7)`.
3. **Automatic Reuse on Subsequent HTTP(S) Requests**:
   - The browser stores the `session` cookie and automatically includes `Cookie: session=<signed_token>` on every subsequent HTTPS request to the Toolbox origin—including page navigations (`GET /`, `/github-downloader`, `/text-to-voice`, `/admin`), form submissions (`POST /tts/submit`, `POST /download-repo`), file downloads (`GET /tts/download/<job_id>`), and in-page AJAX calls (`fetch('/api/tts/jobs')`).
4. **Default-Deny Per-Request Validation (`@app.before_request`)**:
   - On **every** incoming request, `enforce_authentication()` runs before the route handler:
     1. **Signature & Expiry Check**: Flask verifies the cookie's HMAC signature and `Max-Age`. Tampered or expired cookies are discarded immediately.
     2. **Live Database Lookup (`get_current_user()`)**: Flask looks up `session["user_id"]` in SQLite `users` and verifies `session["pwd_updated_at"] == user["updated_at"]`.
        - If an admin deletes a user account, the lookup returns `None` and the session is invalidated immediately on the very next request.
        - If the user modifies their password, `user["updated_at"]` changes—automatically invalidating any sessions on other browsers/devices while updating `pwd_updated_at` on the current session.
     3. **Access Control**: Unauthenticated requests to any non-public webpage are redirected (`302`) to `/login?next=<requested_path>`, returning the user to their original destination after signing in.
5. **Logout (`GET/POST /logout`)**:
   - Clears the session (`session.clear()`), instructing the browser to drop the cookie, and redirects to `/login`.

### 4. REST API Authentication (Shared Default API Key)
- All service REST APIs (`/api/tts/*` and `/api/github/download`) accept **either** an authenticated browser login session (for in-page UI calls) **or** the shared REST API key (for CLI scripts, workers, and integrations).
- **Default API Key**: `tbx-default-api-key-2026` (can be overridden at initial setup via `TOOLBOX_API_KEY` environment variable or rotated anytime in `/admin`).
- **Passing the API Key** (validated in constant time via `hmac.compare_digest`):
  - Header (recommended): `X-API-Key: tbx-default-api-key-2026`
  - Bearer header: `Authorization: Bearer tbx-default-api-key-2026`
  - Query parameter: `?api_key=tbx-default-api-key-2026`
- Missing or invalid keys receive `401 Unauthorized` JSON responses.

---

## Quick Start (Local HTTPS)

```bash
# Clone the repository
git clone https://github.com/similecat/toolbox.git
cd toolbox

# Install dependencies
pip install -r requirements.txt

# Start HTTPS server on port 5000 (auto-generates self-signed cert in instance/ssl/)
python run.py
```

Open `https://localhost:5000` in your browser (accept the self-signed certificate warning in the browser), click **Create Account** (`/register`), and register with an allow-listed email (default: `bo.yang@hotmail.com`).

---

## Production Deployment (Ubuntu / Linux with Nginx HTTPS + Gunicorn)

### Quick Setup Script

```bash
# Pull from prod branch, generate self-signed TLS cert, and configure Nginx HTTPS + Gunicorn
sudo bash setup_nginx.sh

# Or deploy directly from the local directory without pulling from GitHub
sudo bash setup_nginx.sh --local
```

The script will:
1. Sync application code to `/opt/toolbox` (preserving `instance/` and `data/`)
2. Install `nginx`, `openssl`, and Python dependencies
3. Create the `toolbox` systemd service running Gunicorn (`gevent` worker) on `/run/gunicorn/toolbox.sock`
4. Generate a self-signed TLS certificate at `/etc/nginx/ssl/toolbox.crt` (if not already present)
5. Configure Nginx to redirect port `80` → `443` and serve HTTPS on port `443`
6. Enable and start both `toolbox` and `nginx`

### Redeploying Updates

```bash
# Pull latest prod branch and restart Gunicorn (preserves users, allow-list, settings, and jobs in data/)
sudo bash redeploy.sh

# Or sync from local directory
sudo bash redeploy.sh --local
```

### Managing Services

| Command | Description |
|---|---|
| `sudo systemctl status toolbox` | Check Gunicorn service status |
| `sudo systemctl restart toolbox` | Restart Gunicorn |
| `sudo systemctl status nginx` | Check Nginx status |
| `journalctl -u toolbox` | View Gunicorn logs |
| `/var/log/nginx/` | Nginx access and error logs |

### Backing Up the Production Encrypted EPUB Vault to Local

Encrypted EPUB vaults live exclusively on the server under `/opt/toolbox/data/epub_vault` (never tracked in Git). Run `backup_prod_vault.py` locally to pull timestamped, checksum-verified backups into `backups/epub_vault/<YYYYMMDD_HHMMSS>/`:

```bash
# Create a new timestamped backup from production (auto-detects SSH key)
python backup_prod_vault.py

# Create a backup and retain only the 30 newest local versions
python backup_prod_vault.py --keep 30

# List all local timestamped backup versions
python backup_prod_vault.py --list
```
Each version directory (`backups/epub_vault/<YYYYMMDD_HHMMSS>/`) stores an extracted `vault/` copy, a compressed `epub_vault_<timestamp>.tar.gz` archive, and `backup_manifest.json` with per-file SHA-256 checksums.

---

## Running the TTS Worker

The TTS worker registers with Service Discovery (`service_type="tts"`), sends live heartbeats + synthesis telemetry, exposes direct Peer-to-Peer (`synthesize`, `list_voices`) RPC operations for peer workers, and polls `/api/tts/worker/jobs?status=pending` over HTTPS to process queued jobs.

```bash
cd VibeVoice
pip install -e .
cd demo

# Connect to local HTTPS server (auto-selects P2P port or specify --p2p_port)
python tts_worker.py --base_url https://localhost:5000 --p2p_port 9410

# Connect to remote HTTPS deployment
python tts_worker.py --base_url https://byang.japaneast.cloudapp.azure.com --api_key tbx-default-api-key-2026 --p2p_port 9410
```

### Worker Command-Line Options

| Option | Default | Description |
|---|---|---|
| `--base_url` | `https://localhost:5000` | Base HTTPS URL of the Toolbox server |
| `--api_key` | `TOOLBOX_API_KEY` or `tbx-default-api-key-2026` | Shared REST API key |
| `--verify_ssl` | `False` | Enable strict CA verification (off by default for self-signed certs) |
| `--worker_id` | Auto-generated | Stable worker instance ID in Service Discovery |
| `--worker_name` | Auto-generated | Friendly worker display name in `/services` |
| `--heartbeat_interval` | `30` | Service Discovery heartbeat interval in seconds |
| `--p2p_host` | `0.0.0.0` | Bind address for direct Peer-to-Peer worker RPC server |
| `--p2p_port` | `0` (ephemeral) | Listen port for direct P2P worker RPC server |
| `--p2p_advertise_host` | Auto-detected | Explicit routable IP/hostname advertised to peer workers |
| `--model_path` | Auto-detect | Path to VibeVoice model |
| `--device` | Auto-detect | Device for inference: `cuda`, `mps`, `cpu` |
| `--interval` | `5` | Polling interval in seconds |
| `--idle_timeout` | `60` | Seconds of idle time before unloading model from memory |
| `--cfg_scale` | `1.3` | CFG scale for generation |
| `--seed` | `None` | Random seed for reproducibility |

---

## REST API Reference

Include `X-API-Key: <api_key>` (or `Authorization: Bearer <api_key>`) on all REST API requests. When using `curl` against a self-signed certificate, pass `-k` (`--insecure`).

### GitHub Repo Downloader API

**POST** or **GET** `/api/github/download`

Download a GitHub repository as a `.zip` archive.

```bash
curl -k -X POST https://byang.japaneast.cloudapp.azure.com/api/github/download \
  -H "X-API-Key: tbx-default-api-key-2026" \
  -H "Content-Type: application/json" \
  -d '{"github_url": "https://github.com/octocat/Hello-World"}' \
  -o Hello-World.zip
```

### Text-to-Voice APIs

#### Submit a Job
**POST** `/api/tts/submit`
```bash
curl -k -X POST https://byang.japaneast.cloudapp.azure.com/api/tts/submit \
  -H "X-API-Key: tbx-default-api-key-2026" \
  -H "Content-Type: application/json" \
  -d '{"text": "Hello, this is a test.", "language": "en"}'
```

#### List Jobs
**GET** `/api/tts/jobs?status=pending`
```bash
curl -k https://byang.japaneast.cloudapp.azure.com/api/tts/jobs \
  -H "X-API-Key: tbx-default-api-key-2026"
```

#### Get Job Details
**GET** `/api/tts/job/<job_id>`

#### Update Job Status
**POST** `/api/tts/job/<job_id>/status` — JSON body: `{"status": "processing", "error": null}`

#### Upload Audio Result
**POST** `/api/tts/job/<job_id>/result` — Multipart form data with `audio` file field (or `error` field).

#### Download Completed Audio
**GET** `/api/tts/job/<job_id>/download` (or `/tts/download/<job_id>`)
```bash
curl -k -L https://byang.japaneast.cloudapp.azure.com/api/tts/job/<job_id>/download \
  -H "X-API-Key: tbx-default-api-key-2026" \
  -o output.wav
```

#### Retry / Delete a Job
- **POST** `/api/tts/job/<job_id>/retry`
- **DELETE** `/api/tts/job/<job_id>`

### Encrypted EPUB Reader APIs
- **GET** `/api/epub/vault/status` — Check whether the server-side encrypted vault (`data/epub_vault/`, preserved across redeployments and never stored in git) is initialized and unlocked.
- **POST** `/api/epub/vault/init` / `/api/epub/vault/unlock` / `/api/epub/vault/lock` — Initialize, unlock with master passphrase, or lock the in-memory vault key.
- **GET** `/api/epub/books` / **POST** `/api/epub/books/import` / **POST** `/api/epub/books/import-vault` — List encrypted books or import `.epub` / portable `.vault` archives.
- **GET** `/api/epub/books/<book_id>` / **DELETE** `/api/epub/books/<book_id>` — Load book metadata, TOC, spine, and study state, or zero-overwrite delete encrypted objects.
- **GET** `/api/epub/books/<book_id>/chapters/<spine_index>` & `/assets?path=...` — Serve sanitized chapter HTML/CSS and decrypted in-memory book assets.
- **PUT** `/progress`, **POST/PATCH/DELETE** `/bookmarks`, **POST/PATCH/DELETE** `/annotations`, **GET/POST** `/export/{epub,markdown,vault}` — Manage reading progress, study notes, and exports.

### Admin Allow-List API (Requires Admin Web Session)
- **GET** `/api/admin/allowlist` — List allow-listed emails and registration status
- **POST** `/api/admin/allowlist` — Add email (`{"email": "user@example.com"}`)
- **DELETE** `/api/admin/allowlist` — Remove email (`{"email": "user@example.com"}`)

### Machine Discovery APIs & Cross-Platform Client Agent
- **GET** `/api/machines` — List all registered machines with `machine_name`, `ipv4`, `ipv6`, `public_ip`, `serving_status`, `health_status` (`healthy`, `unhealthy`, `unresponsive`, `offline`), and `effective_status`. Supports `?status=`, `?health=`, and `?q=` filters.
- **POST** `/api/machines/register` — Register or update full machine metadata and reset heartbeat timer.
- **POST** `/api/machines/heartbeat` — Ultra-lightweight `60s` pulse (`204 No Content`, or `205 Reset Content` if full registration is required).
- **POST** `/api/machines/deregister` — Mark a machine `stopped` / `offline` on graceful shutdown.
- **DELETE** `/api/machines/<machine_id>` — Remove a retired machine entry.
- **GET** `/api/machines/agent.py` (or `/machines/agent.py`) — Download the zero-dependency Python 3 client script (`clients/machine_agent.py`) for Linux & Windows:

```bash
# Run client agent on Linux or Windows
python3 clients/machine_agent.py --server https://byang.japaneast.cloudapp.azure.com --api-key <API_KEY>

# Install as background service (systemd on Linux, Scheduled Task on Windows)
sudo python3 clients/machine_agent.py --server https://byang.japaneast.cloudapp.azure.com --api-key <API_KEY> --install-service
```

### Worker Service Discovery & Direct P2P Mesh APIs (`/services` & `clients/worker_sdk.py`)
- **Mandatory Worker Policy**: All worker-only endpoints (`GET /api/tts/worker/jobs`, `POST /api/tts/job/<id>/status`, `POST /api/tts/job/<id>/result`, and any future `@require_registered_worker("<service_type>")` routes) require an active worker registration lease (`X-Worker-Id` + `X-Worker-Lease`), returning `428 Precondition Required` if unregistered or expired (`>1.5×` `heartbeat_interval`).
- **GET** `/api/services` — List fleet summary KPIs, grouped `service_catalog`, and all registered workers (supports `ETag` / `If-None-Match` `304 Not Modified`, `?service_type=`, `?status=`, `?health=`, `?capability=`, and `?q=`). Accessible to logged-in web users and API-key clients.
- **GET** `/api/services/discover/<service_type>` — Pull routable online peer workers and their ranked P2P endpoints (supports `ETag` caching and `?capability=key:val`).
- **GET** `/api/services/<worker_id>` — Fetch full worker basic info, live status, custom statistics, and 5-minute historical metric rollups (`?hours=24`).
- **POST** `/api/services/register` — Register worker service metadata, capabilities, and P2P endpoints; returns `worker_lease_token`, `peer_auth_token`, and `peer_verify_secret`.
- **POST** `/api/services/heartbeat` — Send `30s` live status & statistical telemetry pulse (`204 No Content` with refreshed `X-Peer-Token`, or `205 Reset Content` to trigger automatic re-registration).
- **POST** `/api/services/deregister` & **DELETE** `/api/services/<worker_id>` — Gracefully mark a worker offline or delete a retired worker record.
- **GET** `/api/services/worker_sdk.py` (or `/services/worker_sdk.py`) — Download the standalone zero-dependency Python 3.8+ SDK (`clients/worker_sdk.py`) for building workers that call peer workers directly (`runtime.call_peer("tts", "synthesize", ...)` / `runtime.call_peer_raw(...)`) with offline HMAC `X-Peer-Token` verification and automatic replica failover.

---

## Auto-Restart Monitor

The `auto_restart.py` daemon monitors the standalone app and pulls updates daily at 2:00 AM from `prod`:

```bash
python auto_restart.py
```

## Running Tests

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```
