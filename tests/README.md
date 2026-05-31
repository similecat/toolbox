# Tests

Performance and load testing utilities for the TTS service.

## Performance Test

`perf_test.py` measures QPS (Queries Per Second) and latency for key API endpoints.

### Usage

```bash
python tests/perf_test.py --host <hostname>
```

### Arguments

| Flag            | Default  | Description                              |
|-----------------|----------|------------------------------------------|
| `--host`        | *required* | Hostname or IP of the service            |
| `--port`        | `80`     | Port of the service                      |
| `--concurrency` | `20`     | Number of concurrent worker threads      |
| `--duration`    | `15`     | Test duration in seconds per endpoint    |
| `--rps`         | `100`    | Target requests per second per worker    |

### Examples

```bash
# Quick test against localhost
python tests/perf_test.py --host localhost

# Heavy load test on a remote server
python tests/perf_test.py --host myserver.example.com --concurrency 50 --duration 30 --rps 200
```

### Tested Endpoints

| Method | Path                                | Description               |
|--------|-------------------------------------|---------------------------|
| `GET`  | `/`                                 | Home page                 |
| `GET`  | `/text-to-voice`                    | TTS tool page             |
| `GET`  | `/github-downloader`                | GitHub downloader page    |
| `GET`  | `/api/tts/jobs`                     | List all jobs             |
| `POST` | `/api/tts/submit`                   | Submit a TTS job          |
| `GET`  | `/api/tts/jobs?status=pending`      | List pending jobs         |

### Report

After each run, a summary report is printed including:

- **Per-endpoint**: total requests, success/fail counts, success rate, QPS, and latency percentiles (min, avg, p50, p95, p99, max).
- **Overall**: aggregate request count and total QPS across all endpoints.

The script exits with code `1` if any endpoint has a success rate below 50%.

### Automatic Cleanup

Jobs created by `POST /api/tts/submit` during the test are automatically deleted using the `DELETE /api/tts/job/<job_id>` endpoint after the test completes, so no noisy workloads are left behind.

### Requirements

- Python 3.7+
- `requests` (already listed in project `requirements.txt`)
