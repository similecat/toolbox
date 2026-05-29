# Web Toolbox
A collection of useful web tools built with Python Flask.

## Features
- **GitHub Repo Downloader**: Download any GitHub repository as a ZIP file
- **Text to Voice**: Submit text for voice generation with job-based processing

## Setup

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Run the application:
```bash
python app.py
```

3. Open your browser and navigate to `http://localhost:5000`

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
