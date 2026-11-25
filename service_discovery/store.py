"""In-memory worker service registry + throttled SQLite WAL store + 5m telemetry rollups."""
from __future__ import annotations

import datetime
import hashlib
import hmac
import ipaddress
import json
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import db as job_db

DEFAULT_HEARTBEAT_INTERVAL = 30
MIN_HEARTBEAT_INTERVAL = 5
MAX_HEARTBEAT_INTERVAL = 600
DB_HEARTBEAT_FLUSH_SEC = 300.0
METRICS_BUCKET_SEC = 300
METRICS_RETENTION_SEC = 7 * 86400
PEER_TOKEN_TTL_SEC = 600

VALID_WORKER_STATUSES = frozenset(
    {"starting", "serving", "idle", "busy", "draining", "degraded", "error", "stopped"}
)
ROUTABLE_PEER_STATUSES = frozenset({"serving", "idle", "busy"})
VALID_ENDPOINT_PROTOCOLS = frozenset({"http", "https"})
VALID_ENDPOINT_SCOPES = frozenset({"loopback", "lan_ipv4", "ipv6", "hostname", "public"})

_WORKER_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SERVICE_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")


class ServiceValidationError(ValueError):
    """Raised when incoming worker registration or heartbeat fields are invalid."""


class ServiceAdmissionError(Exception):
    """Raised when an unregistered, unauthorized, or expired worker calls a gated endpoint."""

    def __init__(self, message: str, *, status_code: int = 428, code: str = "worker_registration_required") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


