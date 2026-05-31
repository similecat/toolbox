# Web Toolbox
A collection of useful web tools built with Python Flask.

## Features
- **GitHub Repo Downloader**: Download any GitHub repository as a ZIP file
- **Text to Voice**: Submit text for voice generation with job-based processing

## Quick Start

```bash
# Clone the repository
git clone git@github.com:similecat/toolbox.git
cd toolbox

# Install dependencies
pip install -r requirements.txt

# Start the auto-restart monitor (recommended for production)
python auto_restart.py

# Or start manually (Windows)
python run.py
# or
waitress-serve --host=0.0.0.0 --port=80 run:app

# Or start manually (Linux / WSL)
gunicorn -w 1 -k gevent --bind 127.0.0.1:5000 app:app
```

Open your browser and navigate to `http://localhost`

> **Note:** Port 80 is a privileged port. On Windows, you must **run Python as Administrator** to bind to it. The default port is `5000`.

## Auto-Restart Monitor

The `auto_restart.py` script is a cross-platform daemon that keeps the app running and automatically updates it daily.

### Features
- **Daily Auto-Update**: At 2:00 AM every day, stops the app, pulls latest code from `prod` branch, and restarts
- **Cross-Platform**: Works on both Windows and Linux
- **Auto-Recovery**: Starts the app on launch if not already running
- **Graceful Shutdown**: Press `Ctrl+C` to stop cleanly

### Usage

```bash
# Start the monitor
python auto_restart.py

# The script will:
# 1. Start the Flask app if not running
# 2. Monitor and wait until 2:00 AM
# 3. Stop app → git pull → restart app
# 4. Repeat daily
```

### Configuration

Edit these variables at the top of `auto_restart.py`:

| Variable | Default | Description |
|---|---|---|
| `RESTART_HOUR` | `2` | Hour to perform daily restart (24h format) |
| `RESTART_MINUTE` | `0` | Minute to perform daily restart |
| `CHECK_INTERVAL` | `60` | Seconds between status checks |
| `BRANCH` | `"prod"` | Git branch to pull from |

## Setup

### Full Setup

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Install VibeVoice (for Text-to-Voice feature):
```bash
cd VibeVoice
pip install -e .
```

3. Run the application (choose one method):

**Option A — Auto-restart monitor (recommended):**
```bash
python auto_restart.py
```

**Option B — Manual start (Windows):**
```bash
python run.py
```

**Option B2 — Manual start (Windows, waitress CLI):**
```bash
waitress-serve --host=127.0.0.1 --port=8000 run:app
```

**Option C — Manual start (Linux / WSL with Gunicorn):**
```bash
gunicorn -w 1 -k gevent --bind 0.0.0.0:5000 app:app
```

> **Note:** Gunicorn requires Unix/Linux (it uses `fcntl`). On Windows, use `python run.py` or `waitress-serve`, both of which use waitress, a cross-platform WSGI server.

4. Open your browser and navigate to `http://localhost`

## Production Deployment (Ubuntu / Linux)

For production use on Ubuntu/Debian, the recommended setup is **Nginx** as a reverse proxy on port 80, with **Gunicorn** serving the Flask app via a Unix socket.

### Quick Setup Script

A one-command setup script is provided:

```bash
# Clone the repo, deploy to /opt/toolbox, and configure nginx + gunicorn
sudo bash setup_nginx.sh
```

The script will:
1. Clone or update the app to `/opt/toolbox` (from `prod` branch)
2. Install nginx
3. Install Python dependencies
4. Create a `systemd` service for gunicorn (with gevent workers)
5. Configure nginx to proxy port 80 to gunicorn via a Unix socket
6. Enable and start both services

### Manual Setup

If you prefer to set things up manually:

```bash
# 1. Install nginx
sudo apt update && sudo apt install -y nginx

# 2. Install Python dependencies
pip3 install -r requirements.txt

# 3. Start gunicorn (Unix socket recommended for production)
sudo gunicorn -w 2 -k gevent \
    --bind unix:/run/gunicorn/toolbox.sock \
    --access-logfile - --error-logfile - \
    app:app
```

Then configure nginx (`/etc/nginx/sites-available/toolbox`):

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://unix:/run/gunicorn/toolbox.sock;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
    }
}
```

Enable the site and restart nginx:

```bash
sudo ln -s /etc/nginx/sites-available/toolbox /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

### Managing Services

| Command | Description |
|---|---|
| `sudo systemctl status toolbox` | Check gunicorn status |
| `sudo systemctl restart toolbox` | Restart gunicorn |
| `sudo systemctl status nginx` | Check nginx status |
| `journalctl -u toolbox` | View gunicorn logs |
| `/var/log/nginx/` | Nginx access/error logs |

> **Tip:** After deployment, the TTS worker should point to the correct base URL:
> ```bash
> cd VibeVoice/demo && python tts_worker.py --base_url http://localhost
> ```

## Tools

### GitHub Repo Downloader
- Enter a GitHub repository URL
- Download the entire repository as a ZIP file
- Supports both public and private repositories (with token)

