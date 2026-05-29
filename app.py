from flask import Flask, render_template, request, send_file, redirect, url_for
import os
import tempfile
import zipfile
import requests
import gtts
from urllib.parse import urlparse

app = Flask(__name__)

# Base URL for GitHub API
GITHUB_API_BASE = "https://api.github.com"
# GitHub repositories raw content URL
GITHUB_RAW_BASE = "https://raw.githubusercontent.com"


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


def text_to_speech(text, language="en"):
    """Convert text to speech and return audio file path."""
    temp_dir = tempfile.mkdtemp()
    audio_path = os.path.join(temp_dir, "speech.mp3")
    
    tts = gtts.gTTS(text, lang=language)
    tts.save(audio_path)
    
    return audio_path


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
        error_msg = f"Failed to access repository. Check if the URL is correct and the repository exists."
        if e.response.status_code == 401:
            error_msg = "Authentication failed. Please check your GitHub token."
        elif e.response.status_code == 404:
            error_msg = "Repository not found. Please check the URL."
        return render_template("github_downloader.html", error=error_msg)
    except ValueError as e:
        return render_template("github_downloader.html", error=str(e))
    except Exception as e:
        return render_template("github_downloader.html", error=f"An error occurred: {str(e)}")


@app.route("/text-to-voice")
def text_to_voice():
    """Text-to-voice tool page."""
    return render_template("text_to_voice.html")


@app.route("/generate-voice", methods=["POST"])
def generate_voice():
    """Handle text-to-voice conversion."""
    text = request.form.get("text", "").strip()
    language = request.form.get("language", "en")
    
    if not text:
        return render_template("text_to_voice.html", error="Please provide some text")
    
    if len(text) > 5000:
        return render_template("text_to_voice.html", error="Text is too long. Maximum 5000 characters allowed.")
    
    try:
        audio_path = text_to_speech(text, language)
        return send_file(
            audio_path,
            as_attachment=True,
            download_name="speech.mp3"
        )
    except Exception as e:
        return render_template("text_to_voice.html", error=f"An error occurred: {str(e)}")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
