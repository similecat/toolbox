# Tests

Unit, security, and performance testing utilities for Web Toolbox.

## Unit & Security Integration Tests

`test_auth_and_https.py` verifies:
- Self-signed TLS certificate generation and live HTTPS encrypted request/response transfer
- Unauthenticated webpage redirect (`302`) to `/login`
- Default email allow-list seeding (`bo.yang@hotmail.com`) and automatic Admin assignment
- Allow-list enforcement (`403`) on `/register` and `/change-password`
- Admin allow-list management (`/admin/allowlist/add`, `/admin/allowlist/remove`, `/api/admin/allowlist`) and non-admin `403` protection
- Session cookie persistence, password modification invalidation, and `X-Accel-Redirect` audio protection
- Shared REST API key (`X-API-Key` and `Authorization: Bearer`) across both TTS (`/api/tts/*`) and GitHub Downloader (`/api/github/download`) services

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

---

## Performance Test

`perf_test.py` measures QPS (Queries Per Second) and latency percentiles for the HTTPS REST API endpoints using the shared API key.

### Usage

```bash
python tests/perf_test.py --host <hostname>
```

### Arguments

| Flag            | Default  | Description                                              |
|-----------------|----------|----------------------------------------------------------|
| `--host`        | *required* | Hostname or IP of the service                          |
| `--port`        | `443`    | Port of the service                                      |
| `--scheme`      | `https`  | Protocol scheme (`https` or `http`)                      |
| `--api-key`     | `TOOLBOX_API_KEY` or `tbx-default-api-key-2026` | Shared REST API key |
| `--concurrency` | `20`     | Number of concurrent worker threads                      |
| `--duration`    | `15`     | Test duration in seconds per endpoint                    |
| `--rps`         | `100`    | Target requests per second per worker                    |

### Examples

```bash
# Quick test against local HTTPS server on port 5000
python tests/perf_test.py --host localhost --port 5000

# Load test on a remote HTTPS server
python tests/perf_test.py --host byang.japaneast.cloudapp.azure.com --concurrency 20 --duration 15
```
