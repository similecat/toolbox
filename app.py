from flask import Flask, render_template, request, send_file, redirect, url_for, jsonify
import os
import tempfile
import zipfile
import requests
import uuid
import datetime
from urllib.parse import urlparse

import db as job_db

app = Flask(__name__)

# Allow larger file uploads (500 MB limit for audio files)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024

# Base URL for GitHub API
GITHUB_API_BASE = "https://api.github.com"
# GitHub repositories raw content URL
GITHUB_RAW_BASE = "https://raw.githubusercontent.com"

# Initialize the database on startup
job_db.init_db()


def extract_repo_info(github_url):
    """Extract owner and repo name from a GitHub URL."""
    parsed_url = urlparse(github_url)
    path_parts = [p for p in parsed_url.path.split("/") if p]
    
    if len(path_parts) < 2:
        raise ValueError("Invalid GitHub repository URL")
    
    owner = path_parts[0]
    repo = path_parts[1]
    
    # Remove .git suffix if present
    if repo.endswith(".git"):
        repo = repo[:-4]
    
    return owner, repo


def get_repo_contents(owner, repo, path="", token=None):
    """Get repository contents recursively using GitHub API."""
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"token {token}"
    
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{path}"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    
    contents = response.json()
    files = []
    
    for item in contents:
        if item["type"] == "file":
            files.append(item)
        elif item["type"] == "dir":
            # Recursively get contents of directories
            sub_files = get_repo_contents(owner, repo, item["path"], token)
            files.extend(sub_files)
    
    return files


def download_github_repo(github_url, token=None):
    """Download a GitHub repository and create a zip file."""
    owner, repo = extract_repo_info(github_url)
    
    # Get all files in the repository
    files = get_repo_contents(owner, repo, "", token)
    
    # Create a temporary zip file
    temp_dir = tempfile.mkdtemp()
    zip_path = os.path.join(temp_dir, f"{repo}.zip")
    
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for file_info in files:
            try:
                # Download file content
                response = requests.get(file_info["download_url"])
                response.raise_for_status()
                
                # Add to zip with proper path
                zipf.writestr(file_info["path"], response.content)
            except Exception as e:
                print(f"Failed to download {file_info['path']}: {e}")
                continue
    
    return zip_path


# ============================================================
# Text-to-Voice: Web Routes (Submit, List, Download)
# ============================================================

@app.route("/text-to-voice")
def text_to_voice():
    """Text-to-voice tool page."""
    return render_template("text_to_voice.html")


@app.route("/api/tts/submit", methods=["POST"])
def tts_submit():
    """Submit a new text-to-voice job.
    
    Accepts JSON or form data.
    Returns: {"job_id": "...", "status": "pending"}
    """
    text = ""
    language = "en"
    
    if request.is_json:
        data = request.get_json()
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
    """Download audio file for a completed job."""
    job = job_db.get_job(job_id)
    if not job:
        return render_template("text_to_voice.html", error="Job not found")
    
    if job["status"] != "completed":
        return render_template("text_to_voice.html", error="Job is not completed yet")
    
    if not job["audio_path"] or not os.path.exists(job["audio_path"]):
        return render_template("text_to_voice.html", error="Audio file not found")
    
    return send_file(
        job["audio_path"],
        as_attachment=True,
        download_name=f"voice_{job_id}.mp3",
        mimetype="audio/mpeg"
    )


# ============================================================
# Text-to-Voice: VibeVoice APIs
# ============================================================

@app.route("/api/tts/jobs", methods=["GET"])
def tts_list_jobs():
    """List all text-to-voice jobs.
    
    VibeVoice can use this to discover pending jobs.
    Optional query param: ?status=pending to filter.
    Returns: {"jobs": [...]}
    """
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
    """Get a specific text-to-voice job.
    
    VibeVoice can use this to get full job details including the full text.
    Returns: {"id", "text", "language", "status", "created_at", "updated_at", "error"}
    """
    job = job_db.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    
    return jsonify(job)


@app.route("/api/tts/job/<job_id>/result", methods=["POST"])
def tts_upload_result(job_id):
    """Upload audio result for a job.
    
    VibeVoice calls this after generating the audio.
    Expects multipart form data with 'audio' file field.
    Optional: 'error' field if processing failed.
    Returns: {"status": "completed" or "failed", "job_id": "..."}
    """
    job = job_db.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    
    # Check if processing failed
    error_msg = request.form.get("error")
    if error_msg:
        job_db.update_job_status(job_id, "failed", error=error_msg)
        return jsonify({"status": "failed", "job_id": job_id})
    
    # Check if audio file is uploaded
    if "audio" not in request.files:
        return jsonify({"error": "No audio file provided"}), 400
    
    audio_file = request.files["audio"]
    if audio_file.filename == "":
        return jsonify({"error": "No audio file selected"}), 400
    
    # Save the audio file
    audio_dir = os.path.join(app.instance_path, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    
    # Preserve original extension
    ext = os.path.splitext(audio_file.filename)[1] or ".mp3"
    filename = f"{job_id}{ext}"
    audio_path = os.path.join(audio_dir, filename)
    
    audio_file.save(audio_path)
    
    # Update job status
    job_db.update_job_audio_path(job_id, audio_path)
    
    return jsonify({"status": "completed", "job_id": job_id})


@app.route("/api/tts/job/<job_id>/status", methods=["POST"])
def tts_update_status(job_id):
    """Update job status (processing, failed, etc.).
    
    VibeVoice can use this to update job status without uploading audio.
    Expects JSON: {"status": "processing" | "failed", "error": "..."}
    Returns: {"status": "...", "job_id": "..."}
    """
    job = job_db.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    
    data = request.get_json()
    new_status = data.get("status")
    
    if new_status not in ("pending", "processing", "completed", "failed"):
        return jsonify({"error": "Invalid status. Must be: pending, processing, completed, failed"}), 400
    
    error_msg = data.get("error")
    job_db.update_job_status(job_id, new_status, error=error_msg)
    
    return jsonify({"status": job["status"], "job_id": job_id})


@app.route("/api/tts/job/<job_id>", methods=["DELETE"])
def tts_delete_job(job_id):
    """Delete a job and its associated audio file.
    
    Returns: {"success": true, "job_id": "..."}
    """
    job = job_db.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    
    # Don't allow deleting jobs that are being processed
    if job["status"] == "processing":
        return jsonify({"error": "Cannot delete a job that is currently being processed"}), 400
    
    job_db.delete_job(job_id)
    
    return jsonify({"success": True, "job_id": job_id})


# ============================================================
# GitHub Downloader Routes
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
    """Handle GitHub repository download."""
    github_url = request.form.get("github_url", "").strip()
    token = request.form.get("token", "").strip()
    
    if not github_url:
        return render_template("github_downloader.html", error="Please provide a GitHub repository URL")
    
    try:
        zip_path = download_github_repo(github_url, token if token else None)
        owner, repo = extract_repo_info(github_url)
        return send_file(
            zip_path,
            as_attachment=True,
            download_name=f"{repo}.zip"
        )
    except requests.exceptions.HTTPError as e:
        error_msg = "Failed to access repository. Check if the URL is correct and the repository exists."
        if e.response.status_code == 401:
            error_msg = "Authentication failed. Please check your GitHub token."
        elif e.response.status_code == 404:
            error_msg = "Repository not found. Please check the URL."
        return render_template("github_downloader.html", error=error_msg)
    except ValueError as e:
        return render_template("github_downloader.html", error=str(e))
    except Exception as e:
        return render_template("github_downloader.html", error=f"An error occurred: {str(e)}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80, debug=False)