def _epoch_to_iso_utc(epoch_sec: float) -> str:
    return (
        datetime.datetime.fromtimestamp(epoch_sec, tz=datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def sanitize_short_text(raw: Any, max_len: int = 256) -> str:
    if raw is None:
        return ""
    cleaned = "".join(ch if ord(ch) >= 32 else " " for ch in str(raw)).strip()
    return cleaned[:max_len]


def validate_worker_id(raw: Any) -> str:
    val = str(raw or "").strip()
    if not val or not _WORKER_ID_RE.match(val):
        raise ServiceValidationError(
            "Invalid worker_id: must be 1-128 characters matching [A-Za-z0-9._:-]"
        )
    return val


def validate_optional_machine_id(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    val = str(raw).strip()
    if not val:
        return None
    if not _WORKER_ID_RE.match(val):
        raise ServiceValidationError(
            "Invalid machine_id: must be 1-128 characters matching [A-Za-z0-9._:-]"
        )
    return val


def validate_worker_name(raw: Any) -> str:
    val = str(raw or "").strip()
    if not val:
        raise ServiceValidationError("worker_name is required")
    if len(val) > 128:
        raise ServiceValidationError("worker_name must be at most 128 characters")
    if any(ord(ch) < 32 for ch in val):
        raise ServiceValidationError("worker_name contains invalid control characters")
    return val


def validate_service_type(raw: Any) -> str:
    val = str(raw or "").strip().lower()
    if not val or not _SERVICE_TYPE_RE.match(val):
        raise ServiceValidationError(
            "Invalid service_type: must be 1-64 characters matching [a-z0-9._-]"
        )
    return val


def validate_service_version(raw: Any) -> str:
    if raw is None or str(raw).strip() == "":
        return "1.0.0"
    val = sanitize_short_text(raw, max_len=64)
    if not val:
        return "1.0.0"
    return val


def validate_worker_status(raw: Any, *, default: str = "serving") -> str:
    if raw is None or str(raw).strip() == "":
        return default
    val = str(raw).strip().lower()
    if val not in VALID_WORKER_STATUSES:
        allowed = ", ".join(sorted(VALID_WORKER_STATUSES))
        raise ServiceValidationError(f"Invalid worker_status {val!r}. Must be one of: {allowed}")
    return val


def validate_heartbeat_interval(raw: Any) -> int:
    if raw is None or str(raw).strip() == "":
        return DEFAULT_HEARTBEAT_INTERVAL
    try:
        val = int(raw)
    except (TypeError, ValueError) as exc:
        raise ServiceValidationError("heartbeat_interval must be an integer") from exc
    if val < MIN_HEARTBEAT_INTERVAL or val > MAX_HEARTBEAT_INTERVAL:
        raise ServiceValidationError(
            f"heartbeat_interval must be between {MIN_HEARTBEAT_INTERVAL} and {MAX_HEARTBEAT_INTERVAL} seconds"
        )
    return val


def validate_bounded_int(
    raw: Any,
    field_name: str,
    *,
    default: int,
    min_val: int = 0,
    max_val: int = 100_000,
) -> int:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError) as exc:
        raise ServiceValidationError(f"{field_name} must be an integer") from exc
    if val < min_val or val > max_val:
        raise ServiceValidationError(f"{field_name} must be between {min_val} and {max_val}")
    return val


def normalize_observed_ip(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    candidate = raw.split(",", 1)[0].strip()
    if not candidate:
        return None
    zone_stripped = candidate.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(zone_stripped)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    return addr.compressed


def normalize_endpoints(raw: Any) -> List[Dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ServiceValidationError("endpoints must be a JSON array")
    if len(raw) > 16:
        raise ServiceValidationError("endpoints may contain at most 16 entries")

    normalized: List[Dict[str, Any]] = []
    seen_urls = set()
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ServiceValidationError(f"endpoints[{idx}] must be an object")

        protocol = str(item.get("protocol") or item.get("scheme") or "http").strip().lower()
        if protocol not in VALID_ENDPOINT_PROTOCOLS:
            raise ServiceValidationError(f"endpoints[{idx}].protocol must be 'http' or 'https'")

        raw_host = str(item.get("host") or "").strip().strip("[]")
        if not raw_host:
            raise ServiceValidationError(f"endpoints[{idx}].host is required")

        zone_stripped = raw_host.split("%", 1)[0]
        is_ipv6 = False
        is_ipv4 = False
        try:
            ip_obj = ipaddress.ip_address(zone_stripped)
            if isinstance(ip_obj, ipaddress.IPv6Address):
                host = ip_obj.compressed
                is_ipv6 = True
            else:
                host = str(ip_obj)
                is_ipv4 = True
        except ValueError:
            if not _HOSTNAME_RE.match(raw_host):
                raise ServiceValidationError(f"endpoints[{idx}].host {raw_host!r} is invalid")
            host = raw_host.lower()

        port = validate_bounded_int(
            item.get("port"),
            f"endpoints[{idx}].port",
            default=0,
            min_val=1,
            max_val=65535,
        )

        default_scope = "hostname"
        if is_ipv4:
            addr4 = ipaddress.IPv4Address(host)
            if addr4.is_loopback:
                default_scope = "loopback"
            elif addr4.is_private:
                default_scope = "lan_ipv4"
            else:
                default_scope = "public"
        elif is_ipv6:
            addr6 = ipaddress.IPv6Address(host)
            default_scope = "loopback" if addr6.is_loopback else "ipv6"
        elif host == "localhost":
            default_scope = "loopback"

        scope = str(item.get("scope") or default_scope).strip().lower()
        if scope not in VALID_ENDPOINT_SCOPES:
            scope = default_scope

        api_prefix = sanitize_short_text(item.get("api_prefix") or "/peer/v1", max_len=64)
        if not api_prefix.startswith("/"):
            api_prefix = "/" + api_prefix
        api_prefix = api_prefix.rstrip("/") or "/peer/v1"

        host_bracketed = f"[{host}]" if is_ipv6 else host
        url = f"{protocol}://{host_bracketed}:{port}"
        if url in seen_urls:
            continue
        seen_urls.add(url)

        normalized.append(
            {
                "protocol": protocol,
                "host": host,
                "port": port,
                "scope": scope,
                "api_prefix": api_prefix,
                "url": url,
            }
        )
    return normalized


def normalize_capabilities(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ServiceValidationError("capabilities must be a JSON object")

    cleaned: Dict[str, Any] = {}
    for k, v in raw.items():
        key = sanitize_short_text(k, max_len=64)
        if not key:
            continue
        if isinstance(v, list):
            cleaned[key] = [sanitize_short_text(x, max_len=128) for x in v[:64] if str(x).strip()]
        elif isinstance(v, (bool, int, float)) or v is None:
            cleaned[key] = v
        elif isinstance(v, dict):
            sub: Dict[str, Any] = {}
            for sk, sv in list(v.items())[:32]:
                skey = sanitize_short_text(sk, max_len=64)
                if skey:
                    sub[skey] = sv if isinstance(sv, (bool, int, float)) else sanitize_short_text(sv, max_len=128)
            cleaned[key] = sub
        else:
            cleaned[key] = sanitize_short_text(v, max_len=256)

    encoded = json.dumps(cleaned, separators=(",", ":"))
    if len(encoded) > 16_384:
        raise ServiceValidationError("capabilities JSON exceeds maximum 16 KB size")
    return cleaned


def _nonneg_int(val: Any, fallback: int = 0) -> int:
    if val is None:
        return fallback
    try:
        return max(0, int(val))
    except (TypeError, ValueError):
        return fallback


def _nonneg_float(val: Any, fallback: float = 0.0) -> float:
    if val is None:
        return fallback
    try:
        f = float(val)
        if f != f or f < 0.0:  # NaN or negative
            return fallback
        return round(f, 3)
    except (TypeError, ValueError):
        return fallback


def _opt_nonneg_float(val: Any, fallback: Optional[float] = None) -> Optional[float]:
    if val is None:
        return fallback
    try:
        f = float(val)
        if f != f or f < 0.0:
            return fallback
        return round(f, 2)
    except (TypeError, ValueError):
        return fallback


def default_stats_dict() -> Dict[str, Any]:
    return {
        "total_requests": 0,
        "succeeded_requests": 0,
        "failed_requests": 0,
        "p2p_requests": 0,
        "queue_jobs": 0,
        "outbound_p2p_calls": 0,
        "outbound_p2p_errors": 0,
        "avg_latency_ms": 0.0,
        "p50_latency_ms": 0.0,
        "p95_latency_ms": 0.0,
        "p99_latency_ms": 0.0,
        "last_latency_ms": 0.0,
        "requests_per_min": 0.0,
        "cpu_percent": None,
        "memory_mb": None,
        "gpu_memory_mb": None,
        "consecutive_failures": 0,
        "last_error": "",
        "last_error_at": None,
        "last_task_at": None,
        "custom": {},
    }


def normalize_stats(raw: Any, existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = dict(existing) if existing else default_stats_dict()
    if raw is None:
        return base
    if not isinstance(raw, dict):
        raise ServiceValidationError("stats must be a JSON object")

    int_fields = (
        "total_requests",
        "succeeded_requests",
        "failed_requests",
        "p2p_requests",
        "queue_jobs",
        "outbound_p2p_calls",
        "outbound_p2p_errors",
        "consecutive_failures",
    )
    for f in int_fields:
        if f in raw:
            base[f] = _nonneg_int(raw.get(f), base.get(f, 0))

    float_fields = (
        "avg_latency_ms",
        "p50_latency_ms",
        "p95_latency_ms",
        "p99_latency_ms",
        "last_latency_ms",
        "requests_per_min",
    )
    for f in float_fields:
        if f in raw:
            base[f] = _nonneg_float(raw.get(f), base.get(f, 0.0))

    opt_float_fields = ("cpu_percent", "memory_mb", "gpu_memory_mb")
    for f in opt_float_fields:
        if f in raw:
            base[f] = None if raw.get(f) is None else _opt_nonneg_float(raw.get(f), base.get(f))

    if "last_error" in raw:
        base["last_error"] = sanitize_short_text(raw.get("last_error"), max_len=512)
    if "last_error_at" in raw:
        val = raw.get("last_error_at")
        base["last_error_at"] = sanitize_short_text(val, max_len=64) if val else None
    if "last_task_at" in raw:
        val = raw.get("last_task_at")
        base["last_task_at"] = sanitize_short_text(val, max_len=64) if val else None

    if "custom" in raw and isinstance(raw.get("custom"), dict):
        merged_custom = dict(base.get("custom") or {})
        for ck, cv in list(raw["custom"].items())[:48]:
            ckey = sanitize_short_text(ck, max_len=64)
            if not ckey:
                continue
            if isinstance(cv, (bool, int, float)) or cv is None:
                merged_custom[ckey] = round(cv, 3) if isinstance(cv, float) else cv
            else:
                merged_custom[ckey] = sanitize_short_text(cv, max_len=256)
        encoded_custom = json.dumps(merged_custom, separators=(",", ":"))
        if len(encoded_custom) <= 8192:
            base["custom"] = merged_custom

    return base


# ============================================================
# HMAC Lease & Offline Peer-to-Peer Token Primitives
# ============================================================

def make_worker_lease_token(worker_id: str, service_type: str, secret: str) -> str:
    msg = f"lease~{worker_id}~{service_type}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"wlease1~{sig}"


def verify_worker_lease_token(token: str, worker_id: str, service_type: str, secret: str) -> bool:
    if not token or not secret:
        return False
    expected = make_worker_lease_token(worker_id, service_type, secret)
    return hmac.compare_digest(str(token).strip(), expected)


def make_peer_auth_token(
    worker_id: str,
    service_type: str,
    secret: str,
    *,
    now: float,
    ttl_sec: int = PEER_TOKEN_TTL_SEC,
) -> Tuple[str, int]:
    exp = int(now) + max(30, int(ttl_sec))
    payload = f"v1~{exp}~{worker_id}~{service_type}"
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}~{sig}", exp


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
    return {
        "worker_id": worker_id,
        "service_type": service_type,
        "expires_at": exp,
    }


# ============================================================
# Derived View Computation
# ============================================================

def compute_worker_view(rec: Dict[str, Any], now: float) -> Dict[str, Any]:
    interval = int(rec["heartbeat_interval"] or DEFAULT_HEARTBEAT_INTERVAL)
    unhealthy_after = int(round(interval * 1.5))
    unresponsive_after = int(interval * 3)

    age_sec = max(0.0, now - float(rec["last_heartbeat_at"]))
    worker_status = rec["worker_status"]

    if worker_status == "stopped":
        health_status = "offline"
        effective_status = "offline"
    elif age_sec > unresponsive_after:
        health_status = "unresponsive"
        effective_status = "unresponsive"
    elif age_sec > unhealthy_after:
        health_status = "unhealthy"
        effective_status = "unhealthy"
    else:
        health_status = "healthy"
        effective_status = worker_status

    uptime_sec = 0 if health_status == "offline" else int(max(0.0, now - float(rec["started_at"])))
    max_conc = max(1, int(rec["max_concurrency"] or 1))
    active_tasks = int(rec["active_tasks"] or 0) if health_status == "healthy" else 0
    queue_depth = int(rec["queue_depth"] or 0) if health_status == "healthy" else 0

    stats = dict(rec["stats"])
    total_req = int(stats.get("total_requests") or 0)
    succ_req = int(stats.get("succeeded_requests") or 0)
    success_rate = round((succ_req / total_req) * 100.0, 2) if total_req > 0 else 100.0

    is_routable = (
        health_status == "healthy"
        and worker_status in ROUTABLE_PEER_STATUSES
        and len(rec["endpoints"]) > 0
    )

    return {
        "worker_id": rec["worker_id"],
        "worker_name": rec["worker_name"],
        "service_type": rec["service_type"],
        "service_version": rec["service_version"],
        "machine_id": rec["machine_id"],
        "host_name": rec["host_name"],
        "os_info": rec["os_info"],
        "endpoints": list(rec["endpoints"]),
        "observed_ip": rec["observed_ip"],
        "capabilities": dict(rec["capabilities"]),
        "worker_status": worker_status,
        "health_status": health_status,
        "effective_status": effective_status,
        "is_routable": is_routable,
        "status_detail": rec["status_detail"],
        "max_concurrency": max_conc,
        "active_tasks": active_tasks,
        "available_slots": max(0, max_conc - active_tasks) if is_routable else 0,
        "load_ratio": round(active_tasks / max_conc, 3),
        "queue_depth": queue_depth,
        "heartbeat_interval": interval,
        "unhealthy_threshold_sec": unhealthy_after,
        "unresponsive_threshold_sec": unresponsive_after,
        "started_epoch": int(rec["started_at"]),
        "started_at": _epoch_to_iso_utc(float(rec["started_at"])),
        "uptime_seconds": uptime_sec,
        "first_registered_epoch": int(rec["first_registered_at"]),
        "first_registered_at": _epoch_to_iso_utc(float(rec["first_registered_at"])),
        "last_heartbeat_epoch": int(rec["last_heartbeat_at"]),
        "last_heartbeat_at": _epoch_to_iso_utc(float(rec["last_heartbeat_at"])),
        "seconds_since_heartbeat": int(age_sec),
        "success_rate_pct": success_rate,
        "stats": stats,
    }


def _matches_capability(capabilities: Dict[str, Any], cap_query: str) -> bool:
    needle = cap_query.strip().lower()
    if not needle:
        return True
    if ":" in needle:
        req_key, req_val = needle.split(":", 1)
        req_key = req_key.strip()
        req_val = req_val.strip()
        for k, v in capabilities.items():
            if k.lower() == req_key:
                if not req_val:
                    return True
                if isinstance(v, list):
                    return any(req_val == str(item).lower() for item in v)
                if isinstance(v, dict):
                    return any(req_val == str(sk).lower() or req_val == str(sv).lower() for sk, sv in v.items())
                return req_val == str(v).lower()
        return False
    for k, v in capabilities.items():
        if needle == k.lower():
            return True
        if isinstance(v, list) and any(needle == str(item).lower() for item in v):
            return True
        if isinstance(v, str) and needle in v.lower():
            return True
    return False


# ============================================================
# ServiceStore
# ============================================================

class ServiceStore:
    """Coherent in-memory worker service registry backed by SQLite WAL + 5-min metric history."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        db_flush_interval_sec: float = DB_HEARTBEAT_FLUSH_SEC,
    ) -> None:
        self._lock = threading.RLock()
        self._clock = clock
        self._db_flush_interval_sec = db_flush_interval_sec
        self._caches_by_db: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._revisions_by_db: Dict[str, int] = {}
        self._last_prune_by_db: Dict[str, float] = {}

    def _db_key(self) -> str:
        return os.path.abspath(job_db.DB_PATH)

    def _bump_revision(self, db_key: str) -> int:
        nxt = self._revisions_by_db.get(db_key, 1) + 1
        self._revisions_by_db[db_key] = nxt
        return nxt

    def _get_cache(self) -> Tuple[str, Dict[str, Dict[str, Any]]]:
        db_key = self._db_key()
        cache = self._caches_by_db.get(db_key)
        if cache is not None:
            return db_key, cache

        conn = job_db._get_conn()
        rows = conn.execute("SELECT * FROM worker_services").fetchall()
        loaded: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            try:
                endpoints = json.loads(item.pop("endpoints_json") or "[]")
            except Exception:
                endpoints = []
            try:
                capabilities = json.loads(item.pop("capabilities_json") or "{}")
            except Exception:
                capabilities = {}
            try:
                stats = normalize_stats(json.loads(item.pop("stats_json") or "{}"))
            except Exception:
                stats = default_stats_dict()

            item["endpoints"] = endpoints if isinstance(endpoints, list) else []
            item["capabilities"] = capabilities if isinstance(capabilities, dict) else {}
            item["stats"] = stats
            item["_last_db_flush_at"] = float(item["last_heartbeat_at"])
            item["_rollup_prev_total"] = int(stats.get("total_requests") or 0)
            item["_rollup_prev_failed"] = int(stats.get("failed_requests") or 0)
            item["_rollup_prev_p2p"] = int(stats.get("p2p_requests") or 0)
            item["_live_bucket"] = None
            loaded[item["worker_id"]] = item

        self._caches_by_db[db_key] = loaded
        self._revisions_by_db.setdefault(db_key, 1)
        return db_key, loaded

    def _compute_etag(self, db_key: str, views: List[Dict[str, Any]]) -> Tuple[int, str]:
        rev = self._revisions_by_db.get(db_key, 1)
        health_sig_src = "|".join(
            f"{v['worker_id']}:{v['effective_status']}:{1 if v['is_routable'] else 0}"
            for v in sorted(views, key=lambda x: x["worker_id"])
        )
        digest = hashlib.sha1(health_sig_src.encode("utf-8")).hexdigest()[:10]
        return rev, f"W/\"srv-{rev}-{digest}\""

    def _update_live_bucket(self, rec: Dict[str, Any], now: float) -> Optional[Dict[str, Any]]:
        """Update in-memory 5-minute telemetry bucket; returns completed bucket if rolled over."""
        bucket_epoch = (int(now) // METRICS_BUCKET_SEC) * METRICS_BUCKET_SEC
        stats = rec["stats"]
        cur_total = int(stats.get("total_requests") or 0)
        cur_failed = int(stats.get("failed_requests") or 0)
        cur_p2p = int(stats.get("p2p_requests") or 0)

        prev_total = int(rec.get("_rollup_prev_total", cur_total))
        prev_failed = int(rec.get("_rollup_prev_failed", cur_failed))
        prev_p2p = int(rec.get("_rollup_prev_p2p", cur_p2p))

        # Handle process restarts gracefully where cumulative counters reset to a smaller number
        req_delta = cur_total - prev_total if cur_total >= prev_total else cur_total
        err_delta = cur_failed - prev_failed if cur_failed >= prev_failed else cur_failed
        p2p_delta = cur_p2p - prev_p2p if cur_p2p >= prev_p2p else cur_p2p

        rec["_rollup_prev_total"] = cur_total
        rec["_rollup_prev_failed"] = cur_failed
        rec["_rollup_prev_p2p"] = cur_p2p

        completed_bucket: Optional[Dict[str, Any]] = None
        live = rec.get("_live_bucket")
        if live is not None and live["bucket_epoch"] != bucket_epoch:
            completed_bucket = live
            live = None

        if live is None:
            live = {
                "worker_id": rec["worker_id"],
                "bucket_epoch": bucket_epoch,
                "requests_delta": 0,
                "errors_delta": 0,
                "p2p_delta": 0,
                "avg_latency_ms": float(stats.get("avg_latency_ms") or 0.0),
                "p95_latency_ms": float(stats.get("p95_latency_ms") or 0.0),
                "active_tasks_max": int(rec.get("active_tasks") or 0),
                "cpu_percent": stats.get("cpu_percent"),
                "memory_mb": stats.get("memory_mb"),
                "gpu_memory_mb": stats.get("gpu_memory_mb"),
                "_dirty": False,
            }
            rec["_live_bucket"] = live

        if req_delta > 0 or err_delta > 0 or p2p_delta > 0:
            live["requests_delta"] += req_delta
            live["errors_delta"] += err_delta
            live["p2p_delta"] += p2p_delta
            live["_dirty"] = True

        live["avg_latency_ms"] = float(stats.get("avg_latency_ms") or live["avg_latency_ms"])
        live["p95_latency_ms"] = max(
            float(live["p95_latency_ms"]),
            float(stats.get("p95_latency_ms") or 0.0),
        )
        live["active_tasks_max"] = max(int(live["active_tasks_max"]), int(rec.get("active_tasks") or 0))
        if stats.get("cpu_percent") is not None:
            live["cpu_percent"] = stats.get("cpu_percent")
        if stats.get("memory_mb") is not None:
            live["memory_mb"] = stats.get("memory_mb")
        if stats.get("gpu_memory_mb") is not None:
            live["gpu_memory_mb"] = stats.get("gpu_memory_mb")

        return completed_bucket

    def _persist_bucket(self, conn: Any, bucket: Optional[Dict[str, Any]]) -> None:
        if not bucket:
            return
        conn.execute(
            """
            INSERT INTO worker_metrics_history (
                worker_id, bucket_epoch, requests_delta, errors_delta, p2p_delta,
                avg_latency_ms, p95_latency_ms, active_tasks_max,
                cpu_percent, memory_mb, gpu_memory_mb
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(worker_id, bucket_epoch) DO UPDATE SET
                requests_delta = worker_metrics_history.requests_delta + excluded.requests_delta,
                errors_delta = worker_metrics_history.errors_delta + excluded.errors_delta,
                p2p_delta = worker_metrics_history.p2p_delta + excluded.p2p_delta,
                avg_latency_ms = excluded.avg_latency_ms,
                p95_latency_ms = MAX(worker_metrics_history.p95_latency_ms, excluded.p95_latency_ms),
                active_tasks_max = MAX(worker_metrics_history.active_tasks_max, excluded.active_tasks_max),
                cpu_percent = COALESCE(excluded.cpu_percent, worker_metrics_history.cpu_percent),
                memory_mb = COALESCE(excluded.memory_mb, worker_metrics_history.memory_mb),
                gpu_memory_mb = COALESCE(excluded.gpu_memory_mb, worker_metrics_history.gpu_memory_mb)
            """,
            (
                bucket["worker_id"],
                bucket["bucket_epoch"],
                bucket["requests_delta"],
                bucket["errors_delta"],
                bucket["p2p_delta"],
                bucket["avg_latency_ms"],
                bucket["p95_latency_ms"],
                bucket["active_tasks_max"],
                bucket["cpu_percent"],
                bucket["memory_mb"],
                bucket["gpu_memory_mb"],
            ),
        )
        # Reset flushed deltas so subsequent flushes within the same 5-min window don't double-add
        bucket["requests_delta"] = 0
        bucket["errors_delta"] = 0
        bucket["p2p_delta"] = 0
        bucket["_dirty"] = False

    def _maybe_prune_history(self, conn: Any, db_key: str, now: float) -> None:
        last_prune = self._last_prune_by_db.get(db_key, 0.0)
        if (now - last_prune) < 3600.0:
            return
        cutoff = int(now) - METRICS_RETENTION_SEC
        conn.execute("DELETE FROM worker_metrics_history WHERE bucket_epoch < ?", (cutoff,))
        self._last_prune_by_db[db_key] = now

    def _persist_upsert(
        self,
        db_key: str,
        rec: Dict[str, Any],
        now: float,
        *,
        completed_bucket: Optional[Dict[str, Any]] = None,
        flush_live_bucket: bool = True,
    ) -> None:
        conn = job_db._get_conn()
        conn.execute(
            """
            INSERT INTO worker_services (
                worker_id, worker_name, service_type, service_version,
                machine_id, host_name, os_info, endpoints_json, observed_ip,
                capabilities_json, worker_status, status_detail,
                max_concurrency, active_tasks, queue_depth, heartbeat_interval,
                stats_json, started_at, first_registered_at, last_heartbeat_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
                worker_name = excluded.worker_name,
                service_type = excluded.service_type,
                service_version = excluded.service_version,
                machine_id = excluded.machine_id,
                host_name = excluded.host_name,
                os_info = excluded.os_info,
                endpoints_json = excluded.endpoints_json,
                observed_ip = excluded.observed_ip,
                capabilities_json = excluded.capabilities_json,
                worker_status = excluded.worker_status,
                status_detail = excluded.status_detail,
                max_concurrency = excluded.max_concurrency,
                active_tasks = excluded.active_tasks,
                queue_depth = excluded.queue_depth,
                heartbeat_interval = excluded.heartbeat_interval,
                stats_json = excluded.stats_json,
                started_at = excluded.started_at,
                last_heartbeat_at = excluded.last_heartbeat_at,
                updated_at = excluded.updated_at
            """,
            (
                rec["worker_id"],
                rec["worker_name"],
                rec["service_type"],
                rec["service_version"],
                rec["machine_id"],
                rec["host_name"],
                rec["os_info"],
                json.dumps(rec["endpoints"], separators=(",", ":")),
                rec["observed_ip"],
                json.dumps(rec["capabilities"], separators=(",", ":")),
                rec["worker_status"],
                rec["status_detail"],
                rec["max_concurrency"],
                rec["active_tasks"],
                rec["queue_depth"],
                rec["heartbeat_interval"],
                json.dumps(rec["stats"], separators=(",", ":")),
                rec["started_at"],
                rec["first_registered_at"],
                rec["last_heartbeat_at"],
                rec["updated_at"],
            ),
        )
        if completed_bucket is not None:
            self._persist_bucket(conn, completed_bucket)
        if flush_live_bucket and rec.get("_live_bucket") is not None:
            self._persist_bucket(conn, rec["_live_bucket"])
        self._maybe_prune_history(conn, db_key, now)
        conn.commit()
        rec["_last_db_flush_at"] = float(rec["last_heartbeat_at"])

    def _persist_heartbeat_and_stats(
        self,
        db_key: str,
        rec: Dict[str, Any],
        now: float,
        *,
        completed_bucket: Optional[Dict[str, Any]] = None,
    ) -> None:
        conn = job_db._get_conn()
        conn.execute(
            """
            UPDATE worker_services
               SET active_tasks = ?,
                   queue_depth = ?,
                   stats_json = ?,
                   last_heartbeat_at = ?,
                   updated_at = ?
             WHERE worker_id = ?
            """,
            (
                rec["active_tasks"],
                rec["queue_depth"],
                json.dumps(rec["stats"], separators=(",", ":")),
                rec["last_heartbeat_at"],
                rec["updated_at"],
                rec["worker_id"],
            ),
        )
        if completed_bucket is not None:
            self._persist_bucket(conn, completed_bucket)
        if rec.get("_live_bucket") is not None:
            self._persist_bucket(conn, rec["_live_bucket"])
        self._maybe_prune_history(conn, db_key, now)
        conn.commit()
        rec["_last_db_flush_at"] = float(rec["last_heartbeat_at"])

    # ============================================================
    # Public Mutations & Queries
    # ============================================================

    def register(
        self,
        payload: Dict[str, Any],
        *,
        observed_ip: Optional[str] = None,
    ) -> Dict[str, Any]:
        worker_id = validate_worker_id(payload.get("worker_id"))
        worker_name = validate_worker_name(payload.get("worker_name"))
        service_type = validate_service_type(payload.get("service_type"))
        service_version = validate_service_version(payload.get("service_version"))
        machine_id = validate_optional_machine_id(payload.get("machine_id"))
        host_name = sanitize_short_text(payload.get("host_name"), max_len=128)
        os_info = sanitize_short_text(payload.get("os_info"), max_len=128)
        endpoints = normalize_endpoints(payload.get("endpoints"))
        capabilities = normalize_capabilities(payload.get("capabilities"))
        worker_status = validate_worker_status(payload.get("worker_status"), default="serving")
        status_detail = sanitize_short_text(payload.get("status_detail"), max_len=256)
        max_concurrency = validate_bounded_int(
            payload.get("max_concurrency"), "max_concurrency", default=1, min_val=1, max_val=10_000
        )
        active_tasks = validate_bounded_int(
            payload.get("active_tasks"), "active_tasks", default=0, min_val=0, max_val=100_000
        )
        queue_depth = validate_bounded_int(
            payload.get("queue_depth"), "queue_depth", default=0, min_val=0, max_val=1_000_000
        )
        heartbeat_interval = validate_heartbeat_interval(payload.get("heartbeat_interval"))
        norm_ip = normalize_observed_ip(observed_ip)

        now = self._clock()
        raw_started = payload.get("started_at")
        try:
            started_at = float(raw_started) if raw_started is not None else now
            if started_at <= 0 or started_at > now + 60:
                started_at = now
        except (TypeError, ValueError):
            started_at = now

        secret = job_db.get_peer_signing_secret()
        lease_token = make_worker_lease_token(worker_id, service_type, secret)
        peer_token, peer_exp = make_peer_auth_token(worker_id, service_type, secret, now=now)

        with self._lock:
            db_key, cache = self._get_cache()
            existing = cache.get(worker_id)
            first_reg = float(existing["first_registered_at"]) if existing else now
            merged_stats = normalize_stats(
                payload.get("stats"),
                existing["stats"] if existing and payload.get("stats") is None else None,
            )

            rec: Dict[str, Any] = {
                "worker_id": worker_id,
                "worker_name": worker_name,
                "service_type": service_type,
                "service_version": service_version,
                "machine_id": machine_id if machine_id is not None else (existing["machine_id"] if existing else None),
                "host_name": host_name or (existing["host_name"] if existing else ""),
                "os_info": os_info or (existing["os_info"] if existing else ""),
                "endpoints": endpoints,
                "observed_ip": norm_ip if norm_ip is not None else (existing["observed_ip"] if existing else None),
                "capabilities": capabilities,
                "worker_status": worker_status,
                "status_detail": status_detail,
                "max_concurrency": max_concurrency,
                "active_tasks": active_tasks,
                "queue_depth": queue_depth,
                "heartbeat_interval": heartbeat_interval,
                "stats": merged_stats,
                "started_at": started_at,
                "first_registered_at": first_reg,
                "last_heartbeat_at": now,
                "updated_at": now,
                "_last_db_flush_at": now,
                "_rollup_prev_total": int(existing["_rollup_prev_total"]) if existing else 0,
                "_rollup_prev_failed": int(existing["_rollup_prev_failed"]) if existing else 0,
                "_rollup_prev_p2p": int(existing["_rollup_prev_p2p"]) if existing else 0,
                "_live_bucket": existing.get("_live_bucket") if existing else None,
            }
            completed_bucket = self._update_live_bucket(rec, now)
            cache[worker_id] = rec
            self._bump_revision(db_key)
            self._persist_upsert(db_key, rec, now, completed_bucket=completed_bucket)

            all_views = [compute_worker_view(r, now) for r in cache.values()]
            rev, etag = self._compute_etag(db_key, all_views)
            view = compute_worker_view(rec, now)

        return {
            "ok": True,
            "worker": view,
            "heartbeat_interval": heartbeat_interval,
            "worker_lease_token": lease_token,
            "peer_auth_token": peer_token,
            "peer_token_expires_at": peer_exp,
            "peer_verify_secret": secret,
            "registry_revision": rev,
            "etag": etag,
        }

    def heartbeat(
        self,
        payload: Dict[str, Any],
        *,
        observed_ip: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        worker_id = validate_worker_id(payload.get("worker_id"))
        has_status = "worker_status" in payload and payload.get("worker_status") is not None
        new_status = validate_worker_status(payload.get("worker_status")) if has_status else None
        has_detail = "status_detail" in payload and payload.get("status_detail") is not None
        new_detail = sanitize_short_text(payload.get("status_detail"), max_len=256) if has_detail else None
        has_active = "active_tasks" in payload and payload.get("active_tasks") is not None
        new_active = (
            validate_bounded_int(payload.get("active_tasks"), "active_tasks", default=0)
            if has_active
            else None
        )
        has_queue = "queue_depth" in payload and payload.get("queue_depth") is not None
        new_queue = (
            validate_bounded_int(payload.get("queue_depth"), "queue_depth", default=0)
            if has_queue
            else None
        )
        has_max_conc = "max_concurrency" in payload and payload.get("max_concurrency") is not None
        new_max_conc = (
            validate_bounded_int(payload.get("max_concurrency"), "max_concurrency", default=1, min_val=1)
            if has_max_conc
            else None
        )
        has_endpoints = "endpoints" in payload and payload.get("endpoints") is not None
        new_endpoints = normalize_endpoints(payload.get("endpoints")) if has_endpoints else None
        has_caps = "capabilities" in payload and payload.get("capabilities") is not None
        new_caps = normalize_capabilities(payload.get("capabilities")) if has_caps else None
        has_stats = "stats" in payload and payload.get("stats") is not None
        norm_ip = normalize_observed_ip(observed_ip)

        now = self._clock()
        secret = job_db.get_peer_signing_secret()

        with self._lock:
            db_key, cache = self._get_cache()
            rec = cache.get(worker_id)
            if rec is None:
                return None

            routing_changed = False
            metadata_changed = False

            if new_status is not None and new_status != rec["worker_status"]:
                rec["worker_status"] = new_status
                metadata_changed = True
                routing_changed = True
            if new_detail is not None and new_detail != rec["status_detail"]:
                rec["status_detail"] = new_detail
                metadata_changed = True
            if new_max_conc is not None and new_max_conc != rec["max_concurrency"]:
                rec["max_concurrency"] = new_max_conc
                metadata_changed = True
                routing_changed = True
            if new_endpoints is not None and new_endpoints != rec["endpoints"]:
                rec["endpoints"] = new_endpoints
                metadata_changed = True
                routing_changed = True
            if new_caps is not None and new_caps != rec["capabilities"]:
                rec["capabilities"] = new_caps
                metadata_changed = True
                routing_changed = True
            if norm_ip is not None and norm_ip != rec["observed_ip"]:
                rec["observed_ip"] = norm_ip
                metadata_changed = True

            if new_active is not None:
                rec["active_tasks"] = new_active
            if new_queue is not None:
                rec["queue_depth"] = new_queue
            if has_stats:
                rec["stats"] = normalize_stats(payload.get("stats"), rec["stats"])

            rec["last_heartbeat_at"] = now
            rec["updated_at"] = now
            completed_bucket = self._update_live_bucket(rec, now)

            if routing_changed:
                self._bump_revision(db_key)

            if metadata_changed:
                self._persist_upsert(db_key, rec, now, completed_bucket=completed_bucket)
            elif (
                completed_bucket is not None
                or (now - float(rec.get("_last_db_flush_at", 0.0))) >= self._db_flush_interval_sec
            ):
                self._persist_heartbeat_and_stats(db_key, rec, now, completed_bucket=completed_bucket)

            peer_token, peer_exp = make_peer_auth_token(
                rec["worker_id"], rec["service_type"], secret, now=now
            )
            all_views = [compute_worker_view(r, now) for r in cache.values()]
            rev, etag = self._compute_etag(db_key, all_views)

        return {
            "peer_auth_token": peer_token,
            "peer_token_expires_at": peer_exp,
            "registry_revision": rev,
            "etag": etag,
        }

    def deregister(self, payload: Dict[str, Any]) -> bool:
        worker_id = validate_worker_id(payload.get("worker_id"))
        detail = sanitize_short_text(payload.get("status_detail", "worker stopped"), max_len=256)
        has_stats = "stats" in payload and payload.get("stats") is not None

        now = self._clock()
        with self._lock:
            db_key, cache = self._get_cache()
            rec = cache.get(worker_id)
            if rec is None:
                return False

            if has_stats:
                rec["stats"] = normalize_stats(payload.get("stats"), rec["stats"])
            rec["worker_status"] = "stopped"
            rec["active_tasks"] = 0
            rec["queue_depth"] = 0
            if detail:
                rec["status_detail"] = detail
            rec["last_heartbeat_at"] = now
            rec["updated_at"] = now

            completed_bucket = self._update_live_bucket(rec, now)
            self._bump_revision(db_key)
            self._persist_upsert(db_key, rec, now, completed_bucket=completed_bucket, flush_live_bucket=True)
            return True

    def delete(self, worker_id: str) -> bool:
        wid = validate_worker_id(worker_id)
        with self._lock:
            db_key, cache = self._get_cache()
            removed = cache.pop(wid, None)
            conn = job_db._get_conn()
            cur = conn.execute("DELETE FROM worker_services WHERE worker_id = ?", (wid,))
            conn.execute("DELETE FROM worker_metrics_history WHERE worker_id = ?", (wid,))
            conn.commit()
            if removed is not None or cur.rowcount > 0:
                self._bump_revision(db_key)
                return True
            return False

    def verify_worker_admission(
        self,
        worker_id: Optional[str],
        lease_token: Optional[str],
        *,
        expected_service_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Enforce mandatory registration + valid lease + healthy heartbeat for worker endpoints."""
        raw_wid = (worker_id or "").strip()
        raw_lease = (lease_token or "").strip()
        if not raw_wid or not raw_lease:
            raise ServiceAdmissionError(
                "Worker service registration is required. Register via POST /api/services/register "
                "and supply X-Worker-Id and X-Worker-Lease headers.",
                status_code=428,
                code="worker_registration_required",
            )

        try:
            wid = validate_worker_id(raw_wid)
        except ServiceValidationError as exc:
            raise ServiceAdmissionError(str(exc), status_code=400, code="invalid_worker_id") from exc

        now = self._clock()
        secret = job_db.get_peer_signing_secret()
        with self._lock:
            _db_key, cache = self._get_cache()
            rec = cache.get(wid)
            if rec is None:
                raise ServiceAdmissionError(
                    f"Worker '{wid}' is not registered with Service Discovery. "
                    "Call POST /api/services/register before invoking worker APIs.",
                    status_code=428,
                    code="worker_not_registered",
                )

            svc_type = rec["service_type"]
            if not verify_worker_lease_token(raw_lease, wid, svc_type, secret):
                raise ServiceAdmissionError(
                    "Invalid X-Worker-Lease token for worker.",
                    status_code=403,
                    code="invalid_worker_lease",
                )

            if expected_service_type and svc_type != expected_service_type.lower():
                raise ServiceAdmissionError(
                    f"Worker '{wid}' is registered for service '{svc_type}', expected '{expected_service_type}'.",
                    status_code=403,
                    code="worker_service_type_mismatch",
                )

            view = compute_worker_view(rec, now)
            if view["health_status"] != "healthy":
                raise ServiceAdmissionError(
                    f"Worker '{wid}' lease is {view['health_status']} (last heartbeat {view['seconds_since_heartbeat']}s ago). "
                    "Send a heartbeat or re-register via /api/services/register.",
                    status_code=428,
                    code="worker_lease_expired",
                )
            return view

    def get_worker_detail(self, worker_id: str, *, hours: int = 24) -> Optional[Dict[str, Any]]:
        wid = validate_worker_id(worker_id)
        hours_clamped = max(1, min(168, int(hours)))
        now = self._clock()
        cutoff_bucket = ((int(now) - hours_clamped * 3600) // METRICS_BUCKET_SEC) * METRICS_BUCKET_SEC

        with self._lock:
            _db_key, cache = self._get_cache()
            rec = cache.get(wid)
            if rec is None:
                return None
            view = compute_worker_view(rec, now)

            conn = job_db._get_conn()
            rows = conn.execute(
                """
                SELECT bucket_epoch, requests_delta, errors_delta, p2p_delta,
                       avg_latency_ms, p95_latency_ms, active_tasks_max,
                       cpu_percent, memory_mb, gpu_memory_mb
                  FROM worker_metrics_history
                 WHERE worker_id = ? AND bucket_epoch >= ?
                 ORDER BY bucket_epoch ASC
                """,
                (wid, cutoff_bucket),
            ).fetchall()

            by_bucket: Dict[int, Dict[str, Any]] = {}
            for r in rows:
                d = dict(r)
                d["bucket_iso"] = _epoch_to_iso_utc(float(d["bucket_epoch"]))
                by_bucket[int(d["bucket_epoch"])] = d

            live = rec.get("_live_bucket")
            if live is not None and int(live["bucket_epoch"]) >= cutoff_bucket:
                b_ep = int(live["bucket_epoch"])
                if b_ep in by_bucket:
                    merged = by_bucket[b_ep]
                    merged["requests_delta"] += int(live["requests_delta"])
                    merged["errors_delta"] += int(live["errors_delta"])
                    merged["p2p_delta"] += int(live["p2p_delta"])
                    merged["avg_latency_ms"] = float(live["avg_latency_ms"])
                    merged["p95_latency_ms"] = max(float(merged["p95_latency_ms"]), float(live["p95_latency_ms"]))
                    merged["active_tasks_max"] = max(int(merged["active_tasks_max"]), int(live["active_tasks_max"]))
                    if live["cpu_percent"] is not None:
                        merged["cpu_percent"] = live["cpu_percent"]
                    if live["memory_mb"] is not None:
                        merged["memory_mb"] = live["memory_mb"]
                    if live["gpu_memory_mb"] is not None:
                        merged["gpu_memory_mb"] = live["gpu_memory_mb"]
                else:
                    by_bucket[b_ep] = {
                        "bucket_epoch": b_ep,
                        "bucket_iso": _epoch_to_iso_utc(float(b_ep)),
                        "requests_delta": int(live["requests_delta"]),
                        "errors_delta": int(live["errors_delta"]),
                        "p2p_delta": int(live["p2p_delta"]),
                        "avg_latency_ms": float(live["avg_latency_ms"]),
                        "p95_latency_ms": float(live["p95_latency_ms"]),
                        "active_tasks_max": int(live["active_tasks_max"]),
                        "cpu_percent": live["cpu_percent"],
                        "memory_mb": live["memory_mb"],
                        "gpu_memory_mb": live["gpu_memory_mb"],
                    }

            history = [by_bucket[k] for k in sorted(by_bucket.keys())]

        return {
            "server_time": int(now),
            "server_time_iso": _epoch_to_iso_utc(now),
            "worker": view,
            "history_bucket_sec": METRICS_BUCKET_SEC,
            "history": history,
        }

    def list_services(
        self,
        *,
        service_type: Optional[str] = None,
        status_filter: Optional[str] = None,
        health_filter: Optional[str] = None,
        capability: Optional[str] = None,
        machine_id: Optional[str] = None,
        query: Optional[str] = None,
        healthy_only: bool = False,
    ) -> Dict[str, Any]:
        now = self._clock()
        with self._lock:
            db_key, cache = self._get_cache()
            all_views = [compute_worker_view(rec, now) for rec in cache.values()]
            rev, etag = self._compute_etag(db_key, all_views)

        all_views.sort(key=lambda w: (w["service_type"], w["worker_name"].lower(), w["worker_id"]))

        # Build Service Catalog grouped by service_type
        catalog_map: Dict[str, Dict[str, Any]] = {}
        for w in all_views:
            stype = w["service_type"]
            entry = catalog_map.get(stype)
            if entry is None:
                entry = {
                    "service_type": stype,
                    "total_workers": 0,
                    "healthy_workers": 0,
                    "routable_workers": 0,
                    "busy_workers": 0,
                    "active_tasks": 0,
                    "total_capacity": 0,
                    "queue_depth": 0,
                    "total_requests": 0,
                    "succeeded_requests": 0,
                    "failed_requests": 0,
                    "p2p_requests": 0,
                    "requests_per_min": 0.0,
                    "_latency_weighted_sum": 0.0,
                    "_latency_weight": 0,
                    "max_p95_latency_ms": 0.0,
                    "versions": set(),
                    "operations": set(),
                    "languages": set(),
                }
                catalog_map[stype] = entry

            entry["total_workers"] += 1
            entry["versions"].add(w["service_version"])
            caps = w["capabilities"]
            if isinstance(caps.get("operations"), list):
                entry["operations"].update(str(x) for x in caps["operations"])
            if isinstance(caps.get("languages"), list):
                entry["languages"].update(str(x) for x in caps["languages"])

            if w["health_status"] == "healthy":
                entry["healthy_workers"] += 1
                entry["active_tasks"] += w["active_tasks"]
                entry["total_capacity"] += w["max_concurrency"]
                entry["queue_depth"] += w["queue_depth"]
                if w["effective_status"] == "busy":
                    entry["busy_workers"] += 1
            if w["is_routable"]:
                entry["routable_workers"] += 1

            st = w["stats"]
            w_req = int(st.get("total_requests") or 0)
            entry["total_requests"] += w_req
            entry["succeeded_requests"] += int(st.get("succeeded_requests") or 0)
            entry["failed_requests"] += int(st.get("failed_requests") or 0)
            entry["p2p_requests"] += int(st.get("p2p_requests") or 0)
            if w["health_status"] == "healthy":
                entry["requests_per_min"] = round(
                    entry["requests_per_min"] + float(st.get("requests_per_min") or 0.0), 2
                )
            avg_lat = float(st.get("avg_latency_ms") or 0.0)
            if w_req > 0 and avg_lat > 0:
                entry["_latency_weighted_sum"] += avg_lat * w_req
                entry["_latency_weight"] += w_req
            entry["max_p95_latency_ms"] = max(
                entry["max_p95_latency_ms"], float(st.get("p95_latency_ms") or 0.0)
            )

        service_catalog: List[Dict[str, Any]] = []
        for stype in sorted(catalog_map.keys()):
            c = catalog_map[stype]
            w_sum = c.pop("_latency_weighted_sum")
            w_cnt = c.pop("_latency_weight")
            c["avg_latency_ms"] = round(w_sum / w_cnt, 2) if w_cnt > 0 else 0.0
            c["versions"] = sorted(c["versions"])
            c["operations"] = sorted(c["operations"])
            c["languages"] = sorted(c["languages"])
            if c["healthy_workers"] == 0:
                c["service_status"] = "offline"
            elif c["healthy_workers"] < c["total_workers"]:
                c["service_status"] = "degraded"
            else:
                c["service_status"] = "healthy"
            service_catalog.append(c)

        # Fleet-wide summary KPIs
        total_req_all = sum(int(w["stats"].get("total_requests") or 0) for w in all_views)
        succ_req_all = sum(int(w["stats"].get("succeeded_requests") or 0) for w in all_views)
        fail_req_all = sum(int(w["stats"].get("failed_requests") or 0) for w in all_views)
        p2p_req_all = sum(int(w["stats"].get("p2p_requests") or 0) for w in all_views)
        queue_req_all = sum(int(w["stats"].get("queue_jobs") or 0) for w in all_views)
        healthy_views = [w for w in all_views if w["health_status"] == "healthy"]
        total_cap = sum(w["max_concurrency"] for w in healthy_views)
        active_all = sum(w["active_tasks"] for w in healthy_views)
        queue_all = sum(w["queue_depth"] for w in healthy_views)

        lat_weighted = sum(
            float(w["stats"].get("avg_latency_ms") or 0.0) * int(w["stats"].get("total_requests") or 0)
            for w in all_views
        )
        fleet_avg_lat = round(lat_weighted / total_req_all, 2) if total_req_all > 0 else 0.0
        fleet_p95_lat = max(
            (float(w["stats"].get("p95_latency_ms") or 0.0) for w in all_views),
            default=0.0,
        )

        summary = {
            "service_types_total": len(service_catalog),
            "service_types_healthy": sum(1 for c in service_catalog if c["healthy_workers"] > 0),
            "total_workers": len(all_views),
            "healthy_workers": len(healthy_views),
            "serving_or_idle": sum(
                1 for w in all_views if w["effective_status"] in ("serving", "idle")
            ),
            "busy": sum(1 for w in all_views if w["effective_status"] == "busy"),
            "degraded_or_draining": sum(
                1
                for w in all_views
                if w["health_status"] == "healthy"
                and w["worker_status"] in ("degraded", "draining", "error", "starting")
            ),
            "unhealthy": sum(1 for w in all_views if w["health_status"] == "unhealthy"),
            "unresponsive": sum(1 for w in all_views if w["health_status"] == "unresponsive"),
            "offline": sum(1 for w in all_views if w["health_status"] == "offline"),
            "active_tasks": active_all,
            "total_capacity": total_cap,
            "utilization_pct": round((active_all / total_cap) * 100.0, 1) if total_cap > 0 else 0.0,
            "queue_depth": queue_all,
            "total_requests": total_req_all,
            "succeeded_requests": succ_req_all,
            "failed_requests": fail_req_all,
            "success_rate_pct": round((succ_req_all / total_req_all) * 100.0, 2) if total_req_all > 0 else 100.0,
            "p2p_requests": p2p_req_all,
            "queue_jobs": queue_req_all,
            "p2p_share_pct": round((p2p_req_all / total_req_all) * 100.0, 1) if total_req_all > 0 else 0.0,
            "requests_per_min": round(
                sum(float(w["stats"].get("requests_per_min") or 0.0) for w in healthy_views), 2
            ),
            "avg_latency_ms": fleet_avg_lat,
            "p95_latency_ms": round(fleet_p95_lat, 2),
        }

        filtered = all_views
        if service_type:
            st_norm = service_type.strip().lower()
            filtered = [w for w in filtered if w["service_type"] == st_norm]
        if healthy_only:
            filtered = [w for w in filtered if w["health_status"] == "healthy"]
        if status_filter:
            sf = status_filter.strip().lower()
            filtered = [w for w in filtered if w["effective_status"] == sf]
        if health_filter:
            hf = health_filter.strip().lower()
            filtered = [w for w in filtered if w["health_status"] == hf]
        if machine_id:
            mid = machine_id.strip()
            filtered = [w for w in filtered if w["machine_id"] == mid]
        if capability:
            filtered = [w for w in filtered if _matches_capability(w["capabilities"], capability)]
        if query:
            q = query.strip().lower()
            if q:
                filtered = [
                    w
                    for w in filtered
                    if q in w["worker_name"].lower()
                    or q in w["worker_id"].lower()
                    or q in w["service_type"].lower()
                    or q in (w["host_name"] or "").lower()
                    or q in (w["machine_id"] or "").lower()
                    or q in (w["os_info"] or "").lower()
                    or q in (w["status_detail"] or "").lower()
                    or q in w["effective_status"].lower()
                    or any(q in ep["url"].lower() for ep in w["endpoints"])
                    or _matches_capability(w["capabilities"], q)
                ]

        return {
            "server_time": int(now),
            "server_time_iso": _epoch_to_iso_utc(now),
            "registry_revision": rev,
            "etag": etag,
            "summary": summary,
            "service_catalog": service_catalog,
            "workers": filtered,
        }

    def discover_service_peers(
        self,
        service_type: str,
        *,
        capability: Optional[str] = None,
        exclude_worker_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fast-path discovery returning healthy, routable worker replicas ranked by load & latency."""
        stype = validate_service_type(service_type)
        now = self._clock()
        with self._lock:
            db_key, cache = self._get_cache()
            all_views = [compute_worker_view(rec, now) for rec in cache.values()]
            rev, etag = self._compute_etag(db_key, all_views)

        candidates = [
            w
            for w in all_views
            if w["service_type"] == stype
            and w["is_routable"]
            and (not exclude_worker_id or w["worker_id"] != exclude_worker_id)
            and (not capability or _matches_capability(w["capabilities"], capability))
        ]

        candidates.sort(
            key=lambda w: (
                0 if w["available_slots"] > 0 else 1,
                w["load_ratio"],
                float(w["stats"].get("p50_latency_ms") or w["stats"].get("avg_latency_ms") or 0.0),
                w["worker_id"],
            )
        )

        return {
            "server_time": int(now),
            "service_type": stype,
            "registry_revision": rev,
            "etag": etag,
            "peers": candidates,
        }
