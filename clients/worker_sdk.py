#!/usr/bin/env python3
"""Mandatory Worker Service Discovery & Peer-to-Peer Mesh SDK for Web Toolbox.

Zero third-party dependencies (Python 3.8+ standard library only).
Every existing and future Toolbox worker service MUST run with ToolboxWorkerRuntime:
  1. Registers worker metadata, capabilities, and P2P endpoints via POST /api/services/register
  2. Maintains 30s HTTP/1.1 Keep-Alive heartbeats + live telemetry via POST /api/services/heartbeat
  3. Supplies X-Worker-Id and X-Worker-Lease headers required by server-side worker admission gates
  4. Hosts an embedded multi-threaded P2P RPC server (/peer/v1/{health,manifest,invoke/<op>})
  5. Discovers & calls other worker services directly (Peer-to-Peer) without central server hops
  6. Deregisters cleanly on SIGINT / SIGTERM / context exit
"""
from __future__ import annotations

import collections
import contextlib
import ctypes
import datetime
import hashlib
import hmac
import http.client
import http.server
import ipaddress
import json
import os
import platform
import random
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterator, List, Optional, Tuple, Union

DEFAULT_API_KEY = "tbx-default-api-key-2026"
DEFAULT_HEARTBEAT_INTERVAL = 30
DEFAULT_DISCOVERY_CACHE_TTL_SEC = 60.0
CIRCUIT_BREAKER_COOLDOWN_SEC = 15.0


# ============================================================
# Persistent Identity & Zero-Subprocess Network Discovery
# ============================================================

def _default_state_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("PROGRAMDATA")
        if base:
            return Path(base) / "Toolbox"
        return Path.home() / ".toolbox"
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return Path("/var/lib/toolbox-agent")
    return Path.home() / ".toolbox"


def load_or_create_machine_id(explicit_id: Optional[str] = None) -> str:
    candidate = (explicit_id or os.environ.get("TOOLBOX_MACHINE_ID") or "").strip()
    if candidate:
        return candidate

    id_file = _default_state_dir() / "machine_id"
    try:
        if id_file.is_file():
            saved = id_file.read_text(encoding="utf-8").strip()
            if saved:
                return saved
    except OSError:
        pass

    generated = str(uuid.uuid4())
    try:
        id_file.parent.mkdir(parents=True, exist_ok=True)
        id_file.write_text(generated + "\n", encoding="utf-8")
    except OSError:
        seed = f"{platform.node()}-{uuid.getnode()}"
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))
    return generated


def get_os_summary() -> str:
    sys_name = platform.system() or "Unknown"
    release = platform.release() or ""
    arch = platform.machine() or ""
    if sys_name == "Linux":
        try:
            os_rel = Path("/etc/os-release")
            if os_rel.is_file():
                for line in os_rel.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if line.startswith("PRETTY_NAME="):
                        pretty = line.split("=", 1)[1].strip().strip('"')
                        return f"{pretty} ({arch})"[:128]
        except OSError:
            pass
    return f"{sys_name} {release} {arch}".strip()[:128]


def _is_usable_ipv4(addr_str: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(addr_str)
    except ValueError:
        return False
    return not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast)


def discover_primary_ipv4() -> Optional[str]:
    for target in (("8.8.8.8", 80), ("1.1.1.1", 80), ("10.255.255.255", 80)):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(target)
            ip = sock.getsockname()[0]
            if _is_usable_ipv4(ip):
                return ip
        except OSError:
            pass
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = str(info[4][0])
            if _is_usable_ipv4(ip):
                return ip
    except OSError:
        pass
    return None


def _classify_ipv6(addr_str: str) -> int:
    clean = addr_str.split("%", 1)[0].strip()
    try:
        ip = ipaddress.IPv6Address(clean)
    except ValueError:
        return 0
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.ipv4_mapped is not None:
        return 0
    if ip.teredo is not None or ip.sixtofour is not None:
        return 1
    if ip.is_global:
        return 3
    if ip.is_private:
        return 2
    return 1


def discover_primary_ipv6() -> Optional[str]:
    for target in (("2001:4860:4860::8888", 80), ("2606:4700:4700::1111", 80)):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            sock.connect(target)
            ip = sock.getsockname()[0].split("%", 1)[0]
            if _classify_ipv6(ip) >= 2:
                return ipaddress.IPv6Address(ip).compressed
        except OSError:
            pass
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
    candidates: List[Tuple[int, str]] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6):
            raw_ip = str(info[4][0]).split("%", 1)[0]
            score = _classify_ipv6(raw_ip)
            if score > 0:
                candidates.append((score, ipaddress.IPv6Address(raw_ip).compressed))
    except OSError:
        pass
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _utc_iso_now() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


# ============================================================
# Offline HMAC Peer Token Verification
# ============================================================

def verify_peer_auth_token(
    token: str,
    secret: str,
    *,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    if not token or not secret:
        return None
    parts = str(token).strip().split("~")
    if len(parts) != 5 or parts[0] != "v1":
        return None
    _, exp_str, worker_id, service_type, provided_sig = parts
    try:
        exp = int(exp_str)
    except ValueError:
        return None
    cur = time.time() if now is None else now
    if cur > exp:
        return None
    payload = f"v1~{exp}~{worker_id}~{service_type}"
    expected_sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(provided_sig, expected_sig):
        return None
    return {"worker_id": worker_id, "service_type": service_type, "expires_at": exp}


# ============================================================
# Zero-Dependency Process Resource & Telemetry Tracker
# ============================================================

def _read_process_rss_mb() -> Optional[float]:
    if os.name == "nt":
        try:
            class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            pmc = _PROCESS_MEMORY_COUNTERS()
            pmc.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb)
            if ok:
                return round(pmc.WorkingSetSize / (1024.0 * 1024.0), 2)
        except Exception:
            return None
        return None

    status_path = Path("/proc/self/status")
    if status_path.is_file():
        try:
            for line in status_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("VmRSS:"):
                    kb = float(line.split()[1])
                    return round(kb / 1024.0, 2)
        except Exception:
            pass

    try:
        import resource  # type: ignore[import-not-found]
        ru = resource.getrusage(resource.RUSAGE_SELF)
        rss = float(ru.ru_maxrss)
        if sys.platform == "darwin":
            return round(rss / (1024.0 * 1024.0), 2)
        return round(rss / 1024.0, 2)
    except Exception:
        return None


