from flask import (
    Flask,
    Response,
    after_this_request,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
import datetime
import hmac
import mimetypes
import os
import secrets
import shutil
import tempfile
import uuid
import zipfile
from urllib.parse import urlparse

import requests

import db as job_db
from epub_reader.blueprint import create_epub_blueprint
from machine_discovery.blueprint import create_machine_discovery_blueprint
from ssl_utils import ensure_self_signed_cert

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Allow larger file uploads (500 MB limit for audio files)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024

# Initialize the database on startup
job_db.init_db()

app.secret_key = os.environ.get("TOOLBOX_SECRET_KEY") or job_db.get_setting("secret_key")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("TOOLBOX_INSECURE_COOKIES", "0") != "1"
app.config["PERMANENT_SESSION_LIFETIME"] = datetime.timedelta(days=7)

# Base URL for GitHub API
GITHUB_API_BASE = "https://api.github.com"

# Public endpoints that do not require an active session or API key
PUBLIC_ENDPOINTS = {"login", "register", "change_password", "logout", "static"}


# ============================================================
# Authentication & Security Helpers
# ============================================================

def get_csrf_token() -> str:
    """Return or generate a per-session CSRF token."""
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_hex(24)
        session["_csrf_token"] = token
    return token


def validate_csrf() -> bool:
    """Verify the submitted CSRF token matches the session token."""
    expected = session.get("_csrf_token")
    provided = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not expected or not provided:
        return False
    return hmac.compare_digest(str(provided), str(expected))


def extract_api_key() -> str:
    """Extract API key from X-API-Key header, Authorization Bearer/ApiKey header, or query param."""
    header_key = request.headers.get("X-API-Key")
    if header_key:
        return header_key.strip()

    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header:
        parts = auth_header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in ("bearer", "apikey", "token"):
            return parts[1].strip()

    query_key = request.args.get("api_key")
    if query_key:
        return query_key.strip()

    return ""


def has_valid_api_key() -> bool:
    """Validate provided API key against the configured shared service API key."""
    provided = extract_api_key()
    expected = job_db.get_api_key()
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)


def get_current_user():
    """Return the authenticated user dict from session, or None."""
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = job_db.get_user_by_id(user_id)
    if not user:
        session.clear()
        return None
    pwd_updated_at = session.get("pwd_updated_at")
    if pwd_updated_at and user["updated_at"] != pwd_updated_at:
        session.clear()
        return None
    return user


def safe_next_url(target: str) -> str:
    """Validate a relative redirect path to prevent open redirects."""
    if target and target.startswith("/") and not target.startswith("//") and not target.startswith("/\\"):
        return target
    return url_for("index")


@app.context_processor
def inject_template_globals():
    return {
        "current_user": getattr(g, "current_user", None),
        "csrf_token": get_csrf_token,
    }


@app.before_request
def enforce_authentication():
    """Global default-deny authentication enforcement for webpages and REST APIs."""
    g.current_user = get_current_user()
    g.is_api_authenticated = has_valid_api_key()

    if request.endpoint in PUBLIC_ENDPOINTS:
        return None

    # Admin REST APIs require an authenticated admin user session
    if request.path.startswith("/api/admin"):
        if not g.current_user:
            return jsonify({"error": "Authentication required"}), 401
        if not g.current_user["is_admin"]:
            return jsonify({"error": "Forbidden: Admin privileges required"}), 403
        return None

    # Service REST APIs accept the shared default API key OR an active logged-in session
    if request.path.startswith("/api/"):
        if g.is_api_authenticated or g.current_user is not None:
            return None
        return jsonify({
            "error": "Unauthorized. Provide a valid API key via X-API-Key or Authorization: Bearer header."
        }), 401

    # Admin web routes require an authenticated admin user
    if request.path.startswith("/admin"):
        if g.current_user is None:
            next_path = request.full_path.rstrip("?")
            return redirect(url_for("login", next=next_path))
        if not g.current_user["is_admin"]:
            return render_template("login.html", error="Forbidden: Admin privileges required."), 403
        return None

    # Download routes support both logged-in web users and API key clients
    if (
        request.path == "/download-repo"
        or request.path.startswith("/tts/download/")
        or request.path.startswith("/audio/")
    ):
        if g.current_user is not None or g.is_api_authenticated:
            return None
        if extract_api_key():
            return jsonify({"error": "Invalid API key"}), 401
        next_path = request.full_path.rstrip("?")
        return redirect(url_for("login", next=next_path))

    # All other webpages require login
    if g.current_user is None:
        next_path = request.full_path.rstrip("?")
        return redirect(url_for("login", next=next_path))

    return None


