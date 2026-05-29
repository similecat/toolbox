# Web Toolbox
A collection of useful web tools built with Python Flask.

## Features
- **GitHub Repo Downloader**: Download any GitHub repository as a ZIP file
- **Text to Voice**: Convert text to speech in English or Chinese

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
- Enter text in English or Chinese
- Generate and download MP3 audio files
- Maximum 5000 characters per conversion