def _read_optional_gpu_memory_mb() -> Optional[float]:
    torch_mod = sys.modules.get("torch")
    if torch_mod is None:
        return None
    try:
        if torch_mod.cuda.is_available():
            return round(float(torch_mod.cuda.memory_allocated()) / (1024.0 * 1024.0), 2)
    except Exception:
        return None
    return None


class WorkerTelemetry:
    """Thread-safe rolling latency, throughput, error, and resource statistics collector."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.total_requests = 0
        self.succeeded_requests = 0
        self.failed_requests = 0
        self.p2p_requests = 0
        self.queue_jobs = 0
        self.outbound_p2p_calls = 0
        self.outbound_p2p_errors = 0
        self.active_tasks = 0
        self.queue_depth = 0
        self.consecutive_failures = 0
        self.last_latency_ms = 0.0
        self.last_error = ""
        self.last_error_at: Optional[str] = None
        self.last_task_at: Optional[str] = None
        self._latencies_ms: Deque[float] = collections.deque(maxlen=512)
        self._completion_times: Deque[float] = collections.deque(maxlen=2048)
        self._custom: Dict[str, Any] = {}
        self._prev_cpu_time = time.process_time()
        self._prev_mono_time = time.monotonic()
        self._last_cpu_pct: Optional[float] = 0.0

    def set_custom_metric(self, key: str, value: Any) -> None:
        with self._lock:
            self._custom[key] = value

    def set_custom_metrics(self, mapping: Dict[str, Any]) -> None:
        with self._lock:
            self._custom.update(mapping)

    def increment_custom_metric(self, key: str, delta: Union[int, float] = 1) -> None:
        with self._lock:
            cur = self._custom.get(key, 0)
            try:
                self._custom[key] = cur + delta
            except TypeError:
                self._custom[key] = delta

    def set_queue_depth(self, depth: int) -> None:
        with self._lock:
            self.queue_depth = max(0, int(depth))

    def record_outbound_p2p(self, *, ok: bool) -> None:
        with self._lock:
            self.outbound_p2p_calls += 1
            if not ok:
                self.outbound_p2p_errors += 1

    @contextlib.contextmanager
    def track_task(self, *, source: str = "p2p", operation: str = "default") -> Iterator[None]:
        start_mono = time.perf_counter()
        with self._lock:
            self.active_tasks += 1
        err_msg: Optional[str] = None
        try:
            yield
        except Exception as exc:
            err_msg = f"[{operation}] {type(exc).__name__}: {exc}"[:512]
            raise
        finally:
            elapsed_ms = max(0.0, (time.perf_counter() - start_mono) * 1000.0)
            now_mono = time.monotonic()
            iso_now = _utc_iso_now()
            with self._lock:
                self.active_tasks = max(0, self.active_tasks - 1)
                self.total_requests += 1
                if source == "p2p":
                    self.p2p_requests += 1
                elif source == "queue":
                    self.queue_jobs += 1

                self.last_latency_ms = round(elapsed_ms, 3)
                self._latencies_ms.append(elapsed_ms)
                self._completion_times.append(now_mono)
                self.last_task_at = iso_now

                if err_msg is None:
                    self.succeeded_requests += 1
                    self.consecutive_failures = 0
                else:
                    self.failed_requests += 1
                    self.consecutive_failures += 1
                    self.last_error = err_msg
                    self.last_error_at = iso_now

    def snapshot(self) -> Tuple[int, int, Dict[str, Any]]:
        now_mono = time.monotonic()
        cur_cpu = time.process_time()
        rss_mb = _read_process_rss_mb()
        gpu_mb = _read_optional_gpu_memory_mb()

        with self._lock:
            dt = now_mono - self._prev_mono_time
            if dt >= 0.5:
                dcpu = max(0.0, cur_cpu - self._prev_cpu_time)
                cores = max(1, os.cpu_count() or 1)
                self._last_cpu_pct = round(min(100.0, (dcpu / dt / cores) * 100.0), 2)
                self._prev_mono_time = now_mono
                self._prev_cpu_time = cur_cpu

            cutoff_60s = now_mono - 60.0
            while self._completion_times and self._completion_times[0] < cutoff_60s:
                self._completion_times.popleft()
            rpm = round(float(len(self._completion_times)), 2)

            lats = sorted(self._latencies_ms)
            if lats:
                n = len(lats)
                avg_lat = round(sum(lats) / n, 3)
                p50 = round(lats[min(n - 1, int(n * 0.50))], 3)
                p95 = round(lats[min(n - 1, int(n * 0.95))], 3)
                p99 = round(lats[min(n - 1, int(n * 0.99))], 3)
            else:
                avg_lat = p50 = p95 = p99 = 0.0

            stats = {
                "total_requests": self.total_requests,
                "succeeded_requests": self.succeeded_requests,
                "failed_requests": self.failed_requests,
                "p2p_requests": self.p2p_requests,
                "queue_jobs": self.queue_jobs,
                "outbound_p2p_calls": self.outbound_p2p_calls,
                "outbound_p2p_errors": self.outbound_p2p_errors,
                "avg_latency_ms": avg_lat,
                "p50_latency_ms": p50,
                "p95_latency_ms": p95,
                "p99_latency_ms": p99,
                "last_latency_ms": self.last_latency_ms,
                "requests_per_min": rpm,
                "cpu_percent": self._last_cpu_pct,
                "memory_mb": rss_mb,
                "gpu_memory_mb": gpu_mb,
                "consecutive_failures": self.consecutive_failures,
                "last_error": self.last_error,
                "last_error_at": self.last_error_at,
                "last_task_at": self.last_task_at,
                "custom": dict(self._custom),
            }
            return self.active_tasks, self.queue_depth, stats


# ============================================================
# Embedded Peer-to-Peer HTTP/HTTPS RPC Server
# ============================================================

@dataclass
class PeerCallContext:
    operation: str
    caller_worker_id: str
    caller_service_type: str
    headers: Dict[str, str]
    query_params: Dict[str, str]
    raw_body: bytes = b""


@dataclass
class PeerBinaryResponse:
    data: bytes
    content_type: str = "application/octet-stream"
    status_code: int = 200
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def body(self) -> bytes:
        return self.data


class _ThreadingPeerHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class PeerInvocationError(RuntimeError):
    """Raised when calling a peer worker service fails across all available replicas."""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


# ============================================================
# Persistent HTTP/1.1 Keep-Alive Control-Plane Client
# ============================================================

class _ControlPlaneClient:
    """Reuses a single TCP/TLS connection to the Toolbox prod server."""

    def __init__(
        self,
        *,
        server_url: str,
        api_key: str,
        verify_ssl: bool = False,
        ca_cert: Optional[str] = None,
        timeout: float = 10.0,
    ) -> None:
        parsed = urllib.parse.urlparse(server_url.rstrip("/"))
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"Invalid Toolbox server URL: {server_url!r}")

        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.port = parsed.port or (443 if self.scheme == "https" else 80)
        self.base_path = parsed.path.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._lock = threading.Lock()
        self._conn: Optional[http.client.HTTPConnection] = None

        self._ssl_context: Optional[ssl.SSLContext] = None
        if self.scheme == "https":
            if verify_ssl or ca_cert:
                self._ssl_context = ssl.create_default_context(cafile=ca_cert)
            else:
                self._ssl_context = ssl._create_unverified_context()

    def close(self) -> None:
        with self._lock:
            self._close_unlocked()

    def _close_unlocked(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _ensure_conn_unlocked(self) -> http.client.HTTPConnection:
        if self._conn is None:
            if self.scheme == "https":
                self._conn = http.client.HTTPSConnection(
                    self.host, self.port, timeout=self.timeout, context=self._ssl_context
                )
            else:
                self._conn = http.client.HTTPConnection(
                    self.host, self.port, timeout=self.timeout
                )
        return self._conn

    def request(
        self,
        method: str,
        subpath: str,
        payload: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Dict[str, str], bytes]:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None
        full_path = f"{self.base_path}{subpath}"
        headers: Dict[str, str] = {
            "Connection": "keep-alive",
            "X-API-Key": self.api_key,
            "User-Agent": "ToolboxWorkerSDK/1.0",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        if extra_headers:
            headers.update(extra_headers)

        with self._lock:
            last_exc: Optional[Exception] = None
            for attempt in range(2):
                conn = self._ensure_conn_unlocked()
                try:
                    conn.request(method, full_path, body=body, headers=headers)
                    resp = conn.getresponse()
                    resp_bytes = resp.read()
                    resp_headers = {k.lower(): v for k, v in resp.getheaders()}
                    if resp.will_close:
                        self._close_unlocked()
                    return resp.status, resp_headers, resp_bytes
                except (
                    http.client.HTTPException,
                    ConnectionError,
                    TimeoutError,
                    ssl.SSLError,
                    OSError,
                ) as exc:
                    last_exc = exc
                    self._close_unlocked()
                    if attempt == 0:
                        continue
                    raise
            raise RuntimeError(f"Control-plane request failed: {last_exc}")


# ============================================================
# ToolboxWorkerRuntime (Mandatory Base Runtime for All Workers)
# ============================================================

class ToolboxWorkerRuntime:
    """Lifecycle, mandatory registration, telemetry, and P2P RPC runtime for Toolbox workers."""

    def __init__(
        self,
        *,
        server_url: str,
        api_key: str,
        service_type: str,
        worker_name: Optional[str] = None,
        worker_id: Optional[str] = None,
        service_version: str = "1.0.0",
        machine_id: Optional[str] = None,
        capabilities: Optional[Dict[str, Any]] = None,
        max_concurrency: int = 1,
        heartbeat_interval: int = DEFAULT_HEARTBEAT_INTERVAL,
        enable_p2p_server: bool = True,
        listen_host: str = "0.0.0.0",
        listen_port: int = 0,
        p2p_ssl_cert: Optional[str] = None,
        p2p_ssl_key: Optional[str] = None,
        advertise_host: Optional[str] = None,
        extra_endpoints: Optional[List[Dict[str, Any]]] = None,
        verify_ssl: bool = False,
        ca_cert: Optional[str] = None,
        quiet: bool = False,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("api_key is required to register a Toolbox worker service")
        if not service_type or not service_type.strip():
            raise ValueError("service_type is required to register a Toolbox worker service")

        self.server_url = server_url.rstrip("/")
        self.api_key = api_key.strip()
        self.service_type = service_type.strip().lower()
        self.service_version = service_version.strip() or "1.0.0"
        self.host_name = socket.gethostname() or "unknown-host"
        self.machine_id = load_or_create_machine_id(machine_id)
        self.os_info = get_os_summary()
        self.worker_name = (worker_name or f"{self.service_type}-{self.host_name}").strip()[:128]
        self.worker_id = (
            worker_id
            or os.environ.get("TOOLBOX_WORKER_ID")
            or f"{self.service_type}-{self.machine_id[:12]}-{os.getpid()}"
        ).strip()[:128]

        self.capabilities: Dict[str, Any] = dict(capabilities or {})
        self.max_concurrency = max(1, int(max_concurrency))
        self.heartbeat_interval = max(5, min(600, int(heartbeat_interval)))
        self.enable_p2p_server = enable_p2p_server
        self.listen_host = listen_host
        self.listen_port = int(listen_port)
        self.p2p_ssl_cert = p2p_ssl_cert
        self.p2p_ssl_key = p2p_ssl_key
        self.advertise_host = advertise_host
        self.extra_endpoints = list(extra_endpoints or [])
        self.verify_ssl = verify_ssl
        self.ca_cert = ca_cert
        self.quiet = quiet

        self.telemetry = WorkerTelemetry()
        self.started_at = time.time()

        self._state_lock = threading.RLock()
        self._worker_status = "starting"
        self._status_detail = "initializing"
        self._registered = False
        self._worker_lease_token = ""
        self._peer_auth_token = ""
        self._peer_verify_secret = ""
        self._registry_revision = 0
        self._bound_port: Optional[int] = None
        self._endpoints: List[Dict[str, Any]] = []

        self._operations: Dict[str, Callable[[Dict[str, Any], PeerCallContext], Any]] = {}
        self._p2p_httpd: Optional[_ThreadingPeerHTTPServer] = None
        self._p2p_thread: Optional[threading.Thread] = None
        self._hb_stop = threading.Event()
        self._hb_wake = threading.Event()
        self._hb_thread: Optional[threading.Thread] = None

        self._control = _ControlPlaneClient(
            server_url=self.server_url,
            api_key=self.api_key,
            verify_ssl=verify_ssl,
            ca_cert=ca_cert,
        )

        # Peer discovery local cache: key=(service_type, capability) -> {etag, rev, fetched_at, peers}
        self._discovery_lock = threading.Lock()
        self._peer_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._circuit_breaker_until: Dict[str, float] = {}

    # ------------------------------------------------------------
    # Peer Operation Registration
    # ------------------------------------------------------------

    def register_operation(
        self,
        name: str,
        handler: Callable[[Dict[str, Any], PeerCallContext], Any],
    ) -> None:
        op_name = name.strip()
        if not op_name:
            raise ValueError("Operation name cannot be empty")
        with self._state_lock:
            self._operations[op_name] = handler
            ops = list(self.capabilities.get("operations") or [])
            if op_name not in ops:
                ops.append(op_name)
            self.capabilities["operations"] = ops

    def peer_operation(self, name: Optional[str] = None) -> Callable[..., Any]:
        """Decorator registering a direct worker-to-worker P2P RPC operation."""

        def decorator(fn: Callable[[Dict[str, Any], PeerCallContext], Any]) -> Callable[..., Any]:
            op_name = (name or fn.__name__).strip()
            self.register_operation(op_name, fn)
            return fn

        return decorator

    operation = peer_operation

    # ------------------------------------------------------------
    # Worker Status & Task Instrumentation Helpers
    # ------------------------------------------------------------

    def set_status(self, worker_status: str, status_detail: Optional[str] = None) -> None:
        with self._state_lock:
            changed = worker_status != self._worker_status
            self._worker_status = worker_status
            if status_detail is not None:
                if status_detail != self._status_detail:
                    changed = True
                self._status_detail = status_detail
        if changed:
            self._hb_wake.set()

    def update_capabilities(self, updates: Dict[str, Any]) -> None:
        with self._state_lock:
            self.capabilities.update(updates)
            if self._operations:
                ops = list(self.capabilities.get("operations") or [])
                for op in self._operations:
                    if op not in ops:
                        ops.append(op)
                self.capabilities["operations"] = ops

    @contextlib.contextmanager
    def track_task(self, *, source: str = "queue", operation: str = "default") -> Iterator[None]:
        with self.telemetry.track_task(source=source, operation=operation):
            yield

    def worker_headers(self) -> Dict[str, str]:
        """Return authentication + mandatory worker lease headers for central server calls."""
        with self._state_lock:
            if not self._registered or not self._worker_lease_token:
                raise RuntimeError(
                    "Worker is not registered with Service Discovery. Call runtime.start() first."
                )
            return {
                "X-API-Key": self.api_key,
                "X-Worker-Id": self.worker_id,
                "X-Worker-Lease": self._worker_lease_token,
            }

    @property
    def bound_port(self) -> Optional[int]:
        return self._bound_port

    @property
    def endpoints(self) -> List[Dict[str, Any]]:
        with self._state_lock:
            return list(self._endpoints)

    # ------------------------------------------------------------
    # Embedded P2P Server Setup
    # ------------------------------------------------------------

    def _authenticate_peer_request(self, headers: Any) -> Optional[Tuple[str, str]]:
        peer_tok = (headers.get("X-Peer-Token") or "").strip()
        with self._state_lock:
            verify_secret = self._peer_verify_secret

        if peer_tok and verify_secret:
            verified = verify_peer_auth_token(peer_tok, verify_secret)
            if verified is not None:
                return verified["worker_id"], verified["service_type"]

        # Fallback: direct shared API key when peer caller shares cluster API key
        provided_key = (headers.get("X-API-Key") or "").strip()
        if provided_key and hmac.compare_digest(provided_key, self.api_key):
            caller_wid = (headers.get("X-Caller-Worker-Id") or "api-key-client").strip()
            caller_svc = (headers.get("X-Caller-Service-Type") or "external").strip()
            return caller_wid, caller_svc

        return None

    def _start_p2p_server(self) -> None:
        if not self.enable_p2p_server or self._p2p_httpd is not None:
            return

        runtime_ref = self

        class _PeerRequestHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send_bytes(
                self,
                status_code: int,
                body: bytes,
                content_type: str = "application/json; charset=utf-8",
                extra_headers: Optional[Dict[str, str]] = None,
            ) -> None:
                self.send_response(status_code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Worker-Id", runtime_ref.worker_id)
                self.send_header("X-Service-Type", runtime_ref.service_type)
                if extra_headers:
                    for hk, hv in extra_headers.items():
                        self.send_header(hk, str(hv))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _send_json(self, status_code: int, obj: Any) -> None:
                raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
                self._send_bytes(status_code, raw)

            def _read_body(self) -> bytes:
                length = int(self.headers.get("Content-Length") or "0")
                return self.rfile.read(length) if length > 0 else b""

            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path.rstrip("/")
                if path == "/peer/v1/health":
                    active, qdepth, _ = runtime_ref.telemetry.snapshot()
                    with runtime_ref._state_lock:
                        st = runtime_ref._worker_status
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "worker_id": runtime_ref.worker_id,
                            "service_type": runtime_ref.service_type,
                            "service_version": runtime_ref.service_version,
                            "worker_status": st,
                            "active_tasks": active,
                            "max_concurrency": runtime_ref.max_concurrency,
                            "queue_depth": qdepth,
                        },
                    )
                    return

                auth = runtime_ref._authenticate_peer_request(self.headers)
                if auth is None:
                    self._send_json(401, {"error": "Unauthorized peer request"})
                    return

                if path == "/peer/v1/manifest":
                    with runtime_ref._state_lock:
                        caps = dict(runtime_ref.capabilities)
                        ops = sorted(runtime_ref._operations.keys())
                    self._send_json(
                        200,
                        {
                            "worker_id": runtime_ref.worker_id,
                            "worker_name": runtime_ref.worker_name,
                            "service_type": runtime_ref.service_type,
                            "service_version": runtime_ref.service_version,
                            "operations": ops,
                            "capabilities": caps,
                        },
                    )
                    return

                if path.startswith("/peer/v1/invoke/"):
                    op_name = urllib.parse.unquote(path[len("/peer/v1/invoke/") :]).strip("/")
                    self._dispatch_invoke(op_name, auth, parsed.query, b"")
                    return

                self._send_json(404, {"error": "Peer endpoint not found"})

            def do_POST(self) -> None:
                raw_body = self._read_body()
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path.rstrip("/")

                auth = runtime_ref._authenticate_peer_request(self.headers)
                if auth is None:
                    self._send_json(401, {"error": "Unauthorized peer request"})
                    return

                if path.startswith("/peer/v1/invoke/"):
                    op_name = urllib.parse.unquote(path[len("/peer/v1/invoke/") :]).strip("/")
                    self._dispatch_invoke(op_name, auth, parsed.query, raw_body)
                    return

                self._send_json(404, {"error": "Peer endpoint not found"})

            def _dispatch_invoke(
                self,
                op_name: str,
                auth: Tuple[str, str],
                query_str: str,
                raw_body: bytes,
            ) -> None:
                with runtime_ref._state_lock:
                    handler = runtime_ref._operations.get(op_name)
                if handler is None:
                    self._send_json(
                        404,
                        {"error": f"Operation '{op_name}' is not supported by service '{runtime_ref.service_type}'"},
                    )
                    return

                ctype = (self.headers.get("Content-Type") or "").lower()
                payload: Dict[str, Any] = {}
                if raw_body and ("application/json" in ctype or ctype == ""):
                    try:
                        decoded = json.loads(raw_body.decode("utf-8"))
                        if isinstance(decoded, dict):
                            payload = decoded
                        else:
                            payload = {"value": decoded}
                    except Exception as exc:
                        if "application/json" in ctype:
                            self._send_json(400, {"error": f"Invalid JSON payload: {exc}"})
                            return

                q_params = {k: v[0] for k, v in urllib.parse.parse_qs(query_str).items() if v}
                if not payload and q_params:
                    payload = dict(q_params)

                ctx = PeerCallContext(
                    operation=op_name,
                    caller_worker_id=auth[0],
                    caller_service_type=auth[1],
                    headers={k.lower(): v for k, v in self.headers.items()},
                    query_params=q_params,
                    raw_body=raw_body,
                )

                try:
                    with runtime_ref.telemetry.track_task(source="p2p", operation=op_name):
                        result = handler(payload, ctx)
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
                except Exception as exc:
                    self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})
                    return

                if isinstance(result, PeerBinaryResponse):
                    self._send_bytes(
                        result.status_code,
                        result.data,
                        content_type=result.content_type,
                        extra_headers=result.headers,
                    )
                elif isinstance(result, (bytes, bytearray)):
                    self._send_bytes(200, bytes(result), content_type="application/octet-stream")
                else:
                    self._send_json(200, result if result is not None else {"ok": True})

            def log_message(self, *_args: Any) -> None:
                return

        httpd = _ThreadingPeerHTTPServer((self.listen_host, self.listen_port), _PeerRequestHandler)
        use_tls = bool(self.p2p_ssl_cert and self.p2p_ssl_key)
        if use_tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=self.p2p_ssl_cert, keyfile=self.p2p_ssl_key)
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

        self._p2p_httpd = httpd
        self._bound_port = int(httpd.server_port)
        self._endpoints = self._build_advertised_endpoints(protocol="https" if use_tls else "http")

        t = threading.Thread(
            target=httpd.serve_forever,
            name=f"toolbox-p2p-{self.worker_id}",
            daemon=True,
        )
        t.start()
        self._p2p_thread = t

    def _build_advertised_endpoints(self, protocol: str) -> List[Dict[str, Any]]:
        port = self._bound_port
        if not port:
            return list(self.extra_endpoints)

        eps: List[Dict[str, Any]] = []
        if self.advertise_host:
            eps.append(
                {
                    "protocol": protocol,
                    "host": self.advertise_host,
                    "port": port,
                    "api_prefix": "/peer/v1",
                }
            )
        elif self.listen_host in ("127.0.0.1", "localhost", "::1"):
            eps.append(
                {
                    "protocol": protocol,
                    "host": "127.0.0.1",
                    "port": port,
                    "scope": "loopback",
                    "api_prefix": "/peer/v1",
                }
            )
        else:
            lan_v4 = discover_primary_ipv4()
            if lan_v4:
                eps.append(
                    {
                        "protocol": protocol,
                        "host": lan_v4,
                        "port": port,
                        "scope": "lan_ipv4",
                        "api_prefix": "/peer/v1",
                    }
                )
            eps.append(
                {
                    "protocol": protocol,
                    "host": "127.0.0.1",
                    "port": port,
                    "scope": "loopback",
                    "api_prefix": "/peer/v1",
                }
            )
            if ":" in self.listen_host:
                v6 = discover_primary_ipv6()
                if v6:
                    eps.append(
                        {
                            "protocol": protocol,
                            "host": v6,
                            "port": port,
                            "scope": "ipv6",
                            "api_prefix": "/peer/v1",
                        }
                    )

        eps.extend(self.extra_endpoints)
        return eps

    # ------------------------------------------------------------
    # Control-Plane Registration & Heartbeat Loop
    # ------------------------------------------------------------

    def _send_register(self) -> None:
        active, qdepth, stats = self.telemetry.snapshot()
        with self._state_lock:
            ops = list(self.capabilities.get("operations") or [])
            for op in self._operations:
                if op not in ops:
                    ops.append(op)
            if ops:
                self.capabilities["operations"] = ops

            payload = {
                "worker_id": self.worker_id,
                "worker_name": self.worker_name,
                "service_type": self.service_type,
                "service_version": self.service_version,
                "machine_id": self.machine_id,
                "host_name": self.host_name,
                "os_info": self.os_info,
                "endpoints": list(self._endpoints if self.enable_p2p_server else self.extra_endpoints),
                "capabilities": dict(self.capabilities),
                "worker_status": self._worker_status,
                "status_detail": self._status_detail,
                "max_concurrency": self.max_concurrency,
                "active_tasks": active,
                "queue_depth": qdepth,
                "heartbeat_interval": self.heartbeat_interval,
                "started_at": self.started_at,
                "stats": stats,
            }

        code, _headers, raw_body = self._control.request("POST", "/api/services/register", payload)
        if code != 200:
            msg = raw_body.decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"Service Discovery registration failed (HTTP {code}): {msg}")

        data = json.loads(raw_body.decode("utf-8"))
        with self._state_lock:
            self._registered = True
            self._worker_lease_token = str(data.get("worker_lease_token") or "")
            self._peer_auth_token = str(data.get("peer_auth_token") or "")
            self._peer_verify_secret = str(data.get("peer_verify_secret") or "")
            new_rev = int(data.get("registry_revision") or 0)
            rev_changed = new_rev != self._registry_revision
            self._registry_revision = new_rev
            worker_view = data.get("worker") or {}
            if worker_view.get("endpoints"):
                self._endpoints = list(worker_view["endpoints"])

        if rev_changed:
            self._invalidate_peer_cache()

        if not self.quiet:
            ep_str = ", ".join(ep["url"] for ep in self._endpoints) or "none"
            print(
                f"[WorkerSDK] Registered worker={self.worker_id} service={self.service_type} "
                f"endpoints=[{ep_str}]"
            )

    def _send_heartbeat(self) -> None:
        active, qdepth, stats = self.telemetry.snapshot()
        with self._state_lock:
            payload = {
                "worker_id": self.worker_id,
                "worker_status": self._worker_status,
                "status_detail": self._status_detail,
                "active_tasks": active,
                "queue_depth": qdepth,
                "stats": stats,
            }

        code, headers, raw_body = self._control.request("POST", "/api/services/heartbeat", payload)
        if code == 205:
            # Server restarted or lost state -> immediately re-register full worker manifest
            self._send_register()
            return
        if code != 204:
            msg = raw_body.decode("utf-8", errors="replace")[:200]
            raise RuntimeError(f"Heartbeat failed (HTTP {code}): {msg}")

        new_peer_tok = headers.get("x-peer-token")
        raw_rev = headers.get("x-registry-revision")
        rev_changed = False
        with self._state_lock:
            if new_peer_tok:
                self._peer_auth_token = new_peer_tok
            if raw_rev is not None:
                try:
                    parsed_rev = int(raw_rev)
                    if parsed_rev != self._registry_revision:
                        self._registry_revision = parsed_rev
                        rev_changed = True
                except ValueError:
                    pass
        if rev_changed:
            self._invalidate_peer_cache()

    def _heartbeat_loop(self) -> None:
        consecutive_errors = 0
        while not self._hb_stop.is_set():
            jitter = random.uniform(-0.08 * self.heartbeat_interval, 0.08 * self.heartbeat_interval)
            wait_sec = max(1.0, self.heartbeat_interval + jitter)
            self._hb_wake.wait(timeout=wait_sec)
            self._hb_wake.clear()
            if self._hb_stop.is_set():
                break

            try:
                self._send_heartbeat()
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                if not self.quiet:
                    print(f"[WorkerSDK] Heartbeat warning ({consecutive_errors}): {exc}", file=sys.stderr)
                backoff = min(self.heartbeat_interval, 2.0 * (2 ** min(consecutive_errors - 1, 4)))
                self._hb_stop.wait(timeout=backoff)

    def start(
        self,
        *,
        initial_status: str = "serving",
        status_detail: str = "ready",
        max_register_attempts: int = 3,
    ) -> "ToolboxWorkerRuntime":
        """Start embedded P2P server, complete mandatory registration, and launch heartbeat thread."""
        with self._state_lock:
            if self._registered and self._hb_thread and self._hb_thread.is_alive():
                return self
            self._worker_status = initial_status
            self._status_detail = status_detail

        self._start_p2p_server()

        last_err: Optional[Exception] = None
        for attempt in range(1, max(1, max_register_attempts) + 1):
            try:
                self._send_register()
                last_err = None
                break
            except Exception as exc:
                last_err = exc
                if attempt < max_register_attempts:
                    time.sleep(min(5.0, 1.0 * attempt))

        if last_err is not None:
            self._shutdown_p2p_server()
            raise last_err

        self._hb_stop.clear()
        self._hb_wake.clear()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"toolbox-hb-{self.worker_id}",
            daemon=True,
        )
        self._hb_thread.start()
        return self

    def flush_heartbeat(self) -> None:
        """Send an immediate synchronous heartbeat + telemetry flush to the server."""
        self._send_heartbeat()

    def force_reregister(self) -> bool:
        """Re-run full registration immediately (e.g. if central server restarted)."""
        try:
            self._send_register()
            return True
        except Exception:
            return False

    def serve_forever(self, poll_interval: float = 1.0) -> None:
        """Block and serve direct P2P requests until interrupted."""
        while not self._hb_stop.is_set():
            time.sleep(poll_interval)

    def _shutdown_p2p_server(self) -> None:
        if self._p2p_httpd is not None:
            try:
                self._p2p_httpd.shutdown()
                self._p2p_httpd.server_close()
            except Exception:
                pass
            self._p2p_httpd = None
        if self._p2p_thread is not None:
            self._p2p_thread.join(timeout=2.0)
            self._p2p_thread = None

    def stop(self, *, status_detail: str = "worker stopped") -> None:
        """Stop heartbeat loop, deregister cleanly from prod server, and close P2P server."""
        self._hb_stop.set()
        self._hb_wake.set()
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=3.0)
            self._hb_thread = None

        with self._state_lock:
            was_registered = self._registered
            self._registered = False
            self._worker_status = "stopped"
            self._status_detail = status_detail

        if was_registered:
            try:
                _active, _qdepth, final_stats = self.telemetry.snapshot()
                self._control.timeout = 3.0
                self._control.request(
                    "POST",
                    "/api/services/deregister",
                    {
                        "worker_id": self.worker_id,
                        "status_detail": status_detail,
                        "stats": final_stats,
                    },
                )
                if not self.quiet:
                    print(f"[WorkerSDK] Deregistered worker={self.worker_id} cleanly.")
            except Exception:
                pass

        self._shutdown_p2p_server()
        self._control.close()

    def __enter__(self) -> "ToolboxWorkerRuntime":
        return self.start()

    def __exit__(self, exc_type: Any, _exc: Any, _tb: Any) -> None:
        detail = "worker stopped" if exc_type is None else f"stopped ({exc_type.__name__})"
        self.stop(status_detail=detail)

    # ------------------------------------------------------------
    # Peer Service Discovery Pull & Direct P2P Client
    # ------------------------------------------------------------

    def _invalidate_peer_cache(self) -> None:
        with self._discovery_lock:
            for entry in self._peer_cache.values():
                entry["fetched_at"] = 0.0

    def discover_peers(
        self,
        service_type: str,
        *,
        capability: Optional[str] = None,
        force_refresh: bool = False,
    ) -> List[Dict[str, Any]]:
        """Pull healthy peer workers for `service_type` using ETag-cached registry lookups."""
        stype = service_type.strip().lower()
        cap_key = (capability or "").strip().lower()
        cache_key = (stype, cap_key)
        now = time.monotonic()

        cached_etag: Optional[str] = None
        with self._discovery_lock:
            cached = self._peer_cache.get(cache_key)
            if (
                not force_refresh
                and cached is not None
                and (now - float(cached["fetched_at"])) < DEFAULT_DISCOVERY_CACHE_TTL_SEC
            ):
                return list(cached["peers"])
            if cached is not None:
                cached_etag = cached.get("etag")

        params = {"exclude_worker_id": self.worker_id}
        if cap_key:
            params["capability"] = cap_key
        qs = urllib.parse.urlencode(params)
        subpath = f"/api/services/discover/{urllib.parse.quote(stype)}?{qs}"

        req_headers: Dict[str, str] = {"X-Worker-Id": self.worker_id}
        if cached_etag and not force_refresh:
            req_headers["If-None-Match"] = cached_etag

        code, resp_headers, raw_body = self._control.request("GET", subpath, extra_headers=req_headers)
        if code == 304 and cached is not None:
            with self._discovery_lock:
                cached["fetched_at"] = now
                return list(cached["peers"])

        if code != 200:
            msg = raw_body.decode("utf-8", errors="replace")[:200]
            raise PeerInvocationError(
                f"Failed to pull peer discovery for '{stype}' (HTTP {code}): {msg}",
                status_code=502,
            )

        body = json.loads(raw_body.decode("utf-8"))
        peers = body.get("peers") or []
        new_etag = resp_headers.get("etag") or body.get("etag")
        with self._discovery_lock:
            self._peer_cache[cache_key] = {
                "etag": new_etag,
                "fetched_at": now,
                "peers": peers,
            }
        return list(peers)

    def _rank_peer_endpoints(self, peer: Dict[str, Any]) -> List[Dict[str, Any]]:
        same_machine = bool(peer.get("machine_id") and peer.get("machine_id") == self.machine_id)
        if same_machine:
            scope_priority = {"loopback": 0, "lan_ipv4": 1, "ipv6": 2, "hostname": 3, "public": 4}
        else:
            scope_priority = {"lan_ipv4": 0, "ipv6": 1, "hostname": 2, "public": 3, "loopback": 4}

        eps = list(peer.get("endpoints") or [])
        eps.sort(key=lambda ep: scope_priority.get(str(ep.get("scope") or ""), 5))
        return eps

    def _invoke_single_peer_endpoint(
        self,
        endpoint: Dict[str, Any],
        operation: str,
        *,
        payload: Optional[Dict[str, Any]],
        raw_body: Optional[bytes],
        content_type: str,
        timeout: float,
    ) -> PeerBinaryResponse:
        base_url = str(endpoint["url"]).rstrip("/")
        api_prefix = str(endpoint.get("api_prefix") or "/peer/v1").rstrip("/")
        full_url = f"{base_url}{api_prefix}/invoke/{urllib.parse.quote(operation)}"

        if raw_body is not None:
            body_bytes = raw_body
            req_ctype = content_type
        else:
            body_bytes = json.dumps(payload or {}, separators=(",", ":")).encode("utf-8")
            req_ctype = "application/json"

        with self._state_lock:
            peer_token = self._peer_auth_token

        headers = {
            "Content-Type": req_ctype,
            "Content-Length": str(len(body_bytes)),
            "X-Peer-Token": peer_token,
            "X-API-Key": self.api_key,
            "X-Caller-Worker-Id": self.worker_id,
            "X-Caller-Service-Type": self.service_type,
            "User-Agent": "ToolboxWorkerPeerClient/1.0",
        }

        ctx = None
        if full_url.lower().startswith("https://"):
            if self.verify_ssl or self.ca_cert:
                ctx = ssl.create_default_context(cafile=self.ca_cert)
            else:
                ctx = ssl._create_unverified_context()

        req = urllib.request.Request(full_url, data=body_bytes, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                data = resp.read()
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                status = getattr(resp, "status", 200)
                return PeerBinaryResponse(
                    data=data,
                    content_type=resp_headers.get("content-type", "application/octet-stream"),
                    status_code=status,
                    headers=resp_headers,
                )
        except urllib.error.HTTPError as http_err:
            err_body = http_err.read().decode("utf-8", errors="replace")[:400]
            try:
                parsed_err = json.loads(err_body)
                err_msg = parsed_err.get("error") or err_body
            except Exception:
                err_msg = err_body
            raise PeerInvocationError(
                f"Peer HTTP {http_err.code}: {err_msg}",
                status_code=http_err.code,
            ) from http_err
        except Exception as exc:
            raise PeerInvocationError(
                f"Peer network failure ({full_url}): {type(exc).__name__}: {exc}",
                status_code=503,
            ) from exc

    def call_peer_raw(
        self,
        service_type: str,
        operation: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        capability: Optional[str] = None,
        raw_body: Optional[bytes] = None,
        content_type: str = "application/json",
        timeout: float = 30.0,
    ) -> PeerBinaryResponse:
        """Discover a healthy peer worker of `service_type` and invoke `operation` directly (P2P)."""
        with self._state_lock:
            if not self._registered:
                raise RuntimeError(
                    "Worker must be registered (runtime.start()) before calling peer workers."
                )

        peers = self.discover_peers(service_type, capability=capability, force_refresh=False)
        if not peers:
            peers = self.discover_peers(service_type, capability=capability, force_refresh=True)
        if not peers:
            self.telemetry.record_outbound_p2p(ok=False)
            raise PeerInvocationError(
                f"No healthy registered worker available for service '{service_type}'",
                status_code=503,
            )

        now = time.monotonic()
        with self._discovery_lock:
            healthy_peers = [
                p for p in peers if self._circuit_breaker_until.get(p["worker_id"], 0.0) <= now
            ]
        ordered_peers = healthy_peers if healthy_peers else peers

        last_error: Optional[PeerInvocationError] = None
        refreshed_once = False

        idx = 0
        while idx < len(ordered_peers):
            peer = ordered_peers[idx]
            idx += 1
            endpoints = self._rank_peer_endpoints(peer)
            for ep in endpoints:
                try:
                    resp = self._invoke_single_peer_endpoint(
                        ep,
                        operation,
                        payload=payload,
                        raw_body=raw_body,
                        content_type=content_type,
                        timeout=timeout,
                    )
                    self.telemetry.record_outbound_p2p(ok=True)
                    return resp
                except PeerInvocationError as err:
                    last_error = err
                    # Client errors (400, 401, 404) come from application logic -> don't retry other endpoints
                    if 400 <= err.status_code < 500:
                        self.telemetry.record_outbound_p2p(ok=False)
                        raise

            # All endpoints on this peer failed with 5xx/network error -> trip short circuit breaker
            with self._discovery_lock:
                self._circuit_breaker_until[peer["worker_id"]] = time.monotonic() + CIRCUIT_BREAKER_COOLDOWN_SEC

            if not refreshed_once:
                refreshed_once = True
                fresh_peers = self.discover_peers(service_type, capability=capability, force_refresh=True)
                seen_ids = {p["worker_id"] for p in ordered_peers[:idx]}
                for fp in fresh_peers:
                    if fp["worker_id"] not in seen_ids:
                        ordered_peers.append(fp)

        self.telemetry.record_outbound_p2p(ok=False)
        raise last_error or PeerInvocationError(
            f"All peer endpoints failed for service '{service_type}'", status_code=503
        )

    def call_peer(
        self,
        service_type: str,
        operation: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        capability: Optional[str] = None,
        timeout: float = 30.0,
    ) -> Any:
        """Invoke a peer worker operation directly and decode JSON (or return raw bytes if binary)."""
        resp = self.call_peer_raw(
            service_type,
            operation,
            payload=payload,
            capability=capability,
            timeout=timeout,
        )
        if "application/json" in resp.content_type.lower():
            return json.loads(resp.data.decode("utf-8"))
        return resp.data