@app.after_request
def set_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    if request.is_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


# ============================================================
# User Authentication & Password Management Routes
# ============================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    """Sign-in page required to access Toolbox webpages."""
    next_url = request.args.get("next") or request.form.get("next") or ""
    if request.method == "GET":
        if g.current_user is not None:
            return redirect(safe_next_url(next_url))
        registered = request.args.get("registered")
        pwd_changed = request.args.get("pwd_changed")
        success = None
        if registered:
            success = "Account created successfully. Please sign in."
        elif pwd_changed:
            success = "Password updated successfully. Please sign in."
        return render_template("login.html", next_url=next_url, success=success)

    email = job_db.normalize_email(request.form.get("email", ""))
    password = request.form.get("password", "")

    if not email or not password:
        return render_template(
            "login.html",
            error="Please enter both email and password.",
            email=email,
            next_url=next_url,
        ), 400

    user = job_db.get_user_by_email(email)
    if not user or not check_password_hash(user["password_hash"], password):
        return render_template(
            "login.html",
            error="Invalid email or password.",
            email=email,
            next_url=next_url,
        ), 401

    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    session["pwd_updated_at"] = user["updated_at"]
    return redirect(safe_next_url(next_url))


@app.route("/register", methods=["GET", "POST"])
def register():
    """Register a new account (restricted to allow-listed email addresses)."""
    if request.method == "GET":
        if g.current_user is not None:
            return redirect(url_for("index"))
        return render_template("register.html")

    email = job_db.normalize_email(request.form.get("email", ""))
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not job_db.is_valid_email(email):
        return render_template(
            "register.html",
            error="Please provide a valid email address.",
            email=email,
        ), 400

    if not job_db.is_email_allowed(email):
        return render_template(
            "register.html",
            error="Registration is restricted to allow-listed email addresses. Please contact an administrator.",
            email=email,
        ), 403

    if job_db.get_user_by_email(email) is not None:
        return render_template(
            "register.html",
            error="An account with this email already exists. Please sign in or modify your password.",
            email=email,
        ), 400

    if len(password) < 8:
        return render_template(
            "register.html",
            error="Password must be at least 8 characters long.",
            email=email,
        ), 400

    if password != confirm_password:
        return render_template(
            "register.html",
            error="Passwords do not match.",
            email=email,
        ), 400

    pwd_hash = generate_password_hash(password)
    user = job_db.create_user(email, pwd_hash)

    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    session["pwd_updated_at"] = user["updated_at"]
    return redirect(url_for("index"))


@app.route("/change-password", methods=["GET", "POST"])
def change_password():
    """Modify password (restricted to allow-listed email addresses)."""
    default_email = g.current_user["email"] if g.current_user else ""
    if request.method == "GET":
        return render_template("change_password.html", email=default_email)

    email = job_db.normalize_email(request.form.get("email", "") or default_email)
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not job_db.is_valid_email(email):
        return render_template(
            "change_password.html",
            error="Please provide a valid email address.",
            email=email,
        ), 400

    if not job_db.is_email_allowed(email):
        return render_template(
            "change_password.html",
            error="Only allow-listed email users are permitted to modify the password.",
            email=email,
        ), 403

    user = job_db.get_user_by_email(email)
    if not user or not check_password_hash(user["password_hash"], current_password):
        return render_template(
            "change_password.html",
            error="Invalid email or current password.",
            email=email,
        ), 401

    if len(new_password) < 8:
        return render_template(
            "change_password.html",
            error="New password must be at least 8 characters long.",
            email=email,
        ), 400

    if new_password != confirm_password:
        return render_template(
            "change_password.html",
            error="New passwords do not match.",
            email=email,
        ), 400

    job_db.update_user_password(email, generate_password_hash(new_password))
    updated_user = job_db.get_user_by_email(email)

    if g.current_user and g.current_user["id"] == updated_user["id"]:
        session["pwd_updated_at"] = updated_user["updated_at"]
        return render_template(
            "change_password.html",
            email=email,
            success="Your password has been updated successfully.",
        )

    return redirect(url_for("login", pwd_changed="1"))


