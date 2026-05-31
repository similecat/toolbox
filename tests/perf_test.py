"""
Performance test for the TTS service.
Measures QPS (Queries Per Second) for key API endpoints.

Usage:
    python tests/perf_test.py --host localhost
    python tests/perf_test.py --host myserver.example.com --concurrency 50 --duration 30
"""

import argparse
import time
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import List

import requests


@dataclass
class RequestResult:
    """Holds the result of a single HTTP request."""
    success: bool
    status_code: int
    elapsed: float  # seconds


@dataclass
class EndpointStats:
    """Aggregated statistics for one endpoint."""
    name: str
    method: str
    path: str
    results: List[RequestResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def successful(self) -> int:
        return sum(1 for r in self.results if r.success)

    @property
    def failed(self) -> int:
        return self.total - self.successful

    @property
    def qps(self) -> float:
        if self.duration <= 0:
            return 0.0
        return self.successful / self.duration

    @property
    def avg_latency(self) -> float:
        if not self.results:
            return 0.0
        return statistics.mean(r.elapsed for r in self.results)

    @property
    def p50_latency(self) -> float:
        if not self.results:
            return 0.0
        latencies = sorted(r.elapsed for r in self.results)
        idx = len(latencies) // 2
        return latencies[idx]

    @property
    def p95_latency(self) -> float:
        if not self.results:
            return 0.0
        latencies = sorted(r.elapsed for r in self.results)
        idx = int(len(latencies) * 0.95)
        idx = min(idx, len(latencies) - 1)
        return latencies[idx]

    @property
    def p99_latency(self) -> float:
        if not self.results:
            return 0.0
        latencies = sorted(r.elapsed for r in self.results)
        idx = int(len(latencies) * 0.99)
        idx = min(idx, len(latencies) - 1)
        return latencies[idx]

    @property
    def min_latency(self) -> float:
        if not self.results:
            return 0.0
        return min(r.elapsed for r in self.results)

    @property
    def max_latency(self) -> float:
        if not self.results:
            return 0.0
        return max(r.elapsed for r in self.results)

    @property
    def success_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return (self.successful / self.total) * 100

    duration: float = 0.0


def make_request(session: requests.Session, method: str, url: str,
                 json_body: dict = None, headers: dict = None) -> tuple:
    """Execute a single HTTP request and return (RequestResult, response_body)."""
    start = time.monotonic()
    try:
        resp = session.request(method, url, json=json_body, headers=headers, timeout=30)
        elapsed = time.monotonic() - start
        result = RequestResult(success=200 <= resp.status_code < 400,
                              status_code=resp.status_code, elapsed=elapsed)
        try:
            body = resp.json()
        except Exception:
            body = None
        return result, body
    except Exception as e:
        elapsed = time.monotonic() - start
        result = RequestResult(success=False, status_code=0, elapsed=elapsed)
        return result, None


def run_endpoint_test(session: requests.Session, base_url: str, method: str,
                      path: str, json_body: dict, headers: dict,
                      count: int) -> tuple:
    """Run `count` requests against a single endpoint.
    Returns (List[RequestResult], List[str]) — results and any job_ids created."""
    url = f"{base_url}{path}"
    results: List[RequestResult] = []
    job_ids: List[str] = []

    for _ in range(count):
        result, body = make_request(session, method, url, json_body, headers)
        results.append(result)
        # Collect job_ids from successful submit responses
        if body and body.get("job_id"):
            job_ids.append(body["job_id"])

    return results, job_ids


def print_report(stats_list: List[EndpointStats], total_duration: float):
    """Print a formatted performance report."""
    print("\n" + "=" * 80)
    print("PERFORMANCE TEST REPORT")
    print("=" * 80)
    print(f"Total Duration : {total_duration:.2f}s")
    print("-" * 80)

    for stats in stats_list:
        print(f"\nEndpoint  : {stats.method} {stats.path}")
        print(f"  Total Requests : {stats.total}")
        print(f"  Successful     : {stats.successful}")
        print(f"  Failed         : {stats.failed}")
        print(f"  Success Rate   : {stats.success_rate:.1f}%")
        print(f"  QPS            : {stats.qps:.2f}")
        print(f"  Avg Latency    : {stats.avg_latency * 1000:.2f} ms")
        print(f"  Min Latency    : {stats.min_latency * 1000:.2f} ms")
        print(f"  P50 Latency    : {stats.p50_latency * 1000:.2f} ms")
        print(f"  P95 Latency    : {stats.p95_latency * 1000:.2f} ms")
        print(f"  P99 Latency    : {stats.p99_latency * 1000:.2f} ms")
        print(f"  Max Latency    : {stats.max_latency * 1000:.2f} ms")

    # Overall QPS
    total_requests = sum(s.total for s in stats_list)
    total_successful = sum(s.successful for s in stats_list)
    overall_qps = total_successful / total_duration if total_duration > 0 else 0

    print("\n" + "-" * 80)
    print(f"Overall Requests : {total_requests}")
    print(f"Overall Successful: {total_successful}")
    print(f"Overall QPS      : {overall_qps:.2f}")
    print("=" * 80 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Performance test for TTS service - measures QPS"
    )
    parser.add_argument(
        "--host", type=str, required=True,
        help="Hostname or IP of the service (e.g., localhost, myserver.com)"
    )
    parser.add_argument(
        "--port", type=int, default=80,
        help="Port of the service (default: 80)"
    )
    parser.add_argument(
        "--concurrency", type=int, default=20,
        help="Number of concurrent workers (default: 20)"
    )
    parser.add_argument(
        "--duration", type=int, default=15,
        help="Test duration in seconds per endpoint (default: 15)"
    )
    parser.add_argument(
        "--rps", type=int, default=100,
        help="Target requests per second per worker (default: 100)"
    )

    args = parser.parse_args()

    base_url = f"http://{args.host}:{args.port}"
    requests_per_worker = args.rps * args.duration

    print(f"Target       : {base_url}")
    print(f"Concurrency  : {args.concurrency}")
    print(f"Duration     : {args.duration}s per endpoint")
    print(f"RPS/worker   : {args.rps}")
    print(f"Requests/worker: {requests_per_worker}")
    print()

    # Define endpoints to test
    endpoints = [
        # --- Web Pages ---
        {
            "name": "GET / (Home Page)",
            "method": "GET",
            "path": "/",
            "json_body": None,
            "headers": {"Accept": "text/html"},
        },
        {
            "name": "GET /text-to-voice (TTS Page)",
            "method": "GET",
            "path": "/text-to-voice",
            "json_body": None,
            "headers": {"Accept": "text/html"},
        },
        {
            "name": "GET /github-downloader (Downloader Page)",
            "method": "GET",
            "path": "/github-downloader",
            "json_body": None,
            "headers": {"Accept": "text/html"},
        },
        # --- API Endpoints ---
        {
            "name": "GET /api/tts/jobs",
            "method": "GET",
            "path": "/api/tts/jobs",
            "json_body": None,
            "headers": {"Accept": "application/json"},
        },
        {
            "name": "POST /api/tts/submit",
            "method": "POST",
            "path": "/api/tts/submit",
            "json_body": {"text": "Hello, this is a performance test.", "language": "en"},
            "headers": {"Content-Type": "application/json", "Accept": "application/json"},
        },
        {
            "name": "GET /api/tts/jobs?status=pending",
            "method": "GET",
            "path": "/api/tts/jobs?status=pending",
            "json_body": None,
            "headers": {"Accept": "application/json"},
        },
    ]

    stats_list: List[EndpointStats] = []
    collected_job_ids: List[str] = []
    total_start = time.monotonic()

    for ep in endpoints:
        print(f"Testing: {ep['name']} ...")
        ep_start = time.monotonic()

        all_results: List[RequestResult] = []
        all_job_ids: List[str] = []

        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = []
            for _ in range(args.concurrency):
                f = executor.submit(
                    run_endpoint_test,
                    requests.Session(),
                    base_url,
                    ep["method"],
                    ep["path"],
                    ep["json_body"],
                    ep["headers"],
                    requests_per_worker,
                )
                futures.append(f)

            for f in as_completed(futures):
                results, job_ids = f.result()
                all_results.extend(results)
                all_job_ids.extend(job_ids)

        ep_duration = time.monotonic() - ep_start

        stats = EndpointStats(
            name=ep["name"],
            method=ep["method"],
            path=ep["path"],
            results=all_results,
            duration=ep_duration,
        )
        stats_list.append(stats)

        print(f"  Done - {stats.total} requests in {ep_duration:.2f}s "
              f"(QPS: {stats.qps:.2f}, Success: {stats.success_rate:.1f}%)")

        # Collect job IDs for cleanup
        collected_job_ids.extend(all_job_ids)

    total_duration = time.monotonic() - total_start

    # Print full report
    print_report(stats_list, total_duration)

    # --- Cleanup: delete test jobs ---
    if collected_job_ids:
        print(f"Cleaning up {len(collected_job_ids)} test job(s)... ", end="", flush=True)
        deleted = 0
        session = requests.Session()
        for job_id in collected_job_ids:
            try:
                resp = session.request("DELETE", f"{base_url}/api/tts/job/{job_id}",
                                      timeout=10)
                if 200 <= resp.status_code < 400:
                    deleted += 1
            except Exception:
                pass
        print(f"Deleted {deleted}/{len(collected_job_ids)} job(s).")
    else:
        print("No test jobs to clean up.")

    # Exit with error if any endpoint had > 50% failure rate
    high_failure = any(s.success_rate < 50 for s in stats_list if s.total > 0)
    sys.exit(1 if high_failure else 0)


if __name__ == "__main__":
    main()