### Text to Voice
- Submit text in English or Chinese for voice generation
- Jobs are processed asynchronously by vibevoice
- Download audio files when processing is complete
- Maximum 5000 characters per submission

## Running the TTS Worker

The TTS worker is a background service that polls for pending jobs and generates audio using the VibeVoice model.

### Prerequisites

1. Ensure the VibeVoice model is downloaded locally at `VibeVoice/models/VibeVoice-1.5b` (or it will be fetched from HuggingFace automatically).
2. Install Python dependencies in the VibeVoice directory:
   ```bash
   cd VibeVoice
   pip install -e .
   ```

### Starting the Worker

**Terminal 1** — Start the web application (or use auto_restart.py):

**Windows:**
```bash
python run.py
```
or
```bash
waitress-serve --host=127.0.0.1 --port=8000 run:app
```

**Linux / WSL:**
```bash
gunicorn -w 1 -k gevent --bind 0.0.0.0:5000 app:app
```

> **Tip:** For production use, replace the above with `python auto_restart.py` to get automatic daily updates at 2 AM.

**Terminal 2** — Start the TTS worker:
```bash
cd VibeVoice/demo
python tts_worker.py
```

### Worker Command-Line Options

| Option | Default | Description |
|---|---|---|
| `--base_url` | `http://localhost` | Base URL of the toolbox API |
| `--model_path` | Auto-detect | Path to VibeVoice model (auto-detects `VibeVoice/models/VibeVoice-1.5b`) |
| `--device` | Auto-detect | Device for inference: `cuda`, `mps`, `cpu` |
| `--interval` | `5` | Polling interval in seconds |
| `--cfg_scale` | `1.3` | CFG scale for generation |
| `--seed` | None | Random seed for reproducibility |

**Example with custom options:**
```bash
python tts_worker.py --base_url http://localhost --device cuda --interval 10
```

### Speaker Configuration

The worker uses the following default speakers:
- **English (`en`)**: Alice
- **Chinese (`zh`)**: Xinran

You can modify the speaker mapping in `VibeVoice/demo/tts_worker.py` under `SPEAKER_MAP`.

### How It Works

1. Worker polls `/api/tts/jobs?status=pending` every N seconds
2. Picks up the oldest pending job (FIFO order)
3. Marks the job as `processing`
4. Generates audio using VibeVoice model
5. Uploads the audio result back to the toolbox
6. Job status changes to `completed` and the user can download the audio

Press `Ctrl+C` to stop the worker gracefully.

## VibeVoice API Reference

The Text-to-Voice tool provides REST APIs for vibevoice to process jobs.

### Submit a Job (User-facing)

**POST** `/api/tts/submit`

Submit a new text-to-voice job.

**Request Body** (JSON or form data):
```json
{
  "text": "Hello, this is a test.",
  "language": "en"
}
```

**Response**:
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "pending"
}
```

### List All Jobs

**GET** `/api/tts/jobs`

List all text-to-voice jobs.

**Query Parameters**:
- `status` (optional): Filter by status (`pending`, `processing`, `completed`, `failed`)

**Response**:
```json
{
  "jobs": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "text": "Hello...",
      "language": "en",
      "status": "pending",
      "created_at": "2026-05-29T21:00:00",
      "updated_at": "2026-05-29T21:00:00"
    }
  ]
}
```

### Get Job Details

**GET** `/api/tts/job/<job_id>`

Get full details of a specific job including the complete text.

**Response**:
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "text": "Hello, this is a full test.",
  "language": "en",
  "status": "pending",
  "created_at": "2026-05-29T21:00:00",
  "updated_at": "2026-05-29T21:00:00",
  "error": null
}
```

### Update Job Status

**POST** `/api/tts/job/<job_id>/status`

Update the status of a job (e.g., mark as processing).

**Request Body**:
```json
{
  "status": "processing",
  "error": null
}
```

**Response**:
```json
{
  "status": "processing",
  "job_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

### Upload Audio Result

**POST** `/api/tts/job/<job_id>/result`

Upload the generated audio file for a job.

**Request** (multipart form data):
- `audio`: The audio file (MP3, WAV, etc.)
- `error` (optional): Error message if processing failed

**Response**:
```json
{
  "status": "completed",
  "job_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

### Download Audio (User-facing)

**GET** `/tts/download/<job_id>`

Download the audio file for a completed job.

## Job Workflow

1. **User** submits text via the web UI → Job created with `pending` status
2. **vibevoice** polls `/api/tts/jobs?status=pending` to find new jobs
3. **vibevoice** gets job details via `/api/tts/job/<job_id>`
4. **vibevoice** updates status to `processing` via `/api/tts/job/<job_id>/status`
5. **vibevoice** generates audio and uploads via `/api/tts/job/<job_id>/result`
6. **User** sees `completed` status and downloads audio via `/tts/download/<job_id>`

### Job Statuses
- `pending`: Job submitted, waiting for processing
- `processing`: vibevoice is generating audio
- `completed`: Audio generated and ready for download
- `failed`: Processing failed with error