@app.route("/logout", methods=["GET", "POST"])
def logout():
    """Sign out of the current web session."""
    session.clear()
    return redirect(url_for("login"))


# ============================================================
# Admin Configuration Routes (Allow-List, Users, API Key)
# ============================================================

def _render_admin(error=None, success=None, status_code=200):
    return (
        render_template(
            "admin.html",
            allowed_emails=job_db.list_allowed_emails(),
            users=job_db.list_users(),
            api_key=job_db.get_api_key(),
            error=error,
            success=success,
        ),
        status_code,
    )


@app.route("/admin", methods=["GET"])
def admin_panel():
    """Admin dashboard to manage the email allow-list, users, and API key."""
    return _render_admin()


@app.route("/admin/allowlist/add", methods=["POST"])
def admin_allowlist_add():
    if not validate_csrf():
        return _render_admin(error="Invalid session token. Please refresh and try again.", status_code=400)
    email = job_db.normalize_email(request.form.get("email", ""))
    if not job_db.is_valid_email(email):
        return _render_admin(error="Please enter a valid email address.", status_code=400)
    added = job_db.add_allowed_email(email, added_by=g.current_user["email"])
    if not added:
        return _render_admin(error=f"{email} is already in the allow list.", status_code=400)
    return _render_admin(success=f"Added {email} to the allow list.")


@app.route("/admin/allowlist/remove", methods=["POST"])
def admin_allowlist_remove():
    if not validate_csrf():
        return _render_admin(error="Invalid session token. Please refresh and try again.", status_code=400)
    email = job_db.normalize_email(request.form.get("email", ""))
    if email == g.current_user["email"]:
        return _render_admin(error="Cannot remove your own active admin email from the allow list.", status_code=400)
    removed = job_db.remove_allowed_email(email)
    if not removed:
        return _render_admin(error=f"{email} was not found in the allow list.", status_code=404)
    return _render_admin(success=f"Removed {email} from the allow list.")


@app.route("/admin/users/toggle-admin", methods=["POST"])
def admin_toggle_user_role():
    if not validate_csrf():
        return _render_admin(error="Invalid session token. Please refresh and try again.", status_code=400)
    try:
        user_id = int(request.form.get("user_id", "0"))
    except ValueError:
        return _render_admin(error="Invalid user ID.", status_code=400)
    if user_id == g.current_user["id"]:
        return _render_admin(error="Cannot revoke your own admin role.", status_code=400)
    target = job_db.get_user_by_id(user_id)
    if not target:
        return _render_admin(error="User not found.", status_code=404)
    new_role = not bool(target["is_admin"])
    job_db.set_user_admin(user_id, new_role)
    role_label = "Admin" if new_role else "Standard User"
    return _render_admin(success=f"Updated {target['email']} to {role_label}.")


@app.route("/admin/users/delete", methods=["POST"])
def admin_delete_user():
    if not validate_csrf():
        return _render_admin(error="Invalid session token. Please refresh and try again.", status_code=400)
    try:
        user_id = int(request.form.get("user_id", "0"))
    except ValueError:
        return _render_admin(error="Invalid user ID.", status_code=400)
    if user_id == g.current_user["id"]:
        return _render_admin(error="Cannot delete your own active admin account.", status_code=400)
    target = job_db.get_user_by_id(user_id)
    if not target:
        return _render_admin(error="User not found.", status_code=404)
    job_db.delete_user(user_id)
    return _render_admin(success=f"Deleted user account {target['email']}.")


@app.route("/admin/api-key/update", methods=["POST"])
def admin_update_api_key():
    if not validate_csrf():
        return _render_admin(error="Invalid session token. Please refresh and try again.", status_code=400)
    new_key = request.form.get("api_key", "").strip()
    if len(new_key) < 8:
        return _render_admin(error="API key must be at least 8 characters long.", status_code=400)
    job_db.set_api_key(new_key)
    return _render_admin(success="Shared REST API key updated successfully.")


