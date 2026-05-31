# Performance Test Report

## Test Overview

| Item | Detail |
|------|--------|
| **Target Server** | `http://hostname:80`, 2 vCPU |
| **Test Tool** | Custom Python Performance Tester (`perf_test.py`) |
| **Total Requests** | 60,000 (10,000 per endpoint × 6 endpoints) |
| **Overall Duration** | 1,901.53s (~31.7 minutes) |
| **Overall Success Rate** | 97.0% (58,227 / 60,000) |
| **Overall QPS** | 30.62 |

## Test Configurations

Two test runs were executed with different configurations:

| Parameter | Run 1 | Run 2 |
|-----------|-------|-------|
| Concurrency | 200 | 500 |
| Duration (per endpoint) | 10s | 10s |
| RPS/worker | 5 | 2 |
| Requests/worker | 50 | 20 |

> **Note:** The report below primarily reflects Run 2 (Concurrency 500), which produced the most complete results.

---

## Endpoint Results Summary

### 1. Static Pages (HTML Rendering)

| Endpoint | QPS | Success Rate | Avg Latency | P50 | P95 | P99 |
|----------|-----|-------------|-------------|-----|-----|-----|
| `GET /` | 596.97 | 100.0% | 342.00 ms | 221.89 ms | 896.17 ms | 1958.63 ms |
| `GET /text-to-voice` | 537.94 | 100.0% | 413.06 ms | 286.09 ms | 1046.73 ms | 2242.25 ms |
| `GET /github-downloader` | 310.95 | 100.0% | 360.62 ms | 235.80 ms | 872.50 ms | 2314.86 ms |

**Assessment:** ✅ Healthy. All static pages achieved 100% success with QPS > 300. P95 latencies remained under 1.1s, indicating stable rendering performance.

---

### 2. API Endpoints (Read Operations)

| Endpoint | QPS | Success Rate | Avg Latency | P50 | P95 | P99 |
|----------|-----|-------------|-------------|-----|-----|-----|
| `GET /api/tts/jobs` | 763.52 | 100.0% | 405.25 ms | 337.90 ms | 884.68 ms | 2441.83 ms |

**Assessment:** ✅ Healthy. Highest QPS of all endpoints. Consistent latency profile with no failures.

---

### 3. API Endpoints (Write/Processing Operations)

| Endpoint | QPS | Success Rate | Avg Latency | P50 | P95 | P99 |
|----------|-----|-------------|-------------|-----|-----|-----|
| `POST /api/tts/submit` | 123.99 | 93.3% | 3516.39 ms | 3871.32 ms | 4476.30 ms | 5257.21 ms |
| `GET /api/tts/jobs?status=pending` | 5.10 | 89.0% | 58266.52 ms | 62062.01 ms | 86114.27 ms | 95246.80 ms |

**Assessment:** ⚠️ Degraded. Both endpoints show significant performance issues (detailed below).

---

## Detailed Analysis

### Latency Distribution

```
Endpoint                      Avg (ms)   P50 (ms)   P95 (ms)   P99 (ms)   Max (ms)
─────────────────────────────────────────────────────────────────────────────────────
GET /                         342.0      221.9      896.2      1958.6     6934.2
GET /text-to-voice            413.1      286.1     1046.7      2242.3    10877.4
GET /github-downloader        360.6      235.8      872.5      2314.9    27586.1
GET /api/tts/jobs             405.3      337.9      884.7      2441.8     7745.6
POST /api/tts/submit         3516.4     3871.3     4476.3     5257.2    31141.2
GET /api/tts/jobs?pending    58266.5   62062.0    86114.3    95246.8   125102.6
```

### Failure Breakdown

| Endpoint | Failed Requests | Failure Rate |
|----------|----------------|-------------|
| `POST /api/tts/submit` | 673 | 6.7% |
| `GET /api/tts/jobs?status=pending` | 1,099 | 11.0% |
| **Total** | **1,772** | **2.95%** |

---

## Findings & Observations

### ✅ Strengths

1. **Static page serving is robust** — Home, TTS page, and Downloader page all handled 10K requests with 100% success and sub-1s P95 latency.
2. **Read API is performant** — `GET /api/tts/jobs` achieved the highest QPS (763.52) with consistent latency.
3. **TTS submission is stable** — `POST /api/tts/submit` maintained ~124 QPS with a 93.3% success rate despite higher latency.

### ⚠️ Concerns

1. **`GET /api/tts/jobs?status=pending` is critically slow**
   - Average latency of **58.3 seconds** with P99 exceeding **95 seconds**
   - Only 5.10 QPS — this is a severe bottleneck
   - 11% failure rate suggests timeouts or resource exhaustion
   - This endpoint consumed ~97% of the total test duration (1,745.70s out of 1,901.53s)

2. **Max latency outliers on static pages**
   - `GET /github-downloader` showed a max latency of 27.6s, suggesting occasional GC pauses or resource contention
   - `GET /text-to-voice` max latency reached 10.9s

3. **`POST /api/tts/submit` has a 6.7% failure rate**
   - 673 failed requests under 500 concurrency
   - May indicate connection pool exhaustion or queue saturation

### 🔍 Root Cause Hypotheses

1. **`?status=pending` query performance** — The filter on `status=pending` may not be indexed, causing full table scans as the job queue grows. With 500 concurrent submissions, the pending queue likely grew large during the test.

2. **Resource contention** — High concurrency (500) on write-heavy endpoints may be causing lock contention or memory pressure, leading to the outlier latencies on static pages.

3. **Timeout configuration** — The high failure rate on the pending jobs query suggests requests may be timing out before completion.

---

## Recommendations

### Immediate Actions

1. **Add database index on `status` column** for the jobs table to speed up `?status=pending` queries
2. **Implement pagination or limit** on the `/api/tts/jobs?status=pending` endpoint to prevent large result sets
3. **Review timeout settings** — increase server-side timeout for long-polling endpoints or implement WebSocket-based status updates instead

### Medium-Term Improvements

4. **Connection pool tuning** — investigate the 6.7% failure rate on `POST /api/tts/submit` under high concurrency
5. **Add caching layer** for static pages to reduce P99/P999 latency spikes
6. **Implement rate limiting** to prevent resource exhaustion during traffic spikes

### Monitoring

7. **Add APM instrumentation** to track per-endpoint latency under production load
8. **Set up alerts** for P95 latency exceeding 2s on static pages and 5s on API endpoints

---

## Test Environment

- **Server**: `hostname` (Azure Cloud, 2 vCPU)
- **Protocol**: HTTP (Port 80)
- **Framework**: Flask + Gunicorn (gevent worker)
- **Report Generated**: May 31, 2026