@app.route("/api/admin/allowlist", methods=["GET", "POST", "DELETE"])
def api_admin_allowlist():
    """JSON endpoint for admin allow-list configuration."""
    if request.method == "GET":
        return jsonify({"allowed_emails": job_db.list_allowed_emails()})

    data = request.get_json(silent=True) or {}
    email = job_db.normalize_email(data.get("email") or request.args.get("email") or "")
    if not job_db.is_valid_email(email):
        return jsonify({"error": "Invalid email address format"}), 400

    if request.method == "POST":
        added = job_db.add_allowed_email(email, added_by=g.current_user["email"])
        return jsonify({"email": email, "added": added}), (201 if added else 200)

    if email == g.current_user["email"]:
        return jsonify({"error": "Cannot remove your own active admin email"}), 400
    removed = job_db.remove_allowed_email(email)
    if not removed:
        return jsonify({"error": "Email not found in allow list"}), 404
    return jsonify({"email": email, "removed": True})


# ============================================================
# GitHub Repo Downloader Helpers & Routes
# ============================================================

def extract_repo_info(github_url):
    """Extract owner and repo name from a GitHub URL."""
    parsed_url = urlparse(github_url)
    path_parts = [p for p in parsed_url.path.split("/") if p]

    if len(path_parts) < 2:
        raise ValueError("Invalid GitHub repository URL")

    owner = path_parts[0]
    repo = path_parts[1]

    if repo.endswith(".git"):
        repo = repo[:-4]

    return owner, repo


def get_repo_contents(owner, repo, path="", token=None):
    """Get repository contents recursively using GitHub API."""
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"token {token}"

    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{path}"
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()

    contents = response.json()
    files = []

    for item in contents:
        if item["type"] == "file":
            files.append(item)
        elif item["type"] == "dir":
            sub_files = get_repo_contents(owner, repo, item["path"], token)
            files.extend(sub_files)

    return files


def download_github_repo(github_url, token=None):
    """Download a GitHub repository and create a zip file."""
    owner, repo = extract_repo_info(github_url)
    files = get_repo_contents(owner, repo, "", token)

    temp_dir = tempfile.mkdtemp()
    zip_path = os.path.join(temp_dir, f"{repo}.zip")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for file_info in files:
            try:
                response = requests.get(file_info["download_url"], timeout=30)
                response.raise_for_status()
                zipf.writestr(file_info["path"], response.content)
            except Exception as e:
                print(f"Failed to download {file_info['path']}: {e}")
                continue

    return zip_path, temp_dir


def _send_zip_and_cleanup(zip_path: str, temp_dir: str, repo: str):
    @after_this_request
    def _cleanup(response):
        shutil.rmtree(temp_dir, ignore_errors=True)
        return response

    return send_file(zip_path, as_attachment=True, download_name=f"{repo}.zip")


# ============================================================
# Text-to-Voice: Web Routes & Audio Serving
# ============================================================

def _serve_audio_file(filepath: str):
    """Serve an audio file via Nginx X-Accel-Redirect in production or send_file locally."""
    filename = os.path.basename(filepath)
    mime_type = mimetypes.guess_type(filename)[0] or "audio/mpeg"

    if request.headers.get("X-Nginx-Proxy") == "1":
        resp = Response(status=200, mimetype=mime_type)
        resp.headers["X-Accel-Redirect"] = f"/internal-audio/{filename}"
        resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp

    return send_file(
        filepath,
        as_attachment=True,
        download_name=filename,
        mimetype=mime_type,
    )


@app.route("/text-to-voice")
def text_to_voice():
    """Text-to-voice tool page."""
    return render_template("text_to_voice.html")


@app.route("/api/tts/submit", methods=["POST"])
def tts_submit():
    """Submit a new text-to-voice job."""
    text = ""
    language = "en"

    if request.is_json:
        data = request.get_json() or {}
        text = data.get("text", "").strip()
        language = data.get("language", "en").strip()
    else:
        text = request.form.get("text", "").strip()
        language = request.form.get("language", "en").strip()

    if not text:
        return jsonify({"error": "Please provide some text"}), 400

    if len(text) > 5000:
        return jsonify({"error": "Text is too long. Maximum 5000 characters allowed."}), 400

    job_id = str(uuid.uuid4())
    job_db.add_job(job_id, text, language)

    return jsonify({"job_id": job_id, "status": "pending"}), 201


@app.route("/tts/submit", methods=["POST"])
def tts_submit_form():
    """Handle form-based job submission (redirect back to page)."""
    text = request.form.get("text", "").strip()
    language = request.form.get("language", "en").strip()

    if not text:
        return render_template("text_to_voice.html", error="Please provide some text")

    if len(text) > 5000:
        return render_template("text_to_voice.html", error="Text is too long. Maximum 5000 characters allowed.")

    job_id = str(uuid.uuid4())
    job_db.add_job(job_id, text, language)

    return redirect(url_for("text_to_voice"))


@app.route("/tts/download/<job_id>", methods=["GET"])
def tts_download(job_id):
    """Authenticated audio file download."""
    job = job_db.get_job(job_id)
    if not job:
        return render_template("text_to_voice.html", error="Job not found"), 404

    if job["status"] != "completed":
        return render_template("text_to_voice.html", error="Job is not completed yet"), 400

    if not job["audio_path"] or not os.path.exists(job["audio_path"]):
        return render_template("text_to_voice.html", error="Audio file not found"), 404

    return _serve_audio_file(job["audio_path"])


@app.route("/api/tts/job/<job_id>/download", methods=["GET"])
def tts_api_download(job_id):
    """REST API audio file download."""
    job = job_db.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["status"] != "completed":
        return jsonify({"error": "Job is not completed yet"}), 400
    if not job["audio_path"] or not os.path.exists(job["audio_path"]):
        return jsonify({"error": "Audio file not found"}), 404
    return _serve_audio_file(job["audio_path"])


@app.route("/audio/<filename>")
def tts_audio_static(filename):
    """Authenticated audio file fallback route."""
    safe_name = os.path.basename(filename)
    audio_dir = os.path.join(app.instance_path, "audio")
    filepath = os.path.join(audio_dir, safe_name)

    if not os.path.exists(filepath):
        return "Audio file not found", 404

    return _serve_audio_file(filepath)


# ============================================================
# Text-to-Voice: VibeVoice REST APIs
# ============================================================

@app.route("/api/tts/jobs", methods=["GET"])
def tts_list_jobs():
    """List all text-to-voice jobs."""
    status_filter = request.args.get("status")
    job_list = job_db.list_jobs(status_filter)

    result = []
    for job in job_list:
        result.append({
            "id": job["id"],
            "text": job["text"][:100] + "..." if len(job["text"]) > 100 else job["text"],
            "language": job["language"],
            "status": job["status"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
        })

    return jsonify({"jobs": result})


@app.route("/api/tts/job/<job_id>", methods=["GET"])
def tts_get_job(job_id):
    """Get a specific text-to-voice job."""
    job = job_db.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    return jsonify(job)


@app.route("/api/tts/job/<job_id>/result", methods=["POST"])
def tts_upload_result(job_id):
    """Upload audio result for a job."""
    job = job_db.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    error_msg = request.form.get("error")
    if error_msg:
        job_db.update_job_status(job_id, "failed", error=error_msg)
        return jsonify({"status": "failed", "job_id": job_id})

    if "audio" not in request.files:
        return jsonify({"error": "No audio file provided"}), 400

    audio_file = request.files["audio"]
    if audio_file.filename == "":
        return jsonify({"error": "No audio file selected"}), 400

    audio_dir = os.path.join(app.instance_path, "audio")
    os.makedirs(audio_dir, exist_ok=True)

    ext = os.path.splitext(audio_file.filename)[1] or ".mp3"
    filename = f"{job_id}{ext}"
    audio_path = os.path.join(audio_dir, filename)

    audio_file.save(audio_path)
    job_db.update_job_audio_path(job_id, audio_path)

    return jsonify({"status": "completed", "job_id": job_id})


@app.route("/api/tts/job/<job_id>/status", methods=["POST"])
def tts_update_status(job_id):
    """Update job status (processing, failed, etc.)."""
    job = job_db.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    data = request.get_json(silent=True) or {}
    new_status = data.get("status")

    if new_status not in ("pending", "processing", "completed", "failed"):
        return jsonify({"error": "Invalid status. Must be: pending, processing, completed, failed"}), 400

    error_msg = data.get("error")
    job_db.update_job_status(job_id, new_status, error=error_msg)

    return jsonify({"status": new_status, "job_id": job_id})


@app.route("/api/tts/job/<job_id>/retry", methods=["POST"])
def tts_retry_job(job_id):
    """Retry a failed job by resetting its status to pending."""
    job = job_db.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    if job["status"] != "failed":
        return jsonify({"error": "Only failed jobs can be retried"}), 400

    job_db.update_job_status(job_id, "pending")
    return jsonify({"success": True, "job_id": job_id})


@app.route("/api/tts/job/<job_id>", methods=["DELETE"])
def tts_delete_job(job_id):
    """Delete a job and its associated audio file."""
    job = job_db.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    if job["status"] == "processing":
        return jsonify({"error": "Cannot delete a job that is currently being processed"}), 400

    job_db.delete_job(job_id)
    return jsonify({"success": True, "job_id": job_id})


# ============================================================
# GitHub Downloader Web & REST API Routes
# ============================================================

@app.route("/")
def index():
    """Home page with list of tools."""
    return render_template("index.html")


@app.route("/github-downloader")
def github_downloader():
    """GitHub repository downloader tool page."""
    return render_template("github_downloader.html")


@app.route("/download-repo", methods=["POST"])
def download_repo():
    """Handle GitHub repository download from web form."""
    github_url = request.form.get("github_url", "").strip()
    token = request.form.get("token", "").strip()

    if not github_url:
        return render_template("github_downloader.html", error="Please provide a GitHub repository URL")

    try:
        zip_path, temp_dir = download_github_repo(github_url, token if token else None)
        _, repo = extract_repo_info(github_url)
        return _send_zip_and_cleanup(zip_path, temp_dir, repo)
    except requests.exceptions.HTTPError as e:
        error_msg = "Failed to access repository. Check if the URL is correct and the repository exists."
        if e.response is not None and e.response.status_code == 401:
            error_msg = "Authentication failed. Please check your GitHub token."
        elif e.response is not None and e.response.status_code == 404:
            error_msg = "Repository not found. Please check the URL."
        return render_template("github_downloader.html", error=error_msg)
    except ValueError as e:
        return render_template("github_downloader.html", error=str(e))
    except Exception as e:
        return render_template("github_downloader.html", error=f"An error occurred: {str(e)}")


@app.route("/api/github/download", methods=["GET", "POST"])
def api_github_download():
    """REST API endpoint to download a GitHub repository as a ZIP file."""
    if request.is_json:
        data = request.get_json(silent=True) or {}
        github_url = (data.get("github_url") or "").strip()
        token = (data.get("token") or "").strip()
    elif request.method == "POST":
        github_url = request.form.get("github_url", "").strip()
        token = request.form.get("token", "").strip()
    else:
        github_url = request.args.get("github_url", "").strip()
        token = request.args.get("token", "").strip()

    if not github_url:
        return jsonify({"error": "Please provide github_url"}), 400

    try:
        zip_path, temp_dir = download_github_repo(github_url, token if token else None)
        _, repo = extract_repo_info(github_url)
        return _send_zip_and_cleanup(zip_path, temp_dir, repo)
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else 502
        if status == 401:
            return jsonify({"error": "GitHub authentication failed. Check token."}), 401
        if status == 404:
            return jsonify({"error": "GitHub repository not found."}), 404
        return jsonify({"error": "Failed to fetch repository from GitHub."}), status
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Failed to download repository: {str(e)}"}), 500


# ============================================================
# Encrypted Active-Recall EPUB Reader Blueprint
# ============================================================

app.register_blueprint(
    create_epub_blueprint(
        project_root=os.path.dirname(os.path.abspath(__file__)),
        get_csrf_token=get_csrf_token,
        validate_csrf=validate_csrf,
    )
)

app.register_blueprint(
    create_machine_discovery_blueprint(
        project_root=os.path.dirname(os.path.abspath(__file__)),
        validate_csrf=validate_csrf,
    )
)


if __name__ == "__main__":
    port = int(os.environ.get("TOOLBOX_PORT", "443"))
    ssl_dir = os.path.join(app.instance_path, "ssl")
    cert_path, key_path = ensure_self_signed_cert(ssl_dir)
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        threaded=True,
        ssl_context=(cert_path, key_path),
    )
